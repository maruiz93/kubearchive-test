# Claude Code + Vertex AI Authentication Flow

How Claude Code authenticates to Google Vertex AI when running inside an
OpenShell sandbox, from secret storage to API response.

## The full chain

```
┌─────────────────────────────────────────────────────────────────────┐
│ GitHub Actions CI                                                   │
│                                                                     │
│  ┌──────────────┐    google-github-actions/auth@v2                  │
│  │ GitHub Secret │──────────────────────────────┐                   │
│  │ GCP_SA_KEY   │  (raw JSON content)           │                   │
│  └──────────────┘                               ▼                   │
│                                        ┌──────────────────┐         │
│                                        │ gha-creds-*.json │         │
│                                        │ (file on disk)   │         │
│                                        └────────┬─────────┘         │
│                                                 │                   │
│                          GOOGLE_APPLICATION_CREDENTIALS              │
│                          = /path/to/gha-creds-*.json                │
│                                                 │                   │
│  ┌──────────────────────────────────────────────┼──────────────┐    │
│  │ fullsend runner                              │              │    │
│  │                                              ▼              │    │
│  │  1. Load providers/gcp-vertex.yaml                          │    │
│  │  2. openshell provider create --name gcp-vertex ...         │    │
│  │  3. openshell sandbox create --provider gcp-vertex          │    │
│  │  4. SCP gha-creds-*.json → sandbox (credential_files)      │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘

                              │
                              ▼

┌─────────────────────────────────────────────────────────────────────┐
│ OpenShell Gateway (k3s cluster)                                     │
│                                                                     │
│  Provider store:                                                    │
│    gcp-vertex:                                                      │
│      CLAUDE_CODE_USE_VERTEX = "1"                                   │
│      ANTHROPIC_VERTEX_PROJECT_ID = "my-project"                     │
│      CLOUD_ML_REGION = "us-central1"                                │
│      GOOGLE_APPLICATION_CREDENTIALS = "/tmp/workspace/...json"      │
│                                                                     │
│  On sandbox creation: injects env vars into sandbox supervisor      │
└─────────────────────────────────────────────────────────────────────┘

                              │
                              ▼

┌─────────────────────────────────────────────────────────────────────┐
│ OpenShell Sandbox                                                   │
│                                                                     │
│  Environment (injected by supervisor via provider):                 │
│    CLAUDE_CODE_USE_VERTEX=1                                         │
│    ANTHROPIC_VERTEX_PROJECT_ID=my-project                           │
│    CLOUD_ML_REGION=us-central1                                      │
│    GOOGLE_APPLICATION_CREDENTIALS=/tmp/workspace/.gcp-creds.json    │
│                                                                     │
│  File (copied via SCP):                                             │
│    /tmp/workspace/.gcp-credentials.json  ← service account key      │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Claude Code (node process)                                   │   │
│  │                                                              │   │
│  │  1. Sees CLAUDE_CODE_USE_VERTEX=1 → activates Vertex mode    │   │
│  │  2. Reads ANTHROPIC_VERTEX_PROJECT_ID + CLOUD_ML_REGION      │   │
│  │  3. Constructs endpoint URL:                                 │   │
│  │     https://{region}-aiplatform.googleapis.com/v1/            │   │
│  │       projects/{project}/locations/{region}/                  │   │
│  │       publishers/anthropic/models/{model}:streamRawPredict   │   │
│  │                                                              │   │
│  │  ┌──────────────────────────────────────────────────────┐    │   │
│  │  │ google-auth-library                                  │    │   │
│  │  │                                                      │    │   │
│  │  │  1. Reads GOOGLE_APPLICATION_CREDENTIALS             │    │   │
│  │  │  2. Loads service account JSON (private key + email)  │    │   │
│  │  │  3. Creates JWT:                                     │    │   │
│  │  │     {                                                │    │   │
│  │  │       "iss": "sa@project.iam.gserviceaccount.com",   │    │   │
│  │  │       "scope": "cloud-platform",                     │    │   │
│  │  │       "aud": "https://oauth2.googleapis.com/token",  │    │   │
│  │  │       "exp": now + 3600                              │    │   │
│  │  │     }                                                │    │   │
│  │  │  4. Signs JWT with private key (RS256)               │    │   │
│  │  │  5. POST to oauth2.googleapis.com/token              │    │   │
│  │  │     → receives access_token (1-hour TTL)             │    │   │
│  │  └──────────────┬───────────────────────────────────────┘    │   │
│  │                 │                                            │   │
│  │                 ▼                                            │   │
│  │  6. Sends request to Vertex AI:                              │   │
│  │     POST https://us-central1-aiplatform.googleapis.com/...   │   │
│  │     Authorization: Bearer ya29.c.abc123...  (access token)   │   │
│  │     Content-Type: application/json                           │   │
│  │     Body: { "anthropic_version": "...", "messages": [...] }  │   │
│  │                                                              │   │
│  └──────────────────────────────┬───────────────────────────────┘   │
│                                 │                                   │
│  ┌──────────────────────────────┼───────────────────────────────┐   │
│  │ Sandbox Proxy                │                               │   │
│  │                              ▼                               │   │
│  │  Network policy check:                                       │   │
│  │    ✓ *.googleapis.com:443 — allowed                          │   │
│  │    ✗ anything else — denied                                  │   │
│  │                                                              │   │
│  │  Forwards request to upstream                                │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘

                              │
                              ▼

┌─────────────────────────────────────────────────────────────────────┐
│ Google Cloud                                                        │
│                                                                     │
│  oauth2.googleapis.com                                              │
│    ← JWT signed with SA private key                                 │
│    → access_token (1hr TTL)                                         │
│                                                                     │
│  us-central1-aiplatform.googleapis.com                              │
│    ← access_token + prompt                                          │
│    → Claude response (streamed)                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## What lives where

| Asset | Location | Sensitivity |
|---|---|---|
| Service account JSON (private key) | GitHub Secret → CI disk → sandbox file | **High** — can generate unlimited tokens |
| OAuth2 access token | Generated inside sandbox, sent in HTTP header | **Medium** — expires in 1 hour |
| Project ID, region | Provider env vars in sandbox | **Low** — not secret |
| `CLAUDE_CODE_USE_VERTEX` | Provider env var in sandbox | **None** — just a flag |

## Security boundary

The sandbox network policy restricts egress to `*.googleapis.com:443` only.
Even though the service account private key is inside the sandbox, the agent
cannot exfiltrate it to any other destination. The proxy logs every
allow/deny decision for audit.

## Why OpenShell L7 egress policies can't fully isolate GCP credentials

OpenShell's provider placeholder model (`openshell:resolve:env:*`) works when
credentials are strings placed in known HTTP locations (headers, query params,
path segments) and auth is a single step. GCP Vertex AI breaks every assumption:

1. **Credential is a file, not a string.** The service account JSON contains a
   private key, email, and project ID. Providers inject env vars, not files.
   `GOOGLE_APPLICATION_CREDENTIALS` points to a file path that must exist on
   disk.

2. **Auth is multi-step, not single-hop.** `google-auth-library` (a Google npm
   package used internally by the Anthropic SDK's Vertex integration) reads the
   private key, signs a JWT locally, and POSTs to `oauth2.googleapis.com` to
   receive an access token. Claude Code and the Anthropic SDK never touch the
   private key directly — they just call `getRequestHeaders()` and get back a
   `Bearer` token. But `google-auth-library` needs the key file to do this
   work, and it runs inside the sandbox. The proxy sees two separate HTTPS
   connections (token exchange + API call) with no relationship between them.

3. **Cryptographic operation inside the sandbox.** JWT signing (RS256) is
   performed by `google-auth-library`, not by Claude Code or the Anthropic SDK.
   But since it runs in the same process inside the sandbox, the private key
   must be present in the sandbox filesystem. The proxy can't sign JWTs on
   behalf of the library — there is no hook point.

4. **Dynamic URL construction.** The Anthropic SDK builds
   `{region}-aiplatform.googleapis.com` from env vars before making any HTTP
   request. If `CLOUD_ML_REGION` were a placeholder, DNS resolution would fail.
   These values must be real strings in the process environment.

5. **Token refresh is automatic and internal.** `google-auth-library` caches
   and refreshes the access token when it nears expiry. This requires the
   private key to remain available. A pre-generated token passed through a
   provider would expire after 1 hour with no way to refresh.

6. **No access token injection point.** `google-auth-library` doesn't support
   `GOOGLE_OAUTH_ACCESS_TOKEN` or similar env var. You can't bypass the
   file-based auth without modifying Google's library.

### What L7 policies CAN do

- Restrict egress to `*.googleapis.com:443` (prevents exfiltration)
- Scope which binaries can make outbound connections
- Log every allow/deny decision for audit

### What L7 policies CANNOT do

- Keep the private key out of the sandbox
- Scope requests to specific Vertex AI models or projects at the proxy layer
- Replace the REST server tier for GCP-authenticated services

### Conclusion

OpenShell providers + L7 policies can fully replace the REST server tier for
services with static API key auth (GitHub, OpenAI, Anthropic direct). For
services with OAuth2/OIDC flows like GCP Vertex AI, the private key must be in
the sandbox and the security boundary is the network policy, not credential
isolation. Solving this would require OpenShell to act as an OAuth2 token
broker — generating and refreshing tokens outside the sandbox on behalf of the
agent.

## Possible improvements

**Short-term:** If `google-auth-library` supported accepting a pre-generated
access token via environment variable, the runner could generate the token on
the host and pass only the short-lived token (1hr) through the provider —
keeping the private key out of the sandbox entirely. This is not supported
today.

**Medium-term:** Workload Identity Federation eliminates service account keys
entirely by letting GitHub Actions authenticate to GCP using GitHub's OIDC
identity. This removes the stored secret from GitHub but does not change the
sandbox problem — the credential config file requires the GitHub OIDC token
endpoint (`ACTIONS_ID_TOKEN_REQUEST_URL`) which is only available in CI, not
inside the sandbox. Additionally, WIF couples the authentication to the CI
platform. The GCP Workload Identity Pool must be configured with an OIDC
provider for each platform (GitHub uses `token.actions.githubusercontent.com`,
GitLab uses `gitlab.com`, Tekton uses the cluster's service account issuer).
Moving to a different CI system or running locally requires reconfiguring the
GCP trust relationship each time.

**Long-term:** OpenShell could add an OAuth2 provider type that handles token
generation and refresh on the gateway side, injecting only short-lived access
tokens into the sandbox via the existing placeholder system. This would require
the gateway to manage service account keys and token lifecycles.