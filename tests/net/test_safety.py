from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.net import safety

if TYPE_CHECKING:
    from pathlib import Path
from sanctum_cli.net.types import Baseline, Nat, TopologyReport


def test_snapshot_writes_and_reads_back(tmp_path: Path) -> None:
    rep = TopologyReport(
        firewalla_present=True,
        firewalla_wan_mac="20:6d:31:51:67:82",
        firewalla_wan_mtu=1500,
        nat=Nat.DOUBLE,
        gateway_ip="192.168.2.1",
        isp="bell",
        public_ip="70.53.241.21",
        applicable=True,
        reason="double",
    )
    path = safety.snapshot(rep, root=tmp_path)
    assert path.exists()
    loaded = safety.load(path)
    assert isinstance(loaded, Baseline)
    assert loaded.public_ip == "70.53.241.21"
    assert loaded.mtu == 1500


def test_snapshot_records_wan_ip(tmp_path: Path) -> None:
    rep = TopologyReport(
        firewalla_present=True,
        firewalla_wan_mac="20:6d:31:51:67:82",
        firewalla_wan_mtu=1500,
        nat=Nat.DOUBLE,
        gateway_ip="192.168.2.1",
        isp="bell",
        public_ip="70.53.241.21",
        applicable=True,
        reason="double",
        wan_ip="192.168.2.10",
    )
    path = safety.snapshot(rep, root=tmp_path)
    loaded = safety.load(path)
    assert loaded.wan_ip == "192.168.2.10"


def test_snapshot_path_is_under_root_timestamped(tmp_path: Path) -> None:
    rep = TopologyReport(
        firewalla_present=True,
        firewalla_wan_mac=None,
        firewalla_wan_mtu=None,
        nat=Nat.DOUBLE,
        gateway_ip=None,
        isp="generic",
        public_ip=None,
        applicable=True,
        reason="x",
    )
    path = safety.snapshot(rep, root=tmp_path, stamp="20260614-160000")
    assert path == tmp_path / "20260614-160000" / "baseline.json"
