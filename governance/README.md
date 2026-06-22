# governance/ — Golden Path repo-governance templates

The canonical baseline a new Three Cubes repo adopts so its branch protection,
review routing, dependency policy, and local gate all match the Golden Path —
without hand-copying drift. Consumed by
[`scripts/bootstrap-repo-governance.sh`](https://github.com/three-cubes/tc-agent-zone/blob/main/scripts/bootstrap-repo-governance.sh)
(in tc-agent-zone), one command that wires a repo from these.

| File | What it sets | How it's applied |
|---|---|---|
| [`rulesets/main.json`](rulesets/main.json) | The `main` branch ruleset: block deletion + force-push; PR required with **0 approvals (autonomous work)** + **code-owner review** (HITL on control-plane paths only); **not strict** (no forced up-to-date rebase) and **no stale-dismiss** — to stop the rebaseline → re-run → re-review churn; required checks **Quality gate** + **SonarCloud scan** + **SonarCloud Code Analysis**. | `gh api repos/<repo>/rulesets --method POST` |
| [`CODEOWNERS`](CODEOWNERS) | **Two-tier** review routing — only the control plane (the gate's own definition) is owned, so work merges autonomously on a green gate while gate-defining changes need a human. **No `* @OWNER`. Replace `@OWNER`** with your human team. | committed to the target repo's `.github/CODEOWNERS` |
| [`dependabot.yml`](dependabot.yml) | 3-day-cooldown dependency policy (pip + npm + github-actions), grouped, security-toggle-OFF. | committed to `.github/dependabot.yml` |
| [`pre-commit-config.yaml`](pre-commit-config.yaml) | The cheap local gate (hygiene + detect-secrets + actionlint + shellcheck + ruff + bandit) before the CI round-trip. | committed to `.pre-commit-config.yaml` |

These are a **starting baseline** — a repo trims/extends them (drop `npm` if it
ships no JS; add repo-specific CODEOWNERS paths or pre-commit hooks). The
ruleset's required-check contexts are the Golden Path contract and should not be
weakened.

The `main` ruleset mirrors tc-agent-zone's own repo-level ruleset, so a bootstrapped
repo enforces the same gate. The fitness gate itself lives in
[tc-fitness](https://github.com/three-cubes/tc-fitness); the reusable CI that
produces the required `Quality gate` / `SonarCloud scan` checks lives here in
[tc-pipelines](../README.md).
