#!/usr/bin/env python3
"""
Launcher for the scoped-tools agent experiment.

This script:
1. Gets a GitHub token (from --token flag, gh CLI, or GitHub App auth)
2. Starts the MCP server with the token (agent never sees it)
3. Launches the top-level triage agent, which orchestrates subagents

The top-level agent decides which subagents to invoke and in what order.
Subagents only have read tools. The top-level agent handles all writes.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

MCP_PORT = 8081


def get_token_from_gh_cli() -> str:
    """Get token from gh CLI auth."""
    result = subprocess.run(
        ["gh", "auth", "token"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        print("Error: could not get token from gh CLI. Use --token or authenticate with `gh auth login`.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def get_token_from_github_app(pem_path: str, client_id: str, installation_id: int, repo_id: int | None = None) -> str:
    """Get token via GitHub App authentication."""
    import jwt
    import requests

    with open(pem_path, "rb") as f:
        signing_key = f.read()

    payload = {
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
        "iss": client_id,
    }
    encoded_jwt = jwt.encode(payload, signing_key, algorithm="RS256")

    headers = {
        "Authorization": f"Bearer {encoded_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    body = {}
    if repo_id:
        body["repository_ids"] = [repo_id]

    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers=headers,
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    token_data = resp.json()
    print(f"Token expires: {token_data.get('expires_at', 'N/A')}")
    return token_data["token"]


def launch_agent(
    token: str,
    repo: str,
    issue_number: int,
    working_dir: Path,
) -> None:
    """Launch the top-level triage agent with the MCP server."""

    mcp_server_path = working_dir / "tools" / "mcp" / "mcp_server.py"

    # Start MCP server as a background HTTP process on the host.
    # The token lives only in this process — agents connect over HTTP.
    mcp_env = {**os.environ, "MCP_GH_TOKEN": token, "MCP_ALLOWED_REPO": repo}
    mcp_process = subprocess.Popen(
        ["python3", str(mcp_server_path), "--http", "--port", str(MCP_PORT)],
        env=mcp_env,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
    )

    # Wait for MCP server to be ready
    mcp_ready = False
    for _ in range(20):
        try:
            req = urllib.request.Request(
                f"http://localhost:{MCP_PORT}/",
                data=json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1)
            mcp_ready = True
            break
        except Exception:
            time.sleep(0.5)

    if not mcp_ready:
        print("Error: MCP server failed to start", file=sys.stderr)
        mcp_process.kill()
        sys.exit(1)

    # Build MCP config pointing to the HTTP server.
    # Agents inside OpenShell sandboxes reach the host via host.docker.internal.
    # Agents running unsandboxed use localhost.
    has_openshell = shutil.which("openshell") is not None
    mcp_host = "host.docker.internal" if has_openshell else "localhost"
    mcp_config = {
        "mcpServers": {
            "github-triage": {
                "type": "http",
                "url": f"http://{mcp_host}:{MCP_PORT}/",
            }
        }
    }

    # Write MCP config to a temp file
    mcp_config_file = tempfile.NamedTemporaryFile(
        mode="w", prefix="mcp_config_", suffix=".json", delete=False,
    )
    json.dump(mcp_config, mcp_config_file)
    mcp_config_file.close()

    # Agent environment has NO GitHub token.
    # REPO and ISSUE_NUMBER are passed so subagent sandbox policies can scope.
    agent_env = {
        k: v for k, v in os.environ.items()
        if k not in ("GH_TOKEN", "MCP_GH_TOKEN", "GITHUB_TOKEN")
    }
    agent_env["REPO"] = repo
    agent_env["ISSUE_NUMBER"] = str(issue_number)
    agent_env["REPO_PATH"] = str(working_dir)
    agent_env["MCP_CONFIG_PATH"] = mcp_config_file.name

    prompt = f"Triage issue #{issue_number} in {repo}."
    sandbox_script = working_dir / "tools" / "scripts" / "run-sandboxed.sh"

    # --- Log setup ---
    print(f"Triage: {repo}#{issue_number}")
    print(f"  MCP server:  http://{mcp_host}:{MCP_PORT}/ (pid {mcp_process.pid})")
    print(f"  Agent token:  in MCP server only")
    print(f"  Sandbox tool: {sandbox_script}")

    if has_openshell:
        print("  Sandbox: OpenShell available, policies will be enforced")
    else:
        print("  Sandbox: OpenShell NOT found, policies defined but NOT enforced")

    print("---")
    print(f"Running: {prompt}")
    sys.stdout.flush()

    try:
        # Launch triage agent via run-sandboxed.sh — same tool used by the
        # triage agent to launch subagents. Handles sandbox creation, policy
        # template rendering, and graceful fallback if OpenShell is unavailable.
        process = subprocess.Popen(
            [str(sandbox_script), "triage", prompt],
            env=agent_env,
            stdout=sys.stdout,
            stderr=sys.stderr,
            cwd=working_dir,
        )
        process.wait(timeout=600)

        if process.returncode != 0:
            print(f"\nAgent exited with code {process.returncode}", file=sys.stderr)
            sys.exit(process.returncode)
    finally:
        os.unlink(mcp_config_file.name)
        mcp_process.terminate()
        mcp_process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch triage agent")

    # Token source (mutually exclusive)
    token_group = parser.add_mutually_exclusive_group()
    token_group.add_argument("--token", help="GitHub token (for testing)")
    token_group.add_argument("--pem", help="Path to GitHub App PEM key (for production)")

    # GitHub App auth options (only needed with --pem)
    parser.add_argument("--client-id", help="GitHub App Client ID")
    parser.add_argument("--installation-id", type=int, help="GitHub App Installation ID")
    parser.add_argument("--repo-id", type=int, help="Repository ID (for scoped token)")

    # Required
    parser.add_argument("--repo", required=True, help="Repository in org/repo format")
    parser.add_argument("--issue", required=True, type=int, help="Issue number to triage")

    args = parser.parse_args()

    base_dir = Path(__file__).parent

    # Get token
    if args.token:
        print("Using provided token...")
        token = args.token
    elif args.pem:
        if not args.client_id or not args.installation_id:
            parser.error("--pem requires --client-id and --installation-id")
        print("Authenticating as GitHub App...")
        token = get_token_from_github_app(
            args.pem, args.client_id, args.installation_id, args.repo_id,
        )
    else:
        print("Getting token from gh CLI...")
        token = get_token_from_gh_cli()

    launch_agent(token, args.repo, args.issue, base_dir)


if __name__ == "__main__":
    main()