#!/usr/bin/env bash
set -euo pipefail

HELLO_FILE="output/hello-world.md"
SUMMARY_FILE="output/summary.md"

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

# Validate hello-world.txt exists and contains expected output.
if [ ! -f "$HELLO_FILE" ]; then
  echo "FAIL: $HELLO_FILE not found"
  exit 1
fi

if ! grep -q "Hello world from repo" "$HELLO_FILE"; then
  echo "FAIL: $HELLO_FILE missing expected 'Hello world from repo' line"
  exit 1
fi

# Validate summary.md exists.
if [ ! -f "$SUMMARY_FILE" ]; then
  echo "FAIL: $SUMMARY_FILE not found"
  exit 1
fi

echo "PASS: output validated"
exit 0
