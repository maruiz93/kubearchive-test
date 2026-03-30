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
    output_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/triage-logs"
    os.makedirs(output_dir, exist_ok=True)

    # Find session transcripts
    patterns = [
        os.path.expanduser("~/.claude/projects/**/subagents/*.jsonl"),
        os.path.expanduser("~/.claude/projects/**/*.jsonl"),
    ]

    found = set()
    for pattern in patterns:
        found.update(glob.glob(pattern, recursive=True))

    if not found:
        print("No transcripts found")
        return

    for path in sorted(found):
        name = os.path.basename(path).replace(".jsonl", "")
        text = convert_transcript(path)
        if text:
            out_path = os.path.join(output_dir, f"{name}.txt")
            with open(out_path, "w") as f:
                f.write(text)
            print(f"Converted: {name} ({len(text)} chars)")


if __name__ == "__main__":
    main()