"""Soak harness — public API.

Records a module's unattended health over days.  The gate in
``ship_gates.gate_soak`` reads the result via ``classify_soak``.
"""

from sanctum_cli.soak.harness import (
    Sample,
    SoakResult,
    classify_soak,
    run_soak,
)

__all__ = ["Sample", "SoakResult", "classify_soak", "run_soak"]
