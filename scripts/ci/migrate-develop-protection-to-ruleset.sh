#!/usr/bin/env bash
# Move develop's required status checks from classic branch protection into a
# repository ruleset, so the Claude GitHub App can be a bypass actor.
#
# Why: the Release Dev workflow pushes a "[skip ci]" gitops bump straight to
# develop. That push reports no status checks, so classic protection rejects it
# with "GH006 ... 11 of 11 required status checks are expected". Classic
# protection can only exempt users/teams/apps via `restrictions` (push allow
# list), which does not waive required checks, and has no bypass list for apps
# at all. Rulesets do -- an Integration bypass actor skips the ruleset's rules
# entirely -- so the checks have to live in a ruleset instead.
#
# Idempotent: re-running preserves the live ruleset and skips unchanged writes.
# Dry run by default; pass --apply to mutate. Read/API errors abort migration.
#
# Usage:
#   CLAUDE_APP_ID=<app id> scripts/ci/migrate-develop-protection-to-ruleset.sh [--apply]
#   scripts/ci/migrate-develop-protection-to-ruleset.sh <app id> [--apply]
set -euo pipefail

REPO="${REPO:-Goodwiinz/rag}"
BRANCH="develop"
RULESET_NAME="develop required checks"

APPLY=0
APP_ID="${CLAUDE_APP_ID:-}"
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    ''|*[!0-9]*) echo "unknown argument: $arg" >&2; exit 2 ;;
    *) APP_ID="$arg" ;;
  esac
done

if [[ ! "$APP_ID" =~ ^[1-9][0-9]*$ ]]; then
  echo "error: app id required (set CLAUDE_APP_ID or pass it as an argument)" >&2
  echo "       find it at https://github.com/settings/apps/<slug> -> App ID" >&2
  exit 2
fi

# The parent endpoint omits required_status_checks (or returns null) after migration.
# The child endpoint instead returns HTTP 404 plus a nonempty JSON error body;
# never swallow its exit status and mistake that body for a check list.
protection_json="$(gh api "repos/$REPO/branches/$BRANCH/protection")"
classic_checks_json="$(jq -c '
  if type == "object" and (.url | type == "string") and
     (.enforce_admins.enabled | type == "boolean") then
    .required_status_checks
  else error("Invalid branch protection response") end
' <<< "$protection_json")"

rulesets_json="$(gh api --paginate --slurp "repos/$REPO/rulesets?per_page=100")"
existing_id="$(jq -er --arg name "$RULESET_NAME" '
  add | map(select(.name == $name)) |
  if length == 0 then ""
  elif length == 1 and (.[0].id | type == "number") then .[0].id
  else error("Expected at most one matching ruleset") end
' <<< "$rulesets_json")"

existing_json=null
if [ -n "$existing_id" ]; then
  existing_json="$(gh api "repos/$REPO/rulesets/$existing_id")"
  # Avoid silently repurposing a same-named ruleset with a different scope.
  jq -e --arg ref "refs/heads/$BRANCH" '
    .target == "branch" and .enforcement == "active" and
    .conditions == {ref_name: {include: [$ref], exclude: []}} and
    (.rules | type == "array") and (.bypass_actors | type == "array")
  ' <<< "$existing_json" >/dev/null || {
    echo "error: existing ruleset must be active and target only $BRANCH" >&2
    exit 1
  }
fi

payload="$(jq -en \
  --arg name "$RULESET_NAME" \
  --arg ref "refs/heads/$BRANCH" \
  --argjson app_id "$APP_ID" \
  --argjson classic "$classic_checks_json" \
  --argjson existing "$existing_json" '
  # Keep each check provider and the live strictness setting. Do not invent a
  # fallback list when neither source contains checks.
  (if $classic == null then null
   elif ($classic.checks | type) == "array" and
        ($classic.checks | length) > 0 and
        ($classic.strict | type) == "boolean" then {
     type: "required_status_checks",
     parameters: {
       strict_required_status_checks_policy: $classic.strict,
       required_status_checks: [$classic.checks[] |
         {context, integration_id: .app_id}]
     }
   } else error("Invalid or empty classic required checks") end) as $migrated |
  ($existing // {
    name: $name,
    target: "branch",
    enforcement: "active",
    bypass_actors: [],
    conditions: {ref_name: {include: [$ref], exclude: []}},
    rules: []
  }) | {name, target, enforcement, bypass_actors, conditions, rules} |
  if $migrated != null then
    if any(.rules[]; .type == "required_status_checks") then
      .rules |= map(if .type == "required_status_checks" then
        .parameters.strict_required_status_checks_policy |=
          (. or $migrated.parameters.strict_required_status_checks_policy) |
        .parameters.required_status_checks |=
          ((. + $migrated.parameters.required_status_checks) |
           unique_by([.context, .integration_id]))
      else . end)
    else .rules += [$migrated] end
  else . end |
  if any(.bypass_actors[]; .actor_type == "Integration" and .actor_id == $app_id) then
    .bypass_actors |= map(if .actor_type == "Integration" and .actor_id == $app_id
                         then .bypass_mode = "always" else . end)
  else .bypass_actors += [
    {actor_id: $app_id, actor_type: "Integration", bypass_mode: "always"}
  ] end |
  if ([.rules[] | select(.type == "required_status_checks")] | length) != 1 or
     ([.rules[] | select(.type == "required_status_checks") |
       .parameters.required_status_checks[]] | length) == 0 then
    error("Expected one nonempty required status checks rule")
  else . end
')"

if [ -n "$existing_id" ]; then
  method=PUT
  endpoint="repos/$REPO/rulesets/$existing_id"
else
  method=POST
  endpoint="repos/$REPO/rulesets"
fi

echo "repo:            $REPO"
echo "bypass actor:    Integration app id $APP_ID (bypass_mode: always)"
echo "ruleset:         $method /$endpoint"
echo "$payload" | jq .
echo
if [ "$classic_checks_json" != null ]; then
  echo "then: DELETE /repos/$REPO/branches/$BRANCH/protection/required_status_checks"
else
  echo "classic required_status_checks already absent, nothing to remove"
fi

if [ "$APPLY" -ne 1 ]; then
  echo
  echo "dry run -- nothing changed. Re-run with --apply to migrate."
  exit 0
fi

echo
existing_payload="$(jq -S '{name, target, enforcement, bypass_actors, conditions, rules}' <<< "$existing_json")"
if [ "$existing_payload" = "$(jq -S . <<< "$payload")" ]; then
  echo ">> ruleset already matches, nothing to write"
else
  echo ">> writing ruleset"
  # Capture separately so an API failure cannot be hidden by a JSON formatter.
  written_json="$(gh api --method "$method" "$endpoint" --input - <<< "$payload")"
  jq -e '.id | type == "number"' <<< "$written_json" >/dev/null
  jq '{id, name, enforcement}' <<< "$written_json"
fi
if [ "$classic_checks_json" != null ]; then
  # Read back the rule before removing the original enforcement. Extra API
  # defaults are permitted, but every requested rule/setting must be present.
  ruleset_id="${existing_id:-$(jq -r '.id' <<< "$written_json")}"
  verified_json="$(gh api "repos/$REPO/rulesets/$ruleset_id")"
  jq -e --argjson expected "$payload" 'contains($expected)' <<< "$verified_json" >/dev/null || {
    echo "error: ruleset verification failed; keeping classic required checks" >&2
    exit 1
  }
  echo ">> removing required_status_checks from classic protection"
  gh api --method DELETE "repos/$REPO/branches/$BRANCH/protection/required_status_checks"
else
  echo ">> classic required_status_checks already absent, nothing to remove"
fi
echo ">> done. Verify: gh api repos/$REPO/branches/$BRANCH/protection"
