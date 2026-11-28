"""Unit and CLI tests for Thunderbolt 5 192 GB Apple Silicon Cluster Mode."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app as cli_app
from sanctum_cli.hive.tb5_cluster import (
    ClusterBackend,
    ClusterMode,
    ClusterNode,
    Tb5LinkStatus,
    build_mlx_hostfile,
    plan_cluster_sharding,
    recommended_models_for_vram,
)

if TYPE_CHECKING:
    from pathlib import Path


def _hub_and_peer(*, rdma: bool = False) -> list[ClusterNode]:
    return [
        ClusterNode(
            name="hub-mac",
            ssh_target="127.0.0.1",
            tb5_ip="10.0.5.1",
            chip="Apple M4 Pro",
            memory_gb=64,
            rdma_enabled=rdma,
            is_local=True,
        ),
        ClusterNode(
            name="peer-mac",
            ssh_target="peer-mac",
            tb5_ip="10.0.5.2",
            chip="Apple M4 Max",
            memory_gb=128,
            rdma_enabled=rdma,
            is_local=False,
        ),
    ]


def test_asymmetric_64g_128g_defaults_to_pipeline_and_unlocks_full_vram() -> None:
    nodes = _hub_and_peer(rdma=False)
    plan = plan_cluster_sharding(nodes)

    assert plan.asymmetric is True
    assert plan.mode == ClusterMode.PIPELINE
    assert plan.backend == ClusterBackend.RING
    assert plan.total_ram_gb == 192
    # 47 GB (hub) + 98 GB (peer) = 145 GB usable VRAM -> fits Qwen3-235B-A22B-4bit (132 GB)!
    assert plan.usable_vram_gb == 145
    assert plan.combined_bandwidth_gbps == 273 + 546
    assert plan.layer_split_ratios[1] > plan.layer_split_ratios[0] * 1.9
    model_ids = [m.hf_id for m in plan.recommendations]
    assert "mlx-community/Qwen3-235B-A22B-4bit" in model_ids


def test_tensor_mode_caps_vram_at_2x_smallest_node_and_rdma_selects_jaccl() -> None:
    nodes = _hub_and_peer(rdma=True)
    plan = plan_cluster_sharding(nodes, mode=ClusterMode.TENSOR)

    assert plan.mode == ClusterMode.TENSOR
    assert plan.backend == ClusterBackend.JACCL
    assert plan.usable_vram_gb == 47 * 2
    assert plan.layer_split_ratios == (0.5, 0.5)
    model_ids = [m.hf_id for m in plan.recommendations]
    assert "mlx-community/Mistral-Large-Instruct-2411-4bit" in model_ids
    assert "mlx-community/Qwen3-235B-A22B-4bit" not in model_ids


def test_build_mlx_hostfile_places_local_rank0_first() -> None:
    nodes = list(reversed(_hub_and_peer()))
    hf = build_mlx_hostfile(nodes)
    assert hf == [
        {"ssh": "127.0.0.1", "ips": ["10.0.5.1"]},
        {"ssh": "peer-mac", "ips": ["10.0.5.2"]},
    ]


def test_single_node_rejected_by_planner() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        plan_cluster_sharding([_hub_and_peer()[0]])


def test_recommended_models_filter_honestly_by_vram() -> None:
    small = recommended_models_for_vram(48)
    assert all(m.est_vram_gb <= 48 for m in small)
    assert any("80B" in m.hf_id for m in small)


def test_tb5_cli_status_and_up_dry_run_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SANCTUM_HOME", str(tmp_path))
    nodes = _hub_and_peer(rdma=False)
    link = Tb5LinkStatus(
        bridge_up=True,
        local_ip="10.0.5.1",
        peer_ip="10.0.5.2",
        link_speed_gbps=80,
        peer_mac_visible=True,
        rdma_local=False,
        rdma_peer=False,
        recommended_backend=ClusterBackend.RING,
    )
    monkeypatch.setattr("sanctum_cli.commands.hive_tb5_cmd.detect_local_tb5", lambda **_: (link, nodes))

    runner = CliRunner()
    res = runner.invoke(cli_app, ["hive", "tb5", "status", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["ok"] is True
    assert data["plan"]["total_ram_gb"] == 192
    assert data["plan"]["mode"] == "pipeline"

    dry = runner.invoke(
        cli_app,
        ["hive", "tb5", "up", "--model", "mlx-community/Qwen3-235B-A22B-4bit", "--dry-run", "--json"],
    )
    assert dry.exit_code == 0, dry.output
    dry_data = json.loads(dry.output)
    assert dry_data["dry_run"] is True
    assert "--pipeline" in dry_data["command"]
    assert "ring" in dry_data["command"]


def test_tb5_cli_probe_tcp_firewall_diagnostic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SANCTUM_HOME", str(tmp_path))
    nodes = _hub_and_peer(rdma=False)
    link = Tb5LinkStatus(
        bridge_up=True,
        local_ip="10.0.5.1",
        peer_ip="10.0.5.2",
        link_speed_gbps=80,
        peer_mac_visible=True,
        rdma_local=False,
        rdma_peer=False,
        recommended_backend=ClusterBackend.RING,
    )
    monkeypatch.setattr("sanctum_cli.commands.hive_tb5_cmd.detect_local_tb5", lambda **_: (link, nodes))
    monkeypatch.setattr(
        "sanctum_cli.commands.hive_tb5_cmd.probe_tb5_tcp",
        lambda *_a, **_kw: {
            "ok": False,
            "local_to_peer_ok": False,
            "peer_to_local_ok": True,
            "peer_ip": "10.0.5.2",
            "local_ip": "10.0.5.1",
            "rtt_ms": None,
            "firewall_blocked_on_peer": True,
            "firewall_fix": "Run on mbp: sudo socketfilterfw --unblockapp ...",
        },
    )

    runner = CliRunner()
    res = runner.invoke(cli_app, ["hive", "tb5", "status", "--probe-tcp"])
    assert res.exit_code == 0, res.output
    assert "macOS Firewall Diagnostic" in res.output
    assert "socketfilterfw" in res.output
