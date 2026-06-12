"""``sanctum onboard --recipe family`` — one-shot productization flow.

The 30-second-to-working-backup demo. Composes existing primitives:

  1. Show the recipe + Photos-scope warning (family path) so the operator
     understands what is and isn't covered.
  2. Pre-flight estimate — does the recipe fit the chosen backend's free
     tier?
  3. Cloud setup wizard (R2 by default) if not already configured.
  4. First real backup with the recipe.
  5. Restore canary against ``~/.zshrc`` to prove the round-trip.
  6. Recipe gates, in listed order — family: the family interview (names,
     roles, smartphone numbers → the screen-time registry), then the
     Firewalla compatibility check.
  7. Done — print next-step status.

For the lambda-family audience: this is the only command they should
need to run. Existing operators can use the underlying primitives
(``cloud setup``, ``backup run --recipe``, etc.) directly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from sanctum_cli import config, recipes
from sanctum_cli.backends import b2, gdrive, r2
from sanctum_cli.commands import backup as backup_cmd
from sanctum_cli.errors import LocalError, UserError

console = Console()

# ── Optional-module gates ─────────────────────────────────────────────
# Post-backup steps, keyed by recipe name and run in listed order. A gate
# SKIPS (never blocks) when its module isn't installed/paired or when the
# run is non-interactive (--yes) — onboarding must not hard-fail on an
# optional module being absent, nor hang a scripted run on stdin — but
# runs STRICT when the module answers, so a brand-new operator sees a
# misconfiguration (fix included) before relying on it. Listed as data so
# tests can assert membership and ordering.
RECIPE_GATES: dict[str, tuple[str, ...]] = {
    "family": ("identity-setup", "family-setup", "firewalla-pairing", "firewalla-compat"),
}

# ── Surface polish ────────────────────────────────────────────────────
# The onboarding flow is the first time a new operator (or a friend of the
# operator) meets Sanctum. Apple-like principles: the splash celebrates,
# the completion thanks, the prompts are friendly. Functional content is
# unchanged; only the framing has personality.

_SANCTUM_SPLASH = """
   ╭─────────────────────────────────────────╮
   │        S   A   N   C   T   U   M        │
   ╰─────────────────────────────────────────╯
        your haus, your hardware, your AI
"""


def _print_splash() -> None:
    """Print the Sanctum welcome splash. Centered, cyan, terse."""
    splash = Text(_SANCTUM_SPLASH, style="bold cyan")
    console.print(Align.center(splash))
    try:
        who = os.getlogin()
    except OSError:
        who = os.environ.get("USER", "friend")
    console.print(
        Align.center(
            Text.assemble(
                ("Welcome, ", "dim"),
                (who, "bold"),
                (". Let's wake up your Sanctum.", "dim"),
            )
        )
    )
    console.print()


PHOTOS_SCOPE_NOTICE = (
    "[bold]Photos scope notice[/]\n\n"
    "[yellow]Sanctum does NOT back up your Photos library.[/]\n\n"
    "Apple manages it via iCloud Photos, and the bundle structure is "
    "hostile to backup tools. The wizard auto-detected the library and "
    "will skip it.\n\n"
    "What this recipe DOES cover: Documents, Desktop, ssh keys, dotfiles. "
    "Typical size after dedup: ~5 GB, fits R2 free tier (10 GB).\n\n"
    "For Photos: enable iCloud Photos in System Settings."
)


def onboard_command(
    recipe: Annotated[
        str,
        typer.Option(
            "--recipe", "-r", help="Recipe to onboard (family | operator | code | <user>)."
        ),
    ] = "family",
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help="Cloud backend if not already configured (r2 | b2 | gdrive).",
        ),
    ] = "r2",
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't auto-open browser tabs in cloud setup.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts.")] = False,
) -> None:
    """One-shot first-run: recipe → cloud setup → first backup → canary."""
    # ensure() (not load()) so a brand-new Mac with no ~/.sanctum/instance.yaml
    # gets a minimal one scaffolded here instead of hard-failing on first run.
    cfg = config.ensure()
    rcp = recipes.resolve(recipe, cfg.cli)

    _print_splash()

    console.print(
        Panel.fit(
            f"[bold]Sanctum onboarding — recipe: {recipe}[/]\n\n{rcp.description}",
            border_style="cyan",
        )
    )

    if recipe == "family":
        console.print()
        console.print(Panel.fit(PHOTOS_SCOPE_NOTICE, border_style="yellow"))

    # Step 1 — estimate
    console.print("\n[bold]Step 1.[/] Pre-flight estimate")
    backup_cmd.backup_estimate(recipe=recipe, json_output=False)

    if not yes and not Confirm.ask("\nproceed?", default=True):
        console.print("[dim]aborted by user[/]")
        raise typer.Exit(code=0)

    # Step 2 — cloud setup if needed
    cb = cfg.cli.cloud_backup
    needs_setup = (
        cb is None
        or (rcp.target == "primary" and cb.primary is None)
        or (rcp.target == "secondary" and cb.secondary is None)
    )
    if needs_setup:
        console.print(f"\n[bold]Step 2.[/] Cloud setup ({backend})")
        _dispatch_cloud_setup(backend, no_open=no_open)
        # Reload config after cloud setup writes to instance.yaml
        cfg = config.load()
    else:
        console.print(
            f"\n[bold]Step 2.[/] Cloud target already configured ({rcp.target}) — skipping setup."
        )

    # Step 3 — dry-run for transparency
    console.print("\n[bold]Step 3.[/] Dry-run (no bytes written)")
    backup_cmd.backup_run(recipe=recipe, script=None, dry_run=True)

    if not yes and not Confirm.ask("\nrun the real backup now?", default=True):
        console.print("[dim]stopped before live run; rerun with --yes when ready[/]")
        raise typer.Exit(code=0)

    # Step 4 — first real backup
    console.print("\n[bold]Step 4.[/] First backup")
    backup_cmd.backup_run(recipe=recipe, script=None, dry_run=False)

    # Step 5 — canary
    console.print("\n[bold]Step 5.[/] Restore canary")
    _run_canary()

    # Step 6+ — optional-module gates, in recipe-listed order
    # (family: interview → screen-time registry, then Firewalla compat)
    step_no = 6
    for gate in RECIPE_GATES.get(recipe, ()):
        if gate == "identity-setup":
            console.print(f"\n[bold]Step {step_no}.[/] Operator identity")
            _run_identity_setup(yes=yes)
        elif gate == "family-setup":
            console.print(f"\n[bold]Step {step_no}.[/] Family setup")
            _run_family_setup(yes=yes)
        elif gate == "firewalla-pairing":
            console.print(f"\n[bold]Step {step_no}.[/] Firewalla pairing")
            _run_firewalla_pairing(yes=yes)
        elif gate == "firewalla-compat":
            console.print(f"\n[bold]Step {step_no}.[/] Firewalla compatibility")
            _run_firewalla_compat()
        step_no += 1

    console.print()
    try:
        who = os.getlogin()
    except OSError:
        who = os.environ.get("USER", "friend")

    body = Group(
        Text.from_markup(f"[bold green]Your Sanctum is alive, {who}.[/]\n"),
        Text.from_markup(
            "It just ran its first backup and verified the restore by round-"
            "tripping a known file through your cloud bucket. From here, "
            "Sanctum keeps running in the background — daily backups, drift "
            "heals, audit trails — without asking you to do anything. The "
            "next time you'll hear from it is when something interesting "
            "happens.\n"
        ),
        Text.from_markup("[dim]A few things to try when you're curious:[/]"),
        Text.from_markup(
            "  [cyan]sanctum status[/]            the whole haus at a glance\n"
            "  [cyan]sanctum doctor[/]            deep health check\n"
            "  [cyan]sanctum backup snapshots[/]  list your backup history\n"
            "  [cyan]sanctum chat[/]              talk to your local agents\n"
        ),
        Text.from_markup("[dim italic]Welcome to your haus.[/]"),
    )

    console.print(
        Panel(
            body,
            title="[bold green]onboarding complete[/]",
            border_style="green",
            padding=(1, 2),
        )
    )


def _dispatch_cloud_setup(backend: str, *, no_open: bool) -> None:
    if backend == "r2":
        r2.run_wizard(auto_open=not no_open, persist=True)
    elif backend == "b2":
        b2.run_wizard(auto_open=not no_open, persist=True)
    elif backend == "gdrive":
        gdrive.run_wizard(auto_open=not no_open, persist=True)
    else:
        msg = f"unknown backend for onboarding: {backend!r}"
        raise UserError(msg)


# ── Family setup (interview) ──────────────────────────────────────────
# "Ask family members and smartphone numbers" — the interview seeds the
# screen-time registry (devices.yaml) so the module has people to protect
# the day it's paired. Pure logic (slug/phone/merge) is module-level so
# tests can hit it without a TTY.

_PHONE_FORMATTING_RE = re.compile(r"[\s\-().]")


def slugify_member(name: str) -> str:
    """Family-map key for a member: lowercased, non-alphanumerics stripped."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def normalize_phone(raw: str) -> tuple[str, bool] | None:
    """Loosely formatted phone input → E.164-ish ``+<digits>``.

    Spaces/dashes/parens/dots are formatting, never stored. A leading ``+``
    is trusted as-is (8 to 15 digits, per E.164's ceiling); a bare 10-digit
    number is assumed North American (``+1``) and flagged for operator
    confirmation — the second tuple element is True. Anything else returns
    None and the caller re-prompts.
    """
    cleaned = _PHONE_FORMATTING_RE.sub("", raw.strip())
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if digits.isdigit() and 8 <= len(digits) <= 15:
            return f"+{digits}", False
        return None
    if cleaned.isdigit() and len(cleaned) == 10:
        return f"+1{cleaned}", True
    return None


def merge_family_members(
    devices_config: dict[str, Any], members: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Merge interviewed members into a devices config — never clobbering.

    Returns ``(new_config, added, skipped)``. An existing member with the
    same slug is left exactly as found (skipped); only genuinely new slugs
    are added. The input config is not mutated.
    """
    import copy

    new = copy.deepcopy(devices_config)
    family = new.get("family")
    if not isinstance(family, dict):  # absent or a degenerate scalar `family:`
        family = {}
        new["family"] = family
    added: list[str] = []
    skipped: list[str] = []
    for slug, member in members.items():
        if slug in family:
            skipped.append(slug)
        else:
            family[slug] = member
            added.append(slug)
    return new, added, skipped


# ── Per-setup identity (beta portability) ─────────────────────────────
# Curfews key entirely on a child's device MACs (`c_<mac>_set`), and every
# alert/briefing needs an operator name + a number to reach. None of that is
# derivable — onboarding must collect it, or a beta haus finishes setup with
# nothing to enforce and no one to notify. These helpers are pure (no TTY) so
# the parsing/merge logic is unit-tested without a terminal.

_MAC_SEP_RE = re.compile(r"[\s:.\-]")


def normalize_mac(raw: str) -> str | None:
    """Any common MAC spelling → canonical ``AA:BB:CC:DD:EE:FF``; junk → None.

    Accepts colon/dash/dot/cisco/bare forms, strips whitespace, uppercases.
    Returns None for anything that isn't exactly 12 hex digits.
    """
    hexs = _MAC_SEP_RE.sub("", raw.strip()).upper()
    if len(hexs) != 12 or any(c not in "0123456789ABCDEF" for c in hexs):
        return None
    return ":".join(hexs[i : i + 2] for i in range(0, 12, 2))


def parse_device_selection(raw: str, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve a picker selection ('1,3' / '2 1' / 'all' / '') to chosen devices.

    1-indexed, order-preserving, deduped. Out-of-range and non-numeric tokens
    are ignored. Empty input → no selection (caller falls back to manual entry).
    """
    raw = raw.strip().lower()
    if not raw:
        return []
    if raw in ("all", "*"):
        return list(devices)
    picks: list[dict[str, Any]] = []
    for tok in re.split(r"[,\s]+", raw):
        if tok.isdigit():
            i = int(tok) - 1
            if 0 <= i < len(devices) and devices[i] not in picks:
                picks.append(devices[i])
    return picks


def set_instance_identity(
    owner_name: str | None, signal_target: str | None, path: Path | None = None
) -> None:
    """Write owner name + Signal alert number into ``notifications`` in instance.yaml.

    Raw read-modify-write (matching ``r2._persist``): every other block is
    preserved. Backs up to ``<file>.bak`` first. Empty args are no-ops, so a
    user who skips one field doesn't clobber an existing value.
    """
    target = Path(path) if path else config.instance_path()
    data: dict[str, Any] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    notif = data.get("notifications")
    if not isinstance(notif, dict):
        notif = data["notifications"] = {}
    if owner_name:
        notif["owner_name"] = owner_name
    if signal_target:
        sig = notif.get("signal")
        if not isinstance(sig, dict):
            sig = notif["signal"] = {}
        sig.setdefault("enabled", True)
        sig["target"] = signal_target
    if target.exists():
        backup = target.parent / (target.name + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _prompt_phone(name: str) -> str | None:
    """Optional smartphone number for a child — loops until valid or skipped."""
    while True:
        raw = Prompt.ask(
            f"  {name}'s smartphone number — for bedtime courtesy notices via "
            "iMessage (enter to skip)",
            default="",
            show_default=False,
        ).strip()
        if not raw:
            return None
        normalized = normalize_phone(raw)
        if normalized is None:
            console.print(
                "  [red]couldn't parse that[/] — try +<country><number> or a "
                "10-digit North American number"
            )
            continue
        phone, assumed_na = normalized
        if assumed_na and not Confirm.ask(
            f"  assuming North America → {phone} — correct?", default=True
        ):
            continue
        return phone


def _fetch_firewalla_devices() -> list[dict[str, Any]] | None:
    """Live device inventory from the paired Firewalla, normalized to ``{name, mac}``.

    None when the bridge isn't reachable/paired — onboarding then falls back to
    manual MAC entry. Best-effort: any shape surprise yields None, never raises.
    """
    from sanctum_cli.commands import screen_time

    hosts = screen_time._fetch_bridge_json("/hosts")
    if not isinstance(hosts, list):
        return None
    out: list[dict[str, Any]] = []
    for h in hosts:
        if not isinstance(h, dict):
            continue
        mac = normalize_mac(str(h.get("mac") or h.get("macAddress") or ""))
        if not mac:
            continue
        label = str(h.get("name") or h.get("hostname") or h.get("localDomain") or mac)
        out.append({"name": label, "mac": mac})
    return out or None


def _collect_child_devices(name: str) -> list[dict[str, Any]]:
    """Collect a child's device MACs for curfew enforcement → ``[{name, mac}]``.

    Prefers a live picker against the paired Firewalla (re-runs on a configured
    haus); falls back to manual MAC entry when the bridge isn't reachable (the
    common first-run case).
    """
    fleet = _fetch_firewalla_devices()
    if fleet:
        console.print(
            f"  [dim]Devices on your network — pick {name}'s (e.g. 1,3 or 'all', enter to skip):[/]"
        )
        for i, dev in enumerate(fleet, 1):
            console.print(f"    {i}. {dev['name']}  [dim]{dev['mac']}[/]")
        picked = parse_device_selection(
            Prompt.ask(f"  which are {name}'s?", default="", show_default=False), fleet
        )
        if picked:
            return [{"name": d["name"], "mac": d["mac"]} for d in picked]
        # nothing picked → fall through to manual entry

    devices: list[dict[str, Any]] = []
    console.print(
        f"  Add {name}'s device MAC addresses so curfews can enforce on them "
        "(enter an empty MAC when done)."
    )
    while True:
        raw_mac = Prompt.ask(
            f"  {name}'s device MAC (enter when done)", default="", show_default=False
        ).strip()
        if not raw_mac:
            return devices
        mac = normalize_mac(raw_mac)
        if not mac:
            console.print("  [red]not a MAC[/] — use AA:BB:CC:DD:EE:FF")
            continue
        label = Prompt.ask("  device label", default=f"{name}'s device").strip()
        devices.append({"name": label or f"{name}'s device", "mac": mac})


def _interview_family() -> dict[str, dict[str, Any]]:
    """Ask for family members + smartphone numbers until the operator is done."""
    console.print(
        "  Who lives in the haus? Names and roles seed the screen-time "
        "registry; a child's smartphone number lets Sanctum send bedtime "
        "courtesy notices via iMessage. Press enter on an empty name when "
        "you're done."
    )
    members: dict[str, dict[str, Any]] = {}
    while True:
        name = Prompt.ask(
            "\n  family member name (enter when done)", default="", show_default=False
        ).strip()
        if not name:
            return members
        slug = slugify_member(name)
        if not slug:
            console.print("  [red]a name needs at least one letter or digit[/]")
            continue
        if slug in members:
            console.print(f"  [yellow]already added[/] {slug} this session — skipping")
            continue
        role = Prompt.ask("  role", choices=["child", "parent"], default="child")
        member: dict[str, Any] = {"role": role, "personal_devices": []}
        if role == "child":
            phone = _prompt_phone(name)
            if phone:
                member["notify_imessage"] = phone
            # The curfew engine enforces on these MACs — without them the whole
            # screen-time feature is inert. Collect them at interview time.
            member["personal_devices"] = _collect_child_devices(name)
        members[slug] = member


def _devices_write_path() -> Path:
    """Where the family registry lives — env override, else existing file, else default.

    Mirrors ``screen_time._resolve_devices_path`` but returns the canonical
    default instead of raising when nothing exists yet: onboarding is
    exactly the moment the file gets created.
    """
    from sanctum_cli.commands import screen_time

    override = os.environ.get(screen_time.ENV_DEVICES_FILE)
    if override:
        return Path(override).expanduser()
    for candidate in screen_time._DEVICES_CANDIDATES:
        if candidate.is_file():
            return candidate
    return screen_time._DEVICES_CANDIDATES[0]


def _run_family_setup(*, yes: bool) -> None:
    """Family interview gate — names, roles, smartphone numbers → devices.yaml.

    Interactive by design, so ``--yes`` SKIPS it (prompting a scripted run
    against a closed stdin would hang). Merge never clobbers: re-running
    onboarding on a configured haus only adds new people; an existing slug
    is skipped with a note. A fresh file gets the full engine-loadable
    skeleton (family + empty shared_devices/screens maps).
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` "
            "without --yes to set up the family"
        )
        return

    members = _interview_family()
    if not members:
        console.print("  [dim]no family members added — registry untouched[/]")
        return

    from sanctum_cli.commands import screen_time

    path = _devices_write_path()
    if path.is_file():
        try:
            devices_config = screen_time._load_devices(path)
        except LocalError as exc:
            console.print(f"  [red]✗[/] {exc.message}")
            if exc.fix:
                console.print(f"  [dim]fix: {exc.fix}[/]")
            console.print("  [dim]registry not written — fix the file and re-run[/]")
            return
        merged, added, skipped = merge_family_members(devices_config, members)
        for slug in skipped:
            console.print(f"  [yellow]skipped[/] {slug} — already in {path.name}; not clobbering")
        if not added:
            console.print("  [dim]nothing new to write[/]")
            return
        backup = path.parent / (path.name + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        console.print(
            f"  [green]✓[/] added {len(added)} member(s) to {path} (backup: {backup.name})"
        )
    else:
        skeleton: dict[str, Any] = {"family": members, "shared_devices": {}, "screens": {}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(skeleton, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        console.print(f"  [green]✓[/] wrote {len(members)} member(s) to new {path}")


def _prompt_signal_target() -> str | None:
    """Operator's own Signal/iMessage number for alerts — loops until valid or skipped."""
    while True:
        raw = Prompt.ask(
            "  your phone number for Sanctum alerts (Signal/iMessage; enter to skip)",
            default="",
            show_default=False,
        ).strip()
        if not raw:
            return None
        normalized = normalize_phone(raw)
        if normalized is None:
            console.print("  [red]couldn't parse that[/] — try +<country><number> or 10 digits")
            continue
        phone, assumed_na = normalized
        if assumed_na and not Confirm.ask(
            f"  assuming North America → {phone} — correct?", default=True
        ):
            continue
        return phone


def _identity_configured(path: Path | None = None) -> bool:
    """True when notifications.owner_name AND signal.target are both already set."""
    target = path or config.instance_path()
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    notif = data.get("notifications") if isinstance(data, dict) else None
    if not isinstance(notif, dict):
        return False
    sig = notif.get("signal")
    number = sig.get("target") if isinstance(sig, dict) else None
    return bool(notif.get("owner_name")) and bool(number)


def _run_identity_setup(*, yes: bool) -> None:
    """Collect operator name + Signal alert number → instance.yaml notifications.

    Interactive (``--yes`` skips). Skips silently when already configured so
    re-runs don't re-prompt. Without it a fresh haus has no one to address in
    briefings and nowhere to send alerts (they'd otherwise have to fall back to
    a baked-in number — exactly the per-setup leak we're closing).
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` "
            "without --yes to set your name + alert number"
        )
        return
    if _identity_configured():
        console.print("  [dim]operator identity already configured — skipping[/]")
        return
    try:
        who = os.getlogin()
    except OSError:
        who = os.environ.get("USER", "")
    owner = Prompt.ask("  your name (how Sanctum addresses you)", default=who or "Operator").strip()
    number = _prompt_signal_target()
    set_instance_identity(owner or None, number)
    summary = ", ".join(
        bit
        for bit in (f"name={owner}" if owner else "", f"alerts→{number}" if number else "")
        if bit
    )
    console.print(f"  [green]✓[/] saved operator identity ({summary or 'nothing entered'})")


def set_firewalla_bridge(
    *,
    token: str,
    device_ip: str,
    device_mac: str,
    port: int,
    path: Path | None = None,
    token_file: Path | None = None,
) -> None:
    """Persist a VALIDATED Firewalla bridge pairing.

    Writes ``services.firewalla_bridge`` (enabled + port + device_ip/mac) into
    instance.yaml via raw read-modify-write (sibling blocks preserved), and the
    token into the mode-600 secrets file — NEVER into instance.yaml (which is
    world-readable config, not a secret store). Callers must only invoke this
    after :func:`screen_time.validate_firewalla_pairing` returns ``ok``.
    """
    target = Path(path) if path else config.instance_path()
    data: dict[str, Any] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    services = data.get("services")
    if not isinstance(services, dict):
        services = data["services"] = {}
    services["firewalla_bridge"] = {
        "enabled": True,
        "port": port,
        "device_ip": device_ip,
        "device_mac": device_mac,
    }
    if target.exists():
        backup = target.parent / (target.name + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # Token → secrets file, fail-closed perms (600). Created before write so the
    # token never lands on disk world-readable even briefly.
    tf = Path(token_file) if token_file else (Path.home() / ".sanctum/secrets/firewalla-bridge-token")
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.touch(mode=0o600, exist_ok=True)
    tf.chmod(0o600)
    tf.write_text(token.strip() + "\n", encoding="utf-8")


def _run_firewalla_pairing(*, yes: bool) -> None:
    """Interactive Firewalla bridge pairing — fail-closed.

    Collects the bridge URL/token + device IP/MAC, runs an AUTHENTICATED probe
    (:func:`screen_time.validate_firewalla_pairing`), and persists the pairing
    ONLY on a genuine authenticated 200. A wrong token, an unreachable bridge,
    or a malformed response is surfaced with the precise reason and the pairing
    is NOT written — because a curfew engine pointed at an unpaired bridge
    enforces nothing, and a false "paired" hides that until the first missed
    bedtime. ``--yes`` skips (interactive); re-run later via the same gate.
    """
    from sanctum_cli.commands import screen_time

    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` without "
            "--yes to pair the Firewalla bridge"
        )
        return
    if not Confirm.ask("  pair the Firewalla screen-time bridge now?", default=True):
        console.print("  [dim]skipped — curfews stay inert until the bridge is paired[/]")
        return

    url = Prompt.ask("  bridge URL", default="http://127.0.0.1:1984").strip()
    for attempt in range(3):
        token = Prompt.ask("  bridge token", password=True).strip()
        result = screen_time.validate_firewalla_pairing(url, token)
        if result.ok:
            device_ip = Prompt.ask("  Firewalla device IP (LAN gateway)", default="").strip()
            device_mac = Prompt.ask("  Firewalla device MAC", default="").strip()
            set_firewalla_bridge(
                token=token,
                device_ip=device_ip,
                device_mac=device_mac,
                port=_port_from_url(url),
            )
            console.print(f"  [green]✓[/] bridge paired — {result.detail}")
            return
        console.print(f"  [red]✗[/] not paired: {result.detail}")
        if result.state == "auth_rejected" and attempt < 2:
            console.print("  [dim]check the token and try again[/]")
            continue
        break
    console.print(
        "  [yellow]bridge NOT paired[/] — screen-time curfews will not enforce until you "
        "complete pairing. Re-run `sanctum onboard` (or `sanctum screen-time compat`) after "
        "fixing the bridge."
    )


def _port_from_url(url: str) -> int:
    """Extract the port from a bridge URL; default 1984."""
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    return parsed.port or 1984


def _run_firewalla_compat() -> None:
    """Firewalla screen-time gate — skip-if-absent, strict-if-present.

    The /info probe distinguishes "module not paired" (bridge unreachable or
    no token → SKIP, onboarding continues) from "paired but incompatible" —
    ``compat_command`` raises :class:`LocalError` for both cases, so we probe
    first instead of parsing exception messages. When the bridge answers, the
    assessment runs STRICT: a spoof-mode box or a near-capacity policy table
    is surfaced to the brand-new operator *now*, fix text included, instead
    of via the first silently-unenforced curfew. The verdict is loud but
    non-blocking (same stance as the restore canary): the backup already
    succeeded, and `sanctum screen-time compat --strict` is the hard gate.
    """
    from sanctum_cli.commands import screen_time

    if screen_time._fetch_bridge_json("/info") is None:
        console.print(
            "  [yellow]skipped[/] — screen-time module not paired yet — "
            "run `sanctum screen-time compat` after pairing"
        )
        return
    try:
        # Prints the per-check table (status + fix columns) before raising.
        screen_time.compat_command(strict=True)
    except LocalError as exc:
        console.print(f"  [red]✗[/] {exc.message}")
        if exc.fix:
            console.print(f"  [dim]fix: {exc.fix}[/]")


def _run_canary() -> None:
    """Lightweight canary — restore ~/.zshrc, sha256-diff against live."""
    import hashlib
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    cfg = config.load()
    cb = cfg.cli.cloud_backup
    if cb is None or cb.primary is None:
        console.print(
            "  [yellow]skipped[/] — no cloud_backup.primary; canary needs a configured repo"
        )
        return
    probe = Path("~/.zshrc").expanduser()
    if not probe.exists():
        console.print("  [yellow]skipped[/] — no ~/.zshrc on this host; nothing to round-trip")
        return
    live_sha = hashlib.sha256(probe.read_bytes()).hexdigest()
    console.print(f"  live ~/.zshrc sha256={live_sha[:16]}…")

    from sanctum_cli import keychain

    env = dict(os.environ)
    env["RESTIC_PASSWORD"] = keychain.read(
        account=cb.primary.keychain.account, service=cb.primary.keychain.service
    )
    with tempfile.TemporaryDirectory(prefix="sanctum-onboard-canary-") as tmp:
        proc = subprocess.run(
            [
                "restic",
                "-r",
                cb.primary.repo,
                "restore",
                "latest",
                "--target",
                tmp,
                "--include",
                str(probe),
                "--no-lock",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            console.print(f"  [red]✗[/] restic restore failed: {proc.stderr.strip()[:160]}")
            return
        restored = Path(tmp) / probe.relative_to(probe.anchor)
        if not restored.exists():
            console.print(f"  [yellow]skipped[/] — restored file not found at {restored}")
            return
        restored_sha = hashlib.sha256(restored.read_bytes()).hexdigest()
        if restored_sha == live_sha:
            console.print("  [green]✓[/] canary survived round-trip")
        else:
            console.print(
                f"  [red]✗[/] canary diff: live={live_sha[:16]} != restored={restored_sha[:16]}"
            )
