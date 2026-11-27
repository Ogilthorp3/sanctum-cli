"""``sanctum haus`` — full Rust Sanctum Engine installer, PKI, configs & LaunchAgents.

Installs and manages ALL production Rust services from ``sanctum-rs`` (signed
Apple Silicon arm64 binaries + ``mlx.metallib``) into ``~/.sanctum/bin/``,
mints a unique per-machine mTLS PKI (``~/.sanctum/certs/``), downloads the local
Dragon model + Jedi-Trials-verified R1 champion LoRA, generates portable
``proxyd`` / ``instance.yaml`` configs, and installs macOS user LaunchAgents so
every Sanctum feature (``sanctum chat``, ``sanctum council``, ``sanctum brainstorm``,
Yoda on Signal via Rust ``proxyd :4040`` → ``sanctum-presidio`` → ``sanctum-mlx :1337``,
EventBus, RAM triage, Watchdog, Memory Vault) runs natively on any Apple Silicon Mac.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import os
import platform
import plistlib
import secrets
import shutil
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sanctum_cli import hive_pulse
from sanctum_cli.commands import dragon as dragon_cmd
from sanctum_cli.mesh.metrics import detect_ram_gb

console = Console()
app = typer.Typer(
    name="haus",
    help="Install, run, inspect, and update the full Sanctum Rust engine suite (sanctum-rs).",
    no_args_is_help=True,
)

ENV_RUST_STATE = "SANCTUM_RUST_STATE"
DEFAULT_RUST_TAG = "rust-v1.0.3"
DEFAULT_RUST_URL = (
    "https://github.com/Ogilthorp3/homebrew-sanctum/releases/download/"
    "rust-v1.0.3/sanctum-rust-suite-arm64.tar.gz"
)
DEFAULT_RUST_SHA256 = "8c1bd11514762f401444d78d9c4723023607ca7f7ac1f132eb0b0c258bc2639e"

CATHEDRAL_PREWARM_TXT = (
    "<|im_start|>system\n"
    "You are a jedi in the Sanctum council. You think carefully, respond truthfully, "
    "and prefer concise honesty over comfortable hedging.\n"
    "<|im_end|>\n"
    "<|im_start|>user\n"
)

# Core always-on LaunchAgent services provisioned for a full Sanctum Hub
CORE_LAUNCHAGENTS: tuple[tuple[str, str], ...] = (
    ("haus.sanctum.presidio", "sanctum-presidio"),
    ("haus.sanctum.memory-vault", "sanctum-memory"),
    ("haus.sanctum.mlx", "sanctum-mlx"),
    ("haus.sanctum.mlx-coder", "sanctum-mlx"),
    ("haus.sanctum.proxyd", "proxyd"),
    ("haus.sanctum.force-flow", "sanctum-force-flow"),
    ("haus.sanctum.msg-bus", "sanctum-msg-bus"),
    ("haus.sanctum.chitti", "sanctum-chitti"),
    ("haus.sanctum.thalamus", "sanctum-thalamus"),
    ("haus.sanctum.triage", "sanctum-triage"),
    ("haus.sanctum.pressure-valve", "sanctum-pressure-valve"),
    ("haus.sanctum.watchdog", "sanctumd"),
    ("haus.sanctum.castellan", "castellan"),
    ("haus.sanctum.chi", "sanctum-chi"),
    ("haus.sanctum.endocrine-gland", "sanctum"),
    ("haus.sanctum.r2d2", "sanctum"),
)


def sanctum_root() -> Path:
    return Path.home() / ".sanctum"


def rust_state_path() -> Path:
    override = os.environ.get(ENV_RUST_STATE, "").strip()
    if override:
        return Path(override).expanduser()
    return sanctum_root() / "state" / "rust-suite.json"


def load_rust_state() -> dict[str, Any]:
    path = rust_state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_rust_state(data: dict[str, Any]) -> None:
    path = rust_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def is_rust_suite_installed() -> bool:
    st = load_rust_state()
    return bool(st.get("release_tag")) and (sanctum_root() / "bin" / "sanctum-mlx").is_file()


def is_operator_haus() -> bool:
    """Protect a pre-existing production operator hub from accidental overwrite."""
    root = sanctum_root()
    return (
        (root / "secrets" / "ha-token").is_file()
        and (root / "launchagents" / "live" / "haus.sanctum.signal-cli.plist").is_file()
    )


def fetch_latest_rust_release(
    *, transport: httpx.BaseTransport | None = None
) -> dict[str, str]:
    """Resolve latest sanctum-rust-suite release from ``hive.sanctum.run/latest.json``."""
    fallback = {
        "rust_tag": DEFAULT_RUST_TAG,
        "rust_url": DEFAULT_RUST_URL,
        "rust_sha256": DEFAULT_RUST_SHA256,
    }
    endpoint = hive_pulse._endpoint()
    try:
        with httpx.Client(timeout=5.0, transport=transport) as client:
            r = client.get(f"{endpoint}/latest.json")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("rust_tag") and data.get("rust_url"):
                    return {
                        "rust_tag": str(data["rust_tag"]).strip(),
                        "rust_url": str(data["rust_url"]).strip(),
                        "rust_sha256": str(data.get("rust_sha256") or "").strip(),
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


def _safe_extract_tar_gz(tar_path: Path, dest: Path) -> list[str]:
    """Safely extract flat/nested Rust binaries + metallib into ``dest`` (no traversal)."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    installed: list[str] = []
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            base = Path(member.name).name
            if not base or base.startswith(".") or "/" in base or "\\" in base:
                continue
            target = (dest_resolved / base).resolve()
            if target.parent != dest_resolved:
                msg = f"unsafe tar entry rejected: {member.name}"
                raise ValueError(msg)
            src_fh = tf.extractfile(member)
            if src_fh is None:
                continue
            tmp_target = dest_resolved / f".{base}.tmp"
            with src_fh, tmp_target.open("wb") as out_fh:
                shutil.copyfileobj(src_fh, out_fh)
            if base.endswith((".metallib", ".safetensors", ".txt", ".json")):
                tmp_target.chmod(0o644)
            else:
                tmp_target.chmod(0o755)
            tmp_target.replace(target)
            installed.append(base)
    return sorted(set(installed))


def download_and_install_rust_suite(
    url: str,
    expected_sha256: str,
    bin_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, list[str]]:
    """Download, SHA-256 verify, and atomically install the Rust suite into ``bin_dir``."""
    with tempfile.TemporaryDirectory(prefix="sanctum-rust-") as td:
        tar_path = Path(td) / "sanctum-rust-suite-arm64.tar.gz"
        with (
            httpx.Client(timeout=600.0, follow_redirects=True, transport=transport) as client,
            client.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            with tar_path.open("wb") as out_fh:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    out_fh.write(chunk)

        actual_sha = _sha256_file(tar_path)
        if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
            msg = (
                f"SHA-256 mismatch for Rust suite: "
                f"expected {expected_sha256[:16]}…, got {actual_sha[:16]}…"
            )
            raise ValueError(msg)

        binaries = _safe_extract_tar_gz(tar_path, bin_dir)
        return actual_sha, binaries


def _write_secret_file(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mint_local_pki(certs_dir: Path, *, force: bool = False) -> dict[str, Path]:
    """Mint a unique per-machine mTLS CA + loopback server/client certs (idempotent)."""
    paths = {
        "ca_cert": certs_dir / "ca.crt",
        "ca_key": certs_dir / "ca.key",
        "server_cert": certs_dir / "server.crt",
        "server_key": certs_dir / "server.key",
        "client_cert": certs_dir / "client.crt",
        "client_key": certs_dir / "client.key",
    }
    if not force and all(
        paths[k].is_file() for k in ("ca_cert", "server_cert", "server_key", "client_cert", "client_key")
    ):
        return paths

    certs_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.UTC)
    valid_from = now - dt.timedelta(minutes=5)
    valid_to = now + dt.timedelta(days=3650)

    # 1. Local Root CA
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sanctum Local CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from)
        .not_valid_after(valid_to)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # 2. Loopback Server Cert (sanctum-mlx :1337 + proxyd :4040)
    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from)
        .not_valid_after(valid_to)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # 3. Client Cert (proxyd → sanctum-mlx mTLS, PKCS#8 key)
    cli_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cli_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sanctum-client")]))
        .issuer_name(ca_subject)
        .public_key(cli_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from)
        .not_valid_after(valid_to)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def _pem_cert(c: x509.Certificate) -> bytes:
        return c.public_bytes(serialization.Encoding.PEM)

    def _pem_key(k: rsa.RSAPrivateKey) -> bytes:
        return k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    _write_secret_file(paths["ca_cert"], _pem_cert(ca_cert), mode=0o644)
    _write_secret_file(paths["ca_key"], _pem_key(ca_key), mode=0o600)
    _write_secret_file(paths["server_cert"], _pem_cert(srv_cert), mode=0o644)
    _write_secret_file(paths["server_key"], _pem_key(srv_key), mode=0o600)
    _write_secret_file(paths["client_cert"], _pem_cert(cli_cert), mode=0o644)
    _write_secret_file(paths["client_key"], _pem_key(cli_key), mode=0o600)
    return paths


def ensure_local_tokens(secrets_dir: Path, *, sync_keychain: bool = False) -> None:
    """Create unique per-machine IPC & notify tokens if missing (0600)."""
    for name in ("control-plane-ipc.token", "force-flow-notify.token", "proxyd-api-key"):
        p = secrets_dir / name
        if not p.is_file():
            _write_secret_file(p, (secrets.token_hex(32) + "\n").encode("utf-8"), mode=0o600)
    if sync_keychain and platform.system() == "Darwin" and shutil.which("security"):
        key_path = secrets_dir / "proxyd-api-key"
        probe = subprocess.run(
            ["security", "find-generic-password", "-s", "sanctum-proxy-client", "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0 and key_path.is_file():
            token = key_path.read_text(encoding="utf-8").strip()
            if token:
                subprocess.run(
                    [
                        "security",
                        "add-generic-password",
                        "-U",
                        "-a",
                        "sanctum",
                        "-s",
                        "sanctum-proxy-client",
                        "-w",
                        token,
                    ],
                    capture_output=True,
                    check=False,
                )


def ensure_hub_support_files(root: Path) -> tuple[Path, Path | None]:
    """Initialize Memory Vault, cathedral prewarm, ablation vectors, .node_id, and boot-ready."""
    # 1. Memory Vault directory tree
    mem_dir = root / "memory"
    for sub in ("inbox/agy", "inbox/claude-code", "facts", "journal"):
        (mem_dir / sub).mkdir(parents=True, exist_ok=True)
    readme = mem_dir / "README.md"
    if not readme.is_file():
        readme.write_text(
            "# Sanctum Memory Vault\n\nDurable local-first memory vault served by `sanctum-memory` (:18097) and `memory-vault-mcp`.\n",
            encoding="utf-8",
        )

    # 2. Cathedral KV prewarm prompt
    prewarm_path = root / "prewarm" / "cathedral.txt"
    prewarm_path.parent.mkdir(parents=True, exist_ok=True)
    if not prewarm_path.is_file():
        prewarm_path.write_text(CATHEDRAL_PREWARM_TXT, encoding="utf-8")

    # 3. Stage ablation vectors for :6669 heretic/ablation seat if shipped in bin/
    ablation_src = root / "bin" / "refusal_dirs_38_v2.safetensors"
    ablation_dst = root / "ablation" / "refusal_dirs_38_v2.safetensors"
    ablation_path: Path | None = ablation_dst if ablation_dst.is_file() else None
    if ablation_src.is_file():
        ablation_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ablation_src, ablation_dst)
        ablation_dst.chmod(0o644)
        ablation_path = ablation_dst

    # 4. Node identity (.node_id) & boot-ready state
    node_id_path = root / ".node_id"
    if not node_id_path.is_file():
        slug = (
            "".join(c if c.isalnum() else "-" for c in platform.node().split(".")[0].lower()).strip("-")
            or "sanctum-hub"
        )
        node_id_path.write_text(slug + "\n", encoding="utf-8")
    boot_ready = root / "state" / "boot-ready.json"
    boot_ready.parent.mkdir(parents=True, exist_ok=True)
    if not boot_ready.is_file():
        boot_ready.write_text(
            json.dumps({"ready": True, "ts": dt.datetime.now(dt.UTC).isoformat()}) + "\n",
            encoding="utf-8",
        )
    return prewarm_path, ablation_path


def download_hf_snapshot(
    repo_id: str,
    dest_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Download public HuggingFace MLX model files cleanly using httpx (resumable, zero extra deps)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    api_url = f"https://huggingface.co/api/models/{repo_id}"
    with httpx.Client(timeout=30.0, follow_redirects=True, transport=transport) as client:
        r = client.get(api_url)
        r.raise_for_status()
        meta = r.json()

    siblings = meta.get("siblings") or []
    files = [
        str(s.get("rfilename") or "")
        for s in siblings
        if isinstance(s, dict)
        and str(s.get("rfilename") or "")
        and not str(s.get("rfilename") or "").startswith(".")
    ]
    if not files:
        msg = f"No files listed for HuggingFace repo {repo_id}"
        raise RuntimeError(msg)

    for idx, fname in enumerate(files, 1):
        out_path = (dest_dir / fname).resolve()
        if not str(out_path).startswith(str(dest_dir.resolve()) + os.sep):
            msg = f"Unsafe HF filename rejected: {fname}"
            raise ValueError(msg)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        dl_url = f"https://huggingface.co/{repo_id}/resolve/main/{fname}"

        with (
            httpx.Client(timeout=600.0, follow_redirects=True, transport=transport) as client,
            client.stream("GET", dl_url) as resp,
        ):
            resp.raise_for_status()
            expected_len = int(resp.headers.get("content-length") or "0")
            if out_path.is_file() and expected_len > 0 and out_path.stat().st_size == expected_len:
                continue
            console.print(f"  [dim][{idx}/{len(files)}][/] {fname}")
            tmp_path = out_path.with_name(out_path.name + ".part")
            with tmp_path.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=4 * 1024 * 1024):
                    fh.write(chunk)
            tmp_path.replace(out_path)
    return dest_dir


def write_portable_proxyd_config(
    *,
    config_path: Path,
    certs_dir: Path,
    api_model_name: str,
    coder_model_name: str | None = None,
    force: bool = False,
) -> Path:
    """Generate portable ~/.sanctum/sanctum-proxy/config.yaml wired to local Rust sanctum-mlx."""
    if config_path.is_file() and not force:
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    models: list[dict[str, Any]] = [
        {
            "name": "council-mlx",
            "provider": "local",
            "api_model": api_model_name,
            "api_base": "https://127.0.0.1:1337",
            "mtls": True,
            "max_tokens": 32768,
            "ttfb_timeout_secs": 300,
            "read_timeout_secs": 600,
        }
    ]
    aliases: dict[str, str] = {
        "sanctum-dragon": "council-mlx",
        "council-brain": "council-mlx",
        "council-max-thinking": "council-mlx",
        "council-spacial": "council-mlx",
        "council-finance": "council-mlx",
        "council-heretic": "council-mlx",
        "council-local-think": "council-mlx",
        "gemini-31-pro": "council-mlx",
    }
    fallbacks: dict[str, list[str]] = {}
    if coder_model_name:
        models.append(
            {
                "name": "council-code",
                "provider": "local",
                "api_model": coder_model_name,
                "api_base": "https://127.0.0.1:3301",
                "mtls": True,
                "max_tokens": 32768,
                "ttfb_timeout_secs": 180,
                "read_timeout_secs": 300,
            }
        )
        fallbacks["council-code"] = ["council-mlx"]
    else:
        aliases["council-code"] = "council-mlx"

    cfg = {
        "host": "127.0.0.1",
        "port": 4040,
        "presidio_analyzer": "http://127.0.0.1:5002",
        "presidio_anonymizer": "http://127.0.0.1:5001",
        "presidio_scrub": True,
        "memory": {"enabled": False},
        "mtls_cert": str(certs_dir / "client.crt"),
        "mtls_key": str(certs_dir / "client.key"),
        "mtls_ca": str(certs_dir / "ca.crt"),
        "tls_cert": str(certs_dir / "server.crt"),
        "tls_key": str(certs_dir / "server.key"),
        "model_aliases": aliases,
        "fallbacks": fallbacks,
        "models": models,
    }
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return config_path


def write_portable_castellan_capacity(
    capacity_path: Path,
    *,
    ram_gb: int,
    force: bool = False,
) -> Path:
    """Write a portable ~/.sanctum/castellan/capacity.yaml (0644, non-group-writable)."""
    if capacity_path.is_file() and not force:
        return capacity_path
    capacity_path.parent.mkdir(parents=True, exist_ok=True)
    hostname = socket.gethostname() or "localhost"
    slug = "".join(c if c.isalnum() else "-" for c in hostname.split(".")[0].lower()).strip("-") or "hub"
    phys_mb = max(16384, int(ram_gb or 32) * 1024)
    reserved_mb = 8192 if phys_mb >= 32768 else 4096
    safety_mb = 4096 if phys_mb >= 32768 else 2048
    mlx_budget_mb = 20480 if phys_mb >= 32768 else 7168
    doc = {
        "version": 1,
        "hosts": {
            slug: {
                "hostname": hostname,
                "ram_physical_mb": phys_mb,
                "ram_reserved_mb": reserved_mb,
                "ram_safety_mb": safety_mb,
            }
        },
        "services": {
            "sanctum-mlx": {
                "description": "Local Sanctum MLX Rust cathedral (:1337 mTLS)",
                "ram_budget_mb": mlx_budget_mb,
                "exclusive_with": [],
                "priority": 100,
                "hosts": [slug],
                "process_pattern": "sanctum-mlx",
                "launchagent": "haus.sanctum.mlx",
            },
            "force-flow": {
                "ram_budget_mb": 128,
                "exclusive_with": [],
                "priority": 80,
                "hosts": [slug],
                "launchagent": "haus.sanctum.force-flow",
            },
            "sanctum-watchdog": {
                "ram_budget_mb": 128,
                "exclusive_with": [],
                "priority": 95,
                "hosts": [slug],
                "launchagent": "haus.sanctum.watchdog",
                "process_pattern": "sanctumd",
            },
            "sanctum-proxyd": {
                "description": "Rust LLM router & Presidio scrubber (:4040 TLS)",
                "ram_budget_mb": 256,
                "exclusive_with": [],
                "priority": 90,
                "hosts": [slug],
                "launchagent": "haus.sanctum.proxyd",
                "process_pattern": "proxyd",
            },
        },
        "critical_processes": ["sanctumd", "sanctum-mlx", "proxyd", "WindowServer", "launchd"],
    }
    _write_secret_file(
        capacity_path,
        yaml.safe_dump(doc, sort_keys=False).encode("utf-8"),
        mode=0o644,
    )
    return capacity_path


def write_portable_msg_bus_config(config_path: Path, *, force: bool = False) -> Path:
    """Write a portable ~/.sanctum/msg-bus/config.yaml so sanctum-msg-bus boots cleanly."""
    if config_path.is_file() and not force:
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "quiet_hours": {
            "start": "22:00",
            "end": "08:00",
            "tz": "America/Toronto",
        },
        "severities": {},
        "transports": {},
    }
    config_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return config_path


def ensure_hub_instance_yaml(instance_path: Path) -> None:
    """Ensure ~/.sanctum/instance.yaml exists with role: hub (unlocking full haus CLI commands)."""
    if instance_path.is_file():
        return
    instance_path.parent.mkdir(parents=True, exist_ok=True)
    host_slug = "".join(c if c.isalnum() else "-" for c in platform.node().split(".")[0].lower()).strip("-") or "sanctum-hub"
    doc = {
        "instance": {
            "name": f"{host_slug} Sanctum",
            "slug": host_slug,
            "role": "hub",
        },
        "cli": {
            "default_provider": "mlx_local",
        },
    }
    instance_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def build_launchagent_specs(
    *,
    home: Path,
    model_dir: Path,
    adapter_dir: Path | None,
    ram_gb: int,
    coder_model_dir: Path | None = None,
    ablation_path: Path | None = None,
    prewarm_path: Path | None = None,
    sanctum_bin: str | None = None,
) -> list[dict[str, Any]]:
    """Build portable macOS LaunchAgent plists for the full Rust Sanctum Hub stack."""
    bin_dir = home / ".sanctum" / "bin"
    certs_dir = home / ".sanctum" / "certs"
    log_dir = home / ".sanctum" / "logs"
    secrets_dir = home / ".sanctum" / "secrets"
    base_env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }

    # RAM-aware Metal budgets (matching validated 64 GB hub and 16 GB satellite profiles)
    if ram_gb >= 60:
        cache_mb, mem_mb, wired_mb, slots = "1024", "26000", "18432", "4"
    elif ram_gb >= 30:
        cache_mb, mem_mb, wired_mb, slots = "512", "22000", "16384", "2"
    else:
        cache_mb, mem_mb, wired_mb, slots = "512", "10000", "8000", "2"

    mlx_args = [
        str(bin_dir / "sanctum-mlx"),
        "--model",
        str(model_dir),
    ]
    if adapter_dir is not None and (adapter_dir / "adapters.safetensors").is_file():
        mlx_args.extend(["--adapter-path", str(adapter_dir)])
    mlx_args.extend(
        [
            "--no-plain",
            "--tls-host",
            "127.0.0.1",
            "--tls-port",
            "1337",
            "--tls-cert",
            str(certs_dir / "server.crt"),
            "--tls-key",
            str(certs_dir / "server.key"),
            "--tls-client-ca",
            str(certs_dir / "ca.crt"),
            "--metal-cache-limit-mb",
            cache_mb,
            "--metal-memory-limit-mb",
            mem_mb,
            "--metal-wired-limit-mb",
            wired_mb,
            "--temp",
            "0.4",
            "--max-prompt-tokens",
            "32768",
            "--prompt-cache-slots",
            slots,
        ]
    )

    mlx_env = {
        **base_env,
        "RUST_LOG": "sanctum_mlx=info",
        "SANCTUM_MLX_FUSED_MLP": "1",
        "SANCTUM_MLX_FUSED_QKV": "1",
        "SANCTUM_MLX_KV_QUANT": "8",
        "SANCTUM_MLX_SSM_LADDER": "4",
        "SANCTUM_MLX_ANE_PREFILL": "1",
        "SANCTUM_MLX_ANE_FRACTION": "0.40",
    }
    if prewarm_path is not None:
        mlx_env["SANCTUM_MLX_PREWARM_FILE"] = str(prewarm_path)
    if ablation_path is not None and ram_gb >= 30:
        mlx_env["SANCTUM_MLX_ABLATION_DIRS"] = str(ablation_path)

    def _plist(
        label: str,
        args: list[str],
        env: dict[str, str] | None = None,
        log_name: str = "",
        workdir: Path | None = None,
        start_interval: int | None = None,
    ) -> dict[str, Any]:
        lf = str(log_dir / (log_name or f"{label}.log"))
        doc: dict[str, Any] = {
            "Label": label,
            "ProgramArguments": args,
            "EnvironmentVariables": env or base_env,
            "RunAtLoad": True,
            "ProcessType": "Standard",
            "StandardOutPath": lf,
            "StandardErrorPath": lf,
        }
        if start_interval is not None:
            doc["StartInterval"] = start_interval
        else:
            doc["KeepAlive"] = {"SuccessfulExit": False}
            doc["ThrottleInterval"] = 15
        if workdir is not None:
            doc["WorkingDirectory"] = str(workdir)
        return doc

    proxyd_dir = home / ".sanctum" / "sanctum-proxy"
    proxyd_env = {
        **base_env,
        "SANCTUM_PROXY_CONFIG": str(proxyd_dir / "config.yaml"),
    }
    thalamus_env = {
        **base_env,
        "RUST_LOG": "info",
        "THALAMUS_PORT": "1988",
    }
    memory_env = {
        **base_env,
        "SANCTUM_MEMORY_PORT": "18097",
        "SANCTUM_MEMORY_VAULT_DIR": str(home / ".sanctum" / "memory"),
        "SANCTUM_MEMORY_TOKEN_FILE": str(secrets_dir / "control-plane-ipc.token"),
    }
    castellan_dir = home / ".sanctum" / "castellan"
    castellan_cap = castellan_dir / "capacity.yaml"
    castellan_env = {
        **base_env,
        "CASTELLAN_STATE_DIR": str(castellan_dir),
    }

    specs: list[dict[str, Any]] = [
        _plist(
            "haus.sanctum.presidio",
            [str(bin_dir / "sanctum-presidio"), "--anonymizer-port", "5001", "--analyzer-port", "5002"],
            log_name="presidio.log",
        ),
        _plist(
            "haus.sanctum.memory-vault",
            [str(bin_dir / "sanctum-memory")],
            env=memory_env,
            log_name="memory-vault.log",
        ),
        _plist("haus.sanctum.mlx", mlx_args, env=mlx_env, log_name="sanctum-mlx.log"),
    ]

    if coder_model_dir is not None and ram_gb >= 60:
        coder_args = [
            str(bin_dir / "sanctum-mlx"),
            "--model",
            str(coder_model_dir),
            "--no-plain",
            "--tls-host",
            "127.0.0.1",
            "--tls-port",
            "3301",
            "--tls-cert",
            str(certs_dir / "server.crt"),
            "--tls-key",
            str(certs_dir / "server.key"),
            "--tls-client-ca",
            str(certs_dir / "ca.crt"),
            "--metal-cache-limit-mb",
            "512",
            "--metal-memory-limit-mb",
            "10000",
            "--metal-wired-limit-mb",
            "8000",
            "--temp",
            "0.4",
            "--max-prompt-tokens",
            "32768",
        ]
        coder_env = {
            **base_env,
            "RUST_LOG": "sanctum_mlx=info",
            "SANCTUM_MLX_LABEL": "coder",
            "SANCTUM_MLX_FUSED_MLP": "1",
            "SANCTUM_MLX_FUSED_QKV": "1",
            "SANCTUM_MLX_KV_QUANT": "8",
            "SANCTUM_MLX_SSM_LADDER": "8",
        }
        specs.append(
            _plist("haus.sanctum.mlx-coder", coder_args, env=coder_env, log_name="sanctum-mlx-coder.log")
        )

    specs.extend(
        [
            _plist(
                "haus.sanctum.proxyd",
                [str(bin_dir / "proxyd")],
                env=proxyd_env,
                log_name="proxyd.log",
                workdir=proxyd_dir,
            ),
            _plist(
                "haus.sanctum.force-flow",
                [str(bin_dir / "sanctum-force-flow")],
                log_name="force-flow.log",
                workdir=home / ".sanctum" / "force-flow",
            ),
            _plist(
                "haus.sanctum.msg-bus",
                [str(bin_dir / "sanctum-msg-bus")],
                log_name="msg-bus.log",
                workdir=home / ".sanctum" / "msg-bus",
            ),
            _plist("haus.sanctum.chitti", [str(bin_dir / "sanctum-chitti")], log_name="chitti.log"),
            _plist(
                "haus.sanctum.thalamus",
                [str(bin_dir / "sanctum-thalamus")],
                env=thalamus_env,
                log_name="thalamus.log",
            ),
            _plist(
                "haus.sanctum.triage",
                [str(bin_dir / "sanctum-triage"), "--interval", "30"],
                log_name="triage.log",
            ),
            _plist(
                "haus.sanctum.pressure-valve",
                [str(bin_dir / "sanctum-pressure-valve")],
                log_name="pressure-valve.log",
            ),
            _plist("haus.sanctum.watchdog", [str(bin_dir / "sanctumd")], log_name="watchdog.log"),
            _plist(
                "haus.sanctum.castellan",
                [str(bin_dir / "castellan"), "--capacity-file", str(castellan_cap), "daemon"],
                env=castellan_env,
                log_name="castellan.log",
            ),
            _plist("haus.sanctum.chi", [str(bin_dir / "sanctum-chi")], log_name="chi.log"),
        ]
    )

    resolved_cli = sanctum_bin or shutil.which("sanctum") or "/opt/homebrew/bin/sanctum"
    specs.append(
        _plist(
            "haus.sanctum.endocrine-gland",
            [resolved_cli, "endocrine", "beat"],
            log_name="endocrine-gland.log",
            start_interval=300,
        )
    )
    specs.append(
        _plist(
            "haus.sanctum.r2d2",
            [resolved_cli, "haus", "heartbeat"],
            log_name="r2d2-heartbeat.log",
            start_interval=300,
        )
    )
    return specs


def write_and_bootstrap_launchagents(
    specs: list[dict[str, Any]],
    *,
    agents_dir: Path,
    boot: bool = True,
) -> list[Path]:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (sanctum_root() / "logs").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".openclaw" / "logs").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".openclaw" / "state").mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    written: list[Path] = []
    for spec in specs:
        label = str(spec["Label"])
        bin_path = Path(str(spec["ProgramArguments"][0]))
        if not bin_path.is_file():
            continue
        wd = spec.get("WorkingDirectory")
        if isinstance(wd, str) and wd:
            Path(wd).mkdir(parents=True, exist_ok=True)
        plist_path = agents_dir / f"{label}.plist"
        with plist_path.open("wb") as fh:
            plistlib.dump(spec, fh)
        written.append(plist_path)
        if boot:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                capture_output=True,
                check=False,
            )
    return written


def _probe_http(url: str, *, ca_cert: Path | None = None, client_cert: tuple[Path, Path] | None = None) -> bool:
    try:
        verify: ssl.SSLContext | bool = True
        if ca_cert and ca_cert.is_file():
            ctx = ssl.create_default_context(cafile=str(ca_cert))
            if client_cert and client_cert[0].is_file() and client_cert[1].is_file():
                ctx.load_cert_chain(certfile=str(client_cert[0]), keyfile=str(client_cert[1]))
            verify = ctx
        with httpx.Client(timeout=2.0, verify=verify) as client:
            r = client.get(url)
            return r.status_code < 500
    except Exception:
        return False


def _resolve_client_cert(certs_dir: Path) -> tuple[Path, Path] | None:
    """Locate an mTLS client cert+key pair (supports both fresh hub and per-service operator layouts)."""
    direct_crt = certs_dir / "client.crt"
    direct_key = certs_dir / "client.key"
    if direct_crt.is_file() and direct_key.is_file():
        return (direct_crt, direct_key)
    clients_dir = certs_dir / "clients"
    if clients_dir.is_dir():
        for crt in sorted(clients_dir.glob("*.crt")):
            key = crt.with_suffix(".key")
            if key.is_file():
                return (crt, key)
    return None


def proxyd_health(*, certs_dir: Path | None = None) -> bool:
    cdir = certs_dir or (sanctum_root() / "certs")
    return _probe_http("https://127.0.0.1:4040/health", ca_cert=cdir / "ca.crt")


def mlx_health(*, certs_dir: Path | None = None, port: int = 1337) -> bool:
    cdir = certs_dir or (sanctum_root() / "certs")
    cli_cert = _resolve_client_cert(cdir)
    return _probe_http(
        f"https://127.0.0.1:{port}/health",
        ca_cert=cdir / "ca.crt",
        client_cert=cli_cert,
    )


def probe_core_services(*, certs_dir: Path | None = None) -> dict[str, bool]:
    """Probe all 12 core Sanctum Rust daemons on loopback."""
    cdir = certs_dir or (sanctum_root() / "certs")
    ca = cdir / "ca.crt"
    cli_cert = _resolve_client_cert(cdir)
    return {
        "sanctum-mlx (:1337 mTLS)": _probe_http("https://127.0.0.1:1337/health", ca_cert=ca, client_cert=cli_cert),
        "mlx-coder (:3301 mTLS)": _probe_http("https://127.0.0.1:3301/health", ca_cert=ca, client_cert=cli_cert),
        "proxyd (:4040 TLS)": _probe_http("https://127.0.0.1:4040/health", ca_cert=ca),
        "sanctum-presidio (:5002)": _probe_http("http://127.0.0.1:5002/health"),
        "sanctum-memory (:18097)": _probe_http("http://127.0.0.1:18097/health"),
        "sanctum-force-flow (:4077)": _probe_http("http://127.0.0.1:4077/health"),
        "sanctum-msg-bus (:4076)": _probe_http("http://127.0.0.1:4076/health"),
        "sanctum-chitti (:2188)": _probe_http("http://127.0.0.1:2188/health"),
        "sanctum-thalamus (:1988)": _probe_http("http://127.0.0.1:1988/health"),
        "sanctum-watchdog (:2187)": _probe_http("http://127.0.0.1:2187/health"),
        "castellan (:2189)": _probe_http("http://127.0.0.1:2189/status"),
        "sanctum-chi (:7777)": _probe_http("http://127.0.0.1:7777/health"),
    }


def probe_subscription_seats() -> dict[str, dict[str, Any]]:
    """Probe the three $0 flat-rate subscription proxies (Claude Max, Grok OAuth, Gemini AGY)."""
    return {
        "claude-max": {
            "port": 3456,
            "seat": "council-max-thinking",
            "up": _probe_http("http://127.0.0.1:3456/health") or _probe_http("http://127.0.0.1:3456/v1/models"),
        },
        "grok-oauth": {
            "port": 4200,
            "seat": "council-finance",
            "up": _probe_http("http://127.0.0.1:4200/health"),
        },
        "gemini-agy": {
            "port": 6543,
            "seat": "gemini-31-pro",
            "up": _probe_http("http://127.0.0.1:6543/health") or _probe_http("http://127.0.0.1:6543/v1/models"),
        },
    }


def wire_subscription_seat_in_proxyd(
    *,
    config_path: Path,
    seat: str,
) -> bool:
    """Wire a $0 local subscription proxy seat (grok|gemini|claude) into portable proxyd config.yaml."""
    if not config_path.is_file():
        return False
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        return False
    models = cfg.setdefault("models", [])
    aliases = cfg.setdefault("model_aliases", {})
    fallbacks = cfg.setdefault("fallbacks", {})
    if not isinstance(models, list) or not isinstance(aliases, dict) or not isinstance(fallbacks, dict):
        return False

    seat_defs: dict[str, dict[str, Any]] = {
        "grok": {
            "name": "council-finance",
            "provider": "local",
            "api_model": "grok-best",
            "api_base": "http://127.0.0.1:4200",
            "max_tokens": 8192,
            "context_window": 500000,
            "ttfb_timeout_secs": 600,
            "read_timeout_secs": 900,
        },
        "gemini": {
            "name": "gemini-31-pro",
            "provider": "local",
            "api_model": "gemini-4-carbon",
            "api_base": "http://127.0.0.1:6543",
            "max_tokens": 16384,
            "ttfb_timeout_secs": 300,
            "read_timeout_secs": 600,
        },
        "claude": {
            "name": "council-max-thinking",
            "provider": "local",
            "api_model": "opus",
            "api_base": "http://127.0.0.1:3456",
            "max_tokens": 16384,
            "ttfb_timeout_secs": 600,
            "read_timeout_secs": 900,
        },
    }
    entry = seat_defs.get(seat)
    if entry is None:
        return False

    name = str(entry["name"])
    aliases.pop(name, None)
    models[:] = [m for m in models if not (isinstance(m, dict) and m.get("name") == name)]
    models.append(entry)
    fallbacks[name] = ["council-mlx"]
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return True


def post_r2d2_heartbeat(
    *,
    port: int = 2188,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Post a clean supervisor cycle heartbeat to chitti (:2188) with control-plane IPC token."""
    tok_path = sanctum_root() / "secrets" / "control-plane-ipc.token"
    headers: dict[str, str] = {}
    if tok_path.is_file():
        tok = tok_path.read_text(encoding="utf-8").strip()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    try:
        with httpx.Client(timeout=2.0, transport=transport) as client:
            r = client.post(
                f"http://127.0.0.1:{port}/action",
                headers=headers,
                json={
                    "service": "r2d2",
                    "pattern": "cycle",
                    "action": "haus-heartbeat",
                    "success": True,
                },
            )
            return r.status_code < 400
    except Exception:
        return False


def update_rust_suite_if_needed(
    *,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, str]:
    """Update installed Rust suite binaries + mlx.metallib to latest release if newer."""
    st = load_rust_state()
    if not st.get("release_tag"):
        return True, "not installed"

    latest = fetch_latest_rust_release(transport=transport)
    cur_tag = str(st.get("release_tag") or "")
    cur_sha = str(st.get("tar_sha256") or "")
    needs = cur_tag != latest["rust_tag"] or (
        bool(latest["rust_sha256"]) and cur_sha.lower() != latest["rust_sha256"].lower()
    )
    if not needs:
        return True, f"already up-to-date ({cur_tag})"
    if dry_run:
        return True, f"would upgrade {cur_tag} -> {latest['rust_tag']}"

    try:
        bin_dir = sanctum_root() / "bin"
        actual_sha, binaries = download_and_install_rust_suite(
            latest["rust_url"], latest["rust_sha256"], bin_dir, transport=transport
        )
        st.update(
            {
                "release_tag": latest["rust_tag"],
                "tar_sha256": actual_sha,
                "binaries": binaries,
                "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            }
        )
        save_rust_state(st)
        uid = os.getuid()
        for label, _ in CORE_LAUNCHAGENTS:
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                capture_output=True,
                check=False,
            )
        return True, f"upgraded {cur_tag} -> {latest['rust_tag']} ({len(binaries)} binaries)"
    except Exception as exc:
        return False, f"failed: {exc}"


@app.command(
    "install",
    help="Install ALL sanctum-rs Rust services, local mTLS PKI, Dragon + Coder models, Memory Vault, and LaunchAgents.",
)
def haus_install(
    tier: Annotated[str, typer.Option("--tier", help="Model tier: auto | 27b | 9b.")] = "auto",
    skip_model: Annotated[
        bool, typer.Option("--skip-model", help="Install Rust services & PKI without downloading HF model weights.")
    ] = False,
    skip_coder: Annotated[
        bool, typer.Option("--skip-coder", help="Skip secondary 9B Coder cathedral (:3301) on 64 GB+ Macs.")
    ] = False,
    skip_boot: Annotated[
        bool, typer.Option("--skip-boot", help="Stage LaunchAgent plists without bootstrapping them now.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show plan without modifying this Mac.")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Allow running even on an existing operator hub.")
    ] = False,
) -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        console.print("[red]Sanctum Rust Engine requires an Apple Silicon Mac (M1/M2/M3/M4).[/]")
        raise typer.Exit(code=1)

    if is_operator_haus() and not force:
        console.print(
            Panel.fit(
                "[yellow]This Mac already runs the full production operator hub.[/]\n"
                "Skipping fresh hub provisioning so live certs/configs stay untouched.\n"
                "[dim]Pass `--force` only if you intentionally want to re-provision.[/]",
                title="Operator Haus Guard",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=0)

    ram_gb = detect_ram_gb()
    chosen_tier, hf_repo = dragon_cmd.resolve_tier(tier, ram_gb=ram_gb)
    model_basename = hf_repo.split("/")[-1]
    want_coder = (ram_gb >= 60) and (not skip_coder)
    coder_basename = dragon_cmd.MODEL_9B.split("/")[-1] if want_coder else None
    rust_rel = fetch_latest_rust_release()
    dragon_rel = dragon_cmd.fetch_latest_dragon_release()

    home = Path.home()
    root = sanctum_root()
    bin_dir = root / "bin"
    certs_dir = root / "certs"
    secrets_dir = root / "secrets"
    model_dir = home / "models" / "mlx" / model_basename
    coder_model_dir = (home / "models" / "mlx" / coder_basename) if coder_basename else None
    adapter_dir = (
        home / "models" / "adapters" / dragon_rel["dragon_adapter"]
        if chosen_tier == "27b"
        else None
    )

    coder_line = (
        f"Coder:       [bold]9b[/] → {dragon_cmd.MODEL_9B} (:3301 mTLS — 64 GB+ dual-cathedral hub)\n"
        if want_coder
        else ""
    )
    console.print(
        Panel.fit(
            f"[bold]Sanctum Full Rust Engine Installer (Full Hub)[/]\n"
            f"Rust Suite:  [cyan]{rust_rel['rust_tag']}[/] (32 signed sanctum-rs binaries + metallib + ablation)\n"
            f"Cathedral:   [bold]{chosen_tier}[/] → {hf_repo} (:1337 mTLS + :6669 ablation)\n"
            f"{coder_line}"
            f"Champion:    {dragon_rel['dragon_tag']} ({dragon_rel['dragon_adapter']})\n"
            f"Install dir: {bin_dir}\n"
            f"Hardware:    Apple Silicon · {ram_gb} GB RAM",
            border_style="cyan",
        )
    )

    if dry_run:
        console.print("[dim](dry-run — no files downloaded or services modified)[/]")
        return

    # Step 1: Rust Suite binaries + mlx.metallib + ablation vectors
    console.print("\n[bold]Step 1.[/] Downloading & verifying signed sanctum-rs Rust suite…")
    try:
        tar_sha, binaries = download_and_install_rust_suite(
            rust_rel["rust_url"], rust_rel["rust_sha256"], bin_dir
        )
    except Exception as exc:
        console.print(f"[red]Rust suite install failed:[/] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"  [green]✓[/] Installed {len(binaries)} signed Rust artifacts into {bin_dir}")

    # Step 2: Per-machine mTLS PKI, IPC tokens, Keychain & hub support files
    console.print("[bold]Step 2.[/] Provisioning unique per-machine mTLS PKI, Keychain & Memory Vault…")
    mint_local_pki(certs_dir, force=False)
    ensure_local_tokens(secrets_dir, sync_keychain=True)
    prewarm_path, ablation_path = ensure_hub_support_files(root)
    console.print("  [green]✓[/] mTLS certs, IPC tokens, Memory Vault & cathedral prewarm ready")

    # Step 3: Base HF model(s) + Champion Dragon LoRA
    if not skip_model:
        console.print(f"[bold]Step 3.[/] Ensuring local model {hf_repo} → {model_dir}…")
        try:
            download_hf_snapshot(hf_repo, model_dir)
        except Exception as exc:
            console.print(f"[red]Model download failed:[/] {exc}")
            raise typer.Exit(code=1) from exc
        if adapter_dir is not None:
            console.print(f"  Installing Dragon champion LoRA ({dragon_rel['dragon_tag']})…")
            staging = dragon_cmd.dragon_dir()
            try:
                zip_sha = dragon_cmd._download_and_verify_bundle(
                    dragon_rel["dragon_url"], dragon_rel["dragon_sha256"], staging
                )
                src_ad = staging / "adapter"
                if src_ad.is_dir():
                    adapter_dir.parent.mkdir(parents=True, exist_ok=True)
                    if adapter_dir.exists():
                        shutil.rmtree(adapter_dir)
                    shutil.copytree(src_ad, adapter_dir)
                dragon_cmd.save_manifest(
                    {
                        "release_tag": dragon_rel["dragon_tag"],
                        "champion_id": dragon_rel["dragon_adapter"],
                        "zip_sha256": zip_sha,
                        "mlx_lm_version": dragon_rel["mlx_lm_version"],
                        "runtime": "sanctum-mlx-rust",
                        "tier": chosen_tier,
                        "model_id": hf_repo,
                        "model_dir": str(model_dir),
                        "adapter_dir": str(adapter_dir),
                        "installed_at": dt.datetime.now(dt.UTC).isoformat(),
                        "updated_at": dt.datetime.now(dt.UTC).isoformat(),
                    }
                )
            except Exception as exc:
                console.print(f"[red]Champion LoRA install failed:[/] {exc}")
                raise typer.Exit(code=1) from exc
        if coder_model_dir is not None:
            console.print(f"  Ensuring Coder cathedral {dragon_cmd.MODEL_9B} → {coder_model_dir}…")
            try:
                download_hf_snapshot(dragon_cmd.MODEL_9B, coder_model_dir)
            except Exception as exc:
                console.print(f"[yellow]Coder cathedral download failed (continuing with primary):[/] {exc}")
                coder_model_dir = None
                coder_basename = None
        console.print("  [green]✓[/] Models & Dragon champion ready")
    else:
        console.print("[bold]Step 3.[/] [dim]--skip-model: skipped HF model download[/]")

    # Step 4: Portable configs
    console.print("[bold]Step 4.[/] Generating portable proxyd, Castellan, Msg-Bus & hub configs…")
    ensure_hub_instance_yaml(root / "instance.yaml")
    write_portable_proxyd_config(
        config_path=root / "sanctum-proxy" / "config.yaml",
        certs_dir=certs_dir,
        api_model_name=model_basename,
        coder_model_name=coder_basename,
        force=False,
    )
    write_portable_castellan_capacity(
        root / "castellan" / "capacity.yaml",
        ram_gb=ram_gb,
        force=False,
    )
    write_portable_msg_bus_config(
        root / "msg-bus" / "config.yaml",
        force=False,
    )
    console.print(
        "  [green]✓[/] Configs ready (role=hub, mTLS :1337, TLS proxyd :4040, "
        "Presidio :5001/:5002, Memory :18097, Castellan :2189, Msg-Bus :4076)"
    )

    # Step 5: LaunchAgent fleet
    console.print("[bold]Step 5.[/] Installing macOS LaunchAgent fleet (auto-starts on boot)…")
    specs = build_launchagent_specs(
        home=home,
        model_dir=model_dir,
        adapter_dir=adapter_dir,
        ram_gb=ram_gb,
        coder_model_dir=coder_model_dir,
        ablation_path=ablation_path,
        prewarm_path=prewarm_path,
    )
    written = write_and_bootstrap_launchagents(
        specs, agents_dir=home / "Library" / "LaunchAgents", boot=not skip_boot
    )
    console.print(f"  [green]✓[/] Installed {len(written)} LaunchAgents")

    save_rust_state(
        {
            "release_tag": rust_rel["rust_tag"],
            "tar_sha256": tar_sha,
            "binaries": binaries,
            "tier": chosen_tier,
            "ram_gb": ram_gb,
            "has_coder": coder_model_dir is not None,
            "model_dir": str(model_dir),
            "coder_model_dir": str(coder_model_dir) if coder_model_dir else None,
            "adapter_dir": str(adapter_dir) if adapter_dir else None,
            "launchagents": [p.stem for p in written],
            "installed_at": dt.datetime.now(dt.UTC).isoformat(),
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
        }
    )

    if not skip_boot:
        time.sleep(2)
        post_r2d2_heartbeat()
        presidio_ok = _probe_http("http://127.0.0.1:5002/health")
        proxyd_ok = _probe_http("https://127.0.0.1:4040/health", ca_cert=certs_dir / "ca.crt")
        mem_ok = _probe_http("http://127.0.0.1:18097/health")
        mlx_ok = _probe_http(
            "https://127.0.0.1:1337/health",
            ca_cert=certs_dir / "ca.crt",
            client_cert=(certs_dir / "client.crt", certs_dir / "client.key"),
        )
        console.print(
            f"\n  Live probes: presidio={'[green]UP[/]' if presidio_ok else '[yellow]starting[/]'} · "
            f"memory={'[green]UP[/]' if mem_ok else '[yellow]starting[/]'} · "
            f"proxyd={'[green]UP[/]' if proxyd_ok else '[yellow]starting[/]'} · "
            f"sanctum-mlx={'[green]UP[/]' if mlx_ok else '[yellow]loading GPU weights[/]'}"
        )

    console.print(
        Panel(
            f"[bold green]Sanctum Rust Engine installed ({rust_rel['rust_tag']}).[/]\n\n"
            "  [cyan]sanctum haus status[/]    live health of all Rust services\n"
            "  [cyan]sanctum council[/]        open the Jedi Council (via Rust proxyd + sanctum-mlx)\n"
            "  [cyan]sanctum yoda setup[/]     connect Yoda on Signal (wired to Rust proxyd :4040)\n"
            "  [cyan]sanctum update[/]         update CLI + Rust suite + Dragon + Yoda in one command",
            border_style="green",
        )
    )


@app.command("status", help="Show live status of the Sanctum Rust service suite.")
def haus_status(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    st = load_rust_state()
    bin_dir = sanctum_root() / "bin"
    probes = probe_core_services()
    latest = fetch_latest_rust_release()
    installed_bins = sorted(
        p.name for p in bin_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    ) if bin_dir.is_dir() else []

    info = {
        "installed": is_rust_suite_installed() or is_operator_haus(),
        "operator_haus": is_operator_haus(),
        "release_tag": st.get("release_tag") or ("operator-live" if is_operator_haus() else None),
        "latest_tag": latest["rust_tag"],
        "binary_count": len(installed_bins),
        "probes": probes,
    }
    if json_output:
        print(json.dumps(info, indent=2))
        return

    t = Table(title="Sanctum Rust Engine Suite", show_header=True, header_style="bold")
    t.add_column("Service")
    t.add_column("State", justify="right")
    for name, ok in probes.items():
        t.add_row(name, "[green]HEALTHY[/]" if ok else "[dim]offline[/]")
    console.print(t)
    console.print(
        f"[dim]Release: {info['release_tag'] or 'not installed'} (latest {info['latest_tag']}) · "
        f"{info['binary_count']} artifacts in {bin_dir}[/]"
    )


@app.command("update", help="Update all sanctum-rs Rust binaries and mlx.metallib to latest.")
def haus_update(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Check only, do not apply.")] = False,
) -> None:
    if not is_rust_suite_installed():
        console.print("[yellow]Rust suite not managed here yet.[/] Run `sanctum haus install`.")
        raise typer.Exit(code=1)
    ok, msg = update_rust_suite_if_needed(dry_run=dry_run)
    if ok:
        console.print(f"[green]✓[/] Rust suite: {msg}")
    else:
        console.print(f"[red]✗[/] Rust suite {msg}")
        raise typer.Exit(code=1)


@app.command("start", help="Start/kickstart all installed Sanctum Rust LaunchAgents.")
def haus_start() -> None:
    uid = os.getuid()
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    started = 0
    for label, _ in CORE_LAUNCHAGENTS:
        plist = agents_dir / f"{label}.plist"
        if plist.is_file():
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, check=False)
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], capture_output=True, check=False)
            started += 1
    console.print(f"[green]✓[/] Started/reloaded {started} Sanctum Rust services.")


@app.command(
    "seats",
    help="Inspect or enable $0 flat-rate subscription proxies (Grok :4200, Gemini :6543, Claude Max :3456).",
)
def haus_seats(
    enable: Annotated[
        str | None,
        typer.Option("--enable", help="Enable a $0 subscription seat: grok | gemini | claude | all."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Allow modifying proxyd config even on an operator haus."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    if enable:
        if is_operator_haus() and not force:
            console.print(
                "[yellow]Operator haus guard:[/] skipping proxyd config mutation (pass `--force` to override)."
            )
            raise typer.Exit(code=0)

        targets = ["grok", "gemini", "claude"] if enable.strip().lower() == "all" else [enable.strip().lower()]
        home = Path.home()
        bin_dir = sanctum_root() / "bin"
        log_dir = sanctum_root() / "logs"
        agents_dir = home / "Library" / "LaunchAgents"
        cfg_path = sanctum_root() / "sanctum-proxy" / "config.yaml"
        base_env = {
            "HOME": str(home),
            "PATH": f"{bin_dir}:{home / '.local' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        }
        specs: list[dict[str, Any]] = []
        for t in targets:
            if t == "grok" and (bin_dir / "sanctum-grok-proxy").is_file():
                specs.append(
                    {
                        "Label": "haus.sanctum.grok-oauth-proxy",
                        "ProgramArguments": [str(bin_dir / "sanctum-grok-proxy")],
                        "EnvironmentVariables": {**base_env, "GROK_OAUTH_PROXY_PORT": "4200"},
                        "RunAtLoad": True,
                        "KeepAlive": True,
                        "ProcessType": "Standard",
                        "StandardOutPath": str(log_dir / "grok-oauth-proxy.log"),
                        "StandardErrorPath": str(log_dir / "grok-oauth-proxy.log"),
                    }
                )
            elif t == "gemini" and (bin_dir / "sanctum-agy-proxy").is_file():
                specs.append(
                    {
                        "Label": "haus.sanctum.agy-proxy",
                        "ProgramArguments": [str(bin_dir / "sanctum-agy-proxy")],
                        "EnvironmentVariables": {
                            **base_env,
                            "AGY_PROXY_HOST": "127.0.0.1",
                            "AGY_PROXY_PORT": "6543",
                            "AGY_PROXY_CONCURRENCY": "1",
                        },
                        "RunAtLoad": True,
                        "KeepAlive": True,
                        "ProcessType": "Standard",
                        "StandardOutPath": str(log_dir / "agy-proxy.log"),
                        "StandardErrorPath": str(log_dir / "agy-proxy.log"),
                    }
                )
            wired = wire_subscription_seat_in_proxyd(config_path=cfg_path, seat=t)
            console.print(
                f"  [green]✓[/] Seat [bold]{t}[/]: "
                + ("wired into proxyd config.yaml ($0 sub, council-mlx fallback)" if wired else "proxyd config.yaml not found (run `sanctum haus install` first)")
            )
        if specs:
            written = write_and_bootstrap_launchagents(specs, agents_dir=agents_dir, boot=True)
            console.print(f"  [green]✓[/] Bootstrapped {len(written)} Rust subscription proxy LaunchAgent(s)")

    seats = probe_subscription_seats()
    if json_output:
        print(json.dumps(seats, indent=2))
        return

    tbl = Table(title="Sanctum $0 Subscription Seats", show_header=True, header_style="bold")
    tbl.add_column("Proxy")
    tbl.add_column("Port", justify="right")
    tbl.add_column("Council Seat")
    tbl.add_column("State", justify="right")
    for name, info in seats.items():
        tbl.add_row(
            name,
            f":{info['port']}",
            str(info["seat"]),
            "[green]LIVE[/]" if info["up"] else "[dim]offline[/]",
        )
    console.print(tbl)
    console.print(
        "[dim]Enable any seat with `sanctum haus seats --enable grok|gemini|claude|all` "
        "(zero per-token API fees; falls back to local sanctum-mlx).[/]"
    )


@app.command("heartbeat", help="Post an R2D2 supervisor cycle heartbeat to chitti (:2188).")
def haus_heartbeat() -> None:
    if post_r2d2_heartbeat():
        console.print("[green]✓[/] Posted heartbeat to chitti (:2188)")
    else:
        console.print("[yellow]chitti (:2188) unreachable[/]")
        raise typer.Exit(code=1)


@app.command("stop", help="Stop all installed Sanctum Rust LaunchAgents.")
def haus_stop() -> None:
    uid = os.getuid()
    stopped = 0
    for label, _ in CORE_LAUNCHAGENTS:
        r = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, check=False)
        if r.returncode == 0:
            stopped += 1
    console.print(f"[green]✓[/] Stopped {stopped} Sanctum Rust services.")
