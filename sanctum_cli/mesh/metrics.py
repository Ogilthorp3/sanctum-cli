"""Privacy-preserving macro metrics collection for mesh nodes.

All metrics collected here are purely high-level system/hardware attributes
(chip family, RAM tier, country of origin, OS, and eval baseline) with
zero personal identifiers, prompt contents, or IP addresses.
"""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime

from sanctum_cli.mesh.types import NodeMacroMetrics

CommandRunner = Callable[[list[str]], str]


def default_runner(argv: list[str]) -> str:
    """Run a subprocess command and return stdout stripped."""
    return subprocess.check_output(argv, text=True, stderr=subprocess.DEVNULL).strip()


def detect_country(runner: CommandRunner | None = None) -> str:
    """Detect the two-letter ISO country code from system locale."""
    run = runner or default_runner
    try:
        raw = run(["defaults", "read", "-g", "AppleLocale"])
        if "_" in raw:
            candidate = raw.split("_")[-1][:2].upper()
            if candidate.isalpha() and len(candidate) == 2:
                return candidate
    except Exception:
        pass

    # Fallback to environment variables
    for var in ("LC_ALL", "LANG"):
        val = os.environ.get(var, "")
        if "_" in val:
            candidate = val.split("_")[1].split(".")[0][:2].upper()
            if candidate.isalpha() and len(candidate) == 2:
                return candidate
    return "ZZ"


def detect_chip(runner: CommandRunner | None = None) -> str:
    """Detect the Apple Silicon chip tier or processor model."""
    run = runner or default_runner
    try:
        raw = run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if raw:
            return raw
    except Exception:
        pass
    proc = platform.processor()
    return proc if proc else "Apple Silicon"


def detect_ram_gb(runner: CommandRunner | None = None) -> int:
    """Detect installed system RAM in gigabytes."""
    run = runner or default_runner
    try:
        raw = run(["sysctl", "-n", "hw.memsize"])
        bytes_val = int(raw)
        return round(bytes_val / (1024**3))
    except Exception:
        return 16


def detect_os() -> str:
    """Detect macOS product version."""
    mac_ver = platform.mac_ver()[0]
    if mac_ver:
        return f"macOS {mac_ver}"
    return platform.system()


def collect_local_macro_metrics(
    pubkey: str,
    *,
    eval_baseline: float = 0.881,
    offline_ratio: float = 1.0,
    champions_seeded: int = 0,
    champions_adopted: int = 0,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> NodeMacroMetrics:
    """Gather anonymized macro metrics for the current node."""
    stamp = (now or datetime.now(UTC)).isoformat()
    return NodeMacroMetrics(
        node_id=pubkey,
        country=detect_country(runner),
        chip=detect_chip(runner),
        memory_gb=detect_ram_gb(runner),
        os_version=detect_os(),
        offline_ratio=offline_ratio,
        eval_baseline=eval_baseline,
        champions_seeded=champions_seeded,
        champions_adopted=champions_adopted,
        timestamp=stamp,
    )
