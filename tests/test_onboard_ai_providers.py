"""sanctum onboard — AI-provider chapter (``_run_ai_providers``, Task 1).

The chapter reuses the P4 cred-capture pattern (prompt → fail-closed Keychain →
best-effort trifecta mirror → health-probe → revoke-on-failure), generalized from
device admin secrets to AI-provider API keys:

  * Claude — offer subscription (default, ``via=proxy``, no Keychain write) OR an
    Anthropic API key (``via=direct``, Keychain ``anthropic-api-key``/``sanctum``).
  * Gemini — Google AI / Gemini API key (Keychain ``google-ai-api-key``/``sanctum``).

Military-grade fail-closed contract (mirrors ``_run_network_gear``): a provider is
declared "configured" ONLY when its health-probe genuinely succeeds. A REJECTED
API key REVOKES the just-written Keychain entry and persists NO ``via=direct``
config; a MISSING/not-logged-in ``claude`` CLI persists NOTHING and shows the
install-guidance panel. Nothing half-working is ever left behind.

Every test mocks the Keychain-write seam, the provider ``health()`` seam, and the
``claude``-CLI presence/login probe — NO live API call, NO real ``claude``
shell-out, NO live network, and ``--yes`` never hangs on stdin.
"""

from __future__ import annotations

import getpass
import warnings
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from sanctum_cli import recipes
from sanctum_cli.cli import app
from sanctum_cli.commands import onboard
from sanctum_cli.providers.base import HealthSnapshot

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _ok() -> HealthSnapshot:
    return HealthSnapshot(ok=True, latency_ms=12, quota_remaining=None, detail=None)


def _bad(detail: str = "401 unauthorized") -> HealthSnapshot:
    return HealthSnapshot(ok=False, latency_ms=None, quota_remaining=None, detail=detail)


@pytest.fixture(autouse=True)
def _no_live_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Onboard tests must never probe a real Firewalla bridge (firewalla-compat gate)."""
    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None)


@pytest.fixture(autouse=True)
def _no_real_keychain_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never shell out to ``security`` to write/delete a real Keychain entry.

    Stub the LOW-LEVEL keychain-write + trifecta-mirror + revoke seams so even a
    test driving the REAL :func:`store_device_secret` never touches the host
    Keychain. A test that wants to observe a seam installs its own recorder over
    these (applied after this fixture, so it wins).
    """
    monkeypatch.setattr("sanctum_cli.commands.onboard._keychain_write", lambda *a, **k: None)
    monkeypatch.setattr("sanctum_cli.commands.onboard._mirror_to_trifecta", lambda **k: None)
    monkeypatch.setattr("sanctum_cli.commands.onboard._revoke_device_secret", lambda **k: None)


@pytest.fixture(autouse=True)
def _no_proxy_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subscription path's proxy-wiring seam never touches launchctl by default."""
    monkeypatch.setattr("sanctum_cli.commands.onboard._ensure_claude_proxy", lambda: None)


# ── Persistence helper (atomic YAML write, mirrors set_device_reference) ──


def test_set_provider_config_writes_blocks_preserving_siblings(tmp_path: Path) -> None:
    """cli.providers.{claude,gemini} + cli.default_provider land; siblings survive; .bak."""
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "instance:\n  name: X\n  slug: x\n"
        "services:\n  proxyd:\n    port: 4040\n"
        "cli:\n  telemetry:\n    enabled: true\n",
        encoding="utf-8",
    )
    onboard.set_provider_config(
        claude={"via": "proxy", "endpoint": "http://127.0.0.1:2001"},
        gemini={"model": "gemini-2.5-pro"},
        default_provider="claude",
        path=inst,
    )
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["cli"]["providers"]["claude"]["via"] == "proxy"
    assert data["cli"]["providers"]["claude"]["endpoint"] == "http://127.0.0.1:2001"
    assert data["cli"]["providers"]["gemini"]["model"] == "gemini-2.5-pro"
    assert data["cli"]["default_provider"] == "claude"
    # Sibling blocks untouched — both top-level and inside cli:.
    assert data["services"]["proxyd"]["port"] == 4040
    assert data["instance"]["slug"] == "x"
    assert data["cli"]["telemetry"]["enabled"] is True
    assert (tmp_path / "instance.yaml.bak").exists()


def test_set_provider_config_partial_only_touches_given_keys(tmp_path: Path) -> None:
    """Persisting only Gemini leaves an existing claude block and default alone."""
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "instance:\n  name: X\n  slug: x\n"
        "cli:\n  default_provider: gemini\n"
        "  providers:\n    claude:\n      via: direct\n",
        encoding="utf-8",
    )
    onboard.set_provider_config(gemini={"model": "gemini-2.5-pro"}, path=inst)
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["cli"]["providers"]["gemini"]["model"] == "gemini-2.5-pro"
    # claude block + default_provider preserved (not given → not clobbered).
    assert data["cli"]["providers"]["claude"]["via"] == "direct"
    assert data["cli"]["default_provider"] == "gemini"


def test_set_provider_config_result_loads_under_schema(tmp_path: Path) -> None:
    """The persisted YAML round-trips through the real config loader (contract)."""
    from sanctum_cli import config

    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    onboard.set_provider_config(
        claude={"via": "direct", "endpoint": "https://api.anthropic.com"},
        gemini={"model": "gemini-2.5-pro"},
        default_provider="gemini",
        path=inst,
    )
    cfg = config.load(inst)
    assert cfg.cli.providers.claude.via == "direct"
    assert cfg.cli.default_provider == "gemini"


# ── Gate wiring (the chapter is recipe-listed data) ──────────────────


def test_ai_providers_gate_listed_in_family_recipe() -> None:
    """The AI chapter is recipe-listed data; gates only reference real recipes."""
    assert "ai-providers" in onboard.RECIPE_GATES["family"]
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)


# ── Universal arc: ai-providers is wired into EVERY recipe (Task 3) ──
# The Apple-arc is universal — the recipe only chooses the backup scope, so the
# "Your AI" chapter must run on `family`, `operator`, AND `code`. A recipe that
# lists no gates would render the chapter banner but silently never connect a
# provider, so each built-in recipe carries the `ai-providers` gate.


def test_ai_providers_gate_listed_in_every_builtin_recipe() -> None:
    """`ai-providers` is in family, operator, AND code — the arc is universal."""
    for recipe in ("family", "operator", "code"):
        assert "ai-providers" in onboard.RECIPE_GATES[recipe], recipe
    # Gates may only key on recipes that actually exist (no phantom recipes).
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)
    # operator and code carry their own gate tuple (not just family).
    assert "operator" in onboard.RECIPE_GATES
    assert "code" in onboard.RECIPE_GATES


def test_ai_providers_dispatch_branch_exists() -> None:
    """The dispatcher wires the listed name to `_run_ai_providers` — registration
    is not enough; a listed gate with no branch would be dead data."""
    import inspect

    src = inspect.getsource(onboard._run_gate)
    assert 'gate == "ai-providers"' in src
    assert "_run_ai_providers(yes=yes)" in src


def test_ai_providers_dispatch_invokes_handler() -> None:
    """`_run_gate('ai-providers', ...)` behaviorally routes to `_run_ai_providers`."""
    seen: dict[str, Any] = {}
    with patch(
        "sanctum_cli.commands.onboard._run_ai_providers",
        lambda *, yes: seen.update(yes=yes),
    ):
        onboard._run_gate("ai-providers", yes=True)
    assert seen == {"yes": True}


def test_ai_providers_runs_in_operator_and_code_chapters() -> None:
    """The "Your AI" chapter actually activates `ai-providers` for operator + code.

    The orchestrator runs only the gates the active recipe lists, partitioned by
    chapter (`_chapter_active_gates`). For the arc to read identically on every
    recipe, the "Your AI" chapter must resolve to `('ai-providers',)` on operator
    and code, not the empty tuple (which would render the banner but connect nothing).
    """
    for recipe in ("family", "operator", "code"):
        assert onboard._chapter_active_gates("Your AI", recipe) == ("ai-providers",), recipe


def test_ai_providers_precedes_network_gates_in_every_recipe() -> None:
    """Placement (tools-before-data): `ai-providers` is listed ahead of the network
    gates in each recipe tuple, matching the family ordering decision — so the
    RECIPE_GATES order honors AI → Network within the gate-driven chapters."""
    network_gates = {"firewalla-pairing", "firewalla-compat", "network-gear"}
    for recipe in ("family", "operator", "code"):
        gates = onboard.RECIPE_GATES[recipe]
        ai_idx = gates.index("ai-providers")
        for net_gate in network_gates & set(gates):
            assert ai_idx < gates.index(net_gate), (recipe, net_gate)


# ── --yes skips the whole chapter (no prompt, no write) ──────────────


def _invoke_family_onboard_yes() -> tuple[int, str]:
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch(
            "sanctum_cli.commands.onboard._run_canary",
            return_value=onboard.CanaryOutcome.VERIFIED,
        ),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])
    return result.exit_code, " ".join(result.stdout.split())


def test_ai_providers_yes_skips_without_prompting_or_writing(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes must SKIP the interactive chapter — no prompt, no health, no write."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    health_called = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._provider_health",
        lambda kind, cfg: health_called.__setitem__("n", health_called["n"] + 1) or _ok(),
    )
    store_called = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda **k: store_called.__setitem__("n", store_called["n"] + 1),
    )
    code, out = _invoke_family_onboard_yes()
    assert code == 0, out
    assert "Your AI" in out
    assert "skipped" in out
    assert health_called["n"] == 0
    assert store_called["n"] == 0
    assert "onboarding complete" in out


# ── Interactive harness ──────────────────────────────────────────────


def _invoke_family_onboard_interactive(input_text: str) -> tuple[int, str]:
    """`onboard --recipe family` (no --yes), feeding stdin; OTHER gates mocked out."""
    import sys

    with (
        patch("getpass.getpass", side_effect=lambda *a, **k: sys.stdin.readline().rstrip("\n")),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch(
            "sanctum_cli.commands.onboard._run_canary",
            return_value=onboard.CanaryOutcome.VERIFIED,
        ),
        # Every OTHER interactive gate has its own tests; mock them so they don't
        # consume THIS chapter's stdin.
        patch("sanctum_cli.commands.onboard._run_identity_setup"),
        patch("sanctum_cli.commands.onboard._run_family_setup"),
        patch("sanctum_cli.commands.onboard._run_firewalla_pairing"),
        patch("sanctum_cli.commands.onboard._run_network_gear"),
        # HA Green is a later interactive gate (own tests); mock it so a real TCP
        # probe to 10.0.0.3 never runs and it never consumes this chapter's stdin.
        patch("sanctum_cli.commands.onboard._run_ha_green"),
        # Network-resilience is the last interactive gate (own tests); mock it so a
        # real posture probe / DHCP flip / daemon install never runs here.
        patch("sanctum_cli.commands.onboard._run_network_resilience"),
        patch("sanctum_cli.commands.onboard._run_wifi_identity"),
        patch("sanctum_cli.commands.onboard._run_first_hello"),
        # The masked key prompt (Prompt.ask(password=True)) routes to getpass, which
        # emits GetPassWarning under CliRunner's non-TTY stdin; pyproject turns
        # warnings into errors, so suppress only that benign one here (production has
        # a real TTY; masking is the security-correct default).
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore", getpass.GetPassWarning)
        result = runner.invoke(app, ["onboard", "--recipe", "family"], input=input_text)
    return result.exit_code, " ".join(result.stdout.split())


# ── Claude subscription path (via=proxy, no Keychain) ────────────────


def test_claude_subscription_ready_persists_via_proxy_no_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription default + claude CLI ready + passing proxy health → via=proxy.

    No Keychain write on the subscription path (the Max subscription bills through
    the local proxy, no API key); the proxy is wired; a green proxy health-probe
    earns the persisted ``cli.providers.claude.via=proxy`` + endpoint.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    monkeypatch.setattr("sanctum_cli.commands.onboard._claude_cli_ready", lambda: True)
    wired = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._ensure_claude_proxy",
        lambda: wired.__setitem__("n", wired["n"] + 1),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._provider_health",
        lambda kind, cfg: _ok() if kind == "claude" else _bad(),
    )
    store_called = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda **k: store_called.__setitem__("n", store_called["n"] + 1),
    )

    code, out = _invoke_family_onboard_interactive(
        "\n\n"  # proceed? / run real backup? (defaults)
        "1\n"  # Claude: subscription (the default, made explicit)
        "\n"  # Gemini: skip (empty key)
    )
    assert code == 0, out
    assert wired["n"] == 1  # the proxy was wired
    assert store_called["n"] == 0  # NO keychain write on the subscription path
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    claude = data["cli"]["providers"]["claude"]
    assert claude["via"] == "proxy"
    assert claude["endpoint"] == "http://127.0.0.1:3456"
    assert "Claude" in out


def test_claude_subscription_default_no_explicit_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing enter on the Claude choice takes the subscription default (option 1)."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    monkeypatch.setattr("sanctum_cli.commands.onboard._claude_cli_ready", lambda: True)
    monkeypatch.setattr("sanctum_cli.commands.onboard._provider_health", lambda kind, cfg: _ok())

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "\n"  # Claude: ENTER → default subscription
        "\n"  # Gemini: skip
    )
    assert code == 0, out
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["cli"]["providers"]["claude"]["via"] == "proxy"


def test_claude_subscription_cli_not_ready_persists_nothing_shows_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription chosen but the claude CLI is missing/not-logged-in.

    Fail-closed: persist NOTHING for Claude (no false via=proxy), do NOT wire the
    proxy, and show the calm install-guidance panel with the one next action.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    monkeypatch.setattr("sanctum_cli.commands.onboard._claude_cli_ready", lambda: False)
    wired = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._ensure_claude_proxy",
        lambda: wired.__setitem__("n", wired["n"] + 1),
    )
    # If a health-probe is reached for claude, that is itself a bug (nothing to probe).
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._provider_health",
        lambda kind, cfg: _bad("should not probe claude") if kind == "claude" else _ok(),
    )

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "1\n"  # subscription
        "\n"  # Gemini: skip
    )
    assert code == 0, out  # non-blocking
    assert wired["n"] == 0  # proxy NOT wired on a not-ready CLI
    assert "claude login" in out  # the install guidance's one next action
    data = yaml.safe_load(inst.read_text(encoding="utf-8")) or {}
    # No claude provider block persisted (persist-nothing on the not-ready path).
    claude = data.get("cli", {}).get("providers", {}).get("claude")
    assert claude is None or "via" not in claude


# ── Claude API-key path (via=direct, Keychain anthropic-api-key) ─────


def test_claude_api_key_accepted_stores_and_persists_via_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-key choice → masked key → store_device_secret → green health → via=direct."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    stored: dict[str, Any] = {}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda *, service, account, secret: stored.update(
            service=service, account=account, secret=secret
        ),
    )
    monkeypatch.setattr("sanctum_cli.commands.onboard._provider_health", lambda kind, cfg: _ok())
    revoked = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._revoke_device_secret",
        lambda **k: revoked.__setitem__("n", revoked["n"] + 1),
    )

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "2\n"  # Claude: API key
        "sk-ant-secret\n"  # masked key
        "\n"  # Gemini: skip
    )
    assert code == 0, out
    assert "sk-ant-secret" not in out  # masked
    assert stored == {
        "service": "anthropic-api-key",
        "account": "sanctum",
        "secret": "sk-ant-secret",
    }
    assert revoked["n"] == 0  # accepted key → no revoke
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["cli"]["providers"]["claude"]["via"] == "direct"


def test_claude_api_key_rejected_revokes_and_persists_no_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REJECTED Anthropic key → revoke the Keychain entry; persist NO via=direct.

    Fail-closed (mirrors the network-gear rejected-probe path): the secret is
    written FIRST (the provider re-reads it from the Keychain to authenticate the
    health-probe), then probed; a failing probe REVOKES the write and persists
    nothing, so a bad key leaves nothing usable behind.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    order: list[str] = []
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda **k: order.append("store"),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._provider_health",
        lambda kind, cfg: order.append("probe") or _bad("401 unauthorized"),
    )
    revoked: dict[str, Any] = {}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._revoke_device_secret",
        lambda *, service, account: revoked.update(service=service, account=account),
    )

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "2\n"  # API key
        "sk-bad\n"  # rejected by the health-probe
        "\n"  # Gemini: skip
    )
    assert code == 0, out  # non-blocking
    # Write precedes probe (the load-bearing contract: health re-reads the key).
    assert order == ["store", "probe"]
    assert revoked == {"service": "anthropic-api-key", "account": "sanctum"}
    data = yaml.safe_load(inst.read_text(encoding="utf-8")) or {}
    claude = data.get("cli", {}).get("providers", {}).get("claude")
    assert claude is None or claude.get("via") != "direct"
    assert "not configured" in out or "rejected" in out


# ── Gemini API-key path ──────────────────────────────────────────────


def test_gemini_key_accepted_stores_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini key → store_device_secret(google-ai-api-key) → green health → persisted."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    # Skip Claude cleanly (subscription, not ready → persist nothing for claude).
    monkeypatch.setattr("sanctum_cli.commands.onboard._claude_cli_ready", lambda: False)
    stored: dict[str, Any] = {}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda *, service, account, secret: stored.update(
            service=service, account=account, secret=secret
        ),
    )
    monkeypatch.setattr("sanctum_cli.commands.onboard._provider_health", lambda kind, cfg: _ok())

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "1\n"  # Claude subscription (CLI not ready → skipped)
        "gm-secret-key\n"  # Gemini key
    )
    assert code == 0, out
    assert "gm-secret-key" not in out  # masked
    assert stored == {
        "service": "google-ai-api-key",
        "account": "sanctum",
        "secret": "gm-secret-key",
    }
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["cli"]["providers"]["gemini"]["model"]  # a gemini block was persisted


def test_gemini_key_rejected_revokes_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REJECTED Gemini key → revoke the Keychain entry; persist no gemini block."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    monkeypatch.setattr("sanctum_cli.commands.onboard._claude_cli_ready", lambda: False)
    monkeypatch.setattr("sanctum_cli.commands.onboard.store_device_secret", lambda **k: None)
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._provider_health", lambda kind, cfg: _bad("403")
    )
    revoked: dict[str, Any] = {}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._revoke_device_secret",
        lambda *, service, account: revoked.update(service=service, account=account),
    )

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "1\n"  # Claude subscription (not ready → skipped)
        "gm-bad\n"  # Gemini rejected
    )
    assert code == 0, out
    assert revoked == {"service": "google-ai-api-key", "account": "sanctum"}
    data = yaml.safe_load(inst.read_text(encoding="utf-8")) or {}
    gemini = data.get("cli", {}).get("providers", {}).get("gemini")
    assert gemini is None  # nothing persisted for a rejected key


def test_gemini_skipped_with_empty_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty Gemini key skips Gemini (add later) without a store or probe."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    monkeypatch.setattr("sanctum_cli.commands.onboard._claude_cli_ready", lambda: False)
    store_called = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda **k: store_called.__setitem__("n", store_called["n"] + 1),
    )
    monkeypatch.setattr("sanctum_cli.commands.onboard._provider_health", lambda kind, cfg: _ok())

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "1\n"  # Claude subscription (not ready → skipped)
        "\n"  # Gemini: empty → skip
    )
    assert code == 0, out
    assert store_called["n"] == 0
    data = yaml.safe_load(inst.read_text(encoding="utf-8")) or {}
    assert "gemini" not in data.get("cli", {}).get("providers", {})


# ── _claude_cli_ready seam (shutil.which + login probe) ──────────────


def test_claude_cli_ready_false_when_binary_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``claude`` on PATH → not ready, and the login probe is never reached."""
    monkeypatch.setattr("sanctum_cli.commands.onboard.shutil.which", lambda _b: None)
    probed = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._claude_logged_in",
        lambda: probed.__setitem__("n", probed["n"] + 1) or True,
    )
    assert onboard._claude_cli_ready() is False
    assert probed["n"] == 0  # short-circuits before the login probe


def test_claude_cli_ready_true_when_binary_and_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """``claude`` present AND logged in → ready."""
    monkeypatch.setattr("sanctum_cli.commands.onboard.shutil.which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr("sanctum_cli.commands.onboard._claude_logged_in", lambda: True)
    assert onboard._claude_cli_ready() is True


def test_claude_cli_ready_false_when_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """``claude`` present but NOT logged in → not ready (fail-closed)."""
    monkeypatch.setattr("sanctum_cli.commands.onboard.shutil.which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr("sanctum_cli.commands.onboard._claude_logged_in", lambda: False)
    assert onboard._claude_cli_ready() is False
