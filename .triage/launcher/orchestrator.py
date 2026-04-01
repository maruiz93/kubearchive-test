"""Orchestrator: starts MCP servers and launches the triage agent via the agent runner."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import AGENT_RUNNER_PORT, GH_MCP_PORT


def _wait_for_mcp_server(port: int, label: str, timeout: int = 10) -> None:
    """Wait for an MCP server to respond on the given port."""
    for _ in range(timeout * 2):
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}/",
                data=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1)  # nosec B310
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"{label} failed to start on port {port}")


def _start_gh_mcp_server(token: str, repo: str, working_dir: Path) -> subprocess.Popen:
    """Start the GitHub MCP server as a background HTTP process."""
    server_path = working_dir / "tools" / "gh-mcp" / "gh_mcp_server.py"
    env = {
        **os.environ,
        "MCP_GH_TOKEN": token,
        "MCP_ALLOWED_REPO": repo,
    }
    process = subprocess.Popen(
        [  # nosec B607
            "python3",
            str(server_path),
            "--http",
            "--port",
            str(GH_MCP_PORT),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
    )
    _wait_for_mcp_server(GH_MCP_PORT, "GitHub MCP server")
    return process


def _start_agent_runner_mcp_server(
    working_dir: Path,
    mcp_config_path: str,
    owner: str,
    repo_name: str,
    issue_number: int,
) -> subprocess.Popen:
    """Start the agent runner MCP server as a background HTTP process."""
    server_path = working_dir / "tools" / "agent-runner" / "agent_runner_mcp_server.py"
    env = {
        **os.environ,
        "AGENT_RUNNER_WORKING_DIR": str(working_dir),
        "AGENT_RUNNER_MCP_CONFIG": mcp_config_path,
        "AGENT_RUNNER_OWNER": owner,
        "AGENT_RUNNER_REPO_NAME": repo_name,
        "AGENT_RUNNER_ISSUE_NUMBER": str(issue_number),
    }
    process = subprocess.Popen(
        [  # nosec B607
            "python3",
            str(server_path),
            "--http",
            "--port",
            str(AGENT_RUNNER_PORT),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
    )
    _wait_for_mcp_server(AGENT_RUNNER_PORT, "Agent runner MCP server")
    return process


def launch_agent(
    token: str,
    repo: str,
    issue_number: int,
    working_dir: Path,
) -> None:
    """Launch the top-level triage agent in an OpenShell sandbox."""

    owner, repo_name = repo.split("/", 1)
    prompt = f"Triage issue #{issue_number} in {repo}."

    # Check prerequisites
    if not shutil.which("openshell"):
        print("Error: OpenShell is not installed", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(
        ["openshell", "status"],  # nosec B607
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(
            "Error: OpenShell gateway is not running",
            file=sys.stderr,
        )
        sys.exit(1)

    gh_mcp_process = None
    runner_mcp_process = None
    mcp_config_file_path = None

    def cleanup():
        if runner_mcp_process:
            runner_mcp_process.terminate()
            runner_mcp_process.wait(timeout=5)
        if gh_mcp_process:
            gh_mcp_process.terminate()
            gh_mcp_process.wait(timeout=5)
        if mcp_config_file_path and os.path.exists(mcp_config_file_path):
            os.unlink(mcp_config_file_path)

    try:
        # 1. Start GitHub MCP server
        gh_mcp_process = _start_gh_mcp_server(token, repo, working_dir)

        # 2. Write MCP config with both servers
        mcp_config = {
            "mcpServers": {
                "github-triage": {
                    "type": "http",
                    "url": f"http://host.docker.internal:{GH_MCP_PORT}/",
                },
                "agent-runner": {
                    "type": "http",
                    "url": f"http://host.docker.internal:{AGENT_RUNNER_PORT}/",
                },
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="mcp_config_",
            suffix=".json",
            delete=False,
        ) as mcp_config_tmp:
            json.dump(mcp_config, mcp_config_tmp)
            mcp_config_file_path = mcp_config_tmp.name

        # 3. Start agent runner MCP server
        runner_mcp_process = _start_agent_runner_mcp_server(
            working_dir,
            mcp_config_file_path,
            owner,
            repo_name,
            issue_number,
        )

        # --- Log setup ---
        print(f"Triage: {repo}#{issue_number}")
        print(f"  GH MCP:    http://host.docker.internal:{GH_MCP_PORT}/ (pid {gh_mcp_process.pid})")
        print(
            f"  Runner:    http://host.docker.internal"
            f":{AGENT_RUNNER_PORT}/"
            f" (pid {runner_mcp_process.pid})"
        )
        print("---")
        sys.stdout.flush()

        # 4. Run triage agent via the agent runner MCP server
        #    (same sandbox lifecycle as subagents — the MCP server
        #    is the single entry point for all agent execution)
        rpc_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "run_agent",
                "arguments": {
                    "agent_name": "triage",
                    "prompt": prompt,
                    "stream": True,
                },
            },
        }
        req = urllib.request.Request(
            f"http://localhost:{AGENT_RUNNER_PORT}/",
            data=json.dumps(rpc_request).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=660)  # nosec B310
        result = json.loads(resp.read().decode())

        is_error = result.get("result", {}).get("isError", False)
        if is_error:
            output = result["result"]["content"][0]["text"]
            print(f"\nAgent failed: {output}", file=sys.stderr)
            sys.exit(1)
    finally:
        cleanup()
