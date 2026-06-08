"""``sanctum screen-time`` — coverage view + phone enforcement mode.

The honest-coverage surface that pairs with the screen-time engine's opt-in
network MAC-pause. It reads the canonical ``devices.yaml`` and tells the parent
which of a kid's personal devices are actually curfewed by Sanctum
(``hard-pause``, Wi-Fi) versus deferred to Apple Screen Time (``presence-only``).

``phone-mode`` flips a kid between Apple Screen Time / Sanctum MAC-pause / both.
It previews by default and writes only on ``--apply`` (backing up devices.yaml
first) — so it is safe to explore. It never restarts the live engine; that
stays a deliberate operator step.

The opt-in predicate here mirrors ``screen_time._personal_enforce_macs`` in the
engine so the coverage report cannot lie about what enforcement will actually
do; the tests derive their expectations from the on-disk schema independently.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from rich.console import Console
from rich.table import Table
from rich.text import Text

from sanctum_cli.errors import LocalError, UserError

console = Console()

ENV_DEVICES_FILE = "SANCTUM_DEVICES_FILE"
_DEVICES_CANDIDATES = (
    Path.home() / ".sanctum/screen-time/devices.yaml",
    Path.home() / "Projects/sanctum-screen-time/devices.yaml",
)
_DEVICE_OFF_VALUES = frozenset({"off", "none", "false", "0"})
_VALID_MODES = ("apple", "macpause", "both")


class CoverageRow(NamedTuple):
    """One personal device's enforcement classification."""

    person: str
    role: str
    device: str
    mac: str
    klass: str  # "hard-pause" | "presence-only"
    enforced: bool


# ── Pure logic ────────────────────────────────────────────────────────


def _device_enforced(member: dict[str, Any], dev: dict[str, Any]) -> bool:
    """Whether a personal device is opted into network MAC-pause.

    OFF by default (phones defer to Apple Screen Time). On when the child-level
    ``enforce_personal: macpause`` is set or the device sets ``enforce:
    macpause``; a per-device ``enforce: off|none|false`` opts a single device
    back out when the child-level switch is on. Mirrors the engine exactly.
    """
    child_on = str(member.get("enforce_personal", "")).strip().lower() == "macpause"
    dev_enforce = str(dev.get("enforce", "")).strip().lower()
    if dev_enforce in _DEVICE_OFF_VALUES:
        return False
    return child_on or dev_enforce == "macpause"


def classify_coverage(config: dict[str, Any]) -> list[CoverageRow]:
    """Classify every child's personal devices as hard-pause vs presence-only."""
    rows: list[CoverageRow] = []
    family = config.get("family") or {}
    for person, member in family.items():
        if not isinstance(member, dict) or member.get("role") != "child":
            continue
        for dev in member.get("personal_devices") or []:
            enforced = _device_enforced(member, dev)
            rows.append(
                CoverageRow(
                    person=str(person),
                    role="child",
                    device=str(dev.get("name", "?")),
                    mac=str(dev.get("mac", "?")).upper(),
                    klass="hard-pause" if enforced else "presence-only",
                    enforced=enforced,
                )
            )
    return rows


def set_phone_mode(config: dict[str, Any], kid: str, mode: str) -> dict[str, Any]:
    """Return a *new* config with ``kid``'s phone enforcement mode applied.

    ``apple`` clears the Sanctum flag (Apple Screen Time owns the phone);
    ``macpause`` and ``both`` set ``enforce_personal: macpause`` (Sanctum
    curfews the Wi-Fi). Raises :class:`UserError` for an unknown mode, an
    unknown member, or a non-child target. Does not mutate the input.
    """
    import copy

    mode_norm = mode.strip().lower()
    if mode_norm not in _VALID_MODES:
        raise UserError(
            f"unknown phone mode {mode!r}",
            fix=f"choose one of: {', '.join(_VALID_MODES)}",
        )
    family = config.get("family") or {}
    member = family.get(kid)
    if not isinstance(member, dict):
        raise UserError(
            f"no family member named {kid!r}",
            fix="run `sanctum screen-time coverage` to see the kids on file",
        )
    if member.get("role") != "child":
        raise UserError(
            f"{kid!r} is not a child (role={member.get('role')!r})",
            fix="phone-mode applies to children only",
        )

    new = copy.deepcopy(config)
    member_new = new["family"][kid]
    if mode_norm == "apple":
        member_new.pop("enforce_personal", None)
    else:  # macpause | both -> Sanctum enforces the Wi-Fi side
        member_new["enforce_personal"] = "macpause"
    return new


# ── I/O ───────────────────────────────────────────────────────────────


def _resolve_devices_path() -> Path:
    override = os.environ.get(ENV_DEVICES_FILE)
    if override:
        return Path(override).expanduser()
    for candidate in _DEVICES_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise LocalError(
        "devices.yaml not found",
        fix="expected at ~/.sanctum/screen-time/devices.yaml — is the screen-time module installed?",
    )


def _load_devices(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LocalError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LocalError(f"{path} is not a YAML mapping")
    return data


# ── Commands ──────────────────────────────────────────────────────────


def coverage_command() -> None:
    """Print the per-kid personal-device enforcement table + honest caveats."""
    config = _load_devices(_resolve_devices_path())
    rows = classify_coverage(config)
    if not rows:
        console.print("[yellow]No children with personal devices found in devices.yaml.[/]")
        return

    table = Table(title="Screen-time coverage — personal devices", header_style="bold cyan")
    table.add_column("Kid")
    table.add_column("Device")
    table.add_column("MAC")
    table.add_column("Enforcement")
    for row in rows:
        # User-supplied strings (name, MAC) go through Text so Rich treats them
        # literally — no markup parsing, no :cd:-style emoji substitution.
        enforcement = (
            Text("hard-pause (Wi-Fi)", style="green")
            if row.enforced
            else Text("presence-only (Apple Screen Time)", style="dim")
        )
        table.add_row(Text(row.person), Text(row.device), Text(row.mac), enforcement)
    console.print(table)

    n_hard = sum(1 for r in rows if r.enforced)
    console.print(
        f"\n[bold]{n_hard}/{len(rows)}[/] personal devices are network-enforced "
        "(hard-pause) — Wi-Fi only; cellular escapes a network box."
    )
    console.print(
        "[dim]presence-only devices are NOT curfewed by Sanctum; they defer to "
        "Apple Screen Time. Enforce one with "
        "`sanctum screen-time phone-mode <kid> macpause --apply`.[/]"
    )


def phone_mode_command(kid: str, mode: str, *, apply: bool = False) -> None:
    """Preview (default) or write a kid's phone enforcement mode."""
    path = _resolve_devices_path()
    config = _load_devices(path)
    new = set_phone_mode(config, kid, mode)  # validates kid/mode

    affected = [r for r in classify_coverage(new) if r.person == kid]
    n_hard = sum(1 for r in affected if r.enforced)
    mode_norm = mode.strip().lower()

    if not apply:
        console.print(
            f"[bold]Preview[/] — {kid} → mode [cyan]{mode_norm}[/]: "
            f"{n_hard}/{len(affected)} of {kid}'s personal devices would be "
            "network-enforced."
        )
        if mode_norm in ("macpause", "both"):
            console.print("[dim]Wi-Fi only — cellular still needs Apple Screen Time.[/]")
        console.print(f"[dim]Re-run with [bold]--apply[/] to write {path}.[/]")
        return

    backup = path.parent / (path.name + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(
        yaml.safe_dump(new, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    console.print(
        f"[green]✓[/] {kid} set to [cyan]{mode_norm}[/] — wrote {path} "
        f"(backup: {backup.name}). Reload the screen-time engine to apply."
    )
