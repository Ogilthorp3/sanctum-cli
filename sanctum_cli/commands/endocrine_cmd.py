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


@app.command("creative", help="Dose the council into a creative state.")
def creative_cmd(
    ttl: int = typer.Option(
        0,
        "--ttl",
        help="auto-expire the dose after N seconds (0 = until explicitly calmed)",
    ),
) -> None:
    rec = bloodstream.set_creative_mode(True, ttl_seconds=ttl or None)
    msg = (
        "Creative mode DOSED. The gland will SLOWLY elevate dopamine and lower "
        "cortisol over the next few ticks — receptors that subscribe raise "
        "temperature, engage MAX subscription-seat diversity, and tilt to "
        "divergent framing. This is a STATE the endocrine system sustains, not a "
        "per-prompt flag."
    )
    if rec.get("until_epoch"):
        msg += f"\nAuto-expires in {ttl}s (lapses back to baseline on its own)."
    console.print(RichPanel(msg, title="[green]⚗ creative mode ON[/]", title_align="left"))


@app.command("calm", help="Clear creative mode — return to the resting baseline.")
def calm_cmd() -> None:
    bloodstream.set_creative_mode(False)
    console.print(
        RichPanel(
            "Creative mode CLEARED. The gland decays dopamine back to its "
            "setpoint over the next few ticks (slow, not snap) and the council "
            "returns to its conservative/convergent resting disposition.",
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
