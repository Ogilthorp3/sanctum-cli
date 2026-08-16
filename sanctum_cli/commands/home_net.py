"""CLI: ``sanctum net home`` — dogfood Home Network product surface.

Read-only status by default. ``improve`` only applies fail-safe Firewalla
MSS/mtu_probing (never WAN mode / ADMZ).
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sanctum_cli import config
from sanctum_cli.net import home as home_mod
from sanctum_cli.net.home import (
    ArmorState,
    Health,
    HubReach,
    InternetPath,
    MssGuard,
    Overall,
    WanPath,
    build_home_report,
)

console = Console()

home_app = typer.Typer(
    help="Home network product surface — status + safe improve (dogfood).",
    no_args_is_help=False,
)

_FW_KEY_CANDIDATES = (
    Path.home() / ".openclaw/firewalla/keys/ssh_firewalla",
    Path.home() / ".ssh/firewalla_ed25519",
)
_MSS_SCRIPT = "/home/pi/.firewalla/config/sanctum-mss-fastly-fix.sh"
_ARMOR_VERIFY = Path.home() / ".sanctum/bin/singlenat-verify.sh"


def _fw_host() -> str:
    return str(config.instance_value("devices.firewalla.host", "10.0.0.1"))


def _fw_user() -> str:
    return str(config.instance_value("devices.firewalla.ssh_user", "pi"))


def _fw_key() -> Path | None:
    configured = config.instance_value("firewalla.ssh_key", None)
    if configured:
        p = Path(str(configured)).expanduser()
        return p if p.exists() else None
    for c in _FW_KEY_CANDIDATES:
        if c.exists():
            return c
    return None


def _ssh_fw(remote: str, *, timeout: int = 12) -> tuple[int, str]:
    key = _fw_key()
    if key is None:
        return 127, "no firewalla ssh key"
    host = _fw_host()
    user = _fw_user()
    argv = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={min(timeout, 8)}",
        f"{user}@{host}",
        remote,
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 124, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def _tls_ok(host: str, port: int = 443, timeout: float = 6.0) -> bool:
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        raw.settimeout(timeout)
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(raw, server_hostname=host)
        ss.close()
        return True
    except OSError:
        return False


def probe_internet() -> InternetPath:
    t0 = time.time()
    fastly = _tls_ok("pypi.org")
    control = _tls_ok("www.google.com")
    dt = time.time() - t0
    detail = f"Fastly/PyPI={'ok' if fastly else 'FAIL'} · Google={'ok' if control else 'FAIL'} · {dt:.1f}s"
    return InternetPath(fastly, control, detail)


def probe_wan() -> WanPath | None:
    # Prefer singlenat-verify JSON (already SSH'd once, accurate wan_if).
    if _ARMOR_VERIFY.exists():
        try:
            proc = subprocess.run(
                [str(_ARMOR_VERIFY)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            raw = (proc.stdout or "").strip()
            if raw.startswith("{"):
                data = json.loads(raw)
                wan_if = str(data.get("wan_if") or "") or None
                pub = str(data.get("fw_wan_ip") or data.get("pub_ip") or "") or None
                if wan_if and "pppoe" in wan_if:
                    kind = "pppoe"
                elif pub and not pub.startswith(("10.", "192.168.", "172.")):
                    kind = "public_eth"
                elif pub:
                    kind = "private"
                else:
                    kind = "unknown"
                detail = f"{kind} · if={wan_if or '-'} · public={pub or '-'}"
                return WanPath(kind, pub, wan_if, detail)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    code, out = _ssh_fw(
        "ip -4 addr show pppoe0 2>/dev/null; ip -4 addr show eth0 2>/dev/null; "
        "curl -4 -sS --max-time 4 https://api.ipify.org 2>/dev/null; echo",
        timeout=15,
    )
    if code != 0:
        return None
    body = out
    pub = None
    wan_if = None
    kind = "unknown"
    for line in body.splitlines():
        s = line.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", s):
            pub = s
    if re.search(r"pppoe0:.*\n(?:.*\n)*?\s*inet\s+(\d+\.\d+\.\d+\.\d+)", body):
        kind = "pppoe"
        wan_if = "pppoe0"
        m = re.search(r"pppoe0:.*\n(?:.*\n)*?\s*inet\s+(\d+\.\d+\.\d+\.\d+)", body)
        if m:
            pub = pub or m.group(1)
    elif re.search(r"\beth0:.*inet\s+(\d+\.\d+\.\d+\.\d+)", body, re.S):
        m = re.search(r"\beth0:.*inet\s+(\d+\.\d+\.\d+\.\d+)", body, re.S)
        ip = m.group(1) if m else ""
        wan_if = "eth0"
        kind = (
            "private"
            if ip.startswith(("10.", "192.168.", "172."))
            else "public_eth"
        )
        pub = pub or ip
    detail = f"{kind} · if={wan_if or '-'} · public={pub or '-'}"
    return WanPath(kind, pub, wan_if, detail)


def probe_mss() -> MssGuard | None:
    code, out = _ssh_fw(
        "sudo iptables-save -t mangle 2>/dev/null | grep -F 'set-mss 1400' || true; "
        "sysctl -n net.ipv4.tcp_mtu_probing 2>/dev/null; "
        "test -x /home/pi/.firewalla/config/sanctum-mss-fastly-fix.sh && echo SCRIPT=yes || echo SCRIPT=no",
        timeout=12,
    )
    if code != 0 and "no firewalla" in out:
        return None
    if code == 124:
        return None
    mss_ok = "set-mss 1400" in out or "--set-mss 1400" in out
    probing_ok = None
    for line in out.splitlines():
        if line.strip() in {"0", "1", "2"}:
            probing_ok = line.strip() != "0"
            break
    script = "SCRIPT=yes" in out
    detail = (
        f"MSS1400={'yes' if mss_ok else 'no'} · "
        f"mtu_probing={'on' if probing_ok else 'off' if probing_ok is False else '?'} · "
        f"persist_script={'yes' if script else 'no'}"
    )
    return MssGuard(mss_ok, probing_ok, detail)


def probe_armor() -> ArmorState | None:
    if _ARMOR_VERIFY.exists() and _ARMOR_VERIFY.is_file():
        try:
            proc = subprocess.run(
                [str(_ARMOR_VERIFY)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            raw = (proc.stdout or "").strip()
            if raw.startswith("{"):
                data = json.loads(raw)
                return ArmorState(
                    state=str(data.get("state") or ""),
                    singlenat=data.get("singlenat") in (True, "yes", "true", 1),
                    poison=data.get("poison") in (True, "yes", "true", 1),
                    detail=raw[:200],
                )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
    # fallback: light SSH
    code, out = _ssh_fw(
        "ip -4 addr show pppoe0 eth0 2>/dev/null | head -20; "
        "ip route show table all 2>/dev/null | grep -E '0\\.0\\.0\\.0/1|128\\.0\\.0\\.0/1' | head -3 || true",
        timeout=12,
    )
    if code != 0:
        return None
    poison = "0.0.0.0/1" in out or "128.0.0.0/1" in out
    return ArmorState(
        state="UNKNOWN",
        singlenat=None,
        poison=poison,
        detail="light SSH probe (verify script missing)" if not poison else "POISON routes present",
    )


def probe_hub() -> HubReach:
    host = str(config.instance_value("devices.hub.host", "192.168.2.1"))
    try:
        socket.create_connection((host, 80), timeout=2).close()
        return HubReach(True, host, f"{host}:80 open")
    except OSError:
        try:
            socket.create_connection((host, 443), timeout=2).close()
            return HubReach(True, host, f"{host}:443 open")
        except OSError:
            return HubReach(False, host, f"{host} unreachable (normal under PPPoE bridge)")


def _render(report: home_mod.HomeReport) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("mark", width=2)
    table.add_column("label", style="bold", min_width=14)
    table.add_column("detail")
    mark = {
        Health.OK: Text("✓", style="green"),
        Health.ATTENTION: Text("!", style="yellow"),
        Health.DOWN: Text("✗", style="red"),
        Health.UNKNOWN: Text("?", style="dim"),
    }
    for row in report.rows:
        table.add_row(mark[row.health], row.label, row.detail)
    style = {
        Overall.GREEN: "green",
        Overall.ATTENTION: "yellow",
        Overall.DEGRADED: "red",
    }[report.overall]
    console.print(
        Panel(
            table,
            title=f"Home network — [{style}]{report.overall.value}[/]",
            subtitle=report.headline,
            border_style=style,
        )
    )
    if report.next_steps:
        console.print("[bold]Next steps[/]")
        for s in report.next_steps:
            console.print(f"  → {s}")
    console.print(f"[dim]Safe improve: {report.improve_detail}[/]")


def collect_report() -> home_mod.HomeReport:
    # armor once — also feeds WAN classification when verify JSON is present
    armor = probe_armor()
    wan = probe_wan()
    # If WAN still fuzzy but armor JSON named pppoe0, prefer that
    if (
        armor
        and armor.detail.startswith("{")
        and wan
        and wan.kind in ("unknown", "public_eth")
        and "pppoe0" in armor.detail
    ):
        try:
            data = json.loads(armor.detail)
            wan = WanPath(
                "pppoe",
                str(data.get("fw_wan_ip") or data.get("pub_ip") or wan.public_ip or ""),
                "pppoe0",
                f"pppoe · if=pppoe0 · public={data.get('fw_wan_ip') or data.get('pub_ip') or '-'}",
            )
        except json.JSONDecodeError:
            pass
    return build_home_report(
        internet=probe_internet(),
        wan=wan,
        mss=probe_mss(),
        armor=armor,
        hub=probe_hub(),
    )


@home_app.callback(invoke_without_command=True)
def home_root(ctx: typer.Context) -> None:
    """Show home network status (default)."""
    if ctx.invoked_subcommand is None:
        home_status()


@home_app.command("status", help="One-glance home network health (read-only).")
def home_status(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    report = collect_report()
    if json_out:
        payload: dict[str, Any] = {
            "overall": report.overall.value,
            "headline": report.headline,
            "rows": [
                {
                    "label": r.label,
                    "health": r.health.value,
                    "detail": r.detail,
                    "action": r.action,
                }
                for r in report.rows
            ],
            "next_steps": list(report.next_steps),
            "improve_safe": report.improve_safe,
            "improve_detail": report.improve_detail,
        }
        console.print_json(data=payload)
        return
    _render(report)


@home_app.command(
    "improve",
    help="Apply SAFE fixes only (MSS 1400 + mtu_probing on Firewalla). Never changes WAN mode.",
)
def home_improve(
    force: Annotated[
        bool, typer.Option("--force", help="Apply even if status looks OK.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would run; zero changes.")
    ] = False,
) -> None:
    report = collect_report()
    _render(report)
    if not report.improve_safe and not force:
        console.print(
            "[green]✓[/] Nothing unsafe pending — CDN guard looks good. "
            "Use [bold]--force[/] only to re-assert MSS script."
        )
        raise typer.Exit(0)

    remote = (
        f"sudo bash {_MSS_SCRIPT} 2>/dev/null || true; "
        # inline fallback if script missing
        "sudo sysctl -w net.ipv4.tcp_mtu_probing=1 >/dev/null; "
        "sudo sysctl -w net.ipv4.tcp_probe_threshold=512 >/dev/null 2>/dev/null || true; "
        "if ! sudo iptables -t mangle -C FORWARD -p tcp --tcp-flags SYN,RST SYN "
        "-j TCPMSS --set-mss 1400 2>/dev/null; then "
        "sudo iptables -t mangle -I FORWARD 1 -p tcp --tcp-flags SYN,RST SYN "
        "-j TCPMSS --set-mss 1400; fi; "
        "if ! sudo iptables -t mangle -C OUTPUT -p tcp --tcp-flags SYN,RST SYN "
        "-j TCPMSS --set-mss 1400 2>/dev/null; then "
        "sudo iptables -t mangle -I OUTPUT 1 -p tcp --tcp-flags SYN,RST SYN "
        "-j TCPMSS --set-mss 1400; fi; "
        "sudo iptables-save -t mangle | grep -F '1400' || true; "
        "sysctl -n net.ipv4.tcp_mtu_probing; "
        "echo IMPROVE_DONE"
    )
    console.print(f"[bold]Safe improve[/]: {report.improve_detail}")
    if dry_run:
        console.print("[dim]dry-run — would SSH to Firewalla and re-assert MSS 1400[/]")
        raise typer.Exit(0)

    code, out = _ssh_fw(remote, timeout=20)
    if code != 0 or "IMPROVE_DONE" not in out:
        console.print(f"[red]✗[/] improve failed (ssh={code})")
        console.print(out[:500])
        raise typer.Exit(1)
    console.print("[green]✓[/] Firewalla CDN guard re-asserted (MSS 1400 + mtu_probing).")
    # re-probe internet
    inet = probe_internet()
    console.print(f"  recheck: {inet.detail}")
    if inet.fastly_ok:
        console.print("[green]✓[/] PyPI/Fastly TLS OK — dogfood path healthy.")
    else:
        console.print(
            "[yellow]![/] Fastly still failing — open a new TCP session "
            "(restart terminal/app) or check WAN."
        )


@home_app.command(
    "doctor",
    help="Status + improve recommendation in one shot (read-only unless --apply).",
)
def home_doctor(
    apply: Annotated[
        bool, typer.Option("--apply", help="Also run safe improve if needed.")
    ] = False,
) -> None:
    report = collect_report()
    _render(report)
    if apply and report.improve_safe:
        home_improve(force=False, dry_run=False)
    elif apply:
        console.print("[dim]--apply: nothing safe to fix[/]")
