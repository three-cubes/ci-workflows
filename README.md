# tc-pipelines

**What this is:** the shared CI and deploy steps that every Three Cubes repo's GitHub Actions calls — instead of each repo keeping its own copy.

**Why it exists:** quality checks, CI, and deploy used to be hand-copied across repos and drift apart. This repo holds one shared set of CI steps and deploy steps. Every repo uses these, so CI runs the exact same check you run on your laptop, and one fix here improves every repo at once.

> **Sibling repo:** [tc-fitness](https://github.com/three-cubes/tc-fitness) is the quality check itself — the program (`tc-fitness` CLI) that runs your linters, type-check, tests, coverage, security scan, and architecture rules and gives one pass/fail. This repo (tc-pipelines) is the CI and deploy steps that call that check.

## How to use it (3 steps)

1. **Add the workflow to your repo.** In your `.github/workflows`, call the shared check instead of writing your own job. The whole Python CI job is the caller YAML below — copy it and adjust the inputs.
2. **Lock it to a version.** Always pin to `@v1` (the floating major). Never use `@main`. New versions roll out on the org's dependency-cooldown cadence, so you are never surprised by a change you did not ask for.
3. **Run the same check locally before you push.** Install the full dev env and run the check the same way CI does:
   ```bash
   uv sync --all-extras --all-groups
   uv run pre-commit run --all-files
   uv run tc-fitness run
   ```
   Get it green locally first. The check you run locally is the exact same one CI runs.

## What to expect

- **Green merges itself.** When your PR's checks pass, it merges without waiting for a human reviewer.
- **Red you fix.** A failing check is never bypassed. If it is green locally but red in CI, that is a bug in your local setup — fix the setup, do not force the merge.
- **Changes to the checks need a human.** The only change that needs a human approval is a change to the files that define the quality check or CI themselves. This stops anyone — person or agent — from quietly weakening the check that protects every repo.

## Where to go next

- The canonical standard and full index: [`governance/STANDARDS.md`](governance/STANDARDS.md) — improve that one standard; do not fork your own copy.
- The quality check itself: [tc-fitness](https://github.com/three-cubes/tc-fitness).
- Deploy setup, migration, and cost notes: [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) · [docs/MIGRATION.md](docs/MIGRATION.md) · [docs/COST-OPTIMIZATION.md](docs/COST-OPTIMIZATION.md).

**License:** Apache-2.0 (see [`LICENSE`](LICENSE)). This repo is public and used by both public and private org repos. It holds no credentials of any kind.

---

The rest of this README is reference detail: the workflows you can call, the inputs they take, and the deploy and governance setup. The repo has two halves:

- **CI & quality** — run the shared quality check, scan with Sonar, release, mutation-test, build containers.
- **Azure-VM deploy** — snapshot, apply, and smoke-test a VM on merge.

---

## Part 1 — CI & quality check

The Python quality check is defined **once**, as a program you run: [tc-fitness](https://github.com/three-cubes/tc-fitness) (the `tc-fitness` CLI). The tool knows HOW to run the check; your repo says WHAT to check. Each repo declares what to check — dependency install, the exact pytest invocation (test dirs + `--cov` roots + markers + `-n auto`), ruff/bandit targets, the detect-secrets baseline, and its check-catalogue module — in a `[tool.tc_fitness]` block in `pyproject.toml` (or a `.tc-fitness.toml` file). Then:

- **CI** runs it via [`python-quality-gate.yml`](.github/workflows/python-quality-gate.yml), which is just `checkout → setup-uv-cached → uv run tc-fitness run`.
- **Local** `make check` runs the *same* `uv run tc-fitness run`.

Both run the same program reading the same config, so the check you run locally is the exact same one CI runs. Anything repo-specific is config the tool reads — never baked into the workflow. `tc-agent-zone` and `kairix` are current consumers.

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
| [`python-quality-gate.yml`](.github/workflows/python-quality-gate.yml) | The shared Python check → `uv run tc-fitness run`. Sets up a pinned, cached uv venv, then runs the tool against the repo's `[tool.tc_fitness]` config. |
| [`meta-quality-gate.yml`](.github/workflows/meta-quality-gate.yml) | Self-CI for **non-Python** repos (this repo, docs/action collections). Repo-agnostic hygiene checks — actionlint, yamllint, license-present, branch-naming — each one you can turn on or off; all caller inputs are bound to env vars before any shell runs (injection-safe). |
| [`sonar-scan.yml`](.github/workflows/sonar-scan.yml) | SonarCloud scan that **reuses the test results** (it does not re-run tests) — it imports the coverage XML and JUnit results that `python-quality-gate` produced. `sonar.qualitygate.wait=true`. **Secret:** `SONAR_TOKEN`. |
| [`release.yml`](.github/workflows/release.yml) | Thin shared release flow (needs a human): validate CalVer tag, extract CHANGELOG section, create + push tag, `gh release create`. Repo-specific checks stay in the caller. **Secret:** `gh-token`. |
| [`mutation-gate.yml`](.github/workflows/mutation-gate.yml) | Mutation/parity check scoped to the diff; keeps a survivors baseline that can only improve, never get worse. |
| [`docker-build-publish.yml`](.github/workflows/docker-build-publish.yml) | Build and optionally push a container image. |
| [`fresh-install-smoke.yml`](.github/workflows/fresh-install-smoke.yml) | Clean-image install smoke test via compose. |
| [`example-callers.yml`](.github/workflows/example-callers.yml) | Static **self-check of every reusable workflow above** — `workflow_dispatch` only; a caller mismatch fails *here*, not in a consumer's pipeline. |

Each workflow's inputs, secrets, and defaults are documented in the header of the workflow file. The two main consumer surfaces:

#### `python-quality-gate.yml` — key inputs

| Input | Default | Purpose |
|---|---|---|
| `python-version` | `"3.12"` | Python uv resolves against |
| `uv-version` | `"0.11.16"` | pinned uv version |
| `fetch-depth` | `2` | checkout depth (`0` for full-history scans) |
| `sync-args` | `"--locked --all-packages"` | `uv sync` args (single-package repos: `--all-extras --all-groups`) |
| `ci-requirements-path` | `".github/requirements-ci.txt"` | `--require-hashes` CI-tools file; `""` skips |
| `tc-fitness-args` | `"run"` | args to the tool's CLI (e.g. `run --changed-only`) |
| `pre-steps` / `post-steps` | `""` | bash run before / after the check |
| `upload-coverage-artifact` | `true` | upload coverage XML for `sonar-scan.yml` |
| `run-node` | `false` | run the pnpm/TS half (separate ecosystem) |

**Secret:** `gh-token` (optional) — `GITHUB_TOKEN` for the tool's secret-scan changed-file diff; falls back to `github.token`.

#### `meta-quality-gate.yml` — key inputs

| Input | Default | Purpose |
|---|---|---|
| `run-actionlint` / `run-yamllint` / `run-license` / `run-branch-naming` | `true` | turn each hygiene check on or off |
| `yamllint-paths` | `".github/workflows actions"` | paths yamllint scans |
| `license-file` / `spdx-id` | `"LICENSE"` / `"Apache-2.0"` | license check target + expected SPDX id |
| `branch-name-pattern` | `^[a-z][a-z0-9-]*/[a-z0-9][a-z0-9_/-]*$` | org `<user>/<slug>` branch shape |

### Composite actions (CI install steps)

| Action | Purpose |
|---|---|
| [`actions/setup-uv-cached`](actions/setup-uv-cached/action.yml) | The org-standard install step: pinned `astral-sh/setup-uv` (cache on) + `uv sync <sync-args>` + optional `uv pip install --require-hashes` CI tools. |
| [`actions/pre-commit-cached`](actions/pre-commit-cached/action.yml) | `setup-uv-cached` + `pre-commit/action`, from one source. Self-pins `setup-uv-cached@v1`. |
| [`actions/license-present`](actions/license-present/action.yml) | Asserts a top-level LICENSE declaring the expected SPDX id — the whole-repo provenance check. Used by `meta-quality-gate.yml`'s license check. |

---

## Part 2 — Azure-VM deploy

**The main consumer is [tc-agent-zone](https://github.com/three-cubes/tc-agent-zone)'s `deploy-on-merge` pipeline** — it calls [`azure-vm-deploy.yml@v1`](.github/workflows/azure-vm-deploy.yml) to snapshot → apply → smoke `vm-openclaw` (and `vm-hermes-poc`) on every merge to `main`. Treat that connection as load-bearing: the workflow's inputs, secrets, and permissions are a **stable contract** — change it only behind a major version bump.

Every Three Cubes repo that deploys to Azure VMs does it the same way: composite actions for the small steps (snapshot, WIF login, apply via run-command, smoke check), one reusable workflow for the end-to-end flow, and a Bicep module for the Azure-side identity. Consumers call these instead of re-implementing them.

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
| [`.github/actions/apply-on-vm-via-runcommand`](.github/actions/apply-on-vm-via-runcommand/action.yml) | Runs a script on a VM via `az vm run-command`. |
| [`.github/actions/smoke-systemctl`](.github/actions/smoke-systemctl/action.yml) | Post-deploy `systemctl is-active` rollup. |
| [`infra/bicep/ci-deploy-identity.bicep`](infra/bicep/ci-deploy-identity.bicep) | Provisions the managed identity + federated cred + RBAC roles. |

Design notes: [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) · migration: [docs/MIGRATION.md](docs/MIGRATION.md) · cost: [docs/COST-OPTIMIZATION.md](docs/COST-OPTIMIZATION.md).

> **Layout note:** the CI install steps live under top-level `actions/`; the Azure deploy composites live under `.github/actions/` (referenced by `azure-vm-deploy.yml` via local `./.github/actions/...` paths). Both are valid composite-action locations.

---

## Part 3 — Repo governance templates

[`governance/`](governance/) holds the standard baseline a new repo adopts so its branch protection, review routing, dependency policy, and local check match the shared setup every repo uses: the `main` branch ruleset (`rulesets/main.json`), `CODEOWNERS`, `dependabot.yml`, and `pre-commit-config.yaml`. One command wires a repo from these — [`bootstrap-repo-governance.sh`](https://github.com/three-cubes/tc-agent-zone/blob/main/scripts/bootstrap-repo-governance.sh) (in tc-agent-zone). See [`governance/README.md`](governance/README.md).

---

## Part 4 — Agent identity

Agents act as a dedicated **GitHub App** (`three-cubes-agent`), not a person's account — so they open and prepare PRs that the quality check (and, for changes to the checks themselves, a human) then judges, with clean authorship and no shared personal credentials. The App's ID + private key live in `kv-tc-agents`; CI mints a short-lived installation token at runtime over WIF — no GitHub-stored secret.

| Surface | Purpose |
|---|---|
| [`.github/actions/github-app-token`](.github/actions/github-app-token/action.yml) | WIF → read the agent App creds from Key Vault → mint a short-lived installation token. Outputs `token` (+ `app-slug`, `installation-id`). |

```yaml
# in a consumer repo workflow — authenticate git/gh as the agent App:
permissions: { id-token: write, contents: read }
steps:
  - id: app
    uses: three-cubes/tc-pipelines/.github/actions/github-app-token@v1
    with:
      client-id:       ${{ vars.AZURE_CLIENT_ID }}
      tenant-id:       ${{ vars.AZURE_TENANT_ID }}
      subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
  - run: gh pr merge --auto --merge "$PR_URL"
    env: { GH_TOKEN: ${{ steps.app.outputs.token }} }
```

Prereq: the repo's WIF identity needs Key Vault Secrets User on `kv-tc-agents` — provision once with `ci-deploy-identity.bicep keyVaultName=kv-tc-agents` (Part 2 quick start) and set the `AZURE_*` repo variables.

---

## Principles

- **One definition of the check.** The Python check is the `tc-fitness` program + the consuming repo's `[tool.tc_fitness]` config. CI and local both run it.
- **Config lives in GitHub org/repo variables + secrets and each repo's `[tool.tc_fitness]`, never hardcoded here.** Reusable workflows take only orchestration-level config as `inputs`.
- **Pin everything.** Third-party actions pinned to a full commit SHA; consumers pin this repo's reusables to `@v1`; this repo self-pins its own composites to `@v1`.
- **Public, but no secrets.** Zero credentials in this repo.

## Versioning

Consumers pin `@v1` (the floating major) so changes roll out on the org dependency-cooldown cadence; the `v1.x.y` immutable tags mark exact baselines. Breaking changes go out only through major version bumps. This repo self-pins its own composites to `@v1`.
