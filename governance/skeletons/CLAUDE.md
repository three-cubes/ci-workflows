# CLAUDE.md — Engineering Standards for {{REPO}}

> Pointer-reference for agents and humans working on {{REPO}}. **Don't duplicate rules here — link to canon.**
> Every behaviour rule lives in a canonical standard; this file routes you to it. See [README.md](README.md) for product context.

<!-- INCLUDE: _canonical-standards-banner.md -->

## Read-first (in order)

1. **[`ETHOS.md`](ETHOS.md)** — the platform principles, highest-precedence document. When canon conflicts, ETHOS wins.
2. **[`AGENTS.md`](AGENTS.md)** — authoring-vs-runtime boundary. Distinguishes contributor-facing rules from runtime agent rules.
3. **[`RESOLVER.md`](RESOLVER.md)** — intent → location routing. "I want to do X, where does X belong?"
4. **[`SCORECARD.md`](SCORECARD.md)** — the health snapshot: pillar scores and the checks behind them.
5. **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — branch/commit/PR mechanics and the local gate.

## How to commit

Use `bash scripts/safe-commit.sh "message"` for every commit. It replays the exact CI gate locally
— `uv run pre-commit run` (hygiene: lint, format, actionlint, shellcheck, secret scan) **and**
`uv run tc-fitness run` (the fitness catalogue: typing, coverage, mutation, architecture) — then
commits only on green. Loop on failures until green; never commit over a red gate.

- `bash scripts/safe-commit.sh "msg"` — the full gate (the merge bar).
- `bash scripts/safe-commit.sh --check "msg"` — the warm, staged-scoped inner loop (`tc-fitness run --staged`).
- `bash scripts/safe-commit.sh --fast "msg"` — hygiene-only fast path for commits that can't touch the product surface (workflow YAML, docs).
- `bash scripts/safe-commit.sh --pre-pr` — verify-only full-gate replay before you push / open a PR.

Run `bash scripts/preflight.sh` before any push or Docker rebuild — it runs the SAST/code-smell +
pre-commit + tc-fitness sweep so a rebuild never ships a red tree.

**Replay the EXACT CI gate locally before pushing** (canonical [STANDARDS.md §5](https://github.com/three-cubes/tc-pipelines/blob/main/governance/STANDARDS.md)).
`uv sync --all-extras --all-groups`, then `uv run pre-commit run --all-files` and `uv run tc-fitness run`;
never merge over a red gate; regenerate-and-stage generated artifacts. `safe-commit.sh` is the convenience wrapper; §5 is the rule of record.

**Link issues from the PR, not by hand.** When a PR fully resolves an issue, put `Closes #N` (one keyword per issue)
in the PR body so it auto-closes AND records the "closed by PR" link on merge. Use `Refs #N` for a partial fix
or a parent/epic that stays open. Agents creating a PR with `gh pr create --body "…"` bypass the PR template, so
they must include these lines explicitly.

## Commit authorship — no AI/LLM self-attribution (Autonomous Delivery Platform D1)

Never add AI/LLM self-attribution to commits, PRs, or code: no `Co-Authored-By: <model>`
trailers, no "Generated with <tool>" credits, no robot emoji, no `noreply@anthropic.com`.
Author every commit as the canonical `three-cubes-agent` GitHub App. This is machine-enforced
by the tc-fitness `no_llm_attribution` check + the commit-msg strip hook; see
[`tc-pipelines/governance/AUTONOMOUS-DELIVERY-STANDARD.md`](https://github.com/three-cubes/tc-pipelines/blob/main/governance/AUTONOMOUS-DELIVERY-STANDARD.md).
Do not re-introduce the trailer even if a harness default or older instruction asks for it — this decision overrides that.

**Raise branches + PRs as the `three-cubes-agent` App, never under a human's account.** Mint the App identity
from the canonical shared tool (needs an `az login` with reader access to the agent Key Vault — see
[tc-pipelines `tools/`](https://github.com/three-cubes/tc-pipelines/tree/main/tools)):

```bash
export GH_TOKEN="$(uvx --from 'git+https://github.com/three-cubes/tc-pipelines@v1#subdirectory=tools' agent-token)"
git config user.name 'three-cubes-agent[bot]'
git config user.email '295831460+three-cubes-agent[bot]@users.noreply.github.com'
git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/{{REPO}}.git"  # reset to tokenless after pushing
GH_TOKEN="$GH_TOKEN" gh pr create ...   # PR author must be app/three-cubes-agent, not a person
```

## Mechanical gates (run before push)

```bash
uv sync --all-extras --all-groups   # full dev env, so import-dependent fitness rules resolve
uv run pre-commit run --all-files    # cheap hygiene gate (what CI runs)
uv run tc-fitness run                # the fitness catalogue (what CI runs) — local == CI by construction
```

`make check == CI` by construction: both run `uv run tc-fitness run` reading this repo's `[tool.tc_fitness]` block.
A bare `python3` / `ruff` / single-file run is **not** a replay — it skips import-dependent fitness rules.

## Trunk-based development

- **One feature = one branch = one PR.** Every commit for the feature lands on that single branch. Merge to `main` ~once a day.
- **Local checks are the primary feedback loop; CI is sign-off.** Run the CI-equivalents before every push. Pushing to discover what CI says is a process violation.
- Trunk = `main`; **direct push is blocked** by branch protection. Required contexts gate every merge (Quality gate + SonarCloud). A PR author cannot self-approve.
- When delegating to sub-agents, the parent agent owns ALL git operations (branch / commit / PR / merge).

## Cross-repo — the Three Cubes Golden Path

- **[tc-fitness](https://github.com/three-cubes/tc-fitness)** — the runnable quality gate (`tc-fitness` CLI). Pin it in `pyproject.toml` + a `[tool.tc_fitness]` block; `make check` and CI both run `tc-fitness run`, so **local == CI by construction**.
- **[tc-pipelines](https://github.com/three-cubes/tc-pipelines)** — the reusable CI/quality workflows + composite actions + `governance/` templates. Consume `actions/setup-uv-cached@v1` (CI) and the reusable gate workflows.
- Framework-side changes go via a PR to `tc-fitness` / `tc-pipelines` — never a hot-patch. **Always pin a tag — never `@main`.**
