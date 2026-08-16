"""Google Drive setup wizard.

Collapses the manual 7-step Cloud Console click-ops + rclone OAuth dance
into a guided flow with bounded waits, validated input, and a round-trip
canary. The wizard cannot skip Google's verification gate — that is a
deliberate Google policy. What it CAN do:

  - Open the right Cloud Console URLs at the right moment.
  - Validate ``client_id`` / ``client_secret`` paste live.
  - Configure rclone non-interactively via ``rclone config create``.
  - Drive ``rclone config reconnect`` with bounded timeout — surfaces a
    clear error if the operator never completes OAuth in the browser.
  - Run ``restic init`` with the rclone backend, then a round-trip
    canary, then atomically merge into ``instance.yaml``.

The previous-generation manual flow took ~90 minutes. With this wizard
the operator's web-side time drops to roughly 5 minutes, and the rest
runs unattended.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import webbrowser
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from sanctum_cli import config, keychain
from sanctum_cli.backends.b2 import (  # reuse helpers
    KEYCHAIN_ACCOUNT_RESTIC,
    KEYCHAIN_SERVICE_RESTIC,
    _ensure_keychain_entry,
    _gen_passphrase,
    _persist_to_instance_yaml,
    _prompt_validated,
    _round_trip,
)
from sanctum_cli.errors import LocalError, UserError

if TYPE_CHECKING:
    from pathlib import Path

console = Console()

CLIENT_ID_RE = re.compile(r"^\d{9,14}-[a-z0-9]{20,40}\.apps\.googleusercontent\.com$")
CLIENT_SECRET_RE = re.compile(r"^GOCSPX-[A-Za-z0-9_-]{20,60}$")
RCLONE_REMOTE = "gdrive-sanctum"
RCLONE_TIMEOUT_S = 30
RECONNECT_TIMEOUT_S = 300  # 5 minutes for the OAuth click-through

CONSOLE_URL_PROJECT = "https://console.cloud.google.com/projectcreate"
CONSOLE_URL_ENABLE_DRIVE = "https://console.cloud.google.com/apis/library/drive.googleapis.com"
CONSOLE_URL_OAUTH_CONSENT = "https://console.cloud.google.com/apis/credentials/consent"
CONSOLE_URL_CREDENTIALS = "https://console.cloud.google.com/apis/credentials"


@dataclass(frozen=True, slots=True)
class _SetupResult:
    remote: str
    bucket_path: str
    keychain_service_restic: str


# ─── Pre-flight ─────────────────────────────────────────────────────


def _preflight() -> None:
    if not shutil.which("rclone"):
        msg = "rclone not installed"
        raise UserError(
            msg, fix="brew install rclone && re-run `sanctum cloud setup --backend gdrive`"
        )
    if not shutil.which("restic"):
        msg = "restic not installed"
        raise UserError(msg, fix="brew install restic")


def _existing_remote() -> bool:
    out = subprocess.run(
        ["rclone", "listremotes"],
        capture_output=True,
        text=True,
        check=False,
        timeout=RCLONE_TIMEOUT_S,
    )
    if out.returncode != 0:
        return False
    return f"{RCLONE_REMOTE}:" in out.stdout


# ─── Cloud Console walk-through ─────────────────────────────────────


def _walk_console(auto_open: bool) -> None:
    """Step the operator through the irreducible web clicks."""
    console.print(
        Panel.fit(
            "[bold]Cloud Console steps[/]\n\n"
            "Google requires a browser session for OAuth client creation. "
            "I'll open each page; complete the action and return here.",
            border_style="cyan",
        )
    )

    steps: list[tuple[str, str, str]] = [
        (
            "Create a Cloud project (or pick an existing one)",
            CONSOLE_URL_PROJECT,
            "Name it [cyan]sanctum-backups[/] (or anything). Click CREATE, wait for the project switcher to update.",
        ),
        (
            "Enable the Google Drive API on that project",
            CONSOLE_URL_ENABLE_DRIVE,
            "Click ENABLE.",
        ),
        (
            "Configure the OAuth consent screen",
            CONSOLE_URL_OAUTH_CONSENT,
            "User Type = External. App name = sanctum-backups. Add YOUR email under Developer "
            "contact + Test users. SAVE through every step.",
        ),
        (
            "Click PUBLISH APP on the consent screen",
            CONSOLE_URL_OAUTH_CONSENT,
            "Status flips to [bold]In production[/]. The 'unverified app' warning at OAuth time "
            "is fine — you authored this app, you authorize it.",
        ),
        (
            "Create OAuth Desktop client credentials",
            CONSOLE_URL_CREDENTIALS,
            "+CREATE CREDENTIALS → OAuth client ID → Application type: [cyan]Desktop app[/] → "
            "Name: rclone → CREATE. Copy the client ID and client secret from the modal "
            "(or click the row → DOWNLOAD JSON).",
        ),
    ]
    for n, (title, url, hint) in enumerate(steps, start=1):
        console.print(f"\n[bold]{n}.[/] {title}")
        console.print(f"   [dim]{url}[/]")
        console.print(f"   {hint}")
        if auto_open:
            webbrowser.open(url)
        Confirm.ask("   done?", default=True)


# ─── rclone wiring ──────────────────────────────────────────────────


def _rclone_config_create(client_id: str, client_secret: str) -> None:
    """Create the gdrive-sanctum remote non-interactively."""
    proc = subprocess.run(
        [
            "rclone",
            "config",
            "create",
            RCLONE_REMOTE,
            "drive",
            "client_id",
            client_id,
            "client_secret",
            client_secret,
            "scope",
            "drive",
            "config_is_local",
            "false",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=RCLONE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        msg = f"rclone config create failed: {proc.stderr.strip() or 'unknown'}"
        raise UserError(msg)


def _rclone_reconnect() -> None:
    """Run reconnect — opens browser; bounded by RECONNECT_TIMEOUT_S."""
    console.print(
        f"\n[bold]rclone reconnect[/] will open a browser for OAuth. Click [bold]Advanced → "
        f"Go to sanctum-backups (unsafe) → Allow[/]. Waiting up to {RECONNECT_TIMEOUT_S // 60} minutes."
    )
    proc = subprocess.run(
        ["rclone", "config", "reconnect", f"{RCLONE_REMOTE}:"],
        capture_output=True,
        text=True,
        check=False,
        timeout=RECONNECT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        msg = f"rclone reconnect failed: {proc.stderr.strip() or 'unknown'}"
        raise UserError(
            msg,
            fix="re-run the wizard; check that you clicked Allow in the browser before timeout",
        )


def _rclone_mkdir(remote_path: str) -> None:
    proc = subprocess.run(
        ["rclone", "mkdir", remote_path],
        capture_output=True,
        text=True,
        check=False,
        timeout=RCLONE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        msg = f"rclone mkdir {remote_path} failed: {proc.stderr.strip() or 'unknown'}"
        raise UserError(msg)


def _rclone_about() -> tuple[bool, str]:
    proc = subprocess.run(
        ["rclone", "about", f"{RCLONE_REMOTE}:"],
        capture_output=True,
        text=True,
        check=False,
        timeout=RCLONE_TIMEOUT_S,
    )
    return proc.returncode == 0, proc.stdout if proc.returncode == 0 else proc.stderr


# ─── Wizard ─────────────────────────────────────────────────────────


def run_wizard(*, auto_open: bool = True, persist: bool = True) -> _SetupResult:
    cfg = config.load()
    _preflight()

    cb = cfg.cli.cloud_backup
    target_slot: str = "primary"
    if cb is not None and cb.primary is not None:
        target_slot = "secondary"
        if cb.secondary is not None:
            msg = "both cloud_backup.primary and .secondary already set"
            raise UserError(
                msg,
                fix="edit ~/.sanctum/instance.yaml manually to free a slot before re-running",
            )

    console.print(
        Panel.fit(
            "[bold]Sanctum cloud-setup wizard — Google Drive[/]\n\n"
            "We'll wire a restic repo against your Google Drive: walk the Cloud Console "
            "steps once, paste an OAuth client_id + secret, run rclone reconnect to mint a "
            "long-lived token, init the repo, run a tiny round-trip test, and update "
            "[cyan]~/.sanctum/instance.yaml[/].",
            border_style="cyan",
        )
    )

    # 1. Cloud Console
    _walk_console(auto_open=auto_open)

    # 2. Paste client_id + secret with regex validation
    console.print()
    client_id = _prompt_validated(
        "client_id (NNNNNN-...apps.googleusercontent.com)",
        validator=lambda v: bool(CLIENT_ID_RE.match(v.strip())),
        hint=f"expected pattern matching {CLIENT_ID_RE.pattern}",
    )
    client_secret = _prompt_validated(
        "client_secret (GOCSPX-...)",
        validator=lambda v: bool(CLIENT_SECRET_RE.match(v.strip())),
        hint="expected to start with GOCSPX-",
        password=True,
    )

    # 3. rclone wiring
    if _existing_remote() and not Confirm.ask(
        f"  [yellow]rclone remote {RCLONE_REMOTE!r} already exists. Replace it?[/]",
        default=True,
    ):
        msg = "user declined to replace existing rclone remote"
        raise UserError(msg)
    console.print(f"\n[bold]Configuring rclone remote {RCLONE_REMOTE}…[/]")
    _rclone_config_create(client_id, client_secret)
    _rclone_reconnect()
    ok, detail = _rclone_about()
    if not ok:
        msg = f"rclone about {RCLONE_REMOTE}: failed: {detail.strip()[:160]}"
        raise UserError(msg, fix="OAuth may not have completed; retry the wizard")
    console.print("  [green]✓[/] OAuth complete — token persisted by rclone")

    # 4. Bucket path under Drive
    bucket_path = "sanctum-restic"
    console.print(f"\n[bold]Ensuring [cyan]{RCLONE_REMOTE}:{bucket_path}[/] exists…[/]")
    _rclone_mkdir(f"{RCLONE_REMOTE}:{bucket_path}")
    console.print("  [green]✓[/] folder ready")

    # 5. Keychain — restic passphrase. Reuse existing or generate.
    passphrase: str
    if keychain.exists(account=KEYCHAIN_ACCOUNT_RESTIC, service=KEYCHAIN_SERVICE_RESTIC):
        passphrase = keychain.read(account=KEYCHAIN_ACCOUNT_RESTIC, service=KEYCHAIN_SERVICE_RESTIC)
        console.print("\n[bold]Reusing existing[/] sanctum-backup-key from Keychain.")
    else:
        passphrase = _gen_passphrase()
        _ensure_keychain_entry(
            KEYCHAIN_SERVICE_RESTIC, KEYCHAIN_ACCOUNT_RESTIC, passphrase, replace=False
        )
        console.print("\n[bold]Created[/] sanctum-backup-key in Keychain.")

    # 6. restic init
    repo = f"rclone:{RCLONE_REMOTE}:{bucket_path}"
    console.print(f"\n[bold]Initializing restic repo[/] {repo}")
    _restic_init(repo, passphrase)
    console.print("  [green]✓[/] repo initialized")

    # 7. Round-trip canary
    console.print("\n[bold]Round-trip test[/]")
    import os as _os

    env = dict(_os.environ)
    env["RESTIC_PASSWORD"] = passphrase
    _round_trip(repo, env)
    console.print("  [green]✓[/] canary survived round-trip")

    # 8. Persist into instance.yaml
    if persist:
        console.print(f"\n[bold]Updating ~/.sanctum/instance.yaml ({target_slot})…[/]")
        _persist_gdrive(
            target_slot=target_slot,
            instance_path=config.instance_path(),
            repo=repo,
            keychain_service_restic=KEYCHAIN_SERVICE_RESTIC,
            keychain_account=KEYCHAIN_ACCOUNT_RESTIC,
        )
        console.print(f"  [green]✓[/] cli.cloud_backup.{target_slot} set; .bak file written")
    else:
        console.print(
            f"\n[bold]Persistence skipped (--no-persist).[/] To wire manually:\n"
            f"  cli:\n    cloud_backup:\n      {target_slot}:\n        kind: restic\n"
            f"        repo: {repo}\n        keychain:\n"
            f"          service: {KEYCHAIN_SERVICE_RESTIC}\n          account: {KEYCHAIN_ACCOUNT_RESTIC}"
        )

    console.print()
    console.print(
        Panel.fit(
            f"remote: [cyan]{RCLONE_REMOTE}:{bucket_path}[/]\n"
            f"repo:   [cyan]{repo}[/]\n"
            f"slot:   {target_slot}",
            title="[bold green]done[/]",
            border_style="green",
        )
    )
    return _SetupResult(
        remote=RCLONE_REMOTE,
        bucket_path=bucket_path,
        keychain_service_restic=KEYCHAIN_SERVICE_RESTIC,
    )


def _restic_init(repo: str, passphrase: str) -> None:
    import os as _os

    env = dict(_os.environ)
    env["RESTIC_PASSWORD"] = passphrase
    proc = subprocess.run(
        ["restic", "-r", repo, "init"],
        capture_output=True,
        text=True,
        check=False,
        timeout=RCLONE_TIMEOUT_S * 2,
        env=env,
    )
    if proc.returncode == 0:
        return
    last = (proc.stderr.strip().splitlines() or ["restic init failed"])[-1]
    if "already initialized" in last.lower() or "config file already exists" in last.lower():
        # Tolerate re-running the wizard against an existing repo
        return
    msg = f"restic init failed: {last}"
    raise UserError(msg)


def _persist_gdrive(
    *,
    target_slot: str,
    instance_path: Path,
    repo: str,
    keychain_service_restic: str,
    keychain_account: str,
) -> None:
    """Write the new section into instance.yaml under the chosen slot."""
    if target_slot == "primary":
        # Reuse the helper from b2 (which writes to .primary)
        _persist_to_instance_yaml(
            instance_path,
            bucket=repo.removeprefix("rclone:gdrive-sanctum:"),  # placeholder, not used by helper
            keychain_service_restic=keychain_service_restic,
            keychain_account=keychain_account,
        )
        # Override the repo string the helper wrote (it always uses b2:)
        _override_repo(instance_path, slot="primary", repo=repo)
        return
    # secondary slot
    _override_repo(
        instance_path,
        slot="secondary",
        repo=repo,
        create_keychain_ref=True,
        keychain_service=keychain_service_restic,
        keychain_account=keychain_account,
    )


def _override_repo(
    instance_path: Path,
    *,
    slot: str,
    repo: str,
    create_keychain_ref: bool = False,
    keychain_service: str = "",
    keychain_account: str = "",
) -> None:
    from datetime import datetime

    import yaml as _yaml

    raw = _yaml.safe_load(instance_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        msg = "instance.yaml is not a mapping"
        raise LocalError(msg)
    cli_block = raw.setdefault("cli", {}) or {}
    cb_block = cli_block.setdefault("cloud_backup", {}) or {}
    if create_keychain_ref:
        cb_block[slot] = {
            "kind": "restic",
            "repo": repo,
            "keychain": {"service": keychain_service, "account": keychain_account},
        }
    else:
        cb_block.setdefault(slot, {})
        cb_block[slot]["repo"] = repo
    cb_block.setdefault("retention", {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12})
    cli_block["cloud_backup"] = cb_block
    raw["cli"] = cli_block
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = instance_path.with_suffix(instance_path.suffix + f".bak.{stamp}")
    backup_path.write_text(instance_path.read_text(encoding="utf-8"), encoding="utf-8")
    new_yaml = _yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)
    tmp = instance_path.with_suffix(instance_path.suffix + ".tmp")
    tmp.write_text(new_yaml, encoding="utf-8")
    tmp.replace(instance_path)
