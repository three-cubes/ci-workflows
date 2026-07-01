---
type: adr-policy
id: ADR-POLICY
title: ADR consolidation, home, and namespacing policy (org go-forward)
status: accepted
date: 2026-07-02
owner: "@three-cubes/maintainers"
supersedes: []
superseded_by: []
related:
  - governance/decisions/ADR-INDEX.md
  - governance/STANDARDS.md
  - governance/AUTONOMOUS-DELIVERY-STANDARD.md
  - docs/IMPLEMENTATION.md
linear:
  - SGO-176
  - SGO-179
---

# ADR-POLICY — where architecture decisions live, and how they are numbered

> This is the **go-forward policy** for Architecture Decision Records across the Three Cubes
> paved road (tc-pipelines, tc-fitness, tc-agent-zone, kairix, kata). It records **one rule** —
> org-wide cross-cutting decisions are canon *here*; product decisions stay in their repo — and
> the **namespacing** that lets a cross-repo index exist without renumbering anything. The
> per-repo product decisions themselves are indexed in
> [`ADR-INDEX.md`](ADR-INDEX.md); this file is the org register + the rules.

## Context

The org's paved-road decisions were made real in code and prose before they were ever written as
numbered ADRs. Roughly twenty org-wide, cross-cutting decisions are **embedded** across
`governance/STANDARDS.md`, `governance/gate-hardening.md`, `governance/AUTONOMOUS-DELIVERY-STANDARD.md`,
the `governance/` templates (rulesets, CODEOWNERS, dependabot, renovate), and `docs/IMPLEMENTATION.md`
/ `docs/COST-OPTIMIZATION.md` / `docs/SONAR-MAINTENANCE.md`. They already carry short, stable,
**prefixed handles** in that prose — `STD-MERGE`, `STD-IDENTITY`, `RULESET-D1`, `CODEOWNERS-D1`,
`GATE-HARDEN`, `DEP-D1`, `QG-CONVERGE`, `SONAR-HANDOFF`, `MUT-RATCHET`, `REPO-MERGE`, `WIF-D1..D5`,
`VERS-D1`, `COST-D1/2` — not `ADR-###` numbers.

Meanwhile each **product** repo keeps its own `ADR-###` series in its own `record_dir`
(tc-agent-zone `docs/decisions/`, kairix `docs/architecture/`, kata `docs/adr/`), enforced per-repo
by the tc-fitness `adr_number_unique` core check. Because every repo starts its own count, the same
number means different things in different repos (every repo has an `ADR-013`), and one repo
(tc-agent-zone) even has the **same number twice** across two directories. A naive "renumber
everything into one global series" would break every inbound cross-reference, churn `git blame`, and
violate `guard-forward-only` (Autonomous Delivery Platform decision D2 — no history rewrite).

We need a cross-repo index and a collision fix **without** a global renumber.

## Decision

1. **Org-wide, cross-cutting decisions are canonical in tc-pipelines under `governance/decisions/`.**
   They keep their existing **prefixed IDs** (`STD-*`, `*-D#`, `GATE-HARDEN`, `QG-CONVERGE`, ...) —
   they are **not** renumbered into an `ADR-###` series. This file is their single register (below);
   the authoritative prose each one points at remains the source of record and is not restated here.

2. **Product / repo-local ADRs stay in their repo.** They converge on the repo's `record_dir` and
   the filename pattern `^ADR-\d{3}-.+\.md`, keep **per-repo numbering** (no global renumber), and
   remain owned and evolved by that repo.

3. **Cross-repo disambiguation is by a frontmatter `alias`, not by renumbering.** Each product ADR
   gains a namespaced, org-unique handle via a `alias:` frontmatter field:
   - tc-agent-zone → `TAZ-ADR-###`
   - kairix → `KAI-ADR-###`
   - kata → `KATA-ADR-###`

   The alias is **additive**: the file name and the repo-local `id: ADR-###` are unchanged, so the
   cross-repo index and any cross-repo reference can name a decision unambiguously while each repo's
   own numbering is untouched.

   ```yaml
   ---
   type: adr
   id: ADR-017          # repo-local number — unchanged; the adr_number_unique key
   alias: KAI-ADR-017   # org-stable, cross-repo-unique handle (namespace + number)
   title: Deployment architecture
   status: accepted
   supersedes: []
   superseded_by: []
   ---
   ```

4. **`adr_number_unique` is preserved by construction.** The namespace lives in frontmatter, not in
   the filename, so no repo has to renumber and the check's within-`record_dir` invariant is never
   disturbed. Cross-repo same-numbers never collide because the `alias` disambiguates them. The one
   real violation the register must not paper over is **intra-repo, cross-directory** (see below).

5. **Additive and forward-only.** Decisions are appended, never deleted or renumbered. Supersession
   is recorded with a `superseded_by:` banner on the old record pointing at the new one (D2,
   guard-forward-only). Reconciling a product ADR against an org decision (e.g. kata `ADR-013` vs
   `STD-MERGE`) is done with an amendment/superseded-by banner on the product ADR, never by moving it
   out of its home repo.

## The org-level register (canonical here)

Home repo for every row below is **tc-pipelines**. Each is a decision already in force; this table
gives it a durable ID + one-line statement + its source of record. It does not restate the
authoritative text — read the source.

| ID | Decision (one line) | Source of record |
|---|---|---|
| `STD-IDENTITY` | Agents author every commit + PR as the canonical `three-cubes-agent` GitHub App, never a human identity — so review is possible and attribution is clean. | `STANDARDS.md` §4; `AUTONOMOUS-DELIVERY-STANDARD.md` D1; `README.md` (agent identity); `AGENTS.md` |
| `STD-MERGE` | Autonomous-on-green for ordinary work; HITL only on the control plane; de-churned (no forced up-to-date rebase, no stale-dismiss); never merge over a red gate. | `STANDARDS.md` §4–5; `AUTONOMOUS-DELIVERY-STANDARD.md` D3; `gate-hardening.md` |
| `RULESET-D1` | The `main` ruleset: block deletion + force-push; PR required with **0 approvals (autonomous)** + code-owner review on control-plane paths; **not strict** (no forced rebase) + **no stale-dismiss**; required checks = Quality gate + SonarCloud scan + SonarCloud Code Analysis (+ Mutation). | `governance/rulesets/main.json`; `governance/README.md` |
| `CODEOWNERS-D1` | Two-tier review routing: only the control plane (the gate's own definition) is owned — **no `* @OWNER`** — so work merges autonomously while gate-defining changes need a human. | `governance/CODEOWNERS`; `governance/README.md` |
| `GATE-HARDEN` | Before a repo flips to 0-review its gate must clear the Gate-Hardening bar — a hard bar on new/changed code + monotonic ratchet on legacy debt, determinism non-negotiable. **Harden then flip, never flip first.** | `governance/gate-hardening.md` |
| `QG-CONVERGE` | The reusable Python gate shrinks to `checkout → setup-uv-cached → uv run tc-fitness run`; every step lives in each repo's `[tool.tc_fitness]`, so `make check == CI` by construction. | `.github/workflows/python-quality-gate.yml`; `STANDARDS.md` §2–3; `CHANGELOG.md` |
| `SONAR-HANDOFF` | Sonar runs as an artifact-handoff reusable (`sonar-scan.yml`, `qualitygate.wait=true`) with CORE-owned new-code-reset + weekly hotspot-triage drivers; project key + hotspot rationales stay per-repo and triage fails closed. | `docs/SONAR-MAINTENANCE.md`; `.github/workflows/sonar-scan.yml` / `sonar-triage.yml` / `sonar-new-code-reset.yml` |
| `MUT-RATCHET` | Mutation is diff-scoped and ratcheted: an escaped mutant on a changed line fails; the survivors baseline only ratchets down; Mutation is a required status check. | `governance/gate-hardening.md`; `.github/workflows/mutation-gate.yml` |
| `DEP-D1` | Dependency policy: 3-day-cooldown, grouped dependabot (pip + npm + github-actions, security-toggle-off) + a Renovate customManager pinning the tc-fitness engine version (no silent drift). | `governance/dependabot.yml`; `governance/renovate.json` |
| `REPO-MERGE` | Two CORE paved-road repos — tc-pipelines (reusable CI + governance templates) and tc-fitness (the gate engine); consumers pin `@v1` / engine `@vX` + lockfile SHA; **promote prior work up into CORE, never fork-and-inline.** | `AUTONOMOUS-DELIVERY-STANDARD.md` (paved road); `STANDARDS.md` §3 |
| `VERS-D1` | Semantic major pinning for reusables: `@vN` majors, breaking input/output changes cut a new major, **no `latest` tag** (undeclared moving targets break trust). | `docs/IMPLEMENTATION.md` (versioning policy); `README.md` |
| `WIF-D1` | Azure deploys authenticate via Workload Identity Federation (OIDC): CI mints a short-lived token at runtime — no service-principal secret stored in GitHub. | `docs/IMPLEMENTATION.md` (security model); `README.md` |
| `WIF-D2` | One WIF identity **per consumer repo** (blast-radius isolation); a leaked identity is an `az deployment` rotation away, not an org-wide SP drill. | `docs/IMPLEMENTATION.md` |
| `WIF-D3` | The federated-credential subject is pinned to `repo:OWNER/NAME:ref:refs/heads/main` + `:environment:NAME`, so PR-from-fork cannot deploy. | `docs/IMPLEMENTATION.md` |
| `WIF-D4` | The identity + federated credential + RBAC grants are provisioned as Bicep (`ci-deploy-identity.bicep`) — idempotent, audit-tracked in Azure deployment history. | `docs/IMPLEMENTATION.md` (why Bicep); `infra/bicep/` |
| `WIF-D5` | tc-pipelines is **public** (workflows + Bicep + docs, no secrets), sidestepping the private-repo Actions plan-tier limit for cross-repo reuse. | `docs/IMPLEMENTATION.md` (why public visibility) |
| `COST-D1` | Every deploy-snapshotting consumer runs a snapshot-prune cron (**14-day** retention) so per-deploy snapshots don't accumulate cost indefinitely. | `docs/COST-OPTIMIZATION.md` |
| `COST-D2` | Right-size / stop-when-idle always-on VMs and prefer ephemeral PR envs or smoke-on-prod over a permanently-paid staging tier. | `docs/COST-OPTIMIZATION.md` |

The broader `AUTONOMOUS-DELIVERY-STANDARD.md` locked decisions **D1–D8** (no-attribution, guard-forward-only,
safe lights-out merge, Linear-as-control-surface, and the D5–D8 defaults) sit above this register as
the program-level frame; `STD-IDENTITY` / `STD-MERGE` are their in-force paved-road expression and are
the rows you cite from CI and governance.

## `adr_number_unique` — the one real collision, and the follow-up

Namespacing removes every *cross-repo* number clash. It does **not** by itself fix an *intra-repo,
cross-directory* clash, and tc-agent-zone has exactly one:

- `tc-agent-zone/docs/decisions/ADR-013-agent-constitution-conformance.md`, **and**
- `tc-agent-zone/docs/architecture/adr-013-infrastructure-scaling-triggers.md`

— two `ADR-013`s in one repo, in two directories. The current single-directory, uppercase-only
`adr_number_unique` scan misses this. The go-forward remediation (tracked as follow-ups in
[`ADR-INDEX.md`](ADR-INDEX.md), owned by **SGO-176 / SGO-179**, out of scope for this additive policy
doc):

1. **SGO-179** hardens `adr_number_unique` to take a list of `record_dirs` + a case-insensitive
   `ADR-/adr-` prefix, so a cross-directory dupe is caught.
2. **SGO-176** resolves the collision by `git mv`-ing both `docs/architecture` ADRs
   (`adr-012-repo-consolidation`, `adr-013-infrastructure-scaling-triggers`) into `docs/decisions/`
   at fresh next-free numbers (**≥ 047**), sweeping every inbound reference (bare-number + path form)
   and using `git mv` (status `R`) so `guard-forward-only` holds.

This policy is **additive only** — new files under `governance/decisions/`. It does not perform the
migration or edit the tc-fitness check; it records the rule those changes converge on.

## Consequences

- A cross-repo ADR index becomes possible **without** renumbering anything (see `ADR-INDEX.md`).
- Org decisions have durable, greppable IDs that CI, rulesets, CODEOWNERS, and AGENTS.md already cite.
- Each repo's `adr_number_unique` invariant is preserved; the only outstanding violation has an owned
  follow-up.
- Adding an org decision = append a row here + point it at its source of record. Adding a product ADR =
  a normal `ADR-###` in the repo's `record_dir` with an `alias:` frontmatter line.
