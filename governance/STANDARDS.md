# Three Cubes — Build, Release & Governance Standard (CANONICAL)

> **🛑 Agents & humans: READ THIS before touching CI, the quality gate, fitness functions,
> coverage, mutation, or merge governance in ANY repo. These standards already exist and are
> detailed. Do NOT re-derive them. Converge *up* to them; promote improvements *into* the
> canonical homes below — never fork a parallel standard.**

This is the single referenceable index of the org's build/release/governance intent. It points to
the authoritative sources; it does not restate them.

## 1. Quality + fast-feedback requirements (the OKRs)

**[Build & Release Health initiative](https://linear.app/three-cubes/initiative/build-and-release-health-afb5e313b215)** (Linear) — the requirements, from a 6-agent root-cause diagnosis (2026-06-21):

- **O1 — Trustworthy local loop:** `make check` runs the *literal* CI command; a **<60s smoke tier** exists. (Fast feedback is a first-class requirement, not a nice-to-have.)
- **O2 — Honest, enforcing coverage:** whole-product scope, re-captured baseline, **required monotonic ratchet**, no skip-pass.
- **O3 — Fitness that bites:** ≥1 architecture/layering gate, mutation pilot, de-theatre placement-only rules.
- **O4 — Self-draining supply chain:** dependency auto-merge, zero high-severity alerts > 7 days.

KPIs and the **Wave 0/1/2** execution plan live in the initiative.

## 2. The fitness-function spec (the detailed design)

**[kairix#499 — "fitness-function system v2"](https://github.com/three-cubes/kairix/issues/499)** — the defect-grounded spec:

- the **F-series** fitness functions (~70 ADR/incident-traced rules), a single catalogue, never-renumber discipline;
- the **tiered, time-budgeted execution model** — pre-commit / safe-commit / CI Stage-0 (**<3s**); diff-scoped mutation (**~2–3 min**); nightly soak (non-blocking). *This is how "harder gate" and "fast feedback" coexist — rigour is diff-scoped and tiered, never brute-forced on the inner loop.*
- **Phase 4 = org-shared CI** (`uses:` reusables) — the convergence every repo follows.

**kairix is the reference implementation.** Best-of-breed patterns are promoted *into* the canonical
engine + reusables, then every repo converges up — nobody is down-levelled.

## 3. Canonical homes (where the standard lives — improve it HERE)

- **[`tc-fitness`](https://github.com/three-cubes/tc-fitness)** — the runnable gate engine (`uv run tc-fitness run`) + the check catalogue. New checks land here.
- **[`tc-pipelines`](https://github.com/three-cubes/tc-pipelines)** (this repo) — the reusable `workflow_call` workflows + composite actions + `governance/` templates (rulesets, CODEOWNERS, gate-hardening, dependabot, pre-commit). CI + governance shape land here.
- **[`governance/gate-hardening.md`](gate-hardening.md)** — the bar a repo's gate must clear before it runs autonomously (a pointer to the §2 spec, not a parallel definition).

## 4. Merge governance (the model)

Agents author PRs as a **dedicated GitHub App** (`three-cubes-agent`), never a human identity, so review is possible and attribution is clean. The merge model:

- **Autonomous on green** for ordinary work — a green gate auto-merges, no human, no admin bypass.
- **HITL only on the control plane** — the gate's own definition (CI, `[tool.tc_fitness]`, schemas, validators, dep pins, governance) needs a human (`@three-cubes/maintainers`) via **CODEOWNERS**, so an agent can never weaken the gate that gates it.
- **De-churned** — no forced up-to-date rebase, no stale-dismiss (see [`governance/rulesets/main.json`](rulesets/main.json) + [`governance/CODEOWNERS`](CODEOWNERS)).

This model is **safe only because the gate is hard + fast** (§1–§2). Harden + verify a repo's gate before flipping it to autonomous.

## 5. For agents (the anti-reinvention rule)

Before you design a quality gate, a fitness function, a coverage/mutation policy, a CI workflow, or a
governance rule: **it already exists above.** Read it. If it's missing or weak, **propose the change
into the canonical home** (§3) — open a PR to `tc-fitness`/`tc-pipelines` — do not re-create it in a
single repo. Every repo's `AGENTS.md` / `CLAUDE.md` / `.github/copilot-instructions.md` links here for
exactly this reason.
