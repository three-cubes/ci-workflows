# platform-templates

Three Cubes platform engineering — reusable composite actions, shared workflows, and Bicep modules for cross-repo CI/CD standardisation.

**The premise:** every Three Cubes repo that deploys to Azure VMs should be doing it the same way. This repo holds that "same way" as code — composite actions for atoms (snapshot, WIF login, apply via run-command, smoke check), reusable workflows for end-to-end patterns, and Bicep modules for the Azure-side identity setup. Consumer repos call into these instead of re-implementing.

## What's here

```
.github/
  actions/
    wif-azure-login/              # Composite — wraps azure/login@v2 with the Three Cubes convention
    snapshot-azure-vm-disk/       # Composite — takes OS-disk snapshots before destructive ops
    apply-on-vm-via-runcommand/   # Composite — invokes a script on a VM via az vm run-command
    smoke-systemctl/              # Composite — post-deploy systemctl is-active rollup
  workflows/
    azure-vm-deploy.yml           # Reusable — WIF → snapshot → apply → smoke for one or many VMs
infra/
  bicep/
    ci-deploy-identity.bicep      # Instantiable — provisions the MI + federated cred + 3 RBAC roles
docs/
  IMPLEMENTATION.md               # How the pieces fit together; design rationale
  MIGRATION.md                    # Step-by-step migration for an existing repo
  COST-OPTIMIZATION.md            # Azure + GitHub Actions cost; runner placement; permanent vs ephemeral env
```

## Quick start (new consumer repo)

```bash
# 1. Provision the WIF identity for your repo (one-time, ~2 min)
az deployment group create \
  --resource-group RG-AGENTS-CORE \
  --template-file https://raw.githubusercontent.com/three-cubes/platform-templates/v1/infra/bicep/ci-deploy-identity.bicep \
  --parameters repoOwner=three-cubes repoName=YOUR-REPO keyVaultName=kv-tc-agents

# 2. Populate GitHub repo variables from the outputs
CLIENT_ID=$(az deployment group show --name ci-deploy-identity \
  -g RG-AGENTS-CORE --query 'properties.outputs.clientId.value' -o tsv)
TENANT_ID=$(az deployment group show --name ci-deploy-identity \
  -g RG-AGENTS-CORE --query 'properties.outputs.tenantId.value' -o tsv)
SUB_ID=$(az deployment group show --name ci-deploy-identity \
  -g RG-AGENTS-CORE --query 'properties.outputs.subscriptionId.value' -o tsv)

gh variable set AZURE_CLIENT_ID --body "$CLIENT_ID" --repo three-cubes/YOUR-REPO
gh variable set AZURE_TENANT_ID --body "$TENANT_ID" --repo three-cubes/YOUR-REPO
gh variable set AZURE_SUBSCRIPTION_ID --body "$SUB_ID" --repo three-cubes/YOUR-REPO

# 3. Create the production environment
gh api -X PUT /repos/three-cubes/YOUR-REPO/environments/production --silent

# 4. Add a thin workflow file in your repo that calls the shared workflow
#    See docs/MIGRATION.md for the exact shape.
```

## Versioning

This repo uses simple major tags (`v1`, `v2`, etc.). Consumer repos pin to a major tag and we ratchet breaking changes through major bumps. Patches/improvements within `v1` are picked up automatically next CI run.

## See also

- [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) — design decisions + composition
- [docs/MIGRATION.md](docs/MIGRATION.md) — migrating tc-agent-zone + kairix
- [docs/COST-OPTIMIZATION.md](docs/COST-OPTIMIZATION.md) — Azure spend + runner placement + permanent-env decision
