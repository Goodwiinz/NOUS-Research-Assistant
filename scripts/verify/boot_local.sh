#!/usr/bin/env bash
# Boot a local NOUS target for nous-verify: the backend from backend/.venv and
# the frontend via `dev:offline`, then wait for health. PIDs and logs go under
# .verify-artifacts/boot/ and `stop` signals ONLY the process groups this
# script started, never processes matched by name.
#
# Usage: scripts/verify/boot_local.sh start|stop|status
# Env:   NOUS_VERIFY_BACKEND_PORT (8000), NOUS_VERIFY_FRONTEND_PORT (3000),
#        NOUS_VERIFY_BOOT_TIMEOUT seconds (180),
#        NOUS_VERIFY_BOOT_DIR (<repo>/.verify-artifacts/boot),
#        PYTHON (backend/.venv/bin/python, else the main worktree's venv)
set -uo pipefail

usage() { echo "usage: $0 start|stop|status" >&2; exit 2; }
case "${1:-}" in start|stop|status) ;; *) usage ;; esac

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "boot_local: not inside a git checkout" >&2; exit 2; }
BOOT_DIR="${NOUS_VERIFY_BOOT_DIR:-$ROOT/.verify-artifacts/boot}"
BACKEND_PORT="${NOUS_VERIFY_BACKEND_PORT:-8000}"
FRONTEND_PORT="${NOUS_VERIFY_FRONTEND_PORT:-3000}"
TIMEOUT="${NOUS_VERIFY_BOOT_TIMEOUT:-180}"

for value in "$BACKEND_PORT" "$FRONTEND_PORT" "$TIMEOUT"; do
  case "$value" in ''|*[!0-9]*) echo "boot_local: ports and timeout must be integers" >&2; exit 2 ;; esac
done

resolve_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  if [ -x "$ROOT/backend/.venv/bin/python" ]; then echo "$ROOT/backend/.venv/bin/python"; return; fi
  # A linked worktree has no venv of its own; use the main checkout's.
  local common
  common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || common=""
  if [ -n "$common" ] && [ -x "$(dirname "$common")/backend/.venv/bin/python" ]; then
    echo "$(dirname "$common")/backend/.venv/bin/python"; return
  fi
  echo "$ROOT/backend/.venv/bin/python"
}

port_in_use() { # port
  curl -sS --max-time 2 -o /dev/null "http://127.0.0.1:$1/" >/dev/null 2>&1
}

alive_or_fail() { # name
  local pid
  pid="$(cat "$BOOT_DIR/$1.pid" 2>/dev/null)"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    echo "boot_local: $1 died during startup; see $BOOT_DIR/$1.log" >&2
    return 1
  fi
}

wait_for() { # url seconds service-name
  local url="$1" deadline=$(( $(date +%s) + $2 ))
  until curl -fsS --max-time 5 "$url" >/dev/null 2>&1; do
    alive_or_fail "$3" || return 1  # fail fast instead of waiting out the timeout
    if [ "$(date +%s)" -ge "$deadline" ]; then echo "boot_local: timeout waiting for $url" >&2; return 1; fi
    sleep 2
  done
}

stop() {
  local rc=0 name file pid
  for name in frontend backend; do
    file="$BOOT_DIR/$name.pid"
    [ -f "$file" ] || continue
    pid="$(cat "$file")"
    case "$pid" in ''|*[!0-9]*) echo "boot_local: ignoring malformed $file" >&2; rm -f "$file"; rc=1; continue ;; esac
    if kill -0 "$pid" 2>/dev/null; then
      # Each service runs in its own process group (set -m in start), so the
      # group signal also reaches children such as the Next.js server.
      kill -TERM -- "-$pid" 2>/dev/null || kill "$pid" || rc=1
    fi
    rm -f "$file"
  done
  return $rc
}

start() {
  local backend_py
  backend_py="$(resolve_python)"
  BACKEND_PY="$backend_py"
  if [ ! -x "$BACKEND_PY" ]; then echo "boot_local: no backend Python at $BACKEND_PY (set PYTHON)" >&2; exit 2; fi
  mkdir -p "$BOOT_DIR"
  if [ -f "$BOOT_DIR/backend.pid" ] || [ -f "$BOOT_DIR/frontend.pid" ]; then
    echo "boot_local: already started; run '$0 stop' first" >&2; exit 2
  fi
  for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    if port_in_use "$port"; then
      echo "boot_local: 127.0.0.1:$port is already answering; stop that server or set NOUS_VERIFY_*_PORT" >&2
      exit 2
    fi
  done
  set -m  # give each background service its own process group
  ( cd "$ROOT/backend" && exec "$BACKEND_PY" -m uvicorn src.main:app --host 127.0.0.1 --port "$BACKEND_PORT" ) \
    >"$BOOT_DIR/backend.log" 2>&1 &
  echo $! >"$BOOT_DIR/backend.pid"
  ( cd "$ROOT" && exec pnpm --dir frontend dev:offline --port "$FRONTEND_PORT" ) \
    >"$BOOT_DIR/frontend.log" 2>&1 &
  echo $! >"$BOOT_DIR/frontend.pid"
  set +m
  wait_for "http://127.0.0.1:$BACKEND_PORT/health" "$TIMEOUT" backend || { stop; echo "boot_local: see $BOOT_DIR/backend.log" >&2; exit 1; }
  wait_for "http://127.0.0.1:$BACKEND_PORT/health/readiness" "$TIMEOUT" backend || { stop; echo "boot_local: see $BOOT_DIR/backend.log" >&2; exit 1; }
  wait_for "http://127.0.0.1:$FRONTEND_PORT/login" "$TIMEOUT" frontend || { stop; echo "boot_local: see $BOOT_DIR/frontend.log" >&2; exit 1; }
  alive_or_fail backend || { stop; exit 1; }
  alive_or_fail frontend || { stop; exit 1; }
  echo "backend http://127.0.0.1:$BACKEND_PORT  frontend http://127.0.0.1:$FRONTEND_PORT  logs $BOOT_DIR"
}

status() {
  local name file
  for name in backend frontend; do
    file="$BOOT_DIR/$name.pid"
    if [ -f "$file" ] && kill -0 "$(cat "$file")" 2>/dev/null; then
      echo "$name: running (pid $(cat "$file"))"
    else
      echo "$name: stopped"
    fi
  done
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) usage ;;
esac
