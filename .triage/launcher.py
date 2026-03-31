#!/usr/bin/env python3
"""
Launcher for the scoped-tools agent experiment.

This script:
1. Gets a GitHub token (from --token flag, gh CLI, or GitHub App auth)
2. Starts the MCP server with the token (agent never sees it)
3. Starts a subagent executor HTTP server on the host
4. Creates an OpenShell sandbox for the triage agent
5. Launches the triage agent inside the sandbox

The subagent executor handles sandbox lifecycle for subagents on the host,
where OpenShell gateway auth is available. The triage agent calls it via
HTTP from inside its own sandbox, so every agent runs sandboxed without
requiring nested sandbox creation.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

MCP_PORT = 8081
EXECUTOR_PORT = 8082
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


# --- OpenShell sandbox helpers ---


def create_sandbox(name: str) -> None:
    """Create a persistent OpenShell sandbox and wait for it to be ready."""
    result = subprocess.run(
        ["timeout", "60", "openshell", "sandbox", "create",
         "--name", name, "--keep", "--no-auto-providers", "--no-tty"],
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=65,
    )
    # timeout exits 124 — sandbox create may exit non-zero after the
    # interactive shell is killed. Check if the sandbox actually exists.
    if result.returncode not in (0, 124):
        check = subprocess.run(
            ["openshell", "sandbox", "get", name],
            capture_output=True, timeout=10,
        )
        if check.returncode != 0:
            raise RuntimeError(f"Sandbox create failed: {result.stderr.decode()}")

    # Wait for sandbox to be fully ready (image pull can take a while)
    for _ in range(30):
        check = subprocess.run(
            ["openshell", "sandbox", "get", name],
            capture_output=True, text=True, timeout=10,
        )
        if check.returncode == 0 and "Ready" in check.stdout:
            return
        time.sleep(2)
    raise RuntimeError(f"Sandbox '{name}' not ready after 60s")


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
    raise RuntimeError("Policy set failed after 3 attempts")


def get_ssh_config(sandbox_name: str) -> str:
    """Get SSH config for a sandbox, return as string."""
    result = subprocess.run(
        ["openshell", "sandbox", "ssh-config", sandbox_name],
        capture_output=True, text=True, timeout=10, check=True,
    )
    return result.stdout


def sandbox_scp(ssh_config_path: str, sandbox_name: str, local: str, remote: str) -> None:
    """Copy a file or directory into a sandbox."""
    subprocess.run(
        ["scp", "-F", ssh_config_path, "-r", str(local),
         f"openshell-{sandbox_name}:{remote}"],
        check=True, timeout=60,
    )


def sandbox_ssh(ssh_config_path: str, sandbox_name: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command inside a sandbox."""
    return subprocess.run(
        ["ssh", "-F", ssh_config_path, f"openshell-{sandbox_name}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


LOG_DIR = "/tmp/triage-logs"


def extract_transcripts(
    ssh_config_path: str, sandbox_name: str, agent_name: str,
) -> None:
    """Copy Claude transcript files out of a sandbox before it's deleted."""
    os.makedirs(LOG_DIR, exist_ok=True)

    # Find transcript files (Claude stores them in ~/.claude/projects/)
    result = sandbox_ssh(
        ssh_config_path, sandbox_name,
        "find /home -name '*.jsonl' -path '*/.claude/*' 2>/dev/null || true",
        timeout=10,
    )
    files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    if not files:
        print(f"  [{agent_name}] No transcripts found")
        return

    for remote_path in files:
        local_name = f"{agent_name}-{os.path.basename(remote_path)}"
        local_path = os.path.join(LOG_DIR, local_name)
        try:
            subprocess.run(
                ["scp", "-F", ssh_config_path, "-r",
                 f"openshell-{sandbox_name}:{remote_path}", local_path],
                check=True, timeout=30, capture_output=True,
            )
            print(f"  [{agent_name}] Saved transcript: {local_name}")
        except Exception as e:
            print(f"  [{agent_name}] Failed to copy transcript: {e}", file=sys.stderr)


def render_policy(
    template_path: Path, owner: str, repo_name: str, issue_number: int,
) -> str:
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


def delete_sandbox(name: str) -> None:
    """Delete a sandbox, ignoring errors."""
    subprocess.run(
        ["openshell", "sandbox", "delete", name],
        capture_output=True, timeout=10,
    )


def discover_agents(working_dir: Path) -> dict[str, str | None]:
    """Read agent definitions and return {name: policy_relative_path}."""
    agents = {}
    for agent_file in (working_dir / "agents").glob("*.md"):
        name = agent_file.stem
        with open(agent_file) as f:
            content = f.read()
        match = re.search(r'^sandbox:\s*(.+)$', content, re.MULTILINE)
        policy = match.group(1).strip() if match else None
        agents[name] = policy
    return agents


def get_vertex_env() -> dict[str, str]:
    """Collect Vertex AI environment variables from the host, if present."""
    vertex_vars = {}
    for key in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID",
                "CLOUD_ML_REGION"):
        val = os.environ.get(key)
        if val:
            vertex_vars[key] = val
    return vertex_vars


def get_vertex_creds_path() -> str | None:
    """Return the path to the GCP credentials file, if set."""
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and os.path.isfile(path):
        return path
    return None


SANDBOX_CREDS_PATH = f"{SANDBOX_WORKSPACE}/gcp_credentials.json"


def bootstrap_vertex_creds(
    ssh_config_path: str, sandbox_name: str,
) -> str:
    """Copy GCP credentials into the sandbox. Returns export commands."""
    scp = lambda local, remote: sandbox_scp(ssh_config_path, sandbox_name, local, remote)

    vertex_env = get_vertex_env()
    creds_path = get_vertex_creds_path()

    exports = ""
    for key, val in vertex_env.items():
        exports += f"export {key}='{val}' && "

    if creds_path:
        scp(creds_path, SANDBOX_CREDS_PATH)
        exports += f"export GOOGLE_APPLICATION_CREDENTIALS='{SANDBOX_CREDS_PATH}' && "

    return exports


def bootstrap_agent_sandbox(
    ssh_config_path: str,
    sandbox_name: str,
    working_dir: Path,
    mcp_config_path: str,
) -> None:
    """Copy claude binary, agent files, skills, and MCP config into a sandbox."""
    scp = lambda local, remote: sandbox_scp(ssh_config_path, sandbox_name, local, remote)
    ssh = lambda cmd: sandbox_ssh(ssh_config_path, sandbox_name, cmd)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude binary not found in PATH")

    # Create workspace structure
    ssh(f"mkdir -p {SANDBOX_WORKSPACE}/.claude/agents "
        f"{SANDBOX_WORKSPACE}/.claude/skills "
        f"{SANDBOX_WORKSPACE}/bin")

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


# --- Subagent executor ---


class SubagentExecutor:
    """
    Runs subagents in sandboxed environments on the host.

    The triage agent (inside its own sandbox) calls this executor via HTTP.
    The executor creates a sandbox for the requested subagent, runs it,
    and returns the output. This avoids nested sandbox creation issues
    (gateway auth not available from inside a sandbox).
    """

    def __init__(self, working_dir: Path, mcp_config_path: str,
                 owner: str, repo_name: str, issue_number: int):
        self.working_dir = working_dir
        self.mcp_config_path = mcp_config_path
        self.owner = owner
        self.repo_name = repo_name
        self.issue_number = issue_number
        self.agents = discover_agents(working_dir)

    def run_agent(self, agent_name: str, prompt: str) -> tuple[int, str]:
        """Run an agent in a fresh sandbox. Returns (exit_code, output)."""
        if agent_name not in self.agents:
            return 1, f"Unknown agent: {agent_name}"
        if agent_name == "triage":
            return 1, "Cannot run triage agent as a subagent"

        policy_rel = self.agents[agent_name]
        if not policy_rel:
            return 1, f"No sandbox policy defined for agent '{agent_name}'"

        policy_template = self.working_dir / policy_rel
        if not policy_template.exists():
            return 1, f"Policy template not found: {policy_template}"

        sandbox_name = f"sub-{agent_name}-{os.getpid()}-{int(time.time())}"
        ssh_config_path = f"/tmp/openshell-ssh-{sandbox_name}.config"
        policy_path = None

        try:
            # 1. Render policy
            policy_path = render_policy(
                policy_template, self.owner, self.repo_name, self.issue_number,
            )

            # 2. Create sandbox
            print(f"[executor] Creating sandbox for '{agent_name}'...", file=sys.stderr)
            create_sandbox(sandbox_name)

            # 3. Apply policy
            print(f"[executor] Applying policy for '{agent_name}'...", file=sys.stderr)
            apply_policy(sandbox_name, policy_path)

            # 4. Get SSH config
            ssh_config = get_ssh_config(sandbox_name)
            with open(ssh_config_path, "w") as f:
                f.write(ssh_config)

            # 5. Bootstrap sandbox
            print(f"[executor] Bootstrapping '{agent_name}'...", file=sys.stderr)
            bootstrap_agent_sandbox(
                ssh_config_path, sandbox_name,
                self.working_dir, self.mcp_config_path,
            )

            # 5b. Copy Vertex AI credentials
            vertex_exports = bootstrap_vertex_creds(ssh_config_path, sandbox_name)

            # 6. Run agent
            print(f"[executor] Running '{agent_name}'...", file=sys.stderr)
            mcp_config = f"{SANDBOX_WORKSPACE}/mcp_config.json"
            result = subprocess.run(
                ["ssh", "-F", ssh_config_path, f"openshell-{sandbox_name}",
                 f"cd {SANDBOX_WORKSPACE} && "
                 f"export PATH={SANDBOX_WORKSPACE}/bin:$PATH && "
                 f"{vertex_exports}"
                 f"claude --print --agent '{agent_name}' "
                 f"--mcp-config '{mcp_config}' "
                 f"--strict-mcp-config --dangerously-skip-permissions "
                 f"'{prompt}'"],
                capture_output=True, text=True, timeout=300,
            )
            print(f"[executor] '{agent_name}' exited with code {result.returncode}", file=sys.stderr)
            return result.returncode, result.stdout

        except Exception as e:
            return 1, f"Error running '{agent_name}': {e}"
        finally:
            # Extract transcripts before deleting the sandbox
            if os.path.exists(ssh_config_path):
                try:
                    extract_transcripts(ssh_config_path, sandbox_name, agent_name)
                except Exception as e:
                    print(f"[executor] Failed to extract transcripts for '{agent_name}': {e}", file=sys.stderr)
            delete_sandbox(sandbox_name)
            if policy_path and os.path.exists(policy_path):
                os.unlink(policy_path)
            if os.path.exists(ssh_config_path):
                os.unlink(ssh_config_path)


def make_executor_handler(executor: SubagentExecutor) -> type:
    """Create an HTTP handler for the subagent executor."""

    class ExecutorHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            # URL format: /run/<agent-name>
            path_match = re.match(r'^/run/([a-zA-Z0-9_-]+)$', self.path)
            if not path_match:
                self.send_error(404, "Use POST /run/<agent-name>")
                return

            agent_name = path_match.group(1)
            content_length = int(self.headers.get("Content-Length", 0))
            prompt = self.rfile.read(content_length).decode("utf-8")

            if not prompt:
                self.send_error(400, "Prompt is required in request body")
                return

            exit_code, output = executor.run_agent(agent_name, prompt)

            response = json.dumps({
                "exit_code": exit_code,
                "output": output,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format, *args):
            print(f"[executor] {args[0]}", file=sys.stderr)

    return ExecutorHandler


def start_executor(executor: SubagentExecutor) -> HTTPServer:
    """Start the subagent executor HTTP server in a background thread."""
    handler = make_executor_handler(executor)
    server = HTTPServer(("0.0.0.0", EXECUTOR_PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[executor] Listening on http://0.0.0.0:{EXECUTOR_PORT}/", file=sys.stderr)
    return server


# --- Triage sandbox bootstrap ---


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


# --- Main launch ---


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