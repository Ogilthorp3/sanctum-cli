"""sanctum onboard — network-gear gate: registration, skippability, cred-resolution,
and the hostile-cred-value boundary (Task 4).

This module is the cross-cutting verification layer over Tasks 1-3. It does NOT
re-prove the per-function YAML/probe mechanics (those live in
``test_onboard_network_gear.py`` / ``test_onboard_trifecta_mirror.py``); it locks
the three contracts a reviewer most wants pinned for a security-sensitive,
ADDITIVE onboard gate:

1. **The gate is registered AND dispatched AND skippable.** It is data
   (``RECIPE_GATES``) wired into the dispatch loop, and ``--yes`` short-circuits
   it before any detection/connect/write — a scripted run can never hang on the
   masked-password prompt, and the rest of onboarding still completes.

2. **``device_creds`` reads instance.yaml overrides** — the discovery-first tuple
   the gate persists/reads is honored end to end (override → ``Creds`` →
   ``keychain_service``), not a hardcoded brand constant.

3. **The hostile-cred-value boundary holds (CLAUDE.md "Own the escaping at the
   boundary; test the hostile input, not the happy path").** A captured admin
   password is a *caller-supplied string crossing into an external CLI* twice:
   the GUARANTEED Keychain tier (``security add-generic-password -w <value>``) and
   the best-effort 1P mirror (``op item ... credential=<value>``). The contract is
   argv-not-shell: the value MUST ride as ONE argv element so a password carrying
   ``;``, ``$``, backticks, a leading ``--flag`` lookalike, ``%``, a space, AND a
   non-ASCII char neither injects a shell command nor gets misread as a CLI flag.
   We drive the REAL ``_keychain_write`` / ``_op_write_item`` (cheap subprocess
   boundary — NOT mocked away per CLAUDE.md sub-rule 3) and mock ONLY
   ``subprocess.run`` (the real socket/process), asserting on the exact argv the
   boundary built. The expectation is derived from the boundary's own contract
   (the value is the element after ``-w`` / the ``credential=`` token), NOT from
   re-using the production string — a shared-assumption bug can't hide.

Nothing here touches a real Keychain, ``op`` binary, ``sops``, SSH, a live device,
or the host instance.yaml — every external call is a module-level seam.
"""

from __future__ import annotations

import getpass
import warnings
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sanctum_cli import recipes
from sanctum_cli.cli import app
from sanctum_cli.commands import net as net_cmd
from sanctum_cli.commands import onboard
from sanctum_cli.devices.base import Creds, NetContext

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

# A deliberately hostile admin password: shell metacharacters (``;``, ``$``,
# backticks), a CLI-flag lookalike (``--update`` collides with the real
# ``security`` flag), a literal ``%``, a space, AND a non-ASCII char. CLAUDE.md:
# never test the boundary with a benign value like "hunter2". Authored here, in
# the TEST, as a different source than any production constant.
HOSTILE_PASSWORD = "p; rm -rf $HOME `whoami` --update %25 café"


# ── Shared isolation: never touch the real Keychain / mirror in the gate body ──


@pytest.fixture(autouse=True)
def _no_live_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Onboard tests must never probe a real Firewalla bridge (firewalla-compat gate)."""
    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None)


# ── 1. Gate registered + dispatched + skippable ──────────────────────


def test_gate_is_registered_data_referencing_a_real_recipe() -> None:
    """``network-gear`` is listed in RECIPE_GATES['family']; every gate maps a real recipe."""
    assert "network-gear" in onboard.RECIPE_GATES["family"]
    # Gates only reference recipes that actually exist (manifest contract: a gate
    # keyed on a phantom recipe would silently never run).
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)


def test_gate_is_wired_into_the_dispatch_loop() -> None:
    """The 'network-gear' branch is actually dispatched — registration is not enough.

    A gate listed in RECIPE_GATES but with no matching branch in the dispatch loop
    would be dead data. Assert the source wires the listed name to
    ``_run_network_gear`` (the contract between the data table and the dispatcher),
    so 'registered' genuinely means 'runs'.
    """
    import inspect

    src = inspect.getsource(onboard.onboard_command)
    assert 'gate == "network-gear"' in src
    assert "_run_network_gear(yes=yes)" in src


def test_gate_runs_after_firewalla_compat_additive_ordering() -> None:
    """Additive: the new gate is appended AFTER the pre-existing firewalla gates."""
    gates = onboard.RECIPE_GATES["family"]
    assert gates.index("network-gear") > gates.index("firewalla-compat")
    assert gates.index("network-gear") > gates.index("firewalla-pairing")


def _invoke_family_onboard_yes() -> tuple[int, str]:
    """``onboard --recipe family --yes`` with the backup/cloud/canary primitives mocked."""
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])
    return result.exit_code, " ".join(result.stdout.split())


def test_gate_skippable_under_yes_no_detect_no_connect_no_write(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--yes`` SKIPS the gate before any detection/connect/write — and onboarding
    still completes. A scripted run must never hang on the masked-password prompt
    nor mutate the Keychain unattended.
    """
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    counters = {"detect": 0, "store": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.detect_network_gear",
        lambda net: counters.__setitem__("detect", counters["detect"] + 1) or [],
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda **k: counters.__setitem__("store", counters["store"] + 1),
    )

    code, out = _invoke_family_onboard_yes()
    assert code == 0, out
    assert "Network gear" in out  # the step header still prints
    assert "skipped" in out
    assert counters == {"detect": 0, "store": 0}  # short-circuited before any work
    assert "onboarding complete" in out  # the rest of onboarding still finishes


# ── 2. device_creds reads instance.yaml overrides ────────────────────


def _stub_instance(monkeypatch: pytest.MonkeyPatch, values: dict[str, object]) -> None:
    """Make ``net.config.instance_value`` answer from an in-memory dict (no file)."""
    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value",
        lambda key, default=None: values.get(key, default),
    )


def test_device_creds_honors_instance_yaml_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """An instance.yaml ``devices.<kind>.keychain.{service,account}`` override flows
    all the way into the resolved ``Creds`` — service AND account, not a brand
    constant. This is the resolution the pairing gate persists and later reads.
    """
    _stub_instance(
        monkeypatch,
        {
            "devices.hub.keychain.service": "my-router-admin",
            "devices.hub.keychain.account": "operator",
        },
    )
    creds = net_cmd.device_creds("hub", NetContext(gateway_ip="192.168.2.1", runner=None))
    assert isinstance(creds, Creds)
    assert creds.username == "operator"  # account override → login user
    assert creds.keychain_service == "my-router-admin"  # service override → carried
    assert creds.secret is None  # provider self-resolves the password from Keychain


def test_device_creds_falls_back_to_per_kind_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With NOTHING configured, the per-kind default tuple resolves (no regression).

    Stated old→new explicitly (Bert is dyslexic): unset hub → (bell-hub-admin,
    admin); unset orbi → (orbi-admin, admin).
    """
    _stub_instance(monkeypatch, {})
    hub = net_cmd.device_creds("hub", NetContext(gateway_ip="192.168.2.1", runner=None))
    orbi = net_cmd.device_creds("orbi", NetContext(gateway_ip="192.168.1.1", runner=None))
    assert (hub.keychain_service, hub.username) == ("bell-hub-admin", "admin")
    assert (orbi.keychain_service, orbi.username) == ("orbi-admin", "admin")


# ── 3. Hostile-cred-value boundary: argv-not-shell ───────────────────


def test_keychain_write_passes_hostile_value_as_single_argv_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUARANTEED tier: a hostile password rides as ONE argv element after ``-w``.

    Drives the REAL ``_keychain_write`` (cheap subprocess boundary — exercised for
    real per CLAUDE.md) with ONLY ``subprocess.run`` mocked. The contract: no shell
    (``security`` is invoked argv-style, so ``;``/``$``/backticks cannot inject) and
    the value cannot be misread as a flag (it is the element immediately after
    ``-w``; even a value of ``--update`` is consumed as the password, not a flag).
    The expectation is derived from the boundary's own shape (``-w`` then the
    value), not by re-reading the production string.
    """
    captured: dict[str, Any] = {}

    class _Ok:
        returncode = 0
        stderr = ""

    def fake_run(argv: list[str], *a: Any, **k: Any) -> _Ok:
        captured["argv"] = argv
        # argv-style invocation: shell=True would be the footgun this guards.
        assert k.get("shell", False) is False
        return _Ok()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(onboard.shutil, "which", lambda _b: "/usr/bin/security")

    onboard._keychain_write(account="admin", service="bell-hub-admin", value=HOSTILE_PASSWORD)

    argv = captured["argv"]
    # The value is exactly the single element following ``-w`` — verbatim, unsplit.
    w_idx = argv.index("-w")
    assert argv[w_idx + 1] == HOSTILE_PASSWORD
    # And it appears EXACTLY once as its own element (never split on spaces/metachars).
    assert argv.count(HOSTILE_PASSWORD) == 1
    # The shell metacharacters are NOT expanded into separate argv tokens.
    assert "rm" not in argv
    assert "$HOME" not in argv  # the literal token never becomes its own argv element


def test_op_mirror_passes_hostile_value_verbatim_in_credential_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The best-effort 1P tier: the hostile value rides verbatim in ``credential=<value>``.

    Drives the REAL ``_op_write_item`` with ONLY ``subprocess.run`` mocked (the
    ``op read`` existence probe returns 'absent' → the create path). The captured
    value is bound INSIDE the ``credential=`` token, so a value that itself
    contains ``--vault Evil`` cannot be reinterpreted as separate ``op`` flags.
    """
    calls: list[list[str]] = []

    class _Rec:
        def __init__(self, rc: int) -> None:
            self.returncode = rc
            self.stderr = ""
            self.stdout = ""

    def fake_run(argv: list[str], *a: Any, **k: Any) -> _Rec:
        calls.append(argv)
        assert k.get("shell", False) is False
        if argv[1] == "read":  # existence probe → report absent so we hit `create`
            return _Rec(1)
        return _Rec(0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(onboard.shutil, "which", lambda _b: "/usr/bin/op")

    onboard._op_write_item(title="Sanctum - Bell Hub Admin", value=HOSTILE_PASSWORD)

    create = calls[-1]
    assert create[1:3] == ["item", "create"]  # `op item create ...`
    cred_tokens = [a for a in create if a.startswith("credential=")]
    # Exactly one ``credential=`` token, carrying the value verbatim (single element).
    assert cred_tokens == [f"credential={HOSTILE_PASSWORD}"]
    # The hostile ``--vault Evil`` inside the password did NOT become its own flag:
    # the only ``--vault`` is the real one we set to the Sanctum vault.
    assert create.count("--vault=Sanctum") == 1
    assert "Evil" not in create


def test_store_device_secret_threads_hostile_value_through_both_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: ``store_device_secret`` hands the hostile value, UNMANGLED, to the
    guaranteed Keychain tier first, then the best-effort mirror — both seams see the
    exact bytes (no truncation at a space, no shell-expansion of metacharacters).
    """
    seen: dict[str, str] = {}

    def fake_keychain_write(*, account: str, service: str, value: str) -> None:
        seen["keychain"] = value

    def fake_mirror(*, service: str, account: str, secret: str) -> None:
        seen["mirror"] = secret

    monkeypatch.setattr("sanctum_cli.commands.onboard._keychain_write", fake_keychain_write)
    monkeypatch.setattr("sanctum_cli.commands.onboard._mirror_to_trifecta", fake_mirror)

    onboard.store_device_secret(
        service="bell-hub-admin", account="admin", secret=HOSTILE_PASSWORD
    )
    assert seen["keychain"] == HOSTILE_PASSWORD
    assert seen["mirror"] == HOSTILE_PASSWORD


def test_paired_gate_writes_hostile_password_verbatim_to_keychain_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full pairing gate with a hostile password: the value reaches the keychain seam
    verbatim and is NEVER echoed to stdout (masked prompt), and the devices block
    lands. Exercises the whole interactive gate, not just the leaf seam.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    # A detected hub whose connect() probe succeeds (read-only fake).
    class _FakeHub:
        kind = "hub"
        brand = "sagemcom"

        def connect(self, creds: Creds | None) -> None:
            self.connected_with = creds

        def disconnect(self) -> None:
            return None

    hub = _FakeHub()
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.detect_network_gear", lambda net: [("hub", hub)]
    )

    stored: dict[str, str] = {}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda *, service, account, secret: stored.update(
            service=service, account=account, secret=secret
        ),
    )

    import yaml

    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
        patch("sanctum_cli.commands.onboard._run_identity_setup"),
        patch("sanctum_cli.commands.onboard._run_family_setup"),
        patch("sanctum_cli.commands.onboard._run_firewalla_pairing"),
        patch("sanctum_cli.commands.onboard._run_ai_providers"),
        # Prompt.ask(password=True) routes to getpass, which warns on a non-TTY
        # CliRunner; pyproject filterwarnings=error would crash the prompt. The
        # warning is a test-environment artifact (a real TTY never fires it), and
        # masking the password IS the security-correct default.
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore", getpass.GetPassWarning)
        result = runner.invoke(
            app,
            ["onboard", "--recipe", "family"],
            input="\n\n"  # proceed? / run the real backup now? (defaults)
            "y\n"  # pair the detected hub
            f"{HOSTILE_PASSWORD}\n",  # the hostile admin password
        )
    out = " ".join(result.stdout.split())
    assert result.exit_code == 0, out
    # The hostile password reached the keychain seam verbatim (the load-bearing tier).
    assert stored["secret"] == HOSTILE_PASSWORD
    assert stored["service"] == "bell-hub-admin"
    assert stored["account"] == "admin"
    # It was masked — the secret never appears in onboarding output.
    assert HOSTILE_PASSWORD not in result.stdout
    assert "rm -rf" not in result.stdout
    # The devices.hub reference block persisted (genuine probe success).
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["devices"]["hub"]["brand"] == "sagemcom"
    assert data["devices"]["hub"]["keychain"]["service"] == "bell-hub-admin"
