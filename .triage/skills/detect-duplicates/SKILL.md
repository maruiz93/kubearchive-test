---
name: detect-duplicates
description: Search for and identify duplicate issues in a repository
allowed-tools: Bash(gh issue view *), Bash(gh issue list *), Bash(gh search issues *)
---

Search for issues with similar titles and keywords to the current issue
using the `gh` CLI. Use `gh issue view $ISSUE_NUMBER --repo $REPO` to
read the current issue, and `gh issue list --repo $REPO` or
`gh search issues` to find potential duplicates.

Respond ONLY with a JSON object:
```json
{
  "duplicate_of": <issue number or null>,
  "confidence": "high" | "medium" | "low",
  "reason": "<brief explanation>"
}
```
