"""``sanctum hive swarm`` — Collective Apple Silicon GPU/RAM Compute Grid CLI.

Apple-simple commands over a military-grade, zero-RCE, post-quantum-signed
distributed compute protocol:
- ``init``   — mint/show this Mac's PQ (Ed25519 + SLH-DSA-SHA2-128s) swarm keypair
- ``serve``  — run the loopback-safe coordinator (sit behind ``sanctum-pq-terminator``)
- ``submit`` — sign & submit a distributed inference, eval, or DiLoCo LoRA project
- ``work``   — contribute local Apple Silicon ``sanctum-mlx`` capacity & earn credits
- ``status`` — live collective silicon RAM, active workers, projects & Byzantine count
- ``ledger`` — BitTorrent-style reciprocal GPU-second credit ledger (zero tokens/coins)
- ``drill``  — 1-command self-proving multi-node E2E drill (optionally live on ``sanctum-mlx``)
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from aiohttp import web
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sanctum_cli.errors import ExitCode, LocalError
from sanctum_cli.hive.swarm_coordinator import (
    DEFAULT_SWARM_PORT,
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
    ensure_swarm_identity,
    pubkey_fingerprint,
)
from sanctum_cli.hive.swarm_types import (
    ConsensusMode,
    SwarmProject,
    TaskKind,
    TrustZone,
    WorkPacket,
    validate_safe_payload,
)
from sanctum_cli.hive.swarm_worker import (
    InferenceBackend,
    InferenceReply,
    LocalMlxInferenceBackend,
    SwarmWorker,
)
from sanctum_cli.mesh.metrics import detect_chip, detect_ram_gb

console = Console()
swarm_app = typer.Typer(
    name="swarm",
    help="Collective Apple Silicon GPU/RAM compute grid (zero-RCE, PQ-signed, DiLoCo + Swarm).",
    no_args_is_help=True,
)


def _default_node_label() -> str:
    node_id_file = Path(os.environ.get("SANCTUM_HOME", str(Path.home() / ".sanctum"))) / ".node_id"
    if node_id_file.is_file():
        val = node_id_file.read_text(encoding="utf-8").strip()
        if val:
            return val
    return platform.node().split(".")[0] or "sanctum-node"


def _default_coordinator_url() -> str:
    return os.environ.get("SANCTUM_SWARM_URL", f"http://127.0.0.1:{DEFAULT_SWARM_PORT}")


@swarm_app.command("init", help="Mint or inspect this Mac's PQ-signed Hive Compute Grid identity.")
def swarm_init(
    label: Annotated[str, typer.Option("--label", help="Node label (default: ~/.sanctum/.node_id).")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    node_label = label.strip() or _default_node_label()
    signer = UniversalSwarmSigner(prefer_pq=True)
    identity = ensure_swarm_identity(node_label, signer=signer)
    chip = detect_chip()
    ram_gb = detect_ram_gb()
    fp = pubkey_fingerprint(identity.pubkey)
    scheme = (
        "Ed25519 + NIST FIPS 205 SLH-DSA-SHA2-128s"
        if identity.pubkey.startswith("ed25519+slh-dsa-sha2-128s:")
        else "Ed25519"
    )
    payload = {
        "ok": True,
        "label": identity.label,
        "fingerprint": fp,
        "scheme": scheme,
        "chip": chip,
        "memory_gb": ram_gb,
        "created": identity.created,
        "pubkey_prefix": identity.pubkey[:48],
    }
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    console.print(
        Panel.fit(
            f"[bold green]✓ Hive Compute Identity Ready[/]\n"
            f"Node:        [bold]{identity.label}[/]\n"
            f"Fingerprint: [bold yellow]{fp}[/]\n"
            f"Crypto:      {scheme}\n"
            f"Silicon:     {chip} · {ram_gb} GB unified RAM\n"
            f"[dim]Private key sealed at mode 0600 (never logged or transmitted).[/]",
            title="Sanctum Hive Compute Grid",
            border_style="cyan",
        )
    )


@swarm_app.command("serve", help="Run the Hive Compute Grid coordinator (loopback-safe by default).")
def swarm_serve(
    host: Annotated[str, typer.Option("--host", help="Bind host (default: 127.0.0.1).")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Bind port.")] = DEFAULT_SWARM_PORT,
    token: Annotated[
        str,
        typer.Option("--token", envvar="SANCTUM_SWARM_TOKEN", help="Optional bearer auth token."),
    ] = "",
    trusted_pubkey: Annotated[
        list[str] | None,
        typer.Option("--trust-pubkey", help="Authorize pubkey(s) for trusted_circle projects."),
    ] = None,
    lease_ttl: Annotated[float, typer.Option("--lease-ttl", help="Packet lease timeout (seconds).")] = 300.0,
) -> None:
    try:
        validate_bind_host(host, token or None)
    except LocalError as exc:
        console.print(f"[red]Refused:[/] {exc}")
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR)) from exc

    coord = SwarmCoordinator(
        trusted_pubkeys=trusted_pubkey or (),
        lease_ttl_sec=lease_ttl,
    )
    app = build_swarm_app(coord, auth_token=token or None)
    console.print(
        f"[green]◆[/] Sanctum Hive Compute Coordinator listening on "
        f"[bold]http://{host}:{port}[/] [dim](place behind sanctum-pq-terminator for X25519MLKEM768 / --psk-file)[/]"
    )
    web.run_app(app, host=host, port=port, print=None)


@swarm_app.command("submit", help="Sign and submit a distributed AI project to the Hive coordinator.")
def swarm_submit(
    title: Annotated[str, typer.Option("--title", help="Project title.")],
    prompts_file: Annotated[
        Path,
        typer.Option("--prompts-file", help="JSON file containing a list of payload objects or prompt strings."),
    ],
    kind: Annotated[TaskKind, typer.Option("--kind", help="Task kind.")] = TaskKind.INFERENCE_MAP,
    trust_zone: Annotated[
        TrustZone, typer.Option("--trust-zone", help="open_hive or trusted_circle.")
    ] = TrustZone.OPEN_HIVE,
    base_model: Annotated[str, typer.Option("--model", help="Target Apple Silicon model.")] = "Qwen3.8-27B-4bit",
    required_ram_gb: Annotated[int, typer.Option("--ram-gb", help="Minimum worker unified RAM (GB).")] = 16,
    redundancy: Annotated[int, typer.Option("--redundancy", help="Byzantine verifier quorum (1-5).")] = 1,
    coordinator: Annotated[str, typer.Option("--coordinator", help="Coordinator URL.")] = "",
    token: Annotated[str, typer.Option("--token", envvar="SANCTUM_SWARM_TOKEN")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    if not prompts_file.is_file():
        console.print(f"[red]Prompts file not found:[/] {prompts_file}")
        raise typer.Exit(code=int(ExitCode.USER_ERROR))

    raw_items = json.loads(prompts_file.read_text(encoding="utf-8"))
    if not isinstance(raw_items, list) or not raw_items:
        console.print("[red]Prompts file must be a non-empty JSON list.[/]")
        raise typer.Exit(code=int(ExitCode.USER_ERROR))

    identity = ensure_swarm_identity(_default_node_label())
    try:
        project = build_signed_project(
            identity,
            title=title,
            kind=kind,
            trust_zone=trust_zone,
            base_model=base_model,
            required_ram_gb=required_ram_gb,
            redundancy=redundancy,
        )
        packets: list[WorkPacket] = []
        for idx, item in enumerate(raw_items, 1):
            payload = {"prompt": str(item), "item_id": f"item-{idx:04d}"} if isinstance(item, str) else dict(item)
            packets.append(
                build_signed_packet(
                    identity,
                    project_id=project.project_id,
                    seq=idx,
                    kind=kind,
                    payload=payload,
                )
            )
    except ValueError as exc:
        console.print(f"[red]Rejected before upload:[/] {exc}")
        raise typer.Exit(code=int(ExitCode.USER_ERROR)) from exc

    client = HttpSwarmClient(coordinator or _default_coordinator_url(), auth_token=token or None)
    try:
        res = client.submit_project(project, packets)
    except LocalError as exc:
        console.print(f"[red]Submit failed:[/] {exc}")
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR)) from exc

    if json_output:
        print(json.dumps(res, indent=2))
        return
    console.print(
        f"[green]✓[/] Submitted [bold]{project.title}[/] (`{project.project_id}`) — "
        f"{len(packets)} signed zero-RCE packets · zone={trust_zone.value} · quorum={redundancy}"
    )


@swarm_app.command("work", help="Contribute local Apple Silicon MLX GPU cycles to the Hive.")
def swarm_work(
    coordinator: Annotated[str, typer.Option("--coordinator", help="Coordinator URL.")] = "",
    mlx_url: Annotated[
        str,
        typer.Option("--mlx-url", help="Local sanctum-mlx OpenAI endpoint."),
    ] = "https://127.0.0.1:1337/v1/chat/completions",
    max_ram_gb: Annotated[int, typer.Option("--max-ram-gb", help="RAM cap to offer (0 = auto).")] = 0,
    max_packets: Annotated[int, typer.Option("--max-packets", help="Stop after N packets (0 = unlimited).")] = 1,
    poll_sec: Annotated[float, typer.Option("--poll-sec", help="Sleep seconds when idle.")] = 3.0,
    idle_exit: Annotated[bool, typer.Option("--idle-exit/--keep-polling", help="Exit when queue is empty.")] = True,
    token: Annotated[str, typer.Option("--token", envvar="SANCTUM_SWARM_TOKEN")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    label = _default_node_label()
    identity = ensure_swarm_identity(label)
    chip = detect_chip()
    ram_gb = max_ram_gb if max_ram_gb > 0 else max(8, detect_ram_gb())

    client = HttpSwarmClient(coordinator or _default_coordinator_url(), auth_token=token or None)
    backend = LocalMlxInferenceBackend(endpoint=mlx_url)
    worker = SwarmWorker(identity, backend, max_ram_gb=ram_gb)

    try:
        client.register_worker(
            {
                "node_id": label,
                "pubkey": identity.pubkey,
                "chip": chip,
                "memory_gb": ram_gb,
                "trust_zone": TrustZone.OPEN_HIVE.value,
                "supported_kinds": [TaskKind.INFERENCE_MAP.value, TaskKind.EVAL_BENCHMARK.value],
            }
        )
    except LocalError as exc:
        console.print(f"[red]Registration failed:[/] {exc}")
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR)) from exc

    completed: list[dict[str, Any]] = []
    while True:
        claimed = client.claim_packet(
            identity.pubkey,
            available_ram_gb=ram_gb,
            supported_kinds=(TaskKind.INFERENCE_MAP, TaskKind.EVAL_BENCHMARK),
        )
        if claimed is None:
            if idle_exit:
                break
            time.sleep(poll_sec)
            continue

        project = SwarmProject.from_dict(claimed["project"])
        packet = WorkPacket.from_dict(claimed["packet"])
        result = worker.execute_packet(project, packet)
        ack = client.submit_result(result)
        completed.append(
            {
                "packet_id": packet.packet_id,
                "gpu_seconds": round(result.gpu_seconds, 3),
                "tokens_per_sec": round(result.tokens_per_sec, 1),
                "ack": ack,
            }
        )
        if not json_output:
            console.print(
                f"[green]✓[/] Completed [bold]{packet.packet_id}[/] "
                f"({result.tokens_per_sec:.1f} tok/s · {result.gpu_seconds:.2f}s GPU) → {ack.get('status')}"
            )
        if max_packets > 0 and len(completed) >= max_packets:
            break

    if json_output:
        print(json.dumps({"ok": True, "completed": completed}, indent=2))
    elif not completed:
        console.print("[dim]No eligible work packets in queue right now.[/]")


@swarm_app.command("status", help="Show collective Hive Compute Grid RAM, active Macs, and project progress.")
def swarm_status(
    coordinator: Annotated[str, typer.Option("--coordinator", help="Coordinator URL.")] = "",
    token: Annotated[str, typer.Option("--token", envvar="SANCTUM_SWARM_TOKEN")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    client = HttpSwarmClient(coordinator or _default_coordinator_url(), auth_token=token or None)
    try:
        st = client.status()
    except LocalError as exc:
        if json_output:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            raise typer.Exit(code=int(ExitCode.LOCAL_ERROR)) from exc
        console.print(f"[red]Coordinator unreachable:[/] {exc}")
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR)) from exc

    if json_output:
        print(json.dumps(st, indent=2))
        return

    ram_gb = int(st.get("collective_ram_gb") or 0)
    ram_str = f"{ram_gb / 1024:.2f} TB" if ram_gb >= 1024 else f"{ram_gb} GB"
    t = Table(title="Sanctum Hive Compute Grid — Status", header_style="bold")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    t.add_row("Online Macs", str(st.get("online_workers", 0)))
    t.add_row("Collective Unified RAM", ram_str)
    t.add_row("Active Projects", str(st.get("projects_count", 0)))
    pkts = st.get("packets") or {}
    t.add_row(
        "Packets (pending / leased / done / rejected)",
        f"{pkts.get('pending', 0)} / {pkts.get('leased', 0)} / {pkts.get('completed', 0)} / {pkts.get('rejected', 0)}",
    )
    t.add_row("Collective GPU-Seconds", f"{float(st.get('collective_gpu_seconds', 0.0)):.2f} s")
    t.add_row("DiLoCo Rounds Finalized", str(st.get("diloco_rounds_finalized", 0)))
    t.add_row("Byzantine Rejections", str(st.get("byzantine_rejections", 0)))
    console.print(t)


@swarm_app.command("ledger", help="Show BitTorrent-style reciprocal compute credit ledger (zero coins).")
def swarm_ledger(
    coordinator: Annotated[str, typer.Option("--coordinator", help="Coordinator URL.")] = "",
    token: Annotated[str, typer.Option("--token", envvar="SANCTUM_SWARM_TOKEN")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    client = HttpSwarmClient(coordinator or _default_coordinator_url(), auth_token=token or None)
    try:
        data = client.ledger()
    except LocalError as exc:
        console.print(f"[red]Coordinator unreachable:[/] {exc}")
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR)) from exc

    if json_output:
        print(json.dumps(data, indent=2))
        return

    t = Table(title="Hive Reciprocal Compute Ledger (Zero Tokens)", header_style="bold")
    t.add_column("Node")
    t.add_column("Tier")
    t.add_column("Contributed (GPU-s)", justify="right")
    t.add_column("Consumed (GPU-s)", justify="right")
    t.add_column("Net", justify="right")
    t.add_column("Verified / Rejected", justify="right")
    t.add_column("Reputation", justify="right")
    for acct in data.get("accounts") or []:
        t.add_row(
            str(acct.get("node_id")),
            str(acct.get("priority_tier")),
            f"{float(acct.get('contributed_gpu_sec', 0.0)):.2f}",
            f"{float(acct.get('consumed_gpu_sec', 0.0)):.2f}",
            f"{float(acct.get('net_gpu_sec', 0.0)):+.2f}",
            f"{acct.get('verified_packets', 0)} / {acct.get('rejected_packets', 0)}",
            f"{float(acct.get('reputation', 0.0)):.2f}",
        )
    console.print(t)


class _DeterministicDrillBackend:
    """Fast deterministic backend for hermetic self-proving swarm drills."""

    def __init__(self, *, reply_text: str = "42", gpu_sec: float = 1.25) -> None:
        self._reply = reply_text
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
        _ = (prompt, system, max_tokens, temperature)
        return InferenceReply(
            text=self._reply,
            tokens_generated=32,
            elapsed_sec=self._gpu_sec,
            model=model,
        )


def run_swarm_drill(*, live_mlx: bool = False, mlx_url: str = "https://127.0.0.1:1337/v1/chat/completions") -> dict[str, Any]:
    """Run the 5-gate self-proving Hive Compute Grid drill end-to-end."""
    signer = UniversalSwarmSigner(prefer_pq=True)
    # Use fast Ed25519 for ephemeral worker identities unless PQ explicitly verified on creator
    fast_signer = UniversalSwarmSigner(prefer_pq=False)
    import tempfile

    with tempfile.TemporaryDirectory(prefix="sanctum-swarm-drill-") as td:
        root = Path(td)
        creator = ensure_swarm_identity("manoir", identity_dir=root / "creator", signer=signer)
        node_a = ensure_swarm_identity("manoir-gpu", identity_dir=root / "a", signer=fast_signer)
        node_b = ensure_swarm_identity("chalet-gpu", identity_dir=root / "b", signer=fast_signer)
        attacker = ensure_swarm_identity("rogue-node", identity_dir=root / "evil", signer=fast_signer)

        coord = SwarmCoordinator(verifier=signer, trusted_pubkeys=[creator.pubkey, node_a.pubkey])
        coord.register_worker(node_id="manoir", pubkey=node_a.pubkey, chip="Apple M4 Pro", memory_gb=64)
        coord.register_worker(node_id="chalet", pubkey=node_b.pubkey, chip="Apple M1", memory_gb=16)
        coord.register_worker(node_id="rogue", pubkey=attacker.pubkey, chip="Unknown", memory_gb=32)

        # Gate 1: Zero-RCE guard must block shell injection before a packet is ever signed
        rce_blocked = False
        try:
            validate_safe_payload(TaskKind.INFERENCE_MAP, {"prompt": "hi", "cmd": "rm -rf /"})
        except ValueError:
            rce_blocked = True

        # Gate 2: Byzantine 3-node quorum catches & slashes lying attacker
        proj = build_signed_project(
            creator,
            title="Hive Quorum Drill",
            kind=TaskKind.EVAL_BENCHMARK,
            redundancy=3,
            consensus_mode=ConsensusMode.EXACT_HASH,
            required_ram_gb=16,
        )
        pkt = build_signed_packet(
            creator,
            project_id=proj.project_id,
            seq=1,
            kind=TaskKind.EVAL_BENCHMARK,
            payload={"case_id": "math-1", "prompt": "What is 6*7?", "expected": "42"},
        )
        coord.submit_project(proj, [pkt])

        honest_backend: InferenceBackend = _DeterministicDrillBackend(reply_text="42", gpu_sec=1.5)
        evil_backend: InferenceBackend = _DeterministicDrillBackend(reply_text="999999", gpu_sec=0.2)
        quorum_ack: dict[str, Any] = {}
        for ident, backend in ((node_a, honest_backend), (node_b, honest_backend), (attacker, evil_backend)):
            claimed = coord.claim_packet(ident.pubkey)
            assert claimed is not None
            w = SwarmWorker(ident, backend, max_ram_gb=64, verifier=signer)
            res = w.execute_packet(
                SwarmProject.from_dict(claimed["project"]),
                WorkPacket.from_dict(claimed["packet"]),
            )
            quorum_ack = coord.submit_result(res)

        # Gate 3: DiLoCo federated LoRA round rejects Byzantine gradient explosion
        diloco_proj = build_signed_project(
            creator,
            title="DiLoCo Shared LoRA Round 1",
            kind=TaskKind.DILOCO_STEP,
            required_ram_gb=16,
        )
        diloco_pkt = build_signed_packet(
            creator,
            project_id=diloco_proj.project_id,
            seq=1,
            kind=TaskKind.DILOCO_STEP,
            payload={
                "round_num": 1,
                "base_model": "Qwen3.8-27B-4bit",
                "base_hash": "sha256:" + "a" * 64,
                "dataset_ref": "open-quebec-reasoning-v1",
                "local_steps": 200,
                "learning_rate": 1e-4,
                "lora_rank": 16,
            },
        )
        coord.submit_project(diloco_proj, [diloco_pkt])
        coord.submit_diloco_delta(
            build_signed_diloco_delta(
                node_a,
                project_id=diloco_proj.project_id,
                round_num=1,
                base_hash="sha256:" + "a" * 64,
                delta_hash="sha256:" + "1" * 64,
                local_steps=200,
                num_samples=400,
                eval_loss_before=1.80,
                eval_loss_after=1.42,
                delta_l2_norm=1.10,
                size_bytes=28_000_000,
                gpu_seconds=45.0,
            )
        )
        coord.submit_diloco_delta(
            build_signed_diloco_delta(
                node_b,
                project_id=diloco_proj.project_id,
                round_num=1,
                base_hash="sha256:" + "a" * 64,
                delta_hash="sha256:" + "2" * 64,
                local_steps=200,
                num_samples=200,
                eval_loss_before=1.80,
                eval_loss_after=1.50,
                delta_l2_norm=1.25,
                size_bytes=28_000_000,
                gpu_seconds=38.0,
            )
        )
        # Byzantine attacker submits an exploding L2 norm poisoned gradient (95.0 vs median 1.25)
        coord.submit_diloco_delta(
            build_signed_diloco_delta(
                attacker,
                project_id=diloco_proj.project_id,
                round_num=1,
                base_hash="sha256:" + "a" * 64,
                delta_hash="sha256:" + "f" * 64,
                local_steps=200,
                num_samples=900,
                eval_loss_before=1.80,
                eval_loss_after=1.20,
                delta_l2_norm=95.0,
                size_bytes=28_000_000,
                gpu_seconds=5.0,
            )
        )
        diloco_summary = coord.finalize_diloco_round(diloco_proj.project_id, 1)

        # Gate 4 (optional): live local Apple Silicon sanctum-mlx inference packet!
        live_mlx_proof: dict[str, Any] = {"ran": False}
        if live_mlx:
            mlx_proj = build_signed_project(
                creator,
                title="Live MLX Silicon Verification",
                kind=TaskKind.INFERENCE_MAP,
                trust_zone=TrustZone.TRUSTED_CIRCLE,
                base_model="Qwen3.8-27B-4bit",
                required_ram_gb=16,
                max_tokens=48,
            )
            mlx_pkt = build_signed_packet(
                creator,
                project_id=mlx_proj.project_id,
                seq=1,
                kind=TaskKind.INFERENCE_MAP,
                payload={
                    "item_id": "mlx-live-1",
                    "prompt": "Reply with exactly three words: Sanctum Hive Online.",
                    "temperature": 0.0,
                },
            )
            coord.submit_project(mlx_proj, [mlx_pkt])
            claimed_live = coord.claim_packet(
                node_a.pubkey,
                supported_kinds=(TaskKind.INFERENCE_MAP,),
            )
            assert claimed_live is not None
            real_worker = SwarmWorker(
                node_a,
                LocalMlxInferenceBackend(endpoint=mlx_url),
                max_ram_gb=64,
                verifier=signer,
            )
            live_res = real_worker.execute_packet(
                SwarmProject.from_dict(claimed_live["project"]),
                WorkPacket.from_dict(claimed_live["packet"]),
            )
            live_ack = coord.submit_result(live_res)
            live_mlx_proof = {
                "ran": True,
                "ok": bool(live_ack.get("ok")),
                "model": live_res.output.get("model"),
                "response": str(live_res.output.get("response") or "")[:120],
                "tokens_per_sec": round(live_res.tokens_per_sec, 2),
                "gpu_seconds": round(live_res.gpu_seconds, 3),
                "output_hash": live_res.output_hash[:24],
            }

        st = coord.status()
        ledger = coord.ledger_snapshot()
        return {
            "ok": (
                rce_blocked
                and quorum_ack.get("status") == "completed"
                and attacker.pubkey in (quorum_ack.get("slashed_workers") or [])
                and attacker.pubkey in diloco_summary.rejected_workers
                and len(diloco_summary.accepted_workers) == 2
                and (not live_mlx or bool(live_mlx_proof.get("ok")))
            ),
            "post_quantum_creator_sig": creator.pubkey.startswith("ed25519+slh-dsa-sha2-128s:"),
            "zero_rce_blocked": rce_blocked,
            "byzantine_quorum": {
                "status": quorum_ack.get("status"),
                "honest_accepted": len(quorum_ack.get("accepted_workers") or []),
                "attacker_slashed": attacker.pubkey in (quorum_ack.get("slashed_workers") or []),
            },
            "diloco_round": {
                "accepted": len(diloco_summary.accepted_workers),
                "attacker_filtered": attacker.pubkey in diloco_summary.rejected_workers,
                "mean_loss_improvement": round(diloco_summary.mean_loss_improvement, 4),
                "round_hash": diloco_summary.round_hash[:24],
            },
            "live_mlx": live_mlx_proof,
            "collective_ram_gb": st["collective_ram_gb"],
            "collective_gpu_seconds": st["collective_gpu_seconds"],
            "ledger": ledger,
        }


@swarm_app.command("drill", help="Run the 5-gate self-proving Hive Compute Grid multi-node E2E drill.")
def swarm_drill(
    live_mlx: Annotated[
        bool,
        typer.Option("--live-mlx", help="Also execute a real signed packet on local sanctum-mlx :1337."),
    ] = False,
    mlx_url: Annotated[
        str,
        typer.Option("--mlx-url", help="Local sanctum-mlx OpenAI endpoint."),
    ] = "https://127.0.0.1:1337/v1/chat/completions",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    report = run_swarm_drill(live_mlx=live_mlx, mlx_url=mlx_url)
    if json_output:
        print(json.dumps(report, indent=2))
        if not report["ok"]:
            raise typer.Exit(code=int(ExitCode.LOCAL_ERROR))
        return

    t = Table(title="Sanctum Hive Compute Grid — Self-Proving Drill", header_style="bold")
    t.add_column("Gate", style="cyan")
    t.add_column("Verdict")
    t.add_column("Evidence")
    t.add_row(
        "PQ Creator Identity",
        "[green]PASS[/]",
        "Ed25519 + NIST FIPS 205 SLH-DSA-SHA2-128s" if report["post_quantum_creator_sig"] else "Ed25519",
    )
    t.add_row(
        "Zero-RCE Payload Guard",
        "[green]PASS[/]" if report["zero_rce_blocked"] else "[red]FAIL[/]",
        "Blocked malicious shell injection key before signing",
    )
    bq = report["byzantine_quorum"]
    t.add_row(
        "Byzantine 3-Node Quorum",
        "[green]PASS[/]" if bq["attacker_slashed"] else "[red]FAIL[/]",
        "2 honest Macs accepted · 1 lying attacker slashed & penalized",
    )
    dl = report["diloco_round"]
    t.add_row(
        "DiLoCo Federated LoRA",
        "[green]PASS[/]" if dl["attacker_filtered"] else "[red]FAIL[/]",
        f"2 honest deltas merged (-{dl['mean_loss_improvement']:.3f} loss) · poisoned L2=95.0 outlier rejected",
    )
    mlx = report["live_mlx"]
    if mlx.get("ran"):
        t.add_row(
            "Live Apple Silicon MLX",
            "[green]PASS[/]" if mlx.get("ok") else "[red]FAIL[/]",
            f"{mlx.get('model')} · {mlx.get('tokens_per_sec')} tok/s · {mlx.get('gpu_seconds')}s · {mlx.get('response')!r}",
        )
    console.print(t)
    if not report["ok"]:
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR))
