#!/usr/bin/env python3
"""
REST API server that proxies GitHub operations for sandboxed agents.

The server holds the GitHub token internally — agents never see it.
It exposes read and write endpoints; sandbox L7 network policies control
which HTTP methods and paths each agent can reach.

Usage:
  python3 gh_server.py --port 8081

Requires environment variables:
  GH_TOKEN: GitHub API token
  GH_ALLOWED_REPO: Repository in org/repo format (e.g. "org/repo")
"""

import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def gh(args: list[str], token: str) -> subprocess.CompletedProcess:
    """Run a gh CLI command with the given token."""
    env = {**os.environ, "GH_TOKEN": token}
    return subprocess.run(
        ["gh", *args],  # nosec B607
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def make_http_handler(token: str, allowed_repo: str) -> type:
    """Create an HTTP request handler class with the token and repo bound."""

    owner, repo_name = allowed_repo.split("/", 1)

    class GitHubAPIHandler(BaseHTTPRequestHandler):

        def do_GET(self):
            # GET /health
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
                return

            # GET /repos/{owner}/{repo}/issues
            if self.path == f"/repos/{owner}/{repo_name}/issues":
                result = gh(
                    ["issue", "list", "--repo", allowed_repo, "--json",
                     "number,title,state,labels,createdAt", "--limit", "100"],
                    token,
                )
                if result.returncode != 0:
                    self._send_json(500, {"error": result.stderr.strip()})
                    return
                self._send_json(200, json.loads(result.stdout))
                return

            # GET /repos/{owner}/{repo}/issues/{number}
            parts = self.path.split("/")
            if (
                len(parts) == 6
                and parts[1] == "repos"
                and parts[2] == owner
                and parts[3] == repo_name
                and parts[4] == "issues"
                and parts[5].isdigit()
            ):
                issue_number = parts[5]
                result = gh(
                    ["issue", "view", issue_number, "--repo", allowed_repo],
                    token,
                )
                if result.returncode != 0:
                    self._send_json(500, {"error": result.stderr.strip()})
                    return
                self._send_json(200, {"body": result.stdout.strip()})
                return

            # GET /search/issues?q=...
            if self.path.startswith("/search/issues"):
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                query = params.get("q", [""])[0]
                if not query:
                    self._send_json(400, {"error": "Missing query parameter 'q'"})
                    return
                # Use --repo flag to scope the search instead of
                # injecting repo: into the query string (which causes
                # gh CLI to produce malformed API queries).
                result = gh(
                    ["search", "issues", query,
                     "--repo", allowed_repo,
                     "--json", "number,title,state,repository"],
                    token,
                )
                if result.returncode != 0:
                    self._send_json(500, {"error": result.stderr.strip()})
                    return
                self._send_json(200, json.loads(result.stdout))
                return

            self._send_json(404, {"error": f"Not found: {self.path}"})

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)

            try:
                body = json.loads(body_bytes) if body_bytes else {}
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
                return

            parts = self.path.split("/")

            # POST /repos/{owner}/{repo}/issues/{number}/comments
            if (
                len(parts) == 7
                and parts[1] == "repos"
                and parts[2] == owner
                and parts[3] == repo_name
                and parts[4] == "issues"
                and parts[5].isdigit()
                and parts[6] == "comments"
            ):
                issue_number = parts[5]
                comment_body = body.get("body", "")

                if not comment_body:
                    self._send_json(400, {"error": "Missing 'body' field"})
                    return

                # Truncate long comments
                if len(comment_body) > 4096:
                    comment_body = (
                        comment_body[:4096]
                        + "\n\n[truncated by server: exceeded 4096 character limit]"
                    )

                # Scan for credential patterns
                credential_patterns = [
                    r"ghp_[a-zA-Z0-9]{36}",
                    r"ghs_[a-zA-Z0-9]+",
                    r"github_pat_",
                    r"-----BEGIN .* KEY-----",
                    r"sk-[a-zA-Z0-9]{20,}",
                ]
                for pattern in credential_patterns:
                    if re.search(pattern, comment_body, re.IGNORECASE):
                        self._send_json(
                            400,
                            {"error": "Comment appears to contain credentials. Refusing."},
                        )
                        return

                result = gh(
                    ["issue", "comment", issue_number, "--repo", allowed_repo,
                     "--body", comment_body],
                    token,
                )
                if result.returncode != 0:
                    self._send_json(500, {"error": result.stderr.strip()})
                    return
                self._send_json(200, {"status": "ok"})
                return

            # POST /repos/{owner}/{repo}/issues/{number}/labels
            if (
                len(parts) == 7
                and parts[1] == "repos"
                and parts[2] == owner
                and parts[3] == repo_name
                and parts[4] == "issues"
                and parts[5].isdigit()
                and parts[6] == "labels"
            ):
                issue_number = parts[5]
                labels = body.get("labels", "")

                if not labels:
                    self._send_json(400, {"error": "Missing 'labels' field"})
                    return

                # Accept both comma-separated string and list
                if isinstance(labels, list):
                    labels = ",".join(labels)

                result = gh(
                    ["issue", "edit", issue_number, "--repo", allowed_repo,
                     "--add-label", labels],
                    token,
                )
                if result.returncode != 0:
                    self._send_json(500, {"error": result.stderr.strip()})
                    return

                # Return updated labels
                view_result = gh(
                    ["issue", "view", issue_number, "--repo", allowed_repo,
                     "--json", "labels"],
                    token,
                )
                if view_result.returncode != 0:
                    self._send_json(200, {"status": "ok", "labels": labels})
                    return
                self._send_json(200, json.loads(view_result.stdout))
                return

            self._send_json(404, {"error": f"Not found: {self.path}"})

        def _send_json(self, status: int, data) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            print(f"[gh-server] {args[0]}", file=sys.stderr)

    return GitHubAPIHandler


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="REST server for GitHub operations")
    parser.add_argument("--port", type=int, default=8081, help="HTTP port (default: 8081)")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN")
    allowed_repo = os.environ.get("GH_ALLOWED_REPO")

    if not token:
        print("Error: GH_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not allowed_repo:
        print("Error: GH_ALLOWED_REPO not set", file=sys.stderr)
        sys.exit(1)

    handler = make_http_handler(token, allowed_repo)
    server = HTTPServer(("0.0.0.0", args.port), handler)  # nosec B104
    print(
        f"GitHub REST server listening on http://0.0.0.0:{args.port}/",
        file=sys.stderr,
    )
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
