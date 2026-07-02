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
- [`loop_governor.py`](loop_governor.py) — the **runtime governor** (SP-C-4 / PLA-312). Where the
  harness proves each guardrail *can* fire, the governor is the runtime that *enforces* them during a
  live loop: it wraps a `dispatch → verify → close` cycle and, on any breach, HALTS rather than
  silently continuing, opening an escalation to the human-accountable assignee. It **consumes**
  `loop_state_machine` directly — the same `GuardrailConfig` ceilings, `admit_verification`
  determinism check, `arm_auto_dispatch` lights-out gate, and `GuardrailTripped` vocabulary — so the
  runtime and the spec cannot drift. It records per-issue attempt/cost state (`IssueLedger`) and
  exposes `should_continue()` / `record_attempt()` / `record_cost()` / `escalate()`; it **refuses to
  run** unless armed. It enforces (1) the N-retry ceiling, (2) the per-issue budget cap, (3) the
  global budget cap, (4) a cross-issue circuit-breaker (repeated failures *across* issues halt all
  dispatch — a systemic red, distinct from the budget breaker), (5) determinism (a `--reruns` /
  networked / unpinned-seed "pass" can never close — it escalates), and (6) ambiguous / repeated
  failure → escalate. Escalation goes through an injected `EscalationSink` (the Linear adapter +
  PushNotification in production; a recording fake in tests) **exactly once** per issue (idempotent);
  budget counters are fed by `record_cost()` off the token-logger spend path (SGO-44).
- [`harvest_gate.py`](harvest_gate.py) — the **harvest-present close-invariant** (SP-C-6 / PLA-314),
  promoting ADR-038 (harvest-then-decay / harvest-before-destroy) into a CORE close-invariant: an ADP
  issue may not sit in a terminal `done`/`completed` state without a distilled **harvest artifact**
  recorded on it (*no close / teardown without a harvest*). It is the machine-checkable predicate half
  of the invariant that [`../../.github/workflows/verify-and-close.yml`](../../.github/workflows/verify-and-close.yml)
  enforces at run time (it distils + posts the harvest comment BEFORE the Done transition, fail-closed
  if it cannot). `has_harvest` / `assert_closed_carries_harvest` are **provenance-stamped** (keyed on
  the issue id) and **idempotent**, and key off the SAME `<!-- adp-harvest issue=… -->` marker the
  workflow writes, so the spec, the workflow, and the check cannot drift. Stdlib only, no network.
- [`tests/test_harvest_gate.py`](tests/test_harvest_gate.py) — the close-invariant harness. It encodes
  the PLA-314 **sabotage test** (*force-close with no harvest is refused; close after a harvest
  succeeds*) plus the provenance/idempotency properties of the predicate — a harvest stamped for
  another issue never satisfies this one, an open issue is exempt (the gate fires only at the close
  boundary), and duplicate harvests still read as present.
- [`tests/test_loop_governor.py`](tests/test_loop_governor.py) — the governor tests. They simulate
  each failure mode against a governed cycle and assert the governor **halts/escalates** — the
  never-greening issue escalates at the ceiling, the per-issue cap escalates that item, the global
  cap and the cross-issue circuit-breaker halt the fleet, a non-deterministic "pass" never closes,
  and an ambiguous verdict escalates — and that every escalation is emitted to the human **exactly
  once** (idempotent). It also asserts the governor refuses to run until `arm_auto_dispatch` validated.

- [`loop_runner.py`](loop_runner.py) — the **continuous-dispatch driver** (the runner), the piece
  that replaces the human hand-cranking the loop. On a cadence (the scheduled
  [`loop-dispatch.yml`](../../.github/workflows/loop-dispatch.yml) workflow) `Runner.run_once(sink,
  dry_run)` (0) loads the **durable** governor ledger from a `StateStore` (the `loop-state` git ref)
  BEFORE any guardrail check, so retry/budget/breaker counters accumulate ACROSS the fresh-process
  ticks; (1) refuses unless the governor is armed **and** the guardrail harness re-validates this
  tick (fail-closed — a missing/renamed harness is not green); (2) queries READY via
  `loop_dispatcher`; (3) applies the **governor** (retry ceiling, per-issue + global budget,
  cross-issue circuit-breaker) BEFORE selecting; (4) selects the next item; (5) records the
  `DispatchContract`; (6) dispatches only when armed AND not dry-run AND **past the soak window**,
  persisting the ledger before handing it to `sink.dispatch`. Every guardrail breach short-circuits
  to `REFUSED` / `HALTED` — never dispatch. The spawn seam is a pluggable `DispatchSink`; the safe
  default (`LoggingDispatchSink`) records the contract and **spawns nothing**, and three documented
  STUB sinks (`GitHubActionsHeadlessSink`, `AgentPlatformSink`, `LinearDelegationSink`) are the
  candidate runtimes the org will choose between (none implemented yet — each raises
  `NotImplementedError`). See [`ARMING.md`](ARMING.md) for the arm / soak / disarm / kill-switch /
  escalation runbook.
- [`tests/test_loop_runner.py`](tests/test_loop_runner.py) — the runner tests, including the
  fail-safe cases (disarmed, red harness, per-issue/global budget breach, dry-run → **no dispatch**;
  armed + logging sink → the seam is reached but **logs only**) and the CLI.
- [`tests/test_loop_hardening.py`](tests/test_loop_hardening.py) — the enforceability proofs: the
  retry ceiling, per-issue + global budget, and cross-issue circuit-breaker all trip **across
  separate fresh-process ticks** through the durable `JsonFileStateStore` (with a control showing
  the non-durable store never trips); `guardrails_validated` fails **closed** on a missing / renamed
  / truncated harness; and the **soak** keeps the first N armed ticks record-only so a cold armed
  tick never dispatches.

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
