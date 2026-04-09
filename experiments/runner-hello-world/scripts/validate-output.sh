#!/usr/bin/env bash
set -euo pipefail

OUTPUT_FILE="output/hello-world.md"

# Counter-based failure for testing retry logic.
# VALIDATION_EXPECTED_FAILURES controls how many times to fail before passing.
EXPECTED_FAILURES="${VALIDATION_EXPECTED_FAILURES:-0}"
COUNTER_FILE=".validation-counter"

if [ "$EXPECTED_FAILURES" -gt 0 ]; then
  COUNT=0
  if [ -f "$COUNTER_FILE" ]; then
    COUNT=$(cat "$COUNTER_FILE")
  fi
  COUNT=$((COUNT + 1))
  echo "$COUNT" > "$COUNTER_FILE"

  if [ "$COUNT" -le "$EXPECTED_FAILURES" ]; then
    echo "FAIL: deliberate failure $COUNT of $EXPECTED_FAILURES (testing retry)"
    exit 1
  fi
fi

echo "PASS: output validated"
exit 0
