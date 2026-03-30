#!/bin/bash
# Runs a subagent in an OpenShell sandbox.
#
# Usage: run-sandboxed.sh <agent-name> <prompt>
#
# Reads the agent's sandbox policy from its definition,
# wraps `claude --print --agent <name>` in OpenShell,
# and returns the agent's output.
#
# Expects MCP_CONFIG_PATH in env (set by launcher.py).

set -euo pipefail

AGENT_NAME="$1"
PROMPT="$2"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_FILE="${BASE_DIR}/agents/${AGENT_NAME}.md"

if [[ ! -f "$AGENT_FILE" ]]; then
    echo "Error: agent definition not found: $AGENT_FILE" >&2
    exit 1
fi

# Read the sandbox field from agent frontmatter
POLICY=$(sed -n '/^---$/,/^---$/{ /^sandbox:/{ s/^sandbox: *//; p; q; } }' "$AGENT_FILE")

# Build the claude command
CLAUDE_CMD=(
    claude --print
    --agent "$AGENT_NAME"
    --mcp-config "$MCP_CONFIG_PATH"
    --strict-mcp-config
    --dangerously-skip-permissions
    "$PROMPT"
)

if [[ -z "$POLICY" ]]; then
    echo "[run-sandboxed] No sandbox policy for '${AGENT_NAME}', running unsandboxed" >&2
    exec "${CLAUDE_CMD[@]}"
fi

POLICY_PATH="${BASE_DIR}/${POLICY}"

if [[ ! -f "$POLICY_PATH" ]]; then
    echo "[run-sandboxed] Policy not found: ${POLICY_PATH}, running unsandboxed" >&2
    exec "${CLAUDE_CMD[@]}"
fi

if ! command -v openshell &> /dev/null; then
    echo "[run-sandboxed] OpenShell not found, '${AGENT_NAME}' running unsandboxed (would use: ${POLICY})" >&2
    exec "${CLAUDE_CMD[@]}"
fi

echo "[run-sandboxed] Running '${AGENT_NAME}' in sandbox: ${POLICY}" >&2
exec openshell run \
    --policy "$POLICY_PATH" \
    --env "REPO=${REPO}" \
    --env "ISSUE_NUMBER=${ISSUE_NUMBER}" \
    --env "REPO_PATH=${REPO_PATH:-/sandbox/repo}" \
    -- "${CLAUDE_CMD[@]}"
