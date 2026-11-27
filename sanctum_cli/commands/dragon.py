"""``sanctum dragon`` — install, run, chat with, and update the local Sanctum Dragon.

Downloads the signed, Jedi-Trials-verified Dragon champion release (default:
``dragon-2`` — ``qwen-haus-dragon-193d2d7407ec`` on ``Qwen3.8-27B-4bit`` for
32 GB+ Macs, or ``Qwen3.5-9B-4bit`` for 16 GB Macs), verifies SHA-256 before
extracting, creates an isolated ``uv`` venv with the latest tested ``mlx-lm``,
and exposes an OpenAI-compatible loopback API (``127.0.0.1:8787/v1``) for
``sanctum yoda`` and local tools.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sanctum_cli import hive_pulse
from sanctum_cli.mesh.metrics import detect_ram_gb

console = Console()
app = typer.Typer(
    name="dragon",
    help="Install, chat with, serve, and update the local Apple Silicon Dragon.",
    no_args_is_help=True,
)

ENV_DRAGON_DIR = "SANCTUM_DRAGON_DIR"
DEFAULT_DRAGON_TAG = "dragon-2"
DEFAULT_DRAGON_ADAPTER = "qwen-haus-dragon-193d2d7407ec"
DEFAULT_DRAGON_URL = (
    "https://github.com/Ogilthorp3/homebrew-sanctum/releases/download/"
    "dragon-2/sanctum-dragon-guest.zip"
)
DEFAULT_DRAGON_SHA256 = "1defbb9c13269bc6bfa40e83cae38e92716dc915a46ac385a1e2c3a61f52bf61"
DEFAULT_MLX_LM_VERSION = "0.32.2"
DEFAULT_DRAGON_PORT = 8787
MODEL_27B = "mlx-community/Qwen3.8-27B-4bit"
MODEL_9B = "mlx-community/Qwen3.5-9B-4bit"


def dragon_dir() -> Path:
    override = os.environ.get(ENV_DRAGON_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".sanctum" / "dragon"


def manifest_path() -> Path:
    return dragon_dir() / "manifest.json"


def load_manifest() -> dict[str, Any]:
    path = manifest_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(data: dict[str, Any]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def is_dragon_installed() -> bool:
    return manifest_path().is_file()


def resolve_tier(requested: str = "auto", *, ram_gb: int | None = None) -> tuple[str, str]:
    """Return ``(tier, hf_model_id)`` based on requested tier and host RAM."""
    tier = requested.strip().lower()
    if tier == "27b":
        return "27b", MODEL_27B
    if tier == "9b":
        return "9b", MODEL_9B
    mem = detect_ram_gb() if ram_gb is None else ram_gb
    if mem >= 30:
        return "27b", MODEL_27B
    return "9b", MODEL_9B


def fetch_latest_dragon_release(
    *, transport: httpx.BaseTransport | None = None
) -> dict[str, str]:
    """Fetch latest Dragon release metadata from ``hive.sanctum.run/latest.json``."""
    fallback = {
        "dragon_tag": DEFAULT_DRAGON_TAG,
        "dragon_adapter": DEFAULT_DRAGON_ADAPTER,
        "dragon_url": DEFAULT_DRAGON_URL,
        "dragon_sha256": DEFAULT_DRAGON_SHA256,
        "mlx_lm_version": DEFAULT_MLX_LM_VERSION,
    }
    endpoint = hive_pulse._endpoint()
    try:
        with httpx.Client(timeout=5.0, transport=transport) as client:
            r = client.get(f"{endpoint}/latest.json")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("dragon_tag") and data.get("dragon_url"):
                    return {
                        "dragon_tag": str(data["dragon_tag"]).strip(),
                        "dragon_adapter": str(
                            data.get("dragon_adapter") or DEFAULT_DRAGON_ADAPTER
                        ).strip(),
                        "dragon_url": str(data["dragon_url"]).strip(),
                        "dragon_sha256": str(
                            data.get("dragon_sha256") or DEFAULT_DRAGON_SHA256
                        ).strip(),
                        "mlx_lm_version": str(
                            data.get("mlx_lm_version") or DEFAULT_MLX_LM_VERSION
                        ).strip(),
                    }
    except Exception:
        pass
    return fallback


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract_zip(zip_path: Path, dest: Path) -> Path:
    """Safely extract zip into ``dest`` (rejecting path traversal / symlinks)."""
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = (dest_resolved / member.filename).resolve()
            if not str(member_path).startswith(str(dest_resolved) + os.sep) and member_path != dest_resolved:
                msg = f"unsafe zip entry rejected: {member.filename}"
                raise ValueError(msg)
        zf.extractall(dest_resolved)
    sub = dest_resolved / "sanctum-dragon-guest"
    return sub if sub.is_dir() else dest_resolved


def _download_and_verify_bundle(
    url: str,
    expected_sha256: str,
    target_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Download Dragon zip, verify SHA-256, and stage code + adapter into ``target_dir``."""
    with tempfile.TemporaryDirectory(prefix="sanctum-dragon-") as td:
        tmp_dir = Path(td)
        zip_path = tmp_dir / "sanctum-dragon-guest.zip"
        with (
            httpx.Client(timeout=300.0, follow_redirects=True, transport=transport) as client,
            client.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            with zip_path.open("wb") as out:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    out.write(chunk)

        actual_sha = _sha256_file(zip_path)
        if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
            msg = (
                f"SHA-256 mismatch for {url}: "
                f"expected {expected_sha256[:16]}…, got {actual_sha[:16]}…"
            )
            raise ValueError(msg)

        extract_root = tmp_dir / "extract"
        extract_root.mkdir(parents=True, exist_ok=True)
        src_root = _safe_extract_zip(zip_path, extract_root)

        target_dir.mkdir(parents=True, exist_ok=True)
        for name in ("chat.py", "serve.py", "chat.sh", "serve.sh", "README.md"):
            src = src_root / name
            if src.is_file():
                dst = target_dir / name
                shutil.copy2(src, dst)
                if name.endswith(".sh"):
                    dst.chmod(0o755)

        src_adapter = src_root / "adapter"
        if src_adapter.is_dir():
            dst_adapter = target_dir / "adapter"
            if dst_adapter.exists():
                shutil.rmtree(dst_adapter)
            shutil.copytree(src_adapter, dst_adapter)

        return actual_sha


def _ensure_venv(target_dir: Path, mlx_lm_version: str) -> Path:
    uv_bin = shutil.which("uv")
    if not uv_bin:
        brew_bin = shutil.which("brew")
        if not brew_bin:
            msg = "uv not found and Homebrew missing — install uv first: brew install uv"
            raise RuntimeError(msg)
        subprocess.run([brew_bin, "install", "uv"], check=True)
        uv_bin = shutil.which("uv") or "/opt/homebrew/bin/uv"

    venv_dir = target_dir / ".venv"
    py_bin = venv_dir / "bin" / "python"
    if not py_bin.is_file():
        subprocess.run([uv_bin, "venv", "--python", "3.12", str(venv_dir)], check=True)
    subprocess.run(
        [uv_bin, "pip", "install", "--python", str(py_bin), f"mlx-lm=={mlx_lm_version}"],
        check=True,
    )
    return py_bin


def _dragon_health(port: int = DEFAULT_DRAGON_PORT) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=1.5) as client:
            r = client.get(f"http://127.0.0.1:{port}/health")
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, dict) else {"status": "ok"}
    except Exception:
        return None
    return None


def _pid_file() -> Path:
    return dragon_dir() / "dragon.pid"


def _stop_server_process() -> bool:
    pf = _pid_file()
    if not pf.is_file():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        pf.unlink(missing_ok=True)
        return True
    except (ValueError, ProcessLookupError, OSError):
        pf.unlink(missing_ok=True)
        return False


def update_dragon_if_needed(
    *,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, str]:
    """Check and apply latest Dragon release + mlx-lm if installed.

    Returns ``(ok, human_summary)``. Never raises.
    """
    mf = load_manifest()
    if not mf:
        return True, "not installed"

    latest = fetch_latest_dragon_release(transport=transport)
    cur_tag = str(mf.get("release_tag") or "")
    cur_sha = str(mf.get("zip_sha256") or "")
    cur_mlx = str(mf.get("mlx_lm_version") or "")
    needs_bundle = (
        cur_tag != latest["dragon_tag"]
        or (bool(latest["dragon_sha256"]) and cur_sha.lower() != latest["dragon_sha256"].lower())
    )
    needs_mlx = cur_mlx != latest["mlx_lm_version"]

    if not needs_bundle and not needs_mlx:
        return True, f"already up-to-date ({cur_tag} · mlx-lm {cur_mlx})"

    if dry_run:
        return (
            True,
            f"would upgrade {cur_tag or '?'} -> {latest['dragon_tag']} "
            f"(mlx-lm {cur_mlx or '?'} -> {latest['mlx_lm_version']})",
        )

    was_running = _dragon_health() is not None
    try:
        target = dragon_dir()
        actual_sha = cur_sha
        if needs_bundle:
            actual_sha = _download_and_verify_bundle(
                latest["dragon_url"],
                latest["dragon_sha256"],
                target,
                transport=transport,
            )
        if needs_mlx:
            _ensure_venv(target, latest["mlx_lm_version"])

        mf.update(
            {
                "release_tag": latest["dragon_tag"],
                "champion_id": latest["dragon_adapter"],
                "zip_sha256": actual_sha,
                "mlx_lm_version": latest["mlx_lm_version"],
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        save_manifest(mf)
        if was_running:
            _stop_server_process()
            return (
                True,
                f"upgraded {cur_tag} -> {latest['dragon_tag']} "
                "(stopped old server; restart with `sanctum dragon start`)",
            )
        return True, f"upgraded {cur_tag} -> {latest['dragon_tag']} (mlx-lm {latest['mlx_lm_version']})"
    except Exception as exc:
        return False, f"failed: {exc}"


@app.command("install", help="Install the latest local Sanctum Dragon (27B champion on 32GB+, 9B on 16GB).")
def dragon_install(
    tier: Annotated[
        str, typer.Option("--tier", help="Model tier: auto | 27b | 9b.")
    ] = "auto",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show plan without downloading or installing.")
    ] = False,
) -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        console.print("[red]The Sanctum Dragon requires an Apple Silicon Mac (M1/M2/M3/M4).[/]")
        raise typer.Exit(code=1)

    ram_gb = detect_ram_gb()
    chosen_tier, model_id = resolve_tier(tier, ram_gb=ram_gb)
    if ram_gb and ram_gb < 16:
        console.print(f"[red]This Mac has {ram_gb} GB RAM. Local models need at least 16 GB.[/]")
        raise typer.Exit(code=1)
    if chosen_tier == "27b" and ram_gb and ram_gb < 30:
        console.print(
            f"[yellow]Warning:[/] {ram_gb} GB detected — the 27B Dragon needs 32 GB. "
            "Use `--tier 9b` for 16 GB Macs."
        )

    latest = fetch_latest_dragon_release()
    target = dragon_dir()

    console.print(
        Panel.fit(
            f"[bold]Sanctum Dragon Installer[/]\n"
            f"Release:  [cyan]{latest['dragon_tag']}[/] ({latest['dragon_adapter']})\n"
            f"Tier:     [bold]{chosen_tier}[/] → {model_id}\n"
            f"Runtime:  mlx-lm=={latest['mlx_lm_version']}\n"
            f"Target:   {target}",
            border_style="cyan",
        )
    )

    if dry_run:
        console.print("[dim](dry-run — no files downloaded or modified)[/]")
        return

    console.print("[bold]Step 1.[/] Downloading & verifying Dragon champion bundle…")
    try:
        actual_sha = _download_and_verify_bundle(
            latest["dragon_url"], latest["dragon_sha256"], target
        )
    except Exception as exc:
        console.print(f"[red]Download/verify failed:[/] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"  [green]✓[/] Verified SHA-256 ({actual_sha[:16]}…)")

    console.print(f"[bold]Step 2.[/] Setting up isolated Python venv (mlx-lm=={latest['mlx_lm_version']})…")
    try:
        _ensure_venv(target, latest["mlx_lm_version"])
    except Exception as exc:
        console.print(f"[red]Venv setup failed:[/] {exc}")
        raise typer.Exit(code=1) from exc
    console.print("  [green]✓[/] Dragon runtime ready")

    save_manifest(
        {
            "release_tag": latest["dragon_tag"],
            "champion_id": latest["dragon_adapter"],
            "zip_sha256": actual_sha,
            "mlx_lm_version": latest["mlx_lm_version"],
            "tier": chosen_tier,
            "model_id": model_id,
            "installed_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )

    console.print(
        Panel(
            f"[bold green]Dragon installed ({latest['dragon_tag']}).[/]\n\n"
            "  [cyan]sanctum dragon chat[/]    talk directly to your local Dragon\n"
            "  [cyan]sanctum dragon start[/]   start local API (127.0.0.1:8787) for Yoda\n"
            "  [cyan]sanctum yoda setup[/]     connect Yoda on Signal\n"
            "  [cyan]sanctum update[/]         keep CLI + Dragon + Yoda on latest",
            border_style="green",
        )
    )


@app.command("update", help="Update the installed Dragon champion adapter and mlx-lm to latest.")
def dragon_update(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Check only, do not apply.")] = False,
) -> None:
    if not is_dragon_installed():
        console.print("[yellow]Dragon is not installed yet.[/] Run `sanctum dragon install`.")
        raise typer.Exit(code=1)
    ok, msg = update_dragon_if_needed(dry_run=dry_run)
    if ok:
        console.print(f"[green]✓[/] Dragon: {msg}")
    else:
        console.print(f"[red]✗[/] Dragon {msg}")
        raise typer.Exit(code=1)


@app.command("chat", help="Start an interactive local chat with your Sanctum Dragon.")
def dragon_chat() -> None:
    mf = load_manifest()
    py = dragon_dir() / ".venv" / "bin" / "python"
    script = dragon_dir() / "chat.py"
    if not mf or not py.is_file() or not script.is_file():
        console.print("[yellow]Dragon is not installed.[/] Run `sanctum dragon install` first.")
        raise typer.Exit(code=1)
    env = dict(os.environ)
    env.setdefault("SANCTUM_DRAGON_TIER", str(mf.get("tier") or "auto"))
    proc = subprocess.run([str(py), str(script)], env=env, check=False)
    raise typer.Exit(code=proc.returncode)


@app.command("serve", help="Run the Dragon OpenAI-compatible loopback server in the foreground.")
def dragon_serve(
    port: Annotated[int, typer.Option("--port", help="Loopback port.")] = DEFAULT_DRAGON_PORT,
) -> None:
    mf = load_manifest()
    py = dragon_dir() / ".venv" / "bin" / "python"
    script = dragon_dir() / "serve.py"
    if not mf or not py.is_file() or not script.is_file():
        console.print("[yellow]Dragon is not installed.[/] Run `sanctum dragon install` first.")
        raise typer.Exit(code=1)
    env = dict(os.environ)
    env["SANCTUM_DRAGON_PORT"] = str(port)
    env.setdefault("SANCTUM_DRAGON_TIER", str(mf.get("tier") or "auto"))
    proc = subprocess.run([str(py), str(script)], env=env, check=False)
    raise typer.Exit(code=proc.returncode)


@app.command("start", help="Start the local Dragon loopback server in the background.")
def dragon_start(
    port: Annotated[int, typer.Option("--port", help="Loopback port.")] = DEFAULT_DRAGON_PORT,
    wait_seconds: Annotated[int, typer.Option("--wait", help="Seconds to wait for /health.")] = 120,
) -> None:
    if _dragon_health(port) is not None:
        console.print(f"[green]✓[/] Dragon server already healthy on http://127.0.0.1:{port}")
        return
    mf = load_manifest()
    py = dragon_dir() / ".venv" / "bin" / "python"
    script = dragon_dir() / "serve.py"
    if not mf or not py.is_file() or not script.is_file():
        console.print("[yellow]Dragon is not installed.[/] Run `sanctum dragon install` first.")
        raise typer.Exit(code=1)

    log_path = Path.home() / ".sanctum" / "logs" / "dragon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["SANCTUM_DRAGON_PORT"] = str(port)
    env.setdefault("SANCTUM_DRAGON_TIER", str(mf.get("tier") or "auto"))

    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            [str(py), str(script)],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    _pid_file().write_text(f"{proc.pid}\n", encoding="utf-8")
    console.print(
        f"Starting Dragon ({mf.get('release_tag')}, pid {proc.pid}) — "
        "first launch downloads weights (~15 GB on 27B / ~5.5 GB on 9B)…"
    )
    deadline = time.monotonic() + max(1, wait_seconds)
    while time.monotonic() < deadline:
        if _dragon_health(port) is not None:
            console.print(f"[green]✓[/] Dragon ready at http://127.0.0.1:{port}/v1")
            return
        if proc.poll() is not None:
            console.print(f"[red]Dragon exited early ({proc.returncode}).[/] Check {log_path}")
            raise typer.Exit(code=1)
        time.sleep(2)
    console.print(
        f"[yellow]Still loading weights in background (pid {proc.pid}).[/] "
        f"Check progress: tail -f {log_path}"
    )


@app.command("stop", help="Stop the background Dragon server and release unified memory.")
def dragon_stop() -> None:
    if _stop_server_process():
        console.print("[green]✓[/] Dragon server stopped (memory released).")
    else:
        console.print("[dim]No background Dragon server pid found.[/]")


@app.command("status", help="Show installed Dragon release, runtime health, and available updates.")
def dragon_status(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    port: Annotated[int, typer.Option("--port", help="Loopback port.")] = DEFAULT_DRAGON_PORT,
) -> None:
    mf = load_manifest()
    health = _dragon_health(port)
    latest = fetch_latest_dragon_release()
    update_available = bool(mf) and (
        mf.get("release_tag") != latest["dragon_tag"]
        or mf.get("mlx_lm_version") != latest["mlx_lm_version"]
    )
    info = {
        "installed": bool(mf),
        "dir": str(dragon_dir()),
        "release_tag": mf.get("release_tag"),
        "champion_id": mf.get("champion_id"),
        "tier": mf.get("tier"),
        "model_id": mf.get("model_id"),
        "mlx_lm_version": mf.get("mlx_lm_version"),
        "server_running": health is not None,
        "server_url": f"http://127.0.0.1:{port}/v1",
        "latest_tag": latest["dragon_tag"],
        "latest_mlx_lm": latest["mlx_lm_version"],
        "update_available": update_available,
    }
    if json_output:
        print(json.dumps(info, indent=2))
        return

    t = Table(title="Sanctum Dragon", show_header=False, box=None, pad_edge=False)
    t.add_column(style="bold cyan")
    t.add_column()
    t.add_row("installed", "[green]yes[/]" if info["installed"] else "[dim]no (run `sanctum dragon install`)[/]")
    if info["installed"]:
        t.add_row("release", f"{info['release_tag']} ({info['champion_id']})")
        t.add_row("tier / model", f"{info['tier']} · {info['model_id']}")
        t.add_row("mlx-lm", str(info["mlx_lm_version"]))
    t.add_row(
        "server",
        f"[green]running[/] ({info['server_url']})" if info["server_running"] else "[dim]stopped[/]",
    )
    t.add_row(
        "latest",
        f"{info['latest_tag']} · mlx-lm {info['latest_mlx_lm']}"
        + (" [yellow](update available — run `sanctum update`)[/]" if update_available else " [green](current)[/]"),
    )
    console.print(t)
    with contextlib.suppress(Exception):
        hive_pulse.maybe_heartbeat("status")
