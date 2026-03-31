#!/usr/bin/env python3
"""Convert Claude session JSONL transcripts to readable plain text."""

import glob
import json
import os
import sys


def convert_transcript(jsonl_path: str) -> str:
    lines = []
    for raw in open(jsonl_path):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        role = msg.get("type", "")
        content = msg.get("message", {}).get("content", [])

        if not content:
            continue

        for block in content:
            if isinstance(block, str):
                lines.append(f"[{role.upper()}] {block}")
                continue
            if block.get("type") == "text":
                prefix = "ASSISTANT" if role == "assistant" else role.upper()
                lines.append(f"[{prefix}] {block['text']}")
            elif block.get("type") == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                if isinstance(inp, dict) and len(str(inp)) > 200:
                    inp = {k: v[:100] + "..." if isinstance(v, str) and len(v) > 100 else v for k, v in inp.items()}
                lines.append(f"[TOOL CALL] {name}: {json.dumps(inp, indent=2)}")
            elif block.get("type") == "tool_result":
                content_val = block.get("content", "")
                if isinstance(content_val, list):
                    content_val = " ".join(
                        b.get("text", "") for b in content_val if b.get("type") == "text"
                    )
                if len(str(content_val)) > 500:
                    content_val = str(content_val)[:500] + "..."
                lines.append(f"[TOOL RESULT] {content_val}")

    return "\n\n".join(lines)


def main():
    log_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/triage-logs"
    os.makedirs(log_dir, exist_ok=True)

    # Find .jsonl transcripts already extracted into log_dir by launcher.py
    found = glob.glob(os.path.join(log_dir, "*.jsonl"))

    if not found:
        print("No transcripts found")
        return

    for path in sorted(found):
        basename = os.path.basename(path).replace(".jsonl", "")
        # Strip the UUID suffix to get clean agent names
        # e.g. "duplicate-detector-695a86be-f7dc-..." -> "duplicate-detector"
        parts = basename.split("-")
        # UUIDs are 5 hyphen-separated hex groups; find where the UUID starts
        agent_name = basename
        for i in range(len(parts)):
            candidate = "-".join(parts[i:])
            # Check if the remainder looks like a UUID (8-4-4-4-12 hex)
            if len(candidate) == 36 and all(c in "0123456789abcdef-" for c in candidate):
                agent_name = "-".join(parts[:i]) if i > 0 else basename
                break

        text = convert_transcript(path)
        if text:
            out_path = os.path.join(log_dir, f"{agent_name}.txt")
            with open(out_path, "w") as f:
                f.write(text)
            print(f"Converted: {agent_name} ({len(text)} chars)")


if __name__ == "__main__":
    main()
