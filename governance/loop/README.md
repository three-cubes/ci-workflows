# governance/loop/ — the autonomous-delivery loop: guardrail harness (SP-C-1) + dispatcher (SP-C-3)

The machine-checkable half of the canonical spec
[`../autonomous-loop.md`](../autonomous-loop.md) and its decision record
[`../decisions/ADR-LOOP-STATE-MACHINE.md`](../decisions/ADR-LOOP-STATE-MACHINE.md).

- [`loop_state_machine.py`](loop_state_machine.py) — the explicit loop state machine
  (`ready → dispatched → in_review → verifying → done | needs_fix → escalated`) as a
  **deterministic** transition table, plus the guardrail engine that enforces the hard
  stop-conditions. The **judgment** transitions (spec-conformance / verification assessment) are an
  *injected* verdict seam, so the deterministic glue + guardrails are fully testable while the
  un-guaranteed agent judgment stays pluggable.
- [`tests/test_loop_guardrails.py`](tests/test_loop_guardrails.py) — the harness. It **simulates the
  loop and asserts each of the 5 guardrails fires**: (1) the N-retry ceiling halts + escalates; (2)
  the per-issue budget cap and global circuit-breaker stop dispatch; (3) determinism is enforced (a
  `--reruns` / networked / unpinned-seed "pass" can never reach `done`); (4) ambiguous /
  consecutive-failure verification escalates; (5) the dispatch rate-limit backs off and recovers
  (transient backpressure, not a terminal failure).
- [`loop_dispatcher.py`](loop_dispatcher.py) — the backlog **dispatcher** (SP-C-3 / PLA-311), the
  "pull next issue" leg. It selects the next READY work item(s) from the *Autonomous Delivery
  Platform* initiative and emits a **dispatch contract** the runtime consumes; it never spawns agents
  and never writes to Linear. It (1) queries the Linear-adapter snapshot for `Backlog`, unblocked
  candidates (an open `blocked-by` relation excludes an item, fail-closed); (2) orders them by the
  `adp-wave-N` label → priority → age (the READY queue); (3) gates emission through the
  `loop_state_machine` guardrail engine — `arm_auto_dispatch` must validate and every item passes the
  per-issue + global budget / circuit-breaker checks, **refusing to emit if not armed**; (4) emits
  each item as a `DispatchContract` (issue id, title, inferred repo, `<user>/<team>-<n>-<slug>`
  branch, acceptance-criteria pointer). The Linear transport is an injected seam (`IssueSource`), so
  selection/ordering/gating are testable offline and the same code runs live against Linear's GraphQL.
- [`tests/test_loop_dispatcher.py`](tests/test_loop_dispatcher.py) — the dispatcher tests
  (filtering, wave/priority/age ordering, the guardrail gate, repo/branch inference, the GraphQL
  snapshot parser, and the CLI), all exercised offline with an injected Linear source.

## Dispatcher CLI (`--dry-run` — the bootstrap-phase view)

Print the READY queue + what WOULD be dispatched, with **zero side effects** — no agents spawned, no
Linear writes. Run from `governance/loop/`:

```sh
# against a Linear-adapter snapshot (a list, or {"issues": [...]})
python3 -m loop_dispatcher --dry-run --limit 3 --issues-file snapshot.json

# against live Linear (stdlib urllib transport)
LINEAR_API_KEY=lin_api_… python3 -m loop_dispatcher --dry-run

# emit the machine dispatch contract instead of the human view
python3 -m loop_dispatcher --json --limit 1 --issues-file snapshot.json
```

Repo inference precedence: a `repo:<name>` label → the `**Repos:**` line in the description → a
`team-key → repo` fallback map. The dispatcher runs the guardrail harness in-process as its
lights-out-gate proof before arming, so a red harness means it emits nothing.

## The lights-out gate

**No auto-dispatch flag may flip to enabled until this harness is green.** `arm_auto_dispatch`
refuses to arm unless handed proof the guardrails validated — see the ADR. This is a CORE repo, so
per decision **D3** these files also take **n+1 human review** before merge.

## Run

```sh
python3 -m unittest discover -s governance/loop/tests -v
```

Stdlib only — no dependencies, no network (the transport + judgment seams are injected fakes),
matching the determinism the loop itself enforces.
