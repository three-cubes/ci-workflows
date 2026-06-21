# Changelog

All notable changes to `three-cubes/ci-workflows` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for the consumer-facing `@vN` workflow/action references.

## [Unreleased]

### Changed

- **`python-quality-gate.yml` now calls the fitness engine.** The reusable
  Python gate shrinks to `checkout → setup-uv-cached → uv run tc-fitness run`.
  The gate's STEPS (ruff/bandit/mypy/pytest/coverage/detect-secrets targets and
  the run-* toggles) are no longer workflow inputs — each consuming repo
  declares them in a repo-local `[tool.tc_fitness]` config that the engine
  reads. The same binary + config is what `make check` runs locally, so
  local == CI by construction. Removed inputs: `ruff-lint-paths`,
  `ruff-lint-args`, `ruff-format-paths`, `bandit-paths`, `bandit-args`,
  `mypy-paths`, `mypy-args`, `compileall-paths`, `shellcheck-find-paths`,
  `pytest-args`, `coverage-paths`, `coverage-fail-under`,
  `normalize-coverage-script`, `detect-secrets-baseline`, and all Python-step
  `run-*` toggles. Added inputs: `tc-fitness-args`, `pre-steps`, `post-steps`,
  `upload-coverage-artifact`, `coverage-xml-path`, `coverage-artifact-name`.
  The Node/TS half (`run-node` + pnpm/node inputs) is retained — a separate
  ecosystem the Python engine does not orchestrate.
- **`python-quality-gate.yml` now uploads the engine-produced coverage XML**
  as the `coverage-data` artifact, completing the artifact-handoff to
  `sonar-scan.yml` from within the converged single job.
- Self-pinned both composites and the reusable gate to
  `three-cubes/ci-workflows/actions/setup-uv-cached@v1` (was `@main`),
  honouring the repo's own "pin @vN" principle.

### Added

- `LICENSE` — Apache-2.0 (was an undeclared `Proprietary` marker on a public
  repo). Matched across `fitness-engine` and `platform-templates`.
- `CHANGELOG.md` (this file).
- Self-CI: `yamllint` over the workflows/actions and a LICENSE-presence
  assertion, alongside the existing `actionlint` pass.

## [1.0.0] — 2026-06-14

First tagged baseline of the org-shared CI surface (kairix#499 Phase 4).

### Added

- Reusable workflows: `python-quality-gate.yml`, `sonar-scan.yml`,
  `release.yml`, `mutation-gate.yml`, `docker-build-publish.yml`,
  `fresh-install-smoke.yml`, and the `example-callers.yml` static
  call-graph self-check.
- Composite actions: `setup-uv-cached` (pinned uv + cached venv install seam)
  and `pre-commit-cached` (synced venv + pre-commit run).
- `ci.yml` self-check running `actionlint`.

[Unreleased]: https://github.com/three-cubes/ci-workflows/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/three-cubes/ci-workflows/releases/tag/v1.0.0
