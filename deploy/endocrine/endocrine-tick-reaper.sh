#!/bin/bash
# Kill a gland beat that is still the launchd pid past the wall.
#
# endocrine-tick.sh arms its own timer after it has been exec'd. A stall
# inside xpcproxy, or in this process before that timer is forked, never
# reaches it. launchd will not start the next StartInterval while that pid
# lives, and the panel then ages past 600s.
#
# This job is not a child of the beat. Each run records the gland pid.
# The same pid still alive SANCTUM_ENDOCRINE_TICK_WALL seconds later is
# SIGKILLed. The clock starts at the first sight, so a beat can run about
# one StartInterval past the wall. The sentinel stays observe-only; this
# reaper does not kickstart.

set -u

WALL="${SANCTUM_ENDOCRINE_TICK_WALL:-25}"
case "$WALL" in
  ''|*[!0-9]*)
    echo "endocrine-reaper: SANCTUM_ENDOCRINE_TICK_WALL must be whole seconds, got ${WALL}" >&2
    exit 78
    ;;
esac
if (( WALL < 1 )); then
  echo "endocrine-reaper: SANCTUM_ENDOCRINE_TICK_WALL must be >= 1, got ${WALL}" >&2
  exit 78
fi

LABEL="${SANCTUM_ENDOCRINE_LABEL:-haus.sanctum.endocrine-gland}"
STATE="${SANCTUM_ENDOCRINE_REAPER_STATE:-${HOME:-}/.sanctum/state/endocrine/reaper-seen}"
LAUNCHCTL="${LAUNCHCTL:-/bin/launchctl}"

out="$("$LAUNCHCTL" print "gui/$(id -u)/${LABEL}" 2>/dev/null)" || exit 0
pid="$(printf '%s\n' "$out" | /usr/bin/awk '$1 == "pid" && $2 == "=" { print $3; exit }')"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || (( pid < 2 )) || (( pid == $$ )); then
  rm -f "$STATE"
  exit 0
fi
if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$STATE"
  exit 0
fi

mkdir -p "$(dirname "$STATE")" 2>/dev/null || exit 0
now="$(date +%s)"

seen_pid=""
seen_at=""
if [[ -f "$STATE" ]]; then
  read -r seen_pid seen_at < "$STATE" || true
fi
if [[ ! "$seen_pid" =~ ^[0-9]+$ ]] || [[ ! "$seen_at" =~ ^[0-9]+$ ]] || (( seen_pid != pid )) || (( now < seen_at )); then
  umask 077
  printf '%s %s\n' "$pid" "$now" > "$STATE"
  exit 0
fi
age=$((now - seen_at))
if (( age < WALL )); then
  exit 0
fi

echo "endocrine-reaper: beat pid ${pid} still running after ${age}s (> ${WALL}s); SIGKILL" >&2
kill -KILL "$pid" 2>/dev/null || true
# The beat is the process-group leader (endocrine-tick.sh setpgrp). A
# non-leader pid makes this ESRCH, which is harmless.
kill -KILL -"$pid" 2>/dev/null || true
rm -f "$STATE"
exit 0
