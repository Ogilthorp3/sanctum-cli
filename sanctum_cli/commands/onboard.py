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

import contextlib
import enum
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

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
from sanctum_cli.onboard_experience import chapter_banner, green_check, recap_card
from sanctum_cli.providers.base import HealthSnapshot

if TYPE_CHECKING:
    from sanctum_cli.devices.base import DeviceProvider, NetContext

console = Console()


# ── Restore-canary outcome (honest verify) ────────────────────────────
# The "Your Data" chapter proves the backup round-trips by restoring a known file
# and sha-diffing it against live. The orchestrator must report what ACTUALLY
# happened — never a blanket "verified" — so the canary returns a tri-state the
# recap + green-check thread, exactly as the AI/Network/You gates thread their
# CONFIGURED bool (design spec §2: "guided + verify, never 'probably worked'"). The
# canary is NON-BLOCKING: every path returns an outcome, none raises, onboarding
# continues regardless.
class CanaryOutcome(enum.Enum):
    """The honest result of the restore canary.

    * ``VERIFIED`` — the round-trip succeeded (restored sha == live sha).
    * ``SKIPPED`` — the canary could not run (no configured repo, no ``~/.zshrc``,
      restored file absent): not a failure, just nothing to prove.
    * ``FAILED`` — the canary RAN and the restore did NOT round-trip (restic restore
      errored, or the restored bytes differ from live): a real backup-integrity
      problem the operator must see, never papered over with a green check.
    """

    VERIFIED = "verified"
    SKIPPED = "skipped"
    FAILED = "failed"


def _canary_recap_status(outcome: CanaryOutcome) -> str:
    """Map a canary outcome to its recap-card row status string.

    VERIFIED → a configured/verified status; SKIPPED → a gentle "skipped"; FAILED →
    an honest "FAILED — needs attention". Shared by the orchestrator and the recap
    test so the test's expectation is DERIVED from the outcome (not hard-coded) — a
    test cannot catch a bug it shares.
    """
    return {
        CanaryOutcome.VERIFIED: "backup + canary ✓",
        CanaryOutcome.SKIPPED: "backup ✓ · canary skipped",
        CanaryOutcome.FAILED: "FAILED — needs attention",
    }[outcome]


# ── Optional-module gates ─────────────────────────────────────────────
# Post-backup steps, keyed by recipe name and run in listed order. A gate
# SKIPS (never blocks) when its module isn't installed/paired or when the
# run is non-interactive (--yes) — onboarding must not hard-fail on an
# optional module being absent, nor hang a scripted run on stdin — but
# runs STRICT when the module answers, so a brand-new operator sees a
# misconfiguration (fix included) before relying on it. Listed as data so
# tests can assert membership and ordering.
RECIPE_GATES: dict[str, tuple[str, ...]] = {
    "family": (
        "identity-setup",
        "family-setup",
        "ai-providers",
        "firewalla-pairing",
        "firewalla-compat",
        "network-gear",
    ),
    # The Apple arc is UNIVERSAL — the recipe only chooses the backup scope, so the
    # "You" (identity) and "Your AI" chapters run on every recipe. operator/code are
    # non-family contexts (no kids → no screen-time/Firewalla gates, no family
    # interview), so their tuples are the minimal universal pair: operator identity,
    # then the AI providers. ai-providers is placed AFTER identity-setup and (per the
    # tools-before-data ordering decision) ahead of any network gates — matching the
    # family ordering and the _CHAPTER_GATES partition, so the arc reads identically.
    "operator": (
        "identity-setup",
        "ai-providers",
    ),
    "code": (
        "identity-setup",
        "ai-providers",
    ),
}

# ── The Apple-grade arc (experience framing) ──────────────────────────
# The onboarding flow is narrated as five named chapters, each with a one-line
# *why* and a persistent "Step N of M" counter, capped by a recap card + the
# celebratory "alive" panel ("You're Alive"). The recipe gates are partitioned
# into the gate-driven chapters (You / Your AI / Your Network) by name; a chapter
# whose gates aren't in this recipe still renders (the arc is universal) — its
# recap row just reads "skipped". "Welcome" (splash + recipe panel) and "Your Data"
# (the cloud/backup/canary block) are not gate-driven and are handled inline.
#
# ORDERING DECISION (design spec §5 wanted "tools before data"): we FIRST wrote a
# characterization test pinning the current step order, then TRIED moving the recipe
# gates ahead of the cloud/backup block. That reorder BROKE 18 existing interactive
# onboard tests — their stdin sequences assume the proceed/backup confirms are
# consumed BEFORE the gate prompts (the data block ran first historically). Per the
# plan's explicit fallback ("if the reorder breaks anything, DO NOT force it — keep
# the existing order and frame the arc + recap over it"), we KEPT the existing
# execution order (Welcome → Your Data → You → Your AI → Your Network) and let the
# recap card tie the journey together. The data block's internal estimate → backup →
# canary order is preserved (pinned by test_onboard_data_block_internal_order_is_invariant).

#: The arc's named chapters, in EXECUTION order, with their calm one-line *why*.
#: The "Step N of M" counter is 1-indexed over this tuple; M is its length.
_ARC_CHAPTERS: tuple[tuple[str, str], ...] = (
    ("Welcome", "Let's wake up your Sanctum — here's what this recipe covers."),
    ("Your Data", "Back up what matters and prove the restore round-trips."),
    ("You", "Who you are, and who's in the haus — so Sanctum knows who to protect."),
    ("Your AI", "Sanctum routes your prompts to the best model — let's connect yours."),
    ("Your Network", "Pair the gear that guards your haus — your hub, mesh, and firewall."),
)

#: Which recipe gates belong to which gate-driven chapter title. The orchestrator
#: runs only the gates present in the active recipe, in RECIPE_GATES order; a
#: chapter with no matching gate in this recipe still shows its banner + a recap row.
_CHAPTER_GATES: dict[str, tuple[str, ...]] = {
    "You": ("identity-setup", "family-setup"),
    "Your AI": ("ai-providers",),
    "Your Network": ("firewalla-pairing", "firewalla-compat", "network-gear"),
}

#: Human label shown per gate inside the per-step header (calm, lowercase-free).
_GATE_LABELS: dict[str, str] = {
    "identity-setup": "Operator identity",
    "family-setup": "Family setup",
    "ai-providers": "AI providers",
    "firewalla-pairing": "Firewalla pairing",
    "firewalla-compat": "Firewalla compatibility",
    "network-gear": "Network gear",
}


def _run_gate(gate: str, *, yes: bool) -> bool:
    """Dispatch a single recipe gate to its handler (presentation lives in the arc).

    Returns the handler's CONFIGURED signal: ``True`` iff the gate actually
    persisted something real (a provider verified, a device paired, an identity
    saved), ``False`` when it skipped / configured nothing. This is the truth the
    chapter loop threads into the recap + green-check — never "did we run it", which
    is the false-"connected" defect this dispatcher's return value closes. A gate
    name with no handler is a silent no-op that configured nothing (``False``) —
    defensive; the arc tuples are the source of truth.
    """
    if gate == "identity-setup":
        return _run_identity_setup(yes=yes)
    if gate == "family-setup":
        return _run_family_setup(yes=yes)
    if gate == "ai-providers":
        return _run_ai_providers(yes=yes)
    if gate == "firewalla-pairing":
        return _run_firewalla_pairing(yes=yes)
    if gate == "firewalla-compat":
        return _run_firewalla_compat()
    if gate == "network-gear":
        return _run_network_gear(yes=yes)
    return False


def _chapter_active_gates(chapter_title: str, recipe: str) -> tuple[str, ...]:
    """The gates for ``chapter_title`` that the active ``recipe`` actually lists.

    Preserves RECIPE_GATES order (so per-recipe ordering tests stay authoritative),
    filtered to the chapter's gate set. A chapter with nothing in this recipe yields
    an empty tuple — the orchestrator still shows its banner (the arc is universal),
    its recap row just reads "skipped".
    """
    recipe_gates = RECIPE_GATES.get(recipe, ())
    chapter_gates = _CHAPTER_GATES.get(chapter_title, ())
    return tuple(g for g in recipe_gates if g in chapter_gates)


def _run_chapter_gates(chapter_title: str, *, recipe: str, yes: bool) -> bool:
    """Run every active gate for a chapter, each under its own per-step header.

    Returns True iff at least one gate in the chapter actually CONFIGURED something
    (persisted a verified provider / paired a device / saved an identity) — the
    truth the recap + green-check use to show "set up / connected / paired" vs a
    gentle "skipped". This is the honest signal, NOT "did we run interactively":
    an interactive run where the user enters no key / detects no gear configures
    nothing and must read "skipped", never a false "connected" (design spec §2/§11,
    "guided + verify — never 'probably worked'"). Under ``--yes`` every gate skips
    (each prints its own note) and returns False, so the chapter reports "skipped"
    honestly. The per-gate booleans are OR-ed, so a chapter with one configured gate
    and one skipped gate still reads as configured.
    """
    gates = _chapter_active_gates(chapter_title, recipe)
    configured = False
    for gate in gates:
        label = _GATE_LABELS.get(gate, gate)
        console.print(f"\n  [bold]{label}[/]")
        configured = _run_gate(gate, yes=yes) or configured
    return configured


def _run_data_chapter(
    *,
    recipe: str,
    backend: str,
    no_open: bool,
    cfg: config.Config,
    rcp: config.Recipe,
    yes: bool,
) -> CanaryOutcome:
    """The "Your Data" chapter: estimate → cloud setup → dry-run → backup → canary.

    Returns the restore canary's :class:`CanaryOutcome` so the orchestrator can gate
    the green-check + recap row on the REAL round-trip result (never a blanket
    "verified"). The internal order is invariant (pinned by
    ``test_onboard_data_block_internal_order_is_invariant``): the pre-flight estimate
    precedes the backup runs, which precede the restore canary (the canary
    round-trips exactly what the live backup just wrote). The two interactive
    confirms (``proceed?`` / ``run the real backup now?``) still ``Exit(0)`` on a
    decline — the tool chapters above have already completed, so a user who stops
    here keeps their configured AI/network gear (forgiving, tools-before-data).
    ``--yes`` skips both confirms.
    """
    # Pre-flight estimate.
    console.print("\n  [bold]Pre-flight estimate[/]")
    backup_cmd.backup_estimate(recipe=recipe, json_output=False)

    if not yes and not Confirm.ask("\nproceed?", default=True):
        console.print("[dim]aborted by user[/]")
        raise typer.Exit(code=0)

    # Cloud setup if needed.
    cb = cfg.cli.cloud_backup
    needs_setup = (
        cb is None
        or (rcp.target == "primary" and cb.primary is None)
        or (rcp.target == "secondary" and cb.secondary is None)
    )
    if needs_setup:
        console.print(f"\n  [bold]Cloud setup ({backend})[/]")
        _dispatch_cloud_setup(backend, no_open=no_open)
    else:
        console.print(
            f"\n  [bold]Cloud target already configured ({rcp.target})[/] — skipping setup."
        )

    # Dry-run for transparency.
    console.print("\n  [bold]Dry-run (no bytes written)[/]")
    backup_cmd.backup_run(recipe=recipe, script=None, dry_run=True)

    if not yes and not Confirm.ask("\nrun the real backup now?", default=True):
        console.print("[dim]stopped before live run; rerun with --yes when ready[/]")
        raise typer.Exit(code=0)

    # First real backup.
    console.print("\n  [bold]First backup[/]")
    backup_cmd.backup_run(recipe=recipe, script=None, dry_run=False)

    # Restore canary.
    console.print("\n  [bold]Restore canary[/]")
    return _run_canary()


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
    """One-shot first-run: a narrated arc — Welcome → Your Data → You → Your AI → Your Network."""
    # ensure() (not load()) so a brand-new Mac with no ~/.sanctum/instance.yaml
    # gets a minimal one scaffolded here instead of hard-failing on first run.
    cfg = config.ensure()
    rcp = recipes.resolve(recipe, cfg.cli)

    total = len(_ARC_CHAPTERS)
    recap_rows: list[tuple[str, str]] = []

    # ── Chapter 1 — Welcome (splash + recipe panel + Photos notice) ──
    title, why = _ARC_CHAPTERS[0]
    console.print(chapter_banner(1, total, title, why))
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
    console.print(green_check("Welcome to Sanctum"))

    # ── Chapter 2 — Your Data (cloud setup → backup → canary) ──
    # ORDERING: the existing flow runs the cloud/backup block FIRST, then the recipe
    # gates. The characterized reorder (gates → data) broke 18 existing interactive
    # onboard tests whose stdin sequence assumes the proceed/backup confirms come
    # before the gate prompts — so per the plan we DID NOT force it and KEPT the
    # existing order, framing the Apple arc + recap over it instead. The recap card
    # (rendered at the end) is what ties the journey together regardless of the
    # execution order. The data block's internal estimate → backup → canary order is
    # preserved (pinned by test_onboard_data_block_internal_order_is_invariant).
    title, why = _ARC_CHAPTERS[1]
    console.print(chapter_banner(2, total, title, why))
    canary = _run_data_chapter(
        recipe=recipe, backend=backend, no_open=no_open, cfg=cfg, rcp=rcp, yes=yes
    )
    # HONEST VERIFY: the green-check + recap row reflect the REAL round-trip outcome —
    # never a blanket "verified". Mirrors how the AI/Network/You gates thread their
    # CONFIGURED bool (design spec §2: "guided + verify, never 'probably worked'").
    if canary is CanaryOutcome.VERIFIED:
        console.print(green_check("Backup verified by restore canary"))
    elif canary is CanaryOutcome.FAILED:
        console.print(
            Text.assemble(
                ("  ✗ ", "bold red"),
                ("Backup canary FAILED — your restore did not round-trip", "red"),
            )
        )
    else:  # SKIPPED
        console.print("  [dim]Backup canary skipped — nothing to round-trip[/]")
    recap_rows.append((title, _canary_recap_status(canary)))

    # ── Chapter 3 — You (operator identity + family setup gates) ──
    title, why = _ARC_CHAPTERS[2]
    console.print(chapter_banner(3, total, title, why))
    you_did = _run_chapter_gates("You", recipe=recipe, yes=yes)
    console.print(green_check("Operator + family set up" if you_did else "You step ready"))
    recap_rows.append((title, "set up" if you_did else "skipped"))

    # ── Chapter 4 — Your AI (ai-providers gate) ──
    title, why = _ARC_CHAPTERS[3]
    console.print(chapter_banner(4, total, title, why))
    ai_did = _run_chapter_gates("Your AI", recipe=recipe, yes=yes)
    console.print(green_check("AI connected" if ai_did else "AI step ready"))
    recap_rows.append((title, "connected" if ai_did else "skipped"))

    # ── Chapter 5 — Your Network (firewalla + network-gear gates) ──
    title, why = _ARC_CHAPTERS[4]
    console.print(chapter_banner(5, total, title, why))
    net_did = _run_chapter_gates("Your Network", recipe=recipe, yes=yes)
    console.print(green_check("Network paired" if net_did else "Network step ready"))
    recap_rows.append((title, "paired" if net_did else "skipped"))

    # ── You're Alive — recap card + the celebratory ending ──
    recap_rows.insert(0, ("Welcome", "ready"))
    recap_rows.append(("Offline fallback", "always on (mlx_local)"))
    console.print(recap_card(recap_rows))
    console.print(green_check("Setup verified — your Sanctum is alive"))

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


def _run_family_setup(*, yes: bool) -> bool:
    """Family interview gate — names, roles, smartphone numbers → devices.yaml.

    Returns True iff at least one NEW member was actually written to the registry;
    returns False when ``--yes`` skips, no member was entered, nothing new was
    added, or the file could not be loaded — so the recap reads "skipped" rather
    than a false "set up" (design spec §2/§11). Interactive by design, so ``--yes``
    SKIPS it (prompting a scripted run against a closed stdin would hang). Merge
    never clobbers: re-running onboarding on a configured haus only adds new people;
    an existing slug is skipped with a note. A fresh file gets the full
    engine-loadable skeleton (family + empty shared_devices/screens maps).
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` "
            "without --yes to set up the family"
        )
        return False

    members = _interview_family()
    if not members:
        console.print("  [dim]no family members added — registry untouched[/]")
        return False

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
            return False
        merged, added, skipped = merge_family_members(devices_config, members)
        for slug in skipped:
            console.print(f"  [yellow]skipped[/] {slug} — already in {path.name}; not clobbering")
        if not added:
            console.print("  [dim]nothing new to write[/]")
            return False
        backup = path.parent / (path.name + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        console.print(
            f"  [green]✓[/] added {len(added)} member(s) to {path} (backup: {backup.name})"
        )
        return True
    skeleton: dict[str, Any] = {"family": members, "shared_devices": {}, "screens": {}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(skeleton, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    console.print(f"  [green]✓[/] wrote {len(members)} member(s) to new {path}")
    return True


# ── AI-provider chapter ("Your AI") ──────────────────────────────────
# The first chapter that connects the user's models. It reuses the P4 cred-capture
# pattern (prompt → fail-closed Keychain → best-effort trifecta mirror → health-
# probe → revoke-on-failure), generalized from device admin secrets to AI-provider
# API keys. Claude is offered two ways, defaulting to the $0 Max/Pro subscription
# (``via=proxy``, no Keychain write); the Anthropic-API-key path and Gemini both
# capture a masked key into the Keychain and earn their persisted config ONLY on a
# green health-probe — a rejected key REVOKES the entry and persists nothing
# (fail-closed, mirroring _run_network_gear). Interactive by design, so ``--yes``
# SKIPS the whole chapter (a scripted run against a closed stdin would hang). Every
# external call — the claude-CLI presence/login probe, the proxy wiring, the
# provider health() — is a module-level seam the tests monkeypatch, so no live API
# call, no real ``claude`` shell-out, and no live network occurs in the suite.

#: The Keychain (service, account) the Anthropic-API-key path captures the key
#: under — the same pair ``cli.providers.claude.keychain`` defaults to, so the
#: provider re-reads it at use time (registry.make_provider).
_CLAUDE_KEYCHAIN = ("anthropic-api-key", "sanctum")
#: The Keychain (service, account) for the Gemini key (matches the gemini default).
_GEMINI_KEYCHAIN = ("google-ai-api-key", "sanctum")
#: The local claude-cli-proxy endpoint the subscription path points the provider at.
_CLAUDE_PROXY_ENDPOINT = "http://127.0.0.1:2001"


def _claude_logged_in() -> bool:
    """Cheap probe: is the local ``claude`` CLI logged in? — a module-level seam.

    Runs a fast, READ-ONLY ``claude`` invocation and treats a clean exit as
    "logged in". This is the one place a real ``claude`` shell-out would happen, so
    it is a seam the tests monkeypatch (NO real ``claude`` is run in the suite). A
    missing binary is handled by :func:`_claude_cli_ready` before this is reached;
    any subprocess failure here is read as "not logged in" (fail-closed) rather
    than raising out of onboarding.
    """
    import subprocess

    claude_bin = shutil.which("claude")
    if claude_bin is None:  # pragma: no cover - guarded by _claude_cli_ready upstream
        return False
    try:
        proc = subprocess.run(
            [claude_bin, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _claude_cli_ready() -> bool:
    """True iff the ``claude`` CLI is installed AND logged in.

    ``shutil.which`` (cheap, no shell-out) short-circuits the login probe when the
    binary is absent, so a stock Mac with no ``claude`` never shells out. Fail-
    closed: anything short of "present and logged in" returns False, which makes
    the subscription path persist NOTHING and show the install guidance instead of
    a false ``via=proxy``.
    """
    if shutil.which("claude") is None:
        return False
    return _claude_logged_in()


def _ensure_claude_proxy() -> None:
    """Best-effort: ensure the ``claude-cli-proxy`` LaunchAgent is wired + running.

    A module-level seam (tests monkeypatch it) so the subscription path can wire
    the local proxy the ``via=proxy`` provider talks to without the onboarding test
    touching launchctl. Best-effort by design — the health-probe that follows is
    the real gate on whether the proxy actually serves — so any failure here is
    surfaced as a note and never aborts the chapter.
    """
    from sanctum_cli.commands import agent
    from sanctum_cli.commands.proxy import KNOWN_PROXIES

    label, _url = KNOWN_PROXIES["claude-cli-proxy"]
    with contextlib.suppress(Exception):
        agent.agent_restart(label)


def _provider_health(kind: str, cfg: config.Config) -> HealthSnapshot:
    """Build provider ``kind`` from ``cfg`` and return its ``health()`` — a seam.

    Mirrors ``doctor._provider``: a provider that cannot even be constructed (a
    Keychain miss surfaces as ``LocalError`` at build time) OR whose ``health()``
    raises is reported as a non-ok snapshot rather than crashing onboarding. The
    tests monkeypatch THIS function so no provider is built and no live API/network
    call fires; production builds the real provider, which re-reads the just-stored
    key from the Keychain to authenticate the probe.
    """
    from sanctum_cli.providers import make_provider

    try:
        provider = make_provider(kind, cfg.cli.providers)
        return provider.health()
    except Exception as exc:  # build or health failure → fail-closed snapshot
        return HealthSnapshot(ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160])


def set_provider_config(
    *,
    claude: dict[str, Any] | None = None,
    gemini: dict[str, Any] | None = None,
    default_provider: str | None = None,
    path: Path | None = None,
) -> None:
    """Persist VERIFIED provider config to ``cli.providers.*`` in instance.yaml.

    Mirrors :func:`set_device_reference`'s atomic read-modify-write: every sibling
    block (top-level AND inside ``cli:``) is preserved, a ``<file>.bak`` is written
    first, and the parent dir is created for a fresh file. Only the arguments that
    are given are written — a ``None`` ``claude``/``gemini``/``default_provider``
    leaves any existing value untouched (so persisting only the provider that
    passed its health-probe never clobbers the other). The captured SECRET never
    lands here — it is in the Keychain (via :func:`store_device_secret`); only the
    routing config (``via``/``endpoint``/``model``) and the default selection are
    written. Callers must only invoke this AFTER a green health-probe.
    """
    target = Path(path) if path else config.instance_path()
    data: dict[str, Any] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    cli = data.get("cli")
    if not isinstance(cli, dict):
        cli = data["cli"] = {}
    if claude is not None or gemini is not None:
        providers = cli.get("providers")
        if not isinstance(providers, dict):
            providers = cli["providers"] = {}
        if claude is not None:
            providers["claude"] = claude
        if gemini is not None:
            providers["gemini"] = gemini
    if default_provider is not None:
        cli["default_provider"] = default_provider
    if target.exists():
        backup = target.parent / (target.name + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


_CLAUDE_INSTALL_GUIDANCE = (
    "[bold]Claude subscription not connected[/]\n\n"
    "Sanctum routes your prompts to your Claude Max/Pro subscription through a "
    "local proxy — no API charges. It needs the [cyan]claude[/] CLI installed and "
    "logged in.\n\n"
    "[bold]One next step:[/] install the Claude CLI, run [cyan]claude login[/], "
    "then re-run [cyan]sanctum onboard[/].\n\n"
    "[dim]Prefer an API key instead? Re-run and choose option 2 at the Claude "
    "prompt.[/]"
)


def _run_claude_subscription() -> dict[str, Any] | None:
    """Subscription (``via=proxy``) path — returns the claude config or None.

    Verifies the ``claude`` CLI is ready (installed + logged in); if not, shows the
    calm install-guidance panel and returns None (persist NOTHING — no false
    ``via=proxy``). If ready: wires the local proxy, health-probes it, and returns
    the ``{via: proxy, endpoint}`` config ONLY on a green probe. A failing probe
    surfaces the reason and returns None (nothing half-working persisted).
    """
    if not _claude_cli_ready():
        console.print(Panel.fit(_CLAUDE_INSTALL_GUIDANCE, border_style="yellow"))
        return None
    _ensure_claude_proxy()
    claude_cfg: dict[str, Any] = {"via": "proxy", "endpoint": _CLAUDE_PROXY_ENDPOINT}
    cfg = _config_with_provider_overrides(claude=claude_cfg)
    health = _provider_health("claude", cfg)
    if not health.ok:
        console.print(
            f"  [yellow]Claude proxy not healthy[/] — {health.detail or 'no response'}. "
            "Claude not configured; re-run after `claude login` or check "
            "`sanctum proxy status`."
        )
        return None
    console.print("  [green]✓[/] Claude connected (subscription, via proxy)")
    return claude_cfg


def _run_claude_api_key() -> dict[str, Any] | None:
    """API-key (``via=direct``) path — masked key → store → probe → revoke-or-config.

    Captures the masked Anthropic key into the Keychain FIRST (the provider re-reads
    it from the Keychain to authenticate the health-probe — same ordering contract
    as _run_network_gear), then probes. On a green probe returns the
    ``{via: direct, endpoint}`` config; on a REJECTED key REVOKES the Keychain entry
    and returns None (persist nothing — fail-closed). An empty key skips Claude.
    """
    key = Prompt.ask("  Anthropic API key", password=True).strip()
    if not key:
        console.print("  [dim]no key entered — Claude skipped (add later)[/]")
        return None
    service, account = _CLAUDE_KEYCHAIN
    store_device_secret(service=service, account=account, secret=key)
    claude_cfg: dict[str, Any] = {"via": "direct", "endpoint": "https://api.anthropic.com"}
    cfg = _config_with_provider_overrides(claude=claude_cfg)
    health = _provider_health("claude", cfg)
    if not health.ok:
        _revoke_device_secret(service=service, account=account)
        console.print(
            f"  [red]✗[/] Claude not configured — the API key was rejected "
            f"({health.detail or 'auth failed'}). Nothing written; re-run to retry."
        )
        return None
    console.print("  [green]✓[/] Claude connected (API key)")
    return claude_cfg


def _run_gemini() -> dict[str, Any] | None:
    """Gemini API-key path — masked key → store → probe → revoke-or-config.

    Same fail-closed shape as the Claude API-key path: store the masked key FIRST,
    probe, and persist the ``{model}`` config ONLY on a green probe; a rejected key
    REVOKES the Keychain entry and returns None. An empty key skips Gemini.
    """
    key = Prompt.ask(
        "  Google AI / Gemini API key (enter to skip)", password=True, default="", show_default=False
    ).strip()
    if not key:
        console.print(
            "  [dim]Gemini skipped — add later with "
            "`sanctum onboard` or store the key in your Keychain[/]"
        )
        return None
    service, account = _GEMINI_KEYCHAIN
    store_device_secret(service=service, account=account, secret=key)
    gemini_cfg: dict[str, Any] = {"model": "gemini-2.5-pro"}
    cfg = _config_with_provider_overrides(gemini=gemini_cfg)
    health = _provider_health("gemini", cfg)
    if not health.ok:
        _revoke_device_secret(service=service, account=account)
        console.print(
            f"  [red]✗[/] Gemini not configured — the API key was rejected "
            f"({health.detail or 'auth failed'}). Nothing written; re-run to retry."
        )
        return None
    console.print("  [green]✓[/] Gemini connected (API key)")
    return gemini_cfg


def _config_with_provider_overrides(
    *, claude: dict[str, Any] | None = None, gemini: dict[str, Any] | None = None
) -> config.Config:
    """Build an in-memory Config whose claude/gemini sub-config reflects the choice.

    The health-probe builds the provider from a Config; before persisting we don't
    yet have the new ``via``/``model`` on disk, so layer the chosen override onto a
    fresh-loaded Config so ``make_provider`` constructs the RIGHT flavour
    (``via=direct`` for the API-key probe, ``via=proxy`` for the subscription probe).
    Reads the existing keychain refs from the schema defaults so the provider points
    at the just-stored key. Never written — purely the probe's input.
    """
    cfg = config.ensure()
    providers = cfg.cli.providers
    new_claude = (
        providers.claude.model_copy(update=claude) if claude is not None else providers.claude
    )
    new_gemini = (
        providers.gemini.model_copy(update=gemini) if gemini is not None else providers.gemini
    )
    new_providers = providers.model_copy(update={"claude": new_claude, "gemini": new_gemini})
    new_cli = cfg.cli.model_copy(update={"providers": new_providers})
    return cfg.model_copy(update={"cli": new_cli})


def _run_ai_providers(*, yes: bool) -> bool:
    """AI-provider chapter — connect Claude (sub/API) + Gemini, fail-closed.

    Returns True iff a provider was actually VERIFIED and persisted (so the chapter
    honestly reports "connected" vs "skipped"); an interactive run where the user
    enters no key / a rejected key configures nothing and returns False — never a
    false "connected" (design spec §2/§11). Interactive by design, so ``--yes``
    SKIPS (a scripted run against a closed stdin would hang) and returns False.
    Claude is offered two ways, defaulting to the $0 Max/Pro subscription; both the
    Anthropic-API-key and the Gemini paths capture a masked key into the Keychain
    and earn their persisted config ONLY on a green health-probe — a rejected key
    REVOKES the entry and persists nothing. Each provider is independent: a
    failed/declined one never blocks the other. On success the verified
    ``cli.providers.{claude,gemini}`` blocks + ``cli.default_provider`` are written
    atomically (siblings preserved); ``mlx_local`` always remains as the offline
    floor, so the chapter is fully skippable. Non-blocking: the backup already
    succeeded, so a provider miss never fails the run.
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` "
            "without --yes to connect your AI providers"
        )
        return False

    console.print(
        "  [dim]Sanctum routes your prompts to the best model — let's connect "
        "yours. (Your local offline model always stays as a fallback.)[/]"
    )

    # ── Claude ──
    console.print(
        "\n  How do you want to connect Claude?\n"
        "    [bold]1[/] Claude Max/Pro subscription (free — recommended)\n"
        "    [bold]2[/] Anthropic API key"
    )
    choice = Prompt.ask("  choose", choices=["1", "2"], default="1")
    claude_cfg = _run_claude_subscription() if choice == "1" else _run_claude_api_key()

    # ── Gemini ──
    gemini_cfg = _run_gemini()

    # ── Persist what was verified (each independent) ──
    if claude_cfg is None and gemini_cfg is None:
        console.print(
            "  [dim]no cloud providers configured — your local offline model "
            "(mlx_local) remains the default[/]"
        )
        return False

    # Default to Claude when it configured, else Gemini — the verified provider the
    # user most likely wants first; mlx_local stays the floor regardless.
    default_provider = "claude" if claude_cfg is not None else "gemini"
    set_provider_config(
        claude=claude_cfg, gemini=gemini_cfg, default_provider=default_provider
    )
    summary = " · ".join(
        bit
        for bit in (
            "Claude ✓" if claude_cfg is not None else "",
            "Gemini ✓" if gemini_cfg is not None else "",
            "offline fallback ✓",
        )
        if bit
    )
    console.print(f"  [green]✓[/] AI configured — {summary}")
    return True


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


def _run_identity_setup(*, yes: bool) -> bool:
    """Collect operator name + Signal alert number → instance.yaml notifications.

    Returns True iff an identity is configured after this step — either it was
    ALREADY configured (a re-run; honestly "set up") or this run saved at least a
    name or an alert number. Returns False when ``--yes`` skips or the user entered
    nothing, so the recap reads "skipped" rather than a false "set up" (design spec
    §2/§11). Interactive (``--yes`` skips). Skips silently when already configured so
    re-runs don't re-prompt. Without it a fresh haus has no one to address in
    briefings and nowhere to send alerts (they'd otherwise have to fall back to
    a baked-in number — exactly the per-setup leak we're closing).
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` "
            "without --yes to set your name + alert number"
        )
        return False
    if _identity_configured():
        console.print("  [dim]operator identity already configured — skipping[/]")
        return True
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
    return bool(owner) or bool(number)


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


def _run_firewalla_pairing(*, yes: bool) -> bool:
    """Interactive Firewalla bridge pairing — fail-closed.

    Returns True ONLY on a genuine authenticated 200 that persisted the pairing;
    returns False when ``--yes`` skips, the operator declines, or every attempt is
    rejected — so the recap reads "skipped" rather than a false "paired" (design
    spec §2/§11). Collects the bridge URL/token + device IP/MAC, runs an
    AUTHENTICATED probe (:func:`screen_time.validate_firewalla_pairing`), and
    persists the pairing ONLY on a genuine authenticated 200. A wrong token, an
    unreachable bridge, or a malformed response is surfaced with the precise reason
    and the pairing is NOT written — because a curfew engine pointed at an unpaired
    bridge enforces nothing, and a false "paired" hides that until the first missed
    bedtime. ``--yes`` skips (interactive); re-run later via the same gate.
    """
    from sanctum_cli.commands import screen_time

    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` without "
            "--yes to pair the Firewalla bridge"
        )
        return False
    if not Confirm.ask("  pair the Firewalla screen-time bridge now?", default=True):
        console.print("  [dim]skipped — curfews stay inert until the bridge is paired[/]")
        return False

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
            return True
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
    return False


def _port_from_url(url: str) -> int:
    """Extract the port from a bridge URL; default 1984."""
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    return parsed.port or 1984


def _run_firewalla_compat() -> bool:
    """Firewalla screen-time gate — skip-if-absent, strict-if-present.

    Always returns False: this is a read-only compatibility ASSESSMENT, not a
    configuration step — it persists nothing, so it never contributes a "configured"
    signal to the chapter recap (the recap reflects what was PAIRED, which the
    pairing gate owns). The /info probe distinguishes "module not paired" (bridge
    unreachable or no token → SKIP, onboarding continues) from "paired but
    incompatible" — ``compat_command`` raises :class:`LocalError` for both cases, so
    we probe first instead of parsing exception messages. When the bridge answers,
    the assessment runs STRICT: a spoof-mode box or a near-capacity policy table is
    surfaced to the brand-new operator *now*, fix text included, instead of via the
    first silently-unenforced curfew. The verdict is loud but non-blocking (same
    stance as the restore canary): the backup already succeeded, and `sanctum
    screen-time compat --strict` is the hard gate.
    """
    from sanctum_cli.commands import screen_time

    if screen_time._fetch_bridge_json("/info") is None:
        console.print(
            "  [yellow]skipped[/] — screen-time module not paired yet — "
            "run `sanctum screen-time compat` after pairing"
        )
        return False
    try:
        # Prints the per-check table (status + fix columns) before raising.
        screen_time.compat_command(strict=True)
    except LocalError as exc:
        console.print(f"  [red]✗[/] {exc.message}")
        if exc.fix:
            console.print(f"  [dim]fix: {exc.fix}[/]")
    return False


# ── Network-gear detection + pairing gate ────────────────────────────
# The family path runs a backup, then walks the network gear it can find and
# offers GUIDED pairing — exactly the apple-like "your haus, your hardware"
# moment. This mirrors the Firewalla-pairing gate (prompt → READ-ONLY
# auth-probe → persist on a genuine success), generalized over every registered
# DeviceProvider kind via the registry's detect() fingerprints. It is ADDITIVE
# (a new RECIPE_GATES entry) and SKIPPABLE (--yes), and absent/unrecognized gear
# is silently skipped — a fresh haus with no supported gear finishes onboarding
# unbothered. NO live-device call is fired in default tests: detection, connect,
# and the keychain write are module-level seams the tests monkeypatch.

# Kinds we offer guided pairing for, with the human label shown in the prompt.
# Firewalla is deliberately ABSENT: it has its OWN dedicated pairing gate
# (firewalla-pairing, bearer-token + on-disk secret, NOT a Keychain password),
# so re-pairing it here would double-prompt for the same box. This gate covers
# the Keychain-password kinds (hub / orbi) — exactly the kinds in
# net._DEVICE_KEYCHAIN_DEFAULTS.
_NETWORK_GEAR_KINDS: tuple[tuple[str, str], ...] = (
    ("hub", "network hub / gateway"),
    ("orbi", "mesh wifi (Orbi)"),
)


def _net_context() -> NetContext:
    """Build the NetContext the registry fingerprints gear over (read-only).

    Parses the default gateway from the real ``route`` probe — the same seam the
    ``sanctum net`` sub-apps use — and threads the real runner so a provider's
    ``detect()`` can probe without owning its own subprocess plumbing. Tests
    monkeypatch :func:`detect_network_gear` so no shell-out / socket occurs.
    """
    from sanctum_cli.devices.base import NetContext as _NetContext
    from sanctum_cli.net import detect, system

    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    return _NetContext(gateway_ip=gw, runner=system.real_runner)


def _detect_kind(kind: str, net: NetContext) -> DeviceProvider | None:
    """Resolve a provider for ``kind`` via the registry, IFF a real driver claims it.

    Uses the registry's auto-detection (``detect()`` scoring) rather than a brand
    pin: a kind whose registered providers all score 0 falls back to the
    read-only ``GenericReadOnlyProvider``, which we treat as "not present" (it
    drives no real gear). Returns the resolved provider only when a brand-specific
    driver claimed the network, else ``None``. Read-only — no ``connect``, no
    mutation — safe during onboarding's scan. The seam tests monkeypatch.
    """
    from sanctum_cli.devices import registry

    provider = registry.resolve(kind, net)
    # The generic fallback advertises a ``generic-<kind>`` brand and drives no
    # real gear — treat it as "nothing detected" so absent gear is skipped.
    if provider.brand == f"generic-{kind}":
        return None
    return provider


def detect_network_gear(net: NetContext) -> list[tuple[str, DeviceProvider]]:
    """Registry scan: the (kind, provider) pairs whose driver claims this network.

    Walks the pairing-eligible kinds, asking the registry to resolve each over
    ``net``; a kind whose read-only ``detect()`` fingerprint matched a real driver
    is included. Absent / unrecognized gear yields an empty list (haus-aware: the
    gate then silently skips). Pure of side effects — detection is read-only and
    no provider is connected here.
    """
    detected: list[tuple[str, DeviceProvider]] = []
    for kind, _label in _NETWORK_GEAR_KINDS:
        provider = _detect_kind(kind, net)
        if provider is not None:
            detected.append((kind, provider))
    return detected


def set_device_reference(
    *,
    kind: str,
    brand: str,
    host: str,
    keychain_service: str,
    keychain_account: str,
    path: Path | None = None,
) -> None:
    """Persist a VALIDATED ``devices.<kind>`` reference block to instance.yaml.

    Mirrors :func:`set_firewalla_bridge`'s atomic read-modify-write: every sibling
    block is preserved, a ``<file>.bak`` is written first, and the secret itself
    NEVER lands here (it goes to the Keychain via :func:`store_device_secret`).
    The block records the brand (so ``devices.<kind>.brand`` pins the provider on
    later runs — bypassing a stubbed ``detect``), the host, and the Keychain
    ``(service, account)`` the provider re-reads its password under. Callers must
    only invoke this AFTER a genuine read-only ``connect()`` auth-probe succeeds.
    """
    target = Path(path) if path else config.instance_path()
    data: dict[str, Any] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    devices = data.get("devices")
    if not isinstance(devices, dict):
        devices = data["devices"] = {}
    devices[kind] = {
        "brand": brand,
        "host": host,
        "keychain": {"service": keychain_service, "account": keychain_account},
    }
    if target.exists():
        backup = target.parent / (target.name + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _keychain_write(account: str, service: str, value: str) -> None:
    """Write a generic-password entry to the login Keychain via ``security``.

    The guaranteed secrets tier (CLAUDE.md secrets-trifecta): the Keychain is
    always present on macOS, so this is the one write that MUST succeed for the
    pairing to be usable. Mirrors ``keychain_cmd``'s ``add-generic-password -U``
    (update-or-create). Raises ``LocalError`` on a genuine failure so the caller
    can surface it — a paired device whose password did not land is worse than a
    loud failure. The seam tests monkeypatch so no real Keychain entry is created.
    """
    import subprocess

    from sanctum_cli.keychain import SECURITY_BIN

    if not shutil.which(SECURITY_BIN):
        msg = f"missing required binary: {SECURITY_BIN}"
        raise LocalError(msg, fix="install Xcode Command Line Tools: xcode-select --install")
    proc = subprocess.run(
        [SECURITY_BIN, "add-generic-password", "-a", account, "-s", service, "-w", value, "-U"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = f"Keychain write failed: {proc.stderr.strip() or 'unknown error'}"
        raise LocalError(msg)


# ── Trifecta cred-capture seam (1P / SOPS / Keychain) ────────────────
# CLAUDE.md secrets-trifecta: 1Password (human SoT) → VM SOPS (agents) →
# Mini Keychain (Mini tools), drift-synced daily by tools/secret-rotator/sync.py.
# When the network-gear pairing gate captures a device admin secret it ALWAYS
# writes the Keychain (the GUARANTEED tier — see store_device_secret). On a FULL
# haus (op service-account token + SOPS present) it ALSO best-effort mirrors the
# secret into the trifecta: write/update the 1P item AND emit a providers.yaml
# `sync_mirrors` mapping so the DAILY DRIFT-SYNC owns real cross-tier propagation
# thereafter. The heavyweight SOPS/VM push is deferred to that sync by design —
# onboarding only hands it the mapping. On a stock friend-install (no op/SOPS) the
# whole mirror is a clean no-op; it must NEVER block onboarding. Every external
# call (haus-detect, op write) is a module-level seam the tests monkeypatch, so no
# real `op`/`sops` binary or 1Password account is touched in the gate.

#: Default providers.yaml the daily drift-sync reads. The canonical daemon copy
#: lives outside the OneDrive-synced repo (CLAUDE.md secrets §); env override lets
#: a haus point this at its real file. Onboarding only APPENDS the mapping — never
#: the secret value — so even a world-readable providers.yaml leaks nothing.
ENV_PROVIDERS_FILE = "SANCTUM_PROVIDERS_FILE"
_DEFAULT_PROVIDERS_FILE = Path("~/.sanctum/secret-rotator/providers.yaml").expanduser()

#: The op service-account token env the headless drift-sync authenticates with
#: (CLAUDE.md secrets §: service=op-service-account-token). Its presence is the
#: cheapest "this is a haus with 1P wired" signal that never shells out.
_OP_SERVICE_ACCOUNT_ENV = "OP_SERVICE_ACCOUNT_TOKEN"

#: The 1P vault device secrets land in (mirrors the trifecta item naming).
_OP_VAULT = "Sanctum"


@dataclass(frozen=True)
class _TrifectaNames:
    """The (logical_key, op_title, sops_key) a keychain service maps to.

    A pure, deterministic derivation so re-pairing the same device updates the
    same trifecta entry (idempotent) and two different devices never collide.
    """

    logical_key: str
    op_title: str
    sops_key: str


def _trifecta_names_for(kc_service: str) -> _TrifectaNames:
    """Derive stable trifecta names from a keychain service — a pure function.

    ``bell-hub-admin`` → logical/sops ``bell_hub_admin`` (underscored, the
    providers.yaml/SOPS key convention) and 1P title ``Sanctum - Bell Hub Admin``
    (the trifecta item-title convention, ``Sanctum - <Title Case>``). Same service
    → same names (idempotent re-pairing); different service → different names (no
    collision), because the keychain service is itself unique per kind.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", kc_service.lower()).strip("_") or "device"
    title = " ".join(part.capitalize() for part in slug.split("_"))
    return _TrifectaNames(
        logical_key=slug,
        op_title=f"Sanctum - {title}",
        sops_key=slug,
    )


def _haus_trifecta_present() -> bool:
    """True iff this box has the trifecta tooling (op service-account token + SOPS).

    The cheapest, non-blocking haus-fingerprint for the secrets trifecta: a
    1Password service-account token in the environment (how the headless daily
    drift-sync authenticates — CLAUDE.md secrets §) AND a ``sops`` binary on PATH
    (the VM-tier encryptor). A stock friend-install has neither, so the mirror
    no-ops there. Reads the env + a PATH lookup only — never runs ``op``/``sops``,
    so it can never hang and never prompts for 1P unlock.
    """
    token = os.environ.get(_OP_SERVICE_ACCOUNT_ENV, "").strip()
    return bool(token) and shutil.which("sops") is not None


def _op_write_item(*, title: str, value: str) -> None:
    """BEST-EFFORT write/update of a 1Password item's credential (real seam).

    Shells out to the headless ``op`` CLI (service-account mode — the token is
    already in the env, asserted by :func:`_haus_trifecta_present`). Edits the
    item's ``credential`` field if it exists, else creates it in the Sanctum vault.
    Module-level so the gate's tests monkeypatch it — NO real ``op`` is run in the
    default suite. Any failure raises (the caller swallows it); the daily
    drift-sync re-pushes from 1P later regardless, so a transient op miss is not
    fatal to custody.
    """
    import subprocess

    op_bin = shutil.which("op")
    if op_bin is None:  # pragma: no cover - guarded by _haus_trifecta_present upstream
        msg = "op CLI not found on PATH"
        raise LocalError(msg)
    ref = f"op://{_OP_VAULT}/{title}/credential"
    # `op item edit` updates an existing item; if it is absent, create it. We probe
    # existence with `op read` (cheap, service-account mode is sub-second) so we
    # don't depend on edit-vs-create error strings.
    exists = (
        subprocess.run(
            [op_bin, "read", ref],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        ).returncode
        == 0
    )
    if exists:
        cmd = [op_bin, "item", "edit", title, f"credential={value}", "--vault", _OP_VAULT]
    else:
        cmd = [
            op_bin,
            "item",
            "create",
            f"--title={title}",
            "--category=password",
            f"--vault={_OP_VAULT}",
            f"credential={value}",
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        msg = f"1Password write failed for {title!r}: {proc.stderr.strip() or 'unknown error'}"
        raise LocalError(msg)


def _providers_file() -> Path:
    """Where the daily drift-sync reads its providers.yaml — env override else default."""
    override = os.environ.get(ENV_PROVIDERS_FILE)
    return Path(override).expanduser() if override else _DEFAULT_PROVIDERS_FILE


def _append_sync_mirror(
    *,
    logical_key: str,
    op_title: str,
    sops_key: str,
    kc_service: str,
    path: Path | None = None,
) -> None:
    """Emit/update a ``sync_mirrors`` mapping in providers.yaml — pure read-modify-write.

    Mirrors :func:`set_device_reference`'s atomic YAML write: every sibling section
    is preserved, a ``<file>.bak`` is written first when the file exists, and the
    parent dir is created for a fresh file. The mapping is the exact shape the
    daily drift-sync (`tools/secret-rotator/sync.py`) consumes —
    ``<logical_key>: {op, sops, kc}`` — so once it lands the key is under managed
    cross-tier custody. Re-pairing the same kind UPDATES the existing entry in
    place (keyed by ``logical_key``), never appending a duplicate. NO secret value
    is ever written here — only the names that point the drift-sync at each tier.
    """
    target = path or _providers_file()
    data: dict[str, Any] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    mirrors = data.get("sync_mirrors")
    if not isinstance(mirrors, dict):
        mirrors = data["sync_mirrors"] = {}
    mirrors[logical_key] = {"op": op_title, "sops": sops_key, "kc": kc_service}
    if target.exists():
        backup = target.parent / (target.name + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _mirror_to_trifecta(*, service: str, account: str, secret: str) -> None:  # noqa: ARG001
    """BEST-EFFORT mirror of a captured device secret into the 1P/SOPS trifecta.

    The trifecta (1Password human-SoT → VM SOPS → Mini Keychain) is the haus's
    cross-tier secret architecture; the Keychain write in
    :func:`store_device_secret` is the GUARANTEED tier, so this whole mirror is
    purely additive. On a stock friend-install (no op service-account token / no
    SOPS — :func:`_haus_trifecta_present` is False) it is a clean NO-OP: keychain
    only, no error, no block. On a full haus it:

    1. writes/updates the 1P item with the captured secret (:func:`_op_write_item`),
    2. emits/updates a ``sync_mirrors`` mapping (:func:`_append_sync_mirror`) so the
       DAILY DRIFT-SYNC owns the heavyweight SOPS/VM propagation thereafter.

    Each sub-step is independently fail-soft: a failing ``op`` write is swallowed
    so the mapping (which hands the key to the drift-sync, which re-pushes from 1P)
    still lands. The whole function never raises out — ``store_device_secret``
    also suppresses, but the internal fail-soft means a transient op error doesn't
    drop the mapping. ``account`` is unused (the trifecta keys off the keychain
    SERVICE, which is unique per device kind).
    """
    if not _haus_trifecta_present():
        return
    names = _trifecta_names_for(service)
    # 1P write is best-effort: the daily drift-sync re-pushes from 1P, so a
    # transient op miss must not abort the mapping emit below.
    with contextlib.suppress(Exception):
        _op_write_item(title=names.op_title, value=secret)
    # The mapping is what hands real cross-tier propagation to the drift-sync; emit
    # it best-effort too (absent providers.yaml dir, read-only fs) so a hiccup here
    # never blocks onboarding.
    with contextlib.suppress(Exception):
        _append_sync_mirror(
            logical_key=names.logical_key,
            op_title=names.op_title,
            sops_key=names.sops_key,
            kc_service=service,
        )


def store_device_secret(*, service: str, account: str, secret: str) -> None:
    """Store a device admin secret: Keychain (guaranteed) + best-effort trifecta.

    Two tiers, in order of guarantee:

    * **Keychain** (:func:`_keychain_write`) — the guaranteed tier on macOS. A
      failure here propagates: a paired device whose password did not land is a
      silent footgun (every later ``connect`` would miss), so it fails loudly.
    * **Trifecta mirror** (:func:`_mirror_to_trifecta`) — BEST-EFFORT. The 1P/SOPS
      tooling is absent on a stock friend-install, so any error here (a missing
      ``op`` binary, an unreachable VM) is SWALLOWED — onboarding must never block
      on the haus tooling being absent. The Keychain copy is enough to drive the
      device.
    """
    _keychain_write(account=account, service=service, value=secret)
    # The mirror is best-effort: the Keychain (guaranteed tier) already holds the
    # secret, so any error here (absent `op`/SOPS, unreachable VM) is SWALLOWED —
    # onboarding must never block on absent 1Password/SOPS haus tooling.
    with contextlib.suppress(Exception):
        _mirror_to_trifecta(service=service, account=account, secret=secret)


def _run_network_gear(*, yes: bool) -> bool:
    """Network-gear detection + guided pairing gate — additive, fail-closed.

    Returns True iff at least one device was actually PAIRED (a green auth-probe +
    a persisted ``devices.<kind>`` block); returns False when ``--yes`` skips, no
    gear is detected, the operator declines every device, or every probe is
    rejected — so the recap reads "skipped" rather than a false "paired" (design
    spec §2/§11). Runs the registry's read-only detection across the registered
    providers over the current network; for EACH detected kind it offers guided
    pairing that mirrors :func:`_run_firewalla_pairing`: prompt the admin password →
    run a READ-ONLY ``provider.connect()`` auth-probe → on a genuine success write
    the password to the Keychain (the resolved ``(service, account)``) AND persist a
    ``devices.<kind>`` reference block to instance.yaml. A rejected probe (wrong
    password / unreachable box) is surfaced loudly and NOTHING is written —
    because a ``devices`` block pointing at a box you cannot auth to is a false
    "paired" that bites on the first real op. Interactive by design, so ``--yes``
    SKIPS it (prompting a scripted run against a closed stdin would hang), and
    absent gear is silently skipped (haus-aware). The step is non-blocking: the
    backup already succeeded, so a pairing miss never fails the run.
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` "
            "without --yes to pair your network gear"
        )
        return False

    from sanctum_cli.commands import net as net_cmd

    net = _net_context()
    detected = detect_network_gear(net)
    if not detected:
        console.print(
            "  [dim]no network gear detected — nothing to pair "
            "(re-run `sanctum onboard` after connecting your hub/mesh)[/]"
        )
        return False

    paired_any = False
    label_map = dict(_NETWORK_GEAR_KINDS)
    for kind, provider in detected:
        label = label_map.get(kind, kind)
        console.print(f"  [bold]{label}[/] detected ({provider.brand})")
        if not Confirm.ask(f"  pair the {label} now?", default=True):
            console.print(f"  [dim]skipped {kind} — leave it unpaired for now[/]")
            continue

        service, account = net_cmd.device_keychain_ref(kind)
        if not service or not account:
            console.print(
                f"  [yellow]skipped {kind}[/] — no Keychain (service, account) resolved; "
                f"set devices.{kind}.keychain.* in instance.yaml first"
            )
            continue

        password = Prompt.ask(f"  {label} admin password", password=True).strip()

        # Order matters for a faithful auth-probe. The REAL providers'
        # ``connect()`` re-read the admin password FROM THE KEYCHAIN under the
        # resolved (service, account) — they deliberately ignore ``creds.secret``
        # (base.Creds docstring; sagemcom/orbi.connect). So the probe can only
        # authenticate with the just-typed password if that password is ALREADY in
        # the Keychain. We therefore write it FIRST, then probe. This is NOT a
        # device mutation (the guardrail's "no mutation" is about device state):
        # connect() opens an authenticated session and changes nothing on the box.
        # If the probe then FAILS (wrong password / unreachable box), we REVOKE the
        # Keychain entry we just wrote and persist NO devices block — so a failed
        # pairing leaves nothing usable behind (a false "paired" is worse than an
        # honest "not paired", mirroring _run_firewalla_pairing's fail-closed
        # stance). The keychain write is guarded; the trifecta mirror is
        # best-effort and never blocks.
        store_device_secret(service=service, account=account, secret=password)
        if not _probe_device(provider, net=net, account=account, service=service, secret=password):
            # Roll back the keychain write so a rejected probe persists nothing.
            _revoke_device_secret(service=service, account=account)
            console.print(
                f"  [red]✗[/] {kind} not paired — the admin password was rejected or the "
                f"box was unreachable. Nothing written; re-run `sanctum onboard` to retry."
            )
            continue

        # Genuine success → persist the devices.<kind> reference block. The secret
        # is already in the Keychain (written above); the block points the provider
        # at that entry on later runs and pins the brand (bypassing a stubbed detect).
        #
        # Pin the CLASS-level brand (``type(provider).brand``), NOT ``provider.brand``.
        # A genuine ``connect()`` in the probe above mutates the INSTANCE brand:
        # SagemcomHubProvider/OrbiProvider._refine_brand rewrite ``self.brand`` to the
        # concrete model (``sagemcom-fast5689``/``orbi-rbr850``). But the registry's
        # ``brand_pin`` path matches against ``cls.brand`` (the constant
        # ``"sagemcom"``/``"orbi"``), so persisting the refined string would make a
        # LATER ``sanctum net hub/orbi`` call's ``registry.resolve(..., brand_pin=...)``
        # raise "no registered provider for pinned brand 'sagemcom-fast5689'". The
        # class attribute is the only value the pin can resolve.
        brand_pin = type(provider).brand
        set_device_reference(
            kind=kind,
            brand=brand_pin,
            host=net.gateway_ip or "",
            keychain_service=service,
            keychain_account=account,
        )
        console.print(f"  [green]✓[/] {label} paired — {brand_pin} ({kind})")
        paired_any = True
    return paired_any


def _probe_device(
    provider: DeviceProvider,
    *,
    net: NetContext,
    account: str,
    service: str,
    secret: str,
) -> bool:
    """READ-ONLY auth-probe: open an authenticated session, mutate NOTHING.

    Builds Creds under the resolved ``(service, account)`` and calls
    ``provider.connect``. The REAL providers re-read the password from the
    Keychain under that ``(service, account)`` (they ignore ``creds.secret`` by
    design — base.Creds docstring), so the caller MUST have written the password
    to the Keychain before this is called; ``secret`` is still carried on Creds so
    a return-convention / test provider that DOES honor it works too. ``connect``
    opens a session and changes nothing on the box.

    The probe must POSITIVELY verify auth, not merely that connect did not raise.
    "connect did not raise" is a faithful auth oracle ONLY for a fail-closed
    provider (the Sagemcom hub re-raises :class:`DeviceError` on a rejected login).
    It is NOT faithful for a BEST-EFFORT connect: ``OrbiProvider.connect`` tolerates
    a wrong password / unreachable box and returns cleanly (so the build never
    blocks on a live call), so a non-raising Orbi connect would FALSELY read as
    "paired" — persisting a kept Keychain secret + a ``devices.orbi`` block pointing
    at a box you cannot auth to, the exact false "paired" that bites on the first
    real ``sanctum net orbi`` op. So when the provider exposes the optional
    :class:`~sanctum_cli.devices.base.AuthProbeProvider` capability (``auth_ok``),
    we REQUIRE it to confirm the session authenticated; a provider without it falls
    back to the connect-raises convention (its own auth oracle). ``auth_ok`` reads
    the recorded login outcome — no new session, no mutation — so it is safe here.

    Returns True only on a clean connect AND a positive ``auth_ok`` (when offered),
    False on a :class:`~sanctum_cli.errors.LocalError` — the base class of both
    :class:`~sanctum_cli.devices.base.DeviceError` (wrong password / unreachable
    box) AND the Keychain errors a provider's ``keychain.read`` can raise — or on a
    rejected ``auth_ok``, so a rejected probe is reported as a failed pairing rather
    than crashing onboarding. ``disconnect`` is always called so a connected
    provider's transport is released.
    """
    from sanctum_cli.devices.base import AuthProbeProvider, Creds
    from sanctum_cli.errors import LocalError

    creds = Creds(
        host=net.gateway_ip or "",
        username=account,
        secret=secret,
        key_path=None,
        keychain_service=service,
    )
    try:
        provider.connect(creds)
        # Positive auth verification for best-effort-connect brands. A provider
        # whose connect() swallows a rejected login (Orbi) MUST expose auth_ok();
        # require it to confirm the session genuinely authenticated. A fail-closed
        # provider (Sagemcom) omits it — its connect already raised on failure, so
        # reaching here is itself proof of auth.
        if isinstance(provider, AuthProbeProvider) and not provider.auth_ok():
            return False
    except LocalError:
        return False
    finally:
        # Best-effort teardown; a provider that never connected no-ops here. A
        # teardown failure must never mask the probe result.
        with contextlib.suppress(Exception):
            provider.disconnect()
    return True


def _revoke_device_secret(*, service: str, account: str) -> None:
    """Best-effort rollback of a Keychain entry written for a failed pairing.

    Mirrors ``commands.uninstall.revoke_keychain_entry``: deletes the
    generic-password entry so a rejected auth-probe persists NOTHING usable. A
    missing entry or a ``security`` failure is swallowed — this is cleanup on an
    already-failed path and must never itself raise out of onboarding. The seam is
    module-level so tests can stub it without shelling out to ``security``.
    """
    import subprocess

    from sanctum_cli.keychain import SECURITY_BIN

    if not shutil.which(SECURITY_BIN):
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [SECURITY_BIN, "delete-generic-password", "-a", account, "-s", service],
            capture_output=True,
            text=True,
            check=False,
        )


def _run_canary() -> CanaryOutcome:
    """Lightweight canary — restore ~/.zshrc, sha256-diff against live.

    Returns the honest :class:`CanaryOutcome` for EVERY path so the orchestrator can
    thread it into the recap + green-check instead of declaring a blanket "verified":

    * SKIPPED — no configured repo, no ``~/.zshrc``, or the restored file is absent.
    * FAILED — restic restore errored, or the restored bytes don't match live (a real
      backup-integrity problem the operator must see).
    * VERIFIED — the round-trip succeeded.

    NON-BLOCKING: this never raises; onboarding continues regardless of the outcome.
    """
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
        return CanaryOutcome.SKIPPED
    probe = Path("~/.zshrc").expanduser()
    if not probe.exists():
        console.print("  [yellow]skipped[/] — no ~/.zshrc on this host; nothing to round-trip")
        return CanaryOutcome.SKIPPED
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
            return CanaryOutcome.FAILED
        restored = Path(tmp) / probe.relative_to(probe.anchor)
        if not restored.exists():
            console.print(f"  [yellow]skipped[/] — restored file not found at {restored}")
            return CanaryOutcome.SKIPPED
        restored_sha = hashlib.sha256(restored.read_bytes()).hexdigest()
        if restored_sha == live_sha:
            console.print("  [green]✓[/] canary survived round-trip")
            return CanaryOutcome.VERIFIED
        console.print(
            f"  [red]✗[/] canary diff: live={live_sha[:16]} != restored={restored_sha[:16]}"
        )
        return CanaryOutcome.FAILED
