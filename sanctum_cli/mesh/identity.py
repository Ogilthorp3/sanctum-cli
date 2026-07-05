"""Mesh identity — mint / persist / sign / verify.

Each haus mints a signing keypair when it joins the mesh; it signs every
champion it seeds, and peers verify those signatures before adopting anything.

The cryptographic primitive is an injected :class:`Signer` seam so the pure
store logic is unit-tested with a deterministic fake. The real ML-DSA-65 signer
lives in ``sanctum_cli.mesh.adapters`` and is integration-tested against the
sanctum PKI.

Seam convention: keys and signatures are opaque *strings* (transport- and
persistence-friendly, matching :class:`~sanctum_cli.mesh.types.MeshIdentity`);
the adapter handles any bytes<->str encoding at the real crypto boundary.
Messages are raw ``bytes``. **Private keys are never logged** — persisted 0600
and kept out of every ``repr``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sanctum_cli.mesh.types import MeshIdentity

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "LoadedIdentity",
    "MeshIdentityStore",
    "Signer",
]

_IDENTITY_FILE = "identity.json"


class Signer(Protocol):
    """Cryptographic seam: keypair generation + detached sign/verify.

    Public keys, private keys, and signatures are opaque strings; messages are
    raw bytes. ``verify`` takes an explicit public key so a peer's signature can
    be checked, not only one's own.
    """

    def generate(self) -> tuple[str, str]:
        """Return a fresh ``(public_key, private_key)`` pair."""
        ...

    def sign(self, private_key: str, message: bytes) -> str:
        """Return a detached signature over ``message`` using ``private_key``."""
        ...

    def verify(self, public_key: str, message: bytes, signature: str) -> bool:
        """Return whether ``signature`` is valid for ``message`` under ``public_key``."""
        ...


class LoadedIdentity:
    """A minted identity bound to its private key + signer.

    Exposes ``sign`` / ``verify`` / ``pubkey`` for the mesh pipeline without ever
    surfacing the private key: it is excluded from ``__slots__``-based ``repr``.
    """

    __slots__ = ("_identity", "_priv", "_signer")

    def __init__(self, identity: MeshIdentity, private_key: str, signer: Signer) -> None:
        self._identity = identity
        self._priv = private_key
        self._signer = signer

    @property
    def identity(self) -> MeshIdentity:
        """The public :class:`MeshIdentity` (pubkey, label, created)."""
        return self._identity

    @property
    def pubkey(self) -> str:
        return self._identity.pubkey

    @property
    def label(self) -> str:
        return self._identity.label

    @property
    def created(self) -> str:
        return self._identity.created

    def sign(self, message: bytes) -> str:
        """Sign ``message`` with this identity's private key."""
        return self._signer.sign(self._priv, message)

    def verify(self, message: bytes, signature: str, public_key: str) -> bool:
        """Verify ``signature`` over ``message`` under ``public_key`` (any peer's)."""
        return self._signer.verify(public_key, message, signature)

    def __repr__(self) -> str:
        # Private key deliberately omitted — never log key material.
        return f"LoadedIdentity(label={self._identity.label!r}, pubkey={self._identity.pubkey!r})"


def _default_dir() -> Path:
    return Path.home() / ".sanctum" / "mesh" / "identity"


class MeshIdentityStore:
    """Mint-if-absent, load-if-present persistent store for the mesh identity.

    ``path`` is the identity *directory* (default ``~/.sanctum/mesh/identity``);
    the keypair is persisted to ``identity.json`` within it at mode ``0600``.
    ``ensure`` is idempotent: the first call mints, subsequent calls load — the
    stored identity (and its mint-time label) is authoritative.
    """

    def __init__(self, signer: Signer, path: Path | None = None) -> None:
        self._signer = signer
        self._dir = path if path is not None else _default_dir()

    @property
    def identity_file(self) -> Path:
        """Path to the persisted keypair file."""
        return self._dir / _IDENTITY_FILE

    def ensure(self, label: str) -> LoadedIdentity:
        """Return the mesh identity, minting + persisting it if absent."""
        if self.identity_file.exists():
            return self._load()
        return self._mint(label)

    def _mint(self, label: str) -> LoadedIdentity:
        public_key, private_key = self._signer.generate()
        created = datetime.now(UTC).isoformat()
        record = {
            "pubkey": public_key,
            "priv": private_key,
            "label": label,
            "created": created,
        }
        self._write_0600(record)
        identity = MeshIdentity(pubkey=public_key, label=label, created=created)
        return LoadedIdentity(identity=identity, private_key=private_key, signer=self._signer)

    def _load(self) -> LoadedIdentity:
        raw: Mapping[str, str] = json.loads(self.identity_file.read_text(encoding="utf-8"))
        identity = MeshIdentity(
            pubkey=raw["pubkey"],
            label=raw["label"],
            created=raw["created"],
        )
        return LoadedIdentity(identity=identity, private_key=raw["priv"], signer=self._signer)

    def _write_0600(self, record: Mapping[str, str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(dict(record), indent=2)
        # Create with restrictive perms atomically so the private key is never
        # briefly world-readable (a chmod-after-write leaves a TOCTOU window).
        fd = os.open(self.identity_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        # Defensive: tighten perms even if the file pre-existed with looser mode.
        self.identity_file.chmod(0o600)
