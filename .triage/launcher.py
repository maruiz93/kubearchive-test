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
import subprocess
import sys
import tempfile
import time
from pathlib import Path


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

    mcp_server_path = working_dir / "tools" / "mcp_server.py"

    # Build MCP config JSON for the GitHub triage tools server.
    # The token is passed via env to the MCP server process, not to the agent.
    mcp_config = {
        "mcpServers": {
            "github-triage": {
                "type": "stdio",
                "command": "python3",
                "args": [str(mcp_server_path)],
                "env": {
                    "MCP_GH_TOKEN": token,
                    "MCP_ALLOWED_REPO": repo,
                },
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
    # REPO and ISSUE_NUMBER are passed so sandbox policies can scope to the original issue.
    agent_env = {
        k: v for k, v in os.environ.items()
        if k not in ("GH_TOKEN", "MCP_GH_TOKEN", "GITHUB_TOKEN")
    }
    agent_env["REPO"] = repo
    agent_env["ISSUE_NUMBER"] = str(issue_number)
    agent_env["REPO_PATH"] = str(working_dir)
    agent_env["MCP_CONFIG_PATH"] = mcp_config_file.name

    agent_command = [
        "claude",
        "--print",
        "--verbose",
        "--agent", "triage",
        "--mcp-config", mcp_config_file.name,
        "--strict-mcp-config",
        "--dangerously-skip-permissions",
    ]

    prompt = f"Triage issue #{issue_number} in {repo}."

    # --- Log setup ---
    print(f"Triage: {repo}#{issue_number}")
    print(f"  MCP server:  {mcp_server_path}")
    print(f"  Agent token:  stripped from environment")

    # Log agent tools
    agents_dir = working_dir / "agents"
    for agent_file in sorted(agents_dir.glob("*.md")):
        name = agent_file.stem
        tools_line = ""
        sandbox_line = ""
        in_fm = False
        with open(agent_file) as f:
            for line in f:
                if line.strip() == "---":
                    if in_fm:
                        break
                    in_fm = True
                    continue
                if in_fm and line.startswith("tools:"):
                    tools_line = line.split(":", 1)[1].strip()
                if in_fm and line.startswith("sandbox:"):
                    sandbox_line = line.split(":", 1)[1].strip()
        print(f"  [{name}] tools: {tools_line or 'none'}")
        if sandbox_line:
            print(f"  [{name}] sandbox: {sandbox_line}")

    # Log sandbox status
    has_openshell = shutil.which("openshell") is not None
    if has_openshell:
        print("  Sandbox: OpenShell available, policies will be enforced")
    else:
        print("  Sandbox: OpenShell NOT found, policies defined but NOT enforced")

    print("---")
    print(f"Running: {prompt}")
    sys.stdout.flush()

    try:
        process = subprocess.Popen(
            [*agent_command, prompt],
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