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
    "family": ("family-setup", "firewalla-compat"),
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
        if gate == "family-setup":
            console.print(f"\n[bold]Step {step_no}.[/] Family setup")
            _run_family_setup(yes=yes)
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
                member = {"role": role, "notify_imessage": phone, "personal_devices": []}
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
