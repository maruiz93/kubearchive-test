#!/usr/bin/env python3
"""
Launcher for the scoped-tools agent experiment.

This script:
1. Gets a GitHub token (from --token flag, gh CLI, or GitHub App auth)
2. Starts the MCP server with the token (agent never sees it)
3. Creates an OpenShell sandbox for the triage agent
4. Bootstraps the sandbox (copies binaries, agent files, configs)
5. Launches the triage agent inside the sandbox

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
import urllib.request
from pathlib import Path

MCP_PORT = 8081
SANDBOX_WORKSPACE = "/tmp/workspace"


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


def start_mcp_server(token: str, repo: str, working_dir: Path) -> subprocess.Popen:
    """Start MCP server as a background HTTP process. Returns the process."""
    mcp_server_path = working_dir / "tools" / "mcp" / "mcp_server.py"
    mcp_env = {**os.environ, "MCP_GH_TOKEN": token, "MCP_ALLOWED_REPO": repo}
    mcp_process = subprocess.Popen(
        ["python3", str(mcp_server_path), "--http", "--port", str(MCP_PORT)],
        env=mcp_env,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
    )

    # Wait for MCP server to be ready
    for _ in range(20):
        try:
            req = urllib.request.Request(
                f"http://localhost:{MCP_PORT}/",
                data=json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1)
            return mcp_process
        except Exception:
            time.sleep(0.5)

    print("Error: MCP server failed to start", file=sys.stderr)
    mcp_process.kill()
    sys.exit(1)


def create_sandbox(name: str) -> None:
    """Create a persistent OpenShell sandbox."""
    result = subprocess.run(
        ["timeout", "30", "openshell", "sandbox", "create",
         "--name", name, "--keep", "--no-auto-providers", "--no-tty"],
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=35,
    )
    # timeout exits 124 — sandbox create may exit non-zero after the
    # interactive shell is killed. Check if the sandbox actually exists.
    if result.returncode not in (0, 124):
        check = subprocess.run(
            ["openshell", "sandbox", "get", name],
            capture_output=True, timeout=10,
        )
        if check.returncode != 0:
            print(f"Error: sandbox create failed:\n{result.stderr.decode()}", file=sys.stderr)
            sys.exit(1)


def apply_policy(sandbox_name: str, policy_path: str) -> None:
    """Apply a policy to a sandbox, retrying up to 3 times."""
    for attempt in range(1, 4):
        result = subprocess.run(
            ["openshell", "policy", "set", sandbox_name,
             "--policy", policy_path, "--wait"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return
        print(f"  Policy attempt {attempt} failed, retrying in 3s...", file=sys.stderr)
        time.sleep(3)

    print("Error: policy set failed after 3 attempts", file=sys.stderr)
    sys.exit(1)


def get_ssh_config(sandbox_name: str, ssh_config_path: str) -> None:
    """Get SSH config for a sandbox and write to file."""
    result = subprocess.run(
        ["openshell", "sandbox", "ssh-config", sandbox_name],
        capture_output=True, text=True, timeout=10, check=True,
    )
    with open(ssh_config_path, "w") as f:
        f.write(result.stdout)


def sandbox_scp(ssh_config: str, sandbox_name: str, local: str, remote: str) -> None:
    """Copy a file or directory into a sandbox."""
    subprocess.run(
        ["scp", "-F", ssh_config, "-r", str(local),
         f"openshell-{sandbox_name}:{remote}"],
        check=True, timeout=60,
    )


def sandbox_ssh(ssh_config: str, sandbox_name: str, cmd: str) -> None:
    """Run a command inside a sandbox."""
    subprocess.run(
        ["ssh", "-F", ssh_config, f"openshell-{sandbox_name}", cmd],
        check=True, timeout=30,
    )


def render_policy(template_path: Path, owner: str, repo_name: str, issue_number: int) -> str:
    """Render a policy template and return the temp file path."""
    with open(template_path) as f:
        content = f.read()
    content = (
        content
        .replace("{{OWNER}}", owner)
        .replace("{{REPO_NAME}}", repo_name)
        .replace("{{ISSUE_NUMBER}}", str(issue_number))
    )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", prefix="policy_", suffix=".yaml", delete=False,
    )
    tmp.write(content)
    tmp.close()
    return tmp.name


def bootstrap_sandbox(
    ssh_config: str,
    sandbox_name: str,
    working_dir: Path,
    mcp_config_path: str,
) -> None:
    """Copy all required files and binaries into the triage sandbox."""
    scp = lambda local, remote: sandbox_scp(ssh_config, sandbox_name, local, remote)
    ssh = lambda cmd: sandbox_ssh(ssh_config, sandbox_name, cmd)

    # Create workspace structure
    ssh(f"mkdir -p {SANDBOX_WORKSPACE}/.claude/agents "
        f"{SANDBOX_WORKSPACE}/.claude/skills "
        f"{SANDBOX_WORKSPACE}/agents "
        f"{SANDBOX_WORKSPACE}/policies "
        f"{SANDBOX_WORKSPACE}/tools/scripts")

    # Copy agent definitions
    # - agents/ for run-sandboxed.sh (reads frontmatter)
    # - .claude/agents/ for claude --agent CLI
    for agent_file in (working_dir / "agents").glob("*.md"):
        scp(str(agent_file), f"{SANDBOX_WORKSPACE}/agents/")
        scp(str(agent_file), f"{SANDBOX_WORKSPACE}/.claude/agents/")

    # Copy skills for claude --agent CLI
    for skill_dir in (working_dir / "skills").iterdir():
        if skill_dir.is_dir():
            scp(str(skill_dir), f"{SANDBOX_WORKSPACE}/.claude/skills/")

    # Copy policy templates (subagents need these)
    for policy_file in (working_dir / "policies").glob("*.yaml"):
        scp(str(policy_file), f"{SANDBOX_WORKSPACE}/policies/")

    # Copy run-sandboxed.sh
    scp(str(working_dir / "tools" / "scripts" / "run-sandboxed.sh"),
        f"{SANDBOX_WORKSPACE}/tools/scripts/")
    ssh(f"chmod +x {SANDBOX_WORKSPACE}/tools/scripts/run-sandboxed.sh")

    # Copy MCP config
    scp(mcp_config_path, f"{SANDBOX_WORKSPACE}/mcp_config.json")

    # Copy binaries
    openshell_bin = shutil.which("openshell")
    claude_bin = shutil.which("claude")
    if not openshell_bin:
        print("Error: openshell binary not found in PATH", file=sys.stderr)
        sys.exit(1)
    if not claude_bin:
        print("Error: claude binary not found in PATH", file=sys.stderr)
        sys.exit(1)

    scp(openshell_bin, "/usr/local/bin/openshell")
    ssh("chmod +x /usr/local/bin/openshell")
    scp(claude_bin, "/usr/local/bin/claude")
    ssh("chmod +x /usr/local/bin/claude")

    # Configure openshell gateway inside sandbox to reach host gateway
    ssh("openshell gateway add http://host.docker.internal:8080")


def launch_agent(
    token: str,
    repo: str,
    issue_number: int,
    working_dir: Path,
) -> None:
    """Launch the top-level triage agent in an OpenShell sandbox."""

    owner, repo_name = repo.split("/", 1)
    sandbox_name = f"triage-main-{os.getpid()}"
    ssh_config_path = f"/tmp/openshell-ssh-{sandbox_name}.config"
    sandbox_mcp_config = f"{SANDBOX_WORKSPACE}/mcp_config.json"
    prompt = f"Triage issue #{issue_number} in {repo}."

    # Check prerequisites
    if not shutil.which("openshell"):
        print("Error: OpenShell is not installed", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(
        ["openshell", "status"], capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        print("Error: OpenShell gateway is not running", file=sys.stderr)
        sys.exit(1)

    # 1. Start MCP server
    mcp_process = start_mcp_server(token, repo, working_dir)

    # Write MCP config (agents connect via host.docker.internal from sandbox)
    mcp_config = {
        "mcpServers": {
            "github-triage": {
                "type": "http",
                "url": f"http://host.docker.internal:{MCP_PORT}/",
            }
        }
    }
    mcp_config_file = tempfile.NamedTemporaryFile(
        mode="w", prefix="mcp_config_", suffix=".json", delete=False,
    )
    json.dump(mcp_config, mcp_config_file)
    mcp_config_file.close()

    # Render triage policy template
    policy_path = render_policy(
        working_dir / "policies" / "triage-write.yaml",
        owner, repo_name, issue_number,
    )

    # --- Log setup ---
    print(f"Triage: {repo}#{issue_number}")
    print(f"  MCP server:  http://host.docker.internal:{MCP_PORT}/ (pid {mcp_process.pid})")
    print(f"  Sandbox:     {sandbox_name}")
    print(f"  Policy:      policies/triage-write.yaml")
    print("---")
    sys.stdout.flush()

    def cleanup():
        subprocess.run(
            ["openshell", "sandbox", "delete", sandbox_name],
            capture_output=True, timeout=10,
        )
        for path in (mcp_config_file.name, policy_path, ssh_config_path):
            if os.path.exists(path):
                os.unlink(path)
        mcp_process.terminate()
        mcp_process.wait(timeout=5)

    try:
        # 2. Create triage sandbox
        print("Creating triage sandbox...")
        create_sandbox(sandbox_name)

        # 3. Apply triage policy
        print("Applying triage policy...")
        apply_policy(sandbox_name, policy_path)

        # 4. Get SSH config
        get_ssh_config(sandbox_name, ssh_config_path)

        # 5. Bootstrap sandbox with all required files
        print("Bootstrapping triage sandbox...")
        bootstrap_sandbox(
            ssh_config_path, sandbox_name, working_dir, mcp_config_file.name,
        )

        # 6. Run triage agent inside sandbox
        print(f"Running: {prompt}")
        sys.stdout.flush()

        env_vars = (
            f"REPO='{repo}' "
            f"ISSUE_NUMBER='{issue_number}' "
            f"MCP_CONFIG_PATH='{sandbox_mcp_config}'"
        )
        process = subprocess.Popen(
            ["ssh", "-F", ssh_config_path, f"openshell-{sandbox_name}",
             f"cd {SANDBOX_WORKSPACE} && {env_vars} "
             f"claude --print --agent triage "
             f"--mcp-config '{sandbox_mcp_config}' "
             f"--strict-mcp-config --dangerously-skip-permissions "
             f"'{prompt}'"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        process.wait(timeout=600)

        if process.returncode != 0:
            print(f"\nAgent exited with code {process.returncode}", file=sys.stderr)
            sys.exit(process.returncode)
    finally:
        cleanup()


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
