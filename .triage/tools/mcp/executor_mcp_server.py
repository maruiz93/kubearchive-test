#!/usr/bin/env python3
"""
MCP server that exposes a run_agent tool for subagent execution.

The server manages sandbox lifecycle on the host — creating, bootstrapping,
running, and cleaning up OpenShell sandboxes for each subagent invocation.
The triage agent (inside its own sandbox) calls this as an MCP tool.

Usage:
  python3 executor_mcp_server.py --http --port 8082

Requires environment variables:
  EXECUTOR_WORKING_DIR: path to the experiment directory
  EXECUTOR_MCP_CONFIG: path to the MCP config file for subagents
  EXECUTOR_OWNER: GitHub org/owner
  EXECUTOR_REPO_NAME: GitHub repo name
  EXECUTOR_ISSUE_NUMBER: issue number being triaged
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Add the experiment root to sys.path so we can import the launcher package
sys.path.insert(0, os.environ.get("EXECUTOR_WORKING_DIR", "."))

from launcher.executor import SubagentExecutor  # noqa: E402

TOOLS = {
    "run_agent": {
        "description": (
            "Run a subagent in its own OpenShell sandbox. "
            "Returns the agent's output as text. "
            "Available agents: duplicate-detector, "
            "completeness-assessor, reproducibility-verifier."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Name of the agent to run "
                        "(e.g. duplicate-detector, "
                        "completeness-assessor, "
                        "reproducibility-verifier)"
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "The prompt to send to the agent, including repo and issue context"
                    ),
                },
            },
            "required": ["agent_name", "prompt"],
        },
    },
}


def handle_request(request: dict, executor: SubagentExecutor) -> dict:
    """Handle a single JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "fullsend-executor",
                    "version": "0.1.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        tools_list = [
            {
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            }
            for name, tool in TOOLS.items()
        ]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools_list},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name != "run_agent":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Unknown tool: {tool_name}",
                        }
                    ],
                    "isError": True,
                },
            }

        agent_name = arguments.get("agent_name", "")
        prompt = arguments.get("prompt", "")

        if not agent_name or not prompt:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "agent_name and prompt are required",
                        }
                    ],
                    "isError": True,
                },
            }

        exit_code, output = executor.run_agent(agent_name, prompt)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": output}],
                "isError": exit_code != 0,
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32601,
            "message": f"Unknown method: {method}",
        },
    }


def make_http_handler(executor: SubagentExecutor) -> type:
    """Create an HTTP handler with the executor bound."""

    class ExecutorMCPHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            try:
                request = json.loads(body)
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return

            response = handle_request(request, executor)

            if response is None:
                self.send_response(204)
                self.end_headers()
                return

            response_body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format, *args):
            print(f"[executor-mcp] {args[0]}", file=sys.stderr)

    return ExecutorMCPHandler


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="MCP server for subagent execution")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run as HTTP server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8082,
        help="HTTP port (default: 8082)",
    )
    args = parser.parse_args()

    working_dir = os.environ.get("EXECUTOR_WORKING_DIR")
    mcp_config = os.environ.get("EXECUTOR_MCP_CONFIG")
    owner = os.environ.get("EXECUTOR_OWNER")
    repo_name = os.environ.get("EXECUTOR_REPO_NAME")
    issue_number = os.environ.get("EXECUTOR_ISSUE_NUMBER")

    for name, val in [
        ("EXECUTOR_WORKING_DIR", working_dir),
        ("EXECUTOR_MCP_CONFIG", mcp_config),
        ("EXECUTOR_OWNER", owner),
        ("EXECUTOR_REPO_NAME", repo_name),
        ("EXECUTOR_ISSUE_NUMBER", issue_number),
    ]:
        if not val:
            print(f"Error: {name} not set", file=sys.stderr)
            sys.exit(1)

    executor = SubagentExecutor(
        working_dir=Path(working_dir),
        mcp_config_path=mcp_config,
        owner=owner,
        repo_name=repo_name,
        issue_number=int(issue_number),
    )

    if not args.http:
        print(
            "Error: only --http mode is supported",
            file=sys.stderr,
        )
        sys.exit(1)

    handler = make_http_handler(executor)
    server = HTTPServer(
        ("0.0.0.0", args.port),
        handler,  # nosec B104
    )
    print(
        f"Executor MCP server listening on http://0.0.0.0:{args.port}/",
        file=sys.stderr,
    )
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
