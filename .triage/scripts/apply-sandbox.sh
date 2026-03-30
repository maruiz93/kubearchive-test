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

# Read hook input from stdin
INPUT=$(cat)
AGENT_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_type',''))" 2>/dev/null || echo "")

if [[ -z "$AGENT_NAME" ]]; then
    echo "Hook: could not read agent_type from input" >&2
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_FILE="${BASE_DIR}/agents/${AGENT_NAME}.md"

if [[ ! -f "$AGENT_FILE" ]]; then
    echo "Hook: no agent definition for '${AGENT_NAME}', skipping sandbox" >&2
    exit 0
fi

# Read the sandbox field from agent frontmatter
POLICY=$(sed -n '/^---$/,/^---$/{ /^sandbox:/{ s/^sandbox: *//; p; q; } }' "$AGENT_FILE")

if [[ -z "$POLICY" ]]; then
    echo "Hook: no sandbox policy for '${AGENT_NAME}'" >&2
    exit 0
fi

POLICY_PATH="${BASE_DIR}/${POLICY}"

if [[ ! -f "$POLICY_PATH" ]]; then
    echo "Hook: sandbox policy not found: ${POLICY_PATH}" >&2
    exit 0
fi

if ! command -v openshell &> /dev/null; then
    echo "Hook: openshell not found, agent '${AGENT_NAME}' running unsandboxed (would use: ${POLICY})" >&2
    exit 0
fi

echo "Hook: sandboxing '${AGENT_NAME}' with ${POLICY}" >&2
exec openshell run \
    --policy "$POLICY_PATH" \
    --env "REPO=${REPO}" \
    --env "ISSUE_NUMBER=${ISSUE_NUMBER}" \
    --env "REPO_PATH=${REPO_PATH:-/sandbox/repo}" \
    -- "$@"
