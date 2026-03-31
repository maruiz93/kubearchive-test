#!/bin/bash
# Runs a subagent in an OpenShell sandbox.
#
# Usage: run-sandboxed.sh <agent-name> <prompt>
#
# Reads the agent's sandbox policy from its definition,
# creates an OpenShell sandbox, applies the policy via
# `policy set --wait`, runs the agent via SSH, and cleans up.
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

run_unsandboxed() {
    echo "[run-sandboxed] '${AGENT_NAME}' running unsandboxed${1:+ ($1)}" >&2
    exec claude --print \
        --agent "$AGENT_NAME" \
        --mcp-config "$MCP_CONFIG_PATH" \
        --strict-mcp-config \
        --dangerously-skip-permissions \
        "$PROMPT"
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

if ! openshell status &>/dev/null; then
    run_unsandboxed "OpenShell gateway not running, would use: ${POLICY}"
fi

SANDBOX_NAME="triage-${AGENT_NAME}-$$"
SSH_CONFIG="/tmp/openshell-ssh-${SANDBOX_NAME}.config"

cleanup() {
    openshell sandbox delete "$SANDBOX_NAME" &>/dev/null || true
    rm -f "$SSH_CONFIG"
}
trap cleanup EXIT

echo "[run-sandboxed] Running '${AGENT_NAME}' in sandbox: ${POLICY}" >&2

# 1. Create a persistent sandbox
#    `sandbox create` always opens an interactive shell, so we use
#    `timeout` to let it create the sandbox and then move on.
if ! timeout 30 openshell sandbox create \
    --name "$SANDBOX_NAME" \
    --keep \
    --no-auto-providers \
    --no-tty </dev/null 2>/tmp/openshell-err-$$.log; then

    # timeout exits 124, sandbox create may exit non-zero after the
    # interactive shell is killed — check if the sandbox exists
    if ! openshell sandbox get "$SANDBOX_NAME" &>/dev/null; then
        echo "[run-sandboxed] OpenShell sandbox create failed, falling back" >&2
        cat /tmp/openshell-err-$$.log >&2 2>/dev/null
        rm -f /tmp/openshell-err-$$.log
        trap - EXIT
        run_unsandboxed "sandbox create failed"
    fi
fi
rm -f /tmp/openshell-err-$$.log

# 2. Apply the custom policy (replaces built-in defaults)
#    Retry up to 3 times — the first sandbox after gateway start can
#    hit a cold-start timeout while the policy engine initializes.
POLICY_APPLIED=false
for attempt in 1 2 3; do
    if openshell policy set "$SANDBOX_NAME" --policy "$POLICY_PATH" --wait 2>&1; then
        POLICY_APPLIED=true
        break
    fi
    echo "[run-sandboxed] Policy set attempt $attempt failed, retrying in 3s..." >&2
    sleep 3
done

if [[ "$POLICY_APPLIED" != "true" ]]; then
    echo "[run-sandboxed] Policy set failed after 3 attempts, falling back" >&2
    run_unsandboxed "policy set failed"
fi

# 3. Get SSH config
openshell sandbox ssh-config "$SANDBOX_NAME" > "$SSH_CONFIG"

echo "[run-sandboxed] '${AGENT_NAME}' sandbox ready, running agent via SSH" >&2

# 4. Run the claude agent inside the sandbox via SSH
ssh -F "$SSH_CONFIG" "openshell-${SANDBOX_NAME}" \
    "claude --print --agent '${AGENT_NAME}' --mcp-config '${MCP_CONFIG_PATH}' --strict-mcp-config --dangerously-skip-permissions '${PROMPT}'"