#!/bin/bash
# SubagentStart hook: wraps a subagent in an OpenShell sandbox.
#
# Called by Claude Code as a hook. Receives JSON on stdin with agent_type.
# Reads the agent's sandbox policy from its definition file.
#
# Required env vars (set by launcher.py):
#   REPO           - org/repo (e.g. "myorg/myrepo")
#   ISSUE_NUMBER   - issue number being triaged
#   REPO_PATH      - local path to repo checkout (for reproducibility-verifier)

set -euo pipefail

SANDBOX_LOG="${SANDBOX_LOG:-/tmp/sandbox.log}"

log() {
    echo "[$(date -u '+%H:%M:%S')] $1" >> "$SANDBOX_LOG"
    echo "$1" >&2
}

# Read hook input from stdin
INPUT=$(cat)
AGENT_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_type',''))" 2>/dev/null || echo "")

if [[ -z "$AGENT_NAME" ]]; then
    log "Hook: could not read agent_type from input"
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_FILE="${BASE_DIR}/agents/${AGENT_NAME}.md"

if [[ ! -f "$AGENT_FILE" ]]; then
    log "Hook: no agent definition for '${AGENT_NAME}', skipping sandbox"
    exit 0
fi

# Read the sandbox field from agent frontmatter
POLICY=$(sed -n '/^---$/,/^---$/{ /^sandbox:/{ s/^sandbox: *//; p; q; } }' "$AGENT_FILE")

if [[ -z "$POLICY" ]]; then
    log "Hook: no sandbox policy for '${AGENT_NAME}'"
    exit 0
fi

POLICY_PATH="${BASE_DIR}/${POLICY}"

if [[ ! -f "$POLICY_PATH" ]]; then
    log "Hook: sandbox policy not found: ${POLICY_PATH}"
    exit 0
fi

if ! command -v openshell &> /dev/null; then
    log "Hook: openshell not found, agent '${AGENT_NAME}' running unsandboxed (would use: ${POLICY})"
    exit 0
fi

log "Hook: sandboxing '${AGENT_NAME}' with ${POLICY}"
exec openshell run \
    --policy "$POLICY_PATH" \
    --env "REPO=${REPO}" \
    --env "ISSUE_NUMBER=${ISSUE_NUMBER}" \
    --env "REPO_PATH=${REPO_PATH:-/sandbox/repo}" \
    -- "$@"
