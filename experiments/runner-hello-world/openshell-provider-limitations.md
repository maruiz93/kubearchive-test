# OpenShell Provider Credentials: Limitations for Sandbox Environment Variables

## How OpenShell provider credentials work

When you create a provider and attach it to a sandbox:

```bash
openshell provider create --name my-provider --type generic \
  --credential API_KEY=sk-secret-value

openshell sandbox create --provider my-provider
```

The credential value is **never exposed as a plaintext environment variable** inside the sandbox. Instead, OpenShell uses a placeholder-based security architecture:

1. The real credential value is stored on the gateway and held only by the sandbox supervisor
2. Child processes see a **placeholder**: `API_KEY=openshell:resolve:env:API_KEY`
3. When the process makes an HTTP request that includes the placeholder in a **header**, **query parameter**, or **URL path**, the sandbox proxy intercepts it and swaps the placeholder for the real value before forwarding upstream
4. The real secret never enters the agent's address space

This was an intentional security decision ([NVIDIA/OpenShell#112](https://github.com/NVIDIA/OpenShell/issues/112)) to prevent prompt injection and malicious skills from exfiltrating credentials via `process.env`.

## What this covers

The proxy-based resolution works for credentials that travel through HTTP requests:

| Location | Example | Resolved? |
|----------|---------|-----------|
| HTTP header | `Authorization: Bearer openshell:resolve:env:API_KEY` | Yes |
| Query parameter | `?key=openshell:resolve:env:API_KEY` | Yes |
| URL path | `/bot<placeholder>/sendMessage` | Yes |
| Basic auth | `https://user:openshell:resolve:env:TOKEN@host/` | Yes |

This covers most API key authentication patterns: `x-api-key` headers, Bearer tokens, query-string API keys, and similar.

## What this does NOT cover

The placeholder mechanism fails for any value that is **read directly by application code** via `os.Getenv()` or used as a **local configuration value** rather than an HTTP credential. The process reads the literal placeholder string, which is meaningless to the application.

### Claude Code with Vertex AI

Claude Code via Vertex AI is a concrete example where provider credentials cannot work. Claude Code requires several environment variables that it reads directly at startup:

| Variable | Purpose | Why placeholders fail |
|----------|---------|----------------------|
| `CLAUDE_CODE_USE_VERTEX` | Set to `1` to enable Vertex AI mode | Claude Code checks `os.Getenv("CLAUDE_CODE_USE_VERTEX") == "1"` at startup. The placeholder `openshell:resolve:env:CLAUDE_CODE_USE_VERTEX` is not `"1"`, so Vertex mode is never activated. |
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID | Passed to the Vertex AI SDK which builds the API endpoint URL locally. The SDK reads the env var directly; no HTTP request carries this value as a header. |
| `CLOUD_ML_REGION` | GCP region (e.g. `us-east5`) | Same as above — the SDK reads it to construct the regional endpoint URL. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to a GCP service account JSON file | The GCP client library reads this env var to locate a **local file** on disk. The placeholder string is not a valid file path. |

The result: Claude Code sees `CLAUDE_CODE_USE_VERTEX=openshell:resolve:env:CLAUDE_CODE_USE_VERTEX`, does not recognize it as `"1"`, and reports **"Not logged in"**.

### Configuration values in general

Beyond Claude Code, any non-secret configuration value has the same problem:

- **Feature flags** (`ENABLE_FEATURE=true`) — application checks for a boolean, gets a placeholder string
- **File paths** (`CONFIG_PATH=/etc/app/config.yaml`) — application tries to open a file at the placeholder path
- **Endpoint URLs** (`DATABASE_URL=postgres://...`) — client libraries parse the URL locally, no HTTP proxy involved
- **Identifiers** (`REPO_NAME=org/repo`, `WORKSPACE_ID=abc123`) — application logic uses these directly
- **Numeric values** (`TIMEOUT_SECONDS=30`, `MAX_RETRIES=3`) — parsed as integers, placeholder string causes parse errors

### The `--config` flag is not a workaround

OpenShell's `openshell provider create` accepts `--config` in addition to `--credential`. However, `--config` values are **not injected as environment variables** at all. They are only used internally for inference route configuration (e.g., setting `OPENAI_BASE_URL` for proxy routing). A test in the OpenShell codebase explicitly asserts that config keys do not appear in the sandbox environment.

### `SandboxSpec.environment` exists but is not exposed

The OpenShell protobuf definition includes a `SandboxSpec.environment` field for plain (non-placeholder) environment variables at the Kubernetes pod level. However, this field is **not exposed via the CLI** — there is no `--env` flag on `openshell sandbox create`. No open or closed issue tracks adding this capability.

## Current workaround

For non-secret configuration values, the only working approach is to write a shell script (`.env` file) with `export` statements, copy it into the sandbox via `scp`, and `source` it before running commands:

```bash
# On the host, generate .env
echo 'export CLAUDE_CODE_USE_VERTEX=1' > /tmp/env.sh
echo 'export ANTHROPIC_VERTEX_PROJECT_ID=my-project' >> /tmp/env.sh

# Copy into sandbox
scp -F ssh.config /tmp/env.sh openshell-sandbox:/tmp/workspace/.env

# Source before running the agent
ssh -F ssh.config openshell-sandbox \
  "source /tmp/workspace/.env && claude --print ..."
```

This bypasses the provider system entirely and injects the actual values into the shell environment.

## Related OpenShell issues

- [NVIDIA/OpenShell#112](https://github.com/NVIDIA/OpenShell/issues/112) — **refactor: move secret values out of sandbox environment into supervisor-managed placeholders** (closed). The issue that introduced the placeholder architecture. Deliberately removed plaintext credentials from `process.env`.

- [NVIDIA/OpenShell#538](https://github.com/NVIDIA/OpenShell/issues/538) — **feat: L7 credential injection for non-inference providers** (closed). Extended proxy-based credential injection to arbitrary REST APIs beyond inference endpoints. Reinforces the design direction: credentials belong at the proxy layer, not in env vars.

- [NVIDIA/OpenShell#147](https://github.com/NVIDIA/OpenShell/issues/147) — **refactor(providers): introduce provider properties, sandbox hooks, and credential encryption** (closed). Proposed sandbox pre-spawn hooks that could write config files and set env vars. Mentions decoupling provider properties from env var names, but the plain env var injection gap remains.

- [NVIDIA/OpenShell#790](https://github.com/NVIDIA/OpenShell/issues/790) — **bug: git clone fails inside sandbox — missing GIT_SSL_CAINFO environment variable** (open). An example of a non-secret env var that needs to be set in the sandbox for tools to work correctly. Currently proposed as a fix to the sandbox image, not the provider system.

- [NVIDIA/OpenShell#785](https://github.com/NVIDIA/OpenShell/issues/785) — **Feature Request: Support Custom K3s/containerd Configuration, Proxy/Mirror Settings, Volume Mounts, and Environment Variables for Gateway Container** (open). Requests `--env` support for the gateway container, not the sandbox. The closest existing request to env var passthrough, but for a different scope.
