"""``sanctum tailnet`` — the Tailscale toolkit (diagnose / apply / creds / ssh).

This is the impure boundary for the tailnet group: every probe (ifconfig, the
``tailscale`` CLI, a TCP connect, the Tailscale API, the keychain) lives here
behind a module-level seam the tests monkeypatch, and the pure verdict logic
lives in :mod:`sanctum_cli.net.tailnet`. Doctrine mirrors ``sanctum net``:
``doctor`` is read-only; ``apply`` is dry-run by default and only pushes behind
``--apply`` with backup + honest post-verify; credentials never flow through
argv where avoidable and are never logged.

The group makes real the commands ``tailnet/apply-acl.sh`` already advertises
(``sanctum tailnet setup``/``apply``) and folds the ACL-push contract, the
credential bootstrap (across the secrets trifecta, reusing the onboard seam),
and a connect wrapper into one first-class surface.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, TypeVar

import typer
import yaml
from rich.console import Console
from rich.markup import escape

from sanctum_cli import config, keychain
from sanctum_cli.errors import (
    LocalError,
    NetworkError,
    ProviderError,
    SanctumError,
    UserError,
)
from sanctum_cli.net.status import RowStatus, StatusReport
from sanctum_cli.net.tailnet import (
    AclDrift,
    CredState,
    PeerReach,
    SpineState,
    TrifectaState,
    build_tailnet_report,
    diff_acl,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import NoReturn

console = Console()
err_console = Console(stderr=True)
tailnet_app = typer.Typer(
    help="Tailscale tailnet: diagnose, apply the ACL, bootstrap credentials, connect."
)

# ── constants ────────────────────────────────────────────────────────────────

_API = "https://api.tailscale.com/api/v2"
_TAILNET = "-"  # '-' = the authenticated user's default tailnet
_OAUTH_URL = "https://login.tailscale.com/admin/settings/oauth"

_KC_ACCOUNT = "sanctum"
_KC_OAUTH_ID = "tailscale-oauth-client-id"
_KC_OAUTH_SECRET = "tailscale-oauth-secret"
_KC_API_KEY = "tailscale-api-key"

_TS_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_DEFAULT_PEER = "berts-mbp"
_DEFAULT_ACL_PATH = Path("~/Projects/Claude_Code/tailnet/acl.hujson").expanduser()

_HTTP_TIMEOUT_S = 20
_OAUTH_TIMEOUT_S = 15

_P = TypeVar("_P")


# ── error reporting (mirrors commands.net._report) ─────────────────────────────


def _report(exc: SanctumError) -> None:
    err_console.print(f"[bold red]error:[/] {escape(exc.message)}")
    if exc.fix:
        err_console.print(f"[dim]fix:[/] {escape(exc.fix)}")


def _fail(exc: SanctumError) -> NoReturn:
    """Print a SanctumError + its fix and exit with its code (lets mypy narrow)."""
    _report(exc)
    raise typer.Exit(code=int(exc.exit_code))


def _safe_probe(probe: Callable[[], _P]) -> _P | None:
    """Run a probe seam; any failure → None (fail-open → UNKNOWN row, never a crash)."""
    try:
        return probe()
    except Exception:
        return None


# ── HTTP + credential seams ────────────────────────────────────────────────────


def _api_request(
    method: str,
    path: str,
    token: str,
    *,
    body: str | None = None,
    content_type: str | None = None,
    accept: str | None = None,
) -> tuple[int, str]:
    """One Tailscale API call with ``token:`` basic auth. Returns (status, text).

    Never raises — a transport failure yields ``(0, "")`` and an HTTP error yields
    its real status + body — so the callers classify on a code, not an exception.
    The token is sent only in the Authorization header, never on argv or logged.
    """
    auth = base64.b64encode(f"{token}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    if content_type:
        headers["Content-Type"] = content_type
    if accept:
        headers["Accept"] = accept
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{_API}/{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return (getattr(resp, "status", 200), resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return (0, "")


def _mint_oauth_token(client_id: str, client_secret: str) -> str | None:
    """Mint a 1-hour access token from an OAuth client (never expires → nothing to
    rotate). Returns the token, or None on any failure. Creds ride the POST body."""
    data = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": client_secret}
    ).encode("utf-8")
    req = urllib.request.Request(f"{_API}/oauth/token", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_OAUTH_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    return token if isinstance(token, str) and token else None


def _kc_get(service: str) -> str | None:
    """Read a keychain value (account ``sanctum``), None on missing/locked/error."""
    try:
        return keychain.read(_KC_ACCOUNT, service)
    except LocalError:  # KeychainEntryMissingError / KeychainLockedError subclass this
        return None


def _resolve_token() -> tuple[str | None, str]:
    """Resolve a Tailscale API bearer token, PREFERRING a non-expiring OAuth client
    (mint a fresh 1h token), falling back to a legacy API key. Mirrors the order in
    ``apply-acl.sh:key()``. Returns (token_or_None, human source label)."""
    env_id = os.environ.get("TS_OAUTH_CLIENT_ID")
    env_secret = os.environ.get("TS_OAUTH_SECRET")
    client_id = env_id or _kc_get(_KC_OAUTH_ID)
    client_secret = env_secret or _kc_get(_KC_OAUTH_SECRET)
    if client_id and client_secret:
        token = _mint_oauth_token(client_id, client_secret)
        source = "env oauth" if env_id else "keychain oauth"
        return (token, source if token else "oauth mint failed")
    env_key = os.environ.get("TS_API_KEY")
    if env_key:
        return (env_key, "env api-key")
    api_key = _kc_get(_KC_API_KEY)
    if api_key:
        return (api_key, "keychain api-key")
    return (None, "none")


# ── tailscale-CLI + connectivity seams ─────────────────────────────────────────


def _tailscale_bin() -> str | None:
    """Resolve the ``tailscale`` CLI (PATH, then the macOS app bundle), or None."""
    return shutil.which("tailscale") or (_TS_APP if Path(_TS_APP).exists() else None)


def _magicdns_suffix() -> str:
    """The local MagicDNS suffix (``tailXXXX.ts.net``), or "" — reuses the ha_green seam."""
    from sanctum_cli.devices import ha_green

    return ha_green._tailnet_suffix()


def _peer_fqdn(peer: str) -> str:
    """Qualify a bare peer name with the MagicDNS suffix (``peer.tailXXXX.ts.net``)."""
    if "." in peer:
        return peer
    suffix = _magicdns_suffix()
    return f"{peer}.{suffix}" if suffix else peer


def _disco_ping(peer: str) -> bool | None:
    """Tailscale disco ping (is the overlay up end-to-end?). None if no ``tailscale``."""
    binary = _tailscale_bin()
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [binary, "ping", "-c", "1", "--timeout", "3s", peer],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=8,
            check=False,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    return "pong" in proc.stdout.lower()


def _tcp_open(host: str, port: int) -> bool:
    """Raw TCP connect test (the ACL actually permits the port). Mirrors
    ``commands.net._firewalla_present``. False on any connect failure."""
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _ifconfig_text() -> str:
    from sanctum_cli.net import system

    return system._run(["ifconfig"])


# ── ACL path + providers.yaml custody read ─────────────────────────────────────


def _acl_path() -> Path:
    """The ACL policy path — instance.yaml ``tailnet.acl_path`` else the repo default."""
    configured = config.instance_value("tailnet.acl_path", None)
    return Path(str(configured)).expanduser() if configured else _DEFAULT_ACL_PATH


def _read_local_acl() -> str | None:
    try:
        return _acl_path().read_text(encoding="utf-8")
    except OSError:
        return None


def _providers_row_present() -> bool:
    """Is there a live ``sync_mirrors`` row for the tailscale OAuth cred? (reads
    providers.yaml only — never a secret). A row hands cross-tier custody to the
    daily drift-sync (``secret-rotator/sync.py``)."""
    override = os.environ.get("SANCTUM_PROVIDERS_FILE")
    path = (
        Path(override).expanduser()
        if override
        else Path("~/.sanctum/secret-rotator/providers.yaml").expanduser()
    )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict):
        return False
    mirrors = data.get("sync_mirrors")
    if not isinstance(mirrors, dict):
        return False
    return any("tailscale_oauth" in str(key) for key in mirrors)


# ── probe seams (each returns a pure value object; patched wholesale in tests) ──


def _probe_spine() -> SpineState:
    from sanctum_cli.net import heal

    on_tailnet, _tb5 = heal._spine_from_ifconfig(_ifconfig_text())
    return SpineState(on_tailnet=on_tailnet, suffix=_magicdns_suffix())


def _probe_peer(peer: str) -> PeerReach:
    return PeerReach(peer=peer, ping_ok=_disco_ping(peer), tcp22_open=_tcp_open(_peer_fqdn(peer), 22))


def _probe_cred() -> CredState:
    token, source = _resolve_token()
    if token is None:
        return CredState(http_code=0, source=source)
    code, _ = _api_request("GET", f"tailnet/{_TAILNET}/acl", token, accept="application/hujson")
    return CredState(http_code=code, source=source)


def _probe_drift() -> AclDrift:
    local = _read_local_acl()
    if local is None:
        return AclDrift(in_sync=None, summary="local acl.hujson not found")
    token, _ = _resolve_token()
    if token is None:
        return AclDrift(in_sync=None, summary="no API credential to read the live ACL")
    code, body = _api_request("GET", f"tailnet/{_TAILNET}/acl", token, accept="application/hujson")
    if code != 200:
        return AclDrift(in_sync=None, summary=f"could not read live ACL (HTTP {code})")
    return diff_acl(local, body)


def _probe_trifecta() -> TrifectaState:
    keychain_ok = keychain.exists(_KC_ACCOUNT, _KC_OAUTH_ID) and keychain.exists(
        _KC_ACCOUNT, _KC_OAUTH_SECRET
    )
    # 1P is not probed inline (best-effort tier; an `op` call can prompt/hang) — the
    # keychain + providers.yaml row are the cheap, non-blocking custody signals.
    return TrifectaState(
        keychain=keychain_ok, onepassword=None, providers_row=_providers_row_present()
    )


# ── rendering (mirrors commands.net._render_status) ─────────────────────────────

_ROW_STYLE = {
    RowStatus.OK: ("green", "✓"),
    RowStatus.ATTENTION: ("yellow", "!"),
    RowStatus.DOWN: ("red", "✗"),
    RowStatus.UNKNOWN: ("dim", "?"),
}
_OVERALL_STYLE = {"GREEN": "green", "ATTENTION": "yellow", "DEGRADED": "red"}


def _render(report: StatusReport) -> None:
    from rich.panel import Panel
    from rich.table import Table

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    for row in report.rows:
        style, glyph = _ROW_STYLE[row.status]
        table.add_row(
            f"[{style}]{glyph}[/]",
            f"[bold]{escape(row.label)}[/]",
            f"[{style}]{escape(row.status.name)}[/]",
            f"[dim]{escape(row.detail)}[/]",
        )
    overall_style = _OVERALL_STYLE.get(report.overall, "dim")
    console.print(
        Panel(
            table,
            title=f"Tailnet — [{overall_style}]{escape(report.overall)}[/]",
            title_align="left",
            border_style=overall_style,
        )
    )


def _report_json(report: StatusReport) -> None:
    print(
        json.dumps(
            {
                "overall": report.overall,
                "rows": [
                    {"label": r.label, "status": r.status.name, "detail": r.detail}
                    for r in report.rows
                ],
            },
            indent=2,
        )
    )


# ── commands ────────────────────────────────────────────────────────────────


@tailnet_app.command(
    "doctor",
    help="One-glance tailnet health: spine, peer reach, credential, ACL drift, custody (read-only).",
)
def tailnet_doctor(
    peer: Annotated[
        str, typer.Option("--peer", help="Peer whose reachability to probe.")
    ] = _DEFAULT_PEER,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of the panel.")
    ] = False,
) -> None:
    """Collapse the five tailnet subsystems into ONE read-only pane + verdict.

    Each row is probed behind a guarded seam: a probe that fails degrades ONLY its
    own row to UNKNOWN — the pane always renders. Nothing here mutates or needs
    sudo. The peer row encodes the ACL-gap signal (disco ping OK + TCP :22 filtered
    ⇒ the policy, not the host, is the problem)."""
    report = build_tailnet_report(
        spine=_safe_probe(_probe_spine),
        peer=_safe_probe(lambda: _probe_peer(peer)),
        cred=_safe_probe(_probe_cred),
        drift=_safe_probe(_probe_drift),
        trifecta=_safe_probe(_probe_trifecta),
    )
    if json_output:
        _report_json(report)
        return
    _render(report)


@tailnet_app.command(
    "apply",
    help="Validate + push acl.hujson to the tailnet. Dry-run by default; --apply to push.",
)
def tailnet_apply(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually push (backup → validate → push → verify)."),
    ] = False,
    acl_file: Annotated[
        Path | None,
        typer.Option("--acl", help="ACL policy path (default: instance.yaml tailnet.acl_path)."),
    ] = None,
) -> None:
    """Push the tailnet ACL with a real backup + honest post-verify.

    Dry-run (default) validates against the live tailnet and stops — it fires NO
    mutating push and writes NO backup. ``--apply`` backs up the live ACL, validates,
    pushes, then re-reads the live ACL and confirms it matches local (✓ only from that
    real re-read, not from "the POST returned 200")."""
    path = acl_file.expanduser() if acl_file else _acl_path()
    try:
        local = path.read_text(encoding="utf-8")
    except OSError:
        _fail(
            LocalError(
                f"cannot read ACL policy at {path}",
                fix="pass --acl <path> or set tailnet.acl_path in instance.yaml",
            )
        )
    token, source = _resolve_token()
    if token is None:
        _fail(LocalError("no working Tailscale API credential", fix="run: sanctum tailnet creds"))

    # Validate (non-mutating server-side; safe in dry-run).
    vcode, vbody = _api_request(
        "POST",
        f"tailnet/{_TAILNET}/acl/validate",
        token,
        body=local,
        content_type="application/hujson",
    )
    if vcode == 0:
        _fail(NetworkError("could not reach the Tailscale API to validate"))
    if vcode != 200:
        _fail(
            UserError(
                f"ACL validation FAILED (HTTP {vcode})\n{vbody.strip()}",
                fix=f"fix {path} and retry",
            )
        )
    console.print(f"[green]✓[/] valid — Tailscale accepts {escape(str(path))} [dim][{source}][/]")

    if not apply:
        console.print("[dim]dry-run: no changes made. Re-run with --apply to push.[/]")
        return

    # Backup the live ACL before mutating (best-effort — validate already passed).
    bcode, blive = _api_request(
        "GET", f"tailnet/{_TAILNET}/acl", token, accept="application/hujson"
    )
    if bcode == 200:
        backup = path.parent / f"acl.live-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.hujson"
        try:
            backup.write_text(blive, encoding="utf-8")
            console.print(f"[green]✓[/] backup → {escape(str(backup))}")
        except OSError:
            console.print("[yellow]![/] backup write failed (continuing — push is validated)")
    else:
        console.print(f"[yellow]![/] could not read live ACL for backup (HTTP {bcode}); continuing")

    # Push.
    pcode, pbody = _api_request(
        "POST", f"tailnet/{_TAILNET}/acl", token, body=local, content_type="application/hujson"
    )
    if pcode != 200:
        _fail(ProviderError(f"apply FAILED (HTTP {pcode}) — policy unchanged\n{pbody.strip()}"))
    console.print("[green]✓[/] APPLIED — the new policy is live.")

    # Honest post-verify: re-read the live ACL and confirm it now matches local.
    rcode, rbody = _api_request(
        "GET", f"tailnet/{_TAILNET}/acl", token, accept="application/hujson"
    )
    if rcode == 200 and diff_acl(local, rbody).in_sync:
        console.print("[green]✓[/] verified — live ACL now matches local acl.hujson.")
    else:
        console.print(
            "[yellow]![/] pushed, but could not confirm the live ACL matches local "
            "(re-run: sanctum tailnet doctor)."
        )


@tailnet_app.command(
    "creds",
    help="One-time: create/verify a Tailscale OAuth client, store it across the trifecta.",
)
def tailnet_creds() -> None:
    """Guided OAuth bootstrap — the one irreducible human step (Tailscale needs SSO).

    Opens the OAuth page, prompts for the id/secret, then PROVES both required
    scopes (ACL + Devices) against the live API BEFORE storing anything. On success
    it stores the pair via the onboard trifecta seam: keychain (guaranteed tier —
    what ``apply``/``doctor`` read) + best-effort 1P + a providers.yaml row so the
    daily drift-sync owns cross-tier propagation. Nothing is stored if a scope is
    missing (your keychain is left untouched)."""
    console.print("\n[bold]🔑 Tailscale OAuth — one-time setup (then never rotate)[/]\n")
    console.print("Opening the OAuth clients page. There:")
    console.print("  • click [bold]Generate OAuth client…[/]")
    console.print("  • grant scopes: [bold]ACL (write)[/] AND [bold]Devices Core (write)[/]")
    console.print("  • copy the Client ID and Client secret it shows you\n")
    _open_url(_OAUTH_URL)

    client_id = typer.prompt("  Client ID").strip()
    client_secret = typer.prompt("  Client secret", hide_input=True).strip()
    if not client_id or not client_secret:
        _fail(UserError("both the Client ID and secret are required"))

    console.print("\n[dim]Verifying (minting a test token; nothing stored yet)…[/]")
    token = _mint_oauth_token(client_id, client_secret)
    if token is None:
        _fail(
            UserError(
                "couldn't mint a token — re-check the id/secret "
                "(a stray space is the usual culprit)"
            )
        )

    acl_code, _ = _api_request("GET", f"tailnet/{_TAILNET}/acl", token, accept="application/hujson")
    dev_code, _ = _api_request("GET", f"tailnet/{_TAILNET}/devices", token)
    missing = []
    if acl_code != 200:
        missing.append("ACL (read+write) — needed to push the policy")
    if dev_code != 200:
        missing.append("Devices Core (write) — needed to tag devices")
    if missing:
        bullets = "\n  ".join(f"• {m}" for m in missing)
        _fail(
            UserError(
                f"the OAuth client is missing scope(s):\n  {bullets}",
                fix=f"add the scope(s) at {_OAUTH_URL}, then re-run. Nothing was stored.",
            )
        )
    console.print("[green]✓[/] both scopes confirmed (ACL ✓ Devices ✓) — this credential will work.")

    _store_creds(client_id, client_secret)
    console.print("[green]✓[/] stored in keychain (account: sanctum) + best-effort trifecta mirror.")
    console.print(
        "[dim]Every `sanctum tailnet apply` now mints its own 1-hour token — zero rotation.[/]\n"
    )


@tailnet_app.command(
    "ssh",
    help="SSH to a tailnet peer: tailscale ssh, falling back to direct ssh over MagicDNS.",
)
def tailnet_ssh(
    host: Annotated[str, typer.Argument(help="Peer name (e.g. berts-mbp) or MagicDNS fqdn.")],
    command: Annotated[
        list[str] | None, typer.Argument(help="Optional remote command (default: interactive shell).")
    ] = None,
    user: Annotated[str, typer.Option("--user", "-l", help="Remote user.")] = "bert",
) -> None:
    """Connect robustly: try ``tailscale ssh`` (identity is the tailnet), and on
    failure fall back to a direct ``ssh`` over the MagicDNS name with accept-new host
    keys, a bounded connect timeout, and ``IdentityAgent=none`` (so a stale agent
    can't flood Touch-ID prompts). Fail-soft: on total failure it points at the
    doctor rather than dumping a raw ssh error."""
    remote = command or []
    if _run_tailscale_ssh(host, user, remote) == 0:
        raise typer.Exit(0)
    console.print("[dim]tailscale ssh unavailable/failed — falling back to direct ssh…[/]")
    if _run_direct_ssh(host, user, remote) != 0:
        _fail(
            NetworkError(
                f"could not connect to {host}",
                fix=f"diagnose with: sanctum tailnet doctor --peer {host}",
            )
        )
    raise typer.Exit(0)


# ── command helper seams ────────────────────────────────────────────────────


def _open_url(url: str) -> None:
    import webbrowser

    with contextlib.suppress(Exception):
        webbrowser.open(url)


def _store_creds(client_id: str, client_secret: str) -> None:
    """Store the OAuth id/secret across the trifecta via the onboard seam.

    Reuses ``onboard.store_device_secret`` (keychain guaranteed tier + best-effort
    1P create/edit + a providers.yaml ``sync_mirrors`` row) so the daily drift-sync
    propagates to SOPS afterward — un-retiring the tailscale rows that were never
    provisioned. The keychain services are exactly what ``apply``/``doctor`` read."""
    from sanctum_cli.commands import onboard

    onboard.store_device_secret(service=_KC_OAUTH_ID, account=_KC_ACCOUNT, secret=client_id)
    onboard.store_device_secret(service=_KC_OAUTH_SECRET, account=_KC_ACCOUNT, secret=client_secret)


def _run_tailscale_ssh(host: str, user: str, command: list[str]) -> int:
    binary = _tailscale_bin()
    if binary is None:
        return 1
    try:
        return subprocess.run(
            [binary, "ssh", f"{user}@{host}", *command], check=False
        ).returncode
    except (subprocess.SubprocessError, OSError, ValueError):
        return 1


def _run_direct_ssh(host: str, user: str, command: list[str]) -> int:
    argv = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "IdentityAgent=none",
        f"{user}@{_peer_fqdn(host)}",
        *command,
    ]
    try:
        return subprocess.run(argv, check=False).returncode
    except (subprocess.SubprocessError, OSError, ValueError):
        return 1
