"""Cloudflare R2 setup wizard.

R2 is the recommended default for Sanctum cloud backups: 10 GB free
storage, 1 M Class A + 10 M Class B ops/month free, and **zero egress
charges, ever**. The egress-free property is exactly what a backup
target wants — restore drills cost nothing.

The wizard collects three things from the operator:
  - Cloudflare account ID (32 hex chars, also visible in the dashboard URL)
  - R2 access key ID (32 hex chars)
  - R2 secret access key (64 hex chars)

Auth is probed via ListBuckets (S3 GET /) signed with SigV4. Bucket
creation is a single SigV4 PUT against the account endpoint. We do not
require rclone for R2 — restic talks to R2 directly via its native S3
backend, and ``boto3`` is too heavy for what amounts to two API calls.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import shutil
import socket
import string
import subprocess
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from sanctum_cli import config, keychain
from sanctum_cli.backends.b2 import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_ACCOUNT_RESTIC,
    KEYCHAIN_SERVICE_RESTIC,
    _ensure_keychain_entry,
    _gen_passphrase,
    _prompt_validated,
)
from sanctum_cli.errors import LocalError, UserError

console = Console()

# Cloudflare canonical formats:
#   Account ID:        32 lowercase hex chars
#   Access Key ID:     32 lowercase hex chars
#   Secret Access Key: 64 lowercase hex chars
R2_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
R2_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
BUCKET_NAME_MAX = 63

KEYCHAIN_SERVICE_R2_ACCESS_KEY = "r2-access-key-id"
KEYCHAIN_SERVICE_R2_SECRET = "r2-secret-access-key"

R2_DASHBOARD_URL = "https://dash.cloudflare.com/?to=/:account/r2/api-tokens"
HTTP_TIMEOUT_S = 10
RESTIC_TIMEOUT_S = 30


@dataclass(frozen=True, slots=True)
class _R2Creds:
    account_id: str
    access_key: str
    secret_key: str

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


@dataclass(frozen=True, slots=True)
class _SetupResult:
    account_id: str
    bucket: str
    repo: str
    keychain_account: str


# ─── Pre-flight ─────────────────────────────────────────────────────


def _preflight(cfg: config.Config) -> str:
    """Return the slot to populate: ``primary`` or ``secondary``."""
    if not shutil.which("restic"):
        msg = "restic not installed"
        raise UserError(msg, fix="brew install restic && re-run `sanctum cloud setup`")
    cb = cfg.cli.cloud_backup
    if cb is None or cb.primary is None:
        return "primary"
    if cb.secondary is None:
        return "secondary"
    msg = "both cloud_backup.primary and .secondary already configured"
    raise UserError(
        msg,
        fix=(
            "free a slot by editing ~/.sanctum/instance.yaml directly, then re-run; "
            "atomic-replace flow lands in v0.7"
        ),
    )


# ─── SigV4 signing ──────────────────────────────────────────────────


def _sigv4_sign(
    creds: _R2Creds,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return headers (incl. Authorization) for an R2 S3 request.

    R2 ignores the region in the credential scope but expects ``auto``;
    using a different region works in practice but ``auto`` is the
    documented contract.
    """
    region = "auto"
    service = "s3"
    host = f"{creds.account_id}.r2.cloudflarestorage.com"

    payload_hash = hashlib.sha256(body).hexdigest()
    now = datetime.now(tz=UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if extra_headers:
        for k, v in extra_headers.items():
            headers[k.lower()] = v

    sorted_keys = sorted(headers)
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted_keys)
    signed_headers = ";".join(sorted_keys)

    canonical_request = "\n".join(
        [method, path, "", canonical_headers, signed_headers, payload_hash]
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    k_date = hmac.new(
        ("AWS4" + creds.secret_key).encode(), date_stamp.encode(), hashlib.sha256
    ).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={creds.access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    return headers


# ─── R2 API operations ──────────────────────────────────────────────


def _list_buckets(creds: _R2Creds) -> list[str]:
    """Auth probe: returns bucket names. Raises UserError on auth failure."""
    headers = _sigv4_sign(creds, "GET", "/")
    try:
        r = httpx.get(creds.endpoint + "/", headers=headers, timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        msg = f"R2 ListBuckets network failure: {exc}"
        raise UserError(msg, fix="check connectivity to *.r2.cloudflarestorage.com") from exc
    if r.status_code == 403:
        msg = "R2 rejected credentials (403)"
        raise UserError(
            msg,
            fix=(
                "the access key may be missing 'Object Read & Write' permissions, "
                "or the account ID may not match the key. Re-create the key in "
                "the R2 dashboard with full read/write."
            ),
        )
    if r.status_code != 200:
        msg = f"R2 ListBuckets returned HTTP {r.status_code}: {r.text[:160]}"
        raise UserError(msg)
    # Extract bucket names from the XML — minimal regex avoids an XML dep
    return re.findall(r"<Name>([^<]+)</Name>", r.text)


def _create_bucket(creds: _R2Creds, bucket: str) -> None:
    """Idempotent: BucketAlreadyOwnedByYou is treated as success."""
    headers = _sigv4_sign(creds, "PUT", f"/{bucket}")
    try:
        r = httpx.put(
            f"{creds.endpoint}/{bucket}", headers=headers, timeout=HTTP_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        msg = f"R2 CreateBucket network failure: {exc}"
        raise UserError(msg) from exc
    if r.status_code == 200:
        return
    if r.status_code == 409 and "BucketAlreadyOwnedByYou" in r.text:
        return
    msg = f"R2 CreateBucket failed (HTTP {r.status_code}): {r.text[:200]}"
    raise UserError(msg)


# ─── Helpers ────────────────────────────────────────────────────────


def _gen_bucket_name() -> str:
    host = socket.gethostname().split(".", 1)[0].lower()
    host = re.sub(r"[^a-z0-9-]", "-", host)[:20]
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    candidate = f"sanctum-restic-{host}-{suffix}"
    return candidate[:BUCKET_NAME_MAX]


def _restic_env(creds: _R2Creds, passphrase: str) -> dict[str, str]:
    import os as _os

    env = dict(_os.environ)
    env["AWS_ACCESS_KEY_ID"] = creds.access_key
    env["AWS_SECRET_ACCESS_KEY"] = creds.secret_key
    env["AWS_DEFAULT_REGION"] = "auto"
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
    if proc.returncode == 0:
        return
    last = (proc.stderr.strip().splitlines() or ["restic init failed"])[-1]
    if "already initialized" in last.lower() or "config file already exists" in last.lower():
        return
    msg = f"restic init failed: {last}"
    raise UserError(msg)


def _round_trip(repo: str, env: dict[str, str]) -> None:
    """Tiny backup + restore + diff to prove the wiring."""
    with tempfile.TemporaryDirectory(prefix="sanctum-r2-canary-") as src_str:
        src = Path(src_str)
        canary = src / "canary.txt"
        canary.write_text("sanctum-r2-setup-canary\n", encoding="utf-8")
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
        with tempfile.TemporaryDirectory(prefix="sanctum-r2-restore-") as dst_str:
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
            restored = Path(dst_str) / canary.relative_to(canary.anchor)
            if not restored.exists() or restored.read_text() != canary.read_text():
                msg = "canary round-trip diff mismatch"
                raise LocalError(msg)


def _persist(
    instance_path: Path,
    *,
    slot: str,
    repo: str,
    keychain_service_restic: str,
    keychain_account: str,
) -> None:
    raw = yaml.safe_load(instance_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        msg = "instance.yaml top-level is not a mapping"
        raise LocalError(msg)
    cli_block = raw.setdefault("cli", {}) or {}
    cb_block = cli_block.setdefault("cloud_backup", {}) or {}
    cb_block[slot] = {
        "kind": "restic",
        "repo": repo,
        "keychain": {"service": keychain_service_restic, "account": keychain_account},
    }
    cb_block.setdefault(
        "retention", {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12}
    )
    cli_block["cloud_backup"] = cb_block
    raw["cli"] = cli_block

    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = instance_path.with_suffix(instance_path.suffix + f".bak.{stamp}")
    backup_path.write_text(instance_path.read_text(encoding="utf-8"), encoding="utf-8")

    new_yaml = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)
    tmp = instance_path.with_suffix(instance_path.suffix + ".tmp")
    tmp.write_text(new_yaml, encoding="utf-8")
    tmp.replace(instance_path)


# ─── Wizard ─────────────────────────────────────────────────────────


def run_wizard(*, auto_open: bool = True, persist: bool = True) -> _SetupResult:
    cfg = config.load()
    target_slot = _preflight(cfg)

    console.print(
        Panel.fit(
            f"[bold]Sanctum cloud-setup wizard — Cloudflare R2[/]\n\n"
            f"R2 is the recommended default: 10 GB storage + 1 M Class A + 10 M Class B "
            f"free monthly, and [bold]no egress charges, ever[/]. Restore drills cost $0.\n\n"
            f"Three values to paste: Cloudflare account ID, R2 access key ID, R2 secret "
            f"access key. The wizard verifies them against R2, creates a private bucket, "
            f"initializes restic, runs a round-trip canary, and writes "
            f"[cyan]cli.cloud_backup.{target_slot}[/] in [cyan]~/.sanctum/instance.yaml[/].",
            border_style="cyan",
        )
    )

    # 1. Open the R2 API tokens page
    console.print(
        f"\n[bold]Step 1.[/] Open the R2 API Tokens page and click "
        f"[cyan]Create API Token[/] with [bold]Object Read & Write[/] permissions, "
        f"scoped to [bold]All buckets[/] (or your account-level bucket).\n"
        f"[dim]{R2_DASHBOARD_URL}[/]"
    )
    if auto_open:
        webbrowser.open(R2_DASHBOARD_URL)

    # 2. Paste credentials with regex validation
    account_id = _prompt_validated(
        "Cloudflare account ID (32 hex chars — see the URL of the R2 dashboard)",
        validator=lambda v: bool(R2_HEX32_RE.match(v.strip().lower())),
        hint="32 lowercase hex characters",
    )
    access_key = _prompt_validated(
        "R2 access key ID (32 hex chars)",
        validator=lambda v: bool(R2_HEX32_RE.match(v.strip().lower())),
        hint="32 lowercase hex characters",
    )
    secret_key = _prompt_validated(
        "R2 secret access key (64 hex chars)",
        validator=lambda v: bool(R2_HEX64_RE.match(v.strip().lower())),
        hint="64 lowercase hex characters",
        password=True,
    )
    creds = _R2Creds(
        account_id=account_id.lower(),
        access_key=access_key.lower(),
        secret_key=secret_key.lower(),
    )

    # 3. Auth probe
    console.print("\n[bold]Step 2.[/] Probing R2 with the supplied credentials …")
    buckets = _list_buckets(creds)
    console.print(
        f"  [green]✓[/] credentials work · {len(buckets)} existing bucket(s) on this account"
    )

    # 4. Bucket
    bucket = _gen_bucket_name()
    console.print(f"\n[bold]Step 3.[/] Creating private bucket [cyan]{bucket}[/] …")
    _create_bucket(creds, bucket)
    console.print("  [green]✓[/] bucket ready")

    # 5. Keychain
    console.print("\n[bold]Step 4.[/] Storing credentials + restic passphrase in Keychain …")
    replace = Confirm.ask("  overwrite existing R2 entries if present?", default=True)
    _ensure_keychain_entry(
        KEYCHAIN_SERVICE_R2_ACCESS_KEY, KEYCHAIN_ACCOUNT, creds.access_key, replace=replace
    )
    _ensure_keychain_entry(
        KEYCHAIN_SERVICE_R2_SECRET, KEYCHAIN_ACCOUNT, creds.secret_key, replace=replace
    )
    if not keychain.exists(KEYCHAIN_ACCOUNT_RESTIC, KEYCHAIN_SERVICE_RESTIC):
        passphrase = _gen_passphrase()
        _ensure_keychain_entry(
            KEYCHAIN_SERVICE_RESTIC, KEYCHAIN_ACCOUNT_RESTIC, passphrase, replace=False
        )
    else:
        passphrase = keychain.read(
            account=KEYCHAIN_ACCOUNT_RESTIC, service=KEYCHAIN_SERVICE_RESTIC
        )
        console.print("  [dim]reusing existing sanctum-backup-key from Keychain[/]")

    # 6. restic init
    repo = f"s3:{creds.endpoint}/{bucket}"
    console.print(f"\n[bold]Step 5.[/] Initializing restic repo …\n  [dim]{repo}[/]")
    env = _restic_env(creds, passphrase)
    _restic_init(repo, env)
    console.print("  [green]✓[/] repo initialized")

    # 7. Round-trip canary
    console.print("\n[bold]Step 6.[/] Round-trip test (backup + restore + diff) …")
    _round_trip(repo, env)
    console.print("  [green]✓[/] canary survived round-trip")

    # 8. Persist
    if persist:
        console.print(f"\n[bold]Step 7.[/] Updating ~/.sanctum/instance.yaml ({target_slot}) …")
        _persist(
            config.instance_path(),
            slot=target_slot,
            repo=repo,
            keychain_service_restic=KEYCHAIN_SERVICE_RESTIC,
            keychain_account=KEYCHAIN_ACCOUNT_RESTIC,
        )
        console.print(f"  [green]✓[/] cli.cloud_backup.{target_slot} set; .bak file written")
    else:
        console.print(
            "\n[bold]Step 7.[/] [yellow]Skipped persistence (--no-persist).[/] To wire this manually:"
        )
        console.print(
            f"  cli:\n    cloud_backup:\n      primary:\n        kind: restic\n"
            f"        repo: {repo}\n        keychain:\n"
            f"          service: {KEYCHAIN_SERVICE_RESTIC}\n          account: {KEYCHAIN_ACCOUNT_RESTIC}"
        )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan")
    summary.add_column()
    summary.add_row("slot", target_slot)
    summary.add_row("account", creds.account_id)
    summary.add_row("bucket", bucket)
    summary.add_row("repo", repo)
    summary.add_row("keychain (R2 access key)", f"{KEYCHAIN_SERVICE_R2_ACCESS_KEY} / {KEYCHAIN_ACCOUNT}")
    summary.add_row("keychain (R2 secret)", f"{KEYCHAIN_SERVICE_R2_SECRET} / {KEYCHAIN_ACCOUNT}")
    summary.add_row("keychain (restic)", f"{KEYCHAIN_SERVICE_RESTIC} / {KEYCHAIN_ACCOUNT_RESTIC}")
    console.print()
    console.print(Panel.fit(summary, title="[bold green]done[/]", border_style="green"))

    return _SetupResult(
        account_id=creds.account_id,
        bucket=bucket,
        repo=repo,
        keychain_account=KEYCHAIN_ACCOUNT_RESTIC,
    )
