#!/usr/bin/env bash
# dnsdist front entrypoint (issue #146 Phase 2).
#
# The PowerDNS spatium-dns-agent renders ${DNSDIST_CONF} into the shared
# volume whenever the operator changes the group's dnsdist settings, and
# removes it when dnsdist is disabled. dnsdist has no clean full-config hot
# reload, so we supervise it: wait for a config, run dnsdist in the
# background, and restart it whenever the config file's mtime changes (or it
# disappears). This keeps the front in sync with the control plane without a
# container restart.
set -u

CONF="${DNSDIST_CONF:-/agent-state/dnsdist.conf}"
POLL="${DNSDIST_POLL_SECONDS:-5}"
DNSDIST_BIN="$(command -v dnsdist)"

pid=""
last_mtime=""

stop_dnsdist() {
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  pid=""
}

trap 'stop_dnsdist; exit 0' TERM INT

echo "dnsdist front: watching $CONF (poll ${POLL}s)"
while true; do
  if [ -s "$CONF" ]; then
    mtime="$(stat -c %Y "$CONF" 2>/dev/null || echo 0)"
    if [ "$mtime" != "$last_mtime" ]; then
      # Validate before swapping — a bad render shouldn't take the front down
      # in a crash loop; keep the previously-running dnsdist instead.
      if "$DNSDIST_BIN" --check-config -C "$CONF" >/dev/null 2>&1; then
        echo "dnsdist front: (re)loading config (mtime=$mtime)"
        stop_dnsdist
        "$DNSDIST_BIN" --supervised --disable-syslog -C "$CONF" &
        pid="$!"
        last_mtime="$mtime"
      else
        echo "dnsdist front: config failed --check-config; keeping current instance" >&2
        last_mtime="$mtime"
      fi
    fi
  else
    # No config (dnsdist disabled for this group) — ensure nothing is running.
    if [ -n "$pid" ]; then
      echo "dnsdist front: config removed; stopping"
      stop_dnsdist
      last_mtime=""
    fi
  fi
  # If dnsdist died unexpectedly, force a reload next tick.
  if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
    echo "dnsdist front: process exited; will restart" >&2
    pid=""
    last_mtime=""
  fi
  sleep "$POLL"
done
