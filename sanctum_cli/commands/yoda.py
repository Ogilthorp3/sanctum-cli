"""``sanctum yoda`` — guided Yoda-on-Signal setup, chat, status, and updates.

Brings the full Signal consigliere experience to any Apple Silicon Mac without
touching an existing operator haus:
  - Installs latest ``signal-cli`` (Homebrew) + ``openclaw@latest`` + ``@openclaw/signal``
  - Wires an isolated ``sanctum-yoda`` OpenClaw profile with Yoda's ``SOUL.md``
    backed by your local Sanctum Dragon (``127.0.0.1:8787/v1``) or Claude/Gemini
  - Guides Signal pairing (QR link or dedicated bot number SMS registration)
  - Installs a user LaunchAgent so Yoda survives reboots
  - Keeps everything up to date automatically via ``sanctum update``
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import ssl
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from sanctum_cli.commands import dragon as dragon_cmd
from sanctum_cli.commands import haus_cmd

console = Console()
app = typer.Typer(
    name="yoda",
    help="Set up, chat with, and update Yoda on Signal (OpenClaw + signal-cli + Dragon).",
    no_args_is_help=True,
)

ENV_YODA_STATE = "SANCTUM_YODA_STATE"
YODA_PROFILE = "sanctum-yoda"
YODA_PLIST_LABEL = "haus.sanctum.yoda-signal"
DEFAULT_SIGNAL_HTTP_PORT = 8080
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

YODA_SOUL_MD = """# Yoda — Sanctum Haus Consigliere

You are Yoda, the calm, honest guide of this Sanctum haus.

- Speak clearly, warmly, and concisely (with a light, calm Yoda cadence — never cartoon gibberish).
- Watch over this Mac, its backups, and the people who live here.
- Never invent a status, file, or command output you did not actually verify.
- Keep Signal replies crisp and easy to read on a phone screen.
"""


def yoda_state_path() -> Path:
    override = os.environ.get(ENV_YODA_STATE, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".sanctum" / "state" / "yoda-signal.json"


def load_yoda_state() -> dict[str, Any]:
    path = yoda_state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_yoda_state(data: dict[str, Any]) -> None:
    path = yoda_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def is_yoda_signal_installed() -> bool:
    return bool(load_yoda_state().get("installed"))


def is_operator_haus() -> bool:
    """Detect a full production operator haus so studio/beta Yoda never collides with it."""
    home = Path.home()
    return (
        (home / ".sanctum" / "secrets" / "ha-token").is_file()
        and (home / ".sanctum" / "launchagents" / "live" / "haus.sanctum.signal-cli.plist").is_file()
    )


def normalize_e164(raw: str) -> str | None:
    cleaned = re.sub(r"[\s\-().]", "", raw.strip())
    if cleaned.isdigit() and len(cleaned) == 10:
        cleaned = f"+1{cleaned}"
    return cleaned if E164_RE.match(cleaned) else None


def _cmd_version(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        out = (r.stdout or r.stderr).strip()
        return out.splitlines()[0][:80] if r.returncode == 0 and out else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _write_launchagent(*, openclaw_bin: str) -> Path:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path.home() / ".sanctum" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{YODA_PLIST_LABEL}.plist"
    ca_cert = Path.home() / ".sanctum" / "certs" / "ca.crt"
    env_vars = {
        "HOME": str(Path.home()),
        "PATH": f"{Path.home() / '.sanctum' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "NODE_EXTRA_CA_CERTS": str(ca_cert),
    }
    payload = {
        "Label": YODA_PLIST_LABEL,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Standard",
        "ThrottleInterval": 10,
        "ProgramArguments": [
            openclaw_bin,
            "--profile",
            YODA_PROFILE,
            "gateway",
        ],
        "StandardOutPath": str(log_dir / "yoda-signal.log"),
        "StandardErrorPath": str(log_dir / "yoda-signal.log"),
        "EnvironmentVariables": env_vars,
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(payload, fh)
    return plist_path


def update_yoda_if_needed(*, dry_run: bool = False) -> tuple[bool, str]:
    """Upgrade signal-cli + openclaw + @openclaw/signal when Yoda on Signal is installed."""
    state = load_yoda_state()
    if not state.get("installed"):
        return True, "not installed"

    if dry_run:
        return True, "would upgrade signal-cli, openclaw@latest, and @openclaw/signal"

    errors: list[str] = []
    if shutil.which("brew"):
        r = subprocess.run(
            ["brew", "upgrade", "signal-cli"], capture_output=True, text=True, check=False
        )
        if r.returncode != 0 and "already installed" not in (r.stdout + r.stderr).lower():
            errors.append(f"signal-cli: {(r.stderr or r.stdout).strip()[-120:]}")

    if shutil.which("npm"):
        r = subprocess.run(
            ["npm", "install", "-g", "openclaw@latest"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            errors.append(f"openclaw: {(r.stderr or r.stdout).strip()[-120:]}")

    openclaw_bin = shutil.which("openclaw")
    if openclaw_bin:
        subprocess.run(
            [openclaw_bin, "--profile", YODA_PROFILE, "plugins", "install", "@openclaw/signal"],
            capture_output=True,
            text=True,
            check=False,
        )

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{YODA_PLIST_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )

    state.update(
        {
            "signal_cli_version": _cmd_version(["signal-cli", "--version"]) or state.get("signal_cli_version"),
            "openclaw_version": _cmd_version(["openclaw", "--version"]) or state.get("openclaw_version"),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    save_yoda_state(state)

    if errors:
        return False, "; ".join(errors)
    return (
        True,
        f"current (openclaw {state.get('openclaw_version') or '?'} · "
        f"signal-cli {state.get('signal_cli_version') or '?'})",
    )


@app.command("setup", help="Guided setup for Yoda on Signal (signal-cli + OpenClaw + Rust proxyd/sanctum-mlx).")
def yoda_setup(
    bot_number: Annotated[
        str | None,
        typer.Option("--bot-number", help="Dedicated Signal bot number in E.164 (e.g. +15551234567)."),
    ] = None,
    mode: Annotated[
        str,
        typer.Option("--mode", help="Signal pairing mode: qr (link device) | sms (register number)."),
    ] = "qr",
    backend: Annotated[
        str,
        typer.Option("--backend", help="Model backend for Yoda: rust (proxyd :4040 → sanctum-mlx) | dragon | claude | gemini."),
    ] = "rust",
    http_port: Annotated[
        int, typer.Option("--http-port", help="Loopback port for signal-cli HTTP daemon.")
    ] = DEFAULT_SIGNAL_HTTP_PORT,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show setup plan without modifying this Mac.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Allow running even on a full operator haus.")
    ] = False,
) -> None:
    if is_operator_haus() and not force:
        console.print(
            Panel.fit(
                "[yellow]This Mac already runs the full operator haus Signal/Yoda stack.[/]\n"
                "Skipping studio Yoda setup so your live Signal daemon is untouched.\n"
                "[dim]Pass `--force` only if you intentionally want a second isolated profile.[/]",
                title="Operator Haus Guard",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=0)

    console.print(
        Panel.fit(
            "[bold]Yoda on Signal — Sanctum Setup[/]\n\n"
            "[bold yellow]Important Signal rule:[/] Signal's loop protection ignores messages you send\n"
            "to your [italic]own[/] personal number. Give Yoda a [bold]dedicated bot phone number[/]\n"
            "(cheap eSIM / prepaid / VoIP number), then link via QR or register via SMS.",
            border_style="cyan",
        )
    )

    if dry_run:
        console.print(
            "[dim]dry-run plan:[/]\n"
            "  1. Install/upgrade signal-cli (Homebrew), node, openclaw@latest, @openclaw/signal\n"
            f"  2. Create isolated OpenClaw profile `{YODA_PROFILE}` with Yoda SOUL.md (backend={backend})\n"
            f"  3. Pair Signal ({mode}) and wire channel on 127.0.0.1:{http_port}\n"
            f"  4. Install LaunchAgent {YODA_PLIST_LABEL} and verify channel probe"
        )
        return

    if not shutil.which("brew"):
        console.print("[red]Homebrew is required.[/] Run `curl -fsSL https://sanctum.run/install.sh | bash` first.")
        raise typer.Exit(code=1)

    # Step 1: Install/check signal-cli, node, openclaw, @openclaw/signal
    console.print("\n[bold]Step 1.[/] Installing latest signal-cli + OpenClaw + @openclaw/signal…")
    if not shutil.which("signal-cli"):
        subprocess.run(["brew", "install", "signal-cli"], check=True)
    if not shutil.which("npm"):
        subprocess.run(["brew", "install", "node"], check=True)
    subprocess.run(["npm", "install", "-g", "openclaw@latest"], check=True)
    openclaw_bin = shutil.which("openclaw")
    if not openclaw_bin:
        console.print("[red]openclaw installed but not found on PATH.[/]")
        raise typer.Exit(code=1)
    subprocess.run(
        [openclaw_bin, "--profile", YODA_PROFILE, "plugins", "install", "@openclaw/signal"],
        check=False,
    )
    console.print("  [green]✓[/] Runtime dependencies ready")

    # Step 2: Ensure Rust Sanctum engine (or legacy Python Dragon) if chosen
    chosen_backend = backend.strip().lower()
    if chosen_backend == "rust" and not haus_cmd.is_rust_suite_installed() and not haus_cmd.is_operator_haus():
        if Confirm.ask(
            "\n  Sanctum Rust engine (proxyd + sanctum-mlx + Presidio) not installed yet. Install now?",
            default=True,
        ):
            haus_cmd.haus_install(tier="auto", dry_run=False)
        else:
            console.print("  [dim]Install the Rust engine anytime with `sanctum haus install`.[/]")
    elif chosen_backend == "dragon" and not dragon_cmd.is_dragon_installed():
        if Confirm.ask("\n  Local Dragon not installed yet. Install Dragon now?", default=True):
            dragon_cmd.dragon_install(tier="auto", dry_run=False)
        else:
            console.print("  [dim]You can install Dragon anytime with `sanctum dragon install`.[/]")

    # Step 3: Configure isolated OpenClaw profile + Yoda SOUL.md
    console.print(f"\n[bold]Step 2.[/] Configuring OpenClaw profile `{YODA_PROFILE}` + Yoda persona…")
    ws = Path.home() / f".openclaw-{YODA_PROFILE}" / "workspace-yoda"
    ws.mkdir(parents=True, exist_ok=True)
    soul_path = ws / "SOUL.md"
    if not soul_path.is_file():
        soul_path.write_text(YODA_SOUL_MD, encoding="utf-8")

    setup_cmd = [
        openclaw_bin,
        "--profile",
        YODA_PROFILE,
        "setup",
        "--non-interactive",
        "--accept-risk",
        "--baseline",
        "--mode",
        "local",
        "--auth-choice",
        "skip",
        "--skip-channels",
        "--skip-daemon",
        "--skip-health",
        "--skip-skills",
        "--skip-search",
        "--skip-ui",
        "--no-install-daemon",
        "--gateway-bind",
        "loopback",
        "--agent-name",
        "Yoda",
        "--workspace",
        str(ws),
    ]
    if chosen_backend == "rust":
        setup_cmd.extend(
            [
                "--custom-provider-id",
                "sanctum",
                "--custom-base-url",
                "https://127.0.0.1:4040/v1",
                "--custom-model-id",
                "council-mlx",
                "--custom-compatibility",
                "openai",
                "--custom-api-key",
                "sanctum-local",
                "--custom-text-input",
            ]
        )
    elif chosen_backend == "dragon":
        setup_cmd.extend(
            [
                "--custom-provider-id",
                "dragon",
                "--custom-base-url",
                f"http://127.0.0.1:{dragon_cmd.DEFAULT_DRAGON_PORT}/v1",
                "--custom-model-id",
                "sanctum-dragon",
                "--custom-compatibility",
                "openai",
                "--custom-api-key",
                "sanctum-local",
                "--custom-text-input",
            ]
        )
    setup_env = dict(os.environ)
    ca_cert = Path.home() / ".sanctum" / "certs" / "ca.crt"
    if ca_cert.is_file():
        setup_env["NODE_EXTRA_CA_CERTS"] = str(ca_cert)
    subprocess.run(setup_cmd, env=setup_env, check=False)
    console.print(f"  [green]✓[/] Yoda workspace ready at {ws}")

    # Step 4: Signal bot number & pairing
    console.print("\n[bold]Step 3.[/] Pairing Signal account for Yoda…")
    raw_num = bot_number or Prompt.ask("  Yoda's dedicated Signal phone number (e.g. +15551234567)")
    e164 = normalize_e164(raw_num)
    if not e164:
        console.print(f"[red]Invalid E.164 phone number:[/] {raw_num!r}")
        raise typer.Exit(code=1)

    chosen_mode = mode.strip().lower()
    if chosen_mode not in ("qr", "sms"):
        chosen_mode = Prompt.ask("  Pairing method", choices=["qr", "sms"], default="qr")

    if chosen_mode == "qr":
        console.print(
            "  [cyan]QR Link:[/] Open Signal on Yoda's phone/tablet → Settings → Linked Devices → "
            "Link New Device, then scan the QR code below."
        )
        subprocess.run(["signal-cli", "link", "-n", "Sanctum Yoda"], check=False)
    else:
        console.print(f"  Registering {e164} with Signal…")
        reg = subprocess.run(
            ["signal-cli", "-a", e164, "register"], capture_output=True, text=True, check=False
        )
        if reg.returncode != 0 and "captcha" in (reg.stderr + reg.stdout).lower():
            console.print(
                "  [yellow]Signal requires a captcha.[/] Open "
                "https://signalcaptchas.org/registration/generate.html in your browser,\n"
                "  copy the `signalcaptcha://...` link, and paste it below:"
            )
            cap = Prompt.ask("  signalcaptcha:// link").strip()
            subprocess.run(["signal-cli", "-a", e164, "register", "--captcha", cap], check=True)
        code = Prompt.ask("  6-digit SMS verification code").strip()
        subprocess.run(["signal-cli", "-a", e164, "verify", code], check=True)

    # Step 5: Wire Signal channel + LaunchAgent
    console.print("\n[bold]Step 4.[/] Wiring OpenClaw Signal channel & macOS LaunchAgent…")
    signal_cli_bin = shutil.which("signal-cli") or "/opt/homebrew/bin/signal-cli"
    subprocess.run(
        [
            openclaw_bin,
            "--profile",
            YODA_PROFILE,
            "channels",
            "add",
            "--channel",
            "signal",
            "--signal-number",
            e164,
            "--cli-path",
            signal_cli_bin,
            "--http-host",
            "127.0.0.1",
            "--http-port",
            str(http_port),
        ],
        env=setup_env,
        check=False,
    )

    plist_path = _write_launchagent(openclaw_bin=openclaw_bin)
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{YODA_PLIST_LABEL}"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        capture_output=True,
        check=False,
    )

    save_yoda_state(
        {
            "installed": True,
            "profile": YODA_PROFILE,
            "backend": chosen_backend,
            "http_port": http_port,
            "plist_path": str(plist_path),
            "signal_cli_version": _cmd_version(["signal-cli", "--version"]),
            "openclaw_version": _cmd_version(["openclaw", "--version"]),
            "installed_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )

    start_hint = (
        "  1. Verify Rust engine is live: [cyan]sanctum haus status[/]\n"
        if chosen_backend == "rust"
        else "  1. Start your local Dragon (if using Dragon): [cyan]sanctum dragon start[/]\n"
    )
    console.print(
        Panel(
            "[bold green]Yoda on Signal is configured![/]\n\n"
            f"{start_hint}"
            f"  2. Send a Signal message from your personal phone to [bold]{e164}[/]\n"
            f"  3. Approve the pairing code shown in Signal:\n"
            f"     [cyan]openclaw --profile {YODA_PROFILE} pairing approve signal <CODE>[/]\n"
            "  4. Check health anytime: [cyan]sanctum yoda status[/]\n"
            "  5. Update everything later: [cyan]sanctum update[/]",
            border_style="green",
        )
    )


@app.command("status", help="Show Yoda on Signal runtime status (signal-cli, OpenClaw, Rust proxyd/sanctum-mlx).")
def yoda_status(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    state = load_yoda_state()
    sig_ver = _cmd_version(["signal-cli", "--version"])
    oc_ver = _cmd_version(["openclaw", "--version"])
    proxyd_up = haus_cmd.proxyd_health()
    mlx_up = haus_cmd.mlx_health()
    dragon_up = dragon_cmd._dragon_health() is not None
    default_backend = (
        "rust"
        if (haus_cmd.is_rust_suite_installed() or proxyd_up)
        else ("dragon" if dragon_cmd.is_dragon_installed() else "unconfigured")
    )
    info = {
        "installed": bool(state.get("installed")),
        "operator_haus": is_operator_haus(),
        "profile": state.get("profile") or YODA_PROFILE,
        "backend": state.get("backend") or default_backend,
        "signal_cli": sig_ver,
        "openclaw": oc_ver,
        "proxyd_running": proxyd_up,
        "sanctum_mlx_running": mlx_up,
        "dragon_running": dragon_up or mlx_up,
        "plist_path": state.get("plist_path"),
    }
    if json_output:
        print(json.dumps(info, indent=2))
        return

    t = Table(title="Yoda on Signal", show_header=False, box=None, pad_edge=False)
    t.add_column(style="bold cyan")
    t.add_column()
    if info["operator_haus"] and not info["installed"]:
        t.add_row("mode", "[green]full operator haus (native Signal daemon)[/]")
    else:
        t.add_row(
            "configured",
            "[green]yes[/]" if info["installed"] else "[dim]no (run `sanctum yoda setup`)[/]",
        )
    t.add_row("signal-cli", sig_ver or "[dim]not installed[/]")
    t.add_row("openclaw", oc_ver or "[dim]not installed[/]")
    t.add_row("proxyd (:4040 TLS)", "[green]running[/]" if proxyd_up else "[dim]stopped[/]")
    t.add_row("sanctum-mlx (:1337 mTLS)", "[green]running[/]" if mlx_up else "[dim]stopped[/]")
    if dragon_up:
        t.add_row("dragon (:8787 python)", "[green]running[/]")
    console.print(t)


@app.command("chat", help="Talk directly to Yoda via Rust proxyd (:4040) or local Dragon (:8787).")
def yoda_chat(
    message: Annotated[list[str], typer.Argument(help="Message to send to Yoda.")],
    voice: Annotated[bool, typer.Option("--voice", help="Speak the reply aloud with macOS say.")] = False,
) -> None:
    text_in = " ".join(message).strip()
    if not text_in:
        console.print("[red]Message cannot be empty.[/]")
        raise typer.Exit(code=1)

    ws_soul = Path.home() / f".openclaw-{YODA_PROFILE}" / "workspace-yoda" / "SOUL.md"
    soul = ws_soul.read_text(encoding="utf-8") if ws_soul.is_file() else YODA_SOUL_MD

    ca_cert = Path.home() / ".sanctum" / "certs" / "ca.crt"
    if haus_cmd.proxyd_health() and ca_cert.is_file():
        url = "https://127.0.0.1:4040/v1/chat/completions"
        model_id = "council-mlx"
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca_cert))
    elif dragon_cmd._dragon_health() is not None:
        url = f"http://127.0.0.1:{dragon_cmd.DEFAULT_DRAGON_PORT}/v1/chat/completions"
        model_id = "sanctum-dragon"
        verify = True
    else:
        console.print(
            "[yellow]No local Sanctum engine running.[/] "
            "Start with `sanctum haus start` (or `sanctum dragon start`)."
        )
        raise typer.Exit(code=1)

    try:
        with httpx.Client(timeout=300.0, verify=verify) as client:
            r = client.post(
                url,
                headers={"Authorization": "Bearer sanctum-local"},
                json={
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": soul},
                        {"role": "user", "content": text_in},
                    ],
                },
            )
            r.raise_for_status()
            reply = str(r.json()["choices"][0]["message"]["content"]).strip()
    except Exception as exc:
        console.print(f"[red]Yoda request failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(reply)
    if voice and shutil.which("say"):
        subprocess.run(["say", "-r", "140", reply[:500]], check=False)


@app.command("update", help="Update signal-cli, OpenClaw, and @openclaw/signal to latest.")
def yoda_update(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show plan without upgrading.")] = False,
) -> None:
    if not is_yoda_signal_installed():
        console.print("[yellow]Yoda on Signal is not configured yet.[/] Run `sanctum yoda setup`.")
        raise typer.Exit(code=1)
    ok, msg = update_yoda_if_needed(dry_run=dry_run)
    if ok:
        console.print(f"[green]✓[/] Yoda/Signal: {msg}")
    else:
        console.print(f"[red]✗[/] Yoda/Signal update failed: {msg}")
        raise typer.Exit(code=1)
