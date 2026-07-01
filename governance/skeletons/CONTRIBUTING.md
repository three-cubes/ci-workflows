# Contributing to {{REPO}}

Thanks for improving {{REPO}}. This repo rides the Three Cubes Golden Path: a green gate is the contract,
and the same gate runs locally and in CI by construction.

<!-- INCLUDE: _canonical-standards-banner.md -->

## Before you start

- Read [`CLAUDE.md`](CLAUDE.md) — the index that routes every task to its canonical standard.
- Read [`AGENTS.md`](AGENTS.md) — the authoring-vs-runtime boundary.
- Read [`ETHOS.md`](ETHOS.md) — the platform principles (highest precedence).
- Use [`RESOLVER.md`](RESOLVER.md) to decide *where* a new file belongs.

## One-time setup

```bash
uv sync --all-extras --all-groups   # full dev env (so import-dependent fitness rules resolve)
uv run pre-commit install           # wire the hygiene hooks
uv run pre-commit install --hook-type commit-msg   # wire the no-attribution strip hook
```

## The loop

1. **Branch.** `git checkout -b <user>/<team>-<number>-<slug>` (one feature = one branch = one PR).
2. **Work.** Small commits; keep the tree green.
3. **Commit through the gate.** `bash scripts/safe-commit.sh "message"` — replays `uv run pre-commit run`
   + `uv run tc-fitness run` and commits only on green. Use `--check` for the warm inner loop, `--fast`
   for docs/workflow-only commits, `--pre-pr` for the verify-only pre-push replay.
4. **Preflight.** `bash scripts/preflight.sh` before any push or Docker rebuild (SAST/code-smell + pre-commit + tc-fitness).
5. **Open a PR** as the `three-cubes-agent` App (never a human account). Reference the issue with `Closes #N` / `Refs #N`.

## Commit authorship — no AI/LLM self-attribution

Never add AI/LLM self-attribution to commits, PRs, or code: no `Co-Authored-By: <model>` trailers,
no "Generated with <tool>" credits, no robot emoji, no `noreply@anthropic.com`. Author every commit as the
canonical `three-cubes-agent` GitHub App. Machine-enforced by the tc-fitness `no_llm_attribution` check +
the commit-msg strip hook.

## The gate is the contract

`make check == CI` by construction — both run `uv run tc-fitness run` reading `[tool.tc_fitness]`.
Never merge over a red gate. If a rule is wrong, propose the change into the canonical home
([tc-fitness](https://github.com/three-cubes/tc-fitness) / [tc-pipelines](https://github.com/three-cubes/tc-pipelines))
— never fork a parallel standard.
