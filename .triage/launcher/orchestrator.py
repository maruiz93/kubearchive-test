"""Orchestrator: MCP server startup, triage sandbox bootstrap, and agent launch."""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import (
    EXECUTOR_PORT,
    MCP_PORT,
    SANDBOX_CLAUDE_CONFIG,
    SANDBOX_WORKSPACE,
)
from .auth import bootstrap_vertex_creds
from .sandbox import (
    apply_policy,
    create_sandbox,
    delete_sandbox,
    extract_transcripts,
    get_ssh_config,
    render_policy,
    sandbox_scp,
    sandbox_ssh,
)


def _wait_for_mcp_server(port: int, label: str, timeout: int = 10) -> None:
    """Wait for an MCP server to respond on the given port."""
    for _ in range(timeout * 2):
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}/",
                data=json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1)  # nosec B310
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"{label} failed to start on port {port}")


def start_gh_mcp_server(token: str, repo: str, working_dir: Path) -> subprocess.Popen:
    """Start the GitHub MCP server as a background HTTP process."""
    server_path = working_dir / "tools" / "mcp" / "gh_mcp_server.py"
    env = {**os.environ, "MCP_GH_TOKEN": token, "MCP_ALLOWED_REPO": repo}
    process = subprocess.Popen(
        [  # nosec B607
            "python3",
            str(server_path),
            "--http",
            "--port",
            str(MCP_PORT),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
    )
    _wait_for_mcp_server(MCP_PORT, "GitHub MCP server")
    return process


def start_executor_mcp_server(
    working_dir: Path,
    mcp_config_path: str,
    owner: str,
    repo_name: str,
    issue_number: int,
) -> subprocess.Popen:
    """Start the executor MCP server as a background HTTP process."""
    server_path = working_dir / "tools" / "mcp" / "executor_mcp_server.py"
    env = {
        **os.environ,
        "EXECUTOR_WORKING_DIR": str(working_dir),
        "EXECUTOR_MCP_CONFIG": mcp_config_path,
        "EXECUTOR_OWNER": owner,
        "EXECUTOR_REPO_NAME": repo_name,
        "EXECUTOR_ISSUE_NUMBER": str(issue_number),
    }
    process = subprocess.Popen(
        [  # nosec B607
            "python3",
            str(server_path),
            "--http",
            "--port",
            str(EXECUTOR_PORT),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
    )
    _wait_for_mcp_server(EXECUTOR_PORT, "Executor MCP server")
    return process


def bootstrap_triage_sandbox(
    ssh_config_path: str,
    sandbox_name: str,
    working_dir: Path,
    mcp_config_path: str,
) -> None:
    """Bootstrap the triage sandbox with everything it needs."""

    def scp(local, remote):
        sandbox_scp(ssh_config_path, sandbox_name, local, remote)

    def ssh(cmd):
        return sandbox_ssh(ssh_config_path, sandbox_name, cmd)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude binary not found in PATH")

    # Create workspace structure
    ssh(
        f"mkdir -p {SANDBOX_WORKSPACE}/.claude/agents "
        f"{SANDBOX_WORKSPACE}/.claude/skills "
        f"{SANDBOX_WORKSPACE}/bin"
    )

    # Copy claude binary
    scp(claude_bin, f"{SANDBOX_WORKSPACE}/bin/claude")
    ssh(f"chmod +x {SANDBOX_WORKSPACE}/bin/claude")

    # Copy agent definitions
    for agent_file in (working_dir / "agents").glob("*.md"):
        scp(str(agent_file), f"{SANDBOX_WORKSPACE}/.claude/agents/")

    # Copy skills
    for skill_dir in (working_dir / "skills").iterdir():
        if skill_dir.is_dir():
            scp(str(skill_dir), f"{SANDBOX_WORKSPACE}/.claude/skills/")

    # Copy MCP config
    scp(mcp_config_path, f"{SANDBOX_WORKSPACE}/mcp_config.json")


def launch_agent(
    token: str,
    repo: str,
    issue_number: int,
    working_dir: Path,
) -> None:
    """Launch the top-level triage agent in an OpenShell sandbox."""

    owner, repo_name = repo.split("/", 1)
    sandbox_name = f"triage-main-{os.getpid()}"
    ssh_config_path = (  # nosec B108
        f"/tmp/openshell-ssh-{sandbox_name}.config"
    )
    sandbox_mcp_config = f"{SANDBOX_WORKSPACE}/mcp_config.json"
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
    executor_mcp_process = None
    mcp_config_file_path = None
    policy_path = None

    def cleanup():
        # Extract triage transcripts before deleting sandbox
        if os.path.exists(ssh_config_path):
            with contextlib.suppress(Exception):
                extract_transcripts(ssh_config_path, sandbox_name, "triage")
        delete_sandbox(sandbox_name)
        for path in (
            mcp_config_file_path,
            policy_path,
            ssh_config_path,
        ):
            if path and os.path.exists(path):
                os.unlink(path)
        if executor_mcp_process:
            executor_mcp_process.terminate()
            executor_mcp_process.wait(timeout=5)
        if gh_mcp_process:
            gh_mcp_process.terminate()
            gh_mcp_process.wait(timeout=5)

    try:
        # 1. Start GitHub MCP server
        gh_mcp_process = start_gh_mcp_server(token, repo, working_dir)

        # 2. Create triage sandbox
        print("Creating triage sandbox...")
        create_sandbox(sandbox_name)

        # 3. Get SSH config
        ssh_config = get_ssh_config(sandbox_name)
        with open(ssh_config_path, "w") as f:
            f.write(ssh_config)

        # 4. Render and apply triage policy
        policy_path = render_policy(
            working_dir / "policies" / "triage-write.yaml",
            owner,
            repo_name,
            issue_number,
        )
        print("Applying triage policy...")
        apply_policy(sandbox_name, policy_path)

        # 5. Write MCP config with both servers
        mcp_config = {
            "mcpServers": {
                "github-triage": {
                    "type": "http",
                    "url": (f"http://host.docker.internal:{MCP_PORT}/"),
                },
                "executor": {
                    "type": "http",
                    "url": (f"http://host.docker.internal:{EXECUTOR_PORT}/"),
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

        # 6. Start executor MCP server
        executor_mcp_process = start_executor_mcp_server(
            working_dir,
            mcp_config_file_path,
            owner,
            repo_name,
            issue_number,
        )

        # --- Log setup ---
        print(f"Triage: {repo}#{issue_number}")
        print(f"  GH MCP:    http://host.docker.internal:{MCP_PORT}/ (pid {gh_mcp_process.pid})")
        print(
            f"  Executor:  http://host.docker.internal"
            f":{EXECUTOR_PORT}/"
            f" (pid {executor_mcp_process.pid})"
        )
        print(f"  Sandbox:   {sandbox_name}")
        print("  Policy:    policies/triage-write.yaml")
        print("---")
        sys.stdout.flush()

        # 7. Bootstrap sandbox
        print("Bootstrapping triage sandbox...")
        bootstrap_triage_sandbox(
            ssh_config_path,
            sandbox_name,
            working_dir,
            mcp_config_file_path,
        )

        # 7b. Copy Vertex AI credentials
        vertex_exports = bootstrap_vertex_creds(ssh_config_path, sandbox_name)

        # 7c. Verify connectivity from sandbox to host services
        print("Verifying sandbox connectivity...")
        for port, name in [
            (MCP_PORT, "GH MCP server"),
            (EXECUTOR_PORT, "Executor MCP server"),
        ]:
            check = sandbox_ssh(
                ssh_config_path,
                sandbox_name,
                f"curl -s --max-time 5 -o /dev/null "
                f"-w '%{{http_code}}' "
                f"http://host.docker.internal:{port}/ "
                f"2>&1 || echo 'FAIL'",
                timeout=15,
            )
            status = check.stdout.strip()
            print(f"  {name} (host.docker.internal:{port}): {status}")
            if check.stderr.strip():
                print(
                    f"    stderr: {check.stderr.strip()}",
                    file=sys.stderr,
                )
        sys.stdout.flush()

        # 8. Run triage agent inside sandbox
        print(f"Running: {prompt}")
        sys.stdout.flush()

        env_vars = (
            f"export PATH={SANDBOX_WORKSPACE}/bin:$PATH && "
            f"export CLAUDE_CONFIG_DIR={SANDBOX_CLAUDE_CONFIG} && "
            f"export REPO='{repo}' && "
            f"export ISSUE_NUMBER='{issue_number}' && "
            f"{vertex_exports}"
        )
        process = subprocess.Popen(
            [  # nosec B607
                "ssh",
                "-F",
                ssh_config_path,
                f"openshell-{sandbox_name}",
                f"cd {SANDBOX_WORKSPACE} && {env_vars} "
                f"claude --print --agent triage "
                f"--mcp-config '{sandbox_mcp_config}' "
                f"--strict-mcp-config "
                f"--dangerously-skip-permissions "
                f"'{prompt}'",
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        process.wait(timeout=600)

        if process.returncode != 0:
            print(
                f"\nAgent exited with code {process.returncode}",
                file=sys.stderr,
            )
            sys.exit(process.returncode)
    finally:
        cleanup()
