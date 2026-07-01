# governance/scripts/ — canonical harness scripts

Repo-agnostic scripts a repo adopts (rendered/distributed by
[`bootstrap-repo-governance.sh`](bootstrap-repo-governance.sh)) so its local loop matches the Golden Path.
They hardcode no repo paths or modules: the fitness catalogue lives in
[tc-fitness](https://github.com/three-cubes/tc-fitness) (`uv run tc-fitness run`), the hygiene set in
`.pre-commit-config.yaml` (`uv run pre-commit run`). Promote a new check into tc-fitness, never inline here.

| Script | What it does |
|---|---|
| [`safe-commit.sh`](safe-commit.sh) | Commit only if the canonical gate passes. Replays `uv run pre-commit run` + `uv run tc-fitness run`, then commits. Modes: default (full — the merge bar), `--check` (warm staged inner loop, `tc-fitness run --staged`), `--fast` (hygiene-only, for docs/workflow-only commits), `--pre-pr` (verify-only full replay, no commit). Preserves the `run_gate` command-substitution-under-`set -e` guard. Neutral coverage escape hatch: `SAFE_COMMIT_SKIP_COVERAGE=1`. |
| [`preflight.sh`](preflight.sh) | The pre-push / pre-rebuild sweep: SAST/code-smell (gitleaks + Semgrep OSS, auto-detected) + `uv run pre-commit run --all-files` + `uv run tc-fitness run`. `--quick` skips the slow SAST leg. Run it before any push or Docker rebuild. |
| [`bootstrap-repo-governance.sh`](bootstrap-repo-governance.sh) | One-command governance onboarding: repo variables + KV secrets + the `main` ruleset + governance files, **plus** the agent-affordance + harness payload (rendered [skeletons](../skeletons/), the sonar-sqaa hook, an idempotent `settings.json` PostToolUse jq-merge, and `safe-commit.sh` + `preflight.sh`). `--no-affordance` opts out of the payload. No live git — it renders/verifies locally and prints the fetch+commit sequence. |

Tests live in [`tests/`](tests/) (the bootstrap CLI contract) and [`../skeletons/tests/`](../skeletons/tests/)
(the skeleton render contract). Both are hermetic — no `gh`/`az` auth required.
