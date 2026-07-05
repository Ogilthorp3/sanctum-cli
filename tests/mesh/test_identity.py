"""Unit tests for mesh identity (mint / persist / sign / verify).

Every test injects a deterministic, non-crypto ``Signer`` fake — the real
ML-DSA signer lives in adapters and is integration-tested separately. The fake
mirrors the seam contract: ``sign`` needs the private key, ``verify`` needs
only the public key, and a keypair shares a token so signatures made by the
private half verify against the public half.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from sanctum_cli.mesh.identity import MeshIdentityStore

if TYPE_CHECKING:
    from pathlib import Path


class FakeSigner:
    """Deterministic stand-in for the ML-DSA signer.

    A keypair shares ``token``; a "signature" is ``sha256(token | message)``.
    ``verify`` recomputes from the *public* token, so a signature verifies iff
    the public key corresponds to the private key that signed it.
    """

    def __init__(self, token: str = "unit-test-token") -> None:
        self._token = token

    def generate(self) -> tuple[str, str]:
        return (f"fakepub:{self._token}", f"fakepriv:{self._token}")

    def sign(self, private_key: str, message: bytes) -> str:
        token = private_key.split(":", 1)[1]
        return "fakesig:" + hashlib.sha256(token.encode() + b"|" + message).hexdigest()

    def verify(self, public_key: str, message: bytes, signature: str) -> bool:
        token = public_key.split(":", 1)[1]
        expected = "fakesig:" + hashlib.sha256(token.encode() + b"|" + message).hexdigest()
        return signature == expected


class CountingFakeSigner(FakeSigner):
    """A fake whose ``generate`` mints a fresh keypair on every call.

    Used to prove ``ensure`` loads an existing identity instead of re-minting:
    if it re-minted, the pubkey would change between calls.
    """

    def __init__(self) -> None:
        super().__init__()
        self._n = 0

    def generate(self) -> tuple[str, str]:
        self._n += 1
        return (f"fakepub:token-{self._n}", f"fakepriv:token-{self._n}")


@pytest.fixture
def fake_signer() -> FakeSigner:
    return FakeSigner()


def test_sign_verify_roundtrip(fake_signer: FakeSigner, tmp_path: Path) -> None:
    ident = MeshIdentityStore(signer=fake_signer, path=tmp_path / "identity").ensure(label="haus-x")
    sig = ident.sign(b"champion-hash")
    assert ident.verify(b"champion-hash", sig, ident.pubkey) is True
    assert ident.verify(b"tampered", sig, ident.pubkey) is False


def test_verify_wrong_pubkey_is_false(fake_signer: FakeSigner, tmp_path: Path) -> None:
    ident = MeshIdentityStore(signer=fake_signer, path=tmp_path / "identity").ensure(label="haus-x")
    sig = ident.sign(b"champion-hash")
    # A signature is only valid under the matching public key.
    assert ident.verify(b"champion-hash", sig, "fakepub:someone-else") is False


def test_ensure_mints_and_populates_identity(fake_signer: FakeSigner, tmp_path: Path) -> None:
    ident = MeshIdentityStore(signer=fake_signer, path=tmp_path / "identity").ensure(label="haus-x")
    assert ident.pubkey == "fakepub:unit-test-token"
    assert ident.label == "haus-x"
    assert ident.created  # non-empty ISO timestamp
    assert ident.identity.pubkey == ident.pubkey


def test_ensure_persists_private_key_file_0600(fake_signer: FakeSigner, tmp_path: Path) -> None:
    store = MeshIdentityStore(signer=fake_signer, path=tmp_path / "identity")
    store.ensure(label="haus-x")
    key_file = store.identity_file
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600


def test_ensure_is_idempotent_does_not_remint(tmp_path: Path) -> None:
    # A signer that mints a *different* keypair each call — if ensure re-minted,
    # the second pubkey would differ. It must load the persisted one instead.
    store = MeshIdentityStore(signer=CountingFakeSigner(), path=tmp_path / "identity")
    first = store.ensure(label="haus-x")
    second = store.ensure(label="haus-x")
    assert first.pubkey == second.pubkey
    assert first.label == second.label == "haus-x"


def test_reload_from_disk_can_still_sign(fake_signer: FakeSigner, tmp_path: Path) -> None:
    path = tmp_path / "identity"
    MeshIdentityStore(signer=fake_signer, path=path).ensure(label="haus-x")
    # A fresh store over the same path loads the persisted private key.
    reloaded = MeshIdentityStore(signer=FakeSigner(), path=path).ensure(label="ignored-label")
    sig = reloaded.sign(b"champion-hash")
    assert reloaded.verify(b"champion-hash", sig, reloaded.pubkey) is True
    # The label came from disk (the mint-time label), not the second call.
    assert reloaded.label == "haus-x"


def test_private_key_never_in_repr(fake_signer: FakeSigner, tmp_path: Path) -> None:
    ident = MeshIdentityStore(signer=fake_signer, path=tmp_path / "identity").ensure(label="haus-x")
    private_key = fake_signer.generate()[1]  # the exact secret this identity holds
    text = repr(ident)
    assert private_key not in text  # the private key string must not leak
    assert "fakepriv" not in text  # nor any private-key marker
    assert "haus-x" in text  # the label is safe to show
