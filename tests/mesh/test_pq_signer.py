"""Tests for NIST FIPS 205 composite ``CompositeSlhDsaEd25519Signer``."""

from __future__ import annotations

import pytest

from sanctum_cli.mesh.adapters import CompositeSlhDsaEd25519Signer, slh_dsa_available


@pytest.mark.skipif(not slh_dsa_available(), reason="OpenSSL FIPS 205 SLH-DSA-SHA2-128s unavailable")
def test_composite_slh_dsa_ed25519_roundtrip_and_tamper_rejection() -> None:
    signer = CompositeSlhDsaEd25519Signer()
    pub, priv = signer.generate()
    assert pub.startswith("ed25519+slh-dsa-sha2-128s:")
    assert priv.startswith("ed25519+slh-dsa-sha2-128s:")

    msg = b"sanctum-mesh-champion-manifest-s4-proof"
    sig = signer.sign(priv, msg)
    assert signer.verify(pub, msg, sig) is True

    # Modified message fails closed.
    assert signer.verify(pub, msg + b"!", sig) is False

    # Tampered Ed25519 half fails closed even when SLH-DSA half is intact.
    prefix, ed_sig, slh_sig = sig.split(":")
    bad_ed = ("00" if not ed_sig.startswith("00") else "ff") + ed_sig[2:]
    assert signer.verify(pub, msg, f"{prefix}:{bad_ed}:{slh_sig}") is False

    # Tampered SLH-DSA half fails closed even when Ed25519 half is intact.
    bad_slh = ("00" if not slh_sig.startswith("00") else "ff") + slh_sig[2:]
    assert signer.verify(pub, msg, f"{prefix}:{ed_sig}:{bad_slh}") is False

    # Wrong key fails closed.
    pub2, _priv2 = signer.generate()
    assert signer.verify(pub2, msg, sig) is False

    # Malformed prefix / non-composite signature fails closed.
    assert signer.verify(pub, msg, "ed25519:deadbeef") is False
