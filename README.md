# tc-pipelines

> **Part of the [Three Cubes Golden Path](https://github.com/three-cubes)** — the paved road every repo's quality gate, CI, and deploy derive from.
> Sibling: **[tc-fitness](https://github.com/three-cubes/tc-fitness)** — the runnable quality gate (`three-cubes-fitness` / the `tc-fitness` CLI) that this repo's CI invokes.

The org's reusable **pipelines**: the `workflow_call` workflows and composite actions every Three Cubes repo's CI and deploy derive from — one source of truth instead of drifting copies. Two halves under one repo:

- **CI & quality** — run the converged quality gate, scan with Sonar, release, mutation-test, build containers.
- **Azure-VM deploy** — snapshot → apply → smoke a VM on merge.

**License:** Apache-2.0 (see [`LICENSE`](LICENSE)). Public, consumed by public + private org repos; secret-free by construction. **Pin everything to a tag** — consumers pin `@v1` (the floating major); never `@main`.

---

## Part 1 — CI & quality gate

The Python quality gate is defined **once**, as a runnable binary: the fitness engine **[tc-fitness](https://github.com/three-cubes/tc-fitness)** (`three-cubes-fitness`, the `tc-fitness` CLI). Each consuming repo declares its gate — dep-install, the exact pytest invocation (test dirs + `--cov` roots + markers + `-n auto`), ruff/bandit targets, the detect-secrets baseline, and its check-catalogue module — in a repo-local `[tool.tc_fitness]` block (or `.tc-fitness.toml`). Then:

- **CI** runs it via [`python-quality-gate.yml`](.github/workflows/python-quality-gate.yml), which shrinks to `checkout → setup-uv-cached → uv run tc-fitness run`.
- **Local** `make check` runs the *same* `uv run tc-fitness run`.

Because both shell out to the same binary reading the same config, **local == CI by construction**. Repo-specifics are config the engine reads — never inputs baked into the workflow. `tc-agent-zone` and `kairix` are the consumers.

```yaml
# caller in a three-cubes repo — the WHOLE python job:
jobs:
  quality:
    uses: three-cubes/tc-pipelines/.github/workflows/python-quality-gate.yml@v1
    with:
      python-version: "3.12"
      sync-args: "--locked --all-packages"   # workspace repo
      run-node: true                          # taz's TS half
    secrets:
      gh-token: ${{ secrets.GITHUB_TOKEN }}
```

### Reusable workflows

| Workflow | Purpose |
|---|---|
| [`python-quality-gate.yml`](.github/workflows/python-quality-gate.yml) | The converged Python gate → `uv run tc-fitness run`. Provisions a pinned/cached uv venv, then runs the engine against the repo's `[tool.tc_fitness]` config. |
| [`meta-quality-gate.yml`](.github/workflows/meta-quality-gate.yml) | Self-CI for **framework / non-Python** repos (this repo, docs/action collections). Repo-agnostic hygiene legs — actionlint, yamllint, license-present, branch-naming — each independently toggleable; all caller inputs env-bound before any shell body (injection-safe). |
| [`sonar-scan.yml`](.github/workflows/sonar-scan.yml) | SonarCloud scan via **artifact handoff** (not a test re-run) — imports the coverage XML `python-quality-gate` produced + JUnit results. `sonar.qualitygate.wait=true`. **Secret:** `SONAR_TOKEN`. |
| [`release.yml`](.github/workflows/release.yml) | Thin common release **spine** (HITL): validate CalVer tag, extract CHANGELOG section, create + push tag, `gh release create`. Repo-specific gates stay in the caller. **Secret:** `gh-token`. |
| [`mutation-gate.yml`](.github/workflows/mutation-gate.yml) | Diff-scoped mutation / parity gate; ratchets a survivors baseline. |
| [`docker-build-publish.yml`](.github/workflows/docker-build-publish.yml) | Build and optionally push a container image. |
| [`fresh-install-smoke.yml`](.github/workflows/fresh-install-smoke.yml) | Clean-image install smoke via compose. |
| [`example-callers.yml`](.github/workflows/example-callers.yml) | Static **call-graph self-check** of every reusable above — `workflow_dispatch` only; a caller mismatch fails *here*, not in a consumer's pipeline. |

Each `workflow_call` contract (inputs / secrets / defaults) is documented in the workflow file's header. The two primary consumer surfaces:

#### `python-quality-gate.yml` — key inputs

| Input | Default | Purpose |
|---|---|---|
| `python-version` | `"3.12"` | Python uv resolves against |
| `uv-version` | `"0.11.16"` | pinned uv version |
| `fetch-depth` | `2` | checkout depth (`0` for full-history scans) |
| `sync-args` | `"--locked --all-packages"` | `uv sync` args (single-package repos: `--all-extras --all-groups`) |
| `ci-requirements-path` | `".github/requirements-ci.txt"` | `--require-hashes` CI-tools file; `""` skips |
| `tc-fitness-args` | `"run"` | args to the engine CLI (e.g. `run --changed-only`) |
| `pre-steps` / `post-steps` | `""` | bash run before / after the gate |
| `upload-coverage-artifact` | `true` | upload engine coverage XML for `sonar-scan.yml` |
| `run-node` | `false` | run the pnpm/TS half (separate ecosystem) |

**Secret:** `gh-token` (optional) — `GITHUB_TOKEN` for the engine's secret-scan changed-file diff; falls back to `github.token`.

#### `meta-quality-gate.yml` — key inputs

| Input | Default | Purpose |
|---|---|---|
| `run-actionlint` / `run-yamllint` / `run-license` / `run-branch-naming` | `true` | toggle each hygiene leg |
| `yamllint-paths` | `".github/workflows actions"` | paths yamllint scans |
| `license-file` / `spdx-id` | `"LICENSE"` / `"Apache-2.0"` | license leg target + expected SPDX id |
| `branch-name-pattern` | `^[a-z][a-z0-9-]*/[a-z0-9][a-z0-9_/-]*$` | org `<user>/<slug>` branch shape |

### Composite actions (CI install seams)

| Action | Purpose |
|---|---|
| [`actions/setup-uv-cached`](actions/setup-uv-cached/action.yml) | The org-standard install seam: pinned `astral-sh/setup-uv` (cache on) + `uv sync <sync-args>` + optional `uv pip install --require-hashes` CI tools. |
| [`actions/pre-commit-cached`](actions/pre-commit-cached/action.yml) | `setup-uv-cached` + `pre-commit/action`, single-sourced. Self-pins `setup-uv-cached@v1`. |
| [`actions/license-present`](actions/license-present/action.yml) | Asserts a top-level LICENSE declaring the expected SPDX id — the WHOLE-REPO provenance gate. Used by `meta-quality-gate.yml`'s license leg. |

---

## Part 2 — Azure-VM deploy

**The load-bearing consumer is [tc-agent-zone](https://github.com/three-cubes/tc-agent-zone)'s `deploy-on-merge` pipeline** — it calls [`azure-vm-deploy.yml@v1`](.github/workflows/azure-vm-deploy.yml) to snapshot → apply → smoke `vm-openclaw` (and `vm-hermes-poc`) on every merge to `main`. Treat that seam as load-bearing: the workflow's `workflow_call` inputs / secrets / permissions are a **stable contract** — change it only behind a major bump.

Every Three Cubes repo that deploys to Azure VMs does it the same way: composite actions for atoms (snapshot, WIF login, apply via run-command, smoke check), a reusable workflow for the end-to-end pattern, and a Bicep module for the Azure-side identity. Consumers call in instead of re-implementing.

### Quick start (new consumer repo)

```bash
# 1. Provision the WIF identity for your repo (one-time, ~2 min)
az deployment group create \
  --resource-group RG-AGENTS-CORE \
  --template-file https://raw.githubusercontent.com/three-cubes/tc-pipelines/v1/infra/bicep/ci-deploy-identity.bicep \
  --parameters repoOwner=three-cubes repoName=YOUR-REPO keyVaultName=kv-tc-agents

# 2. Populate GitHub repo variables from the outputs (AZURE_CLIENT_ID / TENANT_ID / SUBSCRIPTION_ID)
#    See docs/MIGRATION.md for the exact az/gh sequence.

# 3. Create the production environment
gh api -X PUT /repos/three-cubes/YOUR-REPO/environments/production --silent

# 4. Add a thin workflow in your repo that calls azure-vm-deploy.yml@v1 (see docs/MIGRATION.md).
```

### Azure deploy surfaces

| Surface | Purpose |
|---|---|
| [`.github/workflows/azure-vm-deploy.yml`](.github/workflows/azure-vm-deploy.yml) | Reusable — WIF → snapshot → apply → smoke for one or many VMs. |
| [`.github/actions/wif-azure-login`](.github/actions/wif-azure-login/action.yml) | Wraps `azure/login@v2` with the Three Cubes WIF convention. |
| [`.github/actions/snapshot-azure-vm-disk`](.github/actions/snapshot-azure-vm-disk/action.yml) | OS-disk snapshot before destructive ops. |
| [`.github/actions/apply-on-vm-via-runcommand`](.github/actions/apply-on-vm-via-runcommand/action.yml) | Invokes a script on a VM via `az vm run-command`. |
| [`.github/actions/smoke-systemctl`](.github/actions/smoke-systemctl/action.yml) | Post-deploy `systemctl is-active` rollup. |
| [`infra/bicep/ci-deploy-identity.bicep`](infra/bicep/ci-deploy-identity.bicep) | Provisions the managed identity + federated cred + RBAC roles. |

Design notes: [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) · migration: [docs/MIGRATION.md](docs/MIGRATION.md) · cost: [docs/COST-OPTIMIZATION.md](docs/COST-OPTIMIZATION.md).

> **Layout note:** the CI install seams live under top-level `actions/`; the Azure deploy composites live under `.github/actions/` (referenced by `azure-vm-deploy.yml` via local `./.github/actions/...` paths). Both are valid composite-action locations.

---

## Principles

- **One definition of the gate.** The Python gate is the `tc-fitness` binary + the consuming repo's `[tool.tc_fitness]` config. CI and local both invoke it.
- **Config lives in GitHub org/repo variables + secrets and each repo's `[tool.tc_fitness]`, never hardcoded here.** Reusable workflows take only orchestration-level config as `inputs`.
- **Pin everything.** Third-party actions pinned to a full commit SHA; consumers pin this repo's reusables to `@v1`; this repo self-pins its own composites to `@v1`.
- **Public, but secret-free.** Zero credentials in this repo.

## Versioning

Consumers pin `@v1` (the floating major) so changes roll out on the org dependency-cooldown cadence; the `v1.x.y` immutable tags mark exact baselines. Breaking changes ratchet through major bumps. This repo self-pins its own composites to `@v1`.
