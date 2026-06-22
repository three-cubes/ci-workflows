# Gate-Hardening Standard — making "green" trustworthy enough for autonomous merge

Under the two-tier model (`rulesets/main.json`: **0 approvals on work**), the gate is the
*only* thing standing between an agent's PR and `main`. So "green" must mean **correct,
well-tested, secure, and deterministic** — not just "the tests that exist passed."

**Sequence (load-bearing):** harden a repo's gate to this bar and verify it runs green,
*then* flip that repo to 0-review. Never flip first.

## Principle — clean as you code

Hold **new / changed** code to a hard bar; **ratchet** legacy debt monotonically (it may
only improve). Don't block the agent on pre-existing debt it didn't write; block it on the
diff it just produced. Every threshold below starts at the repo's **current** value (no
day-one breakage) and only goes up.

## The required checks (the merge gate)

`rulesets/main.json` requires these contexts; all must be green to merge:

| Check | Produced by | Enforces |
|---|---|---|
| **Quality gate** | `python-quality-gate.yml` → `uv run tc-fitness run` | lint + format + types + security + tests + **coverage floor** + fitness functions + zero-tolerance secret-scan — one binary, repo `[tool.tc_fitness]` config |
| **SonarCloud scan** / **SonarCloud Code Analysis** | `sonar-scan.yml` (artifact handoff, `qualitygate.wait=true`) | the **new-code** quality gate (below) |
| **Mutation** *(add to required set)* | `mutation-gate.yml` (diff-scoped, ratcheted) | the test suite actually *kills* mutations on changed lines |

## `[tool.tc_fitness]` bar — code repos (kairix, tc-agent-zone)

Each leg is **blocking** (a finding fails the gate), run by the engine in order:

- **lint** — `ruff check` on a defined ruleset; **no blanket `# noqa`**.
- **format** — `ruff format --check`.
- **types** — `mypy` (or `pyright`) **strict on changed packages**.
- **security** — `bandit` (medium+ severity) + `uv`/`pip-audit` (no known-vuln deps).
- **tests** — `pytest -n auto`, **fixed seed**, unit tests **network-blocked** (integration
  tests marked + run outside the required gate).
- **coverage** — `--cov` with **`branch = true`** and **`--cov-fail-under = <floor>`**
  (floor = current; ratchet up). Honest coverage: no `# pragma: no cover` on real branches;
  generated/`__main__`/trivial excluded explicitly, not by gaming.
- **detect-secrets** — zero-tolerance over the empty baseline (already standard).
- **fitness functions — "that bites"** — architectural invariants as code: import-linter
  layering, banned-import rules, public-API/schema conformance, no orphan/unreferenced
  modules. These are the structural guarantees that constrain autonomous work *even where a
  single change's own tests are thin*.

## Sonar new-code quality gate (clean-as-you-code conditions)

On **new code** (the agent's diff), set `sonar.qualitygate.wait=true` and require:

- coverage on new code **≥ 80%**
- **0** new bugs, **0** new vulnerabilities, **0** unreviewed new security hotspots
- new-code maintainability rating **A**; **0** new blocker/critical smells
- duplicated lines on new code **< 3%**

## Mutation (diff-scoped, ratcheted)

- run `mutmut`/`cosmic-ray` on **changed lines only** (fast + relevant)
- an **escaped mutant on a changed line fails** — the new code must be tested enough to kill it
- a survivors baseline carries legacy debt and **only ratchets down**

## Determinism — flaky == agent loops (non-negotiable)

A flaky required check is the single biggest cause of agents "going in circles." So:

- **pinned seeds**; no unseeded `random`/wall-clock in tests
- **no network** in the required unit gate (hermetic)
- **no rerun-masking** (`pytest --reruns`) on the required gate — a rerun hides flakiness and
  manufactures loops. A flaky test is a **bug**: quarantine it (mark + drop from the required
  set + log to a flaky register) and fix it; never auto-retry it green
- **`concurrency: { group: pr-${{ github.ref }}, cancel-in-progress: true }`** on PR CI —
  superseded pushes cancel stale runs (no pileup, no zombie loops)
- hermetic inputs: `uv --locked`, SHA-pinned actions, pinned tool versions

## Data / config repos (kata) — a different bar

kata has no app code to cover; its gate hardens via **structural completeness**, not coverage:

- schema conformance + referential integrity across id-spaces *(already enforced)*
- **every capability MUST declare ≥1 `tests:` entry + a route-test (incl. the `<unmatched>`
  case) + its conformance rules** — enforce in `scripts/validate.py`
- manifest completeness — no unregistered or orphan files *(already)*
- generated-doc freshness (`render-*.py --check`) *(already)*
- zero-tolerance secret-scan *(already)*

Hardening here = make these fitness functions **comprehensive** (close any structural
invariant an agent could currently satisfy with an empty/degenerate artifact).

## Rollout per repo

1. Add the missing legs to the repo's `[tool.tc_fitness]` + Sonar new-code conditions;
   set every threshold to **current** (green on day one).
2. Add **Mutation** to `rulesets/main.json` `required_status_checks`.
3. Run the gate; confirm green and **deterministic** (run it ~5× — zero flakes).
4. Only then apply the 0-review ruleset to that repo. Ratchet thresholds up over time.
