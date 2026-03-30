---
name: detect-duplicates
description: Search for and identify duplicate issues in a repository
allowed-tools: list_issues, read_issue
---

Search for issues with similar titles and keywords to the current issue.
Compare the content of potential matches.

Respond ONLY with a JSON object:
```json
{
  "duplicate_of": <issue number or null>,
  "confidence": "high" | "medium" | "low",
  "reason": "<brief explanation>"
}
```
