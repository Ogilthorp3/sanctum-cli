"""``sanctum keys backup <path>`` — export sanctum Keychain entries to an
encrypted bundle.

Council-doctrine for the trust gate (uninstall + reinstall): operator can
walk away cleanly because they have a portable, encrypted bundle of their
credentials. Reinstall is a single ``sanctum keys restore <path>``.

The bundle (v2):
  - is assembled strictly in-memory as a tar.gz (zero plaintext files on disk)
  - is encrypted with AES-256-GCM + PBKDF2-HMAC-SHA256 (600,000 iterations)
    and a versioned AEAD-bound header (``SANCTUMKB\\x02``)
  - asks the operator for a passphrase interactively (twice, confirms)
  - is written atomically at mode ``0600``
  - includes a manifest with service names + timestamps (no values)
"""

from __future__ import annotations

import getpass
import io
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import typer
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from rich.console import Console
from rich.panel import Panel

from sanctum_cli import keychain
from sanctum_cli.errors import LocalError

console = Console()

MAGIC_V2 = b"SANCTUMKB\x02"
PBKDF2_ITERATIONS_V2 = 600_000
_SALT_LEN = 16
_NONCE_LEN = 12
_HEADER_LEN_V2 = len(MAGIC_V2) + 4 + _SALT_LEN + _NONCE_LEN  # 42 bytes

_MAX_MEMBERS = 256
_MAX_MEMBER_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_SAFE_KEYCHAIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# (service, account) — the account matters: most live under 'sanctum', but the
# restic passphrase lives under 'sanctum-backup'. Names verified against the real
# write sites (backends/b2.py, onboard.py, config.py) — the old list had drifted:
# 'gemini-api-key' was never written (it's google-ai-api-key) and
# 'b2-application-key-id' should be 'b2-account-id', so BOTH were silently absent
# from the bundle. The restic passphrase was missing entirely — without it a DR
# bundle can't decrypt the very backups it pairs with.
KEYCHAIN_SERVICES = [
    ("openrouter-api-key", "sanctum"),
    ("openrouter-mgmt-key", "sanctum"),
    ("openrouter-mgmt-key-backup", "sanctum"),
    ("anthropic-api-key", "sanctum"),
    ("google-ai-api-key", "sanctum"),          # was gemini-api-key (never written)
    ("r2-account-id", "sanctum"),
    ("r2-access-key-id", "sanctum"),
    ("r2-secret-access-key", "sanctum"),
    ("b2-account-id", "sanctum"),              # was b2-application-key-id
    ("b2-application-key", "sanctum"),
    ("sanctum-backup-key", "sanctum-backup"),  # restic passphrase — decrypts the backups
]


def _read_keychain(service: str, account: str = "sanctum") -> str | None:
    try:
        return keychain.read(account=account, service=service)
    except LocalError:
        return None


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def _encrypt_v2(tar_gz_bytes: bytes, passphrase: str) -> bytes:
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    header = MAGIC_V2 + struct.pack(">I", PBKDF2_ITERATIONS_V2) + salt + nonce
    key = _derive_key(passphrase, salt, PBKDF2_ITERATIONS_V2)
    ciphertext = AESGCM(key).encrypt(nonce, tar_gz_bytes, associated_data=header)
    return header + ciphertext


def _decrypt_v2(envelope: bytes, passphrase: str) -> bytes:
    if len(envelope) < _HEADER_LEN_V2 + 16:
        raise InvalidTag("truncated v2 bundle")
    header = envelope[:_HEADER_LEN_V2]
    offset = len(MAGIC_V2)
    (iterations,) = struct.unpack(">I", header[offset : offset + 4])
    if iterations < PBKDF2_ITERATIONS_V2:
        raise InvalidTag("weak PBKDF2 iteration count in v2 header")
    salt = header[offset + 4 : offset + 4 + _SALT_LEN]
    nonce = header[offset + 4 + _SALT_LEN : _HEADER_LEN_V2]
    ciphertext = envelope[_HEADER_LEN_V2:]
    key = _derive_key(passphrase, salt, iterations)
    return AESGCM(key).decrypt(nonce, ciphertext, associated_data=header)


def _decrypt_legacy_v1(path: Path, passphrase: str) -> bytes:
    """Decrypt a legacy ``Salted__`` OpenSSL AES-256-CBC bundle in memory via fd pipe."""
    if not shutil.which("openssl"):
        console.print("[red]openssl binary missing — cannot decrypt legacy bundle.[/]")
        raise typer.Exit(code=1)
    r_fd, w_fd = os.pipe()
    try:
        os.write(w_fd, (passphrase + "\n").encode("utf-8"))
        os.close(w_fd)
        w_fd = -1
        proc = subprocess.run(
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-in",
                str(path),
                "-pass",
                f"fd:{r_fd}",
            ],
            pass_fds=(r_fd,),
            capture_output=True,
            check=False,
        )
    finally:
        if w_fd != -1:
            os.close(w_fd)
        os.close(r_fd)
    if proc.returncode != 0:
        console.print("[red]Decryption failed[/] — wrong passphrase or corrupt bundle.")
        raise typer.Exit(code=1)
    return proc.stdout


def _extract_in_memory_bundle(tar_gz_bytes: bytes) -> dict[str, bytes]:
    """Validate and extract ``sanctum-keys/*`` entries strictly in memory."""
    files: dict[str, bytes] = {}
    seen_names: set[str] = set()
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_gz_bytes), mode="r:gz") as tf:
            for count, member in enumerate(tf, start=1):
                if count > _MAX_MEMBERS:
                    console.print("[red]Bundle rejected:[/] too many tar members.")
                    raise typer.Exit(code=1)
                if (
                    member.issym()
                    or member.islnk()
                    or member.ischr()
                    or member.isblk()
                    or member.isfifo()
                    or member.isdev()
                    or not (member.isdir() or member.isreg())
                ):
                    console.print(
                        f"[red]Bundle rejected:[/] unsafe tar member type ({member.name!r})."
                    )
                    raise typer.Exit(code=1)

                name = member.name
                if not name or "\x00" in name or "\\" in name:
                    console.print("[red]Bundle rejected:[/] invalid tar member name.")
                    raise typer.Exit(code=1)
                clean = name.rstrip("/")
                raw_parts = clean.split("/")
                posix = PurePosixPath(clean)
                canonical = posix.as_posix()
                if (
                    not clean
                    or clean.startswith("/")
                    or posix.is_absolute()
                    or any(part in ("..", ".", "") for part in raw_parts)
                    or clean != canonical
                ):
                    console.print(
                        f"[red]Bundle rejected:[/] unsafe tar member path ({name!r})."
                    )
                    raise typer.Exit(code=1)
                if canonical in seen_names:
                    console.print(
                        f"[red]Bundle rejected:[/] duplicate tar member ({canonical!r})."
                    )
                    raise typer.Exit(code=1)
                seen_names.add(canonical)

                if member.isdir():
                    if posix.parts != ("sanctum-keys",):
                        console.print(
                            f"[red]Bundle rejected:[/] unexpected directory ({name!r})."
                        )
                        raise typer.Exit(code=1)
                    continue

                if len(posix.parts) != 2 or posix.parts[0] != "sanctum-keys":
                    console.print(
                        f"[red]Bundle rejected:[/] unexpected tar path ({name!r})."
                    )
                    raise typer.Exit(code=1)

                if member.size < 0 or member.size > _MAX_MEMBER_BYTES:
                    console.print(
                        f"[red]Bundle rejected:[/] member {name!r} exceeds size limit."
                    )
                    raise typer.Exit(code=1)

                extracted = tf.extractfile(member)
                if extracted is None:
                    console.print(
                        f"[red]Bundle rejected:[/] unreadable tar member ({name!r})."
                    )
                    raise typer.Exit(code=1)
                with extracted:
                    data = extracted.read(_MAX_MEMBER_BYTES + 1)
                if len(data) > _MAX_MEMBER_BYTES:
                    console.print(
                        f"[red]Bundle rejected:[/] member {name!r} exceeds size limit."
                    )
                    raise typer.Exit(code=1)
                total_bytes += len(data)
                if total_bytes > _MAX_TOTAL_BYTES:
                    console.print("[red]Bundle rejected:[/] total extracted size exceeds limit.")
                    raise typer.Exit(code=1)

                files[posix.parts[1]] = data
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        console.print(f"[red]Corrupt or invalid bundle archive:[/] {exc}")
        raise typer.Exit(code=1) from exc
    return files


def keys_backup_command(
    out: Annotated[
        Path,
        typer.Argument(
            help="Where to write the encrypted bundle (e.g. ~/Documents/sanctum-keys-2026-05-22.tar.gz.enc).",
        ),
    ],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Export sanctum Keychain entries to an AES-256-GCM-encrypted bundle."""
    out = out.expanduser().absolute()
    if out.exists() or out.is_symlink():
        console.print(f"[red]refusing to overwrite[/] {out}")
        raise typer.Exit(code=1)

    console.print()
    console.print(Panel.fit(
        f"[bold]sanctum keys backup → {out}[/]\n\n"
        f"Will export up to {len(KEYCHAIN_SERVICES)} sanctum Keychain entries "
        f"into an AES-256-GCM-encrypted tar bundle. The bundle is portable: copy "
        f"it to another Mac, run [cyan]sanctum keys restore <path>[/], enter "
        f"the same passphrase.",
        border_style="cyan",
    ))
    console.print()

    if not yes and not typer.confirm("Continue?", default=True):
        raise typer.Exit(code=0)

    # Prompt for the passphrase twice; PBKDF2-SHA256 (600k) derives the AEAD key.
    p1 = getpass.getpass("Passphrase for the bundle: ")
    p2 = getpass.getpass("Confirm passphrase: ")
    if p1 != p2:
        console.print("[red]passphrases differ — aborting[/]")
        raise typer.Exit(code=1)
    if len(p1) < 12:
        console.print("[red]passphrase must be at least 12 characters[/]")
        raise typer.Exit(code=1)

    manifest: dict[str, Any] = {
        "created": datetime.now(UTC).isoformat(),
        "host": os.uname().nodename,
        "services": [],
    }
    bio = io.BytesIO()
    captured = 0
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        root_info = tarfile.TarInfo("sanctum-keys")
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o700
        root_info.uid = 0
        root_info.gid = 0
        tf.addfile(root_info)

        for service, account in KEYCHAIN_SERVICES:
            value = _read_keychain(service, account)
            if value is None:
                continue
            val_bytes = value.encode("utf-8")
            info = tarfile.TarInfo(f"sanctum-keys/{service}")
            info.size = len(val_bytes)
            info.mode = 0o600
            info.uid = 0
            info.gid = 0
            tf.addfile(info, io.BytesIO(val_bytes))
            manifest["services"].append({
                "service": service,
                "account": account,  # restore must write it back to the right account
                "length": len(value),
                "prefix": value[:4] + "…" if len(value) > 8 else "***",
            })
            captured += 1

        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        m_info = tarfile.TarInfo("sanctum-keys/MANIFEST.json")
        m_info.size = len(manifest_bytes)
        m_info.mode = 0o600
        m_info.uid = 0
        m_info.gid = 0
        tf.addfile(m_info, io.BytesIO(manifest_bytes))

    envelope = _encrypt_v2(bio.getvalue(), p1)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except Exception:
            os.close(fd)
            raise
        with os.fdopen(fd, "wb") as fh:
            fh.write(envelope)
        try:
            os.link(tmp, out)
        except FileExistsError as exc:
            console.print(f"[red]refusing to overwrite[/] {out}")
            raise typer.Exit(code=1) from exc
    finally:
        tmp.unlink(missing_ok=True)

    console.print()
    console.print(Panel(
        f"[bold green]Wrote {captured} key(s) to {out}[/]\n\n"
        f"File is encrypted (AES-256-GCM, PBKDF2-SHA256 600k). Mode 600. "
        f"Keep the passphrase somewhere safe (your password manager).\n\n"
        f"To restore on another machine:\n"
        f"  [cyan]sanctum keys restore {out.name}[/]",
        border_style="green",
        padding=(1, 2),
    ))


def _write_keychain(service: str, account: str, value: str) -> bool:
    """Write Keychain entry via ``keychain.write`` (stdin, no argv secret)."""
    try:
        keychain.write(account=account, service=service, value=value, update=True)
    except LocalError:
        return False
    return True


def keys_restore_command(
    path: Path,
    legacy: Annotated[
        bool,
        typer.Option(
            "--legacy",
            help="Allow decrypting legacy unauthenticated v1 (OpenSSL CBC) bundles.",
        ),
    ] = False,
) -> None:
    """Decrypt a `keys backup` bundle and write every entry back into this Mac's
    Keychain, each under the (service, account) recorded in the manifest."""
    if not path.is_file():
        console.print(f"[red]No such bundle:[/] {path}")
        raise typer.Exit(code=1)
    raw = path.read_bytes()
    is_v2 = raw.startswith(MAGIC_V2)
    is_v1 = raw.startswith(b"Salted__")
    if not is_v2 and not is_v1:
        console.print("[red]Unrecognized or corrupt bundle format.[/]")
        raise typer.Exit(code=1)
    if is_v1 and not legacy:
        console.print(
            "[red]Legacy v1 bundle uses unauthenticated AES-256-CBC (10k PBKDF2).[/] "
            "Re-run with [bold]--legacy[/] to restore, then re-export with "
            "[cyan]sanctum keys backup[/]."
        )
        raise typer.Exit(code=1)

    p1 = getpass.getpass("Passphrase for the bundle: ")
    if is_v2:
        try:
            tar_gz_bytes = _decrypt_v2(raw, p1)
        except InvalidTag as exc:
            console.print(
                "[red]Decryption failed[/] — wrong passphrase or tampered/corrupt bundle."
            )
            raise typer.Exit(code=1) from exc
    else:
        console.print(
            "[bold yellow]WARNING:[/] Restoring legacy unauthenticated v1 (OpenSSL CBC) bundle. "
            "Re-export with [cyan]sanctum keys backup[/] after restoring."
        )
        tar_gz_bytes = _decrypt_legacy_v1(path, p1)

    files = _extract_in_memory_bundle(tar_gz_bytes)
    manifest_raw = files.get("MANIFEST.json")
    if manifest_raw is None:
        console.print("[red]Bundle has no MANIFEST.json[/] — cannot restore.")
        raise typer.Exit(code=1)
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        console.print("[red]Bundle MANIFEST.json is corrupt[/] — cannot restore.")
        raise typer.Exit(code=1) from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("services"), list):
        console.print("[red]Bundle MANIFEST.json has invalid structure[/] — cannot restore.")
        raise typer.Exit(code=1)

    restored, failed = 0, []
    for entry in manifest["services"]:
        if not isinstance(entry, dict):
            console.print("[red]Bundle MANIFEST.json has invalid entry[/] — cannot restore.")
            raise typer.Exit(code=1)
        service = entry.get("service")
        account = entry.get("account", "sanctum")
        if (
            not isinstance(service, str)
            or not _SAFE_KEYCHAIN_NAME_RE.fullmatch(service)
            or not isinstance(account, str)
            or not _SAFE_KEYCHAIN_NAME_RE.fullmatch(account)
        ):
            console.print(
                f"[red]Bundle rejected:[/] invalid service/account name ({service!r} / {account!r})."
            )
            raise typer.Exit(code=1)
        raw_val = files.get(service)
        if raw_val is None:
            failed.append(service)
            continue
        try:
            value = raw_val.decode("utf-8")
        except UnicodeDecodeError:
            failed.append(service)
            continue
        if _write_keychain(service, account, value):
            restored += 1
        else:
            failed.append(service)

    console.print()
    body = f"[bold green]Restored {restored} key(s) into the Keychain.[/]"
    if failed:
        body += f"\n[yellow]{len(failed)} failed:[/] {', '.join(failed)}"
    console.print(Panel(body, border_style="green" if not failed else "yellow", padding=(1, 2)))
