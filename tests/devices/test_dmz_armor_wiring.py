"""``single_nat_dmz`` armor wiring — the default installer seam.

T4 wires the concrete :class:`sanctum_cli.devices.armor.SinglenatArmorInstaller`
into the orchestrator's ``apply_armor`` stage. A test/CLI may still INJECT a
``armor=`` installer (the existing ``test_single_nat_dmz`` suite does), but a real
caller that omits it must get the real installer constructed for it — so the
cutover actually deploys the kit instead of requiring every call site to hand-
build the seam.

These tests assert the wiring at the *boundary*: that the orchestrator constructs
the real installer (with the supplied Firewalla/Mini coordinates) on the apply
path, and — critically — that the dry-run path constructs NOTHING (it must make
ZERO host contact, the overnight-build guardrail), via a recording double swapped
in for the installer class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.devices.base import (
    Capability,
    CapabilityOp,
    OpResult,
    Snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

DMZ_PATH = "Device/Services/BellNetworkCfg/AdvancedDMZ"


class FakeHub:
    kind = "hub"
    brand = "fake-bell-hub"

    def __init__(self) -> None:
        self._v: dict[str, str] = {DMZ_PATH: "off"}
        self.set_calls: list[tuple[str, str]] = []
        self.reboot_calls = 0
        self.rollback_calls = 0

    @staticmethod
    def detect(net: object) -> float:
        return 1.0

    def connect(self, creds: object | None) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def get(self, path: str) -> str | None:
        return self._v.get(path)

    def set(self, path: str, value: str) -> OpResult:
        before = self._v.get(path)
        self._v[path] = value
        self.set_calls.append((path, value))
        return OpResult(ok=True, detail=f"set {path}", before=before, after=value)

    def reboot(self) -> OpResult:
        self.reboot_calls += 1
        return OpResult(ok=True, detail="reboot issued")

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET, Capability.DMZ, Capability.REBOOT}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.DMZ:
            return CapabilityOp(path=DMZ_PATH, engaged="on")
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(brand=self.brand, taken_at="t", data=dict(self._v))

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        self._v = dict(snap.data)
        return OpResult(ok=True, detail="rolled back DMZ")


class FakeRunner:
    """Records tags; serves a PUBLIC downstream lease for the observe-lease read.

    The ``observe_lease`` stage now classifies the lease it reads (the FIX-4
    wiring): an empty/APIPA lease fails-closed before the later ``apply_armor``
    stage is reached. These wiring tests exercise the armor seam on the HAPPY
    path, so the runner must serve a public single-NAT lease (``203.0.113.7``)
    for ``lease_observe`` — otherwise the flip would (correctly) roll back at
    observe_lease and never reach the armor stage under test.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            return "203.0.113.7"
        return ""


def _all_pass_verifiers() -> dict[str, object]:
    from sanctum_cli.devices import flip

    return {stage: (lambda: True) for stage in flip.FLIP_STAGES}


def test_dry_run_constructs_no_armor_installer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """apply=False (no injected armor) constructs NO installer + contacts no host.

    The dry-run is the overnight-build guardrail: it must resolve the plan and
    make zero host contact. If it constructed the real installer it would still
    touch nothing (install() isn't called), but constructing it at all is needless
    on the describe-only path — so the wiring constructs lazily, only on apply.
    """
    constructed: list[object] = []

    import sanctum_cli.devices.armor as armor_mod

    real_cls = armor_mod.SinglenatArmorInstaller

    def _spy(*args: object, **kwargs: object) -> object:
        constructed.append((args, kwargs))
        return real_cls(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(armor_mod, "SinglenatArmorInstaller", _spy)

    from sanctum_cli.devices.intents import single_nat_dmz

    res = single_nat_dmz(FakeHub(), FakeRunner(), apply=False)
    assert res.applied is False
    assert constructed == []  # nothing built on the dry-run path


def test_apply_without_injected_armor_uses_default_installer(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """apply=True with no injected armor builds the real installer + runs install().

    The default installer is constructed from the Firewalla/Mini coordinates and
    its ``install()`` is the ``apply_armor`` stage. We swap in a recording double
    for the class so no real scp/ssh runs, and assert it was both constructed and
    installed (so the cutover genuinely deploys the kit through the default seam).
    """
    installs: list[int] = []

    class _RecordingInstaller:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def stage(self) -> OpResult:
            # The PRE-DMZ armor-staging stage (FIX-2): deploy the /32 hook while the
            # LAN is healthy. Recorded separately from install().
            return OpResult(ok=True, detail="armor staged")

        def install(self) -> OpResult:
            installs.append(1)
            return OpResult(ok=True, detail="armor installed")

    import sanctum_cli.devices.intents as intents_mod

    monkeypatch.setattr(intents_mod, "SinglenatArmorInstaller", _RecordingInstaller)

    res = intents_mod.single_nat_dmz(
        FakeHub(),
        FakeRunner(),
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),  # type: ignore[arg-type]
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is True
    assert installs == [1]  # the default installer's install() ran exactly once
