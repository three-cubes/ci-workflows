# governance/ — Golden Path repo-governance templates

The canonical baseline a new Three Cubes repo adopts so its branch protection,
review routing, dependency policy, and local gate all match the Golden Path —
without hand-copying drift. Consumed by
[`scripts/bootstrap-repo-governance.sh`](https://github.com/three-cubes/tc-agent-zone/blob/main/scripts/bootstrap-repo-governance.sh)
(in tc-agent-zone), one command that wires a repo from these.

| File | What it sets | How it's applied |
|---|---|---|
| [`rulesets/main.json`](rulesets/main.json) | The `main` branch ruleset: block deletion + force-push; PR required (1 approval, code-owner review, stale-dismiss, thread resolution, merge-only); required checks **Quality gate** + **SonarCloud scan** + **SonarCloud Code Analysis** (strict). | `gh api repos/<repo>/rulesets --method POST` |
| [`CODEOWNERS`](CODEOWNERS) | HITL review routing — default owner + gate-critical paths owner-only. **Replace `@OWNER`.** | committed to the target repo's `.github/CODEOWNERS` |
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
