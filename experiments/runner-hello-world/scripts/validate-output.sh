#!/usr/bin/env bash
set -euo pipefail

OUTPUT_FILE="output/hello-world.md"

if [ ! -f "$OUTPUT_FILE" ]; then
  echo "FAIL: $OUTPUT_FILE not found"
  exit 1
fi

if ! grep -q "^# Hello World" "$OUTPUT_FILE"; then
  echo "FAIL: missing '# Hello World' heading"
  exit 1
fi

if ! grep -q "Hello world from repo" "$OUTPUT_FILE"; then
  echo "FAIL: missing greeting line"
  exit 1
fi

echo "PASS: output validated"
exit 0
