#!/bin/bash
# Wraps a subagent process in an OpenShell sandbox.
#
# Usage: apply-sandbox.sh <agent-name> [-- <command...>]
#
# Reads the agent's sandbox policy from its definition file and
# launches the process inside OpenShell with that policy.
#
# Required env vars (set by launcher.py):
#   REPO           - org/repo (e.g. "myorg/myrepo")
#   ISSUE_NUMBER   - issue number being triaged
#   REPO_PATH      - local path to repo checkout (for reproducibility-verifier)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_NAME="$1"
shift

AGENT_FILE="${BASE_DIR}/agents/${AGENT_NAME}.md"

if [[ ! -f "$AGENT_FILE" ]]; then
    echo "Error: agent definition not found: $AGENT_FILE" >&2
    exit 1
fi

# Read the sandbox field from agent frontmatter
POLICY=$(sed -n '/^---$/,/^---$/{ /^sandbox:/{ s/^sandbox: *//; p; q; } }' "$AGENT_FILE")

if [[ -z "$POLICY" ]]; then
    echo "Warning: no sandbox policy for agent '${AGENT_NAME}', running unsandboxed" >&2
    exec "$@"
fi

POLICY_PATH="${BASE_DIR}/${POLICY}"

if [[ ! -f "$POLICY_PATH" ]]; then
    echo "Error: sandbox policy not found: $POLICY_PATH" >&2
    exit 1
fi

if ! command -v openshell &> /dev/null; then
    echo "Warning: openshell not found, running agent '${AGENT_NAME}' unsandboxed (policy: ${POLICY})" >&2
    exec "$@"
fi

echo "Sandboxing agent '${AGENT_NAME}' with policy: ${POLICY}" >&2
exec openshell run \
    --policy "$POLICY_PATH" \
    --env "REPO=${REPO}" \
    --env "ISSUE_NUMBER=${ISSUE_NUMBER}" \
    --env "REPO_PATH=${REPO_PATH:-/sandbox/repo}" \
    -- "$@"