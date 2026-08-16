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

from sanctum_cli.devices import firewalla
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
    path.write_text(yaml.safe_dump(new, sort_keys=False, allow_unicode=True), encoding="utf-8")
    console.print(
        f"[green]✓[/] {kid} set to [cyan]{mode_norm}[/] — wrote {path} "
        f"(backup: {backup.name}). Reload the screen-time engine to apply."
    )


# ── Box compatibility gate ────────────────────────────────────────────
#
# Enforcement strength is NOT uniform across Firewalla models. Router/DHCP
# modes put the box in the traffic path (blocks bite unconditionally); spoof
# mode enforces via ARP, so a kid's device that slips `monitored` is silently
# unenforced. Capacity values are read from the box firmware's own
# platform/*/Platform.js getPolicyCapacity() (verified on-box 2026-06-10):
# Red = 1000, everything else (Blue/Purple/Navy/Gold/GSE/Gold Pro/PSE) = 3000.
# The 2026-06-09 corpse-pile incident is why capacity is checked at all.

BOX_POLICY_CAPACITY: dict[str, int] = {"red": 1000}
DEFAULT_POLICY_CAPACITY = 3000

# Models the Sanctum stack has actually run against (Purple pre-2026-05
# migration, Gold Pro since) plus models sharing their platform code paths.
_KNOWN_MODELS = frozenset(
    {"red", "blue", "blueplus", "purple", "purplese", "navy", "gold", "gse", "goldpro", "pse"}
)
# In-path modes: every LAN packet traverses the box, blocks enforce regardless
# of per-device monitoring. Everything else depends on ARP spoofing.
_IN_PATH_MODES = frozenset({"router", "dhcp"})

_BRIDGE_URL_ENV = "FIREWALLA_BRIDGE_URL"
_BRIDGE_TOKEN_ENV = "FIREWALLA_BRIDGE_TOKEN"
_BRIDGE_TOKEN_FILE = Path.home() / ".sanctum/secrets/firewalla-bridge-token"


class CompatCheck(NamedTuple):
    """One compatibility assertion with an actionable fix."""

    name: str
    status: str  # "PASS" | "WARN" | "FAIL"
    detail: str
    fix: str | None


def assess_compat(
    info: dict[str, Any],
    policy_count: int | None,
    monitored: dict[str, bool | None] | None,
) -> list[CompatCheck]:
    """Pure compatibility assessment from the bridge's /info payload.

    ``monitored`` maps managed MACs to the box's per-device `monitored` flag
    (None = unknown); only consulted outside in-path modes, where it decides
    whether enforcement actually reaches each kid's device.
    """
    box = info.get("box") or {}
    caps = info.get("capabilities") or {}
    model = str(box.get("model") or "").lower()
    mode = str(caps.get("box_mode") or box.get("mode") or "").lower()
    checks: list[CompatCheck] = []

    if caps.get("enforcement_ready"):
        checks.append(CompatCheck("box-link", "PASS", "bridge paired and box API live", None))
    else:
        checks.append(
            CompatCheck(
                "box-link",
                "FAIL",
                "bridge reachable but box API not ready",
                "check pairing keys (~/.openclaw/firewalla/keys) + box connectivity",
            )
        )

    in_path = mode in _IN_PATH_MODES
    if in_path:
        checks.append(
            CompatCheck("mode", "PASS", f"{mode} mode — box is in the traffic path", None)
        )
    else:
        checks.append(
            CompatCheck(
                "mode",
                "WARN",
                f"{mode or 'unknown'} mode — enforcement depends on per-device "
                "monitoring (ARP spoof); an unmonitored device is NOT blocked",
                "keep Monitoring ON for every kid device in the Firewalla app",
            )
        )

    capacity = BOX_POLICY_CAPACITY.get(model, DEFAULT_POLICY_CAPACITY)
    if policy_count is None:
        checks.append(
            CompatCheck(
                "capacity",
                "WARN",
                f"could not read the box policy count (cap {capacity})",
                "retry; if persistent, check `GET /policies` on the bridge",
            )
        )
    else:
        pct = 100 * policy_count / capacity
        status = "PASS" if pct < 60 else ("WARN" if pct < 90 else "FAIL")
        checks.append(
            CompatCheck(
                "capacity",
                status,
                f"{policy_count}/{capacity} policy rules used ({pct:.0f}%)",
                None
                if status == "PASS"
                else "purge stale rules: bridge POST /policies/purge (see 2026-06-09 incident)",
            )
        )

    if model in _KNOWN_MODELS:
        checks.append(CompatCheck("model", "PASS", f"{model} — known platform", None))
    else:
        checks.append(
            CompatCheck(
                "model",
                "WARN",
                f"{model or 'unknown'} — Sanctum has not been validated on this model",
                "expected to work (shared firmware) — report issues",
            )
        )

    if not in_path and monitored is not None:
        unmon = sorted(m for m, v in monitored.items() if v is False)
        unknown = sorted(m for m, v in monitored.items() if v is None)
        if unmon:
            checks.append(
                CompatCheck(
                    "monitoring",
                    "FAIL",
                    "unmonitored managed device(s): " + ", ".join(unmon),
                    "enable Monitoring for these devices in the Firewalla app",
                )
            )
        elif unknown:
            checks.append(
                CompatCheck(
                    "monitoring",
                    "WARN",
                    "monitoring state unknown for: " + ", ".join(unknown),
                    "device may be offline — re-check when it joins the network",
                )
            )
        else:
            checks.append(CompatCheck("monitoring", "PASS", "all managed devices monitored", None))

    return checks


def _fetch_bridge_json(path: str) -> dict[str, Any] | None:
    """GET a bridge endpoint; None on any transport/auth failure (caller decides).

    Bridge reads are routed through the :class:`FirewallaProvider` HTTP seam
    (``sanctum_cli.devices.firewalla._fetch_bridge_json``) so the engine and the
    provider share ONE bridge transport. This module still owns the *resolution*
    of the bridge URL + bearer token from its own constants (env override →
    on-disk ``_BRIDGE_TOKEN_FILE`` fallback) and passes them in explicitly — the
    fail-soft ``dict | None`` contract is unchanged: no token → ``None`` without
    a probe; non-200 / non-JSON / non-dict / transport error → ``None``.
    """
    url = os.environ.get(_BRIDGE_URL_ENV, "http://127.0.0.1:1984")
    token = os.environ.get(_BRIDGE_TOKEN_ENV, "").strip()
    if not token:
        try:
            token = _BRIDGE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    if not token:
        return None
    return firewalla._fetch_bridge_json(path, url=url, token=token)


class PairingResult(NamedTuple):
    """Outcome of an authenticated bridge probe. ``ok`` is True ONLY for a
    genuine authenticated 200 — every other state is fail-closed (not paired)."""

    state: str  # paired | auth_rejected | unreachable | bad_response | no_token
    ok: bool
    detail: str


def validate_firewalla_pairing(url: str, token: str, *, timeout: float = 10.0) -> PairingResult:
    """Probe the bridge with the candidate token and classify the result.

    Fail-closed: onboarding writes ``enabled: true`` ONLY when this returns
    ``ok``. A wrong token (401/403), an unreachable bridge (connect error /
    timeout), or a 200 that isn't the expected host list all return ``ok=False``
    with a precise, actionable ``state`` — never a silent "looks fine".
    """
    import httpx

    token = (token or "").strip()
    if not token:
        return PairingResult("no_token", False, "no token provided — cannot authenticate")
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/hosts",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return PairingResult("unreachable", False, f"bridge unreachable at {url}: {exc}")
    if resp.status_code in (401, 403):
        return PairingResult(
            "auth_rejected", False, f"bridge rejected the token (HTTP {resp.status_code})"
        )
    if resp.status_code != 200:
        return PairingResult("bad_response", False, f"bridge returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        return PairingResult("bad_response", False, "bridge returned 200 but the body was not JSON")
    if not isinstance(data, (list, dict)):
        return PairingResult("bad_response", False, "bridge /hosts returned an unexpected shape")
    n = len(data)
    return PairingResult("paired", True, f"authenticated — bridge sees {n} device(s)")


def _managed_macs(config: dict[str, Any]) -> list[str]:
    """Every MAC the engine can be asked to enforce (family + shared + screens)."""
    macs: set[str] = set()
    for member in (config.get("family") or {}).values():
        if isinstance(member, dict):
            for dev in member.get("personal_devices") or []:
                if isinstance(dev, dict) and dev.get("mac"):
                    macs.add(str(dev["mac"]).upper())
    shared = config.get("shared_devices") or {}
    for dev_info in shared.values() if isinstance(shared, dict) else []:
        if isinstance(dev_info, dict) and dev_info.get("mac"):
            macs.add(str(dev_info["mac"]).upper())
    for screen in (config.get("screens") or {}).values():
        if isinstance(screen, dict):
            for mac in screen.get("macs") or []:
                macs.add(str(mac).upper())
    return sorted(macs)


def compat_command(*, strict: bool = False) -> None:
    """``sanctum screen-time compat`` — assert the box can enforce what we promise."""
    info = _fetch_bridge_json("/info")
    if info is None:
        raise LocalError(
            "Firewalla bridge unreachable — compatibility cannot be verified",
            fix="is com.sanctum.firewalla running? (`launchctl print system/com.sanctum.firewalla`)",
        )

    policies = _fetch_bridge_json("/policies")
    policy_count = policies.get("count") if isinstance(policies, dict) else None
    if not isinstance(policy_count, int):
        policy_count = None

    caps = info.get("capabilities") or {}
    mode = str(caps.get("box_mode") or (info.get("box") or {}).get("mode") or "").lower()
    monitored: dict[str, bool | None] | None = None
    if mode not in _IN_PATH_MODES:
        try:
            config = _load_devices(_resolve_devices_path())
        except LocalError:
            config = None
        if config:
            monitored = {}
            for mac in _managed_macs(config):
                host = _fetch_bridge_json(f"/host/{mac}")
                val = host.get("monitored") if isinstance(host, dict) else None
                monitored[mac] = val if isinstance(val, bool) else None

    checks = assess_compat(info, policy_count, monitored)

    box = info.get("box") or {}
    table = Table(
        title=f"Firewalla compatibility — {box.get('modelDisplay') or '?'} ({mode or '?'} mode)"
    )
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    table.add_column("fix")
    style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    for c in checks:
        table.add_row(c.name, Text(c.status, style=style[c.status]), c.detail, c.fix or "")
    console.print(table)

    fails = [c for c in checks if c.status == "FAIL"]
    warns = [c for c in checks if c.status == "WARN"]
    if fails:
        raise LocalError(
            "compatibility FAIL: " + "; ".join(f"{c.name}: {c.detail}" for c in fails),
            fix=fails[0].fix,
        )
    if warns and strict:
        raise LocalError(
            "compatibility WARN (strict): " + "; ".join(f"{c.name}: {c.detail}" for c in warns),
            fix=warns[0].fix,
        )
    console.print(
        "[green]✓ compatible[/]" + (f" [yellow]({len(warns)} warning(s))[/]" if warns else "")
    )
