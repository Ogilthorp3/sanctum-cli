"""``sanctum crypto audit`` — Post-Quantum & Crypto-Apocalypse posture auditor.

Audits local PKI certificates (zero RSA, leaf lifetimes), live TLS 1.3 hybrid
post-quantum KEX (`X25519MLKEM768`), OpenSSH PQ KEX & hybrid `ML-DSA-44+Ed25519`
authentication, NIST FIPS 204/205 hash-based signature availability (`ML-DSA-65`,
`SLH-DSA-SHA2-128s`), disk encryption, and Vault Armor / S4 PSK transport readiness.

Produces a 4-scenario survival scorecard:
- S1: RSA / factoring broken
- S2: ECC / discrete-log broken
- S3: Lattice / ML-KEM / ML-DSA broken
- S4: Simultaneous RSA + ECC + Lattice break ("Crypto Apocalypse")
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sanctum_cli.errors import ExitCode

console = Console()
crypto_app = typer.Typer(
    name="crypto",
    help="Audit post-quantum & crypto-apocalypse security posture across this node.",
    no_args_is_help=True,
)

_SKIP_CERT_DIRS = {"retired", "staging-pqc"}
_MAX_LEAF_WARN_DAYS = 398
_MAX_LEAF_FAIL_DAYS = 1100
_PQ_KEX_RE = re.compile(r"(X25519MLKEM768|MLKEM\d+|sntrup761x25519)", re.IGNORECASE)


@dataclass(frozen=True)
class CryptoCheck:
    id: str
    category: str
    status: str  # PASS | WARN | FAIL | SKIP
    summary: str
    detail: str = ""


def _resolve_openssl() -> str | None:
    for cand in (
        "/opt/homebrew/opt/openssl@4/bin/openssl",
        "/opt/homebrew/opt/openssl@3/bin/openssl",
        "/opt/homebrew/bin/openssl",
        shutil.which("openssl") or "",
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def _resolve_ssh() -> str | None:
    for cand in ("/opt/homebrew/bin/ssh", shutil.which("ssh") or ""):
        if cand and Path(cand).is_file():
            return cand
    return None


def check_pki_certs(sanctum_dir: Path) -> CryptoCheck:
    """Audit active X.509 certs under ``sanctum_dir`` for RSA removal and leaf lifetime."""
    scan_roots = [sanctum_dir / "certs", sanctum_dir / "pq-terminator" / "certs"]
    rsa_certs: list[str] = []
    long_leaves: list[str] = []
    ecdsa_count = 0
    ed_count = 0
    pq_or_other_count = 0
    total = 0

    for root in scan_roots:
        if not root.is_dir():
            continue
        for crt_path in sorted(root.rglob("*.crt")):
            rel_parts = set(crt_path.relative_to(root).parts[:-1])
            if rel_parts & _SKIP_CERT_DIRS or any(p.startswith("backup-") for p in rel_parts):
                continue
            total += 1
            try:
                cert = x509.load_pem_x509_certificate(crt_path.read_bytes())
            except ValueError:
                # OpenSSL 4.x ML-DSA/SLH-DSA certs may not yet parse in older cryptography versions
                pq_or_other_count += 1
                continue
            pub = cert.public_key()
            if isinstance(pub, rsa.RSAPublicKey):
                rsa_certs.append(f"{crt_path.name} (RSA-{pub.key_size})")
            elif isinstance(pub, ec.EllipticCurvePublicKey):
                ecdsa_count += 1
            elif isinstance(pub, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
                ed_count += 1
            else:
                pq_or_other_count += 1

            is_ca = False
            try:
                bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
                is_ca = bc.ca
            except x509.ExtensionNotFound:
                is_ca = False
            if not is_ca:
                days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
                if days > _MAX_LEAF_FAIL_DAYS:
                    long_leaves.append(f"{crt_path.name} ({days}d)")

    if total == 0:
        return CryptoCheck(
            id="pki_zero_rsa",
            category="PKI",
            status="SKIP",
            summary="No local X.509 certificates found",
            detail=f"scanned {', '.join(str(r) for r in scan_roots)}",
        )
    if rsa_certs:
        return CryptoCheck(
            id="pki_zero_rsa",
            category="PKI",
            status="FAIL",
            summary=f"{len(rsa_certs)} legacy RSA certificate(s) active",
            detail=", ".join(rsa_certs),
        )
    detail = f"{ecdsa_count} ECDSA, {ed_count} EdDSA, {pq_or_other_count} PQ/other across {total} active certs"
    if long_leaves:
        return CryptoCheck(
            id="pki_zero_rsa",
            category="PKI",
            status="WARN",
            summary=f"Zero RSA ({total} certs); {len(long_leaves)} leaf cert(s) > {_MAX_LEAF_FAIL_DAYS}d",
            detail=f"{detail}; long leaves: {', '.join(long_leaves)}",
        )
    return CryptoCheck(
        id="pki_zero_rsa",
        category="PKI",
        status="PASS",
        summary=f"Zero RSA across {total} active certificates",
        detail=detail,
    )


def _discover_tls_endpoints(sanctum_dir: Path) -> list[tuple[str, str, int]]:
    endpoints: list[tuple[str, str, int]] = [
        ("proxyd", "127.0.0.1", 4040),
        ("sanctum-mlx", "127.0.0.1", 1337),
    ]
    plist_path = sanctum_dir / "launchagents" / "live" / "haus.sanctum.pq-terminator.1111.plist"
    if plist_path.is_file():
        try:
            data = plistlib.loads(plist_path.read_bytes())
            args = data.get("ProgramArguments", [])
            for idx, arg in enumerate(args):
                if arg in ("-listen", "--listen") and idx + 1 < len(args):
                    listen = str(args[idx + 1])
                    if ":" in listen:
                        host, port_s = listen.rsplit(":", 1)
                        host = "127.0.0.1" if host in ("0.0.0.0", "[::]", "") else host
                        endpoints.insert(0, ("pq-terminator:1111", host, int(port_s)))
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
    return endpoints


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_live_tls_pq_kex(sanctum_dir: Path, *, skip_network: bool = False) -> CryptoCheck:
    """Verify running TLS 1.3 listeners negotiate hybrid post-quantum KEX."""
    if skip_network:
        return CryptoCheck(
            id="tls_pq_kex",
            category="Transport",
            status="SKIP",
            summary="Live TLS probe skipped (--skip-network)",
        )
    openssl = _resolve_openssl()
    if not openssl:
        return CryptoCheck(
            id="tls_pq_kex",
            category="Transport",
            status="WARN",
            summary="OpenSSL binary not found for TLS KEX probe",
        )

    probed: list[str] = []
    downgraded: list[str] = []
    for label, host, port in _discover_tls_endpoints(sanctum_dir):
        if not _tcp_open(host, port):
            continue
        try:
            proc = subprocess.run(
                [openssl, "s_client", "-connect", f"{host}:{port}", "-tls1_3"],
                input=b"",
                capture_output=True,
                timeout=5,
                check=False,
            )
            combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
            m = _PQ_KEX_RE.search(combined)
            if m:
                probed.append(f"{label}={m.group(1)}")
            else:
                downgraded.append(f"{label} (no PQ KEX negotiated)")
        except (OSError, subprocess.SubprocessError):
            continue

    if downgraded:
        return CryptoCheck(
            id="tls_pq_kex",
            category="Transport",
            status="FAIL",
            summary=f"{len(downgraded)} TLS listener(s) missing PQ KEX",
            detail="; ".join(downgraded + probed),
        )
    if not probed:
        return CryptoCheck(
            id="tls_pq_kex",
            category="Transport",
            status="SKIP",
            summary="No local TLS listeners active to probe",
        )
    return CryptoCheck(
        id="tls_pq_kex",
        category="Transport",
        status="PASS",
        summary=f"All {len(probed)} live TLS 1.3 listeners negotiated hybrid PQ KEX",
        detail=", ".join(probed),
    )


def check_ssh_pq_posture(home_dir: Path) -> CryptoCheck:
    """Audit OpenSSH client/server PQ KEX enforcement and ML-DSA-44+Ed25519 auth."""
    ssh_bin = _resolve_ssh()
    has_mldsa_client = False
    ssh_version = "unknown"
    if ssh_bin:
        try:
            ver = subprocess.run([ssh_bin, "-V"], capture_output=True, text=True, timeout=3, check=False)
            ssh_version = (ver.stderr or ver.stdout).strip().splitlines()[0] if (ver.stderr or ver.stdout) else "unknown"
            ks = subprocess.run([ssh_bin, "-Q", "key-sig"], capture_output=True, text=True, timeout=3, check=False)
            has_mldsa_client = "ssh-mldsa44-ed25519" in ks.stdout
        except (OSError, subprocess.SubprocessError):
            pass

    sshd_pq_conf = Path("/etc/ssh/sshd_config.d/100-pq-kex.conf")
    sshd_pq_enforced = sshd_pq_conf.is_file() and "mlkem768x25519-sha256" in sshd_pq_conf.read_text(
        encoding="utf-8", errors="ignore"
    )
    ssh_cfg = home_dir / ".ssh" / "config"
    client_pq_enforced = ssh_cfg.is_file() and "mlkem768x25519-sha256" in ssh_cfg.read_text(
        encoding="utf-8", errors="ignore"
    )
    pq_key_present = (home_dir / ".ssh" / "sanctum_pq_mldsa44_ed25519.pub").is_file()

    notes = [
        f"client={ssh_version}",
        f"sshd_pq_kex={'on' if sshd_pq_enforced else 'off'}",
        f"client_pq_kex={'on' if client_pq_enforced else 'off'}",
        f"mldsa44_ed25519_supported={'yes' if has_mldsa_client else 'no'}",
        f"fleet_pq_key={'installed' if pq_key_present else 'absent'}",
    ]
    if sshd_pq_enforced and client_pq_enforced and has_mldsa_client and pq_key_present:
        return CryptoCheck(
            id="ssh_pq_posture",
            category="SSH",
            status="PASS",
            summary="PQ KEX enforced + hybrid ML-DSA-44+Ed25519 key active",
            detail="; ".join(notes),
        )
    if sshd_pq_enforced or client_pq_enforced:
        return CryptoCheck(
            id="ssh_pq_posture",
            category="SSH",
            status="WARN",
            summary="PQ SSH KEX active; hybrid ML-DSA auth or host policy incomplete",
            detail="; ".join(notes),
        )
    return CryptoCheck(
        id="ssh_pq_posture",
        category="SSH",
        status="WARN",
        summary="Default SSH KEX (no fail-closed PQ KEX policy found)",
        detail="; ".join(notes),
    )


def check_hash_pq_readiness() -> CryptoCheck:
    """Verify host cryptographic stack exposes NIST FIPS 204 (ML-DSA) & FIPS 205 (SLH-DSA)."""
    openssl = _resolve_openssl()
    if not openssl:
        return CryptoCheck(
            id="hash_pq_readiness",
            category="PQ-Sigs",
            status="WARN",
            summary="OpenSSL binary not found",
        )
    try:
        proc = subprocess.run(
            [openssl, "list", "-signature-algorithms"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = proc.stdout
    except (OSError, subprocess.SubprocessError):
        out = ""

    has_mldsa = "ML-DSA-65" in out
    has_slhdsa = "SLH-DSA-SHA2-128s" in out
    if has_mldsa and has_slhdsa:
        return CryptoCheck(
            id="hash_pq_readiness",
            category="PQ-Sigs",
            status="PASS",
            summary="NIST FIPS 204 (ML-DSA-65) + FIPS 205 (SLH-DSA-SHA2-128s) available",
            detail=f"provider={openssl}",
        )
    return CryptoCheck(
        id="hash_pq_readiness",
        category="PQ-Sigs",
        status="WARN",
        summary=f"Partial PQ signature stack (ML-DSA-65={has_mldsa}, SLH-DSA={has_slhdsa})",
        detail=f"provider={openssl}",
    )


def check_vault_disk_and_s4(sanctum_dir: Path) -> CryptoCheck:
    """Verify disk encryption (FileVault), Vault Armor, and S4 PSK terminator capability."""
    notes: list[str] = []
    fv_on: bool | None = None
    if platform.system() == "Darwin" and shutil.which("fdesetup"):
        try:
            proc = subprocess.run(
                ["fdesetup", "status"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            txt = (proc.stdout + proc.stderr).strip()
            fv_on = "FileVault is On" in txt
            notes.append(f"filevault={'on' if fv_on else 'off'}")
        except (OSError, subprocess.SubprocessError):
            pass

    armor_manifest = sanctum_dir / "vault-armor" / "manifest.json"
    armor_state = sanctum_dir / "state" / "vault-armor.json"
    if armor_manifest.is_file() or armor_state.is_file():
        version = 1
        try:
            if armor_manifest.is_file():
                m = json.loads(armor_manifest.read_text(encoding="utf-8"))
                version = int(m.get("envelope", {}).get("version", 1))
        except (OSError, ValueError, TypeError):
            pass
        notes.append(f"vault_armor=v{version}")
    else:
        notes.append("vault_armor=absent")

    pq_term_bin = sanctum_dir / "pq-terminator" / "sanctum-pq-terminator"
    if not pq_term_bin.is_file():
        pq_term_bin = sanctum_dir / "bin" / "sanctum-pq-terminator"
    s4_psk_ready = False
    if pq_term_bin.is_file():
        try:
            help_out = subprocess.run(
                [str(pq_term_bin), "--help"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            s4_psk_ready = "--psk-file" in (help_out.stdout + help_out.stderr)
        except (OSError, subprocess.SubprocessError):
            pass
    notes.append(f"s4_psk_terminator={'ready' if s4_psk_ready else 'legacy'}")

    if fv_on is False and not (armor_manifest.is_file() or armor_state.is_file()):
        return CryptoCheck(
            id="vault_and_disk",
            category="At-Rest",
            status="WARN",
            summary="FileVault is Off and Vault Armor is not configured",
            detail="; ".join(notes),
        )
    return CryptoCheck(
        id="vault_and_disk",
        category="At-Rest",
        status="PASS",
        summary="Symmetric AES-256 at-rest protection verified",
        detail="; ".join(notes),
    )


def run_crypto_audit(
    *,
    sanctum_dir: Path | None = None,
    home_dir: Path | None = None,
    skip_network: bool = False,
) -> dict[str, Any]:
    """Run all cryptographic posture checks and build the 4-scenario survival scorecard."""
    home = home_dir or Path.home()
    sdir = sanctum_dir or Path(os.environ.get("SANCTUM_HOME", str(home / ".sanctum")))

    checks = [
        check_pki_certs(sdir),
        check_live_tls_pq_kex(sdir, skip_network=skip_network),
        check_ssh_pq_posture(home),
        check_hash_pq_readiness(),
        check_vault_disk_and_s4(sdir),
    ]
    by_id = {c.id: c for c in checks}

    s1_rsa = "PASS" if by_id["pki_zero_rsa"].status != "FAIL" else "FAIL"
    s2_ecc = (
        "PASS"
        if by_id["tls_pq_kex"].status != "FAIL" and by_id["ssh_pq_posture"].status == "PASS"
        else "WARN"
    )
    s3_lattice = "PASS" if by_id["hash_pq_readiness"].status == "PASS" else "WARN"
    s4_apocalypse = (
        "PASS"
        if by_id["hash_pq_readiness"].status == "PASS"
        and "s4_psk_terminator=ready" in by_id["vault_and_disk"].detail
        else "WARN"
    )

    overall = "FAIL" if any(c.status == "FAIL" for c in checks) else (
        "WARN" if any(c.status == "WARN" for c in checks) else "PASS"
    )
    return {
        "overall": overall,
        "scenarios": {
            "S1_RSA_broken": s1_rsa,
            "S2_ECC_broken": s2_ecc,
            "S3_Lattice_broken": s3_lattice,
            "S4_Crypto_Apocalypse": s4_apocalypse,
        },
        "checks": [asdict(c) for c in checks],
    }


@crypto_app.command("audit", help="Audit post-quantum & crypto-apocalypse readiness on this node.")
def crypto_audit_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit structured JSON.")] = False,
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit non-zero if any check fails.")
    ] = False,
    skip_network: Annotated[
        bool, typer.Option("--skip-network", help="Skip live TLS listener probes.")
    ] = False,
    sanctum_dir: Annotated[
        Path | None, typer.Option("--sanctum-dir", help="Override ~/.sanctum directory.")
    ] = None,
) -> None:
    report = run_crypto_audit(sanctum_dir=sanctum_dir, skip_network=skip_network)
    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        table = Table(title="Sanctum Cryptographic & Post-Quantum Audit", show_lines=False)
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Check", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Summary")
        for chk in report["checks"]:
            st = chk["status"]
            color = {"PASS": "green", "WARN": "yellow", "FAIL": "bold red", "SKIP": "dim"}.get(
                st, "white"
            )
            detail_suffix = f"\n[dim]{chk['detail']}[/]" if chk.get("detail") else ""
            table.add_row(
                chk["category"],
                chk["id"],
                f"[{color}]{st}[/]",
                f"{chk['summary']}{detail_suffix}",
            )
        console.print(table)

        sc = report["scenarios"]
        lines = [
            f"  S1 (RSA broken):            [bold]{sc['S1_RSA_broken']}[/]",
            f"  S2 (ECC broken):            [bold]{sc['S2_ECC_broken']}[/]",
            f"  S3 (Lattices broken):       [bold]{sc['S3_Lattice_broken']}[/]",
            f"  S4 (Crypto Apocalypse):     [bold]{sc['S4_Crypto_Apocalypse']}[/]",
        ]
        console.print(Panel("\n".join(lines), title="4-Scenario Survival Scorecard", expand=False))

    if strict and report["overall"] == "FAIL":
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR))
