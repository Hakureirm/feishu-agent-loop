#!/usr/bin/env bash
# One bounded consumer; never stop the shared Lark event bus.
set -u

if [ "$#" -ne 3 ]; then
  printf 'usage: bash listen.sh CONFIG.json LINE OUTBOX.jsonl\n' >&2
  exit 2
fi
CONFIG=$1
LINE=$2
OUTBOX=$3
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
PYTHON=${PYTHON3:-python3}
LARK=${LARK_CLI:-lark-cli}
DELAY=${FEISHU_LOOP_RETRY_DELAY:-1}
MAX_CYCLES=${FEISHU_LOOP_MAX_CYCLES:-0}
SECONDS_PER_CONSUME=${FEISHU_LOOP_CONSUME_SECONDS:-600}
MAX_DEFERRED=${FEISHU_LOOP_MAX_DEFERRED:-32}
for number in "$DELAY" "$MAX_CYCLES" "$SECONDS_PER_CONSUME" "$MAX_DEFERRED"; do
  case "$number" in ''|*[!0-9]*) printf 'listener limits must be nonnegative integers\n' >&2; exit 2;; esac
done
if [ "$SECONDS_PER_CONSUME" -eq 0 ] || [ "$MAX_DEFERRED" -eq 0 ]; then
  printf 'consume seconds and max deferred must be positive\n' >&2
  exit 2
fi

failure() {
  printf '{"kind":"listener_error","stage":"%s","exit_code":%d,"consecutive_failures":%d}\n' "$1" "$2" "$3"
}

"$PYTHON" "$HERE/events.py" check --config "$CONFIG" --line "$LINE" --outbox "$OUTBOX"
rc=$?
if [ "$rc" -ne 0 ]; then failure preflight "$rc" 1; exit "$rc"; fi

umask 077
WORK=$(mktemp -d "${TMPDIR:-/tmp}/feishu-loop.XXXXXXXX") || exit 2
consumer_pid=""
retain=0
EVENT=""
cleanup() {
  if [ -n "$consumer_pid" ]; then
    kill -TERM "$consumer_pid" 2>/dev/null || :
    wait "$consumer_pid" 2>/dev/null || :
  fi
  if [ "$retain" -eq 0 ]; then
    [ -z "$EVENT" ] || rm -f -- "$EVENT"
    rmdir -- "$WORK" 2>/dev/null || :
  else
    printf 'feishu-loop: retained private event captures in %s\n' "$WORK" >&2
  fi
}
trap cleanup EXIT
trap 'retain=1; exit 130' INT
trap 'retain=1; exit 143' TERM

export LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1
failures=0
cycles=0
deferred=0
while :; do
  cycles=$((cycles + 1))
  EVENT="$WORK/event.$cycles.ndjson"
  "$LARK" event consume im.message.receive_v1 --as bot \
    --timeout "${SECONDS_PER_CONSUME}s" --max-events 1 </dev/null >"$EVENT" &
  consumer_pid=$!
  wait "$consumer_pid"
  rc=$?
  consumer_pid=""
  if [ "$rc" -ne 0 ]; then
    failures=$((failures + 1))
    failure consume "$rc" "$failures"
    if [ -s "$EVENT" ]; then
      retain=1
      printf '{"kind":"listener_stopped","reason":"consume_failed_with_retained_output"}\n'
      exit "$rc"
    fi
    rm -f -- "$EVENT"
    if [ "$failures" -ge 3 ]; then
      printf '{"kind":"listener_stopped","reason":"three_consecutive_consume_failures"}\n'
      exit 1
    fi
  else
    "$PYTHON" "$HERE/events.py" route --config "$CONFIG" --line "$LINE" \
      --outbox "$OUTBOX" --input "$EVENT"
    rc=$?
    if [ "$rc" -eq 3 ]; then
      retain=1
      deferred=$((deferred + 1))
      if [ "$deferred" -ge "$MAX_DEFERRED" ]; then
        printf '{"kind":"listener_stopped","reason":"deferred_capture_limit"}\n'
        exit 3
      fi
    elif [ "$rc" -ne 0 ]; then
      retain=1
      failure route "$rc" 1
      printf '{"kind":"listener_stopped","reason":"event_retained_for_repair"}\n'
      exit "$rc"  # Do not consume the next message after losing this one's route.
    else
      rm -f -- "$EVENT"
    fi
    failures=0
  fi
  if [ "$MAX_CYCLES" -gt 0 ] && [ "$cycles" -ge "$MAX_CYCLES" ]; then
    [ "$failures" -eq 0 ] || exit 1
    [ "$deferred" -eq 0 ] || exit 3
    exit 0
  fi
  sleep "$DELAY"
done
