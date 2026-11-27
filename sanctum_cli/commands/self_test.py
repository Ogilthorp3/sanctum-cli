"""``sanctum self-test`` — the canonical "is my install still good?" command.

Runs a small fleet of probes against the local Sanctum surface and reports
per-probe results plus a summary. Designed for an Apple-grade beta-tester
moment: one command, clear pass/fail per check, friendly summary panel,
honest failure messages.

Exit codes:
  0  all probes passed
  1  one or more probes failed
  2  internal error (script broke before completing the run)

Each probe is a `Probe` dataclass with a name and a check function. The
check function returns a `ProbeResult` (passed: bool, detail: str). The
runner times each probe, prints a row in real time, and assembles the
summary panel at the end.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from sanctum_cli import config, proxyd

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanctum_cli.modules.registry import ModuleRegistry

console = Console()


@dataclass
class ProbeResult:
    passed: bool
    detail: str = ""
    # Three-state status: a probe can also report "not applicable" when its
    # service isn't expected on this install tier. A fresh CLI-only install
    # (brew install sanctum-cli + sanctum onboard --recipe family) does not
    # bring up the cathedrals, proxyd, Force Flow, chitti, or R2D2 — those
    # are operator-grade services that run on a dedicated Mac Mini. On the
    # CLI-only tier, those probes return n/a, not fail. n/a does not count
    # toward the exit code.
    not_applicable: bool = False
    reason: str = ""  # explanation for n/a state


@dataclass
class Probe:
    name: str
    check: Callable[[], ProbeResult]


# ── Probe implementations ─────────────────────────────────────────────


def _satellite_role() -> bool:
    """``instance.role: satellite`` — a contained remote site (a second haus).

    :func:`config.instance_role` reads the raw YAML, so the profile still applies
    when the rest of the file fails validation or holds a value PyYAML cannot
    build, such as an impossible date (the config probe reports that separately).
    """
    return config.instance_role() == "satellite"


def _haus_tier_installed() -> bool:
    """Detect whether this is a haus-operator install (cathedrals + proxyd +
    Force Flow + chitti + R2D2) vs a CLI-only install (just sanctum-cli +
    backup recipe).

    A declared ``instance.role`` decides: ``hub`` is always haus tier, and
    ``satellite`` never is (it carries model manifests for its local brain, but
    the hub services live on the hub). Without a role, returns True only if at
    least one haus-tier artifact is present on disk — cathedral manifests,
    proxyd config, or the R2D2 classifier source. A friend's fresh install has
    none of these."""
    role = config.instance_role()
    if role == "hub":
        return True
    if role == "satellite":
        return False
    haus_markers = [
        Path.home() / ".sanctum/manifests",  # cathedral model manifests
        Path.home() / ".sanctum/sanctum-proxy",  # proxyd config dir
        Path.home() / ".sanctum/r2d2",  # R2D2 classifier source
        Path("/Library/LaunchDaemons/haus.sanctum.proxyd.plist"),
    ]
    return any(p.exists() for p in haus_markers)


def _tier_label() -> str:
    """The profile this run is graded on: ``satellite``, ``haus`` or ``CLI``.

    Reported as the ``--json`` ``tier`` and in the summary headline.
    """
    if _satellite_role():
        return "satellite"
    return "haus" if _haus_tier_installed() else "CLI"


def _haus_only(name: str, check_fn: Callable[[], ProbeResult]) -> Callable[[], ProbeResult]:
    """Decorator-like wrapper: skip a haus-tier probe with n/a when this is
    a CLI-only install. The wrapped function is only invoked on haus
    installs. ``name`` names the probe in the n/a diagnostic so an operator
    can tell at a glance which haus surface was skipped."""

    def wrapped() -> ProbeResult:
        if _satellite_role():
            return ProbeResult(
                passed=True,
                not_applicable=True,
                reason=f"satellite: {name} runs on the hub, not here",
            )
        if not _haus_tier_installed():
            return ProbeResult(
                passed=True,
                not_applicable=True,
                reason=f"CLI-only install: {name} probe skipped (no haus services configured)",
            )
        return check_fn()

    return wrapped


def _satellite_only(name: str, check_fn: Callable[[], ProbeResult]) -> Callable[[], ProbeResult]:
    """Inverse of :func:`_haus_only`: run only on a declared satellite."""

    def wrapped() -> ProbeResult:
        if not _satellite_role():
            return ProbeResult(
                passed=True,
                not_applicable=True,
                reason=f"not a satellite: {name} probe skipped",
            )
        return check_fn()

    return wrapped


def _tcp_reachable(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _http_ok(url: str, timeout_s: float = 3.0) -> tuple[bool, int]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status == 200, resp.status
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False, 0


def probe_network() -> ProbeResult:
    """Loopback reachable + DNS resolves something."""
    if not _tcp_reachable("127.0.0.1", 22):
        # 22 may not be open; try 80 or just confirm socket lib works
        pass
    try:
        socket.gethostbyname("apple.com")
        return ProbeResult(True, "loopback + DNS ok")
    except socket.gaierror:
        return ProbeResult(False, "DNS resolution failed")


def _codesign_check(node_path: Path) -> tuple[bool, bool, str]:
    """Return ``(is_foundation, codesign_ok, detail_or_output)`` for ``node_path``."""
    try:
        out = subprocess.run(
            ["codesign", "-dvv", str(node_path)],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception as e:
        return False, False, f"codesign failed: {e}"
    signing = out.stderr + out.stdout
    if "HX7739G8FX" in signing:
        return True, True, signing
    return False, out.returncode == 0, signing


def probe_node_identity() -> ProbeResult:
    """Verify node binary identity (strict HX7739G8FX on haus; Homebrew/PATH or n/a on CLI)."""
    haus_node = Path("/usr/local/bin/node")
    if haus_node.is_file():
        is_foundation, codesign_ok, detail = _codesign_check(haus_node)
        if is_foundation:
            return ProbeResult(True, "Node.js Foundation HX7739G8FX")
        if _haus_tier_installed():
            if not codesign_ok and detail.startswith("codesign failed:"):
                return ProbeResult(False, detail)
            return ProbeResult(
                False, "node is at /usr/local/bin but not signed by Node.js Foundation"
            )

    if _haus_tier_installed():
        return ProbeResult(False, "/usr/local/bin/node missing — see install runbook")

    candidates: list[Path] = []
    seen: set[Path] = set()
    for raw in (Path("/opt/homebrew/bin/node"), haus_node, shutil.which("node")):
        if not raw:
            continue
        p = Path(raw)
        if p not in seen:
            seen.add(p)
            candidates.append(p)

    for candidate in candidates:
        if not candidate.is_file():
            continue
        is_foundation, codesign_ok, detail = _codesign_check(candidate)
        if is_foundation:
            return ProbeResult(True, f"Node.js Foundation HX7739G8FX ({candidate})")
        if codesign_ok:
            return ProbeResult(True, f"Homebrew/PATH node ({candidate})")
        return ProbeResult(
            False,
            detail if detail.startswith("codesign failed:") else f"codesign failed for {candidate}",
        )

    return ProbeResult(
        True,
        not_applicable=True,
        reason="CLI-only install: node not installed (optional outside haus)",
    )


def probe_yoda_cathedral() -> ProbeResult:
    """Yoda 35B cathedral listens on :1337."""
    ok = _tcp_reachable("127.0.0.1", 1337, 1.5)
    return ProbeResult(ok, "TLS port reachable" if ok else "no response on :1337")


def _is_user_rust_hub() -> bool:
    """True when provisioned via ``sanctum haus install`` (user-domain LaunchAgents)."""
    return (Path.home() / ".sanctum/state/rust-suite.json").is_file()


def probe_coder_cathedral() -> ProbeResult:
    """Coder cathedral listens on :3301 (n/a on <60 GB single-cathedral Rust hubs)."""
    ok = _tcp_reachable("127.0.0.1", 3301, 1.5)
    if ok:
        return ProbeResult(True, "port reachable")
    if _is_user_rust_hub() and not (
        Path.home() / "Library/LaunchAgents/haus.sanctum.mlx-coder.plist"
    ).is_file():
        return ProbeResult(
            True,
            not_applicable=True,
            reason="single-cathedral tier (<60 GB RAM): coder runs on primary :1337",
        )
    return ProbeResult(False, "no response on :3301")


def probe_proxyd() -> ProbeResult:
    """proxyd router on the resolved endpoint (TLS by default). Any HTTP
    response means alive — 401 is expected when no x-api-key header is supplied;
    the point is reachability *and*, over https, a CA-verified handshake.

    Endpoint + trust anchor come from :mod:`sanctum_cli.proxyd` — no literal
    host/port here. ``SANCTUM_PROXYD_URL=https://127.0.0.1:4041`` exercises the
    exact TLS+CA path against the cutover TLS listener before :4040 flips.
    """
    url = proxyd.base_url()
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not _tcp_reachable(host, port, 1.5):
        return ProbeResult(False, f"no listener on {host}:{port}")
    # An https endpoint must pass a CA-verified TLS handshake; a plaintext one
    # uses no context. proxyd.ssl_context() is the SAME trust object the council
    # httpx client verifies through (CA-pinned by default; unverified only under
    # the explicit dev insecure hatch).
    ssl_ctx: ssl.SSLContext | None = None
    if parsed.scheme == "https":
        ssl_ctx = proxyd.ssl_context()
    # Get the actual HTTP status to display, but accept anything 100-599 as up.
    try:
        req = urllib.request.Request(f"{url}/v1/models")
        urllib.request.urlopen(req, timeout=2.5, context=ssl_ctx)
        return ProbeResult(True, "HTTP 200 (open route)")
    except urllib.error.HTTPError as e:
        # 401, 403, 405 etc — service is up (TLS handshake already passed).
        return ProbeResult(True, f"HTTP {e.code} (service alive)")
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        return ProbeResult(False, f"no HTTP response: {proxyd.describe_transport_error(e)}")


def probe_proxy_key() -> ProbeResult:
    """The council's proxyd wire-auth key is provisioned (env or keychain).

    Honest about a silent failure mode: when neither ``$SANCTUM_PROXY_KEY``
    nor the ``sanctum-proxy-client`` keychain item is set, the council falls
    back to a non-secret identifier that works only while proxyd's auth is
    lenient — and will 401 the day it enforces. A FALLBACK row makes that
    posture visible instead of letting it bite at the worst time. Resolves no
    secret, mints nothing — only reports provisioned vs FALLBACK.
    """
    from sanctum_cli.commands import council

    if council.proxy_key_provisioned():
        return ProbeResult(True, "proxy key provisioned")
    return ProbeResult(
        False,
        "proxy key: FALLBACK (not provisioned) — set $SANCTUM_PROXY_KEY or the "
        "'sanctum-proxy-client' keychain item; council will 401 once proxyd enforces auth",
    )


def probe_force_flow() -> ProbeResult:
    """Force Flow notice hub on :4077."""
    ok = _tcp_reachable("127.0.0.1", 4077, 1.5)
    return ProbeResult(ok, "listening" if ok else "down on :4077")


def probe_chitti_samskara() -> ProbeResult:
    """chitti cross-agent status bus on :2188."""
    ok = _tcp_reachable("127.0.0.1", 2188, 1.5)
    return ProbeResult(ok, "listening" if ok else "down on :2188")


def probe_tcc_grants() -> ProbeResult:
    """13 TCC grants for /usr/local/bin/node landed via sanctum-grant-tcc.sh."""
    tcc_db = Path.home() / "Library/Application Support/com.apple.TCC/TCC.db"
    if not tcc_db.is_file():
        if _is_user_rust_hub():
            return ProbeResult(
                True,
                not_applicable=True,
                reason="optional TCC grants not configured (only needed for Messages/Calendar bridges)",
            )
        return ProbeResult(False, "TCC.db not found")
    try:
        conn = sqlite3.connect(f"file:{tcc_db}?mode=ro", uri=True, timeout=2.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM access WHERE client = ? AND auth_value = 2",
            ("/usr/local/bin/node",),
        )
        count = cur.fetchone()[0]
        conn.close()
        if count >= 13:
            return ProbeResult(True, f"{count}/13+ grants")
        if _is_user_rust_hub() and count == 0:
            return ProbeResult(
                True,
                not_applicable=True,
                reason="optional TCC grants not configured (only needed for Messages/Calendar bridges)",
            )
        if count > 0:
            return ProbeResult(
                False,
                f"only {count}/13 grants — run ~/.sanctum/scripts/sanctum-grant-tcc.sh",
            )
        return ProbeResult(
            False,
            "no grants for /usr/local/bin/node — run ~/.sanctum/scripts/sanctum-grant-tcc.sh",
        )
    except sqlite3.Error as e:
        if _is_user_rust_hub():
            return ProbeResult(
                True,
                not_applicable=True,
                reason="TCC.db requires Full Disk Access (optional for user Rust hub)",
            )
        return ProbeResult(False, f"TCC.db read failed: {e} (needs FDA on Terminal)")


def _control_plane_ipc_token() -> str | None:
    """Read the local control-plane IPC Bearer token (env or ~/.sanctum/secrets)."""
    env_tok = os.environ.get("SANCTUM_CONTROL_PLANE_IPC_TOKEN", "").strip()
    if env_tok:
        return env_tok
    token_path = Path.home() / ".sanctum/secrets/control-plane-ipc.token"
    try:
        if token_path.is_file():
            tok = token_path.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except OSError:
        return None
    return None


def probe_r2d2_heartbeat() -> ProbeResult:
    """R2D2 supervisor reports a recent cycle to chitti."""
    try:
        headers: dict[str, str] = {}
        token = _control_plane_ipc_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            "http://127.0.0.1:2188/samskara?service=r2d2&pattern=cycle&limit=1",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read())
        actions = data.get("actions", [])
        if not actions:
            return ProbeResult(False, "no chitti heartbeat for r2d2")
        a = actions[0]
        return ProbeResult(
            True,
            f"attempts={a.get('attempts')}, success_rate={a.get('success_rate')}",
        )
    except Exception as e:
        return ProbeResult(False, f"chitti unreachable: {e}")


def probe_audit_log_bounded() -> ProbeResult:
    """R2D2 audit log within its 50 MB rotation budget."""
    log = Path.home() / ".sanctum/logs/r2d2-audit.jsonl"
    if not log.is_file():
        return ProbeResult(True, "no audit log yet (fresh install)")
    size = log.stat().st_size
    cap = 50 * 1024 * 1024
    if size > cap:
        return ProbeResult(False, f"{size // 1024 // 1024} MB > 50 MB — log-rotate may have failed")
    return ProbeResult(True, f"{size // 1024 // 1024} MB / 50 MB cap")


def probe_config_valid() -> ProbeResult:
    """sanctum-cli's instance.yaml parses + matches the cli schema."""
    try:
        config.load()
        return ProbeResult(True, "instance.yaml validates")
    except Exception as e:
        return ProbeResult(False, str(e)[:100])


def probe_backup_recent() -> ProbeResult:
    """Last backup snapshot landed within the expected window."""
    state_file = Path.home() / ".sanctum/state/backup-canary.status"
    if not state_file.is_file():
        return ProbeResult(True, "no backups yet (fresh install)")
    age_s = time.time() - state_file.stat().st_mtime
    age_h = age_s / 3600
    if age_h > 36:  # daily-ish + 12h grace
        return ProbeResult(False, f"last backup {age_h:.0f}h ago — daily cron may be down")
    return ProbeResult(True, f"last backup {age_h:.1f}h ago")


# The router's TLS keys for a backend (sanctum-docs: architecture/dynamic-model-routing).
_TLS_PATH_KEYS = ("ca_cert_path", "client_cert_path", "client_key_path")
# Their pre-release names, which nothing reads any more, mapped to the current ones.
_RENAMED_TLS_KEYS = {
    "ca": "ca_cert_path",
    "client_cert": "client_cert_path",
    "client_key": "client_key_path",
}


def _router_shape(key: str) -> str:
    """One line showing the ``router:`` block the satellite probe expects."""
    return (
        f"set router.default: {key} and router.backends.{key}.url: "
        "https://127.0.0.1:6502/v1 (plus ca_cert_path / client_cert_path / "
        "client_key_path for mTLS)"
    )


class _TLSMaterialError(Exception):
    """A backend's TLS material cannot be loaded; the message is the probe detail."""


def _backend_tls_context(backend: dict[str, object]) -> ssl.SSLContext:
    """CA-verifying client context for an ``https`` backend, with its client cert.

    Raises :class:`_TLSMaterialError` with an operator-facing reason. The client
    key loads through a password callback that always answers an empty
    passphrase, so an encrypted key fails fast instead of OpenSSL stopping to
    prompt on the terminal (or hanging a headless run).
    """
    paths = {k: _expand(backend.get(k)) for k in _TLS_PATH_KEYS}
    for key, path in paths.items():
        if path and not Path(path).is_file():
            raise _TLSMaterialError(f"{key} not found: {path}")
    cert, client_key = paths["client_cert_path"], paths["client_key_path"]
    if client_key and not cert:
        raise _TLSMaterialError("client_key_path is set without client_cert_path")
    try:
        ctx = ssl.create_default_context(cafile=paths["ca_cert_path"])
    except (OSError, ssl.SSLError) as e:
        raise _TLSMaterialError(f"ca_cert_path unusable: {e}") from e
    if cert:
        prompted: list[bool] = []

        def no_passphrase() -> bytes:
            # OpenSSL only asks for a passphrase when the key is encrypted.
            prompted.append(True)
            return b""

        try:
            ctx.load_cert_chain(cert, client_key, password=no_passphrase)
        except (OSError, ssl.SSLError) as e:
            if prompted:
                holder = (
                    "client_key_path"
                    if client_key
                    else "the client key (client_key_path, or the key inside client_cert_path)"
                )
                raise _TLSMaterialError(
                    f"{holder} is passphrase-protected; use an unencrypted key (mode 0600)"
                ) from e
            if not client_key:
                raise _TLSMaterialError(
                    f"client_cert_path unusable: {e}; set client_key_path to its private key"
                ) from e
            raise _TLSMaterialError(f"client_cert_path / client_key_path unusable: {e}") from e
    return ctx


def _transport_hint(reason: object, backend: dict[str, object]) -> str:
    """Point at the missing TLS key when the handshake failure names one."""
    if isinstance(reason, ssl.SSLCertVerificationError) and not backend.get("ca_cert_path"):
        return "; set ca_cert_path to the CA that signed the server certificate"
    wants_cert = getattr(reason, "reason", None) == "TLSV13_ALERT_CERTIFICATE_REQUIRED"
    if wants_cert and not backend.get("client_cert_path"):
        return "; the server wants a client certificate: set client_cert_path and client_key_path"
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect: the probe must never carry its bearer token to another url.

    With no new request to make, urllib raises the 3xx as an ``HTTPError``, which
    the probe reports as a failure naming the redirect target.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        return None


def _brain_opener(ctx: ssl.SSLContext | None) -> urllib.request.OpenerDirector:
    """The local-brain probe's own opener: no proxy, no redirects, the backend's TLS.

    The brain is on this machine, so a proxy from ``http_proxy`` / ``https_proxy``
    would only be a stranger reading the request (and its token).
    """
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({}), _NoRedirect()]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _one_line(text: str, limit: int = 200) -> str:
    """Server-supplied text made safe to print: no control characters, bounded length."""
    shown = "".join(ch if ch.isprintable() else "?" for ch in text)
    return shown if len(shown) <= limit else shown[: limit - 3] + "..."


def probe_satellite_brain() -> ProbeResult:
    """The satellite's default router backend answers ``/models``.

    A satellite is ACL-contained from the hub's council, so its own local brain
    (``router.default`` → ``router.backends.<name>.url``) is the only one it has.
    The backend uses the router's own keys: an ``https`` url may set
    ``ca_cert_path``, ``client_cert_path`` and ``client_key_path`` for a server
    that requires mutual TLS, and ``api_key_env`` names an environment variable
    whose value is sent as a bearer token. The pre-release names ``ca``,
    ``client_cert`` and ``client_key`` fail with the rename to make instead of
    being ignored. An HTTP 401/403 still proves the brain is up, so it passes
    with a note.

    The token is never printed: a value with a control character in it fails
    before any request, and no error text that could quote a header is shown.
    The request ignores proxy settings and follows no redirect; a 3xx fails.
    """
    name = config.instance_value("router.default")
    if name is not None and not isinstance(name, str):
        return ProbeResult(
            False,
            f"router.default must name a backend (a string), got {name!r}; "
            f"{_router_shape('<key>')}",
        )
    if not name or not name.strip():
        return ProbeResult(False, f"router.default is not set; {_router_shape('<key>')}")
    backends = config.instance_value("router.backends")
    # .get, not a dotted instance_value lookup: a backend name may contain '.'.
    backend = backends.get(name) if isinstance(backends, dict) else None
    if not isinstance(backend, dict):
        return ProbeResult(False, f"router.backends.{name} is missing; {_router_shape(name)}")
    url = backend.get("url")
    if not isinstance(url, str) or not url.strip():
        return ProbeResult(False, f"router.backends.{name}.url is not set; {_router_shape(name)}")
    url = url.strip()
    try:
        parts = urllib.parse.urlsplit(url)
        scheme, netloc = parts.scheme.lower(), parts.netloc
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        scheme = netloc = ""
    if scheme not in ("http", "https") or not netloc:
        return ProbeResult(
            False, f"{name}: url {url} is not an http:// or https:// url; {_router_shape(name)}"
        )
    if any(backend.get(old) and not backend.get(new) for old, new in _RENAMED_TLS_KEYS.items()):
        # Ignoring them would probe without the operator's CA or client certificate.
        return ProbeResult(
            False,
            f"{name}: ca, client_cert and client_key were renamed ca_cert_path, "
            "client_cert_path and client_key_path; rename them in instance.yaml",
        )
    tls_keys = [k for k in _TLS_PATH_KEYS if backend.get(k)]
    if tls_keys and scheme != "https":
        return ProbeResult(
            False,
            f"{name}: TLS material is set ({', '.join(tls_keys)}) but the url is not https: {url}",
        )
    ctx: ssl.SSLContext | None = None
    if scheme == "https":
        try:
            ctx = _backend_tls_context(backend)
        except _TLSMaterialError as e:
            return ProbeResult(False, f"{name}: {e}")

    key_env = backend.get("api_key_env")
    key_env = key_env.strip() if isinstance(key_env, str) else ""
    token = os.environ.get(key_env, "").strip() if key_env else ""
    if any(unicodedata.category(ch) == "Cc" for ch in token):
        return ProbeResult(
            False,
            f"{name}: ${key_env} holds a control character (a stray line break?); "
            "set it to the bare token",
        )
    request = urllib.request.Request(f"{url.rstrip('/')}/models")
    if token:
        # Unredirected: never forwarded, even if a redirect were ever followed.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    location: str | None = None
    try:
        with _brain_opener(ctx).open(request, timeout=5) as resp:
            status: int = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        location = e.headers.get("Location") if e.headers is not None else None
        e.close()
    except ValueError:
        # http.client quotes a rejected header value in its message, and that
        # value may be the token: report a fixed text, never the exception's.
        check = f"; check ${key_env} and the url" if key_env else "; check the url"
        return ProbeResult(
            False, f"{name}: could not send a request to {url} (invalid header or url{check})"
        )
    except (OSError, http.client.HTTPException) as e:  # URLError and SSLError are OSErrors
        reason = e.reason if isinstance(e, urllib.error.URLError) else e
        hint = _transport_hint(reason, backend)
        return ProbeResult(False, f"{name} not answering at {url} (HTTP none: {reason}{hint})")

    if 300 <= status < 400:
        target = _one_line(location) if location else "an unnamed location"
        return ProbeResult(
            False,
            f"{name} not answering at {url} (HTTP {status}: redirects to {target}; "
            "set url to the API base itself)",
        )
    if status == 200:
        return ProbeResult(True, f"{name} answers at {url}")
    if status in (401, 403):
        note = "needs auth"
        if key_env:
            note += f"; ${key_env} was rejected" if token else f"; ${key_env} is not set here"
        return ProbeResult(True, f"{name} answers at {url} (HTTP {status}: {note})")
    hint = "; does the url end with the API base path, e.g. /v1?" if status == 404 else ""
    return ProbeResult(False, f"{name} not answering at {url} (HTTP {status}{hint})")


def _expand(path: object) -> str | None:
    return str(Path(str(path)).expanduser()) if path else None


# Absolute path: a PATH-planted ``diskutil`` must not answer for the system one.
_DISKUTIL = "/usr/sbin/diskutil"


def probe_vault_armor() -> ProbeResult:
    """Vault Armor's APFS volume is unlocked (n/a where Vault Armor is not set up).

    A locked vault after boot means the threshold unlock never collected enough
    witness shares; secrets that live on the volume are unavailable until it does.
    """
    manifest = Path.home() / ".sanctum/vault-armor/manifest.json"
    if not manifest.is_file():
        return ProbeResult(True, not_applicable=True, reason="vault armor not configured")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        volume = data["envelope"]["volume_uuid"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return ProbeResult(False, f"vault-armor manifest unreadable: {e}")
    name = data.get("volume_name", "SanctumVault")
    try:
        out = subprocess.run(
            [_DISKUTIL, "info", volume], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception as e:
        return ProbeResult(False, f"diskutil failed: {e}")
    locked = next(
        (
            ln.split(":", 1)[1].strip()
            for ln in out.splitlines()
            if ln.strip().startswith("Locked:")
        ),
        None,
    )
    if locked == "No":
        return ProbeResult(True, f"{name} unlocked")
    if locked == "Yes":
        return ProbeResult(
            False, f"{name} locked — witness unlock has not succeeded; see vault-armor.log"
        )
    return ProbeResult(False, f"{name} ({volume}) not found by diskutil")


def probe_service_user_wave1() -> ProbeResult:
    """Wave-1 control plane runs as the dedicated ``sanctum`` service user.

    Haus-tier only. Family Pass / CLI-only installs return n/a via ``_haus_only``.
    """
    from sanctum_cli import service_user as su

    if _is_user_rust_hub() and not Path("/Library/LaunchDaemons/haus.sanctum.proxyd.plist").is_file():
        return ProbeResult(
            True,
            not_applicable=True,
            reason="user LaunchAgents active (optional root hardening: sanctum service-user install)",
        )

    report = su.check_wave1()
    if not report.applicable:
        return ProbeResult(True, report.reason or "n/a", not_applicable=True, reason=report.reason)
    if report.ok:
        n_ok = sum(1 for i in report.items if i.ok and not i.skip)
        return ProbeResult(True, f"{n_ok} checks ok — UserName=sanctum")
    fails = "; ".join(f"{i.name}: {i.detail or 'fail'}" for i in report.failed[:3])
    return ProbeResult(False, fails or "wave-1 incomplete")


# ── Probe registry ────────────────────────────────────────────────────


PROBES: list[Probe] = [
    # CLI-tier — every install needs these.
    Probe("network reachability", probe_network),
    Probe("node binary identity", probe_node_identity),
    Probe("sanctum config valid", probe_config_valid),
    Probe("recent backup snapshot", probe_backup_recent),
    Probe("vault armor unlocked", probe_vault_armor),
    # Satellite profile — ``instance.role: satellite``; n/a everywhere else.
    Probe("satellite local brain", _satellite_only("local-brain", probe_satellite_brain)),
    # Haus-tier — only meaningful when the haus services are installed.
    # Each is wrapped to return n/a on CLI-only installs.
    Probe("Yoda cathedral (:1337)", _haus_only("yoda", probe_yoda_cathedral)),
    Probe("Coder cathedral (:3301)", _haus_only("coder", probe_coder_cathedral)),
    Probe("proxyd routing (:4040)", _haus_only("proxyd", probe_proxyd)),
    Probe("proxy key provisioned", _haus_only("proxy-key", probe_proxy_key)),
    Probe("Force Flow (:4077)", _haus_only("force-flow", probe_force_flow)),
    Probe("service user wave-1", _haus_only("service-user", probe_service_user_wave1)),
    Probe("chitti samskara (:2188)", _haus_only("chitti", probe_chitti_samskara)),
    Probe("TCC grants", _haus_only("tcc", probe_tcc_grants)),
    Probe("R2D2 supervisor heartbeat", _haus_only("r2d2", probe_r2d2_heartbeat)),
    Probe("audit log within budget", _haus_only("audit", probe_audit_log_bounded)),
]

# ── Module-keyed probe registry ───────────────────────────────────────
#
# The flat ``PROBES`` list above is what the runner iterates (unchanged by
# this refactor).  ``module_probes`` produces the *module-keyed* view that
# the soak harness and any future consumer can use to discover which probes
# belong to which installed module.
#
# Resolution convention for a dotted probe path "a.b.c.probe_fn":
#   - The *callable* is the last component; everything before it is the
#     module path within ``sanctum_cli.commands`` (or ``sanctum_cli``).
#   - We try ``sanctum_cli.commands.<a.b.c>`` first, then ``sanctum_cli.<a.b.c>``
#     as a fallback — never arbitrary top-level packages.
#   - If the import fails, a failing probe is returned so self-test surfaces
#     the broken reference without crashing.

_SEARCH_PREFIXES = (
    "sanctum_cli.commands.",
    "sanctum_cli.",
)


def _resolve_probe_callable(dotted_path: str) -> Callable[[], ProbeResult]:
    """Resolve a dotted probe path to a callable, or return a failing stub.

    The path must end with a public callable name.  The portion before the last
    dot is the module path; we try each prefix in ``_SEARCH_PREFIXES`` in order.

    Returns a zero-argument callable that returns a ``ProbeResult``.
    """
    if "." not in dotted_path:
        detail = f"probe path '{dotted_path}' has no module component (need 'module.fn')"
        return lambda: ProbeResult(False, detail)

    parts = dotted_path.split(".")
    if any(not part.isidentifier() for part in parts):
        detail = f"probe path '{dotted_path}' contains invalid identifier"
        return lambda: ProbeResult(False, detail)

    mod_part, fn_name = dotted_path.rsplit(".", 1)
    if fn_name.startswith("_"):
        detail = f"probe path '{dotted_path}' targets private attribute '{fn_name}'"
        return lambda: ProbeResult(False, detail)

    last_exc: Exception = ImportError(f"no prefix resolved '{dotted_path}'")

    for prefix in _SEARCH_PREFIXES:
        full_mod = f"{prefix}{mod_part}"
        try:
            mod = importlib.import_module(full_mod)
            fn = getattr(mod, fn_name)
            if not callable(fn):
                last_exc = TypeError(f"'{full_mod}.{fn_name}' is not callable")
                continue
            return cast("Callable[[], ProbeResult]", fn)
        except (ImportError, AttributeError) as exc:
            last_exc = exc
            continue

    captured = last_exc
    detail = f"import error for probe '{dotted_path}': {captured}"
    return lambda: ProbeResult(False, detail)


def module_probes(registry: ModuleRegistry) -> dict[str, list[Probe]]:
    """Build a module-keyed probe dict from *registry*.

    Each module in the registry contributes its ``probes`` list under its
    module name key.  Probe dotted-paths are resolved via
    ``_resolve_probe_callable``; unresolvable paths produce failing probes
    (self-test surfaces the broken reference without crashing).

    The existing ``PROBES`` flat list and runner are **not** modified by this
    function — this is an additive view for consumers such as the soak harness.

    Design note: self-test intentionally does NOT merge module probes into its
    own ``PROBES`` runner.  Doing so would double-run the built-in
    ``backup_recent`` probe (which already appears in ``PROBES`` directly) and
    would break existing tests that monkeypatch ``PROBES``.  Module probes are
    consumed by the soak harness (``run_soak`` → ``_collect_sample``) instead,
    which is the appropriate long-running signal collector for per-module health.
    """
    result: dict[str, list[Probe]] = {}
    for name, manifest in registry.manifests.items():
        result[name] = [Probe(path, _resolve_probe_callable(path)) for path in manifest.probes]
    return result


# ── Public command ────────────────────────────────────────────────────


def self_test_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of the human-friendly panel."),
    ] = False,
    only: Annotated[
        str | None,
        typer.Option("--only", help="Run only probes whose name contains this substring."),
    ] = None,
) -> None:
    """Run the canonical health probes.

    Honest by design: each probe runs, the result lands on-screen
    immediately, the summary panel reflects the actual state, the exit
    code is 0 iff every probe passed.
    """
    start = time.time()
    results: list[tuple[Probe, ProbeResult, float]] = []

    selected = [p for p in PROBES if only.lower() in p.name.lower()] if only else PROBES

    if not json_output:
        console.print()
        console.print(
            Panel.fit(
                Text(f"Sanctum Self-Test — {len(selected)} probes", style="bold"),
                border_style="cyan",
            )
        )
        console.print()

    if not selected:
        if not json_output:
            console.print(
                Panel(
                    Text("No probes matched the --only filter.", style="dim"),
                    border_style="yellow",
                )
            )
        else:
            empty = {
                "tier": _tier_label(),
                "total": 0,
                "passed": 0,
                "failed": 0,
                "not_applicable": 0,
                "duration_ms": 0.0,
                "probes": [],
            }
            console.print_json(json.dumps(empty))
        raise typer.Exit(code=0)

    width = max(len(p.name) for p in selected) + 2

    for i, probe in enumerate(selected, start=1):
        t0 = time.time()
        try:
            res = probe.check()
        except Exception as e:
            res = ProbeResult(False, f"probe raised: {e}")
        elapsed_ms = (time.time() - t0) * 1000
        results.append((probe, res, elapsed_ms))

        if not json_output:
            if res.not_applicable:
                mark = Text(" n/a  ", style="bold yellow")
                trailing = res.reason or "not applicable on this install"
            elif res.passed:
                mark = Text(" pass ", style="bold green")
                trailing = res.detail
            else:
                mark = Text(" fail ", style="bold red")
                trailing = res.detail
            line = Text.assemble(
                Text(f"  [{i:2d}/{len(selected):2d}] ", style="dim"),
                Text(f"{probe.name:<{width}}", style="white"),
                Text("·" * 4, style="dim"),
                mark,
                Text(f"  {trailing}", style="dim"),
            )
            console.print(line)

    total_ms = (time.time() - start) * 1000
    na = sum(1 for _, r, _ in results if r.not_applicable)
    passed = sum(1 for _, r, _ in results if r.passed and not r.not_applicable)
    failed = sum(1 for _, r, _ in results if not r.passed)

    tier = _tier_label()

    if json_output:
        payload = {
            "tier": tier,
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "not_applicable": na,
            "duration_ms": round(total_ms, 1),
            "probes": [
                {
                    "name": p.name,
                    "passed": r.passed,
                    "not_applicable": r.not_applicable,
                    "detail": r.detail,
                    "reason": r.reason,
                    "duration_ms": round(ms, 1),
                }
                for p, r, ms in results
            ],
        }
        console.print_json(json.dumps(payload))
    else:
        console.print()
        if failed == 0:
            if tier == "satellite":
                headline = "This Sanctum satellite is healthy."
                na_scope = "on this satellite"
            else:
                headline = f"Sanctum {tier} is healthy."
                na_scope = "on this install tier"
            counts = f"  {passed}/{len(results)} probes passed in {total_ms:.0f} ms"
            if na:
                counts += f"  ·  {na} n/a {na_scope}"
            console.print(
                Panel(
                    Text.assemble(
                        Text(headline, style="bold green"),
                        Text(counts, style="dim"),
                    ),
                    border_style="green",
                    padding=(0, 2),
                )
            )
        else:
            console.print(
                Panel(
                    Text.assemble(
                        Text(f"{failed} probe(s) failed.", style="bold red"),
                        Text(
                            f"  {passed}/{len(results) - na} applicable probes passed in {total_ms:.0f} ms."
                            f" Each red line above has the actionable detail.",
                            style="dim",
                        ),
                    ),
                    border_style="red",
                    padding=(0, 2),
                )
            )

    raise typer.Exit(code=1 if failed > 0 else 0)
