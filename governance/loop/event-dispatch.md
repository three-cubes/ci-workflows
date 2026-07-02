# event-dispatch — the failure-driven open side of the autonomous delivery loop

Reusable workflow: [`.github/workflows/event-dispatch.yml`](../../.github/workflows/event-dispatch.yml)
(`on: workflow_call`).

This is **SP-C-2** (decision **D5**) of the
[Autonomous Delivery Platform](https://linear.app/three-cubes/initiative/autonomous-delivery-platform-dae678e12c5d)
initiative — the automation that turns a **CI failure** or a **PR review-comment**
into the **next action, with no human re-prompt**. Where
[`verify-and-close`](./verify-and-close.md) (SP-C-3) is the deterministic **close**
side, this is the event-driven **open** side. It closes the failure loop: an
agent PR goes red (or a reviewer leaves a comment), and this workflow
deterministically annotates the linked Linear issue and **emits the re-dispatch
signal** so the work is re-picked — with no per-failure human approval gate
(Increment-3 "Shape-as-orchestrator + enforcing hooks").

## What it emits (and what it does not)

This workflow **emits the re-dispatch signal only**: it annotates the Linear
issue, applies the `needs-redispatch` label, and moves the issue to a needs-fix
(backlog-type) state. That label + state **is** the signal.

It does **not** spawn an agent itself. The signal is consumed by the **backlog
dispatcher** ([`governance/loop/loop_dispatcher.py`](./loop_dispatcher.py)), whose
`CANDIDATE_STATE_TYPE` is `backlog` — it re-picks the labelled, backlog-state
issue on its next pass and drives the fixing agent through
[`loop_state_machine.py`](./loop_state_machine.py)'s guardrails. Agent dispatch
and the storm/dispatch-cap (a burst of reds coalescing to one escalation) live on
that dispatcher / SP-C-1 side, **not** here. Keeping emit and dispatch separate
means this workflow has no way to manufacture an agent loop on its own.

**Idempotent by construction.** Emitting the signal is a set of idempotent
writes: adding a label already present is a no-op, and moving to the state the
issue is already in is a no-op. So re-delivery of the same event (a retried
`workflow_run`, a duplicate webhook) re-asserts the same signal rather than
double-dispatching — the dispatcher's own dedup then decides the single next
action.

## The contract

A consumer wires a thin **trigger** workflow that fires on the failure event and
`uses:` this reusable, forwarding the event fields as inputs. The reusable then,
in order:

1. **Resolves the linked Linear issue id.** Precedence:
   1. the `issue-id` input, if set;
   2. the PR head branch, parsed against the org convention
      `<user>/<team>-<number>-<slug>` (e.g. `dan/pla-310-...` => `PLA-310`). The
      branch is taken directly (`workflow_run` carries `head_branch`;
      `pull_request_review` carries `pull_request.head.ref`) or read from the PR
      for `pr-number` (an `issue_comment` event carries only the PR number);
   3. the PR body, scanned for an explicit identifier (e.g. `PLA-310`).
   - If **none** resolve, the workflow emits a **documented SKIP** with `fix:` /
     `next:` guidance and does **not** touch Linear — annotating the wrong issue
     is worse than a skip (mirrors the verify-and-close deterministic-fallback
     rule). The unresolved branch is also the natural **agent-PR scope guard**: a
     branch that does not parse to an issue id is silently skipped.
2. **Posts a structured annotation** onto the issue:
   - `event-kind: ci-failure` — the failing check name (`failing-check`), the
     failure-log link (`failure-log-url`), and an optional first-actionable
     `fix:` line (`failure-summary`);
   - `event-kind: review-comment` — the comment author (`comment-author`), the
     thread link (`comment-url`), and the **comment body itself**
     (`comment-body`).
3. **Emits the re-dispatch signal** — moves the issue to `needs-fix-state`
   (default `Backlog`, falling back to the team's first `backlog` then
   `unstarted` state) and adds the `needs-redispatch` label (created on the
   issue's team if absent). The backlog dispatcher picks it up from there — the
   failure is **never silently dropped** (SP-C failure-driven-next).

## Wiring it (consumer)

The consumer owns the `on:` triggers; this reusable is trigger-agnostic. Scope
the triggers to **agent PRs** and, for comments, **filter out the agent's own
comments** to avoid a self-reply loop (e.g.
`if: github.event.comment.user.login != 'three-cubes-agent[bot]'`).

### CI-red — `on: workflow_run` (secret-free, recommended)

```yaml
name: on-ci-red
on:
  workflow_run:
    workflows: ["python-quality-gate"] # the CI workflow(s) to watch
    types: [completed]

permissions:
  contents: read
  pull-requests: read # resolve the PR (branch + body)
  id-token: write # federate to Azure for the Key Vault fetch (no stored secret)

jobs:
  on-red:
    if: github.event.workflow_run.conclusion == 'failure'
    uses: three-cubes/tc-pipelines/.github/workflows/event-dispatch.yml@v1
    with:
      event-kind: ci-failure
      branch-name: ${{ github.event.workflow_run.head_branch }}
      failing-check: ${{ github.event.workflow_run.name }}
      failure-log-url: ${{ github.event.workflow_run.html_url }}
      linear-key-vault: ${{ vars.KAIRIX_KV_NAME }}
      azure-client-id: ${{ vars.AZURE_CLIENT_ID }}
      azure-tenant-id: ${{ vars.AZURE_TENANT_ID }}
      azure-subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
    # no `secrets:` block — the key is fetched from Key Vault at run time
```

### Review-comment — `on: issue_comment` (fallback GitHub secret)

```yaml
name: on-review-comment
on:
  issue_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: read # read the PR head branch for issue resolution

jobs:
  on-comment:
    # only PR comments, and never the agent's own (avoid a self-reply loop)
    if: >-
      github.event.issue.pull_request != null &&
      github.event.comment.user.login != 'three-cubes-agent[bot]'
    uses: three-cubes/tc-pipelines/.github/workflows/event-dispatch.yml@v1
    with:
      event-kind: review-comment
      pr-number: ${{ github.event.issue.number }}
      comment-author: ${{ github.event.comment.user.login }}
      comment-url: ${{ github.event.comment.html_url }}
      comment-body: ${{ github.event.comment.body }} # env-bound in the reusable
    secrets:
      LINEAR_API_KEY: ${{ secrets.LINEAR_API_KEY }}
```

A `pull_request_review` trigger is wired the same way, forwarding
`github.event.pull_request.head.ref` as `branch-name`,
`github.event.review.body` as `comment-body`, and
`github.event.review.html_url` as `comment-url`.

Common overrides:

| input | default | when to change |
|---|---|---|
| `event-kind` | `ci-failure` | `review-comment` for the review-thread shape |
| `branch-name` | *(empty)* | the PR head branch (workflow_run / review triggers) |
| `pr-number` | *(empty)* | read the branch + body from the PR (issue_comment) |
| `failing-check` / `failure-log-url` / `failure-summary` | *(empty)* | the CI-failure annotation fields |
| `comment-body` / `comment-url` / `comment-author` | *(empty)* | the review-comment annotation fields |
| `issue-id` / `pr-body` | *(auto)* | non-standard triggers that don't map to a PR/branch |
| `needs-fix-state` | `Backlog` | your dispatcher's re-pick state (**must** be backlog-type) |
| `needs-redispatch-label` | `needs-redispatch` | your dispatcher's queue label |
| `linear-key-vault` | *(empty)* | **recommended** — a KV name enables the secret-free WIF fetch |
| `linear-key-secret-name` | `ci-verify-and-close` | the KV secret name holding the Linear key |
| `azure-client-id` / `-tenant-id` / `-subscription-id` | *(empty)* | the WIF identity, required with `linear-key-vault` |

## The Linear API key — two paths

Identical to the [`verify-and-close`](./verify-and-close.md#the-linear-api-key--two-paths)
contract (this workflow shares the same key + KV secret). The key authenticates to
the Linear GraphQL API (`https://api.linear.app/graphql`) with a
personal/workspace API key sent verbatim in the `Authorization` header with **no
`Bearer` prefix**. It needs write access to the target team's issues: **update
issue state**, **create comments**, **add labels**, and **create a label** (only
the first time `needs-redispatch` is applied on a team). Whichever path is used,
the key is scoped to **only** the Linear step and **never written to a job or
step output**.

### RECOMMENDED — secret-free (Key Vault via WIF)

Set `linear-key-vault` to an Azure **Key Vault name**. The job federates to Azure
via **Workload Identity Federation** (OIDC — no stored credential) and reads the
key from Key Vault **at run time, inside the Linear step**. The consumer stores
**no GitHub secret**.

- The vault name and `linear-key-secret-name` (default **`ci-verify-and-close`**)
  are env-bound and handed to `az keyvault secret show` — never interpolated into
  a shell body (injection-safe). The fetched value is `::add-mask::`-ed the
  instant it is read.
- The caller job must grant **`id-token: write`** (for the OIDC federation) and
  pass the managed-identity coordinates via the `azure-client-id` /
  `azure-tenant-id` / `azure-subscription-id` inputs — typically the repo
  variables `vars.AZURE_CLIENT_ID` / `vars.AZURE_TENANT_ID` /
  `vars.AZURE_SUBSCRIPTION_ID`.
- Prereq: the WIF identity needs **Key Vault Secrets User** on the target vault,
  and a federated credential bound to the consumer repo.

### FALLBACK — GitHub secret

Leave `linear-key-vault` empty and pass `secrets.LINEAR_API_KEY`. Store it as an
**Actions secret** in the consumer repo, e.g.
`gh secret set LINEAR_API_KEY --repo <owner>/<repo>`. Kept for consumers without
an Azure WIF identity.

## Injection-safety

Every caller-supplied input and every `github.event.*` field — the
**review-comment body above all** — is **env-bound** before it reaches a shell
body, never interpolated into a `run:` string, so no value can break out of its
string context. Dynamic values injected into GraphQL go through `jq --arg`
(JSON-encoded), never string-spliced into a query. On the secret-free path the
vault name and secret name reach `az keyvault secret show` as env values (never
interpolated), the fetched key is `::add-mask::`-ed the moment it is read, and it
stays a local shell variable inside the Linear step — never a job or step output.
Third-party actions are SHA-pinned.
