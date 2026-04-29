"""``sanctum bridge`` — talk to the Sanctum Bridge from the terminal.

Subcommands:

    sanctum bridge health                         liveness check
    sanctum bridge whoami                         show effective config
    sanctum bridge manifest [--json]              list modules + actions
    sanctum bridge folder <path>                  look up a SP folder
    sanctum bridge upload <file> <folder> ...     upload a file to SP

Credentials come from the Keychain (services
``sanctum-bridge-cf-access-client-{id,secret}`` and
``sanctum-bridge-hmac-sharepoint``). The base URL defaults to
``https://bridge.nepveu.name`` and can be overridden with
``SANCTUM_BRIDGE_URL`` for staging.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli import keychain
from sanctum_cli.bridge_client import (
    BridgeClient,
    BridgeCreds,
    base_url_from_env,
    encode_file,
)
from sanctum_cli.errors import SanctumError, UserError

console = Console()
err_console = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _client(module: str = "sharepoint") -> BridgeClient:
    return BridgeClient(
        BridgeCreds.from_keychain(),
        base_url=base_url_from_env(),
        module=module,
    )


def _parse_kv_list(items: list[str] | None) -> dict[str, str]:
    """Convert ``["k=v", "x=y"]`` into ``{"k":"v", "x":"y"}``."""
    if not items:
        return {}
    out: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            msg = f"--metadata expects key=value, got {raw!r}"
            raise UserError(msg, fix="pass each pair as --metadata k=v")
        k, _, v = raw.partition("=")
        k = k.strip()
        if not k:
            msg = f"--metadata key is empty in {raw!r}"
            raise UserError(msg)
        out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Command implementations
# --------------------------------------------------------------------------- #

def health_command(json_output: bool = False) -> None:
    with _client() as c:
        data = c.health()
    if json_output:
        console.print_json(data=data)
        return
    modules = ", ".join(data.get("modules", [])) or "(none)"
    console.print(
        f"[green]ok[/green]  v{data.get('version', '?')}  modules: {modules}  "
        f"@ {c.base_url}"
    )


def whoami_command() -> None:
    creds = BridgeCreds.from_keychain()
    base = base_url_from_env()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("base URL", base)
    table.add_row("CF Access client id", creds.cf_access_id)
    table.add_row(
        "CF Access secret",
        f"{creds.cf_access_secret[:8]}…{creds.cf_access_secret[-4:]} (redacted)",
    )
    table.add_row(
        "HMAC secret",
        f"{creds.hmac_secret[:8]}…{creds.hmac_secret[-4:]} (redacted)",
    )
    console.print(table)


def manifest_command(json_output: bool = False) -> None:
    with _client() as c:
        data = c.manifest()
    if json_output:
        console.print_json(data=data)
        return
    for mod in data.get("modules", []):
        console.print(f"[bold]{mod['name']}[/bold]  v{mod['version']}")
        actions = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        actions.add_column("action")
        actions.add_column("HTTP")
        actions.add_column("summary", overflow="fold")
        for a in mod.get("actions", []):
            actions.add_row(
                f"{mod['name']}.{a['name']}",
                f"{a['method']} /{mod['name']}{a['path']}",
                a.get("summary", ""),
            )
        console.print(actions)


def folder_command(path: str, json_output: bool = False) -> None:
    with _client() as c:
        data = c.folder(path)
    if json_output:
        console.print_json(data=data)
        return
    console.print(f"[bold]{path}[/bold]")
    console.print(f"  drive_item_id : {data['drive_item_id']}")
    console.print(f"  child_count   : {data['child_count']}")
    console.print(f"  last_modified : {data['last_modified']}")
    console.print(f"  web_url       : [link]{data['web_url']}[/link]")


def upload_command(
    file: Path,
    folder: str,
    *,
    if_exists: str = "version",
    doc_type: str | None = None,
    metadata: list[str] | None = None,
    no_create_folders: bool = False,
    json_output: bool = False,
) -> None:
    if if_exists not in {"version", "overwrite", "rename", "fail"}:
        msg = f"--if-exists must be one of version|overwrite|rename|fail (got {if_exists!r})"
        raise UserError(msg)

    filename, b64 = encode_file(file)
    meta = _parse_kv_list(metadata)
    if doc_type:
        meta.setdefault("doc_type", doc_type)

    with _client() as c:
        result = c.upload(
            folder_path=folder,
            filename=filename,
            content_b64=b64,
            metadata=meta,
            create_folder_if_missing=not no_create_folders,
            if_exists=if_exists,
        )

    if json_output:
        console.print_json(data=result)
        return
    console.print(f"[green]uploaded[/green]  {filename} → {folder}/")
    console.print(f"  web_url       : [link]{result['web_url']}[/link]")
    if result.get("version") is not None:
        console.print(f"  version       : {result['version']}")
    console.print(f"  drive_item_id : {result['drive_item_id']}")
    console.print(f"  etag          : {result['etag']}")
    if not result.get("metadata_applied", True):
        err_console.print(
            "[yellow]warning[/yellow]: metadata patch was rejected — "
            "the SharePoint library probably has no column for one of the "
            "supplied keys. The file is uploaded; only the metadata is missing."
        )


# --------------------------------------------------------------------------- #
# Doctor — health probes with green/red per-row table
# --------------------------------------------------------------------------- #

Status = Literal["OK", "WARN", "FAIL", "SKIP"]

_STATUS_STYLES: dict[Status, str] = {
    "OK": "[green]OK[/green]",
    "WARN": "[yellow]WARN[/yellow]",
    "FAIL": "[red]FAIL[/red]",
    "SKIP": "[dim]SKIP[/dim]",
}

# Keychain entries we expect on a host that runs the bridge end-to-end.
# (These exist on the manoir gateway; they are not all expected on a
# pure client host.)
_KEYCHAIN_LOCAL = (
    "sanctum-bridge-cf-access-client-id",
    "sanctum-bridge-cf-access-client-secret",
    "sanctum-bridge-cf-access-token-id",
    "sanctum-bridge-hmac-sharepoint",
)
# Additional services that only live on the gateway. Doctor surfaces them
# but doesn't fail the run if they're missing on a remote operator's mac.
_KEYCHAIN_GATEWAY = (
    "sanctum-bridge-sp-client-id",
    "sanctum-bridge-sp-client-secret",
    "sanctum-bridge-sp-tenant-id",
    "sanctum-bridge-sp-site-id",
)


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""


def _check_keychain() -> list[Check]:
    out: list[Check] = []
    for svc in _KEYCHAIN_LOCAL:
        out.append(
            Check(
                name=f"keychain: {svc}",
                status="OK" if keychain.exists("dummy", svc) or _kc_any(svc) else "FAIL",
                detail="present" if _kc_any(svc) else "missing — re-run bridge bootstrap",
            )
        )
    for svc in _KEYCHAIN_GATEWAY:
        present = _kc_any(svc)
        out.append(
            Check(
                name=f"keychain: {svc}",
                status="OK" if present else "SKIP",
                detail="present" if present else "missing — gateway-only entry",
            )
        )
    return out


def _kc_any(service: str) -> bool:
    """``keychain.exists`` accepts an account; we don't always know it. Fall
    back to a service-only lookup via the security CLI."""
    import subprocess
    try:
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_health(c: BridgeClient) -> list[Check]:
    try:
        h = c.health()
    except SanctumError as exc:
        return [Check("bridge /_health", "FAIL", exc.message)]

    out = [
        Check("bridge /_health", "OK", f"v{h.get('version')} commit={h.get('commit')}"),
        Check(
            "modules loaded",
            "OK" if h.get("modules") else "FAIL",
            ", ".join(h.get("modules", [])) or "(none)",
        ),
    ]
    started = h.get("started_at")
    if started:
        try:
            t0 = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            uptime_s = (datetime.now(timezone.utc) - t0).total_seconds()
            if uptime_s < 60:
                out.append(Check("uptime", "WARN", f"{uptime_s:.0f}s — recently restarted"))
            else:
                out.append(Check("uptime", "OK", _humanize_seconds(uptime_s)))
        except ValueError:
            out.append(Check("uptime", "WARN", f"unparseable started_at: {started}"))
    al_count = int(h.get("allowlist_count", 0))
    out.append(
        Check(
            "allowlist count",
            "OK" if al_count > 0 else "WARN",
            f"{al_count} root(s)" if al_count else "empty — bridge will reject all writes",
        )
    )
    return out


def _check_diagnostic(c: BridgeClient) -> list[Check]:
    try:
        d = c.diagnostic()
    except SanctumError as exc:
        return [Check("bridge /_diagnostic", "FAIL", exc.message)]

    out: list[Check] = []
    out.append(
        Check(
            "CF Access JWT",
            "OK" if d.get("cf_access_jwt") == "enabled" else "WARN",
            str(d.get("cf_access_jwt", "?")),
        )
    )

    rotator = d.get("rotator") or {}
    last_run = rotator.get("last_run") or rotator.get("status", "never_ran")
    outcome = rotator.get("outcome", "")
    detail = rotator.get("detail", "")

    if last_run == "never_ran":
        out.append(Check("rotator last_run", "WARN", "never ran — scheduled daily 09:00 local"))
    else:
        try:
            t = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600
            if age_h > 48:
                out.append(Check("rotator last_run", "FAIL", f"{age_h:.0f}h ago — stale"))
            else:
                out.append(Check("rotator last_run", "OK", f"{age_h:.1f}h ago"))
        except ValueError:
            out.append(Check("rotator last_run", "WARN", f"unparseable: {last_run}"))

    if outcome == "fail":
        out.append(Check("rotator outcome", "FAIL", detail or "fail"))
    elif outcome == "skip":
        out.append(Check("rotator outcome", "OK", detail or "skip"))
    elif outcome == "rotated":
        out.append(Check("rotator outcome", "OK", detail or "rotated"))
    elif outcome:
        out.append(Check("rotator outcome", "WARN", outcome))

    req_total = int(d.get("request_count_total", 0))
    out.append(Check("requests since boot", "OK", f"{req_total}"))

    return out


def _humanize_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def doctor_command(json_output: bool = False) -> None:
    creds = BridgeCreds.from_keychain()
    base = base_url_from_env()
    checks: list[Check] = []
    checks.extend(_check_keychain())
    with BridgeClient(creds, base_url=base) as c:
        checks.extend(_check_health(c))
        checks.extend(_check_diagnostic(c))

    if json_output:
        console.print_json(
            data={
                "base_url": base,
                "checks": [
                    {"name": c_.name, "status": c_.status, "detail": c_.detail}
                    for c_ in checks
                ],
                "overall": _overall(checks),
            }
        )
        return

    table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for c_ in checks:
        table.add_row(c_.name, _STATUS_STYLES[c_.status], c_.detail)
    console.print(table)
    console.print()
    console.print(f"overall: {_STATUS_STYLES[_overall(checks)]}  base={base}")


def _overall(checks: list[Check]) -> Status:
    rank = {"OK": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}
    worst: Status = "OK"
    for c_ in checks:
        if rank[c_.status] > rank[worst]:
            worst = c_.status
    return worst
