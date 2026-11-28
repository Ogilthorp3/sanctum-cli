"""``sanctum hive tb5`` — Thunderbolt 5 192 GB Apple Silicon Cluster Mode CLI.

Apple-simple commands over real 80 Gb/s Thunderbolt 5 links:
- ``status`` — inspect live TB5 link speed, RDMA/JACCL status, pooled RAM & model fit
- ``bench``  — run a real 2-Mac GPU ``all_sum`` latency + bandwidth benchmark over TB5
- ``up``     — park local Yoda MLX cleanly (optional), launch 2-Mac pipeline/tensor cluster
- ``down``   — cleanly stop distributed ranks and wake Yoda (``haus.sanctum.mlx``) back up
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sanctum_cli.errors import ExitCode
from sanctum_cli.hive.tb5_cluster import (
    DEFAULT_CLUSTER_PORT,
    DEFAULT_TB5_PEER_HOST,
    ClusterBackend,
    ClusterMode,
    build_mlx_hostfile,
    detect_local_tb5,
    load_cluster_state,
    park_local_mlx_services,
    plan_cluster_sharding,
    probe_tb5_tcp,
    run_live_tb5_bench,
    save_cluster_state,
    stop_tb5_cluster,
    unpark_local_mlx_services,
)

console = Console()
tb5_app = typer.Typer(
    name="tb5",
    help="Thunderbolt 5 Apple Silicon Cluster Mode (pool 64 GB + 128 GB = 192 GB for giant models).",
    no_args_is_help=True,
)


@tb5_app.command("status", help="Inspect live Thunderbolt 5 link, RDMA status, pooled VRAM, and model fit.")
def tb5_status(
    peer: Annotated[str, typer.Option("--peer", help="SSH host alias of TB5 peer Mac.")] = DEFAULT_TB5_PEER_HOST,
    mode: Annotated[ClusterMode | None, typer.Option("--mode", help="Force pipeline or tensor plan.")] = None,
    probe_tcp: Annotated[bool, typer.Option("--probe-tcp/--no-probe-tcp", help="Verify live TCP over TB5.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    link, nodes = detect_local_tb5(peer_host=peer)
    plan_dict: dict[str, Any] | None = None
    if len(nodes) >= 2:
        plan = plan_cluster_sharding(nodes, mode=mode)
        plan_dict = plan.to_dict()
    tcp_res = (
        probe_tb5_tcp(peer, link.peer_ip, local_ip=link.local_ip) if (probe_tcp and len(nodes) >= 2) else None
    )
    state = load_cluster_state()

    out: dict[str, Any] = {
        "ok": link.bridge_up and len(nodes) >= 2,
        "link": link.to_dict(),
        "nodes": [n.to_dict() for n in nodes],
        "plan": plan_dict,
        "tcp_probe": tcp_res,
        "cluster_state": state,
    }
    if json_output:
        print(json.dumps(out, indent=2))
        return

    rdma_str = (
        "[bold green]ENABLED (JACCL <10us RDMA)[/]"
        if (link.rdma_local and link.rdma_peer)
        else "[yellow]DISABLED (using ring over TB5 IP — run `rdma_ctl enable` in Recovery for JACCL)[/]"
    )
    console.print(
        Panel.fit(
            f"Bridge:      {'[green]ACTIVE[/]' if link.bridge_up else '[red]DOWN[/]'} · "
            f"{link.local_ip} <-> {link.peer_ip} · [bold]{link.link_speed_gbps} Gb/s[/]\n"
            f"RDMA/JACCL:  {rdma_str}\n"
            f"Active Run:  {'[bold green]RUNNING[/] (' + str(state.get('model')) + ')' if state.get('active') else '[dim]idle[/]'}",
            title="Sanctum Thunderbolt 5 Cluster",
            border_style="cyan",
        )
    )

    nt = Table(title="Connected Apple Silicon Nodes", header_style="bold")
    nt.add_column("Node")
    nt.add_column("Chip")
    nt.add_column("Unified RAM", justify="right")
    nt.add_column("Usable VRAM", justify="right")
    nt.add_column("Est. BW", justify="right")
    nt.add_column("TB5 IP")
    for n in nodes:
        nt.add_row(
            f"{n.name} {'(local)' if n.is_local else ''}",
            n.chip,
            f"{n.memory_gb} GB",
            f"~{n.usable_vram_gb} GB",
            f"~{n.est_bandwidth_gbps} GB/s",
            n.tb5_ip,
        )
    console.print(nt)

    if plan_dict is not None:
        ratios = " / ".join(f"{round(r * 100)}%" for r in plan_dict["layer_split_ratios"])
        console.print(
            f"[bold]Pooled Pool:[/] {plan_dict['total_ram_gb']} GB total · "
            f"[bold green]~{plan_dict['usable_vram_gb']} GB usable VRAM[/] · "
            f"~{plan_dict['combined_bandwidth_gbps']} GB/s combined · "
            f"mode=[bold]{plan_dict['mode']}[/] ({ratios}) · backend=[bold]{plan_dict['backend']}[/]"
        )
        mt = Table(title="Recommended Giant Models Fitting This TB5 Pool", header_style="bold")
        mt.add_column("Model (HF ID)")
        mt.add_column("Params")
        mt.add_column("VRAM", justify="right")
        mt.add_column("Mode")
        for m in plan_dict["recommendations"]:
            mt.add_row(m["hf_id"], m["params"], f"~{m['est_vram_gb']} GB", m["recommended_mode"])
        console.print(mt)

    if tcp_res is not None:
        if tcp_res.get("ok"):
            console.print(
                f"[bold green]✓ Bidirectional TB5 TCP verified[/] "
                f"({tcp_res['local_ip']} <-> {tcp_res['peer_ip']} · {tcp_res.get('rtt_ms')} ms)"
            )
        elif tcp_res.get("firewall_fix"):
            l2p_s = "[green]OK[/]" if tcp_res.get("local_to_peer_ok") else "[red]BLOCKED[/]"
            p2l_s = "[green]OK[/]" if tcp_res.get("peer_to_local_ok") else "[red]BLOCKED[/]"
            console.print(
                Panel.fit(
                    f"[yellow]Inbound TB5 TCP to mlx-lm Python is blocked by macOS Application Firewall[/]\n"
                    f"({link.local_ip} -> {peer}: {l2p_s} · {peer} -> {link.local_ip}: {p2l_s})\n\n"
                    f"[bold]One-time fix:[/]\n  {tcp_res.get('firewall_fix')}",
                    title="macOS Firewall Diagnostic",
                    border_style="yellow",
                )
            )
        else:
            console.print(
                f"[red]TB5 TCP probe failed:[/] l2p={tcp_res.get('local_to_peer_ok')} "
                f"p2l={tcp_res.get('peer_to_local_ok')} ({tcp_res.get('local_to_peer_error') or tcp_res.get('peer_to_local_error')})"
            )


@tb5_app.command("bench", help="Run a live distributed MLX GPU all_sum latency & bandwidth test across TB5.")
def tb5_bench(
    peer: Annotated[str, typer.Option("--peer", help="SSH host alias of TB5 peer Mac.")] = DEFAULT_TB5_PEER_HOST,
    backend: Annotated[ClusterBackend | None, typer.Option("--backend", help="ring or jaccl.")] = None,
    iters: Annotated[int, typer.Option("--iters", help="Latency iterations.")] = 30,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    link, nodes = detect_local_tb5(peer_host=peer)
    if len(nodes) < 2:
        console.print(f"[red]Peer {peer!r} unreachable or missing TB5 link.[/]")
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR))
    chosen = backend or link.recommended_backend
    res = run_live_tb5_bench(nodes, backend=chosen, iters=iters)
    if json_output:
        print(json.dumps(res, indent=2))
        if not res.get("ok"):
            raise typer.Exit(code=int(ExitCode.LOCAL_ERROR))
        return
    if not res.get("ok"):
        console.print(f"[red]TB5 MLX benchmark failed:[/] {res.get('error')}")
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR))
    console.print(
        Panel.fit(
            f"[bold green]✓ LIVE 2-MAC TB5 METAL GPU COLLECTIVES VERIFIED[/]\n"
            f"World Size:       {res['world_size']} Macs · backend={res['backend']}\n"
            f"16 KB Hop (all):  [bold yellow]{res['hop_16kb_ms']} ms[/]\n"
            f"64 MB Bulk BW:    [bold cyan]{res['bulk_64mb_gbps']} Gbps[/]",
            title="Thunderbolt 5 MLX Benchmark",
            border_style="green",
        )
    )


@tb5_app.command("up", help="Start a 2-Mac Thunderbolt 5 distributed model server (with safe Yoda RAM handoff).")
def tb5_up(
    model: Annotated[str, typer.Option("--model", help="HuggingFace model ID or local path.")],
    peer: Annotated[str, typer.Option("--peer", help="SSH host alias of TB5 peer Mac.")] = DEFAULT_TB5_PEER_HOST,
    mode: Annotated[ClusterMode | None, typer.Option("--mode", help="pipeline (full 192 GB) or tensor.")] = None,
    backend: Annotated[ClusterBackend | None, typer.Option("--backend", help="ring or jaccl.")] = None,
    port: Annotated[int, typer.Option("--port", help="Local OpenAI server port.")] = DEFAULT_CLUSTER_PORT,
    park_local_mlx: Annotated[
        bool,
        typer.Option("--park-local-mlx/--keep-local-mlx", help="Park local MLX daemons to free ~50 GB."),
    ] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print launch command and plan without starting.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    link, nodes = detect_local_tb5(peer_host=peer)
    if len(nodes) < 2:
        console.print(f"[red]Peer {peer!r} unreachable over SSH/TB5.[/]")
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR))

    plan = plan_cluster_sharding(nodes, mode=mode, backend=backend or link.recommended_backend)
    hostfile = build_mlx_hostfile(nodes)
    uv_bin = Path.home() / ".local" / "share" / "uv" / "tools" / "mlx-lm" / "bin"
    launch_bin = uv_bin / "mlx.launch"
    server_bin = uv_bin / "mlx_lm.server"

    state_dir = Path.home() / ".sanctum" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    hf_path = state_dir / "tb5-hostfile.json"

    cmd = [
        str(launch_bin),
        "--backend",
        plan.backend.value,
        "--hostfile",
        str(hf_path),
        str(server_bin),
        "--model",
        model,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if plan.mode == ClusterMode.PIPELINE:
        cmd.append("--pipeline")

    if dry_run:
        payload = {
            "ok": True,
            "dry_run": True,
            "plan": plan.to_dict(),
            "hostfile": hostfile,
            "command": cmd,
            "park_local_mlx": park_local_mlx,
        }
        if json_output:
            print(json.dumps(payload, indent=2))
        else:
            console.print(f"[cyan]Dry-run command:[/] {' '.join(cmd)}")
        return

    hf_path.write_text(json.dumps(hostfile, indent=2) + "\n", encoding="utf-8")
    parked = park_local_mlx_services() if park_local_mlx else []
    log_path = Path.home() / ".sanctum" / "logs" / "tb5-cluster.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with log_path.open("a", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True)
        save_cluster_state(
            {
                "active": True,
                "pid": proc.pid,
                "model": model,
                "mode": plan.mode.value,
                "backend": plan.backend.value,
                "port": port,
                "peer_host": peer,
                "parked_services": parked,
                "started_at": datetime.now(UTC).isoformat(),
                "log_path": str(log_path),
            }
        )
        ready = False
        for _ in range(30):
            if proc.poll() is not None:
                break
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=2.0)
                if r.status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2.0)
    except Exception:
        if parked:
            unpark_local_mlx_services(parked)
        raise

    out = {
        "ok": proc.poll() is None,
        "ready": ready,
        "pid": proc.pid,
        "endpoint": f"http://127.0.0.1:{port}/v1",
        "model": model,
        "mode": plan.mode.value,
        "backend": plan.backend.value,
        "parked_services": parked,
        "log_path": str(log_path),
    }
    if json_output:
        print(json.dumps(out, indent=2))
        return
    console.print(
        f"[green]✓[/] TB5 Cluster launched (PID {proc.pid}, mode={plan.mode.value}, backend={plan.backend.value}) "
        f"→ [bold]http://127.0.0.1:{port}/v1[/] (ready={ready}; logs: {log_path})"
    )


@tb5_app.command("down", help="Stop the Thunderbolt 5 cluster cleanly and wake parked local Yoda MLX.")
def tb5_down(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    res = stop_tb5_cluster(unpark_local=True)
    if json_output:
        print(json.dumps(res, indent=2))
        return
    woken = ", ".join(res.get("woken_services") or []) or "none needed"
    console.print(f"[green]✓[/] TB5 cluster stopped · woken local services: [bold]{woken}[/]")
