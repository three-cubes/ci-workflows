# MERGE-QUEUE-D1 — GitHub merge queue for PRODUCT repos (re-test vs latest tip)

Status: Accepted (template — inert until a repo adopts + flips it)
Scope: tc-pipelines (CORE governance templates), kairix (product pilot)
Supersedes (for product repos): kata `docs/adr/ADR-013` (which removed
`merge_group` for "auto-merge, no queue"). See "Reconciliation" below.
Related: SGO-166 (auto-merge-on-green), SGO-168 (this note), SGO-180 (two-profile
rulesets), ADRs STD-MERGE / RULESET-D1 / GATE-HARDEN.

## Decision

Product repos merge to `main` through a **GitHub merge queue** so every PR is
**re-tested against the latest tip** before it lands (the "not-rocket-science"
rule: never merge a green-against-a-stale-base PR that would red `main`). Small
config/data repos may keep **auto-merge, no queue** — their change shape doesn't
carry the semantic-conflict risk a queue exists to catch.

The queue configuration snapshot lives in
[`governance/rulesets/merge-queue.json`](../rulesets/merge-queue.json):

- **grouping = ALLGREEN** — a group merges only if the whole group is green
  (one red entry fails the group, not just itself).
- **group size** — `min_entries_to_merge: 1`, `max_entries_to_merge: 5`,
  `min_entries_to_merge_wait_minutes: 5`, `max_entries_to_build: 5`.
- **allowed merge method** — `SQUASH`.
- **check-response timeout** — `check_response_timeout_minutes: 60` (a required
  check that never reports within the window fails the entry, not hangs the queue).

## ⚠️ The REST 422 gotcha — the `merge_queue` rule is WEB-UI-ONLY

The `merge_queue` **ruleset rule cannot be created via the REST rulesets API** —
`POST`/`PUT /repos/{owner}/{repo}/rulesets` returns **HTTP 422** when the payload
contains a `merge_queue` rule. It **must** be enabled via
**Settings → Rules → Rulesets** in the web UI.

Consequences, and the guardrail:

- `merge-queue.json` is a **documentation snapshot** of the web-UI settings, not
  an `--input` payload. Do **not** `gh api ... --input merge-queue.json`.
- For the same reason, the `merge_queue` rule is deliberately **absent** from
  [`governance/rulesets/main-product.json`](../rulesets/main-product.json)
  (which IS API-appliable). Enabling the queue is a separate, manual web-UI step.
- This mirrors the `_comment` convention already in kairix
  `.github/rulesets/main.json`, so that **re-applying a JSON snapshot never
  silently drops the queue** — the operator is reminded the rule lives only in
  the UI.

## `on: merge_group` — the fan-in check must report on queue events

A merge queue builds a temporary `merge_group` ref and expects the required
checks to report against it. If a product repo's fan-in **"CI gate"** job does
not run on `merge_group` events, the queue can never go green. Add the trigger:

```yaml
on:
  pull_request:
  merge_group:      # required: the fan-in "CI gate" job must report here too
```

The job named **"CI gate"** (the fan-in check-run that
[`auto-merge-on-green.yml`](../../.github/workflows/auto-merge-on-green.yml)
keys off) must therefore report on BOTH `pull_request` and `merge_group`.

## Reconciliation with kata ADR-013

kata `docs/adr/ADR-013` deliberately **removed** `merge_group` to run
"auto-merge, no queue". This note does **not** reverse that decision globally —
it **scopes** it:

- **Product repos** (kairix): adopt the queue (re-test vs tip). ADR-013's
  no-queue stance is superseded **here only**.
- **Small config / data repos** (kata-shaped): may keep auto-merge-no-queue;
  their diffs don't carry the mid-air-collision risk the queue guards against.

## Rollout (per product repo)

1. Add `merge_group:` to the repo's `ci.yml` `on:` and confirm the "CI gate" job
   reports on it.
2. Harden the gate to `governance/gate-hardening.md` and verify it runs green and
   deterministic (never flip a queue on top of a flaky gate).
3. Enable the merge queue in **Settings → Rules → Rulesets** using
   `governance/rulesets/merge-queue.json` as the snapshot (web-UI only — 422).
4. Apply `governance/rulesets/main-product.json` (0-review) via the API.
5. Verify two stacked PRs are each re-tested against the updated tip before merge.
