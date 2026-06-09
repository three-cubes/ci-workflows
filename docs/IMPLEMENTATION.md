# Implementation

How the layers compose. Read this to understand WHY the repo is shaped the way it is — and where to extend.

## The 4 layers

```
┌─────────────────────────────────────────────────────────────────┐
│ Consumer repo (tc-agent-zone, kairix, ...)                      │
│ ─────────────────────────────────────────                       │
│ .github/workflows/deploy.yml — thin file, ~20 lines             │
│ Calls the shared reusable workflow below.                       │
└──────────────────────────┬──────────────────────────────────────┘
                           │  uses:
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2 — Reusable workflow                                     │
│ ─────────────────────────────                                   │
│ .github/workflows/azure-vm-deploy.yml                           │
│ Defines the WIF → snapshot → apply → smoke shape.               │
└──────────────────────────┬──────────────────────────────────────┘
                           │  uses:
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1 — Composite actions (the atoms)                         │
│ ─────────────────────────────────                               │
│ .github/actions/wif-azure-login                                 │
│ .github/actions/snapshot-azure-vm-disk                          │
│ .github/actions/apply-on-vm-via-runcommand                      │
│ .github/actions/smoke-systemctl                                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Layer 0 — Azure-side identity (provisioned once per consumer)   │
│ ─────────────────────────────────────────                       │
│ infra/bicep/ci-deploy-identity.bicep                            │
│ Creates: user-assigned MI + federated cred + 3 RBAC roles       │
└─────────────────────────────────────────────────────────────────┘
```

## Why this shape

### Why composite actions for atoms (not bash scripts)

A bash script that lives in this repo would need to be `curl`'d down by each consumer workflow run. Composite actions are first-class GitHub Actions — they:

- Get cached by GitHub on the runner (faster startup)
- Expose typed inputs/outputs (visible in the UI; auto-validated)
- Compose naturally with `uses:` from other actions and workflows
- Surface their interface via the same `action.yml` syntax everyone already knows

### Why a reusable workflow on top

Composite actions can't run on different runners than their caller — they're inline-expanded. A reusable workflow defines its own jobs + runners and can be composed at a higher level (parallel matrix, conditional jobs, environment protection). The deploy pattern needs that level — it's a job-shaped concern, not a step-shaped concern.

### Why Bicep for the identity layer

The WIF identity + federated credential + RBAC grants are infrastructure, not workflow logic. Bicep:

- Is idempotent — re-running converges, doesn't drift
- Outputs the identity's clientId/principalId for the GitHub variables step
- Tracks state in the Azure deployment history (audit trail of "when did we change RBAC")
- Composes with other Bicep modules (consumer repos that already use Bicep for VMs/KV can `module .. = { ... }` the deploy identity alongside)

Terraform would work equivalently. Pick whichever your consumer repos already use; the Bicep here is a reference shape.

### Why public visibility

GitHub Actions reusable workflows + composite actions can be consumed across private repos only if both repos' Actions settings allow it AND the org plan supports it. On Free tier with private internal-visibility-disabled, cross-private-repo consumption fails with a 404.

Making this repo **public** sidesteps the entire plan-tier question. The contents are workflows + Bicep + docs — no secrets, no proprietary code. The exposure is intentional: this is Three Cubes' public platform-engineering posture.

## Caller contract

A consumer workflow looks like this (full example in `docs/MIGRATION.md`):

```yaml
jobs:
  deploy:
    uses: three-cubes/platform-templates/.github/workflows/azure-vm-deploy.yml@v1
    permissions:
      contents: read
      id-token: write     # required for WIF
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

The contract:

| Input | Type | Notes |
|---|---|---|
| `resource-group` | string | Single RG for all targets. Multi-RG deploys need separate jobs. |
| `targets` | YAML string | List of `{vm-name, apply-script, smoke-units}` maps. `smoke-units` can be empty. |
| `op-tag` | string | Snapshot name prefix. Use the deploy mode (`deploy-on-merge`, `manual-apply`). |
| `skip-snapshot` | string | "true" or "false". Maps to the `SKIP_SNAPSHOT=true` env var the snapshot action honours. |
| `azure-{client,tenant,subscription}-id` | string | Repo variables. WIF needs these to mint the OIDC token. |

The reusable workflow does the rest:
1. WIF login (composite action)
2. Snapshot every target VM (composite action)
3. Loop targets: apply via az run-command + smoke check
4. Fail the job loudly on any per-step error — caller sees a clean red X

## Extending

When a new pattern is needed (e.g. ACR push before deploy, Bicep apply before VM deploy):

1. Build it as a **composite action** if it's a single step shape.
2. Build it as a **reusable workflow** if it's a multi-job shape.
3. Add docs/MIGRATION.md examples for at least one consumer.
4. Cut a new major tag (`v2`) if it breaks any existing consumer.

## Versioning policy

- `v1`, `v2`, `v3`: major. Breaking changes to inputs/outputs of any composite or reusable workflow.
- Within a major (`v1.x`): non-breaking improvements. Consumer pinned to `@v1` picks them up.
- No `latest` tag. Consumers must pin a major — undeclared moving targets break trust.

## Security model

- The repo is public. Workflows + Bicep are visible to anyone — that's by design.
- No secrets live in this repo. All secrets live in the consumer repos' GitHub Secrets or in Azure Key Vault.
- WIF identity per consumer repo. A leaked identity rotation is `az deployment` away, not a service-principal rotation drill.
- Federated credential subject pinned to `repo:OWNER/NAME:ref:refs/heads/main` and `repo:OWNER/NAME:environment:NAME`. PR-from-fork can't deploy.
