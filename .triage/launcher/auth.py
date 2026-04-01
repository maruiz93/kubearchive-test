"""GitHub token acquisition and Vertex AI credential helpers."""

import os
import subprocess
import sys
import time

from . import SANDBOX_CREDS_PATH
from .sandbox import sandbox_scp


def get_token_from_gh_cli() -> str:
    """Get token from gh CLI auth."""
    result = subprocess.run(
        ["gh", "auth", "token"],  # nosec B607
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(
            "Error: could not get token from gh CLI. "
            "Use --token or authenticate with `gh auth login`.",
            file=sys.stderr,
        )
        sys.exit(1)
    return result.stdout.strip()


def get_token_from_github_app(
    pem_path: str, client_id: str, installation_id: int, repo_id: int | None = None
) -> str:
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


def get_vertex_env() -> dict[str, str]:
    """Collect Vertex AI environment variables from the host, if present."""
    vertex_vars = {}
    for key in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION"):
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


def bootstrap_vertex_creds(
    ssh_config_path: str,
    sandbox_name: str,
) -> str:
    """Copy GCP credentials into the sandbox. Returns export commands."""

    def scp(local, remote):
        sandbox_scp(ssh_config_path, sandbox_name, local, remote)

    vertex_env = get_vertex_env()
    creds_path = get_vertex_creds_path()

    exports = ""
    for key, val in vertex_env.items():
        exports += f"export {key}='{val}' && "

    if creds_path:
        scp(creds_path, SANDBOX_CREDS_PATH)
        exports += f"export GOOGLE_APPLICATION_CREDENTIALS='{SANDBOX_CREDS_PATH}' && "

    return exports
