"""``sanctum endocrine`` — the council's hormone panel + creative-mode lever.

This is the operator's control surface for the endocrine system (the seventh
organ). It is the ONLY way creative mode is dosed: a file the gland reads as a
real input signal, not a per-prompt flag.

Subcommands:
  panel       show the live hormone panel (read off the bloodstream)
  creative    DOSE the council into a creative state (dopamine↑, cortisol↓);
              optional --ttl to auto-expire back to baseline
  calm        clear creative mode — return to the conservative resting state
  status      one-line disposition summary (creative? + dominant axis)
  tick        run one gland tick locally (read signals → step → broadcast)

Off-by-default: with no dose and no gland running, every read is NEUTRAL and a
subscribing seat is byte-identical to today.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel as RichPanel
from rich.table import Table

from sanctum_cli.endocrine import bloodstream
from sanctum_cli.endocrine.gland import HORMONES, Panel

console = Console()

app = typer.Typer(help="Endocrine system — hormone panel + creative-mode lever.")


def _panel_or_neutral() -> tuple[Panel, bool]:
    """Return (panel, live). live=False means the gland hasn't published → the
    neutral baseline (off-by-default)."""
    p = bloodstream.read_panel()
    if p is None:
        return Panel.neutral(), False
    return p, True


@app.command("panel", help="Show the live hormone panel.")
def panel_cmd() -> None:
    panel, live = _panel_or_neutral()
    table = Table(show_header=True, header_style="bold")
    table.add_column("hormone")
    table.add_column("level", justify="right")
    table.add_column("bar")
    for h in HORMONES:
        v = getattr(panel, h)
        bar = "█" * round(v * 20)
        table.add_row(h, f"{v:.3f}", f"[cyan]{bar}[/]")
    src = (
        "live (gland publishing)"
        if live
        else "NEUTRAL baseline (gland not publishing → off by default)"
    )
    console.print(RichPanel(table, title=f"Hormone panel — {src}", title_align="left"))


# Fail-safe default: a creative dose auto-expires in 4 h unless the operator
# EXPLICITLY opts into a permanent dose with `--ttl 0`. A forgotten dose lapses
# back to baseline on its own — the council can never get stuck hot by accident.
DEFAULT_CREATIVE_TTL = 14_400  # 4 hours


def _gland_caveat() -> str:
    """An honest caveat to append when no gland is publishing — the dose was
    RECORDED but nothing reads it yet, so the council is unchanged until the
    gland daemon runs. Empty string when a live panel is present."""
    _, live = _panel_or_neutral()
    if live:
        return ""
    return (
        "\n\n[yellow]⚠ No gland is publishing right now[/] (no live panel on the "
        "bloodstream). The dose is RECORDED and takes effect the moment the gland "
        "daemon runs; until then the council is unchanged. Start it with "
        "`sanctum endocrine tick` or the gland LaunchAgent."
    )


@app.command("creative", help="Dose the council into a creative state.")
def creative_cmd(
    ttl: int = typer.Option(
        DEFAULT_CREATIVE_TTL,
        "--ttl",
        help=(
            "auto-expire the dose after N seconds "
            f"(default {DEFAULT_CREATIVE_TTL}s = 4 h; pass --ttl 0 for a permanent "
            "dose — discouraged: it can leave the council hot indefinitely)"
        ),
    ),
) -> None:
    # --ttl 0 is the explicit opt-in to a non-expiring dose; warn loudly.
    permanent = ttl == 0
    rec = bloodstream.set_creative_mode(True, ttl_seconds=None if permanent else ttl)
    msg = (
        "Creative mode DOSED. The gland will SLOWLY elevate dopamine and lower "
        "cortisol over the next few ticks — receptors that subscribe raise "
        "temperature and tilt to divergent framing on chat/voice turns. This is "
        "a STATE the endocrine system sustains, not a per-prompt flag."
    )
    if rec.get("until_epoch"):
        msg += f"\nAuto-expires in {ttl}s (lapses back to baseline on its own)."
    else:
        msg += (
            "\n[yellow]⚠ PERMANENT dose (--ttl 0)[/] — it will NOT lapse on its own. "
            "Run `sanctum endocrine calm` to clear it."
        )
    msg += _gland_caveat()
    console.print(RichPanel(msg, title="[green]⚗ creative mode ON[/]", title_align="left"))


@app.command("calm", help="Clear creative mode — return to the resting baseline.")
def calm_cmd() -> None:
    bloodstream.set_creative_mode(False)
    msg = (
        "Creative mode CLEARED. The gland decays dopamine back to its "
        "setpoint over the next few ticks (slow, not snap) and the council "
        "returns to its conservative/convergent resting disposition."
    )
    msg += _gland_caveat()
    console.print(
        RichPanel(
            msg,
            title="[blue]☾ creative mode OFF[/]",
            title_align="left",
        )
    )


@app.command("status", help="One-line disposition summary.")
def status_cmd() -> None:
    panel, live = _panel_or_neutral()
    creative = bloodstream.read_creative_mode()
    axis = panel.dopamine - panel.cortisol
    if axis >= 0.25:
        disp = "exploratory (divergent)"
    elif axis <= -0.25:
        disp = "focused (convergent)"
    else:
        disp = "balanced"
    state = "creative" if creative else "resting"
    src = "live" if live else "neutral(off)"
    console.print(
        f"endocrine: {state} | disposition={disp} | "
        f"dopamine={panel.dopamine:.2f} cortisol={panel.cortisol:.2f} "
        f"noradrenaline={panel.noradrenaline:.2f} | source={src}"
    )


@app.command(
    "off",
    help="Disable endocrine modulation entirely (durable opt-out; distinct from `calm`).",
)
def off_cmd() -> None:
    bloodstream.set_optout(True)
    console.print(
        RichPanel(
            "Endocrine modulation DISABLED (durable). The council no longer reads "
            "the hormone panel at all — temperature and framing return to today's "
            "fixed behaviour. This is broader than `calm`: `calm` clears creative "
            "mode but the council still responds to the gland; `off` unsubscribes "
            "the council from the endocrine system completely.\n\n"
            "Re-enable with `sanctum endocrine on`. (A per-shell SANCTUM_ENDOCRINE=1 "
            "still overrides this marker.)",
            title="[red]⊘ endocrine OFF[/]",
            title_align="left",
        )
    )


@app.command("on", help="Re-enable endocrine modulation (clears the durable opt-out).")
def on_cmd() -> None:
    bloodstream.set_optout(False)
    console.print(
        RichPanel(
            "Endocrine modulation RE-ENABLED. The council reads the live hormone "
            "panel again (ON by default). With no gland publishing this is a no-op "
            "until the gland daemon runs.",
            title="[green]⊙ endocrine ON[/]",
            title_align="left",
        )
    )


@app.command("tick", help="Run one gland tick locally (read signals → step → broadcast).")
def tick_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="compute but broadcast nothing"),
) -> None:
    # Imported lazily so the CLI doesn't pull urllib paths unless used.
    from sanctum_cli.endocrine import gland_daemon

    signals = gland_daemon.read_signals()
    current = gland_daemon.load_checkpoint()
    from sanctum_cli.endocrine.gland import step_panel

    nxt = step_panel(current, signals)
    if not dry_run:
        bloodstream.publish_panel_file(nxt)
        ok = bloodstream.broadcast_to_chitti(nxt)
        chitti = "broadcast→chitti OK" if ok else "chitti broadcast skipped (unreachable)"
    else:
        chitti = "dry-run (no broadcast)"
    console.print(
        f"tick: signals(headroom_mb={signals.headroom_mb} "
        f"alert_rate_1h={signals.alert_rate_1h} hour={signals.hour} "
        f"creative={signals.creative_mode}) → {chitti}"
    )
    status_cmd()
