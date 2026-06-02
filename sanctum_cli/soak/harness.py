"""Soak harness - records a module's unattended health over days.

``SoakResult`` is the on-disk artefact (JSON).  ``classify_soak`` reads a
result and returns ``(days_elapsed, is_clean)``.  ``run_soak`` drives the
collection loop, appending one ``Sample`` per interval (or exactly one
sample under ``--once``).

Dirty conditions (any one -> ``clean=False``):
 1. ``faults`` list is non-empty.
 2. Any sample has ``red_probes`` (a probe returned red during that window).
 3. Any sample has ``service_nonzero`` (a tracked service exited non-zero).
 4. Any sample has ``pressure_level==4`` (critical memory pressure) that is
    NOT followed by a strictly-later sample with ``pressure_level<=2``
    (normal/warn recovery).

Live signal sources (in ``run_soak``):
 - ``sysctl kern.memorystatus_vm_pressure_level`` -> pressure_level int
 - ``sysctl vm.swapusage`` -> swap_used_mb float
 - Module's ``probes`` list via the registry (future: call probe callables)
 - ``launchctl list`` exit codes for the module's declared services
"""
from __future__ import annotations

import contextlib
import json
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from sanctum_cli.commands.self_test import Probe
    from sanctum_cli.modules.registry import ModuleRegistry


# Module-level callable so tests can monkeypatch
# "sanctum_cli.soak.harness.module_probes" to inject controlled probe sets.
# We import lazily (inside the function body) to avoid a circular import at
# module load time: harness <- self_test <- (transitive) harness would cycle.
def module_probes(registry: ModuleRegistry) -> dict[str, list[Probe]]:
    """Return the module-keyed probe dict from the module registry.

    Delegates to ``sanctum_cli.commands.self_test.module_probes`` via a
    lazy import so this harness module can be imported before the commands
    package is fully initialised without triggering a circular import.
    """
    from sanctum_cli.commands.self_test import (
        module_probes as _real_module_probes,
    )
    return _real_module_probes(registry)


# ─── Schema ─────────────────────────────────────────────────────────


class Sample(BaseModel):
    """One health snapshot captured during a soak run."""

    model_config = ConfigDict(extra="forbid")

    ts: str  # ISO-8601 UTC timestamp
    pressure_level: int  # kern.memorystatus_vm_pressure_level (1=normal,2=warn,4=critical)
    swap_used_mb: float  # vm.swapusage used field in MiB
    red_probes: list[str]  # probe names that returned red at this sample
    service_nonzero: list[str]  # service labels whose last-exit was nonzero


class SoakResult(BaseModel):
    """Cumulative soak record written to ``soak.result_path``."""

    model_config = ConfigDict(extra="forbid")

    module: str
    started_at: str  # ISO-8601 UTC — when recording began
    last_at: str  # ISO-8601 UTC — timestamp of the most-recent sample
    samples: list[Sample]
    faults: list[str]  # free-text fault annotations (e.g. from a probe crash)


# ─── Classifier ─────────────────────────────────────────────────────


def classify_soak(result: SoakResult) -> tuple[float, bool]:
    """Classify a soak result into ``(elapsed_days, is_clean)``.

    ``elapsed_days`` = (last_at - started_at) in fractional days.

    ``is_clean`` is False if *any* of the four dirty conditions hold:
     1. ``faults`` non-empty.
     2. Any sample has ``red_probes``.
     3. Any sample has ``service_nonzero``.
     4. Any ``pressure_level==4`` sample lacks a strictly-later sample
        with ``pressure_level <= 2``.
    """
    started = datetime.fromisoformat(result.started_at.replace("Z", "+00:00"))
    last = datetime.fromisoformat(result.last_at.replace("Z", "+00:00"))
    days = (last - started).total_seconds() / 86400.0

    # Condition 1: explicit faults
    if result.faults:
        return days, False

    # Condition 2: any sample with red probes
    if any(s.red_probes for s in result.samples):
        return days, False

    # Condition 3: any sample with nonzero-exit services
    if any(s.service_nonzero for s in result.samples):
        return days, False

    # Condition 4: unrecovered critical memory pressure.
    # For each sample at pressure_level==4, there must be a strictly later
    # sample (by index; ts ordering is preserved by the appender) whose
    # pressure_level <= 2.
    # Note: "strictly later by index" assumes samples are in chronological order.
    # This invariant is guaranteed by run_soak's appender, which always calls
    # result.samples.append(sample) followed by _atomic_write.  Callers that
    # construct SoakResult manually (e.g. tests) must maintain the same order.
    samples = result.samples
    for i, sample in enumerate(samples):
        if sample.pressure_level == 4:
            # look for any later sample that shows recovery
            recovered = any(
                samples[j].pressure_level <= 2 for j in range(i + 1, len(samples))
            )
            if not recovered:
                return days, False

    return days, True


# ─── Live signal readers ─────────────────────────────────────────────


def _read_pressure_level() -> int:
    """Read kern.memorystatus_vm_pressure_level via sysctl (1=normal,2=warn,4=critical)."""
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            text=True,
            timeout=5,
        ).strip()
        return int(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 1  # default to normal if unreadable


def _read_swap_used_mb() -> float:
    """Parse ``vm.swapusage`` → used MiB.

    Example line:
      vm.swapusage: total = 30720.00M  used = 29535.69M  free = 1184.31M  (encrypted)
    """
    try:
        out = subprocess.check_output(
            ["sysctl", "vm.swapusage"],
            text=True,
            timeout=5,
        )
        m = re.search(r"used\s*=\s*([\d.]+)M", out)
        if m:
            return float(m.group(1))
    except (subprocess.SubprocessError, OSError):
        pass
    return 0.0


def _launchctl_exit_codes() -> dict[str, int]:
    """Return {label: last_exit_status} from ``launchctl list``.

    Only includes rows where the status column is a valid integer.
    PID column is ``-`` for not-running services; Status is the exit code.
    """
    codes: dict[str, int] = {}
    try:
        out = subprocess.check_output(
            ["launchctl", "list"],
            text=True,
            timeout=10,
        )
        for line in out.splitlines()[1:]:  # skip header
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            _pid, status_str, label = parts
            with contextlib.suppress(ValueError):
                codes[label.strip()] = int(status_str.strip())
    except (subprocess.SubprocessError, OSError):
        pass
    return codes


def _collect_sample(
    service_labels: list[str],
    *,
    module: str = "",
    registry: ModuleRegistry | None = None,
) -> Sample:
    """Build a single Sample from live system signals.

    When *registry* and *module* are provided, runs the module's declared
    probes via ``module_probes`` and records any that returned a failing
    ``ProbeResult`` in ``red_probes``.  A probe that raises an exception is
    treated as failing (name still recorded) so the soak can surface broken
    probes without crashing the runner.
    """
    ts = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    pressure = _read_pressure_level()
    swap_mb = _read_swap_used_mb()
    exit_codes = _launchctl_exit_codes()
    service_nonzero = [
        label
        for label in service_labels
        if exit_codes.get(label, 0) != 0
    ]

    # Run the module's declared probes and collect any that went red.
    red: list[str] = []
    if registry is not None and module:
        keyed = module_probes(registry)
        for probe in keyed.get(module, []):
            try:
                result = probe.check()
            except Exception:
                result_passed = False
            else:
                result_passed = result.passed
            if not result_passed:
                red.append(probe.name)

    return Sample(
        ts=ts,
        pressure_level=pressure,
        swap_used_mb=swap_mb,
        red_probes=red,
        service_nonzero=service_nonzero,
    )


# ─── Atomic file append ──────────────────────────────────────────────


def _load_or_init(result_path: Path, module: str) -> SoakResult:
    """Load an existing soak result file or create a fresh one."""
    if result_path.is_file():
        data = json.loads(result_path.read_text())
        return SoakResult.model_validate(data)
    now_iso = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    return SoakResult(
        module=module,
        started_at=now_iso,
        last_at=now_iso,
        samples=[],
        faults=[],
    )


def _atomic_write(path: Path, result: SoakResult) -> None:
    """Write *result* to *path* atomically (write tmp, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import os

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".soak-tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(result.model_dump(), f, indent=2)
        Path(tmp).rename(path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        raise


# ─── Runner ─────────────────────────────────────────────────────────


def run_soak(
    module: str,
    registry: ModuleRegistry,
    *,
    days: float = 7.0,  # noqa: ARG001 — stored in CLI help, not used in loop logic
    interval_sec: int = 3600,
    once: bool = False,
) -> None:
    """Run the soak recording loop for *module*.

    Each iteration appends one ``Sample`` to the result file at
    ``manifest.soak.result_path`` and updates ``last_at``.

    Args:
        module:       Module name (must exist in *registry*).
        registry:     Resolved module registry.
        days:         Target soak duration (informational; stored for gate).
        interval_sec: Sleep between samples (ignored when ``once=True``).
        once:         Capture exactly one sample and exit immediately.
    """
    manifest = registry.get(module)
    result_path = Path(
        manifest.soak.result_path.replace("{module}", module)
    ).expanduser()
    service_labels = [s.label for s in manifest.services]

    result = _load_or_init(result_path, module)
    sample = _collect_sample(service_labels, module=module, registry=registry)
    result.samples.append(sample)
    result.last_at = sample.ts
    _atomic_write(result_path, result)

    if once:
        return

    # Continuous loop - runs until interrupted (Ctrl-C / SIGTERM).
    try:
        while True:
            time.sleep(interval_sec)
            sample = _collect_sample(service_labels, module=module, registry=registry)
            result = _load_or_init(result_path, module)  # re-read in case of external write
            result.samples.append(sample)
            result.last_at = sample.ts
            _atomic_write(result_path, result)
    except KeyboardInterrupt:
        pass
