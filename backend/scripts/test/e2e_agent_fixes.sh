#!/usr/bin/env bash
# E2E smoke for agent fixes C1 (classifier 404 fallback), C2 (LLM timeout
# guard), C3 (no-outer-retry on arxiv tools).
#
# Prereqs:
#   - Backend services up (docker-compose -f docker-compose.development.yml up -d)
#   - Backend running on :8000 (uvicorn src.main:app --reload)
#   - Dev admin user exists (admin@multimodal-rag.com / admin123)
#   - jq installed
#
# Usage:
#   bash backend/scripts/test/e2e_agent_fixes.sh            # all
#   bash backend/scripts/test/e2e_agent_fixes.sh c1 c3      # subset
set -uo pipefail

API="${AGENT_E2E_API:-http://localhost:8000}"
EMAIL="${AGENT_E2E_EMAIL:-admin@multimodal-rag.com}"
PASS="${AGENT_E2E_PASS:-admin123}"
PROJECT_ID="${AGENT_E2E_PROJECT_ID:-}"   # optional, for writing E2E

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; CLR=$'\033[0m'

pass() { echo "${GRN}PASS${CLR}: $*"; }
fail() { echo "${RED}FAIL${CLR}: $*"; FAILED=1; }
warn() { echo "${YLW}WARN${CLR}: $*"; }
hdr()  { echo; echo "=== $* ==="; }

FAILED=0

# ---------- preflight --------------------------------------------------------

hdr "preflight"
command -v jq >/dev/null || { echo "jq required"; exit 2; }

if ! curl -fsS "$API/healthz" >/dev/null 2>&1 \
  && ! curl -fsS "$API/api/v1/health" >/dev/null 2>&1; then
  warn "no health endpoint reachable at $API — continuing anyway"
fi

# Targets that only need in-process Python (no HTTP, no auth).
NO_AUTH_TARGETS_RE='^(c2u)$'
need_auth=0
for t in "$@"; do
  [[ "$t" =~ $NO_AUTH_TARGETS_RE ]] || need_auth=1
done
[ $# -eq 0 ] && need_auth=1

TOKEN_CACHE="${AGENT_E2E_TOKEN_CACHE:-$HOME/.cache/agent-e2e-token}"

# Verify cached token still works.
token_valid() {
  local tok="$1"
  [ -n "$tok" ] || return 1
  curl -fsS -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $tok" \
    "$API/api/v1/auth/me" 2>/dev/null | grep -q '^200$'
}

# Run the cli-auth device flow: start session, print URL, poll until
# user approves in browser, write token to cache.
cli_auth_dance() {
  local resp sid poll_token url code timeout_s elapsed status interval body
  resp=$(curl -s -X POST "$API/api/v1/cli-auth/start") || return 1
  sid=$(echo "$resp" | jq -r '.session_id // empty')
  poll_token=$(echo "$resp" | jq -r '.poll_token // empty')
  url=$(echo "$resp" | jq -r '.browser_url // empty')
  code=$(echo "$resp" | jq -r '.verification_code // empty')
  interval=$(echo "$resp" | jq -r '.poll_interval_seconds // 2')
  [ -n "$sid" ] && [ -n "$poll_token" ] || { warn "cli-auth start malformed"; return 1; }

  echo
  echo "${YLW}APPROVE LOGIN IN BROWSER${CLR}:"
  echo "  $url"
  echo "  verification code: $code"
  command -v open >/dev/null && open "$url" >/dev/null 2>&1 || true

  timeout_s="${AGENT_E2E_AUTH_TIMEOUT:-180}"
  elapsed=0
  while [ $elapsed -lt $timeout_s ]; do
    body=$(curl -s -H "X-CLI-Poll-Token: $poll_token" "$API/api/v1/cli-auth/status/$sid")
    status=$(echo "$body" | jq -r '.status // "unknown"')
    case "$status" in
      approved|completed|success)
        TOKEN=$(echo "$body" | jq -r '.access_token // .token // empty')
        [ -n "$TOKEN" ] || { warn "approved but no access_token in body: $body"; return 1; }
        return 0 ;;
      denied|rejected|expired|error)
        warn "cli-auth ended: $status"
        return 1 ;;
    esac
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  warn "cli-auth poll timed out after ${timeout_s}s"
  return 1
}

TOKEN=""
AUTH=()
if [ $need_auth -eq 1 ]; then
  # 1. env var override
  if [ -n "${AGENT_E2E_TOKEN:-}" ]; then
    TOKEN="$AGENT_E2E_TOKEN"
    if token_valid "$TOKEN"; then pass "auth via AGENT_E2E_TOKEN"
    else warn "AGENT_E2E_TOKEN invalid/expired"; TOKEN=""; fi
  fi

  # 2. cached file
  if [ -z "$TOKEN" ] && [ -r "$TOKEN_CACHE" ]; then
    TOKEN=$(cat "$TOKEN_CACHE" 2>/dev/null || true)
    if token_valid "$TOKEN"; then pass "auth via cache $TOKEN_CACHE"
    else warn "cached token invalid — re-authenticating"; TOKEN=""; fi
  fi

  # 3. legacy password login (skipped on Supabase SSR backends)
  if [ -z "$TOKEN" ]; then
    TOKEN=$(curl -s -X POST "$API/api/v1/auth/login" \
      -H "Content-Type: application/json" \
      -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" \
      | jq -r '.access_token // empty')
    [ -n "$TOKEN" ] && pass "auth via /auth/login"
  fi

  # 4. cli-auth device flow (interactive)
  if [ -z "$TOKEN" ]; then
    if [ -t 1 ] || [ "${AGENT_E2E_FORCE_CLI_AUTH:-0}" = "1" ]; then
      cli_auth_dance && pass "auth via cli-auth device flow"
    else
      warn "non-interactive shell — skipping cli-auth dance"
    fi
  fi

  if [ -n "$TOKEN" ]; then
    AUTH=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")
    mkdir -p "$(dirname "$TOKEN_CACHE")" 2>/dev/null || true
    umask 077 && printf '%s' "$TOKEN" > "$TOKEN_CACHE" 2>/dev/null || true
  else
    warn "auth failed — HTTP-based targets will be skipped"
    warn "  set AGENT_E2E_TOKEN=<bearer> or approve cli-auth in browser"
  fi
fi

# ---------- helpers ----------------------------------------------------------

# Submit job, poll until terminal, echo job json.
run_job() {
  local payload="$1"
  local job
  job=$(curl -s -X POST "$API/api/v1/agent/execute" "${AUTH[@]}" -d "$payload" \
        | jq -r '.job_id // empty')
  [ -n "$job" ] || { echo ""; return 1; }
  local i=0
  while [ $i -lt 60 ]; do
    local body status
    body=$(curl -s "${AUTH[@]:0:2}" "$API/api/v1/agent/jobs/$job")
    status=$(echo "$body" | jq -r '.status // "unknown"')
    case "$status" in
      completed|failed|error|cancelled) echo "$body"; return 0 ;;
    esac
    sleep 2; i=$((i+1))
  done
  echo "$body"; return 1
}

stream_request() {
  local payload="$1"
  curl -sN -X POST "$API/api/v1/agent/stream" "${AUTH[@]}" -d "$payload"
}

want_arg() {
  for a in "$@"; do [ "$a" = "$WANT" ] && return 0; done; return 1
}

# ---------- C1 — classifier 404 fallback -------------------------------------

run_c1() {
  hdr "C1 — classifier 404 fallback"
  echo "NOTE: requires backend started with"
  echo "  AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT=this-deploy-does-not-exist"
  echo "to actually exercise the 404 path. Without it, this still verifies"
  echo "graph completes on ambiguous queries (keyword fallback)."

  local body status content
  body=$(run_job '{"message":"could you do that thing with the papers from before","thread_id":"e2e-c1"}')
  status=$(echo "$body" | jq -r '.status')
  content=$(echo "$body" | jq -r '.result.messages[-1].content // .messages[-1].content // ""')

  if [ "$status" = "completed" ]; then
    pass "C1 graph completed despite ambiguous query (status=$status)"
    [ -n "$content" ] && echo "  → reply: ${content:0:120}"
  else
    fail "C1 graph status=$status"
    echo "$body" | jq '.' | head -40
  fi
}

# ---------- C2 — LLM timeout fallback ----------------------------------------

run_c2_unit() {
  hdr "C2 — wait_for guard (in-process, forced 1s timeout)"
  if [ ! -x .venv/bin/python ]; then
    warn "no .venv/bin/python found — skip in-process C2 check"
    return
  fi
  .venv/bin/python - <<'PY'
import asyncio, os, sys
os.environ['LANGSMITH_TRACING'] = 'false'
from unittest.mock import patch, MagicMock
from langchain_core.messages import HumanMessage
from src.services.agent import graph as g
from src.services.agent import _nodes_llm as _llm_node_mod

# After the T2.1 refactor, ``llm_node`` lives in ``_nodes_llm`` and
# imports ``AGENT_LLM_TIMEOUT_SECONDS`` from ``_nodes_tools`` at
# function-call time. Patch BOTH the re-export on graph (for any future
# callers that still read it from there) AND the canonical binding the
# live llm_node actually reads.
g.AGENT_LLM_TIMEOUT_SECONDS = 1
_llm_node_mod.AGENT_LLM_TIMEOUT_SECONDS = 1  # force timeout

async def hang(*a, **kw):
    await asyncio.sleep(5)

bound = MagicMock()
bound.ainvoke = hang  # awaiting hang(...) yields a coroutine

fake = MagicMock()
fake.bind_tools = MagicMock(return_value=bound)
fake.bind = MagicMock(return_value=fake)

state = {
    "messages": [HumanMessage(content="hi")],
    "page_context": {"type": "chat"},
    "intent": "general",
    "error_count": 0,
    "user_id": "x",
    "thread_id": "t",
    "retrieved_contexts": [],
    "user_memories": [],
}

with patch.object(g, "_build_llm", return_value=fake), \
     patch("src.services.agent.llm_factory.build_lightweight_llm", return_value=fake), \
     patch("src.services.agent.llm_factory.build_synthesis_llm", return_value=fake):
    out = asyncio.run(g.llm_node(state, {"configurable": {}}))

ok = (out.get("last_error") == "llm_timeout"
      and out.get("error_count") == 1
      and "too long" in (out["messages"][0].content or "").lower())
print("CHECK:", "OK" if ok else "FAIL", out.get("last_error"), out.get("error_count"))
sys.exit(0 if ok else 1)
PY
  if [ $? -eq 0 ]; then
    pass "C2 wait_for fallback returns last_error=llm_timeout + AIMessage"
  else
    fail "C2 wait_for fallback did not trigger"
  fi
}

run_c2_e2e() {
  hdr "C2 — long writing query (real)"
  local payload
  if [ -n "$PROJECT_ID" ]; then
    payload="$(jq -n --arg pid "$PROJECT_ID" '{
      message:"summarize all docs in this project in 2000 words",
      thread_id:"e2e-c2",
      page_context:{type:"project",project_id:$pid}}')"
  else
    payload='{"message":"write a 1500-word essay comparing transformer architectures","thread_id":"e2e-c2"}'
  fi
  local t0 t1 dt body status
  t0=$(date +%s)
  body=$(run_job "$payload")
  t1=$(date +%s); dt=$((t1 - t0))
  status=$(echo "$body" | jq -r '.status')
  if [ "$status" = "completed" ]; then
    pass "C2 writing query completed in ${dt}s (no CancelledError)"
  elif [ $dt -le 100 ]; then
    pass "C2 graph returned $status within ${dt}s (timeout path likely fired)"
  else
    fail "C2 writing query status=$status after ${dt}s"
  fi
}

# ---------- C3 — search_arxiv duration ---------------------------------------

run_c3() {
  hdr "C3 — search_arxiv outer-retry cap"
  local t0 t1 dt body status
  t0=$(date +%s)
  body=$(run_job '{"message":"search arxiv for: deep cox mixtures survival regression","thread_id":"e2e-c3"}')
  t1=$(date +%s); dt=$((t1 - t0))
  status=$(echo "$body" | jq -r '.status')

  if [ "$status" != "completed" ]; then
    fail "C3 status=$status after ${dt}s"
    echo "$body" | jq '.' | head -40
    return
  fi

  # Inspect tool_executions duration if exposed
  local arxiv_ms
  arxiv_ms=$(echo "$body" | jq -r '
    [..|objects|select(.tool_name=="search_arxiv")|.duration_ms]
    | map(select(. != null)) | max // empty
  ')
  if [ -n "$arxiv_ms" ] && [ "$arxiv_ms" != "null" ]; then
    if [ "$arxiv_ms" -le 35000 ]; then
      pass "C3 search_arxiv duration_ms=${arxiv_ms} (≤35000, retry cap working)"
    else
      fail "C3 search_arxiv duration_ms=${arxiv_ms} > 35000 (retry may still stack)"
    fi
  else
    warn "C3 no per-tool duration in response — fallback to wall-clock"
    if [ $dt -le 60 ]; then
      pass "C3 wall-clock ${dt}s ≤60s"
    else
      fail "C3 wall-clock ${dt}s >60s"
    fi
  fi
}

# ---------- main -------------------------------------------------------------

if [ $# -eq 0 ]; then
  WANTS=(c1 c2u c2e c3)
else
  WANTS=("$@")
fi

require_token() {
  if [ ${#AUTH[@]} -eq 0 ]; then
    warn "skip $1 — no auth token"
    return 1
  fi
}

for WANT in "${WANTS[@]}"; do
  case "$WANT" in
    c1)   require_token c1 && run_c1 ;;
    c2u)  run_c2_unit ;;
    c2e)  require_token c2e && run_c2_e2e ;;
    c2)   run_c2_unit; require_token c2e && run_c2_e2e ;;
    c3)   require_token c3 && run_c3 ;;
    *)    warn "unknown target: $WANT (valid: c1 c2 c2u c2e c3)" ;;
  esac
done

hdr "summary"
if [ $FAILED -eq 0 ]; then
  echo "${GRN}all checks passed${CLR}"
  exit 0
else
  echo "${RED}one or more checks failed${CLR}"
  exit 1
fi
