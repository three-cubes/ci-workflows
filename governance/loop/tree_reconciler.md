# tree_reconciler — the delegation-tree ⇄ Linear sub-issue-tree contract + reconciler

Module: [`tree_reconciler.py`](tree_reconciler.py) · tests:
[`tests/test_tree_reconciler.py`](tests/test_tree_reconciler.py)

This is **SP-C-7** (PLA-315) of the
[Autonomous Delivery Platform](https://linear.app/three-cubes/initiative/autonomous-delivery-platform-dae678e12c5d)
initiative, under Increment-3 "Shape-as-orchestrator + enforcing hooks". It is the
**reconcile** leg of the autonomous-delivery loop — the counterpart to the
dispatch side ([`loop_dispatcher.py`](loop_dispatcher.py)) and the close side
([`verify-and-close.md`](verify-and-close.md)).

## The tree contract

`specify → decompose → delegate` materialises a Linear issue/sub-issue tree that
**mirrors** the live agent delegation tree, and that tree is the control surface
(no human approval gate). The mirror invariant:

| delegation tree | Linear tree |
|---|---|
| a delegation (an agent doing work) | an issue |
| the delegating (parent) agent | the **parent** issue |
| a delegated task | a **sub-issue** (parent relation set to the delegator's issue) |
| the human accountable for the work | the issue **assignee** |
| the agent executing | the issue **delegate** |
| an in-flight delegation | a `started` (In Progress / In Review) issue |
| a real branch / PR | a linked PR attachment on the issue |

Over a long-running fleet the two trees **drift**. The reconciler reads the tree
plus the delegation state and the agent branch/PR list, and reports where they
disagree. It **does not mutate the tree destructively** — no reparenting, no state
changes, no deletes. It emits a report and *proposes* Linear annotations
(comments, a non-destructive annotation); wiring those to `save_comment` is an
explicit, optional live step kept out of the pure module.

## What it flags

| kind | meaning | acceptance-criterion |
|---|---|---|
| `orphan-work` | an in-flight issue with **no owning delegation** (`no-delegation`) or **no linked PR/branch** (`no-link`) | (a) |
| `unlinked-branch` | an agent branch/PR mapping to **no live work item** (`no-work-item`) or an issue not in the tree (`unknown-issue`) | (b), pairs with PLA-313 |
| `drift` | a sub-issue whose Linear parent ≠ the delegation-derived parent | (c) |
| `stale` | a started sub-issue whose delegation is **no longer live** (killed) | reconcile-drift |
| `missing-subissue` | an in-flight delegation with **no mirror** sub-issue | orphan-delegation |
| `overdue` | an in-flight issue past SLA — **In Progress > 3d**, **For Review > 2d** | overdue signal |

The two headline behaviours (the AC test): a matching 2-level delegation ⇄ 2-level
issue tree reconciles **clean**, and **killing a delegation marks its sub-issue
`stale` within one reconcile pass**.

## Design

Pure transform over three injected snapshots, mirroring the dispatcher's
discipline so detection is fully testable offline and the same code runs live:

- **reuses** [`loop_dispatcher.CandidateIssue.from_linear`](loop_dispatcher.py) for
  the base issue fields (id / state / title / url), so the mirror and the backlog
  dispatcher never drift on how a Linear issue is read;
- the Linear read is an injected seam (`TreeSource` / `HttpTreeSource`) with a pure
  `parse_tree_issues` verified against a canned GraphQL payload — never the wire;
- the delegation ledger and the branch/PR list are injected snapshots (the
  outcome-recorder export and the git side);
- stdlib only, no third-party deps, no network in tests.

### Secret-free live read (KV via WIF)

The live tree read uses the **same secret-free Linear key path** as
[`verify-and-close.md`](verify-and-close.md): the key is fetched from Key Vault via
Workload Identity Federation at run time (no stored GitHub secret), scoped to the
reconcile step, masked, and never written to an output. The CLI reads
`LINEAR_API_KEY` from the environment the workflow populates from Key Vault; the
key is sent verbatim in the `Authorization` header (no `Bearer` prefix — the
Linear personal/workspace-key form).

## CLI (`--dry-run` — the report-only view)

Runs on the stuck-session cron cadence. Report-only — **no Linear writes, no side
effects**. Run from `governance/loop/`:

```sh
# offline: a combined snapshot {issues, delegations, branches}
python3 -m tree_reconciler --dry-run --snapshot tree.json

# deterministic overdue clock for a fixed snapshot
python3 -m tree_reconciler --dry-run --snapshot tree.json --now 2026-07-02T12:00:00Z

# live: Linear tree read (KV-fetched key) + exported ledger / branch list
LINEAR_API_KEY=lin_api_… python3 -m tree_reconciler --dry-run \
    --delegations-file ledger.json --branches-file branches.json

# machine report (for cron / CI); gate a run red on drift with --fail-on-findings
python3 -m tree_reconciler --json --snapshot tree.json --fail-on-findings
```

By default the reconciler is a pure reporter (exit 0); `--fail-on-findings` makes a
non-clean report exit `4` so a cron/CI job can surface drift.

## Run

```sh
python3 -m unittest discover -s governance/loop/tests -v
```

This is a CORE repo, so per decision **D3** these files take **n+1 human review**
before merge.
