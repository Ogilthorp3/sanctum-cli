"""Military-grade Hive Compute Grid coordinator, HTTP server, and typed client.

Architecture mirrors ``sanctum_cli.mesh.tracker``:
1. ``SwarmCoordinator`` — pure, deterministic, zero-I/O state machine enforcing
   zero-RCE payload gates, PQ signatures, RAM eligibility, TTL leases, Byzantine
   quorum consensus, DiLoCo trimmed-median gradient filtering, and BitTorrent-style
   reciprocal GPU-second credits.
2. ``build_swarm_app`` — loopback-safe ``aiohttp`` HTTP/JSON coordinator meant to
   sit behind ``sanctum-pq-terminator`` (``X25519MLKEM768`` / ``--psk-file``).
3. ``HttpSwarmClient`` — honest-verify ``httpx`` client that never launders errors.
"""

from __future__ import annotations

import hmac
import json
import statistics
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import httpx
from aiohttp import web

from sanctum_cli.errors import LocalError
from sanctum_cli.hive.swarm_crypto import (
    UniversalSwarmSigner,
    verify_diloco_delta,
    verify_packet,
    verify_project,
    verify_result,
)
from sanctum_cli.hive.swarm_types import (
    ConsensusMode,
    CreditAccount,
    DiLoCoDelta,
    DiLoCoRoundSummary,
    PacketStatus,
    SwarmProject,
    TaskKind,
    TrustZone,
    WorkerNode,
    WorkPacket,
    WorkResult,
    sha256_canonical,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from sanctum_cli.mesh.identity import Signer

__all__ = [
    "DEFAULT_LEASE_TTL_SEC",
    "DEFAULT_SWARM_PORT",
    "HttpSwarmClient",
    "SwarmCoordinator",
    "build_swarm_app",
]

DEFAULT_SWARM_PORT = 8788
DEFAULT_LEASE_TTL_SEC = 300.0
_MAX_HTTP_BODY_BYTES = 256 * 1024
_WORKER_ONLINE_WINDOW_SEC = 600.0
_REPUTATION_SLASH = 0.25
_REPUTATION_GAIN = 0.05
_PROBATION_THRESHOLD = 0.40
_DILOCO_L2_OUTLIER_FACTOR = 3.0
_SCORE_CONSENSUS_TOLERANCE = 0.15
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass
class _PacketState:
    packet: WorkPacket
    active_leases: dict[str, float]  # worker_pubkey -> expires_epoch
    submissions: dict[str, WorkResult]  # worker_pubkey -> verified submission
    accepted_result: WorkResult | None = None


class SwarmCoordinator:
    """In-memory, Byzantine-resilient Hive Compute Grid scheduler & verifier."""

    def __init__(
        self,
        *,
        verifier: Signer | None = None,
        trusted_pubkeys: Iterable[str] = (),
        lease_ttl_sec: float = DEFAULT_LEASE_TTL_SEC,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._verifier: Signer = verifier or UniversalSwarmSigner(prefer_pq=False)
        self._trusted_pubkeys: set[str] = set(trusted_pubkeys)
        self._lease_ttl_sec = max(5.0, float(lease_ttl_sec))
        self._clock = clock

        self._workers: dict[str, WorkerNode] = {}
        self._projects: dict[str, SwarmProject] = {}
        self._project_packets: dict[str, list[str]] = {}
        self._packets: dict[str, _PacketState] = {}
        self._ledger: dict[str, CreditAccount] = {}
        self._diloco_deltas: dict[tuple[str, int], dict[str, DiLoCoDelta]] = {}
        self._diloco_summaries: dict[tuple[str, int], DiLoCoRoundSummary] = {}

    def add_trusted_pubkey(self, pubkey: str) -> None:
        """Authorize a peer's public key for ``TRUSTED_CIRCLE`` projects."""
        self._trusted_pubkeys.add(pubkey)

    def _ensure_account(self, pubkey: str, node_id: str = "") -> CreditAccount:
        acct = self._ledger.get(pubkey)
        if acct is None:
            acct = CreditAccount(pubkey=pubkey, node_id=node_id or pubkey[:12])
            self._ledger[pubkey] = acct
        elif node_id and acct.node_id != node_id:
            acct = replace(acct, node_id=node_id)
            self._ledger[pubkey] = acct
        return acct

    def _reward_worker(self, pubkey: str, gpu_seconds: float, *, diloco: bool = False) -> None:
        acct = self._ensure_account(pubkey)
        self._ledger[pubkey] = replace(
            acct,
            contributed_gpu_sec=acct.contributed_gpu_sec + max(0.0, gpu_seconds),
            verified_packets=acct.verified_packets + (0 if diloco else 1),
            diloco_deltas_accepted=acct.diloco_deltas_accepted + (1 if diloco else 0),
            reputation=min(1.0, acct.reputation + _REPUTATION_GAIN),
        )

    def _charge_creator(self, pubkey: str, gpu_seconds: float) -> None:
        acct = self._ensure_account(pubkey)
        self._ledger[pubkey] = replace(
            acct,
            consumed_gpu_sec=acct.consumed_gpu_sec + max(0.0, gpu_seconds),
        )

    def _slash_worker(self, pubkey: str) -> None:
        acct = self._ensure_account(pubkey)
        self._ledger[pubkey] = replace(
            acct,
            rejected_packets=acct.rejected_packets + 1,
            reputation=max(0.0, acct.reputation - _REPUTATION_SLASH),
        )

    def register_worker(
        self,
        *,
        node_id: str,
        pubkey: str,
        chip: str,
        memory_gb: int,
        trust_zone: TrustZone = TrustZone.OPEN_HIVE,
        supported_kinds: Sequence[TaskKind] = (
            TaskKind.INFERENCE_MAP,
            TaskKind.EVAL_BENCHMARK,
            TaskKind.DILOCO_STEP,
        ),
        psk_fingerprint: str = "",
    ) -> WorkerNode:
        """Register or heartbeat an Apple Silicon compute worker."""
        if not node_id.strip() or not pubkey.strip():
            msg = "node_id and pubkey are required"
            raise ValueError(msg)
        if trust_zone == TrustZone.TRUSTED_CIRCLE and pubkey not in self._trusted_pubkeys:
            msg = "worker pubkey is not pinned in this coordinator's trusted_circle allowlist"
            raise PermissionError(msg)
        node = WorkerNode(
            node_id=node_id.strip(),
            pubkey=pubkey,
            chip=chip.strip() or "Apple Silicon",
            memory_gb=max(4, int(memory_gb)),
            trust_zone=trust_zone,
            supported_kinds=tuple(dict.fromkeys(supported_kinds)),
            last_seen_epoch=self._clock(),
            psk_fingerprint=psk_fingerprint.strip(),
        )
        self._workers[pubkey] = node
        self._ensure_account(pubkey, node_id=node.node_id)
        return node

    def submit_project(
        self,
        project: SwarmProject,
        packets: Sequence[WorkPacket],
    ) -> dict[str, Any]:
        """Validate signatures + zero-RCE payload rules and admit a project & its packets."""
        if project.project_id in self._projects:
            msg = f"project {project.project_id!r} already exists"
            raise ValueError(msg)
        if not verify_project(project, verifier=self._verifier):
            msg = "project manifest signature or policy check failed"
            raise ValueError(msg)
        if (
            project.trust_zone == TrustZone.TRUSTED_CIRCLE
            and project.creator_pubkey not in self._trusted_pubkeys
        ):
            msg = "creator pubkey not authorized for trusted_circle project"
            raise PermissionError(msg)
        if not packets:
            msg = "project must contain at least one work packet"
            raise ValueError(msg)

        seen_ids: set[str] = set()
        for pkt in packets:
            if pkt.project_id != project.project_id:
                msg = f"packet {pkt.packet_id!r} project_id mismatch"
                raise ValueError(msg)
            if pkt.kind != project.kind:
                msg = f"packet {pkt.packet_id!r} kind {pkt.kind.value!r} != project kind {project.kind.value!r}"
                raise ValueError(msg)
            if pkt.creator_pubkey != project.creator_pubkey:
                msg = f"packet {pkt.packet_id!r} creator_pubkey does not match project creator"
                raise ValueError(msg)
            if pkt.packet_id in seen_ids or pkt.packet_id in self._packets:
                msg = f"duplicate packet_id {pkt.packet_id!r}"
                raise ValueError(msg)
            ok, reason = verify_packet(pkt, verifier=self._verifier)
            if not ok:
                msg = f"packet {pkt.packet_id!r} rejected: {reason}"
                raise ValueError(msg)
            seen_ids.add(pkt.packet_id)

        self._projects[project.project_id] = project
        self._project_packets[project.project_id] = []
        for pkt in packets:
            self._project_packets[project.project_id].append(pkt.packet_id)
            self._packets[pkt.packet_id] = _PacketState(
                packet=pkt,
                active_leases={},
                submissions={},
            )
        self._ensure_account(project.creator_pubkey)
        return {
            "ok": True,
            "project_id": project.project_id,
            "packets_admitted": len(packets),
            "trust_zone": project.trust_zone.value,
            "redundancy": project.redundancy,
        }

    def _reap_expired_leases(self, now: float) -> None:
        for st in self._packets.values():
            if st.packet.status in (PacketStatus.COMPLETED, PacketStatus.REJECTED):
                continue
            expired = [w for w, exp in st.active_leases.items() if exp <= now]
            for w in expired:
                del st.active_leases[w]
            if not st.active_leases and not st.submissions and st.packet.status == PacketStatus.LEASED:
                st.packet = replace(st.packet, status=PacketStatus.PENDING, assigned_workers=())

    def claim_packet(
        self,
        worker_pubkey: str,
        *,
        available_ram_gb: int | None = None,
        supported_kinds: Sequence[TaskKind] | None = None,
    ) -> dict[str, Any] | None:
        """Lease the highest-priority eligible packet for ``worker_pubkey``."""
        now = self._clock()
        self._reap_expired_leases(now)

        worker = self._workers.get(worker_pubkey)
        if worker is None:
            msg = "worker must register before claiming packets"
            raise PermissionError(msg)
        acct = self._ensure_account(worker_pubkey, node_id=worker.node_id)
        if acct.reputation < _PROBATION_THRESHOLD:
            msg = f"worker quarantined on probation (reputation={acct.reputation:.2f})"
            raise PermissionError(msg)

        ram_cap = available_ram_gb if available_ram_gb is not None else worker.memory_gb
        kinds_ok = set(supported_kinds) if supported_kinds is not None else set(worker.supported_kinds)

        # Prioritize projects whose creator has higher reciprocal net compute credit
        eligible_projects = sorted(
            self._projects.values(),
            key=lambda p: (
                -self._ensure_account(p.creator_pubkey).net_gpu_sec,
                p.created_at,
                p.project_id,
            ),
        )

        for project in eligible_projects:
            if project.kind not in kinds_ok:
                continue
            if project.required_ram_gb > ram_cap:
                continue
            if (
                project.trust_zone == TrustZone.TRUSTED_CIRCLE
                and worker_pubkey not in self._trusted_pubkeys
            ):
                continue

            for pid in self._project_packets[project.project_id]:
                st = self._packets[pid]
                if st.packet.status in (PacketStatus.COMPLETED, PacketStatus.REJECTED):
                    continue
                if worker_pubkey in st.submissions or worker_pubkey in st.active_leases:
                    continue
                in_flight = len(st.submissions) + len(st.active_leases)
                if in_flight >= project.redundancy:
                    continue

                expires = now + self._lease_ttl_sec
                st.active_leases[worker_pubkey] = expires
                assigned = tuple(sorted(set(st.active_leases) | set(st.submissions)))
                st.packet = replace(
                    st.packet,
                    status=PacketStatus.VERIFYING if st.submissions else PacketStatus.LEASED,
                    assigned_workers=assigned,
                )
                self._workers[worker_pubkey] = replace(worker, last_seen_epoch=now)
                return {
                    "project": project.to_dict(),
                    "packet": st.packet.to_dict(),
                    "lease_expires_epoch": expires,
                }
        return None

    def submit_result(self, result: WorkResult) -> dict[str, Any]:
        """Verify a signed worker result, run Byzantine consensus, and settle credits."""
        st = self._packets.get(result.packet_id)
        if st is None or st.packet.project_id != result.project_id:
            msg = f"unknown packet {result.packet_id!r}"
            raise ValueError(msg)
        project = self._projects[result.project_id]

        ok, reason = verify_result(result, verifier=self._verifier)
        if not ok:
            self._slash_worker(result.worker_pubkey)
            return {
                "ok": False,
                "status": "rejected",
                "reason": reason,
                "packet_status": st.packet.status.value,
            }

        if result.worker_pubkey not in st.active_leases:
            msg = "worker does not hold an active lease for this packet"
            raise PermissionError(msg)
        if st.packet.status in (PacketStatus.COMPLETED, PacketStatus.REJECTED):
            return {
                "ok": True,
                "status": "already_finalized",
                "packet_status": st.packet.status.value,
            }

        del st.active_leases[result.worker_pubkey]
        st.submissions[result.worker_pubkey] = result

        if len(st.submissions) < project.redundancy:
            st.packet = replace(st.packet, status=PacketStatus.VERIFYING)
            return {
                "ok": True,
                "status": "awaiting_quorum",
                "received": len(st.submissions),
                "required": project.redundancy,
                "packet_status": st.packet.status.value,
            }

        # Quorum reached: reconcile submissions
        return self._reconcile_packet(project, st)

    def _reconcile_packet(self, project: SwarmProject, st: _PacketState) -> dict[str, Any]:
        subs = list(st.submissions.values())
        if project.redundancy == 1:
            winner = subs[0]
            st.accepted_result = winner
            st.packet = replace(st.packet, status=PacketStatus.COMPLETED)
            self._reward_worker(winner.worker_pubkey, winner.gpu_seconds)
            self._charge_creator(project.creator_pubkey, winner.gpu_seconds)
            return {
                "ok": True,
                "status": "completed",
                "packet_status": PacketStatus.COMPLETED.value,
                "accepted_workers": [winner.worker_pubkey],
                "slashed_workers": [],
                "output_hash": winner.output_hash,
            }

        accepted: list[WorkResult] = []
        slashed: list[WorkResult] = []

        if project.consensus_mode == ConsensusMode.SCORE_TOLERANCE and all(
            r.eval_score is not None for r in subs
        ):
            scores = [float(r.eval_score or 0.0) for r in subs]
            med = statistics.median(scores)
            for r in subs:
                if abs(float(r.eval_score or 0.0) - med) <= _SCORE_CONSENSUS_TOLERANCE:
                    accepted.append(r)
                else:
                    slashed.append(r)
        else:
            counts = Counter(r.output_hash for r in subs)
            top_hash, top_count = counts.most_common(1)[0]
            quorum_needed = (project.redundancy // 2) + 1
            if top_count >= quorum_needed:
                for r in subs:
                    if r.output_hash == top_hash:
                        accepted.append(r)
                    else:
                        slashed.append(r)
            else:
                # No consensus — slash no one yet? Actually total disagreement rejects packet
                # so the operator sees honest failure rather than silently picking garbage.
                st.packet = replace(st.packet, status=PacketStatus.REJECTED)
                for r in subs:
                    self._slash_worker(r.worker_pubkey)
                return {
                    "ok": False,
                    "status": "no_consensus",
                    "packet_status": PacketStatus.REJECTED.value,
                    "accepted_workers": [],
                    "slashed_workers": [r.worker_pubkey for r in subs],
                }

        for r in accepted:
            self._reward_worker(r.worker_pubkey, r.gpu_seconds)
            self._charge_creator(project.creator_pubkey, r.gpu_seconds)
        for r in slashed:
            self._slash_worker(r.worker_pubkey)

        st.accepted_result = accepted[0]
        st.packet = replace(st.packet, status=PacketStatus.COMPLETED)
        return {
            "ok": True,
            "status": "completed",
            "packet_status": PacketStatus.COMPLETED.value,
            "accepted_workers": [r.worker_pubkey for r in accepted],
            "slashed_workers": [r.worker_pubkey for r in slashed],
            "output_hash": st.accepted_result.output_hash,
        }

    def submit_diloco_delta(self, delta: DiLoCoDelta) -> dict[str, Any]:
        """Accept a signed DiLoCo federated LoRA delta into a round staging bucket."""
        project = self._projects.get(delta.project_id)
        if project is None or project.kind != TaskKind.DILOCO_STEP:
            msg = f"unknown or non-DiLoCo project {delta.project_id!r}"
            raise ValueError(msg)
        if (
            project.trust_zone == TrustZone.TRUSTED_CIRCLE
            and delta.worker_pubkey not in self._trusted_pubkeys
        ):
            msg = "worker pubkey not authorized for trusted_circle DiLoCo project"
            raise PermissionError(msg)

        ok, reason = verify_diloco_delta(delta, verifier=self._verifier)
        if not ok:
            self._slash_worker(delta.worker_pubkey)
            return {"ok": False, "accepted": False, "reason": reason}

        # Local improvement gate: never let a regressed delta dilute the Hive champion
        if delta.eval_loss_after >= delta.eval_loss_before:
            self._slash_worker(delta.worker_pubkey)
            return {
                "ok": False,
                "accepted": False,
                "reason": "no_eval_loss_improvement",
            }

        bucket = self._diloco_deltas.setdefault((delta.project_id, delta.round_num), {})
        bucket[delta.worker_pubkey] = delta
        return {
            "ok": True,
            "accepted": True,
            "project_id": delta.project_id,
            "round_num": delta.round_num,
            "staged_deltas": len(bucket),
        }

    def finalize_diloco_round(self, project_id: str, round_num: int) -> DiLoCoRoundSummary:
        """Apply Byzantine trimmed-median norm guard and emit deterministic FedAvg mixing weights."""
        key = (project_id, round_num)
        deltas = list(self._diloco_deltas.get(key, {}).values())
        if not deltas:
            msg = f"no staged DiLoCo deltas for {project_id!r} round {round_num}"
            raise ValueError(msg)

        project = self._projects[project_id]
        rejected: dict[str, str] = {}
        accepted: list[DiLoCoDelta] = []

        # Byzantine gradient-explosion guard: reject L2 norm > 3x median when >= 3 deltas exist
        if len(deltas) >= 3:
            med_norm = statistics.median(d.delta_l2_norm for d in deltas)
            ceiling = med_norm * _DILOCO_L2_OUTLIER_FACTOR
            for d in deltas:
                if d.delta_l2_norm > ceiling:
                    rejected[d.worker_pubkey] = (
                        f"byzantine_l2_outlier ({d.delta_l2_norm:.3f} > {ceiling:.3f})"
                    )
                    self._slash_worker(d.worker_pubkey)
                else:
                    accepted.append(d)
        else:
            accepted = list(deltas)

        if not accepted:
            msg = "all DiLoCo deltas were rejected by Byzantine norm guard"
            raise ValueError(msg)

        total_samples = sum(d.num_samples for d in accepted)
        weights = {
            d.worker_pubkey: round(d.num_samples / total_samples, 6)
            for d in sorted(accepted, key=lambda x: x.worker_pubkey)
        }
        mean_imp = sum(d.eval_loss_before - d.eval_loss_after for d in accepted) / len(accepted)

        for d in accepted:
            self._reward_worker(d.worker_pubkey, d.gpu_seconds, diloco=True)
            self._charge_creator(project.creator_pubkey, d.gpu_seconds)

        round_payload = {
            "project_id": project_id,
            "round_num": round_num,
            "weights": weights,
            "delta_hashes": {d.worker_pubkey: d.delta_hash for d in accepted},
        }
        summary = DiLoCoRoundSummary(
            project_id=project_id,
            round_num=round_num,
            accepted_workers=tuple(sorted(d.worker_pubkey for d in accepted)),
            rejected_workers=rejected,
            mixing_weights=weights,
            total_samples=total_samples,
            mean_loss_improvement=mean_imp,
            round_hash=sha256_canonical(round_payload),
        )
        self._diloco_summaries[key] = summary
        return summary

    def project_results(self, project_id: str) -> list[dict[str, Any]]:
        """Return ordered completed results for ``project_id``."""
        if project_id not in self._project_packets:
            msg = f"unknown project {project_id!r}"
            raise ValueError(msg)
        out: list[dict[str, Any]] = []
        for pid in self._project_packets[project_id]:
            st = self._packets[pid]
            out.append(
                {
                    "packet_id": pid,
                    "seq": st.packet.seq,
                    "status": st.packet.status.value,
                    "result": st.accepted_result.to_dict() if st.accepted_result else None,
                }
            )
        return out

    def ledger_snapshot(self) -> list[dict[str, Any]]:
        """Return reciprocal credit accounts ordered by net GPU-seconds descending."""
        accounts = sorted(
            self._ledger.values(),
            key=lambda a: (-a.net_gpu_sec, -a.reputation, a.node_id),
        )
        return [a.to_dict() for a in accounts]

    def status(self) -> dict[str, Any]:
        """Return live collective cluster metrics across all registered Apple Silicon Macs."""
        now = self._clock()
        self._reap_expired_leases(now)

        online = [
            w for w in self._workers.values() if (now - w.last_seen_epoch) <= _WORKER_ONLINE_WINDOW_SEC
        ]
        counts: dict[str, int] = {s.value: 0 for s in PacketStatus}
        for st in self._packets.values():
            counts[st.packet.status.value] += 1

        total_gpu_sec = sum(a.contributed_gpu_sec for a in self._ledger.values())
        total_rejected = sum(a.rejected_packets for a in self._ledger.values())

        projects_summary = []
        for proj in self._projects.values():
            pids = self._project_packets[proj.project_id]
            done = sum(
                1 for pid in pids if self._packets[pid].packet.status == PacketStatus.COMPLETED
            )
            projects_summary.append(
                {
                    "project_id": proj.project_id,
                    "title": proj.title,
                    "kind": proj.kind.value,
                    "trust_zone": proj.trust_zone.value,
                    "base_model": proj.base_model,
                    "redundancy": proj.redundancy,
                    "packets_total": len(pids),
                    "packets_completed": done,
                }
            )

        return {
            "ok": True,
            "online_workers": len(online),
            "total_workers": len(self._workers),
            "collective_ram_gb": sum(w.memory_gb for w in online),
            "workers": [w.to_dict() for w in online],
            "projects_count": len(self._projects),
            "projects": projects_summary,
            "packets": counts,
            "collective_gpu_seconds": round(total_gpu_sec, 4),
            "byzantine_rejections": total_rejected,
            "diloco_rounds_finalized": len(self._diloco_summaries),
        }


# ─── HTTP Coordinator Server (aiohttp) ───────────────────────────────────────


def _check_auth(request: web.Request, auth_token: str | None) -> bool:
    if not auth_token:
        return True
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {auth_token}"
    return hmac.compare_digest(header, expected)


async def _read_json_bounded(request: web.Request) -> dict[str, Any]:
    raw = await request.read()
    if len(raw) > _MAX_HTTP_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=_MAX_HTTP_BODY_BYTES, actual_size=len(raw))
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise web.HTTPBadRequest(text="expected JSON object")
    return parsed


def build_swarm_app(
    coordinator: SwarmCoordinator,
    *,
    auth_token: str | None = None,
) -> web.Application:
    """Create the loopback-safe ``aiohttp`` web application for a Hive Compute Grid coordinator."""
    app = web.Application(client_max_size=_MAX_HTTP_BODY_BYTES + 4096)

    @web.middleware
    async def auth_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Any],
    ) -> web.StreamResponse:
        if request.path != "/health" and not _check_auth(request, auth_token):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        resp: web.StreamResponse = await handler(request)
        return resp

    app.middlewares.append(auth_middleware)

    async def handle_health(_request: web.Request) -> web.Response:
        st = coordinator.status()
        return web.json_response(
            {
                "ok": True,
                "service": "sanctum-hive-swarm",
                "online_workers": st["online_workers"],
                "collective_ram_gb": st["collective_ram_gb"],
            }
        )

    async def handle_status(_request: web.Request) -> web.Response:
        return web.json_response(coordinator.status())

    async def handle_ledger(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "accounts": coordinator.ledger_snapshot()})

    async def handle_register(request: web.Request) -> web.Response:
        body = await _read_json_bounded(request)
        try:
            node = coordinator.register_worker(
                node_id=str(body["node_id"]),
                pubkey=str(body["pubkey"]),
                chip=str(body.get("chip", "Apple Silicon")),
                memory_gb=int(body.get("memory_gb", 16)),
                trust_zone=TrustZone(str(body.get("trust_zone", TrustZone.OPEN_HIVE.value))),
                supported_kinds=tuple(
                    TaskKind(str(k))
                    for k in (body.get("supported_kinds") or [TaskKind.INFERENCE_MAP.value])
                ),
                psk_fingerprint=str(body.get("psk_fingerprint", "")),
            )
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=403)
        except (KeyError, ValueError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "worker": node.to_dict()})

    async def handle_submit_project(request: web.Request) -> web.Response:
        body = await _read_json_bounded(request)
        try:
            project = SwarmProject.from_dict(body["project"])
            packets = [WorkPacket.from_dict(p) for p in body.get("packets", [])]
            res = coordinator.submit_project(project, packets)
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=403)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(res)

    async def handle_claim(request: web.Request) -> web.Response:
        body = await _read_json_bounded(request)
        try:
            worker_pubkey = str(body["worker_pubkey"])
            ram = int(body["available_ram_gb"]) if "available_ram_gb" in body else None
            raw_kinds = body.get("supported_kinds")
            kinds = tuple(TaskKind(str(k)) for k in raw_kinds) if raw_kinds is not None else None
            claimed = coordinator.claim_packet(
                worker_pubkey, available_ram_gb=ram, supported_kinds=kinds
            )
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=403)
        except (KeyError, ValueError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        if claimed is None:
            return web.json_response({"ok": True, "claimed": None})
        return web.json_response({"ok": True, "claimed": claimed})

    async def handle_submit_result(request: web.Request) -> web.Response:
        body = await _read_json_bounded(request)
        try:
            result = WorkResult.from_dict(body)
            res = coordinator.submit_result(result)
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=403)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(res, status=200 if res.get("ok") else 422)

    async def handle_diloco_delta(request: web.Request) -> web.Response:
        body = await _read_json_bounded(request)
        try:
            delta = DiLoCoDelta.from_dict(body)
            res = coordinator.submit_diloco_delta(delta)
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=403)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(res, status=200 if res.get("ok") else 422)

    async def handle_diloco_finalize(request: web.Request) -> web.Response:
        body = await _read_json_bounded(request)
        try:
            summary = coordinator.finalize_diloco_round(
                str(body["project_id"]), int(body["round_num"])
            )
        except (KeyError, ValueError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "summary": summary.to_dict()})

    async def handle_project_results(request: web.Request) -> web.Response:
        pid = request.match_info["project_id"]
        try:
            results = coordinator.project_results(pid)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        return web.json_response({"ok": True, "project_id": pid, "results": results})

    app.add_routes(
        [
            web.get("/health", handle_health),
            web.get("/status", handle_status),
            web.get("/ledger", handle_ledger),
            web.post("/workers/register", handle_register),
            web.post("/projects", handle_submit_project),
            web.get("/projects/{project_id}/results", handle_project_results),
            web.post("/packets/claim", handle_claim),
            web.post("/packets/result", handle_submit_result),
            web.post("/diloco/delta", handle_diloco_delta),
            web.post("/diloco/finalize", handle_diloco_finalize),
        ]
    )
    return app


def validate_bind_host(host: str, auth_token: str | None) -> None:
    """Fail closed if an operator binds non-loopback without a bearer token."""
    if host not in _LOOPBACK_HOSTS and not (auth_token and auth_token.strip()):
        msg = (
            f"refusing to bind swarm coordinator on non-loopback host {host!r} without --token "
            "(place behind sanctum-pq-terminator or pass --token)"
        )
        raise LocalError(msg, fix="bind 127.0.0.1 behind sanctum-pq-terminator or pass --token")


# ─── Typed HTTP Client (httpx) ───────────────────────────────────────────────


class HttpSwarmClient:
    """Honest-verify HTTP client for a Sanctum Hive Compute Grid coordinator."""

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str | None = None,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers: dict[str, str] = {"Accept": "application/json"}
        if auth_token:
            self._headers["Authorization"] = f"Bearer {auth_token}"
        self._timeout = timeout
        self._transport = transport

    def _request(self, method: str, path: str, json_body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(
                timeout=self._timeout,
                headers=self._headers,
                transport=self._transport,
            ) as client:
                resp = client.request(method, url, json=dict(json_body) if json_body is not None else None)
        except httpx.HTTPError as exc:
            msg = f"swarm coordinator unreachable at {url}: {exc}"
            raise LocalError(msg, fix="check `sanctum hive swarm serve` or --coordinator URL") from exc

        try:
            data: Any = resp.json()
        except ValueError as exc:
            msg = f"swarm coordinator returned non-JSON ({resp.status_code})"
            raise LocalError(msg) from exc
        if not isinstance(data, dict):
            msg = f"swarm coordinator returned unexpected JSON shape ({type(data).__name__})"
            raise LocalError(msg)
        if resp.status_code >= 400 and resp.status_code != 422:
            err = data.get("error", f"HTTP {resp.status_code}")
            msg = f"swarm coordinator rejected {method} {path}: {err}"
            raise LocalError(msg)
        return data

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/status")

    def ledger(self) -> dict[str, Any]:
        return self._request("GET", "/ledger")

    def register_worker(self, worker_payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/workers/register", worker_payload)

    def submit_project(self, project: SwarmProject, packets: Sequence[WorkPacket]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/projects",
            {"project": project.to_dict(), "packets": [p.to_dict() for p in packets]},
        )

    def claim_packet(
        self,
        worker_pubkey: str,
        *,
        available_ram_gb: int | None = None,
        supported_kinds: Sequence[TaskKind] | None = None,
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {"worker_pubkey": worker_pubkey}
        if available_ram_gb is not None:
            body["available_ram_gb"] = available_ram_gb
        if supported_kinds is not None:
            body["supported_kinds"] = [k.value for k in supported_kinds]
        resp = self._request("POST", "/packets/claim", body)
        claimed = resp.get("claimed")
        return claimed if isinstance(claimed, dict) else None

    def submit_result(self, result: WorkResult) -> dict[str, Any]:
        return self._request("POST", "/packets/result", result.to_dict())

    def submit_diloco_delta(self, delta: DiLoCoDelta) -> dict[str, Any]:
        return self._request("POST", "/diloco/delta", delta.to_dict())

    def finalize_diloco_round(self, project_id: str, round_num: int) -> dict[str, Any]:
        return self._request(
            "POST",
            "/diloco/finalize",
            {"project_id": project_id, "round_num": round_num},
        )
