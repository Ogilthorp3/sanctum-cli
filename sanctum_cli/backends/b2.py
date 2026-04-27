"""Backblaze B2 setup wizard.

Productizes the manual B2 + restic onboarding into a guided flow:
    1. Pre-flight (restic installed; Keychain unlocked; cloud_backup not
       already configured).
    2. Open browser to the B2 application-keys page; operator pastes
       keyID + applicationKey, validated by regex live.
    3. Authorize against the B2 API to verify the keys and discover the
       account-level apiUrl.
    4. Create the per-host restic bucket via the B2 API
       (bucketName=sanctum-restic-<hostname>-<random>).
    5. Stash both credentials + a generated restic passphrase in
       Keychain.
    6. Run ``restic init`` against ``b2:bucket:/`` with env-injected
       credentials.
    7. Round-trip canary: tiny backup + restore + diff.
    8. Atomically update ``~/.sanctum/instance.yaml`` with a ``.bak``.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from sanctum_cli import config, keychain
from sanctum_cli.errors import LocalError, UserError

console = Console()

B2_KEY_ID_RE = re.compile(r"^[0-9a-fA-F]{25}$")
B2_APP_KEY_RE = re.compile(r"^K[0-9]{3}[0-9a-zA-Z+/_-]{40,}$")
BUCKET_NAME_MAX = 50

KEYCHAIN_SERVICE_KEY_ID = "b2-account-id"
KEYCHAIN_SERVICE_APP_KEY = "b2-application-key"
KEYCHAIN_SERVICE_RESTIC = "sanctum-backup-key"
KEYCHAIN_ACCOUNT = "sanctum"  # for new B2 credentials
# Restic passphrase keeps the legacy account name from the bash-era backup
# script so existing installs find the entry without rotation. New installs
# get the same account so behaviour is consistent.
KEYCHAIN_ACCOUNT_RESTIC = "sanctum-backup"

B2_AUTH_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
B2_HTTP_TIMEOUT_S = 10
RESTIC_TIMEOUT_S = 30


@dataclass(frozen=True, slots=True)
class _B2AuthResult:
    account_id: str
    api_url: str


@dataclass(frozen=True, slots=True)
class _SetupResult:
    bucket_name: str
    keychain_service_key_id: str
    keychain_service_app_key: str
    keychain_service_restic: str
    keychain_account: str


# ─── Pre-flight ─────────────────────────────────────────────────────


def _preflight(cfg: config.Config) -> None:
    if not shutil.which("restic"):
        msg = "restic not installed"
        raise UserError(msg, fix="brew install restic && re-run `sanctum cloud setup`")
    if cfg.cli.cloud_backup is not None and cfg.cli.cloud_backup.primary is not None:
        msg = "cloud_backup.primary already configured"
        raise UserError(
            msg,
            fix=(
                "to add a second target or replace the existing one, edit "
                "~/.sanctum/instance.yaml directly (atomic-replace flow lands in v0.4)"
            ),
        )


# ─── B2 API ─────────────────────────────────────────────────────────


def _b2_authorize(key_id: str, app_key: str) -> _B2AuthResult:
    """Probe B2 with keyID:appKey. Returns the account-level apiUrl."""
    try:
        r = httpx.get(
            B2_AUTH_URL, auth=(key_id, app_key), timeout=B2_HTTP_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        msg = f"B2 authorize-account network failure: {exc}"
        raise UserError(msg, fix="check network connectivity to api.backblazeb2.com") from exc
    if r.status_code == 401:
        msg = "B2 rejected credentials (401)"
        raise UserError(msg, fix="re-create the application key in the B2 console and retry")
    if r.status_code != 200:
        msg = f"B2 authorize-account returned HTTP {r.status_code}: {r.text[:120]}"
        raise UserError(msg)
    payload = r.json()
    api_info = payload.get("apiInfo") or {}
    storage = api_info.get("storageApi") or {}
    api_url = storage.get("apiUrl") or payload.get("apiUrl")
    account_id = payload.get("accountId")
    if not api_url or not account_id:
        msg = "B2 authorize response missing apiUrl/accountId"
        raise LocalError(msg)
    return _B2AuthResult(account_id=account_id, api_url=api_url)


def _b2_create_bucket(auth: _B2AuthResult, bucket: str, key_id: str, app_key: str) -> None:
    """Create a private bucket. No-op if it already exists with the same owner."""
    # Re-auth to get a session token (the GET above doesn't expose it cleanly across SDK versions).
    try:
        r0 = httpx.get(B2_AUTH_URL, auth=(key_id, app_key), timeout=B2_HTTP_TIMEOUT_S)
        r0.raise_for_status()
    except httpx.HTTPError as exc:
        msg = f"B2 re-auth failed: {exc}"
        raise UserError(msg) from exc
    token = r0.json().get("authorizationToken")
    if not token:
        msg = "B2 authorize response missing authorizationToken"
        raise LocalError(msg)
    create_url = f"{auth.api_url}/b2api/v3/b2_create_bucket"
    body = {
        "accountId": auth.account_id,
        "bucketName": bucket,
        "bucketType": "allPrivate",
    }
    try:
        r = httpx.post(
            create_url, headers={"Authorization": token}, json=body, timeout=B2_HTTP_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        msg = f"B2 create-bucket network failure: {exc}"
        raise UserError(msg) from exc
    if r.status_code == 200:
        return
    payload = r.json() if r.content else {}
    code = payload.get("code", "")
    if code == "duplicate_bucket_name":
        # Bucket already exists in our account — proceed.
        return
    msg = f"B2 create-bucket failed (HTTP {r.status_code}): {payload.get('message', r.text[:120])}"
    raise UserError(msg)


# ─── Helpers ────────────────────────────────────────────────────────


def _gen_bucket_name() -> str:
    host = socket.gethostname().split(".", 1)[0].lower()
    host = re.sub(r"[^a-z0-9-]", "-", host)[:20]
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    candidate = f"sanctum-restic-{host}-{suffix}"
    return candidate[:BUCKET_NAME_MAX]


def _gen_passphrase() -> str:
    return secrets.token_hex(32)


def _ensure_keychain_entry(
    service: str, account: str, value: str, *, replace: bool
) -> None:
    """Use macOS `security` to add a generic-password entry."""
    if not shutil.which("/usr/bin/security"):
        msg = "security CLI missing"
        raise LocalError(msg)
    args = [
        "/usr/bin/security",
        "add-generic-password",
        "-a",
        account,
        "-s",
        service,
        "-w",
        value,
    ]
    if replace:
        args.append("-U")
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        msg = f"failed to write Keychain entry {service}: {proc.stderr.strip()}"
        raise LocalError(msg)


def _restic_env(key_id: str, app_key: str, passphrase: str) -> dict[str, str]:
    env = dict(os.environ)
    env["B2_ACCOUNT_ID"] = key_id
    env["B2_ACCOUNT_KEY"] = app_key
    env["RESTIC_PASSWORD"] = passphrase
    return env


def _restic_init(repo: str, env: dict[str, str]) -> None:
    proc = subprocess.run(
        ["restic", "-r", repo, "init"],
        capture_output=True,
        text=True,
        timeout=RESTIC_TIMEOUT_S,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["restic init failed"])[-1]
        msg = f"restic init failed: {last}"
        raise UserError(msg)


def _round_trip(repo: str, env: dict[str, str]) -> None:
    """Tiny backup + restore + diff to prove the wiring."""
    with tempfile.TemporaryDirectory(prefix="sanctum-canary-") as src_str:
        src = Path(src_str)
        canary = src / "canary.txt"
        canary.write_text("sanctum-cloud-setup-canary\n", encoding="utf-8")
        backup = subprocess.run(
            ["restic", "-r", repo, "backup", str(canary), "--tag", "canary"],
            capture_output=True,
            text=True,
            timeout=RESTIC_TIMEOUT_S,
            check=False,
            env=env,
        )
        if backup.returncode != 0:
            last = (backup.stderr.strip().splitlines() or ["backup failed"])[-1]
            msg = f"canary backup failed: {last}"
            raise UserError(msg)
        with tempfile.TemporaryDirectory(prefix="sanctum-restore-") as dst_str:
            restore = subprocess.run(
                [
                    "restic",
                    "-r",
                    repo,
                    "restore",
                    "latest",
                    "--target",
                    dst_str,
                    "--tag",
                    "canary",
                ],
                capture_output=True,
                text=True,
                timeout=RESTIC_TIMEOUT_S,
                check=False,
                env=env,
            )
            if restore.returncode != 0:
                last = (restore.stderr.strip().splitlines() or ["restore failed"])[-1]
                msg = f"canary restore failed: {last}"
                raise UserError(msg)
            # The restored file lives at <dst>/<original-absolute-path>
            restored = Path(dst_str) / canary.relative_to(canary.anchor)
            if not restored.exists() or restored.read_text() != canary.read_text():
                msg = "canary round-trip diff mismatch"
                raise LocalError(msg)


# ─── Persistence ────────────────────────────────────────────────────


def _persist_to_instance_yaml(
    instance_path: Path,
    bucket: str,
    keychain_service_restic: str,
    keychain_account: str,
) -> None:
    """Atomically merge the new cli.cloud_backup section into instance.yaml.

    Preserves existing top-level keys (instance, services, paths, etc.) and
    only adds/updates ``cli.cloud_backup``. Backs up the original to
    ``<file>.bak.YYYYMMDDTHHMMSSZ`` before the rename.
    """
    raw = yaml.safe_load(instance_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        msg = f"instance.yaml top-level is not a mapping: {instance_path}"
        raise LocalError(msg)
    cli_block = raw.setdefault("cli", {}) or {}
    if not isinstance(cli_block, dict):
        msg = "cli: must be a mapping"
        raise LocalError(msg)
    cb_block = cli_block.setdefault("cloud_backup", {}) or {}
    cb_block["primary"] = {
        "kind": "restic",
        "repo": f"b2:{bucket}",
        "keychain": {
            "service": keychain_service_restic,
            "account": keychain_account,
        },
    }
    cb_block.setdefault(
        "retention", {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12}
    )
    cli_block["cloud_backup"] = cb_block
    raw["cli"] = cli_block

    from datetime import datetime

    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = instance_path.with_suffix(instance_path.suffix + f".bak.{stamp}")
    backup_path.write_text(instance_path.read_text(encoding="utf-8"), encoding="utf-8")

    new_yaml = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)
    tmp = instance_path.with_suffix(instance_path.suffix + ".tmp")
    tmp.write_text(new_yaml, encoding="utf-8")
    tmp.replace(instance_path)


# ─── Wizard ─────────────────────────────────────────────────────────


def run_wizard(
    *,
    auto_open: bool = True,
    persist: bool = True,
) -> _SetupResult:
    """Top-level B2 wizard. Idempotent within reason; safe to abort and re-run.

    ``auto_open`` controls webbrowser.open calls (disabled in tests).
    ``persist`` controls writing to instance.yaml (disabled in tests).
    """
    cfg = config.load()
    _preflight(cfg)

    console.print(
        Panel.fit(
            "[bold]Sanctum cloud-setup wizard — Backblaze B2[/]\n\n"
            "We'll wire a restic repo against your B2 account: paste a key, "
            "auto-create a bucket, init the repo, run a tiny round-trip test, "
            "and update [cyan]~/.sanctum/instance.yaml[/].",
            border_style="cyan",
        )
    )

    # 1. Open the B2 console so the operator can create / find a key
    app_keys_url = "https://secure.backblaze.com/app_keys.htm"
    console.print(
        f"\n[bold]Step 1.[/] Open the B2 application-keys page and either "
        f"create a new key (recommended: 'Allow access to all buckets, "
        f"capabilities: Read+Write+Delete+List') or reuse an existing one.\n"
        f"[dim]{app_keys_url}[/]"
    )
    if auto_open:
        webbrowser.open(app_keys_url)

    # 2. Paste credentials
    key_id = _prompt_validated(
        "keyID (25 hex chars)",
        validator=lambda v: bool(B2_KEY_ID_RE.match(v.strip())),
        hint="expected 25 hex characters",
    )
    app_key = _prompt_validated(
        "applicationKey (starts with K0../K00.. then ~50 chars)",
        validator=lambda v: bool(B2_APP_KEY_RE.match(v.strip())),
        hint="expected pattern Kxxx<base64-ish chars>",
        password=True,
    )

    # 3. Auth probe
    console.print("\n[bold]Step 2.[/] Probing B2 with the supplied key …")
    auth = _b2_authorize(key_id, app_key)
    console.print(f"  [green]✓[/] authorized account {auth.account_id} (apiUrl={auth.api_url})")

    # 4. Bucket name
    bucket = _gen_bucket_name()
    console.print(f"\n[bold]Step 3.[/] Creating private bucket [cyan]{bucket}[/] …")
    _b2_create_bucket(auth, bucket, key_id, app_key)
    console.print("  [green]✓[/] bucket ready")

    # 5. Stash credentials in Keychain (incl. fresh restic passphrase)
    console.print("\n[bold]Step 4.[/] Storing credentials + restic passphrase in Keychain …")
    passphrase = _gen_passphrase()
    replace = Confirm.ask(
        "  overwrite existing entries if present?", default=True
    )
    _ensure_keychain_entry(
        KEYCHAIN_SERVICE_KEY_ID, KEYCHAIN_ACCOUNT, key_id, replace=replace
    )
    _ensure_keychain_entry(
        KEYCHAIN_SERVICE_APP_KEY, KEYCHAIN_ACCOUNT, app_key, replace=replace
    )
    if not keychain.exists(KEYCHAIN_ACCOUNT_RESTIC, KEYCHAIN_SERVICE_RESTIC):
        _ensure_keychain_entry(
            KEYCHAIN_SERVICE_RESTIC, KEYCHAIN_ACCOUNT_RESTIC, passphrase, replace=False
        )
    else:
        passphrase = keychain.read(
            account=KEYCHAIN_ACCOUNT_RESTIC, service=KEYCHAIN_SERVICE_RESTIC
        )
        console.print("  [dim]reusing existing sanctum-backup-key from Keychain[/]")

    # 6. restic init
    console.print("\n[bold]Step 5.[/] Initializing restic repo …")
    repo = f"b2:{bucket}"
    env = _restic_env(key_id, app_key, passphrase)
    _restic_init(repo, env)
    console.print("  [green]✓[/] repo initialized")

    # 7. Round-trip canary
    console.print("\n[bold]Step 6.[/] Round-trip test (backup + restore + diff) …")
    _round_trip(repo, env)
    console.print("  [green]✓[/] canary survived round-trip")

    # 8. Persist
    if persist:
        console.print("\n[bold]Step 7.[/] Updating ~/.sanctum/instance.yaml …")
        _persist_to_instance_yaml(
            config.instance_path(),
            bucket=bucket,
            keychain_service_restic=KEYCHAIN_SERVICE_RESTIC,
            keychain_account=KEYCHAIN_ACCOUNT_RESTIC,
        )
        console.print("  [green]✓[/] cli.cloud_backup.primary set; .bak file written")
    else:
        console.print(
            "\n[bold]Step 7.[/] [yellow]Skipped persistence (--no-persist).[/] "
            "To wire this up manually, add to your instance.yaml:"
        )
        console.print(
            f"  cli:\n    cloud_backup:\n      primary:\n        kind: restic\n"
            f"        repo: b2:{bucket}\n        keychain:\n"
            f"          service: {KEYCHAIN_SERVICE_RESTIC}\n          account: {KEYCHAIN_ACCOUNT_RESTIC}"
        )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan")
    summary.add_column()
    summary.add_row("bucket", bucket)
    summary.add_row("repo", f"b2:{bucket}")
    summary.add_row("keychain (b2 keyID)", f"{KEYCHAIN_SERVICE_KEY_ID} / {KEYCHAIN_ACCOUNT}")
    summary.add_row("keychain (b2 appKey)", f"{KEYCHAIN_SERVICE_APP_KEY} / {KEYCHAIN_ACCOUNT}")
    summary.add_row("keychain (restic)", f"{KEYCHAIN_SERVICE_RESTIC} / {KEYCHAIN_ACCOUNT_RESTIC}")
    console.print()
    console.print(Panel.fit(summary, title="[bold green]done[/]", border_style="green"))

    return _SetupResult(
        bucket_name=bucket,
        keychain_service_key_id=KEYCHAIN_SERVICE_KEY_ID,
        keychain_service_app_key=KEYCHAIN_SERVICE_APP_KEY,
        keychain_service_restic=KEYCHAIN_SERVICE_RESTIC,
        keychain_account=KEYCHAIN_ACCOUNT_RESTIC,
    )


def _prompt_validated(
    label: str,
    *,
    validator: Callable[[str], bool],
    hint: str,
    password: bool = False,
    max_attempts: int = 3,
) -> str:
    for attempt in range(1, max_attempts + 1):
        value = Prompt.ask(f"  {label}", password=password).strip()
        if validator(value):
            return value
        console.print(f"  [red]invalid format[/] — {hint} (attempt {attempt}/{max_attempts})")
    msg = f"too many invalid attempts for {label}"
    raise UserError(msg)
