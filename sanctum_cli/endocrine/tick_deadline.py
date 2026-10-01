"""Wall-clock cap for one gland beat.

``haus.sanctum.endocrine-gland`` is a launchd StartInterval job. launchd will
not start the next beat while this one is still running, and the sentinel
pages GLAND_DOWN once ``panel.json`` is older than 600s. A beat stuck in
import, DNS, or a socket whose trickle resets ``urlopen``'s per-read timeout
holds that slot and freezes the panel (578s on 2026-09-24).

``SIGALRM`` is a deadline that does not reset when bytes arrive. It raises
:class:`TickDeadlineExceeded`, a ``BaseException``, so the read helpers'
``except Exception`` cannot swallow it and keep the beat alive.

The launchd wrapper (``deploy/endocrine/endocrine-tick.sh``, 25s) is the
outer cap. It still kills the process if this alarm never gets to run
(interpreter stuck before ``tick`` is entered, or a C extension holding the
GIL). This alarm is the inner cap (20s) so a live interpreter exits on its
own before that SIGKILL. ``deploy/endocrine/endocrine-tick-reaper.sh`` is a
separate job that SIGKILLs the launchd pid if it is still the same pid past
the wall, which covers a stall before the wrapper's timer is forked.
"""

from __future__ import annotations

import os
import signal
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# Inner cap. The wrapper's outer cap is a few seconds longer so a live
# interpreter can exit 75 before it is killed.
DEFAULT_DEADLINE_SEC = 20.0
ENV = "SANCTUM_ENDOCRINE_TICK_DEADLINE"
# sysexits.h EX_TEMPFAIL — the beat did not finish; the next interval may.
EXIT_TEMPFAIL = 75

_ARMED = False


class TickDeadlineExceeded(BaseException):
    """The beat outlived its wall deadline.

    Deliberately not an ``Exception``. ``read_memory_headroom_mb``,
    ``read_alert_rate_1h`` and ``broadcast_to_chitti`` catch ``Exception``
    and continue; swallowing this would freeze the panel again.
    """


def deadline_seconds() -> float | None:
    """Seconds for the inner alarm, or None when the operator disables it.

    Unset → :data:`DEFAULT_DEADLINE_SEC`. ``0`` / ``off`` / ``none`` → None
    (tests, and a manual beat that must be allowed to run long). The launchd
    wrapper does not honor a disable: it keeps its own outer cap.
    """
    raw = os.environ.get(ENV)
    if raw is None:
        return DEFAULT_DEADLINE_SEC
    raw = raw.strip().lower()
    if raw in ("", "0", "off", "none"):
        return None
    try:
        sec = float(raw)
    except ValueError:
        return DEFAULT_DEADLINE_SEC
    if sec <= 0:
        return None
    return sec


def arm(seconds: float | None = None) -> float | None:
    """Start the wall timer. ``seconds`` overrides the env. None if disabled."""
    global _ARMED
    sec = deadline_seconds() if seconds is None else seconds
    if sec is None or sec <= 0:
        return None

    def _handler(_signum: int, _frame: object) -> None:
        raise TickDeadlineExceeded(
            f"endocrine tick exceeded {sec:g}s wall deadline"
        )

    signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, sec)
    _ARMED = True
    return sec


def disarm() -> None:
    """Cancel the timer and restore the default SIGALRM action."""
    global _ARMED
    signal.setitimer(signal.ITIMER_REAL, 0)
    if _ARMED:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        _ARMED = False


@contextmanager
def bounded_tick() -> Iterator[float | None]:
    """Arm the deadline for one beat and disarm on every exit path."""
    sec = arm()
    try:
        yield sec
    finally:
        if sec is not None:
            disarm()
