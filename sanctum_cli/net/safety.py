from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sanctum_cli.net.types import Baseline, TopologyReport

if TYPE_CHECKING:
    from pathlib import Path


def snapshot(report: TopologyReport, *, root: Path, stamp: str | None = None) -> Path:
    """Write a rollback baseline before any action. Returns the file path."""
    stamp = stamp or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = root / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "baseline.json"
    data = {
        "wan_ip": report.wan_ip,
        "gateway_ip": report.gateway_ip,
        "public_ip": report.public_ip,
        "mtu": report.firewalla_wan_mtu,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def load(path: Path) -> Baseline:
    d = json.loads(path.read_text(encoding="utf-8"))
    return Baseline(
        wan_ip=d.get("wan_ip"),
        gateway_ip=d.get("gateway_ip"),
        public_ip=d.get("public_ip"),
        mtu=d.get("mtu"),
    )
