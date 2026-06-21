# three-cubes/ci-workflows

Org-shared CI for three-cubes repositories — reusable workflows (`workflow_call`) and composite actions, so every repo's CI derives from one source instead of drifting copies. Part of [kairix#499](https://github.com/three-cubes/kairix/issues/499) Phase 4.

**License:** Apache-2.0 (see [`LICENSE`](LICENSE)). Public, consumed by public + private org repos; secret-free by construction.

## The converged gate

The Python quality gate is defined **once**, as a runnable binary: the fitness
engine (`three-cubes-fitness`, the `tc-fitness` CLI). Each consuming repo
declares its gate — dep-install, the exact pytest invocation (test dirs +
`--cov` roots + markers + `-n auto`), ruff/bandit targets, the detect-secrets
baseline, and its check-catalogue module — in a repo-local `[tool.tc_fitness]`
block (or `.tc-fitness.toml`). Then:

- **CI** runs it via this repo's [`python-quality-gate.yml`](.github/workflows/python-quality-gate.yml), which shrinks to `checkout → setup-uv-cached → uv run tc-fitness run`.
- **Local** `make check` runs the *same* `uv run tc-fitness run`.

Because both shell out to the same binary reading the same config, **local == CI
by construction**. Repo-specifics are config the engine reads — never inputs
baked into the workflow. `tc-agent-zone` and `kairix` are the two consumers.

```yaml
# caller in a three-cubes repo — the WHOLE python job:
jobs:
  quality:
    uses: three-cubes/ci-workflows/.github/workflows/python-quality-gate.yml@v1
    with:
      python-version: "3.12"
      sync-args: "--locked --all-packages"   # workspace repo
      run-node: true                          # taz's TS half
    secrets:
      gh-token: ${{ secrets.GITHUB_TOKEN }}
```

## Layout

```
actions/                         composite actions (shared install seams)
  setup-uv-cached/               pinned uv + cached venv
  pre-commit-cached/             setup-uv-cached + pre-commit run
.github/workflows/               reusable workflow_call workflows
  python-quality-gate.yml        Python gate → `uv run tc-fitness run`
  sonar-scan.yml                 SonarCloud scan via coverage-artifact handoff
  release.yml                    CalVer tag + CHANGELOG notes + gh release (HITL)
  mutation-gate.yml              diff-scoped mutation/parity gate
  docker-build-publish.yml       build + (optional) push a container image
  fresh-install-smoke.yml        clean-image install smoke
  example-callers.yml            static call-graph self-check of the above
  ci.yml                         this repo's own self-check (actionlint/yamllint/license)
```

## Principles

- **One definition of the gate.** The Python gate is the `tc-fitness` binary +
  the consuming repo's `[tool.tc_fitness]` config. CI and local both invoke it.
- **Config lives in GitHub org/repo variables + secrets and in each repo's
  `[tool.tc_fitness]`, never hardcoded here.** Reusable workflows take only
  orchestration-level config as `inputs`.
- **Pin everything.** Third-party actions pinned to a full commit SHA;
  consumers pin this repo's reusables to `@v1`; this repo self-pins its own
  composites to `@v1`.
- **Public, but secret-free.** Zero credentials in this repo.

## Reusable workflows

### `python-quality-gate.yml`

The converged Python gate. Provisions a pinned/cached uv venv
(`setup-uv-cached`), then runs `uv run tc-fitness run` — the engine reads the
repo's `[tool.tc_fitness]` config and runs the gate in order. The workflow owns
only the Actions **environment** around that command.

| Input | Default | Purpose |
|---|---|---|
| `python-version` | `"3.12"` | Python uv resolves against |
| `uv-version` | `"0.11.16"` | pinned uv version (forwarded to the composite) |
| `fetch-depth` | `2` | checkout depth (`0` for full-history scans) |
| `sync-args` | `"--locked --all-packages"` | `uv sync` args (single-package repos: `--all-extras --all-groups`) |
| `ci-requirements-path` | `".github/requirements-ci.txt"` | `--require-hashes` CI-tools file; `""` skips |
| `tc-fitness-args` | `"run"` | args to the engine CLI (e.g. `run --changed-only`) |
| `pre-steps` | `""` | bash run before the gate (repo-specific orchestration) |
| `post-steps` | `""` | bash run after the gate |
| `upload-coverage-artifact` | `true` | upload engine coverage XML for `sonar-scan.yml` |
| `coverage-xml-path` | `"coverage.xml"` | engine coverage XML path (match `sonar-project.properties`) |
| `coverage-artifact-name` | `"coverage-data"` | artifact name the Sonar job downloads |
| `run-node` | `false` | run the pnpm/TS half (separate ecosystem) |
| `node-version-file` | `".nvmrc"` | Node version file (when `run-node`) |
| `pnpm-version` | `"10.27.0"` | pnpm version (when `run-node`) |
| `pnpm-install-args` | `"--frozen-lockfile --ignore-scripts"` | `pnpm install` args |
| `ts-coverage-command` | `"pnpm -r --if-present test:coverage"` | TS coverage command |

**Secret:** `gh-token` (optional) — `GITHUB_TOKEN` for the engine's secret-scan changed-file diff; falls back to `github.token`.

### `meta-quality-gate.yml`

The self-CI gate for **framework / non-Python** repos — the second org GHA
shape. Where `python-quality-gate.yml` runs the full fitness catalogue, repos
with no Python package to gate (this repo, docs repos, action collections) run
the repo-agnostic hygiene legs instead. Each leg is independently toggleable;
all caller inputs are env-bound before any shell body (injection-safe).

| Input | Default | Purpose |
|---|---|---|
| `run-actionlint` | `true` | run actionlint over the workflows + actions |
| `run-yamllint` | `true` | run yamllint over `yamllint-paths` |
| `run-license` | `true` | assert a top-level LICENSE declaring `spdx-id` |
| `run-branch-naming` | `true` | assert the head branch matches `branch-name-pattern` |
| `yamllint-paths` | `".github/workflows actions"` | paths yamllint scans |
| `yamllint-config` | relaxed (line-length + document-start off) | inline yamllint `-d` config |
| `license-file` | `"LICENSE"` | license file the license leg asserts |
| `spdx-id` | `"Apache-2.0"` | expected SPDX id |
| `branch-name-pattern` | `^[a-z][a-z0-9-]*/[a-z0-9][a-z0-9_/-]*$` | org `<user>/<slug>` branch shape (Linear shape is a superset) |

### `sonar-scan.yml`

SonarCloud scan via **artifact handoff** (not a test re-run). Downloads the
coverage XML the `python-quality-gate` job produced (engine-written, uploaded as
`coverage-data`) plus JUnit results, then runs `SonarSource/sonarqube-scan-action`
against the working tree + imported reports. `sonar.qualitygate.wait=true` makes
the job conclusion the gate verdict.

| Input | Default | Purpose |
|---|---|---|
| `fetch-depth` | `0` | full history for blame + new-code detection |
| `coverage-artifact-name` | `"coverage-data"` | coverage artifact to import |
| `test-results-artifact-name` | `"test-results-unit-3.12"` | JUnit artifact to import |
| `project-key` | `""` | `sonar.projectKey` override (else from properties) |
| `args` | `""` | extra scanner args (caller-controlled, never event-derived) |

**Secret:** `SONAR_TOKEN` (required).

### `release.yml`

Thin common release **spine** (HITL): validate the CalVer tag, extract the
matching CHANGELOG section, create + push the annotated tag, `gh release create`
(draft by default), optional fan-out hook. Repo-specific release **gates** stay
in the caller as a `needs:` job that runs first. Touches the release path — only
run with explicit per-action authorization.

Key inputs: `version` (required), `calver-pattern`, `changelog-label`,
`changelog-extract-command`, `release-title`, `tag-message`, `draft`, `ref`,
`fan-out-command`. **Secret:** `gh-token`.

### `mutation-gate.yml`

Diff-scoped mutation / parity gate. Installs via uv or pip, optionally apt
extras, runs a caller-supplied runner against a diff base, ratchets a survivors
baseline. Inputs: `runner-command`, `runner-args`, `baseline-path`,
`diff-base-strategy`, `install-mode`, `uv-sync-args`/`pip-install-args`,
`apt-packages`, `language-partition`, `node-version`/`pnpm-version`,
`timeout-minutes`, `restrict-to-main`.

### `docker-build-publish.yml`

Build and optionally push a container image. Inputs: `image`, `registry`,
`version-source` (`input` | `setuptools-scm`), `version-arg-name`,
`tag-strategy`, `tags`, `fetch-depth`, `push`, `load`. **Secrets:**
`registry-username`, `registry-password` (only when pushing).

### `fresh-install-smoke.yml`

Clean-image install smoke: build the image, run a smoke script against a fresh
install via compose. Inputs: `image-name`, `image-tag`, `build-context`,
`dockerfile`, `smoke-script`, `smoke-env-var-name`, `timeout-minutes`.

### `example-callers.yml`

Static **call-graph self-check** of every reusable above (including
`python-quality-gate.yml`, exercised by a kairix-shaped and a taz-shaped
caller). `workflow_dispatch` only; every job gated `if: run-for-real == 'true'`
(default `false`). actionlint
+ the reusable-workflow resolver validate input names / required-input presence
/ types / secret shapes against each `workflow_call` contract at parse time — so
a caller mismatch fails *here*, not in kairix's or taz's pipeline. The bodies
never execute under the default dispatch.

## Composite actions

### `actions/setup-uv-cached`

The org-standard install seam: pinned `astral-sh/setup-uv` (cache enabled) +
`uv sync <sync-args>` + optional `uv pip install --require-hashes` CI tools.
Inputs: `uv-version`, `python-version`, `sync-args`, `ci-requirements`.

### `actions/pre-commit-cached`

`setup-uv-cached` + `pre-commit/action`, single-sourced so callers stop
hand-chaining the pair (kairix's `arch-fitness-catalogue` hook needs the synced
venv + `tc_fitness` importable). Inputs: `uv-version`, `python-version`,
`sync-args`, `ci-requirements`, `pre-commit-config`, `extra-args`. Self-pins
`setup-uv-cached@v1`.

### `actions/license-present`

Asserts the repo carries a top-level LICENSE file declaring the expected
license — the WHOLE-REPO provenance gate (distinct from the fitness engine's
per-source-file `license_present` header check). Matches either an explicit
`SPDX-License-Identifier: <id>` line or the known body markers for the
recognised ids (Apache-2.0, MIT, BSD-2/3-Clause, GPL-3.0, MPL-2.0). Inputs:
`license-file` (default `LICENSE`), `spdx-id` (default `Apache-2.0`). Used by
`meta-quality-gate.yml`'s license leg.

## Versioning

Consumers pin `@v1` (the floating major) so changes roll out on the org
dependency-cooldown cadence. The `v1.0.0` immutable tag marks the exact baseline.
This repo self-pins its own composites to `@v1`.
