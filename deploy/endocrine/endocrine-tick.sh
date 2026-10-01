#!/bin/bash
# One gland beat for launchd, with a hard wall cap.
#
# haus.sanctum.endocrine-gland is StartInterval. launchd will not start the
# next beat while this process is still running, and the sentinel pages
# GLAND_DOWN once panel.json is older than 600s.
#
# The timer is a separate process, forked before any stat, mktemp, or
# sanctum exec. On 2026-10-01 two beats held the slot for 134s and 299s
# after this script had been exec'd and never logged "outer deadline",
# because the old sleeper was armed only after those calls and the shell
# then blocked in wait. At the wall this timer SIGKILLs the process group
# of the launchd pid, so a child still stuck in the kernel does not keep
# the slot. endocrine-tick-reaper.sh covers a stall before this fork,
# including xpcproxy.

set -u

OUTER_DEADLINE="${SANCTUM_ENDOCRINE_TICK_WALL:-25}"
case "$OUTER_DEADLINE" in
  ''|*[!0-9]*)
    echo "endocrine-tick: SANCTUM_ENDOCRINE_TICK_WALL must be whole seconds, got ${OUTER_DEADLINE}" >&2
    exit 78
    ;;
esac
if (( OUTER_DEADLINE < 1 )); then
  echo "endocrine-tick: SANCTUM_ENDOCRINE_TICK_WALL must be >= 1, got ${OUTER_DEADLINE}" >&2
  exit 78
fi

# Same pid launchd is tracking. Become the process-group leader so the
# deadline kill hits this beat and not the session that started a test.
if [[ "${SANCTUM_ENDOCRINE_TICK_LEADER:-}" != 1 ]]; then
  export SANCTUM_ENDOCRINE_TICK_LEADER=1
  exec /usr/bin/perl -e 'setpgrp(0, 0) or die "setpgrp: $!\n"; exec @ARGV or die "exec: $!\n"' /bin/bash "$0" "$@"
fi

beat_pid=$$
done_flag="${TMPDIR:-/tmp}/endocrine-tick.done.${beat_pid}"

# Own process group: a group-kill of the beat must not stop the timer,
# and the timer must not need ps (a wedged proc must not stall the kill).
/usr/bin/perl -e '
  setpgrp(0, 0) or die "setpgrp: $!\n";
  my ($wall, $pid, $flag) = @ARGV;
  sleep $wall;
  exit 0 if -f $flag;
  exit 0 unless kill 0, $pid;
  warn "endocrine-tick: outer deadline ${wall}s exceeded; killing pid ${pid}\n";
  kill "KILL", -$pid;
' "$OUTER_DEADLINE" "$beat_pid" "$done_flag" &
watch_pid=$!

stop_watch() {
  # SIGKILL the timer itself. Killing only a child sleep would let the
  # timer continue and fire on a beat that already finished.
  kill -KILL "$watch_pid" 2>/dev/null || true
  wait "$watch_pid" 2>/dev/null || true
}

finish() {
  touch "$done_flag" 2>/dev/null || true
  stop_watch
  rm -f "$done_flag"
  exit "$1"
}

trap 'kill -KILL -$$ 2>/dev/null; exit 143' TERM INT

# Unset in production. Tests point this at a program that blocks to prove
# the timer is already armed before sanctum is started.
if [[ -n "${SANCTUM_ENDOCRINE_TICK_PREHOOK:-}" ]]; then
  "${SANCTUM_ENDOCRINE_TICK_PREHOOK}"
fi

SANCTUM="${SANCTUM_BIN:-/Users/bert/.local/bin/sanctum}"
if [[ ! -x "$SANCTUM" ]]; then
  echo "endocrine-tick: sanctum not executable: $SANCTUM" >&2
  finish 78
fi

"$SANCTUM" endocrine tick "$@" &
tick_pid=$!
wait "$tick_pid"
finish "$?"
