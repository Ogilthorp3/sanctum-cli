"""Hermetic tests for ``sanctum crypto audit``."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import crypto_cmd, haus_cmd

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _write_rsa_cert(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "legacy-rsa")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subj)
        .issuer_name(subj)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_crypto_audit_passes_ecdsa_pki_and_flags_rsa(tmp_path: Path) -> None:
    sdir = tmp_path / ".sanctum"
    haus_cmd.mint_local_pki(sdir / "certs")

    chk_clean = crypto_cmd.check_pki_certs(sdir)
    assert chk_clean.status == "PASS"
    assert "Zero RSA" in chk_clean.summary

    # Dropping an RSA cert into retired/ must NOT fail active PKI audit.
    _write_rsa_cert(sdir / "certs" / "retired" / "old.crt")
    assert crypto_cmd.check_pki_certs(sdir).status == "PASS"

    # Dropping an active RSA cert MUST fail closed.
    _write_rsa_cert(sdir / "certs" / "clients" / "legacy.crt")
    chk_rsa = crypto_cmd.check_pki_certs(sdir)
    assert chk_rsa.status == "FAIL"
    assert "RSA-2048" in chk_rsa.detail


def test_crypto_audit_cli_json_and_strict_exit(tmp_path: Path) -> None:
    sdir = tmp_path / ".sanctum"
    haus_cmd.mint_local_pki(sdir / "certs")

    res = runner.invoke(
        app,
        ["crypto", "audit", "--json", "--skip-network", "--sanctum-dir", str(sdir)],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["scenarios"]["S1_RSA_broken"] == "PASS"
    assert len(data["checks"]) == 5

    # Active RSA cert triggers non-zero exit under --strict.
    _write_rsa_cert(sdir / "certs" / "bad-rsa.crt")
    res_strict = runner.invoke(
        app,
        [
            "crypto",
            "audit",
            "--json",
            "--strict",
            "--skip-network",
            "--sanctum-dir",
            str(sdir),
        ],
    )
    assert res_strict.exit_code != 0
    data_bad = json.loads(res_strict.output)
    assert data_bad["overall"] == "FAIL"
    assert data_bad["scenarios"]["S1_RSA_broken"] == "FAIL"
