"""The suite never reads the developer's files or writes under the real ``~/.sanctum``.

``tests/conftest.py`` fakes HOME per test, points ``SANCTUM_INSTANCE_FILE`` at the fake
home (and at an absent file for the whole session, so collection-time imports are covered
too), points the TLS CA variables at a throwaway CA minted for the session, and pins
``service_user.operator_home()``, which resolves the login user through ``pwd`` rather
than HOME.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from sanctum_cli import config
from sanctum_cli import service_user as su
from sanctum_cli.errors import LocalError


def test_instance_file_is_the_fake_homes(tmp_path: Path) -> None:
    expected = tmp_path / "home" / ".sanctum" / "instance.yaml"
    assert os.environ["SANCTUM_INSTANCE_FILE"] == str(expected)
    assert config.instance_path() == expected
    assert not expected.exists()
    assert config.instance_role() is None


def test_operator_home_is_the_fake_home(tmp_path: Path) -> None:
    assert su.operator_home() == tmp_path / "home"


def test_materializing_the_wave1_plists_stays_in_the_fake_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "home"
    # Fail closed: without conftest's pin this is the real home, and nothing may be written.
    assert su.operator_home() == fake, "operator_home() is not pinned to the fake home"
    monkeypatch.setattr(su, "_package_asset", lambda name: "HOME=@OPERATOR_HOME@\n")
    dest = su.materialize_assets()
    assert dest == fake / ".sanctum" / "launchdaemons"
    assert (dest / "haus.sanctum.proxyd.plist").read_text(encoding="utf-8") == f"HOME={fake}\n"


def test_tls_ca_variables_point_at_the_throwaway_test_ca() -> None:
    """Never the developer's ``~/.sanctum/certs/ca.crt``: one CA, minted for the session."""
    from cryptography import x509

    from sanctum_cli.commands import brainstorm

    real_certs = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".sanctum" / "certs"
    names = ("SANCTUM_PROXYD_CA", "SANCTUM_COUNCIL_CACERT", "SANCTUM_COUNCIL_CA")
    cas = {Path(os.environ[name]) for name in names}
    assert len(cas) == 1
    (ca,) = cas
    assert real_certs not in ca.parents  # checked before anything is read
    assert ca == brainstorm.DEFAULT_CACERT  # bound at import, while pytest collected
    cert = x509.load_pem_x509_certificate(ca.read_bytes())
    assert cert.subject.rfc4514_string() == "CN=sanctum-test-ca"


def test_import_time_council_persona_never_saw_a_real_instance_file() -> None:
    """``council.SEATS`` is built while pytest collects; it must only see generic labels."""
    from sanctum_cli.commands import council

    persona = council.SEATS["yoda"].persona
    assert "on your Mac Mini guarding" in persona
    assert "for the operator's eyes" in persona


# ── devices.yaml resolves against the current home, never an import-time one ──


def _fake_devices(home: Path) -> Path:
    path = home / ".sanctum" / "screen-time" / "devices.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("family:\n  kid:\n    name: Kid\n", encoding="utf-8")
    return path


def test_screen_time_devices_path_follows_the_fake_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound at import, the candidates pointed at the developer's real devices.yaml."""
    from sanctum_cli.commands import screen_time

    monkeypatch.delenv("SANCTUM_DEVICES_FILE", raising=False)
    with pytest.raises(LocalError):  # the fake home has none yet
        screen_time._resolve_devices_path()
    expected = _fake_devices(tmp_path / "home")
    assert screen_time._resolve_devices_path() == expected
    assert screen_time.devices_candidates()[0] == expected


def test_onboarding_and_setup_use_the_fake_homes_devices_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanctum_cli.commands import onboard, setup

    monkeypatch.delenv("SANCTUM_DEVICES_FILE", raising=False)
    canonical = tmp_path / "home" / ".sanctum" / "screen-time" / "devices.yaml"
    assert setup._devices_file() is None
    assert onboard._devices_write_path() == canonical  # where onboarding would create it
    _fake_devices(tmp_path / "home")
    assert setup._devices_file() == canonical


def test_devices_and_schedule_read_the_fake_homes_devices_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanctum_cli.commands import devices, schedule

    monkeypatch.delenv("SANCTUM_DEVICES_FILE", raising=False)
    assert devices._load_devices() is None
    assert schedule._load_devices() is None
    _fake_devices(tmp_path / "home")
    expected = {"family": {"kid": {"name": "Kid"}}}
    assert devices._load_devices() == expected
    assert schedule._load_devices() == expected
