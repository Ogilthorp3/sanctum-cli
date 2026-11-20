"""``sanctum devices`` must render the real devices.yaml schema.

Regression: the family loop read ``devices`` (real key is ``personal_devices``)
and the shared loop iterated ``shared_devices`` as a list of dicts when it is a
``{key: {...}}`` mapping — so on real data the command crashed with
``AttributeError: 'str' object has no attribute 'get'``. These tests pin the
real schema and the literal rendering of user strings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from typer.testing import CliRunner

from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _real_schema_config() -> dict:
    return {
        "family": {
            "kid1": {
                "role": "child",
                "personal_devices": [
                    {"name": "Kid One iPhone", "mac": "02:00:AC:CD:8E:01"},
                ],
            },
        },
        "shared_devices": {
            "xbox": {"name": "Xbox", "mac": "CC:DD:EE:FF:00:01"},
        },
    }


def test_devices_renders_real_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = tmp_path / "devices.yaml"
    dev.write_text(yaml.safe_dump(_real_schema_config()), encoding="utf-8")
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(dev))

    result = runner.invoke(app, ["devices"])

    assert result.exit_code == 0, result.stdout
    assert "Kid One iPhone" in result.stdout  # family personal_devices rendered
    assert "Xbox" in result.stdout  # shared_devices dict rendered, not crashed
    assert "💿" not in result.stdout  # MAC octets not emoji-mangled
    assert "02:00:AC:CD:8E:01" in result.stdout
