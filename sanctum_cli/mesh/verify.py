"""The adopt pipeline — verify a peer's champion before it can replace yours.

:func:`adopt` is the trust boundary of the mesh: a discovered artifact only
becomes the local champion after it clears five gates, run in a fixed order and
short-circuited on the **first** failure::

    hash -> signature -> eval -> sandbox -> promote

* **hash** — the downloaded bytes reproduce the manifest's content hash (catches
  a tampered or truncated artifact);
* **signature** — the manifest is signed by its claimed producer (catches forged
  metadata / a stolen id);
* **eval** — the champion :func:`beats_baseline` on the local eval harness (a
  peer's champion that regresses ours is not worth adopting);
* **sandbox** — loading + probing the adapter in an air-gapped VM shows it is
  well-behaved and attempts **no egress** (catches an exfiltrating or malicious
  adapter, and flags its producer);
* **promote** — only now is ``promote`` invoked to swap in the new champion.

The single invariant: **the local champion is authoritative and is never
replaced on any failure** — ``promote`` runs only when every prior gate passes.
Every external dependency is an injected seam (a :class:`typing.Protocol` or a
plain callable), so the whole pipeline is unit-tested with fakes; the real
crypto / eval-harness / VM-sandbox adapters live in
``sanctum_cli.mesh.adapters``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sanctum_cli.mesh.types import Verdict

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanctum_cli.mesh.types import ArtifactRef

__all__ = [
    "EvalGate",
    "ManifestVerifier",
    "Sandbox",
    "SandboxResult",
    "adopt",
    "beats_baseline",
]


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of an air-gapped probe of a candidate adapter.

    ``ok`` is whether the probe *completed* (the adapter loaded and answered);
    ``egress_attempted`` is the security verdict — did the isolated run try to
    reach the network. A champion clears the sandbox gate only when it both
    completed and stayed silent (``ok and not egress_attempted``). ``notes`` is
    free-form detail for the audit trail (e.g. the blocked destination).
    """

    ok: bool
    egress_attempted: bool
    notes: str = ""


class ManifestVerifier(Protocol):
    """The content-integrity seam, split into its two attributable gates.

    :func:`sanctum_cli.mesh.artifact.verify_manifest` folds hash + signature into
    one bool; the pipeline needs to name *which* gate rejected, so the seam
    exposes them separately. The Task 8 adapter implements both against the
    downloaded artifact it closes over (hash via
    :func:`~sanctum_cli.mesh.artifact.content_hash`, signature via the ML-DSA
    verify), while unit tests inject a fake.
    """

    def verify_hash(self, ref: ArtifactRef) -> bool:
        """Return whether the downloaded bytes reproduce ``ref.content_hash``."""
        ...

    def verify_signature(self, ref: ArtifactRef) -> bool:
        """Return whether the manifest signature verifies under its producer key."""
        ...


class EvalGate(Protocol):
    """The eval seam: score a candidate on the local (tiered) harness.

    The comparison against the baseline lives in :func:`beats_baseline` so the
    same policy is shared with seeding (Task 6); the gate only produces the
    number. The real adapter runs the 109-case autoresearch eval on the adapter.
    """

    def score(self, ref: ArtifactRef) -> float:
        """Return the candidate's aggregate eval score."""
        ...


class Sandbox(Protocol):
    """The air-gap seam: load + probe the adapter under network isolation.

    The real adapter runs the candidate in the VM under ``unshare -n`` and
    watches for any egress attempt; the unit tests inject a fake result.
    """

    def probe(self, ref: ArtifactRef) -> SandboxResult:
        """Load + exercise the adapter in isolation and report what happened."""
        ...


def beats_baseline(score: float, baseline: float) -> bool:
    """Return whether ``score`` clears ``baseline`` (meets-or-beats).

    Adoption is a guard against *regression*, so an exact tie passes; only a
    score strictly below the baseline is rejected. Shared with :mod:`seed`
    (Task 6) so a haus applies one consistent bar to what it adopts and seeds.
    """
    return score >= baseline


def adopt(
    ref: ArtifactRef,
    *,
    verify_manifest: ManifestVerifier,
    eval_gate: EvalGate,
    sandbox: Sandbox,
    promote: Callable[[ArtifactRef], None],
    baseline: float,
) -> Verdict:
    """Run the hash -> signature -> eval -> sandbox -> promote pipeline.

    Executes the gates in order, returning a :class:`~sanctum_cli.mesh.types.Verdict`
    the moment one fails (later gates never run) and calling ``promote`` exactly
    once only if all four earlier gates pass. On any failure ``promoted`` is
    ``False`` and ``promote`` is never called — the local champion stays
    authoritative.
    """
    # Stage 1 — content hash: the bytes must reproduce the advertised id.
    if not verify_manifest.verify_hash(ref):
        return Verdict(
            promoted=False,
            reason=f"content hash mismatch for {ref.content_hash}",
            stage="hash",
        )

    # Stage 2 — signature: the manifest must be signed by its claimed producer.
    if not verify_manifest.verify_signature(ref):
        return Verdict(
            promoted=False,
            reason=f"signature failed for producer {ref.manifest.producer_pubkey}",
            stage="signature",
        )

    # Stage 3 — eval gate: a peer's champion must not regress ours.
    score = eval_gate.score(ref)
    if not beats_baseline(score, baseline):
        return Verdict(
            promoted=False,
            reason=f"eval regression: score {score:.4f} < baseline {baseline:.4f}",
            stage="eval",
        )

    # Stage 4 — air-gapped sandbox: it must complete AND attempt no egress.
    result = sandbox.probe(ref)
    if result.egress_attempted:
        detail = f"; {result.notes}" if result.notes else ""
        return Verdict(
            promoted=False,
            reason=f"sandbox flagged producer {ref.manifest.producer_pubkey}: "
            f"egress attempted{detail}",
            stage="sandbox",
        )
    if not result.ok:
        return Verdict(
            promoted=False,
            reason=f"sandbox probe failed: {result.notes or 'no details'}",
            stage="sandbox",
        )

    # Stage 5 — promote: every gate passed; swap in the new champion once.
    promote(ref)
    return Verdict(
        promoted=True,
        reason=f"promoted {ref.content_hash} (eval {score:.4f} >= baseline {baseline:.4f})",
        stage="promote",
    )
