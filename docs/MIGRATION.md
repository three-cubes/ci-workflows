# Migration

How to migrate an existing repo to consume the shared workflow. The two worked examples are the ones we know intimately: `tc-agent-zone` (the agent platform) and `kairix` (the memory runtime).

## tc-agent-zone — before → after

### Before

The repo currently has `.github/workflows/deploy-on-merge.yml` — ~130 lines covering:

- Azure login via WIF (inlined)
- Snapshot loop (inlined)
- Apply on vm-openclaw (inlined)
- Apply on vm-hermes-poc (inlined)
- Smoke check (inlined)

Every change to the deploy pattern means editing this file in-place. Every NEW repo means copy-pasting this file.

### After

Replace `.github/workflows/deploy-on-merge.yml` with a ~25-line thin caller:

```yaml
name: "4 · Deploy on merge to main"

on:
  workflow_dispatch:
    inputs:
      scope:
        description: Scope to deploy (auto = infer from changed paths)
        required: true
        default: auto
        type: choice
        options: [auto, all, config, skills, cron, infra, agents, bootstrap, hermes]
      skip_snapshot:
        description: Skip snapshot-before-apply (dev only)
        required: false
        default: 'false'
        type: choice
        options: ['false', 'true']

permissions:
  contents: read
  id-token: write

jobs:
  deploy:
    uses: three-cubes/tc-pipelines/.github/workflows/azure-vm-deploy.yml@v1
    with:
      resource-group: RG-AGENTS-CORE
      op-tag: deploy-on-merge
      skip-snapshot: ${{ github.event.inputs.skip_snapshot }}
      azure-client-id: ${{ vars.AZURE_CLIENT_ID }}
      azure-tenant-id: ${{ vars.AZURE_TENANT_ID }}
      azure-subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
      targets: |
        - vm-name: vm-openclaw
          apply-script: |
            cd /data/development/tc-agent-zone && git pull --ff-only
            bash devsecops/apply/apply-openclaw-config.sh --no-snapshot
          smoke-units: 'openclaw-gateway cli-proxy-api caddy'
        - vm-name: vm-hermes-poc
          apply-script: |
            cd /data/development/tc-agent-zone && git pull --ff-only
            bash devsecops/apply/hermes/apply-config.sh --no-snapshot
          smoke-units: 'hermes-gateway-northcoast-retention.service'
```

The `runbook-ci-driven-apply.md` runbook collapses to a single `az deployment` command, since `infra/bicep/ci-deploy-identity.bicep` does what the manual `az identity create` + `az role assignment` + `az identity federated-credential` sequence used to do.

### Migration steps for tc-agent-zone

```bash
# 1. Run the Bicep module to (re-)provision the identity. Already provisioned
#    manually 2026-06-09 — this step is idempotent and will update tags +
#    role-assignment names but won't disrupt the live MI.
az deployment group create \
  --resource-group RG-AGENTS-CORE \
  --template-file https://raw.githubusercontent.com/three-cubes/tc-pipelines/v1/infra/bicep/ci-deploy-identity.bicep \
  --parameters repoOwner=three-cubes repoName=tc-agent-zone keyVaultName=kv-tc-agents \
               identityName=mi-github-deploy

# 2. Replace .github/workflows/deploy-on-merge.yml in tc-agent-zone with the
#    thin caller above (PR #N).

# 3. Smoke-test via workflow_dispatch:
gh workflow run "4 · Deploy on merge to main" \
  --field scope=auto --field skip_snapshot=true \
  --repo three-cubes/tc-agent-zone

# 4. After green: re-test with skip_snapshot=false to verify snapshot path.

# 5. Done. Future deploy-pattern improvements ship as platform-templates
#    bumps; tc-agent-zone consumes them automatically.
```

## kairix — before → after

### Before

`.github/workflows/release-vm-deploy.yml` (~250 lines) does a webhook-based deploy:

- Workflow signs a payload with HMAC-SHA256
- POSTs to an operator-configured webhook URL (passes through Cloudflare Access)
- Polls a commit status (`vm-reflib-regression`) every 15s up to 900s for success/failure

### After (target shape)

Two-phase change — bigger than tc-agent-zone's because kairix's current model is fundamentally different:

**Phase 1 — Side-by-side adoption.** Add a new workflow `azure-vm-deploy-poc.yml` that uses the shared workflow against a kairix non-production VM. Validate WIF-based push works for kairix's deploy shape. Keep the existing webhook flow as the production path.

**Phase 2 — Cutover.** Once Phase 1 has run reliably for ~2 weeks, retire the webhook + HMAC secret. Replace `release-vm-deploy.yml`'s deploy job with the shared workflow call. The Cloudflare Access dance + the HMAC secret rotation overhead goes away.

### Migration steps for kairix

```bash
# 1. Provision kairix's WIF identity. New identity, scoped to kairix's RG.
az deployment group create \
  --resource-group RG-KAIRIX-CORE \
  --template-file https://raw.githubusercontent.com/three-cubes/tc-pipelines/v1/infra/bicep/ci-deploy-identity.bicep \
  --parameters repoOwner=three-cubes repoName=kairix keyVaultName=kv-kairix

# 2. Populate GitHub variables on three-cubes/kairix from the deployment outputs.

# 3. Add .github/workflows/azure-vm-deploy-poc.yml with a workflow_dispatch
#    trigger calling the shared workflow against vm-kairix-staging (or
#    whatever the test VM is). Run 2-3 deploys via this path to validate.

# 4. Once Phase 1 is solid: open a PR that replaces release-vm-deploy.yml's
#    deploy job with the shared-workflow call. Remove the HMAC signing logic
#    + the Cloudflare Access env vars.
```

## Bootstrapping a brand-new repo

```bash
# 1. Create the repo using the GitHub template (TODO — not yet built)
gh repo create three-cubes/NEW-REPO --template three-cubes/platform-repo-template --private

# 2. Provision the WIF identity
az deployment group create \
  --resource-group RG-AGENTS-CORE \
  --template-file https://raw.githubusercontent.com/three-cubes/tc-pipelines/v1/infra/bicep/ci-deploy-identity.bicep \
  --parameters repoOwner=three-cubes repoName=NEW-REPO

# 3. Populate variables, create environment
CLIENT_ID=$(az deployment group show -g RG-AGENTS-CORE --name ci-deploy-identity \
  --query 'properties.outputs.clientId.value' -o tsv)
# ...etc
gh variable set AZURE_CLIENT_ID --body "$CLIENT_ID" --repo three-cubes/NEW-REPO
gh api -X PUT /repos/three-cubes/NEW-REPO/environments/production --silent

# 4. The template already includes .github/workflows/deploy-on-merge.yml.
#    Edit it to point at YOUR target VMs + apply scripts.
```

A future PR will add the `three-cubes/platform-repo-template` repo. Until then, copy-paste the thin caller from the tc-agent-zone migration example above.

## Rollback

If the migration introduces a bug:

```bash
# Revert the workflow file in the consumer repo. The shared workflow is
# pinned to @v1 — even if the platform-templates repo has a regression,
# pinning to a previous SHA gets you out:
uses: three-cubes/tc-pipelines/.github/workflows/azure-vm-deploy.yml@<known-good-sha>
```

The shared workflow has snapshots baked in by default — even a buggy apply gets a rollback window.
