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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
import yaml
from rich.align import Align
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from sanctum_cli import config, recipes
from sanctum_cli.backends import b2, gdrive, r2
from sanctum_cli.commands import backup as backup_cmd
from sanctum_cli.commands import net as net_cmd
from sanctum_cli.devices.base import NetContext as _NetContext
from sanctum_cli.errors import LocalError, SanctumError, UserError
from sanctum_cli.gear.scan import build_default_scan, discover_haus
from sanctum_cli.net import heal, link, system
from sanctum_cli.onboard_experience import chapter_banner, green_check, recap_card
from sanctum_cli.providers.base import HealthSnapshot

if TYPE_CHECKING:
    from sanctum_cli.devices.base import DeviceProvider, NetContext
    from sanctum_cli.gear.types import HausInventory

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
        "haus-scan",
        "network-gear",
        "wifi-identity",
        "ha-green",
        "network-resilience",
        "mesh-join",
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
        "wifi-identity",
        "network-resilience",
        "mesh-join",
        "service-user-install",
    ),
    "code": (
        "identity-setup",
        "ai-providers",
        "wifi-identity",
        "network-resilience",
        "mesh-join",
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
    "Your Network": (
        "firewalla-pairing",
        "firewalla-compat",
        "haus-scan",
        "network-gear",
        "wifi-identity",
        "ha-green",
        "network-resilience",
        "mesh-join",
        "service-user-install",
    ),
}

#: Human label shown per gate inside the per-step header (calm, lowercase-free).
_GATE_LABELS: dict[str, str] = {
    "identity-setup": "Operator identity",
    "family-setup": "Family setup",
    "ai-providers": "AI providers",
    "firewalla-pairing": "Firewalla pairing",
    "firewalla-compat": "Firewalla compatibility",
    "haus-scan": "Haus hardware scan",
    "network-gear": "Network gear",
    "wifi-identity": "Wi-Fi identity (stable MAC)",
    "ha-green": "HA Green (Home Assistant)",
    "network-resilience": "Network resilience (self-heal)",
    "mesh-join": "Sanctum mesh (join the swarm)",
    "service-user-install": "Hive service principal (sanctum user)",
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
    if gate == "haus-scan":
        return _run_haus_scan(yes=yes)
    if gate == "network-gear":
        return _run_network_gear(yes=yes)
    if gate == "wifi-identity":
        return _run_wifi_identity(yes=yes)
    if gate == "ha-green":
        return _run_ha_green(yes=yes)
    if gate == "network-resilience":
        return _run_network_resilience(yes=yes)
    if gate == "mesh-join":
        return _run_mesh_join(yes=yes)
    if gate == "service-user-install":
        return _run_service_user_install(yes=yes)
    return False


def _run_service_user_install(*, yes: bool) -> bool:
    """Ensure wave-1 control plane runs as the dedicated sanctum service user.

    Greenfield-safe: packaged plists live in the CLI wheel. Haus-operator only.
    Under --yes, materialize assets and print the one-liner for a later sudo
    install (no password prompt in scripted runs).
    """
    from sanctum_cli import service_user as su

    # This gate is operator-recipe only — always greenfield-capable (packaged assets).
    report = su.check_wave1()
    if report.applicable and report.ok:
        console.print("  [green]ok[/] wave-1 already running as sanctum")
        return True
    if yes:
        su.materialize_assets()
        console.print(
            "  [yellow]skip[/] service principal needs an admin password — run once:\n"
            "         sanctum service-user install"
        )
        return False
    console.print(
        "  Wave-1 control plane (proxyd / force-flow / memory-vault) should run as\n"
        "  the dedicated [bold]sanctum[/] user, not your login session.\n"
        "  Self-contained install (packaged plists; no extra repo sync)."
    )
    if not typer.confirm("  Install hive service principal now?", default=True):
        console.print("  [dim]skipped by operator[/]")
        return False
    rc = su.run_install(dry_run=False)
    if rc != 0:
        console.print(f"  [red]install exited {rc}[/]")
        return False
    report2 = su.check_wave1()
    if report2.applicable and report2.ok:
        console.print("  [green]ok[/] service principal installed")
        return True
    console.print(
        "  [yellow]install finished; some health checks still fail "
        "(ok if binaries not on disk yet)[/]"
    )
    return True


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
        try:
            _dispatch_cloud_setup(backend, no_open=no_open)
        except UserError as exc:
            # Backups need `restic`; a fresh Mac often lacks it. Skip the whole chapter
            # gracefully instead of hard-crashing mid-onboard — the beta user reaches the
            # finish line, then adds backups later via `sanctum cloud setup`. (Only the
            # missing-restic case is swallowed; every other setup error still surfaces.)
            if "restic" in str(exc).lower():
                console.print(
                    "\n  [yellow]restic not installed — skipping the backup chapter.[/]"
                    "\n  [dim]Install it later (brew install restic), then run "
                    "`sanctum cloud setup` to enable backups.[/]"
                )
                return CanaryOutcome.SKIPPED
            raise
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
    if canary is CanaryOutcome.FAILED:
        # Honest finish: never claim "verified" over a recap that shows a failed
        # backup canary. The Sanctum is alive (mlx_local floor), but say what's true.
        console.print(
            "[yellow]Your Sanctum is alive — your backup canary needs attention (see above).[/]"
        )
    else:
        console.print(green_check("Setup verified — your Sanctum is alive"))

    console.print()
    try:
        who = os.getlogin()
    except OSError:
        who = os.environ.get("USER", "friend")

    # The "verified the restore" boast is true ONLY on a clean round-trip — never
    # claim it over a failed/skipped canary (the panel must agree with the recap).
    _tail = (
        "From here, Sanctum keeps running in the background — daily backups, "
        "drift heals, audit trails — without asking you to do anything. The "
        "next time you'll hear from it is when something interesting happens.\n"
    )
    if canary is CanaryOutcome.VERIFIED:
        _intro = (
            "It just ran its first backup and verified the restore by round-"
            "tripping a known file through your cloud bucket. " + _tail
        )
    else:
        _intro = _tail
    body = Group(
        Text.from_markup(f"[bold green]Your Sanctum is alive, {who}.[/]\n"),
        Text.from_markup(_intro),
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

    # The First Hello — the closing beat. After "Your Sanctum is alive", the haus
    # speaks for the first time. Interactive runs only (scripted --yes stays silent).
    if not yes:
        _run_first_hello(_preferred_name(who))


def _preferred_name(fallback: str) -> str:
    """The name the user asked to be called by, for SANCTUM_USER_NAME.

    The identity gate writes it to ``notifications.owner_name`` in instance.yaml
    ("Bert", not "Bertrand"); prefer that over the macOS login name. Falls back to
    the supplied login name when the gate was skipped (``--yes``, a non-family
    recipe, or already-configured).
    """
    try:
        data = yaml.safe_load(config.instance_path().read_text(encoding="utf-8")) or {}
        notif = data.get("notifications") if isinstance(data, dict) else None
        owner = notif.get("owner_name") if isinstance(notif, dict) else None
        if isinstance(owner, str) and owner.strip():
            return owner.strip()
    except (OSError, yaml.YAMLError):
        pass
    return fallback


def _run_first_hello(name: str) -> None:
    """The First Hello — the haus's first words, the closing beat of onboarding.

    Delegates to ~/.sanctum/bin/sanctum-first-hello.py so the Signal/voice logic
    lives in one place. Yoda greets the new operator on Signal (guaranteed) and
    out loud (best-effort), proving he already noticed their network.

    FAIL-SOFT by contract: a missing script, an unreachable Force Flow, or a
    finicky TTS must NEVER turn a completed onboarding into a failure.
    """
    import contextlib
    import subprocess

    script = Path.home() / ".sanctum" / "bin" / "sanctum-first-hello.py"
    if not script.is_file():
        return  # not installed on this haus — silently skip
    console.print("\n[dim]One moment -- the haus wants to say hello...[/]")
    env = dict(os.environ)
    if name:
        env["SANCTUM_USER_NAME"] = name  # Yoda greets THIS name
    # The haus's first words must never break its first run.
    with contextlib.suppress(Exception):
        subprocess.run([str(script), "--voice"], env=env, timeout=180, check=False)


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
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


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
    path.write_text(yaml.safe_dump(skeleton, sort_keys=False, allow_unicode=True), encoding="utf-8")
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
#: The local claude-max-proxy endpoint the subscription path points the provider at
#: (:3456; the old anthropic-proxy on :2001 was retired — see instance.yaml services).
_CLAUDE_PROXY_ENDPOINT = "http://127.0.0.1:3456"


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
        return HealthSnapshot(
            ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
        )


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
        "  Google AI / Gemini API key (enter to skip)",
        password=True,
        default="",
        show_default=False,
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
    set_provider_config(claude=claude_cfg, gemini=gemini_cfg, default_provider=default_provider)
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
    tf = (
        Path(token_file)
        if token_file
        else (Path.home() / ".sanctum/secrets/firewalla-bridge-token")
    )
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


# ── HA Green (Home Assistant appliance) gate ─────────────────────────
# A Home Assistant Green at a stable LAN address (default homeassistant.local;
# HA_GREEN_URL to override). It is a Bearer-(owner-)token REST box exactly like the
# Firewalla bridge, so this gate MIRRORS _run_firewalla_pairing: detect on the LAN
# → verify with the owner token (GET /api/ → "API running.") → record the verified
# pairing + the token (0600 secrets file) → report the Tailscale remote-access
# node. HONEST-VERIFY: every ✓ derives from a REAL successful check (a TCP connect,
# the running marker, the tailnet listing), never from "the step ran". A box that
# is unreachable / not verified persists NOTHING (a false "detected" hides a dead
# appliance until the first missed automation). Interactive (--yes skips); the
# token prompt is masked. Every device call is a module-level seam in
# sanctum_cli.devices.ha_green the tests monkeypatch, so no live HA / Tailscale is
# touched in the suite.

#: The Green's LAN MAC, recorded in the persisted services block for the audit trail.
#: Per-operator — NEVER ship one operator's MAC into another's instance.yaml. From env
#: HA_GREEN_MAC, else "" (the services block simply omits an unknown MAC).
_HA_GREEN_MAC = os.environ.get("HA_GREEN_MAC", "")


def set_ha_green(
    *,
    token: str | None,
    host: str,
    port: int,
    device_mac: str,
    tailnet_node: str,
    path: Path | None = None,
    token_file: Path | None = None,
) -> None:
    """Persist a VERIFIED HA Green pairing — mirrors :func:`set_firewalla_bridge`.

    Writes ``services.ha_green`` (enabled + host + port + device_mac + tailnet_node)
    into instance.yaml via raw read-modify-write (sibling blocks preserved, a
    ``<file>.bak`` written first), and — when a ``token`` is given — the owner token
    into the mode-600 secrets file (``~/.sanctum/secrets/ha-token`` by default, the
    SAME path the provider reads), NEVER into instance.yaml. A ``None`` token means
    "the token already on disk verified" — only the reference block is (re)written.
    Callers must only invoke this AFTER :func:`ha_green.api_running` returns True.
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
    services["ha_green"] = {
        "enabled": True,
        "host": host,
        "port": port,
        "device_mac": device_mac,
        "tailnet_node": tailnet_node,
    }
    if target.exists():
        backup = target.parent / (target.name + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    if token is None:
        return
    # Token → secrets file, fail-closed perms (600). Created before write so the
    # token never lands on disk world-readable even briefly (mirrors the Firewalla
    # bridge-token persistence exactly).
    from sanctum_cli.devices import ha_green

    tf = Path(token_file) if token_file else ha_green._HA_TOKEN_FILE
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.touch(mode=0o600, exist_ok=True)
    tf.chmod(0o600)
    tf.write_text(token.strip() + "\n", encoding="utf-8")


def _report_ha_remote_access(node_present: bool) -> None:
    """Print the honest Tailscale remote-access row for the HA Green chapter.

    True ONLY from a real ``tailscale status`` listing of the ``homeassistant``
    node — never from "we tried". A missing node is a calm note (remote access is
    additive; LAN reach is what the chapter actually verified), with the fix.
    """
    from sanctum_cli.devices import ha_green

    if node_present:
        console.print(
            f"  [green]✓[/] remote access ready — Tailscale node "
            f"[bold]{ha_green.tailnet_fqdn()}[/] is joined"
        )
    else:
        console.print(
            f"  [yellow]remote access not joined[/] — the Tailscale node "
            f"[bold]{ha_green._TAILNET_NODE}[/] isn't in the tailnet yet "
            "(enable the Home Assistant Tailscale add-on to reach it off-LAN)"
        )


def _persist_ha_green(*, token: str | None) -> None:
    """Record the verified HA Green pairing (services block + optional token)."""
    from sanctum_cli.devices import ha_green

    host, port = ha_green._url_host_port()
    set_ha_green(
        token=token,
        host=host,
        port=port,
        device_mac=_HA_GREEN_MAC,
        tailnet_node=ha_green._TAILNET_NODE,
    )


def _run_ha_green(*, yes: bool) -> bool:
    """HA Green detection + verification gate — fail-closed, mirrors firewalla-pairing.

    Returns True ONLY when the Green was genuinely VERIFIED (GET /api/ → "API
    running." with the owner token) AND the pairing was recorded; returns False
    when ``--yes`` skips, the Green is not on the LAN, the operator declines, or
    every token attempt is rejected — so the recap reads "skipped" rather than a
    false "paired" (design spec §2/§11; HONEST-VERIFY). Flow:

    1. detect on the LAN (a real TCP connect to the resolved HA_GREEN_URL host);
    2. if the token already on disk verifies (``api_running``) → record + report
       remote access, no prompt (a re-run on a configured haus is idempotent);
    3. else offer to capture the long-lived OWNER token (masked), verify the
       just-entered token against ``GET /api/``, and persist ONLY on the running
       marker — a rejected token writes nothing (3 attempts).

    Interactive by design, so ``--yes`` SKIPS it; a Green that isn't on the LAN is
    silently skipped (haus-aware). Non-blocking: the backup already succeeded, so a
    miss never fails the run.
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` without "
            "--yes to verify your HA Green"
        )
        return False

    from sanctum_cli.devices import ha_green

    host, port = ha_green._url_host_port()
    if not ha_green.lan_reachable():
        console.print(
            f"  [dim]no HA Green detected at {host}:{port} — nothing to verify "
            "(re-run `sanctum onboard` after powering it on)[/]"
        )
        return False

    console.print(f"  [bold]HA Green[/] detected on the LAN ({host}:{port})")

    # Already verified by the token on disk → record + report, no prompt.
    if ha_green.api_running():
        version = ha_green.ha_version()
        _persist_ha_green(token=None)
        console.print(
            f"  [green]✓[/] HA Core verified — API running"
            f"{f' (version {version})' if version else ''}"
        )
        _report_ha_remote_access(ha_green.tailscale_node_present())
        return True

    # Reachable but not verified → offer to capture the owner token.
    if not Confirm.ask(
        "  HA Green is up but not verified — pair it with the owner token now?", default=True
    ):
        console.print("  [dim]skipped — HA Green stays unverified until you add the owner token[/]")
        return False

    for attempt in range(3):
        token = Prompt.ask("  Home Assistant long-lived OWNER token", password=True).strip()
        if not token:
            console.print("  [dim]no token entered — HA Green skipped[/]")
            return False
        if ha_green.api_running(token=token):
            version = ha_green.ha_version(token=token)
            _persist_ha_green(token=token)
            console.print(
                f"  [green]✓[/] HA Green verified + recorded — API running"
                f"{f' (version {version})' if version else ''}"
            )
            _report_ha_remote_access(ha_green.tailscale_node_present())
            return True
        console.print(
            "  [red]✗[/] not verified — GET /api/ did not return the running marker "
            "(wrong token, or use the long-lived OWNER token)"
        )
        if attempt < 2:
            console.print("  [dim]check the token and try again[/]")

    console.print(
        "  [yellow]HA Green NOT verified[/] — nothing written. Re-run `sanctum onboard` "
        "(or `sanctum net ha-green status`) after fixing the token."
    )
    return False


# ── Network-resilience gate (topology-adaptive self-heal) ─────────────
# The onboard-time front door to ``sanctum net heal``. It reads THIS node's live
# L3 posture (``heal.probe_posture`` → ``heal.diagnose_posture``) and, on a
# STATIC_DRIFT verdict (a pinned Manual address that strands the node on any
# foreign LAN), offers the GUIDED DHCP flip — but ONLY after confirming the
# never-strand spine (Tailscale tailnet / TB5) is alive, so a failed flip is
# always reachable out-of-band. It then installs the self-healing LaunchDaemon so
# the node keeps healing after onboarding. Doctrine, encoded here as it is in the
# pure core: never-strand (no flip without a live spine), fail-closed (UNVERIFIED
# posture → no action), stays-out-of-NAT (DOUBLE_NAT_OVERLAP → alert only, never
# touch the router), and HONEST-VERIFY (the green check is from a REAL re-probe
# that reads DHCP-not-static, never from "the step ran"). No live networksetup /
# ipconfig / launchctl call is fired in tests — the reads/mutations are
# module-level seams (``heal.probe_posture`` / ``_flip_to_dhcp`` /
# ``_install_net_heal_daemon``) the tests patch.


def _flip_to_dhcp() -> None:
    """Flip Wi-Fi from a Manual (static) address to DHCP — a module-level seam.

    The one interface-mutation this gate performs, isolated behind a seam so the
    onboarding tests never shell out to ``networksetup``. Uses the same stable
    ``"Wi-Fi"`` macOS service label the pure core's :func:`heal.heal_action_argv`
    emits for the ``flip_dhcp`` action. Best-effort: a failure surfaces on the
    verifying re-probe (which will still read Manual), never as a raise out of
    onboarding.
    """
    import subprocess

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["networksetup", "-setdhcp", "Wi-Fi"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )


def _install_net_heal_daemon() -> None:
    """Install the ``com.sanctum.net-heal`` self-healing LaunchDaemon — a seam.

    Delegates to the CLI's installer (``net._install_heal_daemon``) so the daemon
    assets + launchctl wiring live in one place; a non-root onboarding run prints
    the exact ``sudo`` command rather than half-installing (the installer already
    handles the root gate). Best-effort by contract — a missing installer or a
    launchctl miss is surfaced there and never aborts the gate. Tests patch this
    seam so no real LaunchDaemon is written and no ``launchctl`` runs.
    """
    from sanctum_cli.commands import net as net_cmd

    net_cmd._install_heal_daemon()


def _run_network_resilience(*, yes: bool) -> bool:
    """Network-resilience gate — DHCP-not-static + heal daemon + spine check; fail-closed.

    Returns True ONLY on a genuinely verified good/handled state: a node already
    healthy on DHCP (the daemon is installed to keep it that way), or a STATIC_DRIFT
    node we flipped to DHCP AND a REAL re-probe confirmed reads DHCP-not-static
    (then the daemon is installed). Returns False when ``--yes`` skips, the probe is
    UNVERIFIED (fail-closed), the never-strand spine is down (never-strand: refuse
    to flip), the operator declines the flip, the verdict is DOUBLE_NAT_OVERLAP
    (stays out of the NAT domain: alert only), or the post-flip re-probe still reads
    Manual (HONEST-VERIFY: no false "healed") — so the recap reads "skipped" rather
    than a false "configured" (design spec §2/§11).

    Interactive-context step, so ``--yes`` SKIPS it (no probe, no mutation). The
    step is non-blocking: the backup already succeeded, so a miss never fails the run.
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` without "
            "--yes to check this node's network resilience"
        )
        return False

    posture = heal.probe_posture()
    diag = heal.diagnose_posture(posture, overlap=heal.overlap_for(posture))

    # Fail-closed: we could not read the posture → do nothing, claim nothing.
    if diag.verdict == "UNVERIFIED":
        console.print(
            "  [dim]could not read this node's network posture — nothing to do "
            "(connect it to your network, then re-run `sanctum onboard`)[/]"
        )
        return False

    # Stays-out-of-the-NAT-domain: an overlapping-DMZ LAN with a dead gateway is a
    # router/NAT change a human must make — we alert and NEVER touch the interface.
    if diag.verdict == "DOUBLE_NAT_OVERLAP":
        console.print(
            "  [yellow]![/] your LAN overlaps your ISP's WAN range (double-NAT) and the "
            "gateway is unreachable — Sanctum stays out of the NAT domain."
        )
        if diag.remedy:
            console.print(f"  [dim]→ {escape(diag.remedy)}[/]")
        return False

    # STATIC_DRIFT → offer the guided DHCP flip, but NEVER without a live spine
    # (never-strand: a failed flip must be reachable out-of-band to revert).
    if diag.verdict == "STATIC_DRIFT":
        if not (posture.on_tailnet or posture.tb5_up):
            console.print(
                "  [yellow]![/] this node is on a Manual (static) address, but the "
                "never-strand spine is down (no Tailscale tailnet, no TB5) — refusing "
                "to flip to DHCP (a failed flip could strand it)."
            )
            console.print(
                "  [dim]→ bring the tailnet/TB5 link up, then re-run `sanctum onboard` "
                "(or run `sanctum net heal --apply` on the node itself).[/]"
            )
            return False

        console.print(
            f"  [bold]This node is on a Manual (static) address[/] "
            f"({escape(posture.ip or '?')}) — that strands it on any foreign LAN. "
            "Flipping to DHCP keeps it online as it roams."
        )
        if not Confirm.ask("  flip Wi-Fi to DHCP now?", default=True):
            console.print(
                "  [dim]skipped — this node stays on its static address until you flip it "
                "(run `sanctum net heal --apply` later).[/]"
            )
            return False

        _flip_to_dhcp()

        # HONEST-VERIFY: only claim DHCP-not-static from a REAL re-probe. A re-probe
        # that still reads Manual means the flip did not take — say so, install nothing.
        after = heal.probe_posture()
        if after.config_method == "Manual" or not after.config_method:
            console.print(
                "  [red]✗[/] the flip did not take — the node still reads Manual. "
                "Nothing else changed; re-run `sanctum net heal --apply` (as root) to retry."
            )
            return False
        console.print(
            f"  [green]✓[/] flipped to DHCP — this node now reads "
            f"[bold]{escape(after.config_method)}[/] and will follow the LAN as it roams."
        )
    else:
        # HEALTHY / GATEWAY_DEAD / WRONG_SUBNET on DHCP: nothing static to flip here;
        # the standing daemon handles a dead gateway / renumber on its own cadence.
        console.print(
            f"  [green]✓[/] network posture reads [bold]{escape(diag.verdict)}[/] on DHCP "
            "— no static address to fix."
        )

    # Install the self-healing daemon so the node keeps healing after onboarding.
    # The installer is root-gated (prints the sudo hint on a non-root run); either
    # way the node is now DHCP-not-static, which is the durable resilience win.
    console.print(
        "  [dim]Installing the self-healing daemon so this node auto-heals a dead "
        "gateway / renumber going forward…[/]"
    )
    _install_net_heal_daemon()
    return True


# ── Mesh-join gate (join the open Sanctum mesh) ───────────────────────
# The onboard-time front door to ``sanctum mesh join``. It brings THIS node onto
# the open Sanctum mesh: ensure the tailnet is up, mint the mesh identity (once),
# and register with the discovery tracker — reusing the exact pure orchestration
# (``mesh.join_mesh``) and the real adapters the ``sanctum mesh join`` command
# uses, so the CLI and the gate can never drift. SKIPPABLE and HONEST-VERIFY, the
# same doctrine as network-resilience: ``--yes`` skips cleanly (no tailnet / identity
# / tracker touch), and a green check is emitted ONLY when the join is REAL
# (``JoinReport.joined`` — the tailnet is up AND the tracker ack'd the registration),
# never from "the step ran". A tracker that is down (the real client RAISES) or a
# tailnet that is not up configures nothing and reads "skipped". Non-blocking: the
# backup already succeeded, so a miss never fails the run.


def _run_mesh_join(*, yes: bool) -> bool:
    """Mesh-join gate — join the open Sanctum mesh (skippable, honest-verify).

    Returns True ONLY on a genuinely verified join: ``JoinReport.joined`` — the
    tailnet reads ``Running`` AND the discovery tracker really ack'd the registration.
    Returns False when ``--yes`` skips (no probe/mint/register), the join flow could
    not run (a down/misbehaving tracker RAISES a :class:`SanctumError` from the real
    HTTP client — caught here as a clean skip, never a crash), the tailnet is not up,
    or the tracker declined the registration (HONEST-VERIFY: no false "joined") — so
    the recap reads "skipped" rather than a false "configured" (design spec §2/§11).

    Interactive-context step, so ``--yes`` SKIPS it. The step is non-blocking: the
    backup already succeeded, so a miss never fails the run.
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` without "
            "--yes to join the open Sanctum mesh"
        )
        return False

    # Local import (mirrors the net gate's lazy sibling-command import): reuse the
    # exact join orchestration + real adapters the `sanctum mesh join` command uses.
    from sanctum_cli.commands import mesh as mesh_cmd

    label = mesh_cmd._resolve_label()
    try:
        run = mesh_cmd._build_command_runner()
        store = mesh_cmd._build_identity_store()
        directory = mesh_cmd._build_directory()
        report = mesh_cmd.join_mesh(store=store, directory=directory, run=run, label=label)
    except SanctumError as exc:
        # A down/misbehaving tracker (the real client raises) or an unbuildable seam:
        # nothing joined — surface it as a forgiving skip, never a crashed onboarding.
        console.print(f"  [yellow]![/] mesh join could not run — {escape(exc.message)}")
        if exc.fix:
            console.print(f"  [dim]→ {escape(exc.fix)}[/]")
        return False

    # Best-effort config cache so a re-run reuses this node's label (never fails join).
    mesh_cmd._persist_mesh_config(label, report.addr)

    # HONEST-VERIFY: claim "joined" only from the real outcome — tailnet up AND ack'd.
    if report.joined:
        console.print(
            f"  [green]✓[/] joined the Sanctum mesh as "
            f"[bold]{escape(report.identity_fingerprint)}[/] "
            f"({len(report.peers)} peers, {len(report.champions)} champions to pull)."
        )
        return True

    if not report.tailnet_up:
        console.print(
            "  [yellow]![/] tailnet not up (no live `tailscale status` == Running) — "
            "nothing joined. Bring your tailnet up (`tailscale up`), then re-run "
            "`sanctum onboard` (or `sanctum mesh join`)."
        )
    else:
        console.print(
            "  [yellow]![/] mesh discovery did not ack the registration — nothing "
            "joined. Check the tracker, then re-run `sanctum onboard` (or "
            "`sanctum mesh join`)."
        )
    return False


# ── Wi-Fi identity gate (SERVER auto-enroll on the home SSID) ─────────
# The onboard-time sibling of ``sanctum link optimize``. On THIS node's home SSID
# it reads the live Wi-Fi identity (``link.probe_identity`` → ``diagnose_identity``)
# and classifies the node (``link.classify_node``). ONLY a fixed-infra SERVER whose
# identity reads QUARANTINED / ROTATING is auto-enrolled: we generate a
# MAC-stability .mobileconfig and narrate the one-click System-Settings approve.
# A ROAMER (laptop/phone) or an UNKNOWN node is NEVER auto-enrolled — it gets a
# one-line informational nudge and configures nothing (privacy-first / per-SSID /
# home-only). HONEST-VERIFY: the green check is derived from the REAL probe verdict,
# never from "the step ran"; an UNVERIFIED probe is fail-closed (no action). No live
# radio/router/profile-install call is fired in tests — every read is a module-level
# seam (``link.probe_identity`` / ``_node_signals`` / ``_write_identity_profile``).


def _node_signals(probe: link.IdentityProbe) -> link.NodeSignals:
    """Best-effort, router-agnostic host signals for ``link.classify_node``.

    A thin impure boundary (tests patch it wholesale): reads THIS node's uptime,
    IP-config method, and portability from the local OS. Fail-open to the most
    CONSERVATIVE (privacy-first) value on any read miss — an unreadable signal must
    never up-classify a node to SERVER (which is the only class that auto-enrolls).
    So a miss yields short-uptime / DHCP / not-reserved / portable, all of which push
    ``classify_node`` toward ROAMER/UNKNOWN, never SERVER.
    """
    import subprocess

    def _run(argv: list[str]) -> str:
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=5, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    # Uptime (days) from `sysctl kern.boottime` → { sec = <epoch>, ... }.
    uptime_days = 0.0
    m = re.search(r"sec\s*=\s*(\d+)", _run(["sysctl", "-n", "kern.boottime"]))
    if m:
        with contextlib.suppress(ValueError, OverflowError):
            uptime_days = max(0.0, (time.time() - float(m.group(1))) / 86400.0)

    # IP-config method + reserved/static from the live interface summary.
    ip_config_method = ""
    ip_reserved_or_static = False
    if probe.iface:
        summary = _run(["ipconfig", "getsummary", probe.iface])
        cm = re.search(r"ConfigMethod\s*:\s*(\w+)", summary)
        if cm:
            ip_config_method = cm.group(1)
            ip_reserved_or_static = ip_config_method.lower() == "manual"

    # Portability: a laptop (model contains "Book") is portable; a desktop/server
    # (Mac mini / Mac Studio / Mac Pro / iMac) is not. Unknown model → portable
    # (conservative: never auto-enroll a node we can't prove is fixed).
    model = _run(["sysctl", "-n", "hw.model"]).strip()
    is_portable = ("book" in model.lower()) or (model == "")

    return link.NodeSignals(
        uptime_days=uptime_days,
        ip_config_method=ip_config_method,
        ip_is_reserved_or_static=ip_reserved_or_static,
        # This onboard gate is scoped to the node's CURRENT home SSID (per-SSID,
        # home-only); it does not track SSID history, so it reports 1 distinct SSID.
        distinct_ssids_seen=1,
        is_portable=is_portable,
    )


def _write_identity_profile(probe: link.IdentityProbe, out: Path) -> None:
    """Render the MAC-stability .mobileconfig for a SERVER, at ``out`` (0644).

    Never touches the radio — GENERATES the payload (deterministic uuid5 over
    ssid+hardware_mac; NO plaintext PSK in the managed payload) and lets the operator
    approve it in System Settings. Mirrors ``link._write_profile`` but takes an
    :class:`link.IdentityProbe` (the identity gate's probe shape). Both fields are
    guaranteed non-empty by the caller's SERVER + at-risk verdict, but we guard
    anyway rather than render a broken profile.
    """
    if not probe.ssid or not probe.hardware_mac:
        return
    enc = link._enc_from_security(probe.security)
    profile_xml = link.render_mac_stability_profile(
        probe.ssid, probe.hardware_mac, encryption_type=enc
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(profile_xml, encoding="utf-8")
    out.chmod(0o644)


def _run_wifi_identity(*, yes: bool) -> bool:
    """Wi-Fi identity gate — SERVER auto-enroll on the home SSID; fail-closed.

    Returns True ONLY when the node's identity is a genuinely verified good/handled
    state: a SERVER already STABLE on its hardware MAC, or a SERVER we just generated
    a stability profile for (QUARANTINED / ROTATING). Returns False when ``--yes``
    skips, the probe is UNVERIFIED (fail-closed), or the node is a ROAMER / UNKNOWN
    (privacy-first: nudge only, never auto-enrolled) — so the recap reads "skipped"
    rather than a false "configured" (design spec §2/§11; HONEST-VERIFY).

    Interactive-context step, so ``--yes`` SKIPS it (no probe, no write). Non-blocking:
    the backup already succeeded, so a miss never fails the run.
    """
    if yes:
        console.print(
            "  [yellow]skipped[/] — interactive step; run `sanctum onboard` without "
            "--yes to check this node's Wi-Fi identity"
        )
        return False

    diag = link.diagnose_identity(link.probe_identity())
    probe = diag.probe

    # Fail-closed: we could not read the identity → do nothing, claim nothing.
    if diag.verdict == "IDENTITY_UNVERIFIED":
        console.print(
            "  [dim]could not read this node's Wi-Fi identity — nothing to do "
            "(connect it to your network, then re-run `sanctum onboard`)[/]"
        )
        return False

    node = link.classify_node(_node_signals(probe))

    # PRIVACY: only a fixed-infra SERVER is ever auto-enrolled. A ROAMER / UNKNOWN
    # (laptop, phone, anything we can't prove is fixed) gets a one-line nudge — its
    # owner opts in explicitly via `sanctum link optimize --apply`, never here.
    if node.klass != "SERVER":
        if diag.verdict in ("IDENTITY_QUARANTINED", "IDENTITY_ROTATING"):
            console.print(
                f"  [dim]this node looks like a {node.klass.lower()} "
                f"({escape(node.reason)}) on a rotating MAC — that's fine for a "
                "roamer. To pin it on this network, run "
                "[bold]sanctum link optimize --apply[/] on the node itself.[/]"
            )
        else:
            console.print(
                f"  [dim]{node.klass.lower()} node — Wi-Fi identity left as-is "
                "(privacy-first; roamers opt in per node).[/]"
            )
        return False

    # SERVER already on its hardware MAC → real, verified good state. No profile.
    if diag.verdict == "IDENTITY_STABLE":
        console.print(
            f"  [green]✓[/] Wi-Fi identity STABLE on [bold]{escape(probe.hardware_mac)}[/] "
            f"— this server is pinned to its hardware MAC on "
            f"[bold]{escape(probe.ssid or '?')}[/]"
        )
        return True

    # SERVER + at-risk identity (QUARANTINED / ROTATING) → auto-generate the profile.
    out = link.default_profile_path()
    try:
        _write_identity_profile(probe, out)
    except OSError as exc:
        console.print(
            f"  [yellow]![/] could not write the stability profile ({escape(str(exc))}) "
            "— run [bold]sanctum link optimize --apply[/] on this node to retry."
        )
        return False

    tag = "QUARANTINED" if diag.verdict == "IDENTITY_QUARANTINED" else "ROTATING"
    console.print(
        f"  [bold]This server reads {tag}[/] on [bold]{escape(probe.ssid or '?')}[/] "
        f"(rotating MAC {escape(probe.current_mac)} ≠ hardware "
        f"{escape(probe.hardware_mac)})."
    )
    console.print(
        f"  [green]✓[/] generated a MAC-stability profile → "
        f"[bold]{escape(str(out))}[/] [dim](0644)[/]"
    )
    console.print(
        f"  [dim]One-click approve:[/] open [bold]{escape(str(out))}[/], then System "
        "Settings ▸ Privacy & Security ▸ Profiles → approve "
        "[bold]Wi-Fi MAC Stability[/]."
    )
    console.print(
        "  [dim]It verifies once approved — re-run [bold]sanctum link status[/] to "
        "confirm IDENTITY reads STABLE. This never toggles the radio.[/]"
    )
    return True


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

    SECURITY — the credential NEVER rides on argv. A value passed as an
    ``op item ... credential=<value>`` argument is visible to any same-box ``ps``
    for the (sub-second, same-uid) life of the process. Instead we feed ``op`` a
    JSON item template on **stdin**: ``op item create`` reads a piped template via
    the trailing ``-`` sentinel, and ``op item edit`` auto-consumes a piped
    template — so the secret travels only through the child's stdin pipe, never
    the command line. (The ``op read`` existence probe carries only the item REF,
    not the value.) Best-effort/off-argv is the actionable half of the onboard
    argv audit; the guaranteed-tier ``security -w`` argv is a separate, documented
    same-uid trade-off left untouched.
    """
    import json
    import subprocess

    op_bin = shutil.which("op")
    if op_bin is None:  # pragma: no cover - guarded by _haus_trifecta_present upstream
        msg = "op CLI not found on PATH"
        raise LocalError(msg)
    ref = f"op://{_OP_VAULT}/{title}/credential"
    # `op item edit` updates an existing item; if it is absent, create it. We probe
    # existence with `op read` (cheap, service-account mode is sub-second) so we
    # don't depend on edit-vs-create error strings. The ref carries no secret.
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
    # The secret rides ONLY inside this JSON template on stdin — never argv. A
    # single CONCEALED field labelled `credential` keeps `op read
    # op://<vault>/<title>/credential` (the probe above) resolving.
    template = json.dumps(
        {
            "title": title,
            "category": "PASSWORD",
            "fields": [
                {
                    "id": "credential",
                    "type": "CONCEALED",
                    "label": "credential",
                    "value": value,
                }
            ],
        },
        ensure_ascii=False,  # keep non-ASCII secrets literal in the utf-8 stdin stream
    )
    if exists:
        # `op item edit <item>` auto-reads a piped item template from stdin.
        cmd = [op_bin, "item", "edit", title, "--vault", _OP_VAULT]
    else:
        # Trailing `-` tells `op item create` to read the template from stdin.
        cmd = [op_bin, "item", "create", f"--vault={_OP_VAULT}", "-"]
    proc = subprocess.run(
        cmd,
        input=template,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
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


# ── Haus hardware auto-detect (haus-scan gate) ────────────────────────
# The haus-scan gate DISCOVERS network gear across the LAN — passive ARP + SSDP
# candidates unioned with the always-known gateway — fingerprints each candidate
# through the device registry, and offers to pair every recognized box INLINE,
# pre-seeded with its DISCOVERED ip (not just the gateway). It reuses the exact
# pairing primitives the network-gear gate uses (store_device_secret →
# _probe_device → set_device_reference), so a device paired here is honest-verified
# (a real read-only auth-probe against its own ip) and persisted identically.
# Fail-open: any discovery failure configures nothing and NEVER blocks onboarding —
# the manual pairing gates remain reachable. Every boundary (discovery, the
# provider resolve, the consent prompt) is a module-level seam the tests replace,
# so no live scan / socket / subprocess runs under pytest.


def _recognize_nothing(ip: str, *, runner: object) -> tuple[str, str, float] | None:
    """A fingerprint that matches nothing — the passive-only (no-consent) scan.

    Consent gates the ACTIVE probes; without it the scan surfaces only the tally of
    unrecognized hosts (via ``discover_haus``), never an authenticated fingerprint of
    a stranger. Matches the ``Fingerprint`` seam shape ``(ip, *, runner)``.
    """
    del ip, runner  # intentionally unused: this fingerprint recognizes nothing
    return None


def _discover_haus_for_onboard(net: _NetContext, *, allow_active: bool) -> HausInventory:
    """Real discovery wiring for the gate (a seam so tests inject an inventory).

    With consent (``allow_active``) it runs the real ARP + SSDP + registry scan;
    without it, a passive-only pass that recognizes nothing but still tallies the
    unrecognized hosts — so a decline probes no stranger yet reports honestly.
    """
    if not allow_active:
        return discover_haus(net, allow_active=False, sources=[], fingerprint=_recognize_nothing)
    # Real ARP/SSDP/httpx scan at the onboard boundary; the pure counting logic is
    # exercised by discover_haus's unit tests.
    return build_default_scan(net)  # pragma: no cover - live network scan


def _consent_active_scan(yes: bool) -> bool:
    """Prompt once for consent to actively fingerprint LAN candidates (skip under --yes)."""
    if yes:
        return False
    return bool(Confirm.ask("  scan the LAN to find your gear (a few gentle probes)?", default=True))


def _provider_for(kind: str, ip: str) -> DeviceProvider:  # pragma: no cover - live registry resolve
    """Resolve the provider for a discovered ``kind`` at ``ip`` (for the auth-probe)."""
    from sanctum_cli.devices import registry

    return registry.resolve(kind, _NetContext(gateway_ip=ip, runner=system.real_runner))


def _run_haus_scan(*, yes: bool) -> bool:
    """Discover haus gear and offer to pair each found device inline.

    Fail-open: a discovery failure configures nothing but never blocks onboarding
    (the manual pairing gates remain reachable). Honest-verify: a device is only
    "paired ✓" after a real auth-probe against its DISCOVERED ip. Interactive by
    design, so ``--yes`` SKIPS it (a scripted run against a closed stdin would hang);
    a passive/declined run probes no stranger. Returns True iff at least one device
    was actually PAIRED, so the recap reads "skipped" rather than a false "paired".
    """
    if yes:
        console.print("  [yellow]skipped[/] — interactive discovery; run 'sanctum onboard' attended.")
        return False

    allow_active = _consent_active_scan(yes)
    net = _net_context()
    try:
        inventory = _discover_haus_for_onboard(net, allow_active=allow_active)
    except Exception:  # discovery is additive; a failed scan must never crash onboarding
        console.print("  [dim]discovery unavailable — continuing to manual pairing.[/]")
        return False

    if not inventory.devices:
        if not allow_active:
            # Declined the LAN scan → we probed nothing (not even the gateway), so
            # don't report the un-probed gateway as an "unrecognized" device.
            console.print("  [dim]scan declined — continuing to manual pairing.[/]")
        else:
            extra = f" ({inventory.unrecognized_count} unrecognized)" if inventory.unrecognized_count else ""
            console.print(f"  [dim]no configurable gear found{extra} — continuing.[/]")
        return False

    console.print(f"  Found {inventory.recognized_count} configurable device(s):")
    for dev in inventory.devices:
        console.print(f"    [bold]{dev.kind}[/] ({dev.brand}) at {dev.ip}")
    if inventory.unrecognized_count:
        console.print(f"    [dim]+ {inventory.unrecognized_count} unrecognized device(s)[/]")

    paired_any = False
    for dev in inventory.devices:
        if not Confirm.ask(f"  pair the {dev.kind} ({dev.brand}) at {dev.ip} now?", default=True):
            console.print(f"  [dim]skipped {dev.kind}[/]")
            continue
        service, account = net_cmd.device_keychain_ref(dev.kind)
        if not service or not account:
            console.print(f"  [yellow]skipped {dev.kind}[/] — no Keychain reference")
            continue
        password = Prompt.ask(f"  {dev.kind} admin password", password=True).strip()
        store_device_secret(service=service, account=account, secret=password)
        provider = _provider_for(dev.kind, dev.ip)
        probe_net = _NetContext(gateway_ip=dev.ip, runner=system.real_runner)
        if not _probe_device(provider, net=probe_net, account=account, service=service, secret=password):
            _revoke_device_secret(service=service, account=account)
            console.print(f"  [red]✗[/] {dev.kind} not paired — the admin password was rejected")
            continue
        set_device_reference(
            kind=dev.kind,
            brand=dev.brand,
            host=dev.ip,  # <-- the DISCOVERED ip, not the gateway
            keychain_service=service,
            keychain_account=account,
        )
        console.print(f"  [green]✓[/] {dev.kind} paired — {dev.brand} at {dev.ip}")
        paired_any = True
    return paired_any


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
