#!/bin/bash
# One gland beat for launchd, with a hard wall cap.
#
# haus.sanctum.endocrine-gland is StartInterval. launchd will not start the
# next beat while this process is still running, and the sentinel pages
# GLAND_DOWN once panel.json is older than 600s. A beat stuck in import or
# in a socket that never returns held that slot for 578s on 2026-09-24.
#
# SANCTUM_ENDOCRINE_TICK_WALL (default 25) is this outer cap. It always
# applies. The Python beat also arms SIGALRM at 20s
# (SANCTUM_ENDOCRINE_TICK_DEADLINE) so a live interpreter exits before
# this SIGTERM. Setting the Python deadline to 0 does not disable this cap.

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

SANCTUM="${SANCTUM_BIN:-/Users/bert/.local/bin/sanctum}"

if [[ ! -x "$SANCTUM" ]]; then
  echo "endocrine-tick: sanctum not executable: $SANCTUM" >&2
  exit 78
fi

token=$(mktemp "${TMPDIR:-/tmp}/endocrine-tick.XXXXXX")
"$SANCTUM" endocrine tick "$@" &
tick_pid=$!

# Only kill a process that is still our endocrine beat. The token file is
# removed the moment the beat is reaped, so a recycled pid is not a target.
kill_tick() {
  local sig="$1"
  local cmd
  [[ -f "$token" ]] || return 0
  cmd=$(ps -p "$tick_pid" -o command= 2>/dev/null || true)
  case "$cmd" in
    *"endocrine tick"*)
      # Children first so a foreground sleep/helper is not reparented.
      pkill "-$sig" -P "$tick_pid" 2>/dev/null || true
      kill "-$sig" "$tick_pid" 2>/dev/null || true
      ;;
  esac
}

(
  sleep "$OUTER_DEADLINE"
  if [[ -f "$token" ]] && kill -0 "$tick_pid" 2>/dev/null; then
    echo "endocrine-tick: outer deadline ${OUTER_DEADLINE}s exceeded; killing pid ${tick_pid}" >&3
    kill_tick TERM
    sleep 2
    if [[ -f "$token" ]] && kill -0 "$tick_pid" 2>/dev/null; then
      kill_tick KILL
    fi
  fi
) 3>&2 2>/dev/null &
watch_pid=$!

stop_watch() {
  # pkill the sleep the watchdog is blocked in, then the watchdog itself.
  # Killing only the subshell would reparent that sleep for the rest of
  # the deadline — one stray sleep per healthy beat.
  pkill -P "$watch_pid" 2>/dev/null || true
  kill "$watch_pid" 2>/dev/null || true
  wait "$watch_pid" 2>/dev/null || true
}

# launchd stops the job by signalling this process. Take the child with it.
trap 'kill_tick TERM; rm -f "$token"; stop_watch; exit 143' TERM INT

wait "$tick_pid"
rc=$?
rm -f "$token"
stop_watch
exit "$rc"
