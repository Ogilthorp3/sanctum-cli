"""Unit tests for seeding a local champion to discovery (Task 6).

``seed`` is the mirror of ``adopt``: before a haus advertises its champion to
the mesh it must clear the SAME eval bar it would demand of a peer's — a
champion that does not beat the local baseline is never announced. When it does
clear, seed content-addresses + signs a manifest and announces it (paired with
this node's addr) via the discovery seam.

Every external dependency is an injected seam driven by a deterministic fake:
the ML-DSA crypto is the identity ``Signer`` fake (the real signer lives in
adapters), and discovery is a recording ``Announcer``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from sanctum_cli.mesh.artifact import verify_manifest
from sanctum_cli.mesh.identity import MeshIdentityStore
from sanctum_cli.mesh.seed import seed
from sanctum_cli.mesh.types import ArtifactKind

if TYPE_CHECKING:
    from pathlib import Path

    from sanctum_cli.mesh.identity import LoadedIdentity
    from sanctum_cli.mesh.types import ChampionManifest


class FakeSigner:
    """Deterministic non-crypto stand-in for the ML-DSA signer (matches the
    identity/artifact fakes): a keypair shares ``token`` and a "signature" is
    ``sha256(token | message)`` so ``verify`` recomputes from the public token."""

    def __init__(self, token: str = "seed-test-token") -> None:
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


class RecordingAnnouncer:
    """An ``Announcer`` fake: records every ``(manifest, addr)`` it is handed so a
    test can prove the champion was (or was NOT) advertised."""

    def __init__(self) -> None:
        self.calls: list[tuple[ChampionManifest, str]] = []

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        self.calls.append((manifest, addr))


_ADDR = "100.64.0.9"
_BASELINE = {"tiered": 0.881}


def _champion(tmp_path: Path) -> Path:
    d = tmp_path / "champion"
    d.mkdir()
    (d / "adapters.safetensors").write_bytes(b"CHAMPION-WEIGHTS")
    (d / "adapter_config.json").write_bytes(b'{"r": 32}')
    return d


def _mint(tmp_path: Path) -> tuple[FakeSigner, LoadedIdentity]:
    signer = FakeSigner()
    ident = MeshIdentityStore(signer=signer, path=tmp_path / "id").ensure(label="haus-x")
    return signer, ident


# ─── gate passes: build + sign + announce ────────────────────────────────


def test_seed_beating_baseline_builds_signs_and_announces(tmp_path: Path) -> None:
    champion = _champion(tmp_path)
    signer, ident = _mint(tmp_path)
    announcer = RecordingAnnouncer()

    manifest = seed(
        champion,
        identity=ident,
        discovery=announcer,
        addr=_ADDR,
        base_model="qwen3.6-35b-a3b-4bit",
        eval_scores={"tiered": 0.897},
        baseline_scores=_BASELINE,
    )

    assert manifest is not None
    # Announced exactly once, with the signed manifest + this node's addr.
    assert len(announcer.calls) == 1
    announced_manifest, announced_addr = announcer.calls[0]
    assert announced_manifest == manifest
    assert announced_addr == _ADDR
    # The announced manifest is signed and self-verifying over the artifact.
    assert verify_manifest(champion, manifest, signer.verify) is True


def test_seed_stamps_manifest_fields(tmp_path: Path) -> None:
    champion = _champion(tmp_path)
    _signer, ident = _mint(tmp_path)
    announcer = RecordingAnnouncer()

    manifest = seed(
        champion,
        identity=ident,
        discovery=announcer,
        addr=_ADDR,
        base_model="qwen3.6-35b-a3b-4bit",
        eval_scores={"tiered": 0.897},
        baseline_scores=_BASELINE,
        kind=ArtifactKind.LORA_ADAPTER,
    )

    assert manifest is not None
    assert manifest.base_model == "qwen3.6-35b-a3b-4bit"
    assert manifest.eval_scores == {"tiered": 0.897}
    assert manifest.kind is ArtifactKind.LORA_ADAPTER
    assert manifest.producer_pubkey == "fakepub:seed-test-token"
    assert manifest.size_bytes == len(b"CHAMPION-WEIGHTS") + len(b'{"r": 32}')


def test_seed_defaults_to_lora_kind(tmp_path: Path) -> None:
    champion = _champion(tmp_path)
    _signer, ident = _mint(tmp_path)
    announcer = RecordingAnnouncer()

    manifest = seed(
        champion,
        identity=ident,
        discovery=announcer,
        addr=_ADDR,
        base_model="qwen",
        eval_scores={"tiered": 0.9},
        baseline_scores=_BASELINE,
    )

    assert manifest is not None
    assert manifest.kind is ArtifactKind.LORA_ADAPTER


# ─── gate fails: NOT announced ───────────────────────────────────────────


def test_seed_below_baseline_is_not_announced(tmp_path: Path) -> None:
    champion = _champion(tmp_path)
    _signer, ident = _mint(tmp_path)
    announcer = RecordingAnnouncer()

    manifest = seed(
        champion,
        identity=ident,
        discovery=announcer,
        addr=_ADDR,
        base_model="qwen",
        eval_scores={"tiered": 0.870},  # below the 0.881 baseline
        baseline_scores=_BASELINE,
    )

    assert manifest is None
    # A regressing champion is NEVER announced.
    assert announcer.calls == []


def test_seed_at_exact_baseline_is_announced(tmp_path: Path) -> None:
    # Meets-or-beats: an exact tie is not a regression, so it seeds.
    champion = _champion(tmp_path)
    _signer, ident = _mint(tmp_path)
    announcer = RecordingAnnouncer()

    manifest = seed(
        champion,
        identity=ident,
        discovery=announcer,
        addr=_ADDR,
        base_model="qwen",
        eval_scores={"tiered": 0.881},
        baseline_scores=_BASELINE,
    )

    assert manifest is not None
    assert len(announcer.calls) == 1


# ─── multi-metric baseline ───────────────────────────────────────────────


def test_seed_beats_multi_metric_baseline(tmp_path: Path) -> None:
    champion = _champion(tmp_path)
    _signer, ident = _mint(tmp_path)
    announcer = RecordingAnnouncer()

    manifest = seed(
        champion,
        identity=ident,
        discovery=announcer,
        addr=_ADDR,
        base_model="qwen",
        eval_scores={"tiered": 0.95, "safety": 0.8},
        baseline_scores={"tiered": 0.881, "safety": 0.7},
    )

    assert manifest is not None
    assert len(announcer.calls) == 1


def test_seed_requires_all_baseline_metrics(tmp_path: Path) -> None:
    # A champion missing a metric the baseline demands cannot prove it clears
    # that bar -> not announced.
    champion = _champion(tmp_path)
    _signer, ident = _mint(tmp_path)
    announcer = RecordingAnnouncer()

    manifest = seed(
        champion,
        identity=ident,
        discovery=announcer,
        addr=_ADDR,
        base_model="qwen",
        eval_scores={"tiered": 0.95},  # missing "safety"
        baseline_scores={"tiered": 0.881, "safety": 0.7},
    )

    assert manifest is None
    assert announcer.calls == []


def test_seed_below_one_of_several_metrics_is_not_announced(tmp_path: Path) -> None:
    champion = _champion(tmp_path)
    _signer, ident = _mint(tmp_path)
    announcer = RecordingAnnouncer()

    manifest = seed(
        champion,
        identity=ident,
        discovery=announcer,
        addr=_ADDR,
        base_model="qwen",
        eval_scores={"tiered": 0.95, "safety": 0.6},  # safety regresses
        baseline_scores={"tiered": 0.881, "safety": 0.7},
    )

    assert manifest is None
    assert announcer.calls == []
