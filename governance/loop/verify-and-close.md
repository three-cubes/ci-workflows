# verify-and-close — the deterministic close side of the autonomous delivery loop

Reusable workflow: [`.github/workflows/verify-and-close.yml`](../../.github/workflows/verify-and-close.yml)
(`on: workflow_call`).

This is **SP-C-3** (decision **D5**) of the
[Autonomous Delivery Platform](https://linear.app/three-cubes/initiative/autonomous-delivery-platform-dae678e12c5d)
initiative — the automation that **replaces a human/agent manually verifying a
merge and hand-marking the Linear issue Done**. It closes the loop that the
dispatch side opens: an agent delivers a PR, it merges to `main`, and this
workflow deterministically verifies the merged result and drives the linked
Linear issue to its terminal state — with no per-merge human approval gate
(Increment-3 "Shape-as-orchestrator + enforcing hooks";
delivery-management verbs from PLA-232, harvest-then-decay from PLA-241/PLA-242).

## The contract

A consumer's **on-merge-to-main** workflow calls this reusable. It then, in order:

1. **Resolves the linked Linear issue id.** Precedence:
   1. the `issue-id` input, if set;
   2. the merged branch name, parsed against the org convention
      `<user>/<team>-<number>-<slug>` (e.g. `dan/sgo-156-...` => `SGO-156`) —
      the branch is read from the **merged PR** for the commit (works for merge,
      squash, and rebase strategies, where the push `ref_name` is only `main`);
   3. the merged PR body, scanned for an explicit identifier (e.g. `SGO-156`).
   - If **none** resolve, the workflow emits a **documented SKIP** with `fix:` /
     `next:` guidance and does **not** touch Linear — a false-close is worse than
     a skip (mirrors the SGO-169 deterministic-fallback rule).
2. **Runs the post-merge verification** (`verify-command`) with the repo checked
   out at the merged sha. This is the **deterministic verification trigger** —
   its exit code is the interim gate.
3. **On PASS** — **harvests, then closes** (SP-C-6 · PLA-314 · ADR-038
   harvest-then-decay). **Before** the Done transition it distils + posts a
   structured **harvest** comment (see [below](#harvest-before-close-sp-c-6--pla-314--adr-038)),
   then transitions the issue to `done-state` (default `Done`) via the Linear
   GraphQL API and posts a `verification-confirmed on <sha>` comment. This is the
   delivery-management **close** verb (`update -> harvest -> close`, PLA-232)
   expressed against Linear. **Fail-closed:** if the harvest cannot be recorded,
   the issue is **not** closed — it stays open and is labelled `needs-harvest`.
4. **On FAIL** — moves the issue to `needs-fix-state` (default `Todo`), adds the
   `needs-redispatch` label (created on the issue's team if absent), and comments
   the failure log (a link to the Actions run). The **backlog dispatcher** picks
   the issue up from there — the failure is **never silently dropped** (SP-C
   failure-driven-next). By default the run then exits non-zero so the failure is
   visible red on `main` (`fail-on-verify-failure`, after the redispatch handoff).

## The independent-verifier seam (SGO-169)

The interim gate is **deterministic**: the close fires when the `verify-command`
job is green. Between verify and close there is an **explicit, documented,
currently-optional hook** for the independent-verifier judgment step —
`.github/workflows/independent-verifier.yml`, a fresh-context agent that checks
the diff against the work item's acceptance criteria (SGO-169). It is the
**judgment upgrade** on top of the deterministic gate, not a replacement for it.

Wire it via the `independent-verifier-verdict` input:

- default `pass` => interim behaviour (deterministic close-on-green);
- a consumer that has adopted SGO-169 runs `independent-verifier.yml` as an
  **upstream job** and passes its check-run conclusion (`pass` | `fail` | `skip`)
  into this input. `fail` blocks the close and routes the issue to needs-fix.

Kept **advisory-until-proven**: a flaky REQUIRED verifier would manufacture agent
loops, so the verifier stays a judgment input to this deterministic close — never
a silent hard gate (`governance/gate-hardening.md`, Determinism).

## Harvest before close (SP-C-6 · PLA-314 · ADR-038)

**No close / teardown without a harvest.** ADR-038 (harvest-then-decay /
harvest-before-destroy) is promoted here into a **CORE close-invariant**: an ADP
issue can never transition to `done-state` until a distilled **harvest artifact**
is recorded on it. The close side enforces this *structurally*, not by convention.

On the PASS path, **before** the Done transition, the workflow:

1. **Distils** the harvest (the `Distill harvest artifact` step) — env-bound and
   injection-safe, no Linear key in scope. It sources the **merged PR title/body**
   (resolved for the commit, or the `pr-body` override) plus the verification
   result, and produces a structured comment: **what shipped** (PR title + a
   distilled change summary, with any AI/LLM self-attribution stripped per D1),
   the **verification result** (`PASS on <sha>` + the independent-verifier
   verdict), and the **key decisions** (the `decision`-headed section of the PR
   body, else a pointer to the PR + linked ADRs). It is stamped with a
   provenance + idempotency marker, keyed on the issue id:

   ```text
   <!-- adp-harvest issue=<ISSUE-ID> -->
   ```

2. **Records** it on the issue (the `Update Linear` step) — **idempotent**: if a
   harvest artifact for the issue already exists (marker present), it is not
   re-posted; the close still proceeds.

3. **Closes only then.** The Done transition fires **only after** the harvest is
   on the issue.

**Fail-closed.** If the harvest cannot be recorded (or no artifact was distilled),
the workflow **refuses to close**: the issue is left **open** and labelled
`needs-harvest-label` (default `needs-harvest`), and the run exits non-zero with an
actionable *run-harvest-first* message. A close with no harvest is impossible — a
missing harvest is surfaced, never silently dropped.

The machine-checkable predicate half of this invariant lives next to the loop
state machine: [`governance/loop/harvest_gate.py`](harvest_gate.py) — `has_harvest`
/ `assert_closed_carries_harvest` (provenance-stamped, idempotent), with the
sabotage test (*force-close with no harvest is refused; close after harvest
succeeds*) in [`tests/test_harvest_gate.py`](tests/test_harvest_gate.py). The
workflow marker and the predicate marker are **byte-identical**, so the workflow,
the spec, and the check cannot drift.

## The Linear API key — two paths

The workflow authenticates to the Linear GraphQL API (`https://api.linear.app/graphql`)
with a Linear **personal/workspace API key**, sent verbatim in the `Authorization`
header with **no `Bearer` prefix** (per the Linear API — an OAuth access token
would use `Bearer`; this workflow uses the raw-key form). The key needs write
access to the target team's issues: **update issue state**, **create comments**,
**add labels**, and **create a label** (only used the first time `needs-redispatch`
is applied on a team).

There are two ways to provision it. Whichever you use, the key is scoped to
**only** the Linear step inside the reusable — never present in the environment
of the arbitrary consumer `verify-command`, and **never written to a job or step
output**.

### RECOMMENDED — secret-free (Key Vault via WIF)

Set `linear-key-vault` to an Azure **Key Vault name**. The job then federates to
Azure via **Workload Identity Federation** (OIDC — no stored credential) and
reads the key from Key Vault **at run time, inside the Linear step**. The
consumer stores **no GitHub secret** at all — nothing to rotate in GitHub, and
one KV secret serves every consumer repo.

- The vault name and `linear-key-secret-name` (default **`ci-verify-and-close`**)
  are env-bound and handed to `az keyvault secret show` — never interpolated into
  a shell body (injection-safe). The fetched value is `::add-mask::`-ed the
  instant it is read.
- The caller job must grant **`id-token: write`** (for the OIDC federation) and
  pass the managed-identity coordinates via the `azure-client-id` /
  `azure-tenant-id` / `azure-subscription-id` inputs — typically the repo
  variables `vars.AZURE_CLIENT_ID` / `vars.AZURE_TENANT_ID` /
  `vars.AZURE_SUBSCRIPTION_ID` (the same WIF identity a repo already uses for
  `wif-azure-login`).
- Prereq: the WIF identity needs **Key Vault Secrets User** on the target vault,
  and a federated credential bound to the consumer repo's `main` /
  `pull_request` ref.

### FALLBACK — GitHub secret

Leave `linear-key-vault` empty and pass `secrets.LINEAR_API_KEY`. Store it as an
**Actions secret** in the consumer repo (or org-level, scoped to the consumer
repos), e.g. `gh secret set LINEAR_API_KEY --repo <owner>/<repo>`. This is the
pre-existing behaviour, kept for consumers without an Azure WIF identity.

## Wiring it (consumer)

### Secret-free (recommended) — Key Vault via WIF

```yaml
name: on-merge-to-main
on:
  push:
    branches: [main]

permissions:
  contents: read
  pull-requests: read # resolve the merged PR (branch + body) for the commit
  id-token: write # federate to Azure for the Key Vault fetch (no stored secret)

jobs:
  verify-and-close:
    uses: three-cubes/tc-pipelines/.github/workflows/verify-and-close.yml@v1
    with:
      verify-command: ./scripts/smoke.sh # your smoke / E2E / deploy-verify
      linear-key-vault: ${{ vars.KAIRIX_KV_NAME }} # or any KV name
      # linear-key-secret-name defaults to ci-verify-and-close
      azure-client-id: ${{ vars.AZURE_CLIENT_ID }}
      azure-tenant-id: ${{ vars.AZURE_TENANT_ID }}
      azure-subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
    # no `secrets:` block — the key is fetched from Key Vault at run time
```

### Fallback — GitHub secret

```yaml
name: on-merge-to-main
on:
  push:
    branches: [main]

permissions:
  contents: read
  pull-requests: read # resolve the merged PR (branch + body) for the commit

jobs:
  verify-and-close:
    uses: three-cubes/tc-pipelines/.github/workflows/verify-and-close.yml@v1
    with:
      verify-command: ./scripts/smoke.sh # your smoke / E2E / deploy-verify
    secrets:
      LINEAR_API_KEY: ${{ secrets.LINEAR_API_KEY }}
```

Common overrides:

| input | default | when to change |
|---|---|---|
| `verify-command` | *(required)* | your post-merge smoke / E2E / deploy-verify |
| `linear-key-vault` | *(empty)* | **recommended** — a KV name enables the secret-free WIF fetch |
| `linear-key-secret-name` | `ci-verify-and-close` | the KV secret name holding the Linear key |
| `azure-client-id` / `-tenant-id` / `-subscription-id` | *(empty)* | the WIF identity, required with `linear-key-vault` |
| `checkout` | `true` | `false` for a pure remote deploy-verify (no working tree) |
| `working-directory` | `.` | monorepo package path |
| `done-state` | `Done` | your team's terminal state name |
| `needs-fix-state` | `Todo` | the state the dispatcher re-picks from |
| `needs-redispatch-label` | `needs-redispatch` | your dispatcher's queue label |
| `needs-harvest-label` | `needs-harvest` | label applied when a close is refused for a missing harvest (SP-C-6) |
| `independent-verifier-verdict` | `pass` | wire SGO-169's conclusion once adopted |
| `fail-on-verify-failure` | `true` | `false` to keep the run green on verify-fail |
| `issue-id` / `branch-name` / `pr-body` | *(auto)* | non-standard triggers that don't map a merge commit to a PR |

## Injection-safety

Every caller-supplied input and every `github.event.*` field is **env-bound**
before it reaches a shell body — never interpolated into a `run:` string — so no
value can break out of its string context. Dynamic values injected into GraphQL
go through `jq --arg` (JSON-encoded), never string-spliced into a query. The
`verify-command` is executed as a deliberate `bash -c "$VERIFY_COMMAND"` and runs
**without** the Linear API key in its environment. The harvest is distilled in a
separate step with **no Linear key in scope**: the merged PR title/body, sha, and
verdicts are env-bound and written to a file with `printf '%s'` (never a runnable
position), and that file is injected into the harvest comment via `jq --arg` — so
untrusted PR text can break out of neither a shell nor a GraphQL string. On the secret-free path the
vault name and secret name reach `az keyvault secret show` as env values (never
interpolated), the fetched key is `::add-mask::`-ed the moment it is read, and it
stays a local shell variable inside the Linear step — never a job or step output.
Third-party actions are SHA-pinned.
