"""Unit tests for the adopt pipeline (Task 5).

``adopt`` runs five ordered gates and short-circuits on the FIRST failure:

    hash -> signature -> eval -> sandbox -> promote

Every external dependency is an injected seam driven here by a deterministic
fake with a call counter, so each test can prove both the returned
:class:`~sanctum_cli.mesh.types.Verdict` *and* that later stages were never
reached. The load-bearing invariant: **the local champion is never replaced on
any failure** — ``promote`` is called only when every gate passes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sanctum_cli.mesh.types import ArtifactKind, ArtifactRef, ChampionManifest
from sanctum_cli.mesh.verify import (
    SandboxResult,
    adopt,
    beats_baseline,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_BASELINE = 0.881


def _ref(
    content_hash: str = "sha256:champ",
    producer: str = "mldsa:PRODUCER",
) -> ArtifactRef:
    """A minimal well-formed discovery hit for the pipeline to chew on."""
    manifest = ChampionManifest(
        content_hash=content_hash,
        kind=ArtifactKind.LORA_ADAPTER,
        base_model="qwen3.6-35b-a3b-4bit",
        eval_scores={"tiered": 0.91},
        size_bytes=42_000_000,
        producer_pubkey=producer,
        signature="sig:XYZ",
    )
    return ArtifactRef(content_hash=content_hash, seeders=["100.64.0.7"], manifest=manifest)


class FakeVerifier:
    """A ``ManifestVerifier`` fake: the two integrity gates return canned bools.

    Separate call counters let a test prove the signature gate is skipped when
    the hash gate already failed.
    """

    def __init__(self, *, hash_ok: bool = True, signature_ok: bool = True) -> None:
        self._hash_ok = hash_ok
        self._signature_ok = signature_ok
        self.hash_calls = 0
        self.signature_calls = 0

    def verify_hash(self, ref: ArtifactRef) -> bool:
        self.hash_calls += 1
        return self._hash_ok

    def verify_signature(self, ref: ArtifactRef) -> bool:
        self.signature_calls += 1
        return self._signature_ok


class FakeEvalGate:
    """An ``EvalGate`` fake returning a fixed score; counts scoring calls."""

    def __init__(self, score: float) -> None:
        self._score = score
        self.calls = 0

    def score(self, ref: ArtifactRef) -> float:
        self.calls += 1
        return self._score


class FakeSandbox:
    """A ``Sandbox`` fake returning a canned :class:`SandboxResult`."""

    def __init__(
        self,
        *,
        ok: bool = True,
        egress_attempted: bool = False,
        notes: str = "",
    ) -> None:
        self._result = SandboxResult(ok=ok, egress_attempted=egress_attempted, notes=notes)
        self.calls = 0

    def probe(self, ref: ArtifactRef) -> SandboxResult:
        self.calls += 1
        return self._result


class PromoteRecorder:
    """Records every artifact handed to ``promote`` so tests can assert it fired
    exactly once with the right ref (or, on reject, never)."""

    def __init__(self) -> None:
        self.calls: list[ArtifactRef] = []

    def __call__(self, ref: ArtifactRef) -> None:
        self.calls.append(ref)


# ─── stage 1: content hash ───────────────────────────────────────────────


def test_adopt_rejects_on_hash_mismatch_and_short_circuits() -> None:
    verifier = FakeVerifier(hash_ok=False)
    gate = FakeEvalGate(0.99)
    sandbox = FakeSandbox()
    promote = PromoteRecorder()

    verdict = adopt(
        _ref(),
        verify_manifest=verifier,
        eval_gate=gate,
        sandbox=sandbox,
        promote=promote,
        baseline=_BASELINE,
    )

    assert verdict.promoted is False
    assert verdict.stage == "hash"
    # The hash gate failed, so nothing downstream ran.
    assert verifier.signature_calls == 0
    assert gate.calls == 0
    assert sandbox.calls == 0
    assert promote.calls == []


# ─── stage 2: signature ──────────────────────────────────────────────────


def test_adopt_rejects_on_bad_signature_after_hash_passes() -> None:
    verifier = FakeVerifier(hash_ok=True, signature_ok=False)
    gate = FakeEvalGate(0.99)
    sandbox = FakeSandbox()
    promote = PromoteRecorder()

    verdict = adopt(
        _ref(),
        verify_manifest=verifier,
        eval_gate=gate,
        sandbox=sandbox,
        promote=promote,
        baseline=_BASELINE,
    )

    assert verdict.promoted is False
    assert verdict.stage == "signature"
    assert verifier.hash_calls == 1
    assert verifier.signature_calls == 1
    assert gate.calls == 0
    assert sandbox.calls == 0
    assert promote.calls == []


# ─── stage 3: eval gate ──────────────────────────────────────────────────


def test_adopt_rejects_on_eval_regression() -> None:
    # Champion scores below the local baseline -> current champion kept.
    gate = FakeEvalGate(0.870)
    sandbox = FakeSandbox()
    promote = PromoteRecorder()

    verdict = adopt(
        _ref(),
        verify_manifest=FakeVerifier(),
        eval_gate=gate,
        sandbox=sandbox,
        promote=promote,
        baseline=_BASELINE,
    )

    assert verdict.promoted is False
    assert verdict.stage == "eval"
    assert "baseline" in verdict.reason
    # A regression must not even reach the (expensive) sandbox.
    assert sandbox.calls == 0
    assert promote.calls == []


def test_adopt_accepts_eval_at_exact_baseline() -> None:
    # Meets-or-beats: an exact tie is NOT a regression and clears the eval gate.
    verdict = adopt(
        _ref(),
        verify_manifest=FakeVerifier(),
        eval_gate=FakeEvalGate(_BASELINE),
        sandbox=FakeSandbox(),
        promote=PromoteRecorder(),
        baseline=_BASELINE,
    )
    assert verdict.promoted is True


# ─── stage 4: sandbox ────────────────────────────────────────────────────


def test_adopt_rejects_and_flags_producer_on_sandbox_egress() -> None:
    sandbox = FakeSandbox(ok=True, egress_attempted=True, notes="dialed 8.8.8.8:53")
    promote = PromoteRecorder()

    verdict = adopt(
        _ref(producer="mldsa:SNEAKY"),
        verify_manifest=FakeVerifier(),
        eval_gate=FakeEvalGate(0.95),
        sandbox=sandbox,
        promote=promote,
        baseline=_BASELINE,
    )

    assert verdict.promoted is False
    assert verdict.stage == "sandbox"
    # "producer flagged": the offending pubkey is named in the reason.
    assert "mldsa:SNEAKY" in verdict.reason
    assert promote.calls == []


def test_adopt_rejects_on_sandbox_probe_failure() -> None:
    # Probe couldn't even complete (crash / load failure) -> reject, don't guess.
    sandbox = FakeSandbox(ok=False, egress_attempted=False, notes="adapter load crashed")
    promote = PromoteRecorder()

    verdict = adopt(
        _ref(),
        verify_manifest=FakeVerifier(),
        eval_gate=FakeEvalGate(0.95),
        sandbox=sandbox,
        promote=promote,
        baseline=_BASELINE,
    )

    assert verdict.promoted is False
    assert verdict.stage == "sandbox"
    assert promote.calls == []


# ─── stage 5: promote (all gates pass) ───────────────────────────────────


def test_adopt_promotes_when_all_gates_pass() -> None:
    verifier = FakeVerifier()
    gate = FakeEvalGate(0.897)
    sandbox = FakeSandbox()
    promote = PromoteRecorder()
    ref = _ref()

    verdict = adopt(
        ref,
        verify_manifest=verifier,
        eval_gate=gate,
        sandbox=sandbox,
        promote=promote,
        baseline=_BASELINE,
    )

    assert verdict.promoted is True
    assert verdict.stage == "promote"
    # promote_fn called exactly once, with the adopted artifact.
    assert promote.calls == [ref]
    # Every gate ran exactly once, in order.
    assert verifier.hash_calls == 1
    assert verifier.signature_calls == 1
    assert gate.calls == 1
    assert sandbox.calls == 1


# ─── invariant: never promote on ANY failure ─────────────────────────────


def _failing_configs() -> list[tuple[str, FakeVerifier, FakeEvalGate, FakeSandbox]]:
    """One config per failing stage, each expected to keep the local champion."""
    return [
        ("hash", FakeVerifier(hash_ok=False), FakeEvalGate(0.99), FakeSandbox()),
        ("signature", FakeVerifier(signature_ok=False), FakeEvalGate(0.99), FakeSandbox()),
        ("eval", FakeVerifier(), FakeEvalGate(0.10), FakeSandbox()),
        ("sandbox", FakeVerifier(), FakeEvalGate(0.99), FakeSandbox(egress_attempted=True)),
    ]


@pytest.mark.parametrize(
    ("stage", "verifier", "gate", "sandbox"),
    _failing_configs(),
    ids=[c[0] for c in _failing_configs()],
)
def test_adopt_never_promotes_on_any_failure(
    stage: str,
    verifier: FakeVerifier,
    gate: FakeEvalGate,
    sandbox: FakeSandbox,
) -> None:
    promote = PromoteRecorder()

    verdict = adopt(
        _ref(),
        verify_manifest=verifier,
        eval_gate=gate,
        sandbox=sandbox,
        promote=promote,
        baseline=_BASELINE,
    )

    assert verdict.promoted is False
    assert verdict.stage == stage
    assert promote.calls == []


# ─── beats_baseline helper (shared with seed, Task 6) ────────────────────


def test_beats_baseline_true_when_strictly_above() -> None:
    assert beats_baseline(0.90, _BASELINE) is True


def test_beats_baseline_true_when_equal() -> None:
    assert beats_baseline(_BASELINE, _BASELINE) is True


def test_beats_baseline_false_when_below() -> None:
    assert beats_baseline(0.87, _BASELINE) is False


def test_promote_is_a_plain_callable_seam() -> None:
    # Documents the seam shape: any ``Callable[[ArtifactRef], None]`` works.
    seen: list[str] = []
    promote: Callable[[ArtifactRef], None] = lambda ref: seen.append(ref.content_hash)  # noqa: E731

    adopt(
        _ref(content_hash="sha256:xyz"),
        verify_manifest=FakeVerifier(),
        eval_gate=FakeEvalGate(0.99),
        sandbox=FakeSandbox(),
        promote=promote,
        baseline=_BASELINE,
    )

    assert seen == ["sha256:xyz"]
