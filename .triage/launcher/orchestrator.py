"""Orchestrator: MCP server startup, triage sandbox bootstrap, and agent launch."""

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
from .executor import SubagentExecutor, start_executor
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


def bootstrap_triage_sandbox(
    ssh_config_path: str,
    sandbox_name: str,
    working_dir: Path,
    mcp_config_path: str,
) -> None:
    """Bootstrap the triage sandbox with everything it needs."""
    scp = lambda local, remote: sandbox_scp(ssh_config_path, sandbox_name, local, remote)
    ssh = lambda cmd: sandbox_ssh(ssh_config_path, sandbox_name, cmd)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude binary not found in PATH")

    # Create workspace structure
    ssh(f"mkdir -p {SANDBOX_WORKSPACE}/.claude/agents "
        f"{SANDBOX_WORKSPACE}/.claude/skills "
        f"{SANDBOX_WORKSPACE}/agents "
        f"{SANDBOX_WORKSPACE}/tools/scripts "
        f"{SANDBOX_WORKSPACE}/bin")

    # Copy claude binary
    scp(claude_bin, f"{SANDBOX_WORKSPACE}/bin/claude")
    ssh(f"chmod +x {SANDBOX_WORKSPACE}/bin/claude")

    # Copy agent definitions (agents/ for run-sandboxed.sh, .claude/agents/ for claude CLI)
    for agent_file in (working_dir / "agents").glob("*.md"):
        scp(str(agent_file), f"{SANDBOX_WORKSPACE}/agents/")
        scp(str(agent_file), f"{SANDBOX_WORKSPACE}/.claude/agents/")

    # Copy skills
    for skill_dir in (working_dir / "skills").iterdir():
        if skill_dir.is_dir():
            scp(str(skill_dir), f"{SANDBOX_WORKSPACE}/.claude/skills/")

    # Copy run-sandboxed.sh (triage agent calls this for subagents)
    scp(str(working_dir / "tools" / "scripts" / "run-sandboxed.sh"),
        f"{SANDBOX_WORKSPACE}/tools/scripts/")
    ssh(f"chmod +x {SANDBOX_WORKSPACE}/tools/scripts/run-sandboxed.sh")

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

    # 2. Start subagent executor (needs MCP config, written after host IP detection)
    mcp_config_file_path = None
    policy_path = None

    def cleanup():
        if hasattr(cleanup, 'executor_server'):
            cleanup.executor_server.shutdown()
        # Extract triage transcripts before deleting sandbox
        if os.path.exists(ssh_config_path):
            try:
                extract_transcripts(ssh_config_path, sandbox_name, "triage")
            except Exception:
                pass
        delete_sandbox(sandbox_name)
        for path in (mcp_config_file_path, policy_path, ssh_config_path):
            if path and os.path.exists(path):
                os.unlink(path)
        mcp_process.terminate()
        mcp_process.wait(timeout=5)

    try:
        # 3. Create triage sandbox (needed to detect host IP)
        print("Creating triage sandbox...")
        create_sandbox(sandbox_name)

        # 4. Get SSH config
        ssh_config = get_ssh_config(sandbox_name)
        with open(ssh_config_path, "w") as f:
            f.write(ssh_config)

        # 5. Render triage policy
        policy_path = render_policy(
            working_dir / "policies" / "triage-write.yaml",
            owner, repo_name, issue_number,
        )

        # 6. Apply triage policy
        print("Applying triage policy...")
        apply_policy(sandbox_name, policy_path)

        # 7. Write MCP config (host.docker.internal resolves to host in OpenShell)
        mcp_config = {
            "mcpServers": {
                "github-triage": {
                    "type": "http",
                    "url": f"http://host.docker.internal:{MCP_PORT}/",
                }
            }
        }
        mcp_config_tmp = tempfile.NamedTemporaryFile(
            mode="w", prefix="mcp_config_", suffix=".json", delete=False,
        )
        json.dump(mcp_config, mcp_config_tmp)
        mcp_config_tmp.close()
        mcp_config_file_path = mcp_config_tmp.name

        # 8. Start subagent executor
        executor = SubagentExecutor(
            working_dir, mcp_config_file_path,
            owner, repo_name, issue_number,
        )
        executor_server = start_executor(executor)
        cleanup.executor_server = executor_server

        # --- Log setup ---
        print(f"Triage: {repo}#{issue_number}")
        print(f"  MCP server:  http://host.docker.internal:{MCP_PORT}/ (pid {mcp_process.pid})")
        print(f"  Executor:    http://host.docker.internal:{EXECUTOR_PORT}/")
        print(f"  Sandbox:     {sandbox_name}")
        print(f"  Policy:      policies/triage-write.yaml")
        print("---")
        sys.stdout.flush()

        # 9. Bootstrap sandbox
        print("Bootstrapping triage sandbox...")
        bootstrap_triage_sandbox(
            ssh_config_path, sandbox_name, working_dir, mcp_config_file_path,
        )

        # 9b. Copy Vertex AI credentials
        vertex_exports = bootstrap_vertex_creds(ssh_config_path, sandbox_name)

        # 9c. Verify connectivity from sandbox to host services
        print("Verifying sandbox connectivity...")
        for port, name in [(MCP_PORT, "MCP server"), (EXECUTOR_PORT, "Executor")]:
            check = sandbox_ssh(
                ssh_config_path, sandbox_name,
                f"curl -s --max-time 5 -o /dev/null -w '%{{http_code}}' "
                f"http://host.docker.internal:{port}/ 2>&1 || echo 'FAIL'",
                timeout=15,
            )
            status = check.stdout.strip()
            print(f"  {name} (host.docker.internal:{port}): {status}")
            if check.stderr.strip():
                print(f"    stderr: {check.stderr.strip()}", file=sys.stderr)
        sys.stdout.flush()

        # 10. Run triage agent inside sandbox
        print(f"Running: {prompt}")
        sys.stdout.flush()

        env_vars = (
            f"export PATH={SANDBOX_WORKSPACE}/bin:$PATH && "
            f"export CLAUDE_CONFIG_DIR={SANDBOX_CLAUDE_CONFIG} && "
            f"export REPO='{repo}' && "
            f"export ISSUE_NUMBER='{issue_number}' && "
            f"export MCP_CONFIG_PATH='{sandbox_mcp_config}' && "
            f"export SANDBOX_EXECUTOR_URL='http://host.docker.internal:{EXECUTOR_PORT}' && "
            f"{vertex_exports}"
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