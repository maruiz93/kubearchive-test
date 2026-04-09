#!/usr/bin/env bash
# test-deploy.sh — Build fullsend, sync experiment files to the test repo,
# upload the binary to a GitHub release, and trigger the workflow.
#
# Usage: ./experiments/runner-hello-world/scripts/test-deploy.sh
#
# Prerequisites:
#   - gh CLI authenticated
#   - /tmp/kubearchive-test is a clone of maruiz93/kubearchive-test

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
EXPERIMENT_DIR="${REPO_ROOT}/experiments/runner-hello-world"
TEST_REPO="/tmp/kubearchive-test"
RELEASE_REPO="maruiz93/fullsend"
RELEASE_TAG="runner-hello-world-dev"
WORKFLOW_REPO="maruiz93/kubearchive-test"
WORKFLOW_FILE="hello-world.yml"

echo "==> Building fullsend (linux/amd64)..."
GOOS=linux GOARCH=amd64 go build -o /tmp/fullsend_build/fullsend "${REPO_ROOT}/cmd/fullsend/"
echo "    Built: /tmp/fullsend_build/fullsend"

echo "==> Creating tarball..."
tar czf /tmp/fullsend_build/fullsend_dev_linux_amd64.tar.gz -C /tmp/fullsend_build fullsend
echo "    Created: /tmp/fullsend_build/fullsend_dev_linux_amd64.tar.gz"

echo "==> Syncing experiment files to ${TEST_REPO}..."
rsync -av --delete \
  --exclude='workflow/' \
  --exclude='PLAN.md' \
  "${EXPERIMENT_DIR}/" "${TEST_REPO}/experiments/runner-hello-world/"

# Sync workflow file to .github/workflows/
cp "${EXPERIMENT_DIR}/workflow/hello-world.yml" "${TEST_REPO}/.github/workflows/hello-world.yml"
echo "    Synced experiment files and workflow"

echo "==> Pushing experiment changes to test repo..."
cd "${TEST_REPO}"
git add -A
if git diff --cached --quiet; then
  echo "    No changes to push"
else
  git commit -m "Update hello-world experiment files"
  git push
  echo "    Pushed"
fi

echo "==> Uploading binary to release ${RELEASE_TAG}..."
gh release upload "${RELEASE_TAG}" \
  /tmp/fullsend_build/fullsend_dev_linux_amd64.tar.gz \
  --clobber --repo "${RELEASE_REPO}"
echo "    Uploaded"

echo "==> Triggering workflow ${WORKFLOW_FILE}..."
RUN_URL=$(gh workflow run "${WORKFLOW_FILE}" --repo "${WORKFLOW_REPO}" 2>&1)
echo "    ${RUN_URL}"

# Give GitHub a moment to register the run, then fetch the URL.
sleep 3
RUN_ID=$(gh run list --repo "${WORKFLOW_REPO}" --workflow "${WORKFLOW_FILE}" --limit 1 --json databaseId --jq '.[0].databaseId')
echo ""
echo "==> Workflow run: https://github.com/${WORKFLOW_REPO}/actions/runs/${RUN_ID}"
echo "    Watch with: gh run watch ${RUN_ID} --repo ${WORKFLOW_REPO}"
