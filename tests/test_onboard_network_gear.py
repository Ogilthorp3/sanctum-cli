"""sanctum onboard — network-gear detection + pairing gate (Task 2).

The gate is ADDITIVE to the existing onboard flow: it runs registry detection
across the registered DeviceProviders over the current NetContext and, for each
DETECTED device kind, offers guided pairing that mirrors the Firewalla-pairing
gate (prompt the admin cred → ``provider.connect()`` READ-ONLY auth-probe →
persist to keychain + an instance.yaml ``devices.<kind>`` reference block).

Military-grade contract (mirrors ``_run_firewalla_pairing``): a device is
declared "paired" ONLY when the read-only ``connect()`` auth-probe genuinely
succeeds. An unreachable box / wrong password must NOT be written.

Every test mocks the provider/registry and the keychain-write seam — NO live
device is ever touched, and ``--yes`` must never hang on stdin.
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
from sanctum_cli.devices.base import Creds, DeviceError, NetContext

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_live_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Onboard tests must never probe a real Firewalla bridge (firewalla-compat gate)."""
    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None)


@pytest.fixture(autouse=True)
def _no_real_keychain_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never shell out to ``security`` to write a real Keychain entry.

    Default stance: stub the LOW-LEVEL keychain-write + trifecta-mirror seams so
    even a test that drives the REAL :func:`store_device_secret` never touches the
    host Keychain — while still exercising its two-tier logic. A test that wants
    to observe the seams installs its OWN recorder over these (applied after this
    fixture, so it wins).
    """
    monkeypatch.setattr("sanctum_cli.commands.onboard._keychain_write", lambda *a, **k: None)
    monkeypatch.setattr("sanctum_cli.commands.onboard._mirror_to_trifecta", lambda **k: None)
    # Never shell out to `security delete-generic-password` on the rollback path.
    monkeypatch.setattr("sanctum_cli.commands.onboard._revoke_device_secret", lambda **k: None)


# ── Fakes ─────────────────────────────────────────────────────────────


class _FakeProvider:
    """A minimal DeviceProvider whose connect() is a recorded READ-ONLY probe.

    Faithful to the real providers, ``brand`` is a CLASS attribute (the resolvable
    registry pin — ``SagemcomHubProvider.brand``/``OrbiProvider.brand``), set per
    instance onto ``type(self)`` from the constructor so ``type(provider).brand``
    returns the canonical pin even after ``connect()`` REFINES the instance brand to
    a concrete model. Because each test builds a fresh fake (and asserts before the
    next), stamping the class attr per-construction is safe here.
    """

    brand = "fake"

    def __init__(self, kind: str, brand: str, *, connect_raises: bool = False) -> None:
        self.kind = kind
        # Stamp the canonical pin onto the (sub)class so ``type(self).brand`` is the
        # resolvable brand — mirroring the real providers' class-level declaration.
        type(self).brand = brand
        self.brand = brand
        self._connect_raises = connect_raises
        self.connected_with: Creds | None = None

    def connect(self, creds: Creds | None) -> None:
        if self._connect_raises:
            raise DeviceError(
                f"{self.brand} auth-probe rejected",
                fix="check the admin password",
            )
        self.connected_with = creds

    def disconnect(self) -> None:
        return None


# ── Gate wiring (data, not a buried conditional) ─────────────────────


def test_network_gear_gate_listed_in_family_recipe() -> None:
    """The gate is recipe-listed data; gates only reference real recipes."""
    assert "network-gear" in onboard.RECIPE_GATES["family"]
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)


def test_network_gear_gate_runs_after_firewalla_compat() -> None:
    """Additive: the new gate runs AFTER the existing firewalla-compat gate."""
    gates = onboard.RECIPE_GATES["family"]
    assert gates.index("network-gear") > gates.index("firewalla-compat")


# ── detect_network_gear (pure-ish: registry scan, no mutation) ───────


def test_detect_network_gear_returns_detected_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each registered kind whose detect() scores > 0 is returned as (kind, provider)."""
    hub = _FakeProvider("hub", "sagemcom")
    orbi = _FakeProvider("orbi", "orbi")

    def fake_resolve(kind: str, net: NetContext) -> Any:
        # hub + orbi detected; firewalla not (the registry's generic fallback).
        return {"hub": hub, "orbi": orbi}.get(kind)

    monkeypatch.setattr("sanctum_cli.commands.onboard._detect_kind", fake_resolve)

    net = NetContext(gateway_ip="192.168.2.1", runner=None)
    detected = onboard.detect_network_gear(net)
    kinds = {k for k, _ in detected}
    assert kinds == {"hub", "orbi"}


def test_detect_network_gear_empty_when_nothing_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """No detected gear → empty list (haus-aware: absent gear is silently skipped)."""
    monkeypatch.setattr("sanctum_cli.commands.onboard._detect_kind", lambda kind, net: None)
    net = NetContext(gateway_ip=None, runner=None)
    assert onboard.detect_network_gear(net) == []


# ── set_device_reference (atomic YAML write, mirrors set_firewalla_bridge) ──


def test_set_device_reference_writes_block_preserving_siblings(tmp_path: Path) -> None:
    """A devices.<kind> reference block lands; sibling blocks survive; .bak written."""
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "instance:\n  name: X\n  slug: x\nservices:\n  proxyd:\n    port: 4040\n",
        encoding="utf-8",
    )
    onboard.set_device_reference(
        kind="hub",
        brand="sagemcom",
        host="192.168.2.1",
        keychain_service="bell-hub-admin",
        keychain_account="admin",
        path=inst,
    )
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    block = data["devices"]["hub"]
    assert block["brand"] == "sagemcom"
    assert block["host"] == "192.168.2.1"
    assert block["keychain"]["service"] == "bell-hub-admin"
    assert block["keychain"]["account"] == "admin"
    # Sibling blocks untouched.
    assert data["services"]["proxyd"]["port"] == 4040
    assert data["instance"]["slug"] == "x"
    assert (tmp_path / "instance.yaml.bak").exists()


def test_set_device_reference_merges_second_kind(tmp_path: Path) -> None:
    """Pairing a second kind keeps the first kind's block (devices map is merged)."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    onboard.set_device_reference(
        kind="hub", brand="sagemcom", host="192.168.2.1",
        keychain_service="bell-hub-admin", keychain_account="admin", path=inst,
    )
    onboard.set_device_reference(
        kind="orbi", brand="orbi", host="192.168.1.1",
        keychain_service="orbi-admin", keychain_account="admin", path=inst,
    )
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["devices"]["hub"]["brand"] == "sagemcom"
    assert data["devices"]["orbi"]["brand"] == "orbi"


# ── Gate behavior under onboarding ───────────────────────────────────


def _invoke_family_onboard_yes() -> tuple[int, str]:
    """`onboard --recipe family --yes` with the backup primitives mocked."""
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])
    return result.exit_code, " ".join(result.stdout.split())


def test_network_gear_yes_skips_without_probing(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes must SKIP the interactive gate — no detect, no connect, no write."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    detect_called = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.detect_network_gear",
        lambda net: detect_called.__setitem__("n", detect_called["n"] + 1) or [],
    )
    code, out = _invoke_family_onboard_yes()
    assert code == 0, out
    assert "Network gear" in out
    assert "skipped" in out
    assert detect_called["n"] == 0  # the gate short-circuits before detection
    assert "onboarding complete" in out


def _invoke_family_onboard_interactive(input_text: str) -> tuple[int, str]:
    """`onboard --recipe family` (no --yes), feeding stdin; other gates mocked out."""
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
        # The other interactive gates have their own tests; mock them so they
        # don't consume this gate's stdin.
        patch("sanctum_cli.commands.onboard._run_identity_setup"),
        patch("sanctum_cli.commands.onboard._run_family_setup"),
        patch("sanctum_cli.commands.onboard._run_firewalla_pairing"),
        patch("sanctum_cli.commands.onboard._run_ai_providers"),
        # The masked admin-password prompt (Prompt.ask(password=True)) routes to
        # getpass, which emits GetPassWarning when stdin is not a real TTY (every
        # CliRunner). The pyproject `filterwarnings=["error"]` would turn that
        # benign, test-environment-only warning into a crash at the prompt — so
        # suppress only that one warning here. In production there IS a TTY and the
        # warning never fires; masking the password is the security-correct default
        # (mirrors `_run_firewalla_pairing`'s token prompt).
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore", getpass.GetPassWarning)
        result = runner.invoke(app, ["onboard", "--recipe", "family"], input=input_text)
    return result.exit_code, " ".join(result.stdout.split())


def test_network_gear_absent_gear_silently_skipped(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No detected gear → a 'no network gear detected' note, onboarding continues."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    monkeypatch.setattr("sanctum_cli.commands.onboard.detect_network_gear", lambda net: [])

    code, out = _invoke_family_onboard_interactive("\n\n")  # proceed / run-backup defaults
    assert code == 0, out
    assert "Network gear" in out
    assert "no network gear detected" in out
    assert "onboarding complete" in out


def test_network_gear_detected_device_paired_on_successful_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detected hub → prompt cred → connect() probe ok → keychain write + devices block."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    order: list[str] = []

    class _OrderedHub(_FakeProvider):
        def connect(self, creds: Creds | None) -> None:
            order.append("probe")
            super().connect(creds)

    hub = _OrderedHub("hub", "sagemcom")
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.detect_network_gear", lambda net: [("hub", hub)]
    )

    stored: dict[str, Any] = {}

    def _record_store(*, service: str, account: str, secret: str) -> None:
        order.append("store")
        stored.update(service=service, account=account, secret=secret)

    monkeypatch.setattr("sanctum_cli.commands.onboard.store_device_secret", _record_store)

    code, out = _invoke_family_onboard_interactive(
        "\n\n"  # proceed? / run the real backup now? (defaults)
        "y\n"  # pair the detected hub now?
        "hunter2\n"  # admin password
    )
    assert code == 0, out
    # connect() received the resolved creds and the password was NOT in stdout.
    assert hub.connected_with is not None
    assert "hunter2" not in out
    # The secret is written to the Keychain BEFORE the auth-probe — the real
    # providers' connect() reads the password FROM the Keychain (it ignores
    # creds.secret), so the write must precede the probe for the probe to
    # authenticate with the just-entered password (the load-bearing contract).
    assert order == ["store", "probe"]
    # Secret written to the keychain seam under the resolved hub (service, account).
    assert stored["service"] == "bell-hub-admin"
    assert stored["account"] == "admin"
    assert stored["secret"] == "hunter2"
    # devices.hub reference block persisted to instance.yaml.
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["devices"]["hub"]["brand"] == "sagemcom"
    assert data["devices"]["hub"]["keychain"]["service"] == "bell-hub-admin"
    assert "paired" in out


def test_network_gear_persists_class_brand_pin_resolvable_after_refine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine connect() that REFINES the instance brand must still persist the
    CLASS-level brand — the only value ``registry.resolve(brand_pin=...)`` resolves.

    Producer→consumer contract (not a structural field assertion): the REAL
    providers' ``connect()`` rewrites ``self.brand`` to the concrete model
    (``SagemcomHubProvider._refine_brand`` → ``sagemcom-fast5689``;
    ``OrbiProvider._refine_brand`` → ``orbi-rbr850``). But the registry's
    ``brand_pin`` path matches the persisted pin against the CLASS ``cls.brand``
    (``"sagemcom"``/``"orbi"``). So if pairing persisted the refined INSTANCE
    brand, a later ``sanctum net hub/orbi`` would call
    ``registry.resolve(..., brand_pin="sagemcom-fast5689")`` and hard-error
    ``no registered provider for pinned brand``.

    The fake here REFINES on connect (exactly as the real ``_refine_brand`` does)
    — the trait the happy-path ``_FakeProvider`` lacks, which is why the existing
    tests share the bug. Expectations are derived from a DIFFERENT source than the
    fake: the real registry's resolution of the persisted pin.
    """
    from sanctum_cli.devices import orbi as orbi_mod  # real provider for the consumer side
    from sanctum_cli.devices import registry
    from sanctum_cli.devices.base import NetContext

    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    class _RefiningOrbi(_FakeProvider):
        """A provider whose connect() mutates self.brand to the concrete model —
        mirrors OrbiProvider._refine_brand. Class brand stays the resolvable pin."""

        brand = orbi_mod.OrbiProvider.brand  # class-level pin: "orbi"

        def connect(self, creds: Creds | None) -> None:
            super().connect(creds)
            self.brand = "orbi-rbr850"  # refined INSTANCE brand (un-resolvable as a pin)

    provider = _RefiningOrbi("orbi", orbi_mod.OrbiProvider.brand)
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.detect_network_gear", lambda net: [("orbi", provider)]
    )
    monkeypatch.setattr("sanctum_cli.commands.onboard.store_device_secret", lambda **k: None)

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "y\n"  # pair the detected orbi
        "hunter2\n"  # admin password
    )
    assert code == 0, out
    assert "paired" in out
    # connect() ran and DID refine the instance brand (so the bug's precondition holds).
    assert provider.brand == "orbi-rbr850"

    persisted = yaml.safe_load(inst.read_text(encoding="utf-8"))["devices"]["orbi"]["brand"]
    # The pin is the CLASS brand, NOT the refined instance attribute.
    assert persisted == "orbi"
    assert persisted != "orbi-rbr850"

    # The load-bearing contract: the persisted pin must resolve on a LATER run.
    # Feed it through the REAL registry (the actual consumer), not a stub.
    resolved = registry.resolve("orbi", NetContext(gateway_ip="192.168.1.1", runner=None),
                                 brand_pin=persisted)
    assert isinstance(resolved, orbi_mod.OrbiProvider)


def test_network_gear_probe_rejected_rolls_back_and_does_not_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong password (connect raises) → keychain write rolled back; NO devices block.

    The REAL providers' connect() reads the password from the Keychain, so the
    pairing flow writes the secret FIRST, then probes. On a rejected probe the
    write is REVOKED — so a failed pairing persists nothing usable (no surviving
    secret, no devices block), and onboarding continues (non-blocking).
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    hub = _FakeProvider("hub", "sagemcom", connect_raises=True)
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.detect_network_gear", lambda net: [("hub", hub)]
    )
    revoked: dict[str, Any] = {}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._revoke_device_secret",
        lambda *, service, account: revoked.update(service=service, account=account),
    )

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "y\n"  # pair the hub
        "wrong-pass\n"  # rejected by the probe
    )
    assert code == 0, out  # the gate is non-blocking (the backup already succeeded)
    assert "not paired" in out
    # The keychain write was rolled back under the resolved (service, account).
    assert revoked == {"service": "bell-hub-admin", "account": "admin"}
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert "devices" not in data or "hub" not in data.get("devices", {})
    assert "onboarding complete" in out


def test_network_gear_decline_pairing_skips_that_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answering 'n' to the pair prompt skips that kind without probing or writing."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    hub = _FakeProvider("hub", "sagemcom")
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.detect_network_gear", lambda net: [("hub", hub)]
    )
    wrote = {"n": 0}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret",
        lambda **k: wrote.__setitem__("n", wrote["n"] + 1),
    )

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "n\n"  # decline pairing the hub
    )
    assert code == 0, out
    assert hub.connected_with is None  # no auth-probe fired
    assert wrote["n"] == 0
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert "devices" not in data


# ── Probe faithfulness against the REAL best-effort Orbi connect ─────
#
# CLAUDE.md "Contracts at the Boundary" §2: the happy-path _FakeProvider models
# connect() as raise-on-failure (faithful to Sagemcom, NOT to Orbi's best-effort
# connect that SWALLOWS a rejected/unreachable login). So the gate tests above
# share the production bug they were meant to catch. These tests drive the probe
# against the REAL OrbiProvider — the actual consumer — so a non-raising connect
# on a rejected/unreachable box correctly reads as a FAILED probe, not a false
# "paired". The real pynetgear client is faked at the _make_client + keychain
# seams (the provider's own test seams), so no socket and no Keychain are touched.


def _real_orbi_with_login(monkeypatch: pytest.MonkeyPatch, *, login: Any) -> Any:
    """A REAL OrbiProvider whose pynetgear client's login() behaves per ``login``.

    ``login`` is a zero-arg callable the fake client's ``login()`` delegates to (it
    may return a bool or raise). Mocks the provider's own _make_client + keychain
    seams — never a socket, never the Keychain.
    """
    from sanctum_cli.devices.orbi import OrbiProvider

    class _FakeNetgear:
        def login(self) -> Any:
            return login()

        def get_info(self, use_cache: bool = True) -> dict[str, str]:
            # A genuinely-authed session would return the model; an un-authed one
            # never reaches here in these tests (login() False/raises first).
            return {"ModelName": "RBR850"}

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", lambda creds: _FakeNetgear())
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    return OrbiProvider()


def test_probe_real_orbi_rejected_password_is_a_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REAL OrbiProvider: a REJECTED login (login() → False) → _probe_device False.

    The bug: OrbiProvider.connect is best-effort — it swallows a falsey login and
    returns cleanly, so the OLD "connect didn't raise → paired" logic returned True
    for a wrong password. The fix asserts auth via the provider's auth_ok() oracle,
    so a rejected login now correctly yields a FAILED probe.
    """
    provider = _real_orbi_with_login(monkeypatch, login=lambda: False)
    net = NetContext(gateway_ip="192.168.1.1", runner=None)
    ok = onboard._probe_device(
        provider, net=net, account="admin", service="orbi-admin", secret="wrong-pass"
    )
    assert ok is False


def test_probe_real_orbi_unreachable_box_is_a_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REAL OrbiProvider: an UNREACHABLE box (login() raises) → _probe_device False.

    connect() tolerates the transport error (returns cleanly), but auth_ok() reports
    the session never authenticated, so the probe fails-close.
    """

    def _boom() -> bool:
        msg = "no route to host"
        raise OSError(msg)

    provider = _real_orbi_with_login(monkeypatch, login=_boom)
    net = NetContext(gateway_ip="192.168.1.1", runner=None)
    ok = onboard._probe_device(
        provider, net=net, account="admin", service="orbi-admin", secret="any"
    )
    assert ok is False


def test_probe_real_orbi_good_password_is_a_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REAL OrbiProvider: a GENUINE login (login() → True) → _probe_device True.

    The fix must not over-rotate: a real, authenticated Orbi still pairs.
    """
    provider = _real_orbi_with_login(monkeypatch, login=lambda: True)
    net = NetContext(gateway_ip="192.168.1.1", runner=None)
    ok = onboard._probe_device(
        provider, net=net, account="admin", service="orbi-admin", secret="right-pass"
    )
    assert ok is True


def test_gate_real_orbi_rejected_password_revokes_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end gate against the REAL OrbiProvider with a WRONG password.

    The fail-closed contract the findings demand: a rejected best-effort Orbi
    login → the just-written Keychain secret is REVOKED and NO devices.orbi block
    is persisted (no false "paired"). Drives the whole interactive gate with the
    real provider (the actual consumer), not a raise-on-failure fake.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    provider = _real_orbi_with_login(monkeypatch, login=lambda: False)  # rejected creds
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.detect_network_gear", lambda net: [("orbi", provider)]
    )
    revoked: dict[str, Any] = {}
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._revoke_device_secret",
        lambda *, service, account: revoked.update(service=service, account=account),
    )

    code, out = _invoke_family_onboard_interactive(
        "\n\n"
        "y\n"  # pair the detected orbi
        "wrong-pass\n"  # rejected by the (real) best-effort connect → auth_ok False
    )
    assert code == 0, out  # non-blocking (the backup already succeeded)
    assert "not paired" in out
    # The keychain write was rolled back under the resolved (service, account).
    assert revoked == {"service": "orbi-admin", "account": "admin"}
    # NO devices.orbi block persisted — the false "paired" is prevented.
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert "devices" not in data or "orbi" not in data.get("devices", {})
    assert "onboarding complete" in out


# ── Trifecta mirror best-effort (keychain is the guaranteed tier) ────


def test_store_device_secret_keychain_only_when_haus_tooling_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keychain write succeeds even when the 1P/SOPS trifecta mirror is absent.

    The trifecta mirror is BEST-EFFORT: a missing `op` / SOPS binary must NOT
    raise — keychain is the guaranteed tier (CLAUDE.md secrets-trifecta).
    """
    calls: dict[str, Any] = {}

    def fake_keychain_write(account: str, service: str, value: str) -> None:
        calls["keychain"] = (service, account, value)

    monkeypatch.setattr("sanctum_cli.commands.onboard._keychain_write", fake_keychain_write)
    # The trifecta mirror raises (tooling absent) — must be swallowed.
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._mirror_to_trifecta",
        lambda **k: (_ for _ in ()).throw(FileNotFoundError("op: command not found")),
    )

    # Must NOT raise even though the mirror blew up.
    onboard.store_device_secret(service="bell-hub-admin", account="admin", secret="s3cret")
    assert calls["keychain"] == ("bell-hub-admin", "admin", "s3cret")
