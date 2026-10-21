#!/bin/sh
# Stdio MCP server entry point that task.toml registers for the harness under
# test (claude-code or codex spawn it inside the main container).
#
# stdout is the MCP JSON-RPC channel: nothing in this script or in launch.py
# writes to it before the relay connects to the isolated real bridge.
# Diagnostics go to stderr. All paths are absolute because the agent
# CLI may spawn this with a minimal environment and an arbitrary cwd.
set -eu

NOUS_EVAL_HOME="${NOUS_EVAL_HOME:-/opt/nous-eval}"
NOUS_EVAL_PYTHON="${NOUS_EVAL_PYTHON:-/usr/bin/python3}"

exec "$NOUS_EVAL_PYTHON" -I "$NOUS_EVAL_HOME/stdio_relay.py" "$@"
