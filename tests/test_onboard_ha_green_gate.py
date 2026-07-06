"""sanctum onboard — HA Green gate: registration, skippability, honest-verify,
and the verified-pairing persistence (services block + 0600 token file).

The HA Green is a Bearer-(owner-)token REST appliance, so its onboard gate MIRRORS
``_run_firewalla_pairing`` (detect on the LAN → verify with the owner token → record
the verified pairing + token) rather than the Keychain-password network-gear gate.
These tests lock the contracts a reviewer most wants pinned:

1. The gate is registered (``RECIPE_GATES``/``_CHAPTER_GATES``), dispatched
   (``_run_gate``), and skippable (``--yes`` short-circuits before ANY probe/write).
2. HONEST-VERIFY: the gate records a pairing ONLY on a real ``api_running`` (GET
   /api/ → "API running.") — an unreachable / unverified Green persists NOTHING.
3. The persistence seam (``set_ha_green``) writes the ``services.ha_green``
   reference block AND the owner token to a mode-600 secrets file — never the token
   into instance.yaml.

Every device call is a module-level seam in ``sanctum_cli.devices.ha_green`` the
tests monkeypatch, so no live HA / Tailscale / socket is ever touched.
"""

from __future__ import annotations

import getpass
import inspect
import warnings
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from sanctum_cli import recipes
from sanctum_cli.cli import app
from sanctum_cli.commands import onboard
from sanctum_cli.devices import ha_green

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_live_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Onboard tests must never probe a real Firewalla bridge (firewalla-compat gate)."""
    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None)


@pytest.fixture(autouse=True)
def _no_live_ha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every HA Green seam to 'absent' so an un-stubbed test never hits the LAN."""
    monkeypatch.setattr(ha_green, "lan_reachable", lambda: False)
    monkeypatch.setattr(ha_green, "api_running", lambda *, url=None, token=None: False)
    monkeypatch.setattr(ha_green, "ha_version", lambda *, url=None, token=None: None)
    monkeypatch.setattr(
        ha_green, "tailscale_node_present", lambda name=ha_green._TAILNET_NODE: False
    )


# ── 1. Gate registered + dispatched + skippable ──────────────────────


def test_gate_is_registered_data_referencing_a_real_recipe() -> None:
    """``ha-green`` is listed in RECIPE_GATES['family']; every gate maps a real recipe."""
    assert "ha-green" in onboard.RECIPE_GATES["family"]
    assert "ha-green" in onboard._CHAPTER_GATES["Your Network"]
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)


def test_gate_is_wired_into_the_dispatch_loop() -> None:
    """The 'ha-green' branch is actually dispatched — registration is not enough."""
    src = inspect.getsource(onboard._run_gate)
    assert 'gate == "ha-green"' in src
    assert "_run_ha_green(yes=yes)" in src


def test_gate_runs_after_network_gear_additive_ordering() -> None:
    """Additive: the HA Green gate is appended AFTER the pre-existing network gates."""
    gates = onboard.RECIPE_GATES["family"]
    assert gates.index("ha-green") > gates.index("network-gear")
    assert gates.index("ha-green") > gates.index("firewalla-compat")


def test_gate_skipped_under_yes_no_probe_no_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--yes`` SKIPS the gate before any detection/probe/write."""
    counters = {"lan": 0, "persist": 0}
    monkeypatch.setattr(
        ha_green, "lan_reachable", lambda: counters.__setitem__("lan", counters["lan"] + 1) or True
    )
    monkeypatch.setattr(
        onboard,
        "_persist_ha_green",
        lambda **k: counters.__setitem__("persist", counters["persist"] + 1),
    )
    assert onboard._run_ha_green(yes=True) is False
    assert counters == {"lan": 0, "persist": 0}  # short-circuited before any work


# ── 2. HONEST-VERIFY: persist only on a real api_running ──────────────


def test_gate_not_on_lan_persists_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Green that isn't on the LAN → skipped, nothing recorded (no false 'detected')."""
    monkeypatch.setattr(ha_green, "lan_reachable", lambda: False)
    persisted: list[Any] = []
    monkeypatch.setattr(onboard, "_persist_ha_green", lambda **k: persisted.append(k))
    assert onboard._run_ha_green(yes=False) is False
    assert persisted == []


def test_gate_existing_token_verifies_records_without_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reachable + the on-disk token already verifies → record (token=None), no prompt."""
    monkeypatch.setattr(ha_green, "lan_reachable", lambda: True)
    monkeypatch.setattr(ha_green, "api_running", lambda *, url=None, token=None: True)
    monkeypatch.setattr(ha_green, "ha_version", lambda *, url=None, token=None: "2026.6.1")
    monkeypatch.setattr(
        ha_green, "tailscale_node_present", lambda name=ha_green._TAILNET_NODE: True
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(onboard, "_persist_ha_green", lambda *, token: captured.update(token=token))
    # No prompt must be reached (a stray prompt would hang on the closed stdin).
    monkeypatch.setattr(
        onboard.Confirm, "ask", staticmethod(lambda *a, **k: pytest.fail("should not prompt"))
    )
    assert onboard._run_ha_green(yes=False) is True
    assert captured == {"token": None}  # re-used the on-disk token; nothing re-written


def test_gate_prompts_and_records_only_after_token_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reachable but unverified → prompt for the owner token; record ONLY once it verifies."""
    monkeypatch.setattr(ha_green, "lan_reachable", lambda: True)
    # The on-disk token does NOT verify; the just-entered token DOES.
    monkeypatch.setattr(
        ha_green, "api_running", lambda *, url=None, token=None: token == "good-token"
    )
    monkeypatch.setattr(ha_green, "ha_version", lambda *, url=None, token=None: "2026.6.1")
    monkeypatch.setattr(
        ha_green, "tailscale_node_present", lambda name=ha_green._TAILNET_NODE: True
    )
    monkeypatch.setattr(onboard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(onboard.Prompt, "ask", staticmethod(lambda *a, **k: "good-token"))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(onboard, "_persist_ha_green", lambda *, token: captured.update(token=token))
    assert onboard._run_ha_green(yes=False) is True
    assert captured == {"token": "good-token"}  # the verified token is what gets recorded


def test_gate_rejected_token_persists_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable but every token attempt is rejected → not verified, nothing recorded."""
    monkeypatch.setattr(ha_green, "lan_reachable", lambda: True)
    monkeypatch.setattr(ha_green, "api_running", lambda *, url=None, token=None: False)
    monkeypatch.setattr(onboard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(onboard.Prompt, "ask", staticmethod(lambda *a, **k: "wrong-token"))
    persisted: list[Any] = []
    monkeypatch.setattr(onboard, "_persist_ha_green", lambda **k: persisted.append(k))
    assert onboard._run_ha_green(yes=False) is False
    assert persisted == []  # a false "paired" is worse than an honest "not paired"


def test_gate_declined_pairing_persists_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable but the operator declines the pairing prompt → nothing recorded."""
    monkeypatch.setattr(ha_green, "lan_reachable", lambda: True)
    monkeypatch.setattr(ha_green, "api_running", lambda *, url=None, token=None: False)
    monkeypatch.setattr(onboard.Confirm, "ask", staticmethod(lambda *a, **k: False))
    persisted: list[Any] = []
    monkeypatch.setattr(onboard, "_persist_ha_green", lambda **k: persisted.append(k))
    assert onboard._run_ha_green(yes=False) is False
    assert persisted == []


# ── 3. set_ha_green: services block + 0600 token file ────────────────


def test_set_ha_green_writes_services_block_and_token_file(tmp_path: Path) -> None:
    """The verified pairing lands in services.ha_green; the token in a mode-600 file."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    token_file = tmp_path / "secrets" / "ha-token"

    onboard.set_ha_green(
        token="owner-token-abc",
        host="10.0.0.3",
        port=8123,
        device_mac="20:F8:3B:02:3A:C8",
        tailnet_node="homeassistant",
        path=inst,
        token_file=token_file,
    )

    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    svc = data["services"]["ha_green"]
    assert svc == {
        "enabled": True,
        "host": "10.0.0.3",
        "port": 8123,
        "device_mac": "20:F8:3B:02:3A:C8",
        "tailnet_node": "homeassistant",
    }
    # The token landed in the secrets file (NOT instance.yaml), mode 600.
    assert "owner-token-abc" not in inst.read_text(encoding="utf-8")
    assert token_file.read_text(encoding="utf-8").strip() == "owner-token-abc"
    assert (token_file.stat().st_mode & 0o777) == 0o600


def test_set_ha_green_none_token_writes_block_only(tmp_path: Path) -> None:
    """A None token (the on-disk token already verified) writes the block, not a token file."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    token_file = tmp_path / "secrets" / "ha-token"

    onboard.set_ha_green(
        token=None,
        host="10.0.0.3",
        port=8123,
        device_mac="20:F8:3B:02:3A:C8",
        tailnet_node="homeassistant",
        path=inst,
        token_file=token_file,
    )
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["services"]["ha_green"]["enabled"] is True
    assert not token_file.exists()  # no token given → no token file touched


def test_set_ha_green_preserves_sibling_blocks(tmp_path: Path) -> None:
    """The atomic read-modify-write preserves every sibling (services + top-level) block."""
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "instance:\n  name: X\n  slug: x\n"
        "services:\n  firewalla_bridge:\n    enabled: true\n    port: 1984\n",
        encoding="utf-8",
    )
    onboard.set_ha_green(
        token=None,
        host="10.0.0.3",
        port=8123,
        device_mac="20:F8:3B:02:3A:C8",
        tailnet_node="homeassistant",
        path=inst,
        token_file=tmp_path / "ha-token",
    )
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["instance"]["name"] == "X"  # top-level sibling preserved
    assert data["services"]["firewalla_bridge"]["port"] == 1984  # service sibling preserved
    assert data["services"]["ha_green"]["host"] == "10.0.0.3"  # new block added


# ── 4. Full interactive gate (end to end through the CLI) ─────────────


def test_full_onboard_ha_green_gate_records_verified_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole interactive arc reaches the HA Green gate, verifies a typed owner
    token, and persists services.ha_green + the 0600 token file — masked in output.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    # Redirect the token file off the real ~/.sanctum/secrets/ha-token.
    token_file = tmp_path / "secrets" / "ha-token"
    monkeypatch.setattr(ha_green, "_HA_TOKEN_FILE", token_file)

    # Reachable + unverified until the typed token; tailnet joined.
    monkeypatch.setattr(ha_green, "lan_reachable", lambda: True)
    monkeypatch.setattr(
        ha_green, "api_running", lambda *, url=None, token=None: token == "OWNER-TOKEN-XYZ"
    )
    monkeypatch.setattr(ha_green, "ha_version", lambda *, url=None, token=None: "2026.6.1")
    monkeypatch.setattr(
        ha_green, "tailscale_node_present", lambda name=ha_green._TAILNET_NODE: True
    )

    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch(
            "sanctum_cli.commands.onboard._run_canary",
            return_value=onboard.CanaryOutcome.VERIFIED,
        ),
        # Mock every gate BEFORE ha-green so they don't consume this gate's stdin.
        patch("sanctum_cli.commands.onboard._run_identity_setup"),
        patch("sanctum_cli.commands.onboard._run_family_setup"),
        patch("sanctum_cli.commands.onboard._run_ai_providers"),
        patch("sanctum_cli.commands.onboard._run_firewalla_pairing"),
        patch("sanctum_cli.commands.onboard._run_network_gear"),
        # Haus-scan runs before ha-green and prompts for scan-consent; mock it so it
        # neither prompts nor runs a real arp/SSDP/httpx scan here.
        patch("sanctum_cli.commands.onboard._run_haus_scan"),
        # Network-resilience runs AFTER ha-green (own tests); mock it so a real
        # posture probe / DHCP flip / daemon install never runs here.
        patch("sanctum_cli.commands.onboard._run_network_resilience"),
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore", getpass.GetPassWarning)
        result = runner.invoke(
            app,
            ["onboard", "--recipe", "family"],
            input="\n\n"  # proceed? / run the real backup now? (defaults)
            "y\n"  # pair the HA Green now?
            "OWNER-TOKEN-XYZ\n",  # the owner token
        )

    out = " ".join(result.stdout.split())
    assert result.exit_code == 0, out
    # The verified pairing landed; the token is masked in output. With no HA_GREEN_URL /
    # HA_GREEN_MAC set, the recorded pairing uses the GENERIC default host and an EMPTY
    # device_mac — never one operator's LAN IP or MAC baked into a stranger's config.
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["services"]["ha_green"]["host"] == "homeassistant.local"
    assert data["services"]["ha_green"]["device_mac"] == ""
    assert token_file.read_text(encoding="utf-8").strip() == "OWNER-TOKEN-XYZ"
    assert (token_file.stat().st_mode & 0o777) == 0o600
    assert "OWNER-TOKEN-XYZ" not in result.stdout  # masked prompt
    assert "HA Green" in out  # the chapter header rendered
    assert "onboarding complete" in out  # the rest of onboarding still finished
