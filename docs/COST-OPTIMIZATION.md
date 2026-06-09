# Cost optimization

Where the money goes in a Three Cubes-shaped Azure + GitHub CI/CD setup, and the decisions that move the needle. Read this when planning a new repo OR when an Azure bill arrives that doesn't add up.

## TL;DR — the 5 highest-leverage moves

| Move | Effort | Monthly saving (typical) |
|---|---|---|
| Add a snapshot-prune cron (14-day retention) | 1h | $20–$150 |
| Right-size always-on VMs to B-series or stop-when-idle | 4h | $30–$200 |
| Move CI to a self-hosted runner on an existing local box (Mac mini / NUC) | 1d setup | $0–$30 (you're likely in free Actions tier today; this is insurance) |
| Skip permanent staging tier; use ephemeral PR envs OR smoke-on-prod | n/a | $50–$300 (a tier you don't pay for) |
| Cache pnpm/uv/pip in CI | 30 min | Time-only — saves 2-4 min per run |

The biggest single line on most Azure bills is **always-on VMs that don't need to be on 24/7**. Second is **snapshots accumulating without prune**. Everything else is small until usage scales 10×.

## Where the money goes

### 1. Always-on Azure VMs

Three Cubes today runs `vm-openclaw` + `vm-hermes-poc` 24/7. Estimated cost depends on SKU:

| SKU | vCPU / RAM | Hourly | Monthly (730h) |
|---|---|---|---|
| Standard_B1s | 1 / 1 GB | $0.0104 | ~$7.50 |
| Standard_B2s | 2 / 4 GB | $0.0416 | ~$30 |
| Standard_D2s_v3 | 2 / 8 GB | $0.0960 | ~$70 |
| Standard_D4s_v3 | 4 / 16 GB | $0.192 | ~$140 |

**What to check:** `az vm list -g RG-AGENTS-CORE --query "[].{name:name, sku:hardwareProfile.vmSize}" -o table`.

**Where to push back:** if either VM is < 30% CPU at peak (check Azure Monitor), downsize one tier. Always-on D4s is usually overprovisioned for an agent platform; D2s or even B-series often fits.

**Stop-when-idle:** if a VM is only used during dev hours, configure an auto-shutdown:

```bash
az vm auto-shutdown -g RG-AGENTS-CORE -n vm-openclaw --time 1900   # 7 PM local
```

Cuts a 24/7 VM to ~10h/day → ~58% saving.

### 2. Snapshots without retention

Every CI deploy from `azure-vm-deploy.yml` creates one snapshot per target VM. At typical sizes:

- OS disk snapshot ~30 GB × $0.05/GB-month = **$1.50/snapshot/month**
- 1 deploy/day × 2 VMs × 30 days = 60 snapshots = **$90/month accumulating** indefinitely

**The cron** (add to every consumer repo):

```yaml
name: "Prune deploy snapshots (14d retention)"
on:
  schedule:
    - cron: '17 4 * * *'   # 4:17 UTC daily
  workflow_dispatch:

permissions:
  contents: read
  id-token: write

jobs:
  prune:
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: three-cubes/platform-templates/.github/actions/wif-azure-login@v1
        with:
          client-id: ${{ vars.AZURE_CLIENT_ID }}
          tenant-id: ${{ vars.AZURE_TENANT_ID }}
          subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
      - name: Delete snapshots older than 14 days
        run: |
          set -euo pipefail
          CUTOFF=$(date -u -d '14 days ago' +%Y-%m-%dT%H:%M:%SZ)
          NAMES=$(az snapshot list -g RG-AGENTS-CORE \
            --query "[?contains(name, 'pre-deploy') && timeCreated < '$CUTOFF'].name" -o tsv)
          for n in $NAMES; do
            echo "Deleting $n"
            az snapshot delete -g RG-AGENTS-CORE -n "$n" --no-wait
          done
```

(`Disk Snapshot Contributor` already grants `snapshots/delete` — no extra RBAC needed.)

### 3. GitHub Actions minutes

**Free tier on private repos:** 2,000 minutes/month for the org (Free plan).

**Typical Three Cubes spend per PR:**
- Quality gate: ~5 min
- SonarCloud: ~5 min
- (No matrix on PR — only main)

**Estimated:** 50 PRs/month × 10 min = 500 minutes. **Well within free tier.**

**When you actually pay:** if PR volume scales to 250+ PRs/month (i.e. high-velocity team), OR if you add expensive jobs (Docker builds, integration tests against real services). Current Three Cubes shape is unlikely to exceed free tier.

**Caveat:** the deploy workflow (`azure-vm-deploy.yml`) adds runtime when it fires. Even at 10 deploys/month × 5 min = 50 min — negligible.

### 4. Egress + Storage

Marginal at current scale. App Insights ingestion is the most variable — if logs spike, costs spike. Cap the daily quota:

```bash
az monitor app-insights component update \
  -g RG-AGENTS-CORE -a ai-tc-agents \
  --query 'workspaceId' -o tsv | xargs -I{} az monitor log-analytics workspace update \
    --workspace-name {} --workspace-resource-group RG-AGENTS-CORE \
    --query 'workspaceCapping.dailyQuotaGb' -o tsv
```

Set a daily quota that's 2x your normal traffic — past that, logs drop instead of charging.

## The two decisions you flagged

### Decision 1: Self-hosted runner vs GitHub-hosted

**The numbers:**

| Option | Monthly cost | Setup effort | Ongoing ops |
|---|---|---|---|
| GitHub-hosted `ubuntu-latest` (Free tier 2,000 min) | $0 | 0 | 0 |
| Self-hosted on Azure B1s | ~$7.50 + storage | 1h | low (reboots, patching) |
| Self-hosted on existing Mac/NUC at home | $0 | 4h | medium (NAT, uptime) |

**Recommendation: stay on GitHub-hosted.** Three Cubes is well under the free-tier ceiling. Self-hosted only becomes interesting when:

1. **You blow past 2,000 minutes/month** consistently (you're not).
2. **You need access to a private network** that GitHub runners can't reach (current pattern uses `az vm run-command`, which routes through Azure's control plane — no VPN needed).
3. **You want CI to test against private resources** (DBs, internal APIs) — applies if FEAT-159 ships a staging tier with private endpoints.

**When this changes:** if/when (3) becomes true, the right shape is a self-hosted runner on a B1s in the SAME VNet as the staging environment — so it can hit private endpoints. ~$7.50/month is worth it then. NOT before.

**Local hardware runner (Mac mini, NUC):**

- Free if you have the box
- Adds NAT + uptime ops — runner has to stay online, ngrok-style tunnel if not on public IP
- Adds latency unpredictability (your home internet quality)
- Best fit: hobbyist solo dev with a Mac mini already running. Not a fit for Three Cubes' multi-person workflow.

### Decision 2: Permanent verification environment vs ephemeral

**Permanent staging VM (FEAT-159 as scoped):**

- ~$50–$200/month for a staging-tier Azure VM
- Always available for ad-hoc testing
- Drifts unless re-applied on a schedule

**Ephemeral per-PR environment:**

- Spin up via Bicep + tear down after merge: ~$0.10 per PR (10 min of B2s × $0.0007/min)
- Adds 5–10 min wait per PR
- Requires Bicep teardown automation + cleanup cron for orphans

**No staging — smoke test on prod with `--no-restart` + immediate rollback path:**

- $0
- Risk: a bad config writes to live state even with `--no-restart` (config validation might catch it; might not)
- Requires loud snapshots + tested revert procedure (you now have both)

**Recommendation: skip permanent staging at current scale. Use the smoke-test-on-prod path with snapshots as the rollback window.** Move to ephemeral envs when EITHER:

1. The deploy blast radius grows (e.g. ingesting real customer data, not just synthetic engagement bundles)
2. PR volume justifies the per-PR env automation cost

**Why not ephemeral now:** the Bicep teardown automation is ~3 days of work. The snapshot-revert path is already in place + tested. At Three Cubes' current "blast radius = synthetic bundles + dev work" scale, the staging-tier ROI doesn't beat the build cost.

**The decision rule, if you want to revisit:** if a bad deploy would cost more than $200 in real harm (lost data, customer trust), pay for staging. If it only costs operator time (~30 min to restore snapshot), don't.

## When to actually act

Trigger conditions for each move:

| Saving | When to act |
|---|---|
| Snapshot prune cron | NOW — every PR you merge adds ~$3/month accumulating |
| Right-size VMs | Within 1 week of having `az monitor` data showing < 30% CPU peak |
| Auto-shutdown VMs | If both VMs are dev-hours-only AND you're OK with morning cold-start |
| Self-hosted runner on B1s | When private-endpoint testing matters (FEAT-159 staging tier) |
| Ephemeral PR env | When deploy blast radius hits customer data |
| Permanent staging | Don't. Use ephemeral when the trigger above hits. |

## Telemetry to add

To know if these decisions stay right as you scale:

1. **GitHub Actions billing API** — `gh api /orgs/three-cubes/settings/billing/actions` returns total minutes used + spend. Add a monthly cron that posts the report to Slack.
2. **Azure Cost Management API** — daily cost rollup by resource tag. Tag every Three Cubes resource with `Repo=<name>` so you can attribute spend per repo.
3. **Snapshot count** — `az snapshot list -g RG-AGENTS-CORE --query 'length(@)'` should stay under ~30 (14-day retention × 2 daily snapshots).

If any of these trends 2× month-over-month, revisit this doc.
