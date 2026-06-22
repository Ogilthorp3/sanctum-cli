"""sanctum onboard — composition test, all underlying ops mocked."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from sanctum_cli import recipes
from sanctum_cli.cli import app
from sanctum_cli.commands import onboard

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_live_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Onboard tests must never probe a real Firewalla bridge.

    Default stance: unreachable (None), i.e. the screen-time module is not
    paired. Gate tests override this with their own fake fetcher.
    """
    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None)


def test_onboard_data_block_internal_order_is_invariant(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterization: the 'Your Data' block keeps estimate → backup → canary order.

    This pins the one load-bearing intra-block dependency that the Apple-arc
    reorder (Task 2: tools before data — recipe gates ahead of the cloud/backup
    block) must NOT disturb: the pre-flight estimate precedes the backup runs,
    which precede the restore canary (the canary round-trips what the backup just
    wrote). Where the whole block sits in the arc may move; this internal order
    may not.
    """
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    calls: list[str] = []

    def _record(name: str) -> Callable[..., None]:
        def _fn(*_a: object, **_k: object) -> None:
            calls.append(name)

        return _fn

    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate", _record("estimate")),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run", _record("backup")),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup", _record("cloud")),
        patch("sanctum_cli.commands.onboard._run_canary", _record("canary")),
        patch("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    # estimate first, both backup_run calls next, canary last — invariant.
    assert calls == ["estimate", "backup", "backup", "canary"], calls


def test_onboard_with_existing_cloud_skips_setup_and_runs_backup(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate") as estimate,
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run") as run_,
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup") as setup,
        patch("sanctum_cli.commands.onboard._run_canary") as canary,
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    estimate.assert_called_once()
    setup.assert_not_called()  # cloud_backup already configured
    # backup_run called twice: once with dry_run=True, once with dry_run=False
    assert run_.call_count == 2
    canary.assert_called_once()


def test_onboard_runs_setup_when_cloud_unconfigured(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))

    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup") as setup,
        patch("sanctum_cli.commands.onboard._run_canary"),
        patch("sanctum_cli.commands.onboard.config.load") as load,
    ):
        # First load: no cloud_backup; second load: simulate it after setup.
        # We don't actually mutate state; just confirm setup was called.
        from sanctum_cli.config import CliConfig, Config, InstanceMetadata

        load.return_value = Config(instance=InstanceMetadata(name="t", slug="t"), cli=CliConfig())
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    setup.assert_called_once_with("r2", no_open=False)


def test_onboard_family_shows_photos_warning(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])
    assert result.exit_code == 0
    assert "iCloud" in result.stdout or "Photos" in result.stdout


def test_onboard_operator_skips_photos_warning(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "operator", "--yes"])
    assert result.exit_code == 0
    # The photos panel mentions iCloud — operator path should not.
    assert "Photos scope notice" not in result.stdout


# ── Firewalla compatibility gate (family recipe) ─────────────────────
#
# Expectations derived from the box firmware semantics already pinned in
# tests/test_screen_time_compat.py: router/dhcp = in-path (PASS), spoof =
# ARP enforcement (WARN + "keep Monitoring ON…" fix). The gate must skip —
# never block — when the optional screen-time module isn't paired, and run
# the assessment STRICT when the bridge answers.


def _bridge_info(
    model: str = "goldpro", mode: str = "router", ready: bool = True
) -> dict[str, Any]:
    return {
        "box": {"model": model, "modelDisplay": model.title(), "mode": mode},
        "capabilities": {"enforcement_ready": ready, "box_mode": mode},
    }


def _invoke_family_onboard() -> tuple[int, str]:
    """Run `onboard --recipe family --yes` with the backup primitives mocked.

    Returns (exit_code, whitespace-normalized stdout) — normalization makes
    phrase assertions immune to rich's 80-column word wrapping.
    """
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])
    return result.exit_code, " ".join(result.stdout.split())


def test_onboard_family_compat_skipped_when_bridge_absent(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bridge unreachable / no token → SKIPPED hint, onboarding continues."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    # autouse fixture already stubs the bridge as unreachable (None)
    code, out = _invoke_family_onboard()
    assert code == 0, out
    assert "Firewalla compatibility" in out
    assert "skipped" in out
    assert "screen-time module not paired yet" in out
    assert "run `sanctum screen-time compat` after pairing" in out
    assert "onboarding complete" in out  # the flow reached the end


def test_onboard_family_compat_all_pass(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-path Gold Pro with headroom → the step passes inside onboarding."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    def fake_fetch(path: str) -> dict[str, Any] | None:
        if path == "/info":
            return _bridge_info()
        if path.startswith("/policies"):
            return {"policies": [], "count": 25}
        raise AssertionError(f"unexpected fetch {path}")

    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", fake_fetch)
    code, out = _invoke_family_onboard()
    assert code == 0, out
    assert "Firewalla compatibility" in out
    assert "✓ compatible" in out
    assert "skipped — screen-time module not paired yet" not in out


def test_onboard_family_compat_warn_fails_step_in_strict_with_fix(
    full_instance_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spoof-mode box → strict promotes the WARN to a step failure, fix shown."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    # Hermetic: no devices.yaml on this host's real paths may leak in.
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(tmp_path / "missing-devices.yaml"))

    def fake_fetch(path: str) -> dict[str, Any] | None:
        if path == "/info":
            return _bridge_info(model="red", mode="spoof")
        if path.startswith("/policies"):
            return {"policies": [], "count": 10}
        if path.startswith("/host/"):
            return {"monitored": True}
        return None

    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", fake_fetch)
    code, out = _invoke_family_onboard()
    # The STEP fails loudly; the RUN still completes — the backup already
    # succeeded and screen-time is an optional module.
    assert code == 0, out
    assert "✗" in out
    assert "compatibility WARN (strict)" in out
    assert "keep Monitoring ON for every kid device in the Firewalla app" in out
    assert "onboarding complete" in out


def test_firewalla_compat_gate_listed_in_family_recipe() -> None:
    """The gate is recipe-listed data, not a buried conditional."""
    assert "firewalla-compat" in onboard.RECIPE_GATES["family"]
    # Gates may only reference recipes that actually exist.
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)


# ── Family setup interview (family recipe) ────────────────────────────
#
# The interview seeds the screen-time registry (devices.yaml): names, roles,
# and — for children — an optional smartphone number for bedtime courtesy
# notices via iMessage. Expectations on the member shape derive from the
# on-disk schema screen_time.py already reads (family.<slug>.role /
# .notify_imessage / .personal_devices), not from the interview code.


def _invoke_family_onboard_interactive(input_text: str) -> tuple[int, str]:
    """Run `onboard --recipe family` (no --yes), feeding stdin to the prompts.

    Callers' input must start with two newlines: they accept the "proceed?"
    and "run the real backup now?" confirms at their defaults. Stdout is
    whitespace-normalized against rich's 80-column wrapping.
    """
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
        # Firewalla pairing + the AI-providers chapter are separate interactive
        # gates (own tests); the family-interview tests mock them out so they
        # don't consume their stdin.
        patch("sanctum_cli.commands.onboard._run_firewalla_pairing"),
        patch("sanctum_cli.commands.onboard._run_ai_providers"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family"], input=input_text)
    return result.exit_code, " ".join(result.stdout.split())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(514) 555-0142", ("+15145550142", True)),  # 10-digit NA, formatted
        ("514.555.0142", ("+15145550142", True)),  # dots are formatting too
        ("+33 6 12 34 56 78", ("+33612345678", False)),  # explicit country code
        ("+1 (514) 555-0142", ("+15145550142", False)),  # leading + is trusted
        ("555-0142", None),  # 7 digits — ambiguous, re-prompt
        ("not a phone", None),
        ("+123", None),  # too short to be a real number
        ("", None),
    ],
)
def test_normalize_phone(raw: str, expected: tuple[str, bool] | None) -> None:
    """Formatting is stripped, + trusted, bare 10 digits flagged as assumed-NA."""
    assert onboard.normalize_phone(raw) == expected


def test_onboard_family_interview_writes_child_with_normalized_phone(
    full_instance_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh haus: interview → skeleton devices.yaml with a normalized +1 number."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    devices = tmp_path / "devices.yaml"
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))

    code, out = _invoke_family_onboard_interactive(
        "\n\n"  # proceed? / run the real backup now? (defaults)
        "Maya\n"  # member name
        "child\n"  # role
        "(514) 555-0142\n"  # loosely formatted NA number
        "y\n"  # confirm the +1 assumption
        "\n"  # no device MAC → done with devices
        "\n"  # empty name → done
    )
    assert code == 0, out
    assert "Family setup" in out
    data = yaml.safe_load(devices.read_text(encoding="utf-8"))
    assert data["family"]["maya"] == {
        "role": "child",
        "notify_imessage": "+15145550142",
        "personal_devices": [],
    }
    # Engine-loadable skeleton: the other top-level maps exist, empty.
    assert data["shared_devices"] == {}
    assert data["screens"] == {}


def test_onboard_family_interview_merge_never_clobbers_existing_member(
    full_instance_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing slug survives untouched; only the genuinely new member is added."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    devices = tmp_path / "devices.yaml"
    devices.write_text(
        "family:\n"
        "  maya:\n"
        "    role: child\n"
        "    enforce_personal: macpause\n"
        "    personal_devices:\n"
        "      - name: iPhone\n"
        '        mac: "AA:BB:CC:DD:EE:FF"\n'
        "shared_devices: {}\n"
        "screens: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "Maya\nchild\n\n"  # same slug, no phone — must be skipped, not merged
        "\n"  # no device MAC → done with Maya's devices
        "Theo\nparent\n"  # parents are never asked for a phone
        "\n"  # done
    )
    assert code == 0, out
    assert "already in devices.yaml" in out  # the skip note
    data = yaml.safe_load(devices.read_text(encoding="utf-8"))
    # Existing member untouched — enforce flag and device list both survive.
    assert data["family"]["maya"]["enforce_personal"] == "macpause"
    assert data["family"]["maya"]["personal_devices"] == [
        {"name": "iPhone", "mac": "AA:BB:CC:DD:EE:FF"}
    ]
    assert "notify_imessage" not in data["family"]["maya"]
    assert data["family"]["theo"] == {"role": "parent", "personal_devices": []}
    assert (tmp_path / "devices.yaml.bak").exists()  # backup before merge-write


def test_onboard_family_interview_yes_skips_without_touching_file(
    full_instance_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes must never hang on stdin nor write the registry."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    devices = tmp_path / "devices.yaml"
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))

    code, out = _invoke_family_onboard()  # --yes path, no stdin supplied
    assert code == 0, out
    assert "Family setup" in out
    assert "skipped — interactive step" in out
    assert "run `sanctum onboard` without --yes to set up the family" in out
    assert not devices.exists()


def test_onboard_family_interview_bad_phone_reprompts_then_accepts(
    full_instance_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7-digit junk re-prompts; a +CC number then lands verbatim, formatting stripped."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    devices = tmp_path / "devices.yaml"
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "Noa\nchild\n"
        "555-0142\n"  # too short — must re-prompt, never store
        "+33 6 12 34 56 78\n"  # then a valid international number
        "\n"  # no device MAC → done with devices
        "\n"
    )
    assert code == 0, out
    assert "couldn't parse that" in out
    data = yaml.safe_load(devices.read_text(encoding="utf-8"))
    assert data["family"]["noa"]["notify_imessage"] == "+33612345678"


def test_family_setup_gate_listed_before_firewalla_compat() -> None:
    """The interview is recipe-listed data and runs before the compat gate."""
    gates = onboard.RECIPE_GATES["family"]
    assert "family-setup" in gates
    assert gates.index("family-setup") < gates.index("firewalla-compat")


# ── Per-setup identity collection (beta portability) ──────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"),
        ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
        ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
        ("aabb.ccdd.eeff", "AA:BB:CC:DD:EE:FF"),
        ("aabbccddeeff", "AA:BB:CC:DD:EE:FF"),
        ("  AA:BB:CC:DD:EE:FF  ", "AA:BB:CC:DD:EE:FF"),
        ("AA:BB:CC:DD:EE", None),
        ("AA:BB:CC:DD:EE:FF:00", None),
        ("ZZ:BB:CC:DD:EE:FF", None),
        ("", None),
    ],
)
def test_normalize_mac(raw: str, expected: str | None) -> None:
    """Any common MAC spelling -> canonical upper colon form; junk -> None."""
    assert onboard.normalize_mac(raw) == expected


def test_parse_device_selection() -> None:
    """A picker selection string resolves to chosen device dicts, in order, deduped."""
    devs = [
        {"name": "iPhone", "mac": "A1"},
        {"name": "iPad", "mac": "B2"},
        {"name": "Mac", "mac": "C3"},
    ]
    assert onboard.parse_device_selection("", devs) == []
    assert onboard.parse_device_selection("2", devs) == [devs[1]]
    assert onboard.parse_device_selection("1,3", devs) == [devs[0], devs[2]]
    assert onboard.parse_device_selection("3 1", devs) == [devs[2], devs[0]]
    assert onboard.parse_device_selection("all", devs) == devs
    assert onboard.parse_device_selection("9", devs) == []
    assert onboard.parse_device_selection("2,2", devs) == [devs[1]]


def test_set_instance_identity_writes_owner_and_signal_preserving_other_blocks(
    tmp_path: Path,
) -> None:
    """Owner name + Signal target land under notifications; sibling blocks survive."""
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "instance:\n  name: X\n  slug: x\nservices:\n  proxyd:\n    port: 4040\n",
        encoding="utf-8",
    )
    onboard.set_instance_identity("Alice", "+15551234567", path=inst)
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["notifications"]["owner_name"] == "Alice"
    assert data["notifications"]["signal"]["target"] == "+15551234567"
    assert data["notifications"]["signal"]["enabled"] is True
    assert data["services"]["proxyd"]["port"] == 4040  # untouched
    assert data["instance"]["slug"] == "x"
    assert (tmp_path / "instance.yaml.bak").exists()


def test_onboard_family_interview_collects_child_device_mac(
    full_instance_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child's manually entered MAC lands in personal_devices, canonicalized."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    devices = tmp_path / "devices.yaml"
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))
    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "Maya\nchild\n"  # name, role
        "\n"  # no phone
        "aa:bb:cc:dd:ee:ff\n"  # one device MAC (bridge unreachable -> manual)
        "Maya iPhone\n"  # device label
        "\n"  # empty MAC -> done with devices
        "\n"  # empty name -> done with members
    )
    assert code == 0, out
    data = yaml.safe_load(devices.read_text(encoding="utf-8"))
    assert data["family"]["maya"]["personal_devices"] == [
        {"name": "Maya iPhone", "mac": "AA:BB:CC:DD:EE:FF"}
    ]
