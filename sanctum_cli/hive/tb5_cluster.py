"""Thunderbolt 5 192 GB Apple Silicon Cluster Mode (``sanctum hive tb5``).

Pure, testable orchestration over injected seams plus live macOS TB5 discovery:
- Auto-discovers Thunderbolt 5 ``bridge0`` links (80/120 Gb/s), Apple ``rdma_ctl``
  status (``jaccl`` <10 us RDMA vs ``ring`` over TB5 IP), and remote Apple Silicon
  RAM/chip inventory.
- Plans optimal sharding for **asymmetric** Mac pairs (e.g. 64 GB M4 Pro + 128 GB
  M4 Max = 192 GB):
  - ``pipeline``: proportional layer split (e.g. 1/3 + 2/3) unlocking full ~154 GB
    usable GPU VRAM for 235B 4-bit MoE / 120B-253B models.
  - ``tensor``: 50/50 split capped by ``2 x smallest_usable_gb`` (~100 GB) at full
    combined memory bandwidth (~819 GB/s) for blazing 70B-123B interactive speed.
- Safe RAM handoff with Castellan/LaunchAgents: parks local ``haus.sanctum.mlx``
  when requested, persists exact parked state atomically to
  ``~/.sanctum/state/tb5-cluster.json``, and unconditionally wakes Yoda back up on
  ``down`` or launch failure (zero-orphan deadman guarantee).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DEFAULT_CLUSTER_PORT",
    "DEFAULT_TB5_LOCAL_IP",
    "DEFAULT_TB5_PEER_HOST",
    "DEFAULT_TB5_PEER_IP",
    "ClusterBackend",
    "ClusterEngine",
    "ClusterMode",
    "ClusterNode",
    "ClusterPlan",
    "ModelRecommendation",
    "Tb5LinkStatus",
    "build_mlx_hostfile",
    "detect_local_tb5",
    "detect_rdma_status",
    "load_cluster_state",
    "park_local_mlx_services",
    "plan_cluster_sharding",
    "probe_tb5_tcp",
    "recommended_models_for_vram",
    "run_live_tb5_bench",
    "save_cluster_state",
    "stop_tb5_cluster",
    "tb5_state_path",
    "unpark_local_mlx_services",
]

DEFAULT_TB5_LOCAL_IP = "10.0.5.1"
DEFAULT_TB5_PEER_IP = "10.0.5.2"
DEFAULT_TB5_PEER_HOST = "mbp"
DEFAULT_CLUSTER_PORT = 1340
_DEFAULT_USABLE_FRACTION = 0.80
_OS_RESERVE_GB = 4
_LOCAL_MLX_LABELS: tuple[str, ...] = ("haus.sanctum.mlx", "haus.sanctum.mlx-coder")


class ClusterMode(StrEnum):
    """Sharding strategy across connected Apple Silicon Macs."""

    PIPELINE = "pipeline"
    TENSOR = "tensor"


class ClusterBackend(StrEnum):
    """MLX distributed collective transport over Thunderbolt 5."""

    JACCL = "jaccl"
    RING = "ring"


class ClusterEngine(StrEnum):
    """Inference server runtime driven by ``sanctum hive tb5``."""

    MLX_LM = "mlx-lm"
    SANCTUM_MLX = "sanctum-mlx"


@dataclass(frozen=True)
class ClusterNode:
    """One Apple Silicon Mac participating in a Thunderbolt 5 cluster."""

    name: str
    ssh_target: str
    tb5_ip: str
    chip: str
    memory_gb: int
    rdma_enabled: bool = False
    is_local: bool = False

    @property
    def usable_vram_gb(self) -> int:
        return max(8, int(self.memory_gb * _DEFAULT_USABLE_FRACTION) - _OS_RESERVE_GB)

    @property
    def est_bandwidth_gbps(self) -> int:
        chip_l = self.chip.lower()
        if "ultra" in chip_l:
            return 819
        if "m4 max" in chip_l or "m5 max" in chip_l:
            return 546
        if "max" in chip_l:
            return 400
        if "m4 pro" in chip_l or "m5 pro" in chip_l:
            return 273
        if "pro" in chip_l:
            return 200
        return 120

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ssh_target": self.ssh_target,
            "tb5_ip": self.tb5_ip,
            "chip": self.chip,
            "memory_gb": self.memory_gb,
            "usable_vram_gb": self.usable_vram_gb,
            "est_bandwidth_gbps": self.est_bandwidth_gbps,
            "rdma_enabled": self.rdma_enabled,
            "is_local": self.is_local,
        }


@dataclass(frozen=True)
class Tb5LinkStatus:
    """Measured physical and L3 status of the Thunderbolt 5 bridge."""

    bridge_up: bool
    local_ip: str
    peer_ip: str
    link_speed_gbps: int
    peer_mac_visible: bool
    rdma_local: bool
    rdma_peer: bool
    recommended_backend: ClusterBackend

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_up": self.bridge_up,
            "local_ip": self.local_ip,
            "peer_ip": self.peer_ip,
            "link_speed_gbps": self.link_speed_gbps,
            "peer_mac_visible": self.peer_mac_visible,
            "rdma_local": self.rdma_local,
            "rdma_peer": self.rdma_peer,
            "recommended_backend": self.recommended_backend.value,
        }


@dataclass(frozen=True)
class ModelRecommendation:
    """Verified Apple Silicon giant model fit for a given pooled VRAM budget."""

    hf_id: str
    params: str
    est_vram_gb: int
    recommended_mode: ClusterMode
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hf_id": self.hf_id,
            "params": self.params,
            "est_vram_gb": self.est_vram_gb,
            "recommended_mode": self.recommended_mode.value,
            "notes": self.notes,
        }


_MODEL_CATALOG: tuple[ModelRecommendation, ...] = (
    ModelRecommendation(
        hf_id="mlx-community/Qwen3-235B-A22B-4bit",
        params="235B MoE (22B active)",
        est_vram_gb=132,
        recommended_mode=ClusterMode.PIPELINE,
        notes="Flagship 235B MoE — fits full 192 GB pool via asymmetric 1/3+2/3 pipeline at 20-35 tok/s",
    ),
    ModelRecommendation(
        hf_id="mlx-community/Mistral-Large-Instruct-2411-4bit",
        params="123B Dense",
        est_vram_gb=70,
        recommended_mode=ClusterMode.TENSOR,
        notes="123B dense powerhouse — fits 50/50 tensor-parallel at full ~819 GB/s combined bandwidth",
    ),
    ModelRecommendation(
        hf_id="mlx-community/Qwen3-Next-80B-A3B-Instruct-4bit",
        params="80B MoE (3B active)",
        est_vram_gb=46,
        recommended_mode=ClusterMode.TENSOR,
        notes="Ultra-fast reasoning & coding MoE with generous KV headroom for 128k context",
    ),
    ModelRecommendation(
        hf_id="mlx-community/DeepSeek-R1-0528-3bit",
        params="671B MoE (37B active, 3-bit)",
        est_vram_gb=148,
        recommended_mode=ClusterMode.PIPELINE,
        notes="Frontier 671B reasoner (3-bit quant) — tight fit on dedicated 192 GB pipeline pool",
    ),
    ModelRecommendation(
        hf_id="mlx-community/Llama-3.3-70B-Instruct-4bit",
        params="70B Dense",
        est_vram_gb=40,
        recommended_mode=ClusterMode.TENSOR,
        notes="Fast 70B dense baseline — fits with Yoda still warm on hub or full tensor speed",
    ),
)


@dataclass(frozen=True)
class ClusterPlan:
    """Computed memory, bandwidth, backend, and layer-sharding plan for a TB5 cluster."""

    nodes: tuple[ClusterNode, ...]
    mode: ClusterMode
    backend: ClusterBackend
    total_ram_gb: int
    usable_vram_gb: int
    combined_bandwidth_gbps: int
    layer_split_ratios: tuple[float, ...]
    asymmetric: bool
    recommendations: tuple[ModelRecommendation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "mode": self.mode.value,
            "backend": self.backend.value,
            "total_ram_gb": self.total_ram_gb,
            "usable_vram_gb": self.usable_vram_gb,
            "combined_bandwidth_gbps": self.combined_bandwidth_gbps,
            "layer_split_ratios": [round(r, 4) for r in self.layer_split_ratios],
            "asymmetric": self.asymmetric,
            "recommendations": [m.to_dict() for m in self.recommendations],
        }


def recommended_models_for_vram(
    usable_vram_gb: int,
    *,
    mode: ClusterMode | None = None,
) -> tuple[ModelRecommendation, ...]:
    """Return models from the verified catalog that fit safely inside ``usable_vram_gb``."""
    out: list[ModelRecommendation] = []
    for item in _MODEL_CATALOG:
        if item.est_vram_gb > usable_vram_gb:
            continue
        if mode is not None and item.recommended_mode != mode and mode == ClusterMode.TENSOR:
            # Pipeline models still fit if est_vram_gb <= tensor cap
            pass
        out.append(item)
    return tuple(sorted(out, key=lambda m: -m.est_vram_gb))


def plan_cluster_sharding(
    nodes: Sequence[ClusterNode],
    *,
    mode: ClusterMode | None = None,
    backend: ClusterBackend | None = None,
) -> ClusterPlan:
    """Compute exact VRAM capacity, asymmetry ratio, and transport backend for ``nodes``."""
    if len(nodes) < 2:
        msg = "TB5 cluster requires at least 2 Apple Silicon nodes"
        raise ValueError(msg)

    node_tuple = tuple(nodes)
    rams = [n.memory_gb for n in node_tuple]
    asymmetric = min(rams) != max(rams)
    chosen_mode = mode or (ClusterMode.PIPELINE if asymmetric else ClusterMode.TENSOR)

    all_rdma = all(n.rdma_enabled for n in node_tuple)
    chosen_backend = backend or (ClusterBackend.JACCL if all_rdma else ClusterBackend.RING)

    total_ram = sum(rams)
    usables = [n.usable_vram_gb for n in node_tuple]
    if chosen_mode == ClusterMode.TENSOR:
        # Symmetric tensor sharding is bounded by N * smallest node's usable VRAM
        usable_vram = min(usables) * len(node_tuple)
        ratios = tuple(1.0 / len(node_tuple) for _ in node_tuple)
    else:
        # Pipeline sharding allocates transformer layers proportionally to each Mac's VRAM
        usable_vram = sum(usables)
        ratios = tuple(u / usable_vram for u in usables)

    combined_bw = sum(n.est_bandwidth_gbps for n in node_tuple)
    recs = recommended_models_for_vram(usable_vram, mode=chosen_mode)

    return ClusterPlan(
        nodes=node_tuple,
        mode=chosen_mode,
        backend=chosen_backend,
        total_ram_gb=total_ram,
        usable_vram_gb=usable_vram,
        combined_bandwidth_gbps=combined_bw,
        layer_split_ratios=ratios,
        asymmetric=asymmetric,
        recommendations=recs,
    )


def build_mlx_hostfile(nodes: Sequence[ClusterNode]) -> list[dict[str, Any]]:
    """Build the canonical ``mlx.launch --hostfile`` JSON list (Rank 0 local first)."""
    ordered = sorted(nodes, key=lambda n: (0 if n.is_local else 1, n.name))
    return [
        {
            "ssh": "127.0.0.1" if n.is_local else n.ssh_target,
            "ips": [n.tb5_ip],
        }
        for n in ordered
    ]


# ─── Live macOS TB5 & RDMA Probes ────────────────────────────────────────────


def detect_rdma_status() -> bool:
    """Return True when Apple macOS ``rdma_ctl status`` reports RDMA enabled."""
    rdma_bin = Path("/usr/bin/rdma_ctl")
    if not rdma_bin.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(rdma_bin), "status"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        return "enabled" in (proc.stdout + proc.stderr).lower()
    except (OSError, subprocess.SubprocessError):
        return False


def detect_local_tb5(
    *,
    peer_host: str = DEFAULT_TB5_PEER_HOST,
    default_local_ip: str = DEFAULT_TB5_LOCAL_IP,
    default_peer_ip: str = DEFAULT_TB5_PEER_IP,
) -> tuple[Tb5LinkStatus, list[ClusterNode]]:
    """Probe local ``bridge0`` TB5 IP, link speed (Gb/s), RDMA, and remote peer specs."""
    local_ip = default_local_ip
    bridge_up = False
    peer_mac_visible = False
    try:
        ifc = subprocess.run(["ifconfig", "bridge0"], capture_output=True, text=True, timeout=4, check=False)
        out = ifc.stdout
        bridge_up = "status: active" in out
        m = re.search(r"inet\s+(10\.0\.5\.\d+|169\.254\.\d+\.\d+)", out)
        if m:
            local_ip = m.group(1)
        peer_mac_visible = "Address cache:" in out and bool(
            re.search(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}\s+Vlan", out)
        )
    except (OSError, subprocess.SubprocessError):
        pass

    link_speed = 0
    try:
        sp = subprocess.run(
            ["system_profiler", "SPThunderboltDataType"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        speeds = [int(s) for s in re.findall(r"Speed:\s*(\d+)\s*Gb/s", sp.stdout)]
        if speeds:
            link_speed = max(speeds)
    except (OSError, subprocess.SubprocessError):
        pass

    local_rdma = detect_rdma_status()
    local_chip = _sysctl_str("machdep.cpu.brand_string", "Apple Silicon")
    local_ram = _sysctl_ram_gb()
    local_name = _read_node_id("hub")

    peer_ip = default_peer_ip
    peer_chip = "Apple Silicon"
    peer_ram = 0
    peer_rdma = False
    try:
        probe_cmd = (
            'printf "%s\\n%s\\n%s\\n%s\\n" '
            '"$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo Unknown)" '
            '"$(sysctl -n hw.memsize 2>/dev/null || echo 0)" '
            '"$(/usr/bin/rdma_ctl status 2>/dev/null || echo disabled)" '
            '"$(ifconfig bridge0 2>/dev/null | awk \'/inet /{print $2; exit}\')"'
        )
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", peer_host, probe_cmd],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if res.returncode == 0:
            lines = [ln.strip() for ln in res.stdout.splitlines()]
            if len(lines) >= 2:
                peer_chip = lines[0] or peer_chip
                peer_ram = max(0, int(lines[1] or "0") // (1024**3))
            if len(lines) >= 3:
                peer_rdma = "enabled" in lines[2].lower()
            if len(lines) >= 4 and lines[3]:
                peer_ip = lines[3]
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    nodes: list[ClusterNode] = [
        ClusterNode(
            name=local_name,
            ssh_target="127.0.0.1",
            tb5_ip=local_ip,
            chip=local_chip,
            memory_gb=local_ram,
            rdma_enabled=local_rdma,
            is_local=True,
        )
    ]
    if peer_ram > 0:
        nodes.append(
            ClusterNode(
                name=peer_host,
                ssh_target=peer_host,
                tb5_ip=peer_ip,
                chip=peer_chip,
                memory_gb=peer_ram,
                rdma_enabled=peer_rdma,
                is_local=False,
            )
        )

    rec_backend = ClusterBackend.JACCL if (local_rdma and peer_rdma) else ClusterBackend.RING
    link = Tb5LinkStatus(
        bridge_up=bridge_up,
        local_ip=local_ip,
        peer_ip=peer_ip,
        link_speed_gbps=link_speed,
        peer_mac_visible=peer_mac_visible,
        rdma_local=local_rdma,
        rdma_peer=peer_rdma,
        recommended_backend=rec_backend,
    )
    return link, nodes


def _sysctl_str(key: str, default: str) -> str:
    try:
        p = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=3, check=False)
        return p.stdout.strip() or default
    except (OSError, subprocess.SubprocessError):
        return default


def _sysctl_ram_gb() -> int:
    try:
        p = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3, check=False)
        return max(8, int(p.stdout.strip() or "0") // (1024**3))
    except (OSError, subprocess.SubprocessError, ValueError):
        return 16


def _read_node_id(default: str) -> str:
    root = Path(os.environ.get("SANCTUM_HOME", str(Path.home() / ".sanctum")))
    nid = root / ".node_id"
    if nid.is_file():
        val = nid.read_text(encoding="utf-8").strip()
        if val:
            return val
    return default


def _peer_mlx_python_expr() -> str:
    return "$HOME/.local/share/uv/tools/mlx-lm/bin/python3"


def _firewall_fix_cmd(py_expr: str = "$HOME/.local/share/uv/tools/mlx-lm/bin/python3") -> str:
    return (
        f'PY="$(readlink -f {py_expr})" && '
        'sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$PY" --unblockapp "$PY"'
    )


def probe_tb5_tcp(
    peer_host: str,
    peer_ip: str,
    *,
    local_ip: str = DEFAULT_TB5_LOCAL_IP,
    port: int = 19557,
) -> dict[str, Any]:
    """Verify bidirectional TCP over Thunderbolt 5 using the actual ``mlx-lm`` runtime binary.

    Tests both ``local -> peer`` and ``peer -> local`` using each Mac's ``mlx-lm`` Python binary
    so asymmetric macOS Application Firewall blocks are detected with exact per-host fixes.
    """
    peer_py = _peer_mlx_python_expr()
    remote_bind = (
        f"import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
        f"s.bind(({peer_ip!r},{port})); s.listen(1); s.settimeout(5); "
        f"c,a=s.accept(); c.sendall(b'TB5_OK'); c.close(); s.close()"
    )
    remote_cmd = f'PY="{peer_py}"; [ -x "$PY" ] || PY="$(which python3)"; "$PY" -c {json.dumps(remote_bind)}'
    srv = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", peer_host, remote_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    l2p_ok = False
    l2p_rtt_ms: float | None = None
    l2p_err: str | None = None
    try:
        time.sleep(1.0)
        t0 = time.perf_counter()
        with socket.create_connection((peer_ip, port), timeout=3.5) as sock:
            reply = sock.recv(16)
        l2p_rtt_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        l2p_ok = reply == b"TB5_OK"
    except OSError as exc:
        l2p_err = str(exc)
    finally:
        try:
            srv.wait(timeout=3)
        except subprocess.TimeoutExpired:
            srv.kill()

    # Reverse direction: peer -> local using local mlx-lm Python binary (exact ring listener binary)
    p2l_ok = False
    p2l_err: str | None = None
    rev_port = port + 1
    _, local_py = _resolve_mlx_launch_bins()
    local_exec = local_py if Path(local_py).is_file() else "python3"
    local_bind = (
        f"import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
        f"s.bind(({local_ip!r},{rev_port})); s.listen(1); s.settimeout(6); "
        f"c,a=s.accept(); data=c.recv(16); c.sendall(b'TB5_REV_OK' if data==b'TB5_REV' else b'ERR'); "
        f"c.close(); s.close()"
    )
    local_srv = subprocess.Popen(
        [local_exec, "-c", local_bind],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.5)
        client_code = (
            f"import socket; s=socket.create_connection(({local_ip!r},{rev_port}),timeout=3.5); "
            f"s.sendall(b'TB5_REV'); print(s.recv(16).decode()); s.close()"
        )
        cli = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                peer_host,
                f"python3 -c {shlex.quote(client_code)}",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if cli.returncode == 0 and "TB5_REV_OK" in cli.stdout:
            p2l_ok = True
        else:
            p2l_err = (cli.stderr or cli.stdout or f"exit {cli.returncode}").strip()[-180:]
    except (OSError, subprocess.SubprocessError) as exc:
        p2l_err = str(exc)
    finally:
        try:
            local_srv.wait(timeout=3)
        except subprocess.TimeoutExpired:
            local_srv.kill()

    firewall_blocked_on_peer = not l2p_ok
    firewall_blocked_on_local = not p2l_ok
    fixes: list[str] = []
    if firewall_blocked_on_local:
        fixes.append(f"Run locally: {_firewall_fix_cmd()}")
    if firewall_blocked_on_peer:
        fixes.append(f"Run on {peer_host}: {_firewall_fix_cmd()}")

    result: dict[str, Any] = {
        "ok": l2p_ok and p2l_ok,
        "local_to_peer_ok": l2p_ok,
        "peer_to_local_ok": p2l_ok,
        "peer_ip": peer_ip,
        "local_ip": local_ip,
        "rtt_ms": l2p_rtt_ms,
        "firewall_blocked_on_local": firewall_blocked_on_local,
        "firewall_blocked_on_peer": firewall_blocked_on_peer,
    }
    if l2p_err:
        result["local_to_peer_error"] = l2p_err
    if p2l_err:
        result["peer_to_local_error"] = p2l_err
    if fixes:
        result["firewall_fix"] = " ; ".join(fixes)
    return result


# ─── State Persistence & Safe Yoda RAM Handoff ───────────────────────────────


def tb5_state_path() -> Path:
    root = Path(os.environ.get("SANCTUM_HOME", str(Path.home() / ".sanctum")))
    return root / "state" / "tb5-cluster.json"


def load_cluster_state() -> dict[str, Any]:
    path = tb5_state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cluster_state(data: dict[str, Any]) -> Path:
    path = tb5_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def park_local_mlx_services(labels: Sequence[str] = _LOCAL_MLX_LABELS) -> list[str]:
    """Gracefully park local LaunchAgent MLX servers to free unified GPU RAM for 192 GB cluster."""
    parked: list[str] = []
    domain = _launchd_domain()
    for lbl in labels:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{lbl}.plist"
        if not plist.is_file():
            continue
        res = subprocess.run(
            ["launchctl", "bootout", f"{domain}/{lbl}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode == 0:
            parked.append(lbl)
    return parked


def unpark_local_mlx_services(labels: Sequence[str] = _LOCAL_MLX_LABELS) -> list[str]:
    """Wake previously parked local MLX LaunchAgents (deadman restoration for Yoda)."""
    woken: list[str] = []
    domain = _launchd_domain()
    for lbl in labels:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{lbl}.plist"
        if not plist.is_file():
            continue
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{domain}/{lbl}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        woken.append(lbl)
    return woken


def _resolve_mlx_launch_bins() -> tuple[str, str]:
    uv_bin = Path.home() / ".local" / "share" / "uv" / "tools" / "mlx-lm" / "bin"
    launch = uv_bin / "mlx.launch"
    py = uv_bin / "python3"
    return (str(launch), str(py))


def run_live_tb5_bench(
    nodes: Sequence[ClusterNode],
    *,
    backend: ClusterBackend = ClusterBackend.RING,
    iters: int = 30,
) -> dict[str, Any]:
    """Run a real distributed MLX GPU ``all_sum`` latency + bandwidth benchmark across TB5."""
    launch_bin, py_bin = _resolve_mlx_launch_bins()
    if not Path(launch_bin).is_file():
        return {"ok": False, "error": f"mlx.launch not found at {launch_bin}"}

    hostfile = build_mlx_hostfile(nodes)
    peer_hosts = [n.ssh_target for n in nodes if not n.is_local]

    bench_code = f"""
import json, platform, time
import mlx.core as mx

group = mx.distributed.init(strict=True, backend={backend.value!r})
rank = group.rank()
size = group.size()

x = mx.ones((512, 512), dtype=mx.float16) * (rank + 1)
y = mx.distributed.all_sum(x, group=group)
mx.eval(y)
expected = float(sum(range(1, size + 1)))
assert float(y[0, 0].item()) == expected

act = mx.ones((1, 1, 8192), dtype=mx.float16)
for _ in range(5):
    act = mx.distributed.all_sum(act, group=group)
    mx.eval(act)

t0 = time.perf_counter()
for _ in range({iters}):
    act = mx.distributed.all_sum(act, group=group)
    mx.eval(act)
hop_ms = ((time.perf_counter() - t0) / {iters}) * 1000.0

big = mx.ones((4096, 8192), dtype=mx.float16)
mx.eval(big)
t1 = time.perf_counter()
for _ in range(8):
    r = mx.distributed.all_sum(big, group=group)
    mx.eval(r)
bw_s = (time.perf_counter() - t1) / 8.0
gbps = (64.0 / 1024.0 * 8.0) / bw_s

if rank == 0:
    print("TB5_BENCH_JSON:" + json.dumps({{
        "ok": True,
        "world_size": size,
        "backend": {backend.value!r},
        "hop_16kb_ms": round(hop_ms, 3),
        "bulk_64mb_gbps": round(gbps, 2),
        "rank0_host": platform.node(),
    }}))
"""
    with tempfile.TemporaryDirectory(prefix="sanctum-tb5-bench-") as td:
        td_path = Path(td)
        hf_path = td_path / "hostfile.json"
        script_path = td_path / "bench.py"
        hf_path.write_text(json.dumps(hostfile, indent=2), encoding="utf-8")
        script_path.write_text(bench_code, encoding="utf-8")

        for peer in peer_hosts:
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", peer, f"mkdir -p {td}"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["scp", "-q", "-o", "BatchMode=yes", str(script_path), f"{peer}:{script_path}"],
                check=True,
                capture_output=True,
            )
        try:
            proc = subprocess.run(
                [launch_bin, "--backend", backend.value, "--hostfile", str(hf_path), py_bin, str(script_path)],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            for line in (proc.stdout + "\n" + proc.stderr).splitlines():
                if "TB5_BENCH_JSON:" in line:
                    payload = line.split("TB5_BENCH_JSON:", 1)[1].strip()
                    result: dict[str, Any] = json.loads(payload)
                    return result
            return {
                "ok": False,
                "returncode": proc.returncode,
                "error": (proc.stderr or proc.stdout).strip()[-400:],
            }
        except subprocess.TimeoutExpired:
            peers_str = ", ".join(peer_hosts) or "peer"
            return {
                "ok": False,
                "error": (
                    f"Timed out waiting for MLX {backend.value} handshake across TB5. "
                    f"If macOS Application Firewall is blocking inbound TCP to mlx-lm Python on {peers_str}, run: "
                    f"{_firewall_fix_cmd()}"
                ),
            }
        finally:
            for peer in peer_hosts:
                subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", peer, f"rm -rf {td}"],
                    capture_output=True,
                    check=False,
                )


def stop_tb5_cluster(*, unpark_local: bool = True) -> dict[str, Any]:
    """Terminate any running TB5 cluster ranks cleanly and wake parked local MLX services."""
    state = load_cluster_state()
    pid = int(state.get("pid") or 0)
    killed = False
    if pid > 0:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed = True
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except OSError:
                pass

    peer_host = str(state.get("peer_host") or DEFAULT_TB5_PEER_HOST)
    port = int(state.get("port") or DEFAULT_CLUSTER_PORT)
    subprocess.run(
        ["pkill", "-f", f"mlx_lm\\.server.*--port {port}"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            peer_host,
            f"pkill -f 'mlx_lm\\.server.*--port {port}' || true",
        ],
        capture_output=True,
        check=False,
    )

    parked = list(state.get("parked_services") or [])
    woken: list[str] = []
    if unpark_local and parked:
        woken = unpark_local_mlx_services(parked)

    save_cluster_state(
        {
            "active": False,
            "stopped_at": datetime.now(UTC).isoformat(),
            "last_woken_services": woken,
        }
    )
    return {"ok": True, "stopped_pid": pid if killed else None, "woken_services": woken}
