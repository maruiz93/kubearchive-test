# How to run the experiment

## Requirements

### Local (to run the experiment)

- **Go toolchain** (1.23+)
- **gh CLI** authenticated with access to the fullsend fork and the test repo
- **podman** (for building and pushing the container image)
- **rsync**
- A local clone of the test repo (see below)

### Test repo

The default test repo is `maruiz93/kubearchive-test`, but any GitHub repository can be used. The test repo needs:

**GitHub secrets** configured (see [Setting up GCP secrets](#setting-up-gcp-secrets) below) and a **GitHub release** on your fullsend fork (used to distribute the binary to the runner).

### Setting up GCP secrets

The experiment uses Claude Code via Vertex AI, which requires a GCP project with the Vertex AI API enabled and a service account key.

1. **Create or select a GCP project** with the [Vertex AI API](https://console.cloud.google.com/apis/library/aiplatform.googleapis.com) enabled.

2. **Create a service account** with the `Vertex AI User` role:
   ```bash
   gcloud iam service-accounts create fullsend-runner \
     --display-name="Fullsend Runner" \
     --project=YOUR_PROJECT_ID

   gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
     --member="serviceAccount:fullsend-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
     --role="roles/aiplatform.user"
   ```

3. **Create and download a JSON key**:
   ```bash
   gcloud iam service-accounts keys create /tmp/sa-key.json \
     --iam-account=fullsend-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com
   ```

4. **Set the secrets on your test repo**:
   ```bash
   gh secret set GCP_SA_KEY --repo your-user/your-repo < /tmp/sa-key.json
   gh secret set GCP_PROJECT --repo your-user/your-repo --body "YOUR_PROJECT_ID"
   gh secret set GCP_REGION --repo your-user/your-repo --body "us-east5"
   ```

5. **Delete the local key file** (it's now stored as a GitHub secret):
   ```bash
   rm /tmp/sa-key.json
   ```

Available regions for Claude on Vertex AI include `us-east5`, `europe-west1`, and `asia-southeast1`. Check the [Vertex AI documentation](https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude#regions) for the latest list.

### GitHub Actions runner

The workflow installs these automatically:

- **fullsend** binary (from a GitHub release)
- **OpenShell** CLI

Claude Code and experiment tool binaries are pre-installed in the container image (`quay.io/manonru/fullsend-exp`), which the sandbox is created from via `--from`.

## Quick start (using defaults)

```bash
# Clone the test repo (one-time setup)
git clone git@github.com:maruiz93/kubearchive-test.git /tmp/kubearchive-test

# Run the experiment (builds image, pushes to quay.io, syncs, triggers workflow)
./experiments/runner-hello-world/run-experiment.sh
```

The script will print the workflow run URL. You can watch it with:

```bash
gh run watch <RUN_ID> --repo maruiz93/kubearchive-test
```

## Using a different test repo

To run against your own repo, edit the variables at the top of `run-experiment.sh`:

```bash
TEST_REPO="/tmp/your-repo"              # Local clone path
RELEASE_REPO="your-user/fullsend"       # Where to upload the fullsend binary
RELEASE_TAG="runner-hello-world-dev"     # Release tag name
WORKFLOW_REPO="your-user/your-repo"     # Where to trigger the workflow
WORKFLOW_FILE="hello-world.yml"         # Workflow file name
IMAGE_REPO="quay.io/your-user/your-image" # Container image registry
```

Then update the workflow file (`workflow/hello-world.yml`) to point the fullsend install step at your release:

```yaml
- name: Install fullsend
  run: |
    curl -LsSf https://github.com/your-user/fullsend/releases/download/runner-hello-world-dev/fullsend_dev_linux_amd64.tar.gz -o /tmp/fullsend.tar.gz
    sudo tar xzf /tmp/fullsend.tar.gz -C /usr/local/bin/
```

Steps:

1. Create a GitHub release on your fullsend fork: `gh release create runner-hello-world-dev --repo your-user/fullsend --title "Dev" --notes "Dev build"`
2. Set the required secrets on your test repo (see [Setting up GCP secrets](#setting-up-gcp-secrets))
3. Clone your test repo locally: `git clone git@github.com:your-user/your-repo.git /tmp/your-repo`
4. Run `./experiments/runner-hello-world/run-experiment.sh`

## Harness env model

- **Sandbox env vars** are delivered as env files under `env/`, copied into the sandbox via `host_files` with `expand: true`. The `${VAR}` references in these files are expanded from the host environment before copying. The sandbox's `.env` file sources all files from `.env.d/` at startup.
- **`runner_env:`** declares host-only vars available to validation scripts but NOT copied to the sandbox (e.g. `VALIDATION_EXPECTED_FAILURES`).
- **`host_files:`** copies files from the host into the sandbox at specified paths. `src` may use `${VAR}` expansion (e.g. `${GOOGLE_APPLICATION_CREDENTIALS}`). When `expand: true`, the file content is also expanded before copying.
- **Providers** (`providers/`) are reserved for credentials that work through OpenShell's HTTP proxy credential resolution (headers, query params, URL paths). See `openshell-provider-limitations.md` for details on when providers work and when they don't.
- All `${VAR}` references in `runner_env` and `host_files` are validated at startup -- the runner fails fast if any referenced host variable is unset.
