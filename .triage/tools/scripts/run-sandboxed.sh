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
# Requires OpenShell to be installed and the gateway running.

set -euo pipefail

AGENT_NAME="$1"
PROMPT="$2"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
AGENT_FILE="${BASE_DIR}/agents/${AGENT_NAME}.md"

if [[ ! -f "$AGENT_FILE" ]]; then
    echo "Error: agent definition not found: $AGENT_FILE" >&2
    exit 1
fi

# Read the sandbox field from agent frontmatter
POLICY=$(sed -n '/^---$/,/^---$/{ /^sandbox:/{ s/^sandbox: *//; p; q; } }' "$AGENT_FILE")

if [[ -z "$POLICY" ]]; then
    echo "Error: no sandbox policy defined for agent '${AGENT_NAME}'" >&2
    exit 1
fi

POLICY_TEMPLATE="${BASE_DIR}/${POLICY}"

if [[ ! -f "$POLICY_TEMPLATE" ]]; then
    echo "Error: policy template not found: ${POLICY_TEMPLATE}" >&2
    exit 1
fi

# Substitute runtime values into policy template.
# REPO (org/repo) and ISSUE_NUMBER are set by launcher.py.
OWNER="${REPO%%/*}"
REPO_NAME="${REPO##*/}"
POLICY_PATH="/tmp/policy-${AGENT_NAME}-$$.yaml"
sed -e "s/{{OWNER}}/${OWNER}/g" \
    -e "s/{{REPO_NAME}}/${REPO_NAME}/g" \
    -e "s/{{ISSUE_NUMBER}}/${ISSUE_NUMBER}/g" \
    "$POLICY_TEMPLATE" > "$POLICY_PATH"

if ! command -v openshell &> /dev/null; then
    echo "Error: OpenShell is not installed" >&2
    exit 1
fi

if ! openshell status &>/dev/null; then
    echo "Error: OpenShell gateway is not running" >&2
    exit 1
fi

SANDBOX_NAME="triage-${AGENT_NAME}-$$"
SSH_CONFIG="/tmp/openshell-ssh-${SANDBOX_NAME}.config"

cleanup() {
    openshell sandbox delete "$SANDBOX_NAME" &>/dev/null || true
    rm -f "$SSH_CONFIG" "$POLICY_PATH"
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
        echo "[run-sandboxed] OpenShell sandbox create failed:" >&2
        cat /tmp/openshell-err-$$.log >&2 2>/dev/null
        rm -f /tmp/openshell-err-$$.log
        exit 1
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
    echo "Error: policy set failed after 3 attempts" >&2
    exit 1
fi

# 3. Get SSH config
openshell sandbox ssh-config "$SANDBOX_NAME" > "$SSH_CONFIG"

# 4. Copy MCP config into the sandbox
#    The MCP config file lives on the host at a temp path that doesn't
#    exist inside the container. Copy it in so the agent can read it.
SANDBOX_MCP_CONFIG="/tmp/mcp_config.json"
scp -F "$SSH_CONFIG" "$MCP_CONFIG_PATH" "openshell-${SANDBOX_NAME}:${SANDBOX_MCP_CONFIG}"

echo "[run-sandboxed] '${AGENT_NAME}' sandbox ready, running agent via SSH" >&2

# 5. Run the claude agent inside the sandbox via SSH
ssh -F "$SSH_CONFIG" "openshell-${SANDBOX_NAME}" \
    "claude --print --agent '${AGENT_NAME}' --mcp-config '${SANDBOX_MCP_CONFIG}' --strict-mcp-config --dangerously-skip-permissions '${PROMPT}'"