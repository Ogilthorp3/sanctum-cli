"""``sanctum screen-time compat`` — the pairing-time compatibility gate.

Why this exists: enforcement strength is NOT uniform across Firewalla models.
A Gold-class box in router mode blocks unconditionally; a Red/Blue (or a
Purple in "simple" mode) enforces via ARP spoofing, so a kid's device that
slips monitoring is silently unenforced. Capacity also differs (Red caps at
1,000 policy rules vs 3,000 elsewhere — values read from the box firmware's
platform/*/Platform.js on 2026-06-10), and the 2026-06-09 corpse-pile incident
showed what living near the cap does to the policy API.

The gate asserts compatibility at onboard time instead of letting a beta
parent discover the gap the hard way. Expectations here are derived from the
box firmware's own semantics (router/dhcp = in-path enforcement; spoof =
per-device `monitored` flag), not from the implementation under test.
"""

from __future__ import annotations

import pytest

from sanctum_cli.commands.screen_time import (
    BOX_POLICY_CAPACITY,
    DEFAULT_POLICY_CAPACITY,
    CompatCheck,
    assess_compat,
)
from sanctum_cli.errors import LocalError


def _info(model: str = "goldpro", mode: str = "router", ready: bool = True) -> dict:
    return {
        "box": {"model": model, "modelDisplay": model.title(), "mode": mode},
        "capabilities": {
            "enforcement_ready": ready,
            "box_mode": mode,
            "router_mode": mode == "router",
        },
    }


def _by_name(checks: list[CompatCheck]) -> dict[str, CompatCheck]:
    return {c.name: c for c in checks}


class TestAssessCompat:
    def test_gold_pro_router_mode_all_pass(self) -> None:
        checks = _by_name(assess_compat(_info(), policy_count=25, monitored=None))
        assert checks["box-link"].status == "PASS"
        assert checks["mode"].status == "PASS"
        assert checks["capacity"].status == "PASS"
        assert checks["model"].status == "PASS"
        # Router mode: monitoring coverage is structurally guaranteed — the
        # check must not appear (it would be noise, not information).
        assert "monitoring" not in checks

    def test_dhcp_mode_is_in_path_and_passes(self) -> None:
        checks = _by_name(assess_compat(_info(mode="dhcp"), 10, None))
        assert checks["mode"].status == "PASS"

    def test_spoof_mode_warns_with_explanation(self) -> None:
        checks = _by_name(assess_compat(_info(model="red", mode="spoof"), 10, None))
        assert checks["mode"].status == "WARN"
        assert "monitor" in checks["mode"].detail.lower()

    def test_enforcement_not_ready_fails(self) -> None:
        checks = _by_name(assess_compat(_info(ready=False), 10, None))
        assert checks["box-link"].status == "FAIL"

    def test_red_capacity_is_1000(self) -> None:
        assert BOX_POLICY_CAPACITY["red"] == 1000
        assert DEFAULT_POLICY_CAPACITY == 3000
        # 700/1000 = 70% on a Red → WARN territory.
        checks = _by_name(assess_compat(_info(model="red", mode="spoof"), 700, None))
        assert checks["capacity"].status == "WARN"
        # The same 700 rows on a Gold Pro (3000 cap) is comfortable.
        checks = _by_name(assess_compat(_info(), 700, None))
        assert checks["capacity"].status == "PASS"

    def test_capacity_near_cap_fails(self) -> None:
        checks = _by_name(assess_compat(_info(), 2850, None))
        assert checks["capacity"].status == "FAIL"

    def test_capacity_unreadable_warns(self) -> None:
        checks = _by_name(assess_compat(_info(), None, None))
        assert checks["capacity"].status == "WARN"

    def test_unknown_model_warns_untested(self) -> None:
        checks = _by_name(assess_compat(_info(model="quantum9000"), 10, None))
        assert checks["model"].status == "WARN"

    def test_spoof_monitored_coverage_full_passes(self) -> None:
        mon = {"AA:BB:CC:DD:EE:01": True, "AA:BB:CC:DD:EE:02": True}
        checks = _by_name(assess_compat(_info(model="red", mode="spoof"), 10, mon))
        assert checks["monitoring"].status == "PASS"

    def test_spoof_unmonitored_kid_device_fails_and_names_it(self) -> None:
        mon = {"AA:BB:CC:DD:EE:01": True, "AA:BB:CC:DD:EE:02": False}
        checks = _by_name(assess_compat(_info(model="red", mode="spoof"), 10, mon))
        assert checks["monitoring"].status == "FAIL"
        assert "AA:BB:CC:DD:EE:02" in checks["monitoring"].detail

    def test_spoof_unknown_monitoring_state_warns(self) -> None:
        mon = {"AA:BB:CC:DD:EE:01": None}
        checks = _by_name(assess_compat(_info(model="red", mode="spoof"), 10, mon))
        assert checks["monitoring"].status == "WARN"


class TestCompatCommand:
    def test_bridge_unreachable_is_local_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sanctum_cli.commands import screen_time as st_cmd

        monkeypatch.setattr(st_cmd, "_fetch_bridge_json", lambda path: None)
        with pytest.raises(LocalError, match="bridge"):
            st_cmd.compat_command()

    def test_all_pass_returns_quietly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sanctum_cli.commands import screen_time as st_cmd

        def fake_fetch(path: str) -> dict | None:
            if path == "/info":
                return _info()
            if path.startswith("/policies"):
                return {"policies": [], "count": 25}
            raise AssertionError(f"unexpected fetch {path}")

        monkeypatch.setattr(st_cmd, "_fetch_bridge_json", fake_fetch)
        st_cmd.compat_command()  # must not raise

    def test_fail_check_raises_local_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sanctum_cli.commands import screen_time as st_cmd

        def fake_fetch(path: str) -> dict | None:
            if path == "/info":
                return _info(ready=False)
            if path.startswith("/policies"):
                return {"policies": [], "count": 25}
            return None

        monkeypatch.setattr(st_cmd, "_fetch_bridge_json", fake_fetch)
        with pytest.raises(LocalError):
            st_cmd.compat_command()

    def test_strict_promotes_warn_to_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sanctum_cli.commands import screen_time as st_cmd

        def fake_fetch(path: str) -> dict | None:
            if path == "/info":
                return _info(model="red", mode="spoof")  # WARN: spoof mode
            if path.startswith("/policies"):
                return {"policies": [], "count": 10}
            if path.startswith("/host/"):
                return {"monitored": True}
            return None

        monkeypatch.setattr(st_cmd, "_fetch_bridge_json", fake_fetch)
        st_cmd.compat_command()  # WARNs tolerated by default
        with pytest.raises(LocalError):
            st_cmd.compat_command(strict=True)
