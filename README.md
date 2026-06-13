# three-cubes/ci-workflows

Org-shared CI for three-cubes repositories — reusable workflows (`workflow_call`) and composite actions, so every repo's CI derives from one source instead of drifting copies. Part of [kairix#499](https://github.com/three-cubes/kairix/issues/499) Phase 4.

## Layout

```
actions/                 composite actions (shared steps)
  setup-uv-cached/        pinned uv + cached venv
.github/workflows/        reusable workflow_call workflows (added incrementally)
```

## Principles

- **Config lives in GitHub org/repo variables + secrets, never hardcoded here or in consuming repos.** Reusable workflows and actions take config as `inputs`; consuming repos pass org/repo `vars`/`secrets` (e.g. `PRIVATE_INFRA_PATTERNS`, `SONAR_TOKEN`, coverage floors, image names). No git-excluded config files.
- **Pin everything.** Third-party actions pinned to a full commit SHA; consumers pin this repo's reusable workflows to a `@vN` tag so a change rolls out per-repo on the org dependency-cooldown cadence, not all at once.
- **Public, but secret-free.** This repo is public (consumed by public + private org repos); it contains zero credentials — those stay in org/repo secrets.

## Consuming

```yaml
# caller in a three-cubes repo:
jobs:
  quality:
    uses: three-cubes/ci-workflows/.github/workflows/python-quality-gate.yml@v1
    with:
      python-version: "3.12"
    secrets: inherit
```
