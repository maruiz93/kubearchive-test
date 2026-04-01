"""Agent runner: runs any agent in a sandboxed environment on the host.

Handles the full sandbox lifecycle: create, apply policy, bootstrap
(copy claude binary, agent/skill definitions, MCP config, credentials),
run the agent via SSH, extract transcripts, and clean up.

Used exclusively by the agent runner MCP server, which is the single
entry point for all agent execution (both triage and subagents).
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from sandbox import (
    apply_policy,
    create_sandbox,
    delete_sandbox,
    extract_transcripts,
    get_ssh_config,
    render_policy,
    sandbox_scp,
    sandbox_ssh,
)

# Constants shared with the launcher package
SANDBOX_WORKSPACE = "/tmp/workspace"  # nosec B108
SANDBOX_CLAUDE_CONFIG = "/tmp/claude-config"  # nosec B108
SANDBOX_CREDS_PATH = f"{SANDBOX_WORKSPACE}/gcp_credentials.json"


def discover_agents(working_dir: Path) -> dict[str, str | None]:
    """Read agent definitions and return {name: policy_relative_path}."""
    agents = {}
    for agent_file in (working_dir / "agents").glob("*.md"):
        name = agent_file.stem
        with open(agent_file) as f:
            content = f.read()
        match = re.search(r"^sandbox:\s*(.+)$", content, re.MULTILINE)
        policy = match.group(1).strip() if match else None
        agents[name] = policy
    return agents


def _get_vertex_env() -> dict[str, str]:
    """Collect Vertex AI environment variables from the host, if present."""
    vertex_vars = {}
    for key in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION"):
        val = os.environ.get(key)
        if val:
            vertex_vars[key] = val
    return vertex_vars


def _get_vertex_creds_path() -> str | None:
    """Return the path to the GCP credentials file, if set."""
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and os.path.isfile(path):
        return path
    return None


def _bootstrap_vertex_creds(ssh_config_path: str, sandbox_name: str) -> str:
    """Copy GCP credentials into the sandbox. Returns export commands."""
    vertex_env = _get_vertex_env()
    creds_path = _get_vertex_creds_path()

    exports = ""
    for key, val in vertex_env.items():
        exports += f"export {key}='{val}' && "

    if creds_path:
        sandbox_scp(ssh_config_path, sandbox_name, creds_path, SANDBOX_CREDS_PATH)
        exports += f"export GOOGLE_APPLICATION_CREDENTIALS='{SANDBOX_CREDS_PATH}' && "

    return exports


def _bootstrap_sandbox(
    ssh_config_path: str,
    sandbox_name: str,
    working_dir: Path,
    mcp_config_path: str,
) -> None:
    """Copy claude binary, agent files, skills, and MCP config into a sandbox."""

    def scp(local, remote):
        sandbox_scp(ssh_config_path, sandbox_name, local, remote)

    def ssh(cmd):
        return sandbox_ssh(ssh_config_path, sandbox_name, cmd)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude binary not found in PATH")

    gh_bin = shutil.which("gh")
    if not gh_bin:
        raise RuntimeError("gh CLI not found in PATH")

    # Create workspace structure
    ssh(
        f"mkdir -p {SANDBOX_WORKSPACE}/.claude/agents "
        f"{SANDBOX_WORKSPACE}/.claude/skills "
        f"{SANDBOX_WORKSPACE}/bin"
    )

    # Copy claude and gh binaries
    scp(claude_bin, f"{SANDBOX_WORKSPACE}/bin/claude")
    scp(gh_bin, f"{SANDBOX_WORKSPACE}/bin/gh")
    ssh(f"chmod +x {SANDBOX_WORKSPACE}/bin/claude {SANDBOX_WORKSPACE}/bin/gh")

    # Copy agent definitions
    for agent_file in (working_dir / "agents").glob("*.md"):
        scp(str(agent_file), f"{SANDBOX_WORKSPACE}/.claude/agents/")

    # Copy skills
    for skill_dir in (working_dir / "skills").iterdir():
        if skill_dir.is_dir():
            scp(str(skill_dir), f"{SANDBOX_WORKSPACE}/.claude/skills/")

    # Copy MCP config
    scp(mcp_config_path, f"{SANDBOX_WORKSPACE}/mcp_config.json")


class AgentRunner:
    """Runs agents in sandboxed environments on the host."""

    def __init__(
        self,
        working_dir: Path,
        mcp_config_path: str,
        owner: str,
        repo_name: str,
        issue_number: int,
    ):
        self.working_dir = working_dir
        self.mcp_config_path = mcp_config_path
        self.owner = owner
        self.repo_name = repo_name
        self.issue_number = issue_number
        self.agents = discover_agents(working_dir)

    def run_agent(
        self,
        agent_name: str,
        prompt: str,
        *,
        stream: bool = False,
    ) -> tuple[int, str]:
        """Run an agent in a fresh sandbox. Returns (exit_code, output).

        If stream=True, stdout/stderr are forwarded to the parent
        process in real time (used for the top-level triage agent).
        """
        if agent_name not in self.agents:
            return 1, f"Unknown agent: {agent_name}"

        policy_rel = self.agents[agent_name]
        if not policy_rel:
            return 1, f"No sandbox policy defined for agent '{agent_name}'"

        policy_template = self.working_dir / policy_rel
        if not policy_template.exists():
            return 1, f"Policy template not found: {policy_template}"

        sandbox_name = f"agent-{agent_name}-{os.getpid()}-{int(time.time())}"
        ssh_config_path = (  # nosec B108
            f"/tmp/openshell-ssh-{sandbox_name}.config"
        )
        policy_path = None

        try:
            # 1. Render policy
            policy_path = render_policy(
                policy_template,
                self.owner,
                self.repo_name,
                self.issue_number,
            )

            # 2. Create sandbox
            print(
                f"[runner] Creating sandbox for '{agent_name}'...",
                file=sys.stderr,
            )
            create_sandbox(sandbox_name)

            # 3. Apply policy
            print(
                f"[runner] Applying policy for '{agent_name}'...",
                file=sys.stderr,
            )
            apply_policy(sandbox_name, policy_path)

            # 4. Get SSH config
            ssh_config = get_ssh_config(sandbox_name)
            with open(ssh_config_path, "w") as f:
                f.write(ssh_config)

            # 5. Bootstrap sandbox
            print(
                f"[runner] Bootstrapping '{agent_name}'...",
                file=sys.stderr,
            )
            _bootstrap_sandbox(
                ssh_config_path,
                sandbox_name,
                self.working_dir,
                self.mcp_config_path,
            )

            # 5b. Copy Vertex AI credentials
            vertex_exports = _bootstrap_vertex_creds(ssh_config_path, sandbox_name)

            # 5c. Verify connectivity
            print(
                f"[runner] Verifying connectivity for '{agent_name}'...",
                file=sys.stderr,
            )
            check = sandbox_ssh(
                ssh_config_path,
                sandbox_name,
                "curl -s --max-time 5 -o /dev/null "
                "-w '%{http_code}' "
                "http://host.docker.internal:8081/ "
                "2>&1 || echo 'FAIL'",
                timeout=15,
            )
            print(
                f"[runner] MCP connectivity: {check.stdout.strip()}",
                file=sys.stderr,
            )

            # 6. Run agent
            print(
                f"[runner] Running '{agent_name}'...",
                file=sys.stderr,
            )
            mcp_config = f"{SANDBOX_WORKSPACE}/mcp_config.json"
            ssh_cmd = [  # nosec B607
                "ssh",
                "-F",
                ssh_config_path,
                f"openshell-{sandbox_name}",
                f"cd {SANDBOX_WORKSPACE} && "
                f"export PATH={SANDBOX_WORKSPACE}/bin:$PATH && "
                f"export CLAUDE_CONFIG_DIR={SANDBOX_CLAUDE_CONFIG} && "
                f"export REPO='{self.owner}/{self.repo_name}' && "
                f"export ISSUE_NUMBER='{self.issue_number}' && "
                f"export MCP_TIMEOUT=300000 && "
                f"{vertex_exports}"
                f"claude --print --agent '{agent_name}' "
                f"--mcp-config '{mcp_config}' "
                f"--strict-mcp-config "
                f"--dangerously-skip-permissions "
                f"'{prompt}'",
            ]

            if stream:
                # Stream to stderr so output reaches the terminal even
                # when running inside the MCP server (whose stdout is
                # DEVNULL but stderr is connected to the parent).
                process = subprocess.Popen(
                    ssh_cmd,
                    stdout=sys.stderr,
                    stderr=sys.stderr,
                )
                process.wait(timeout=600)
                return process.returncode, ""
            else:
                result = subprocess.run(
                    ssh_cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                print(
                    f"[runner] '{agent_name}' exited with code {result.returncode}",
                    file=sys.stderr,
                )
                return result.returncode, result.stdout

        except Exception as e:
            return 1, f"Error running '{agent_name}': {e}"
        finally:
            # Extract transcripts before deleting the sandbox
            if os.path.exists(ssh_config_path):
                try:
                    extract_transcripts(ssh_config_path, sandbox_name, agent_name)
                except Exception as e:
                    print(
                        f"[runner] Failed to extract transcripts for '{agent_name}': {e}",
                        file=sys.stderr,
                    )
            delete_sandbox(sandbox_name)
            if policy_path and os.path.exists(policy_path):
                os.unlink(policy_path)
            if os.path.exists(ssh_config_path):
                os.unlink(ssh_config_path)
