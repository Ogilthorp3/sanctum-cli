"""Unit tests for the real mesh adapters (Task 8).

These adapters sit behind the mesh's injected Protocol seams. Two kinds of test
live here:

* **Real crypto** — :class:`~sanctum_cli.mesh.adapters.Ed25519Signer` is tested
  with genuine keys (roundtrip sign/verify, tampered message, wrong key,
  malformed hex → ``False`` never a raise). No fake stands in for the primitive.
* **Glue** — every other adapter is pure wiring over an injected callable
  (``EvalRunner`` / ``VmRunner`` / ``record``) or the ``artifact`` helpers, so it
  is driven by a deterministic fake and asserted on dispatch, mapping, and
  fallbacks — never a live boundary.

The real runners (``mlx_eval_runner`` / ``vm_airgap_runner``) shell out; their
argv-building + output-parsing glue is unit-tested with a fake ``subprocess``
(hermetic). The live boundary itself is the ``@pytest.mark.integration`` e2e
drill's job, not ``make check``'s — see ``test_real_runner_smoke`` (deselected
by the ``-m 'not integration'`` in ``addopts``, not an unconditional skip).
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from sanctum_cli.errors import LocalError
from sanctum_cli.mesh import adapters, artifact
from sanctum_cli.mesh.adapters import (
    AutoresearchEvalGate,
    BoundManifestVerifier,
    Ed25519Signer,
    LocalArtifactStore,
    SandboxProbe,
    VmAirgapSandbox,
    make_promote,
)
from sanctum_cli.mesh.types import ArtifactKind, ArtifactRef, ChampionManifest

if TYPE_CHECKING:
    from pathlib import Path


# ─── shared fixtures / helpers ───────────────────────────────────────────

# A real-shape sha256 hex id (64 lowercase hex chars). path_for now rejects
# anything else, so helpers and refs default to a valid id.
_HEX = "ab" * 32
_SHA = "sha256:" + _HEX


def _manifest(
    *,
    content_hash: str = _SHA,
    producer: str = "ed25519:PRODUCER",
    signature: str = "sig:XYZ",
    base_model: str = "qwen3.6-35b-a3b-4bit",
    eval_scores: dict[str, float] | None = None,
) -> ChampionManifest:
    return ChampionManifest(
        content_hash=content_hash,
        kind=ArtifactKind.LORA_ADAPTER,
        base_model=base_model,
        eval_scores=eval_scores if eval_scores is not None else {"tiered": 0.9},
        size_bytes=1024,
        producer_pubkey=producer,
        signature=signature,
    )


def _ref(*, content_hash: str = _SHA, **kw: str) -> ArtifactRef:
    manifest = _manifest(content_hash=content_hash, **kw)
    return ArtifactRef(content_hash=content_hash, seeders=["100.64.0.7"], manifest=manifest)


class _FakeIdentity:
    """A :class:`~sanctum_cli.mesh.artifact.SigningIdentity` over a real signer."""

    def __init__(self, pubkey: str, private_key: str, signer: Ed25519Signer) -> None:
        self._pubkey = pubkey
        self._priv = private_key
        self._signer = signer

    @property
    def pubkey(self) -> str:
        return self._pubkey

    def sign(self, message: bytes) -> str:
        return self._signer.sign(self._priv, message)


class RecordingVerifyFn:
    """A ``VerifyFn`` fake: records its args and returns a canned bool."""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[tuple[str, bytes, str]] = []

    def __call__(self, public_key: str, message: bytes, signature: str) -> bool:
        self.calls.append((public_key, message, signature))
        return self.result


class FakeEvalRunner:
    """An ``EvalRunner`` fake returning canned scores; records call args."""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, adapter_path: Path, base_model: str) -> dict[str, float]:
        self.calls.append((adapter_path, base_model))
        return self._scores


class FakeVmRunner:
    """A ``VmRunner`` fake returning a canned probe; records the path it saw."""

    def __init__(self, probe: SandboxProbe) -> None:
        self._probe = probe
        self.calls: list[Path] = []

    def __call__(self, adapter_path: Path) -> SandboxProbe:
        self.calls.append(adapter_path)
        return self._probe


class RecordSpy:
    """Records every ref handed to the promote ``record`` hook."""

    def __init__(self) -> None:
        self.calls: list[ArtifactRef] = []

    def __call__(self, ref: ArtifactRef) -> None:
        self.calls.append(ref)


# ─── Ed25519Signer — REAL crypto ─────────────────────────────────────────


def test_generate_returns_two_hex_keys() -> None:
    pub, priv = Ed25519Signer().generate()
    # Raw ed25519 keys are 32 bytes -> 64 hex chars each.
    assert len(pub) == 64
    assert len(priv) == 64
    bytes.fromhex(pub)  # valid hex (would raise otherwise)
    bytes.fromhex(priv)
    assert pub != priv


def test_sign_verify_roundtrip_with_real_keys() -> None:
    signer = Ed25519Signer()
    pub, priv = signer.generate()
    sig = signer.sign(priv, b"champion-hash-bytes")
    assert signer.verify(pub, b"champion-hash-bytes", sig) is True


def test_verify_false_on_tampered_message() -> None:
    signer = Ed25519Signer()
    pub, priv = signer.generate()
    sig = signer.sign(priv, b"original")
    assert signer.verify(pub, b"tampered", sig) is False


def test_verify_false_under_wrong_key() -> None:
    signer = Ed25519Signer()
    _pub_a, priv_a = signer.generate()
    pub_b, _ = signer.generate()
    sig = signer.sign(priv_a, b"msg")
    assert signer.verify(pub_b, b"msg", sig) is False


def test_verify_false_on_malformed_pubkey_hex() -> None:
    # Non-hex input must be swallowed, not raised.
    assert Ed25519Signer().verify("nothex!!", b"msg", "aa") is False


def test_verify_false_on_malformed_signature_hex() -> None:
    signer = Ed25519Signer()
    pub, _ = signer.generate()
    assert signer.verify(pub, b"msg", "zzzz") is False


def test_verify_false_on_wrong_length_key() -> None:
    signer = Ed25519Signer()
    _pub, priv = signer.generate()
    sig = signer.sign(priv, b"msg")
    # Valid hex but not 32 bytes -> ValueError inside cryptography -> False.
    assert signer.verify("aabb", b"msg", sig) is False


# ─── LocalArtifactStore ──────────────────────────────────────────────────


def test_path_for_strips_prefix_and_joins(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    hex_id = "de" * 32
    assert store.path_for(_ref(content_hash="sha256:" + hex_id)) == tmp_path / hex_id


def test_path_for_does_not_require_existence(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    hex_id = "ca" * 32
    path = store.path_for(_ref(content_hash="sha256:" + hex_id))
    assert path == tmp_path / hex_id
    assert not path.exists()


def test_path_for_raises_on_traversal_hash(tmp_path: Path) -> None:
    # An untrusted peer manifest must not be able to escape the store dir.
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(LocalError):
        store.path_for(_ref(content_hash="sha256:../../../etc/passwd"))


def test_path_for_raises_on_non_hex_hash(tmp_path: Path) -> None:
    # 64 chars but not all hex (g/z are not hex digits).
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(LocalError):
        store.path_for(_ref(content_hash="sha256:" + "z" * 64))


def test_path_for_raises_on_wrong_length_hex(tmp_path: Path) -> None:
    # Valid hex chars but the wrong length for a sha256 id.
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(LocalError):
        store.path_for(_ref(content_hash="sha256:" + "a" * 63))


# ─── BoundManifestVerifier ───────────────────────────────────────────────


def test_verify_hash_true_when_bytes_reproduce_id(tmp_path: Path) -> None:
    data = b"champion adapter weights"
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / digest).write_bytes(data)
    ref = _ref(content_hash="sha256:" + digest)
    verifier = BoundManifestVerifier(LocalArtifactStore(tmp_path), RecordingVerifyFn(True))
    assert verifier.verify_hash(ref) is True


def test_verify_hash_false_when_bytes_differ(tmp_path: Path) -> None:
    claimed = "0" * 64
    (tmp_path / claimed).write_bytes(b"not the advertised bytes")
    ref = _ref(content_hash="sha256:" + claimed)
    verifier = BoundManifestVerifier(LocalArtifactStore(tmp_path), RecordingVerifyFn(True))
    assert verifier.verify_hash(ref) is False


def test_verify_hash_false_when_artifact_missing(tmp_path: Path) -> None:
    # A missing artifact is a failed hash gate, not an exception.
    ref = _ref(content_hash="sha256:" + "1" * 64)
    verifier = BoundManifestVerifier(LocalArtifactStore(tmp_path), RecordingVerifyFn(True))
    assert verifier.verify_hash(ref) is False


def test_verify_hash_false_on_malformed_hash(tmp_path: Path) -> None:
    # A hostile/malformed content hash (path_for would raise) is a clean FAILED
    # hash gate, never a crash out of adopt().
    ref = _ref(content_hash="sha256:../../../etc/passwd")
    verifier = BoundManifestVerifier(LocalArtifactStore(tmp_path), RecordingVerifyFn(True))
    assert verifier.verify_hash(ref) is False


def test_verify_signature_delegates_hash_independent(tmp_path: Path) -> None:
    # No file on disk: the signature gate must be attributable independently of
    # the hash gate. It delegates to artifact.verify_signature with the
    # manifest's producer key + canonical signing message.
    fn = RecordingVerifyFn(True)
    ref = _ref(content_hash="sha256:whatever", producer="ed25519:PROD", signature="sig:S")
    verifier = BoundManifestVerifier(LocalArtifactStore(tmp_path), fn)

    assert verifier.verify_signature(ref) is True
    assert len(fn.calls) == 1
    public_key, message, signature = fn.calls[0]
    assert public_key == "ed25519:PROD"
    assert signature == "sig:S"
    assert message == artifact._signing_message(ref.manifest)


def test_verify_signature_returns_false_when_fn_rejects(tmp_path: Path) -> None:
    verifier = BoundManifestVerifier(LocalArtifactStore(tmp_path), RecordingVerifyFn(False))
    assert verifier.verify_signature(_ref()) is False


# ─── artifact.verify_signature helper (REAL crypto end-to-end) ───────────


def _real_signed_manifest(tmp_path: Path, *, base_model: str = "qwen") -> ChampionManifest:
    signer = Ed25519Signer()
    pub, priv = signer.generate()
    artifact_file = tmp_path / "adapter.safetensors"
    artifact_file.write_bytes(b"weights")
    return artifact.build_manifest(
        artifact_file,
        _FakeIdentity(pub, priv, signer),
        base_model=base_model,
        eval_scores={"tiered": 0.9},
    )


def test_artifact_verify_signature_true_for_genuine(tmp_path: Path) -> None:
    manifest = _real_signed_manifest(tmp_path)
    assert artifact.verify_signature(manifest, Ed25519Signer().verify) is True


def test_artifact_verify_signature_false_on_tampered_metadata(tmp_path: Path) -> None:
    manifest = _real_signed_manifest(tmp_path)
    tampered = replace(manifest, base_model="evil-base")
    assert artifact.verify_signature(tampered, Ed25519Signer().verify) is False


def test_artifact_verify_signature_false_on_wrong_producer(tmp_path: Path) -> None:
    manifest = _real_signed_manifest(tmp_path)
    other_pub, _ = Ed25519Signer().generate()
    forged = replace(manifest, producer_pubkey=other_pub)
    assert artifact.verify_signature(forged, Ed25519Signer().verify) is False


# ─── AutoresearchEvalGate (glue) ─────────────────────────────────────────


def test_eval_gate_returns_named_metric(tmp_path: Path) -> None:
    gate = AutoresearchEvalGate(LocalArtifactStore(tmp_path), FakeEvalRunner({"tiered": 0.91, "x": 0.1}))
    assert gate.score(_ref()) == 0.91


def test_eval_gate_falls_back_to_mean_when_metric_absent(tmp_path: Path) -> None:
    gate = AutoresearchEvalGate(LocalArtifactStore(tmp_path), FakeEvalRunner({"a": 0.8, "b": 0.6}))
    assert gate.score(_ref()) == pytest.approx(0.7)


def test_eval_gate_zero_on_empty_scores(tmp_path: Path) -> None:
    # A champion we cannot score does not clear any positive baseline.
    gate = AutoresearchEvalGate(LocalArtifactStore(tmp_path), FakeEvalRunner({}))
    assert gate.score(_ref()) == 0.0


def test_eval_gate_honours_custom_metric(tmp_path: Path) -> None:
    runner = FakeEvalRunner({"tiered": 0.9, "safety": 0.99})
    gate = AutoresearchEvalGate(LocalArtifactStore(tmp_path), runner, metric="safety")
    assert gate.score(_ref()) == 0.99


def test_eval_gate_passes_resolved_path_and_base_model(tmp_path: Path) -> None:
    runner = FakeEvalRunner({"tiered": 0.9})
    hex_id = "11" * 32
    ref = _ref(content_hash="sha256:" + hex_id, base_model="my-base")
    AutoresearchEvalGate(LocalArtifactStore(tmp_path), runner).score(ref)
    assert runner.calls == [(tmp_path / hex_id, "my-base")]


# ─── VmAirgapSandbox (glue) ──────────────────────────────────────────────


def test_sandbox_completed_no_egress_is_ok(tmp_path: Path) -> None:
    runner = FakeVmRunner(SandboxProbe(completed=True, egress_attempted=False, notes="clean"))
    result = VmAirgapSandbox(LocalArtifactStore(tmp_path), runner).probe(_ref())
    assert result.ok is True
    assert result.egress_attempted is False
    assert result.notes == "clean"


def test_sandbox_egress_surfaces(tmp_path: Path) -> None:
    runner = FakeVmRunner(SandboxProbe(completed=True, egress_attempted=True, notes="dialed 8.8.8.8"))
    result = VmAirgapSandbox(LocalArtifactStore(tmp_path), runner).probe(_ref())
    assert result.egress_attempted is True
    assert result.notes == "dialed 8.8.8.8"


def test_sandbox_not_completed_is_not_ok(tmp_path: Path) -> None:
    runner = FakeVmRunner(SandboxProbe(completed=False, egress_attempted=False, notes="load crashed"))
    result = VmAirgapSandbox(LocalArtifactStore(tmp_path), runner).probe(_ref())
    assert result.ok is False


def test_sandbox_passes_resolved_path(tmp_path: Path) -> None:
    runner = FakeVmRunner(SandboxProbe(completed=True, egress_attempted=False))
    hex_id = "22" * 32
    VmAirgapSandbox(LocalArtifactStore(tmp_path), runner).probe(_ref(content_hash="sha256:" + hex_id))
    assert runner.calls == [tmp_path / hex_id]


# ─── make_promote ────────────────────────────────────────────────────────


def test_promote_present_artifact_records_once(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"weights").hexdigest()
    (tmp_path / digest).write_bytes(b"weights")
    ref = _ref(content_hash="sha256:" + digest)
    spy = RecordSpy()
    make_promote(LocalArtifactStore(tmp_path), record=spy)(ref)
    assert spy.calls == [ref]


def test_promote_absent_artifact_raises_and_skips_record(tmp_path: Path) -> None:
    ref = _ref(content_hash="sha256:" + "0" * 64)
    spy = RecordSpy()
    promote = make_promote(LocalArtifactStore(tmp_path), record=spy)
    with pytest.raises(LocalError):
        promote(ref)
    assert spy.calls == []


def test_promote_without_record_hook_is_noop_when_present(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"w").hexdigest()
    (tmp_path / digest).write_bytes(b"w")
    ref = _ref(content_hash="sha256:" + digest)
    make_promote(LocalArtifactStore(tmp_path))(ref)  # no record, no raise


# ─── real runners: argv + parsing glue (fake subprocess, hermetic) ───────


def test_mlx_eval_runner_builds_argv_and_parses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"aggregate": 0.897}', stderr="")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    out = adapters.mlx_eval_runner(tmp_path / "adapter", "qwen-base")
    assert out == {"tiered": 0.897}
    assert str(tmp_path / "adapter") in captured["argv"]
    assert "qwen-base" in captured["argv"]


def test_mlx_eval_runner_passes_cases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"aggregate": 0.5}', stderr="")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    adapters.mlx_eval_runner(tmp_path / "a", "base", cases=["c1", "c2"])
    assert "c1,c2" in captured["argv"]


def test_mlx_eval_runner_raises_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="not json at all", stderr="")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    with pytest.raises(LocalError):
        adapters.mlx_eval_runner(tmp_path / "a", "base")


def test_mlx_eval_runner_raises_localerror_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A harness that exits non-zero must surface as an attributable LocalError,
    # NOT a raw CalledProcessError out of the eval gate. The fake honours
    # `check=` so a regression that re-adds check=True would raise
    # CalledProcessError and fail this test.
    def fake_run(
        argv: list[str], *, check: bool = False, **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        if check:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="boom: model not found")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom: model not found")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    with pytest.raises(LocalError) as excinfo:
        adapters.mlx_eval_runner(tmp_path / "a", "base")
    assert not isinstance(excinfo.value, subprocess.CalledProcessError)
    assert "boom: model not found" in str(excinfo.value)


def test_vm_airgap_runner_ships_then_probes_and_maps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        report = '{"completed": true, "egress_attempted": false, "notes": "clean"}'
        return subprocess.CompletedProcess(argv, 0, stdout=report, stderr="")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    probe = adapters.vm_airgap_runner(tmp_path / "adapter", host="vm-air")
    assert probe.completed is True
    assert probe.egress_attempted is False
    assert probe.notes == "clean"
    # Ship first (rsync), then probe under isolation (ssh).
    assert calls[0][0] == "rsync"
    assert calls[1][0] == "ssh"


@pytest.mark.integration
def test_real_runner_smoke(tmp_path: Path) -> None:  # pragma: no cover
    # Executable documentation of the live path. DESELECTED from `make check` by
    # the `-m 'not integration'` in addopts (not an unconditional skip) — it
    # needs the mlx-finetune eval harness + air-gapped VM. Run it explicitly
    # with `pytest -m integration` in the 2-box e2e drill.
    scores = adapters.mlx_eval_runner(tmp_path / "champion", "qwen3.6-35b-a3b-4bit")
    assert scores["tiered"] >= 0.0
