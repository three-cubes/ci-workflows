# ARMING — the runbook for the continuous-dispatch driver

How the autonomous-delivery loop's **continuous-dispatch driver** is armed,
disarmed, and escalated. The driver is
[`loop_runner.py`](loop_runner.py), run on a cadence by
[`.github/workflows/loop-dispatch.yml`](../../.github/workflows/loop-dispatch.yml).
It is the piece that replaces the human hand-cranking the loop: each tick it
self-selects the next READY work item, guardrail-gates it, and — **only when
armed and live** — hands it to an agent-spawn seam.

> **Arming is currently SAFE.** The spawn seam is a
> [`LoggingDispatchSink`](loop_runner.py) — it records the dispatch contract and
> **spawns nothing**. No real `DispatchSink` is wired, so even a fully armed,
> live tick only **logs** the selection. Arming today changes what gets logged,
> not what runs. A real spawn cannot happen until the org picks a runtime and
> wires one of the stub sinks (below). Until then, arming has no blast radius.

> **⛔ DO NOT WIRE A REAL `DispatchSink` UNTIL THE ENFORCEABILITY PRECONDITIONS
> HOLD.** A live sink on top of inert guardrails is a runaway. The guardrails are
> only enforceable because three hardening fixes are in place; a future reviewer
> MUST NOT swap a live sink onto the runner until all three still hold:
> 1. **Durable cross-tick ledger** — the governor loads/persists a
>    [`StateStore`](loop_governor.py) each tick (a versioned JSON blob on the
>    dedicated `loop-state` git ref / an Azure blob — never the evictable Actions
>    cache). Without it the retry/budget/circuit-breaker counters reset every
>    fresh-process tick and never trip. An armed **live** tick with no durable
>    store is **refused** by the runner (fail-closed).
> 2. **Fail-closed harness validation** — [`guardrails_validated`](loop_dispatcher.py)
>    returns `True` only when the harness file exists AND at least the
>    known-minimum number of guardrail tests ran green; a missing/renamed/
>    truncated harness reads as **not validated** (never vacuously green).
> 3. **Soak before live** — the first `LOOP_SOAK_TICKS` armed ticks stay
>    record-only, so a cold armed tick can never dispatch (see the soak section).
>
> These are covered by `tests/test_loop_hardening.py`; that harness must stay
> green before precondition 4 (a real sink) is signed off.

This is a **CORE** repo, so per decision **D3** every change here — including
flipping the arming variable's governance — takes **n+1 human review**.

## What "armed" means (and does not)

Three gates must ALL hold before a **scheduled** tick can dispatch for real, and a
fourth (soak) delays even that:

1. **`LOOP_ARMED == "true"`** — a repo variable (the operator's first switch).
2. **The guardrail harness self-validates THIS tick** — the runner re-runs the
   proof ([`arm_auto_dispatch`](loop_state_machine.py)) live, now **fail-closed**
   (the harness file must exist and enough guardrail tests must run green); a red
   or missing harness `REFUSES` regardless of the variables.
3. **`LOOP_LIVE == "true"`** — a **second, independent** repo variable (the "go
   live" key). Until it is set, an armed scheduled tick stays **dry-run** so the
   operator can review the recorded would-dispatch decisions (the soak).
4. **Past the soak window** — even with 1–3, the runner keeps the first
   `LOOP_SOAK_TICKS` armed ticks record-only; only after that can it dispatch.

Even with all four, the runner reaches only the **`LoggingDispatchSink`**. So
today "armed + live + past soak" == "logs the selection it would dispatch, with
the governor's **durable** ledger advancing across ticks (persisted to the
`loop-state` ref, so the ceilings actually trip tick-to-tick)". There is no path
to a real spawn yet.

## PRECONDITIONS — all four before arming for real

Do not set `LOOP_ARMED=true` with intent to dispatch for real until **every** one
of these holds. (Arming while the sink is still `LoggingDispatchSink` is safe and
is a useful shakedown — it exercises selection + gating with zero blast radius.)

| # | Precondition | Evidence |
|---|---|---|
| 1 | **First cycle proven** — the dispatch → verify → close loop has completed at least one end-to-end cycle by hand, with a real merge closed via [`verify-and-close`](verify-and-close.md). | A closed Linear issue with a `verification-confirmed` comment. |
| 2 | **Guardrail harness validated (fail-closed)** — `python3 -m unittest discover -s governance/loop/tests` is green (it IS the lights-out gate; `arm_auto_dispatch` refuses without it). `guardrails_validated` now also refuses a missing/renamed/truncated harness. | Green harness on `main`; the runner re-confirms it every tick. |
| 3 | **Budget + retry caps configured** — the ceilings below are set deliberately for this fleet + cost model, not left at defaults by accident. | The `GuardrailConfig` in force (see the table); reviewed in the arming PR. |
| 3a | **Durable cross-tick ledger wired** — the workflow materialises the `loop-state` git ref into `--state-file` and writes it back, so the governor's retry/budget/circuit-breaker counters accumulate across the fresh-process ticks (they are inert otherwise). An armed **live** tick with no durable store is refused. | The restore/persist steps in [`loop-dispatch.yml`](../../.github/workflows/loop-dispatch.yml); the cross-tick tests in `tests/test_loop_hardening.py` green. |
| 3b | **Soak window configured** — `LOOP_SOAK_TICKS` set so the first N armed ticks stay record-only; the first armed cron tick never dispatches. | `LOOP_SOAK_TICKS` repo variable (default 3); the soak tests green. |
| 4 | **A real `DispatchSink` chosen + wired** — exactly one of the stub seams is implemented and swapped in for `LoggingDispatchSink`, with its own review + tests, **and only after 3a + 3b + fail-closed 2 are confirmed still in place** (a live sink on inert guardrails is a runaway). | A merged PR implementing the chosen sink; the CLI/workflow updated to use it; `test_loop_hardening.py` still green. |

Preconditions 1–3b are met today. **Precondition 4 is deliberately NOT met** —
that is why arming is currently safe.

### Choosing the real spawn seam (precondition 4)

Three documented STUB candidates ship in [`loop_runner.py`](loop_runner.py), each
raising `NotImplementedError` with a wiring note. The org picks exactly one:

- **`GitHubActionsHeadlessSink`** — a scheduled/`workflow_dispatch` worker runs
  `claude -p` headless per item; the API key comes from Key Vault (KV+WIF, no
  stored secret). No standing host; inherits Actions concurrency + audit.
- **`AgentPlatformSink`** — hand the contract to the standing
  `tc-agent-zone` / `vm-openclaw` runner (the persistent OpenClaw gateway that
  already enforces the subagent-spawning decision hook and feeds the spend path).
- **`LinearDelegationSink`** — set the Linear `delegate = agent` so Linear's
  native agent integration picks the item up; dispatch becomes one idempotent
  Linear mutation.

## ARM — the exact steps

1. **Validate the harness.** Confirm it is green on `main`:

   ```sh
   python3 -m unittest discover -s governance/loop/tests -v
   ```

2. **Set the arming variable.** Flip the repo variable to exactly `true`:

   ```sh
   gh variable set LOOP_ARMED --repo three-cubes/tc-pipelines --body true
   ```

   Optionally configure the secret-free Linear read (recommended) and the
   source initiative (the runner selects one READY item per tick):

   ```sh
   gh variable set LOOP_LINEAR_KEY_VAULT --repo three-cubes/tc-pipelines --body <kv-name>
   # LOOP_LINEAR_KEY_SECRET_NAME defaults to ci-verify-and-close
   # LOOP_INITIATIVE defaults to the ADP initiative id
   # AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_SUBSCRIPTION_ID = the repo WIF identity
   ```

3. **Configure the soak window (recommended).** How many armed ticks stay
   record-only before live is permitted (default `3` if unset):

   ```sh
   gh variable set LOOP_SOAK_TICKS --repo three-cubes/tc-pipelines --body 3
   ```

4. **Enable the schedule.** Ensure the
   [`loop-dispatch`](../../.github/workflows/loop-dispatch.yml) workflow is
   enabled (Actions tab → `loop-dispatch` → *Enable workflow* if it was disabled).
   The `*/30` cron then ticks every 30 minutes; each tick runs the runner. With
   only `LOOP_ARMED` set, armed ticks stay **dry-run** and **soak** (record the
   would-dispatch decision to the run log for review) — nothing goes live yet.

5. **Review the soak, then flip the second key.** After watching the recorded
   would-dispatch decisions for at least `LOOP_SOAK_TICKS` armed ticks, set the
   **second, independent** go-live key:

   ```sh
   gh variable set LOOP_LIVE --repo three-cubes/tc-pipelines --body true
   ```

   Only now do scheduled ticks leave dry-run — and only once the runner's own
   soak counter (persisted in the `loop-state` ref) has also elapsed, so the
   very first armed tick still cannot dispatch. While the sink is still
   `LoggingDispatchSink`, even live ticks **log only**.

A scheduled tick goes live only when **`LOOP_ARMED` AND `LOOP_LIVE` are both
`true` AND the soak has elapsed**. A **manual** run (`workflow_dispatch`) stays
dry-run unless you set its `dry_run` input to `false` **and** both keys are set —
a human must explicitly opt a manual tick into live on top of both repo keys.

## SOAK — the mandatory dry-run before live

After `LOOP_ARMED` flips true, the scheduled path does **not** go live on the next
tick. Two layers enforce a soak so an operator can review "what would dispatch"
before anything real happens, and the very first armed cron tick can never go
live:

- **The `LOOP_LIVE` second key.** Scheduled ticks stay dry-run until the operator
  ALSO sets `LOOP_LIVE=true` — a deliberate, separate confirmation made *after*
  reviewing the recorded would-dispatch decisions.
- **The runner's soak counter.** Even once `LOOP_LIVE=true`, the runner keeps the
  first `LOOP_SOAK_TICKS` armed ticks record-only, incrementing a counter that
  **persists in the `loop-state` ref** (so the window elapses across the
  fresh-process ticks, not per-process). Only after the counter reaches
  `LOOP_SOAK_TICKS` can a tick dispatch. This holds even if an operator sets both
  keys at once — the cold armed tick still soaks.

Each soak tick records the `DispatchContract` it WOULD dispatch (visible in the
run log / JSON), with zero side effects. Manual `workflow_dispatch` runs stay
dry-run by default regardless.

## DISARM / KILL-SWITCH

Any one of these stops the loop; **it takes effect on the next tick**:

- **Set `LOOP_LIVE` to anything but `true`** (revert to soak/dry-run without
  fully disarming):

  ```sh
  gh variable set LOOP_LIVE --repo three-cubes/tc-pipelines --body false
  ```

- **Set `LOOP_ARMED` to anything but `true`** (the fast kill-switch):

  ```sh
  gh variable set LOOP_ARMED --repo three-cubes/tc-pipelines --body false
  ```

  The next tick sees `armed=false` and reverts to dry-run (record-only).

- **Disable the workflow** (Actions tab → `loop-dispatch` → *Disable workflow*),
  or delete/comment the `schedule:` trigger. No further ticks fire.

**In-flight work.** Today there is nothing in flight to stop: the sink spawns
nothing. **Once a real `DispatchSink` is wired**, disarming stops *new* dispatch
on the next tick; already-spawned agents are not force-killed by the switch —
they either finish their current item or are stopped out-of-band (cancel the
worker run for `GitHubActionsHeadlessSink`; stop the agent on the platform for
`AgentPlatformSink`; un-delegate the Linear issue for `LinearDelegationSink`).
The kill-switch is fail-safe for *dispatch*, not a remote agent-abort — that is
the chosen runtime's responsibility, and the sink's wiring note must document it
before precondition 4 is signed off.

## ESCALATION path

The driver never guesses and never loops forever. A guardrail breach
short-circuits the tick and hands off to a human:

- **Per-item breach** (retry ceiling reached, or per-issue budget cap) — the
  runner **skips** the item this tick and does not dispatch it. The governor's
  [`escalate`](loop_governor.py) opens a **Linear escalation to the
  human-accountable assignee exactly once** (idempotent) via its
  `EscalationSink` (Linear + PushNotification in production) on the verify/close
  side of the loop.
- **Fleet-wide breach** (global budget cap, or the cross-issue circuit-breaker) —
  the runner returns `HALTED` and dispatches nothing fleet-wide; a single
  fleet-level escalation is opened to the human.
- **Ambiguous / non-deterministic verification** — handled on the close side
  ([`verify-and-close`](verify-and-close.md) + the governor's determinism
  admission): a `--reruns` / networked / unpinned-seed "pass" can never close; it
  escalates rather than advancing.

Escalation always terminates at a **human** (assignee), who decides whether to
re-scope, re-dispatch, or drop the item. There is no auto-retry past the ceiling.

## Ceilings in force

The runtime ceilings the governor enforces (the
[`GuardrailConfig`](loop_state_machine.py) defaults — tune per fleet + cost model
in the arming PR, per precondition 3):

| Ceiling | Default | Behaviour on breach |
|---|---|---|
| **Retry ceiling** (per issue) | `retry_ceiling = 3` | escalate the item to the human (no further fix cycle) |
| **Per-issue budget cap** | `per_issue_budget = 5.0` | escalate *that* item |
| **Global budget cap** (fleet circuit-breaker) | `global_budget = 100.0` | **halt all dispatch** fleet-wide |
| **Cross-issue circuit-breaker** | `circuit_breaker_threshold = 5` consecutive cross-issue failures | **halt all dispatch** fleet-wide (systemic red) |
| **Determinism** | `--reruns` / network / unpinned seed all banned | inadmissible — can never close; escalate |
| **Dispatch rate-limit** | `dispatch_rate_max = None` (off) / `window = 3600s` | back off + retry (transient backpressure, not a failure) |
| **Soak before live** | `LOOP_SOAK_TICKS = 3` (workflow default) | first N armed ticks stay record-only; a cold armed tick never dispatches |

The retry ceiling is what bounds a mis-wired source — **but only because the
ledger is durable**. Each scheduled tick is a fresh process, so an in-memory-only
governor would forget every attempt and re-dispatch the same item forever. The
runner therefore loads the durable [`StateStore`](loop_governor.py) (the
`loop-state` git ref) at the start of each tick and persists it after a dispatch,
so the accumulated `attempts` cross ticks: even if the backlog snapshot keeps
returning the same item, the governor stops re-dispatching it after
`retry_ceiling` attempts *across ticks* and escalates. The same durability is what
makes the per-issue budget, the global budget, and the cross-issue circuit-breaker
actually trip tick-to-tick (proven in `tests/test_loop_hardening.py`).

## References

- [`loop_runner.py`](loop_runner.py) · [`loop_governor.py`](loop_governor.py) ·
  [`loop_dispatcher.py`](loop_dispatcher.py) · [`loop_state_machine.py`](loop_state_machine.py)
- [`../autonomous-loop.md`](../autonomous-loop.md) (the canonical spec + the
  lights-out gate) · [`../decisions/ADR-LOOP-STATE-MACHINE.md`](../decisions/ADR-LOOP-STATE-MACHINE.md)
- [`verify-and-close.md`](verify-and-close.md) (the close side + the KV+WIF
  secret-free Linear pattern this driver mirrors)
- Durable cross-tick ledger: the `loop-state` git ref (`refs/loop-state/ledger`),
  materialised into `--state-file` by [`loop-dispatch.yml`](../../.github/workflows/loop-dispatch.yml);
  the enforceability proofs live in [`tests/test_loop_hardening.py`](tests/test_loop_hardening.py).
- Linear: Increment 3 (`d4b8c682-a2cf-40e6-be44-86960d1505cd`); SP-C (PLA-309–315).
