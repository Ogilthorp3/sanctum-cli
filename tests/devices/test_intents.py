"""Layer-2 apple-like intents — :func:`single_nat` (built, NEVER fired here).

``single_nat`` composes a hub provider's ``SetBridgeMode=on`` change with the
:func:`~sanctum_cli.devices.rails.guarded_apply` rails and a real-site verify.
The single-NAT cutover briefly drops a household's internet and is *attended-only*
— so the intent defaults to ``apply=False`` (a dry-run that mutates nothing) and a
caller must pass ``apply=True`` to fire it. These tests prove both halves of that
contract against the in-memory :class:`FakeProvider`: the dry-run plan makes ZERO
``set`` calls, and the apply path routes through ``guarded_apply`` and honours its
verify verdict. No real gear, no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sanctum_cli.devices.base import Capability, CapabilityOp, OpResult, Snapshot
from sanctum_cli.devices.intents import BRIDGE_MODE_PATH, single_nat

if TYPE_CHECKING:
    from pathlib import Path


class FakeProvider:
    """Minimal in-memory hub mirroring the plan's conformance fake.

    Counts ``set`` calls so a dry-run can be asserted to mutate nothing.
    """

    kind = "hub"
    brand = "fake-hub"

    def __init__(self) -> None:
        self._v: dict[str, str] = {BRIDGE_MODE_PATH: "off"}
        self.set_calls = 0
        self.rollback_calls = 0

    @staticmethod
    def detect(net: object) -> float:
        return 1.0

    def connect(self, creds: object | None) -> None:
        return None

    def get(self, path: str) -> str:
        return self._v[path]

    def set(self, path: str, value: str) -> OpResult:
        self.set_calls += 1
        before = self._v.get(path)
        self._v[path] = value
        return OpResult(ok=True, detail="set", before=before, after=value)

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET, Capability.BRIDGE_MODE}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.BRIDGE_MODE:
            return CapabilityOp(path=BRIDGE_MODE_PATH, engaged="on")
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(brand=self.brand, taken_at="t", data=dict(self._v))

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        self._v = dict(snap.data)
        return OpResult(ok=True, detail="rolled back")


def test_single_nat_dry_run_makes_no_mutations() -> None:
    """Default (apply=False) returns a plan and fires ZERO set() calls."""
    p = FakeProvider()
    res = single_nat(p, force=False, apply=False)
    assert res.applied is False
    assert res.result is None
    # The plan must describe the bridge-mode flip + the verify step.
    plan_text = "\n".join(res.plan)
    assert BRIDGE_MODE_PATH in plan_text
    assert "on" in plan_text
    # Zero mutations on a dry run — this is the hard guardrail.
    assert p.set_calls == 0
    assert p.rollback_calls == 0
    assert p.get(BRIDGE_MODE_PATH) == "off"  # untouched


def test_single_nat_dry_run_keeps_mtu_note() -> None:
    """The plan output carries the Bell MTU-1492 caveat for the operator."""
    p = FakeProvider()
    res = single_nat(p, force=False, apply=False)
    plan_text = "\n".join(res.plan)
    assert "1492" in plan_text


def test_single_nat_apply_commits_on_verify_pass(tmp_path: Path) -> None:
    """apply=True + a passing verify routes through guarded_apply and commits."""
    p = FakeProvider()
    res = single_nat(
        p,
        force=True,
        apply=True,
        verify_fn=lambda: True,
        confirm=lambda _plan: True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is True
    assert p.get(BRIDGE_MODE_PATH) == "on"  # bridge mode flipped on
    assert p.set_calls == 1
    assert p.rollback_calls == 0


def test_single_nat_apply_rolls_back_on_verify_fail(tmp_path: Path) -> None:
    """apply=True + a failing verify auto-rolls-back to the snapshot."""
    p = FakeProvider()
    res = single_nat(
        p,
        force=True,
        apply=True,
        verify_fn=lambda: False,
        confirm=lambda _plan: True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is False
    assert p.get(BRIDGE_MODE_PATH) == "off"  # rolled back
    assert p.rollback_calls == 1


def test_single_nat_is_brand_agnostic_uses_provider_op(tmp_path: Path) -> None:
    """The intent mutates the provider's OWN bridge-mode path, not a hardcoded XPath.

    A non-Bell provider whose capability_op returns a different path/value must be
    driven through THAT path — proving the intent reaches bridge mode via the
    provider abstraction, not the module-level Bell constant (spec criterion #1).
    """

    class OrbiLikeProvider(FakeProvider):
        brand = "orbi-like"

        def __init__(self) -> None:
            super().__init__()
            self._v = {"router/wan/mode": "router"}

        def capability_op(self, capability: Capability) -> CapabilityOp | None:
            if capability is Capability.BRIDGE_MODE:
                return CapabilityOp(path="router/wan/mode", engaged="bridge")
            return None

    p = OrbiLikeProvider()
    # Plan reflects the brand's own vocabulary, NOT the Bell XPath.
    dry = single_nat(p, force=False, apply=False)
    plan_text = "\n".join(dry.plan)
    assert "router/wan/mode" in plan_text
    assert BRIDGE_MODE_PATH not in plan_text  # no Bell XPath leaks in
    # Apply drives the brand's path/value.
    res = single_nat(
        p,
        force=True,
        apply=True,
        verify_fn=lambda: True,
        confirm=lambda _plan: True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None and res.result.ok is True
    assert p.get("router/wan/mode") == "bridge"


def test_single_nat_unsupported_provider_raises_legibly() -> None:
    """A provider with no bridge-mode op fails legibly instead of mutating blindly."""
    from sanctum_cli.devices.base import DeviceError

    class NoBridgeProvider(FakeProvider):
        def capability_op(self, capability: Capability) -> CapabilityOp | None:
            return None

    p = NoBridgeProvider()
    with pytest.raises(DeviceError) as ei:
        single_nat(p, force=False, apply=False)
    assert "bridge mode" in str(ei.value)
    # And it must not have mutated anything.
    assert p.set_calls == 0


def test_single_nat_apply_default_verify_uses_real_site(monkeypatch, tmp_path: Path) -> None:
    """With no verify_fn the intent verifies via net.verify (real-site reachability)."""
    from sanctum_cli.net.types import Verdict

    calls = {"n": 0}

    def fake_verify(*, runner: object) -> tuple[Verdict, str]:
        calls["n"] += 1
        return Verdict.VERIFIED, "single-NAT confirmed"

    monkeypatch.setattr("sanctum_cli.devices.intents.verify.verify", fake_verify)
    p = FakeProvider()
    res = single_nat(
        p,
        force=True,
        apply=True,
        runner=lambda _args: "",
        confirm=lambda _plan: True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert calls["n"] == 1  # default verify was consulted
    assert res.result is not None
    assert res.result.ok is True
    assert p.get(BRIDGE_MODE_PATH) == "on"


# ── BUG 2: the pre-cutover baseline must DISENGAGE each leaf in ITS OWN value-
# space — a boolean true/false leaf → "false", never "on" ─────────────────────
#
# The standalone ``sanctum net single-nat --rollback`` reconstructs the pre-cutover
# baseline via :func:`disengaged_baseline_snapshot` (there is no in-process apply
# snapshot to restore). The old heuristic, ``"off" if engaged == "on" else "on"``,
# sent ``"on"`` to Bell's boolean ``AdvancedDMZ/Enable`` leaf (whose engaged value
# is ``"true"``, not ``"on"``) — and the SAH rejects ``"on"`` with
# ``XMO_INVALID_PARAMETER_TYPE_ERR``, so ``--rollback`` could not disable DMZ
# (2026-06-27). The DMZ op here carries ``engaged="true"`` — the REAL live Bell
# sentinel (``sagemcom._CAPABILITY_OPS``), NOT the on/off model the orchestration
# fakes share — so this test does not inherit the assumption that hid the bug.

DMZ_ENABLE_PATH = "Device/Services/BellNetworkCfg/AdvancedDMZ/Enable"


class _TrueFalseDmzProvider(FakeProvider):
    """A hub whose DMZ leaf uses the LIVE boolean sentinel 'true'/'false'."""

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET, Capability.BRIDGE_MODE, Capability.DMZ}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.DMZ:
            # Verified live on the F5697: the leaf reads 'true'/'false', NOT 'on'/'off'.
            return CapabilityOp(path=DMZ_ENABLE_PATH, engaged="true")
        if capability is Capability.BRIDGE_MODE:
            return CapabilityOp(path=BRIDGE_MODE_PATH, engaged="on")
        return None


def test_disengaged_value_inverts_within_the_leaf_value_space() -> None:
    """A boolean leaf disengages to 'false'; an on/off leaf to 'off' — each in its
    OWN value-space, never crossed (the type error that broke --rollback)."""
    from sanctum_cli.devices.intents import disengaged_value

    assert disengaged_value(CapabilityOp(path="x", engaged="true")) == "false"
    assert disengaged_value(CapabilityOp(path="x", engaged="false")) == "true"
    assert disengaged_value(CapabilityOp(path="x", engaged="on")) == "off"
    assert disengaged_value(CapabilityOp(path="x", engaged="off")) == "on"


def test_disengaged_baseline_snapshot_uses_false_for_boolean_dmz_leaf() -> None:
    """The reconstructed pre-cutover baseline disengages the BOOLEAN DMZ leaf to the
    lowercase STRING 'false' (not 'on'), and the on/off bridge leaf to 'off'."""
    from sanctum_cli.devices.intents import disengaged_baseline_snapshot

    snap = disengaged_baseline_snapshot(_TrueFalseDmzProvider())
    assert snap.data[DMZ_ENABLE_PATH] == "false"
    assert isinstance(snap.data[DMZ_ENABLE_PATH], str)
    assert snap.data[BRIDGE_MODE_PATH] == "off"  # on/off leaf still → "off"
