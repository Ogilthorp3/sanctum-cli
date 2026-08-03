"""``sanctum wizard`` — the haus greets its human cast member.

Bert's Burning Man name is Wizard, so the CLI keeps a robe on a hook. One
small, PG, zero-network easter egg in the ``matrix`` tradition: an ASCII
wizard, one honest line of cheap local status (version + tier — the same
never-raising seams the Setup Assistant's preflight uses), and the only
closing line that scans. Pure stdlib ``print`` — no probes, instant exit.
"""

from __future__ import annotations

#: The robed figure. Staff to the right, star on the hat, nothing spooky.
ART = r"""
          _
        .' '.
       /  *  \
      /_______\
       (o   o)          *
       ( __/ )         /
      /|     |\       /
     / |     | \     /
    *  | ~~~ |  *   *
       |     |      |
      _|_____|_     |
     '---------'   /|\
"""

CLOSING = "You're a wizard, Bert."


def _cheap_status() -> str:
    """Version + tier in one line — trivially-readable local facts, no probes."""
    from sanctum_cli import __version__
    from sanctum_cli.commands.setup import _tier

    return f"sanctum {__version__} · {_tier()} tier"


def wizard_command() -> None:
    """Print the wizard, one line of real status, and the closing line."""
    print(ART)
    print(_cheap_status())
    print()
    print(CLOSING)
