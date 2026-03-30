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

run_unsandboxed() {
    echo "[run-sandboxed] '${AGENT_NAME}' running unsandboxed${1:+ ($1)}" >&2
    exec "${CLAUDE_CMD[@]}"
}

if [[ -z "$POLICY" ]]; then
    run_unsandboxed "no sandbox policy"
fi

POLICY_PATH="${BASE_DIR}/${POLICY}"

if [[ ! -f "$POLICY_PATH" ]]; then
    run_unsandboxed "policy not found: ${POLICY_PATH}"
fi

if ! command -v openshell &> /dev/null; then
    run_unsandboxed "OpenShell not installed, would use: ${POLICY}"
fi

# Try sandbox create with policy — fall back to unsandboxed if the CLI
# doesn't support the flags we need (OpenShell is still alpha).
echo "[run-sandboxed] Running '${AGENT_NAME}' in sandbox: ${POLICY}" >&2
openshell sandbox create \
    --no-keep \
    --no-auto-providers \
    --policy "$POLICY_PATH" \
    -- "${CLAUDE_CMD[@]}" 2>/tmp/openshell-err-$$.log \
&& exit 0

# If openshell failed (e.g. --policy not supported), fall back
OPENSHELL_EXIT=$?
echo "[run-sandboxed] OpenShell sandbox failed (exit $OPENSHELL_EXIT), falling back to unsandboxed" >&2
cat /tmp/openshell-err-$$.log >&2 2>/dev/null
rm -f /tmp/openshell-err-$$.log
exec "${CLAUDE_CMD[@]}"