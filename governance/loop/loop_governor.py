"""Runtime loop GOVERNOR (SP-C-4 / PLA-312).

Where :mod:`loop_state_machine` *defines* the autonomous-delivery loop's states +
hard stop-conditions (and its harness proves each guardrail *can* fire), this
module is the **runtime** that ENFORCES them during a live loop. It wraps a
``dispatch → verify → close`` cycle and, on any breach, HALTS rather than silently
continuing, opening an escalation to the human-accountable assignee.

It consumes :mod:`loop_state_machine` directly — the same :class:`GuardrailConfig`
ceilings, the same determinism admission (:meth:`LoopEngine.admit_verification`),
the same lights-out arming gate (:meth:`LoopEngine.arm_auto_dispatch`), and the
same :class:`GuardrailTripped` exception vocabulary — so the runtime and the spec
cannot drift.

What it governs (each maps to a spec stop-condition, ``governance/autonomous-loop.md``):

1. **N-retry ceiling** — after ``retry_ceiling`` failed attempts on one issue it
   halts + escalates instead of a further fix cycle (never loops forever).
2. **Per-issue budget cap** — cumulative cost/tokens per issue ≤ cap; a projected
   or recorded overshoot escalates *that* item.
3. **Global budget cap (circuit-breaker)** — fleet-wide spend ≤ global cap;
   overshoot halts **all** dispatch.
4. **Cross-issue circuit-breaker** — repeated failures *across* issues (a systemic
   red, not one flaky item) halt all dispatch, distinct from the budget breaker.
5. **Determinism** — a "pass" that used test ``--reruns`` / network / an unpinned
   seed is inadmissible: it can never close; it escalates ("green ≠ correct").
6. **Ambiguous / repeated failure ⇒ escalate to a human** — never guess, never
   re-dispatch blindly.

It records per-issue attempt/cost state (:class:`IssueLedger`) and exposes the
runtime surface: :meth:`Governor.should_continue`, :meth:`Governor.record_attempt`,
:meth:`Governor.record_cost`, and :meth:`Governor.escalate`. It **refuses to run**
unless :meth:`Governor.arm` validated (the lights-out gate).

Escalation is emitted through an injected :class:`EscalationSink` (the Linear
adapter + PushNotification in production; a recording fake in tests) — **exactly
once** per issue (idempotent). Budget counters are fed by :meth:`record_cost` off
the token-logger spend path (SGO-44). Stdlib only, no network — the same
discipline the loop enforces.

Run the harness (this module's tests included)::

    python3 -m unittest discover -s governance/loop/tests -v
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Protocol

# ``loop_state_machine`` is a sibling module (this dir is not a package — the tests
# use the same path shim). Import it whether we run as a script from the repo root
# or with this dir on ``sys.path``.
try:  # pragma: no cover - exercised both ways depending on invocation
    import loop_state_machine as loop
except ModuleNotFoundError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import loop_state_machine as loop

from loop_state_machine import (
    BudgetExceeded,
    DeterminismViolation,
    GuardrailConfig,
    GuardrailTripped,
    IllegalTransition,
    LoopEngine,
    RetryCeilingExceeded,
    Verdict,
    VerificationRun,
)

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
#: Consecutive cross-issue failures that open the fleet circuit-breaker. Distinct
#: from the per-issue retry ceiling (guardrail 1) and the global budget breaker
#: (guardrail 3): this trips on a *systemic* red — many issues failing in a row.
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 5

#: Fallback assignee for an escalation when the caller does not name the
#: human-accountable owner. Production injects the real Linear assignee.
DEFAULT_ESCALATION_ASSIGNEE = "human-accountable"


# --------------------------------------------------------------------------- #
# Errors — the governor reuses the state-machine vocabulary and adds one type
# --------------------------------------------------------------------------- #
class CircuitBreakerOpen(GuardrailTripped):
    """The cross-issue circuit-breaker is open — all dispatch is halted fleet-wide
    after repeated failures across issues (a systemic red, not one flaky item)."""

    scope = "circuit"


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
class ContinueAction(str, Enum):
    """What a caller should do given a :meth:`Governor.should_continue` verdict."""

    CONTINUE = "continue"  # proceed with the cycle
    ESCALATE = "escalate"  # this issue is done for — escalate to a human
    HALT = "halt"  # fleet-wide stop (global budget / circuit-breaker)
    REFUSE = "refuse"  # auto-dispatch is not armed (lights-out gate closed)
    DONE = "done"  # this issue already completed — nothing to do


class Outcome(str, Enum):
    """The terminal result of one governed cycle."""

    DONE = "done"  # verified deterministic pass — closed/merged
    NEEDS_FIX = "needs_fix"  # verified fail, under the ceiling — retry
    ESCALATED = "escalated"  # a stop-condition fired — handed to a human
    HALTED = "halted"  # fleet-wide stop — this issue was not attempted
    REFUSED = "refused"  # not armed — the loop did not run


#: Outcomes that end the driver loop (only ``NEEDS_FIX`` re-dispatches).
_TERMINAL_OUTCOMES = frozenset(
    {Outcome.DONE, Outcome.ESCALATED, Outcome.HALTED, Outcome.REFUSED}
)


@dataclass(frozen=True)
class Continuation:
    """A non-mutating verdict on whether the loop may continue for an issue."""

    ok: bool
    action: ContinueAction
    reason: str
    scope: str = "guardrail"

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class Escalation:
    """A hand-off to the human-accountable assignee. ``issue_id`` is ``None`` for a
    fleet-level escalation (a global budget / circuit-breaker halt)."""

    issue_id: Optional[str]
    reason: str
    scope: str
    assignee: str
    attempts: int
    cost_spent: float
    tokens_spent: int
    at: float


@dataclass(frozen=True)
class CycleResult:
    """The result of one :meth:`Governor.run_cycle`."""

    issue_id: str
    outcome: Outcome
    reason: str
    attempts: int
    cost_spent: float
    escalation: Optional[Escalation] = None


@dataclass
class IssueLedger:
    """Per-issue runtime accounting (attempts, spend, failure streak, disposition).

    Mutable — the governor advances it via :meth:`Governor.record_attempt` /
    :meth:`Governor.record_cost` / the cycle driver."""

    issue_id: str
    attempts: int = 0
    consecutive_failures: int = 0
    cost_spent: float = 0.0
    tokens_spent: int = 0
    escalated: bool = False
    done: bool = False
    escalation: Optional[Escalation] = None


# --------------------------------------------------------------------------- #
# Escalation sink — the injected human hand-off seam
# --------------------------------------------------------------------------- #
class EscalationSink(Protocol):
    """Where escalations go (Linear adapter + PushNotification in production)."""

    def emit(self, escalation: Escalation) -> None: ...


@dataclass
class RecordingEscalationSink:
    """An in-memory sink — the fixture seam for tests (stdlib only, no network)."""

    emitted: list[Escalation] = field(default_factory=list)

    def emit(self, escalation: Escalation) -> None:
        self.emitted.append(escalation)


# --------------------------------------------------------------------------- #
# The governor
# --------------------------------------------------------------------------- #
class Governor:
    """Enforces the loop's hard stop-conditions at runtime, cycle by cycle.

    The governor keeps the authoritative per-issue ledger (attempts / cost /
    tokens / failure streak) and the fleet-wide counters (global spend, cross-issue
    failure streak). It reuses :class:`LoopEngine` for the two seams that belong to
    the spec — determinism admission and the lights-out arming gate — and reuses
    :class:`GuardrailConfig` for every ceiling, so the runtime cannot diverge from
    the state machine it enforces.
    """

    def __init__(
        self,
        config: Optional[GuardrailConfig] = None,
        *,
        engine: Optional[LoopEngine] = None,
        sink: Optional[EscalationSink] = None,
        clock: Callable[[], float] = time.monotonic,
        circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
        escalation_assignee: str = DEFAULT_ESCALATION_ASSIGNEE,
    ) -> None:
        self.config = config or GuardrailConfig()
        self.engine = engine or LoopEngine(self.config, clock=clock)
        self.sink: EscalationSink = sink if sink is not None else RecordingEscalationSink()
        self._clock = clock
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.escalation_assignee = escalation_assignee

        self._ledgers: dict[str, IssueLedger] = {}
        self._escalations: list[Escalation] = []
        self._global_cost: float = 0.0
        self._global_tokens: int = 0
        self._fleet_failures: int = 0  # consecutive cross-issue failures
        self._halted: bool = False
        self._halt_reason: str = ""
        self._circuit_open: bool = False
        self._fleet_escalated: bool = False

    # -- lights-out arming (integrates with arm_auto_dispatch) -------------- #
    def arm(self, *, guardrails_validated: bool) -> None:
        """Arm auto-dispatch via the state machine's lights-out gate. Refused
        unless the guardrail harness is proven green — see the ADR."""
        self.engine.arm_auto_dispatch(guardrails_validated=guardrails_validated)

    @property
    def armed(self) -> bool:
        return self.engine.auto_dispatch_armed

    # -- fleet-wide state --------------------------------------------------- #
    @property
    def halted(self) -> bool:
        """``True`` once any fleet-wide breaker (global budget or circuit) is open."""
        return self._halted

    @property
    def circuit_open(self) -> bool:
        """``True`` once the cross-issue circuit-breaker specifically has tripped."""
        return self._circuit_open

    @property
    def global_cost(self) -> float:
        return self._global_cost

    @property
    def global_tokens(self) -> int:
        return self._global_tokens

    @property
    def escalations(self) -> tuple[Escalation, ...]:
        return tuple(self._escalations)

    # -- per-issue ledger --------------------------------------------------- #
    def ledger(self, issue_id: str) -> IssueLedger:
        """The (lazily created) ledger for ``issue_id``."""
        led = self._ledgers.get(issue_id)
        if led is None:
            led = IssueLedger(issue_id=issue_id)
            self._ledgers[issue_id] = led
        return led

    def record_attempt(self, issue_id: str) -> IssueLedger:
        """Register a dispatch attempt (a fix cycle) for the retry ceiling."""
        led = self.ledger(issue_id)
        led.attempts += 1
        return led

    def record_cost(
        self, issue_id: str, cost: float = 0.0, *, tokens: int = 0
    ) -> IssueLedger:
        """Accumulate measured spend (from the token-logger — SGO-44) per issue and
        fleet-wide. Recorded spend can overshoot the pre-dispatch projection, so if
        it crosses the global cap this opens the fleet breaker (defense in depth)."""
        led = self.ledger(issue_id)
        led.cost_spent += cost
        led.tokens_spent += tokens
        self._global_cost += cost
        self._global_tokens += tokens
        if self._global_cost > self.config.global_budget:
            self._trip_fleet(
                f"global budget cap {self.config.global_budget} exceeded by recorded "
                f"spend ({self._global_cost}) — halting all dispatch",
                scope="global",
            )
        return led

    def record_success(self, issue_id: str) -> IssueLedger:
        """Mark an issue verified + closed. A green result clears the per-issue and
        the cross-issue failure streaks."""
        led = self.ledger(issue_id)
        led.done = True
        led.consecutive_failures = 0
        self._fleet_failures = 0
        return led

    def record_failure(self, issue_id: str) -> IssueLedger:
        """Register a verified-fail cycle. Advances the per-issue and cross-issue
        failure streaks and opens the circuit-breaker on a systemic red."""
        led = self.ledger(issue_id)
        led.consecutive_failures += 1
        self._fleet_failures += 1
        if self._fleet_failures >= self.circuit_breaker_threshold:
            self._trip_fleet(
                f"circuit breaker: {self._fleet_failures} consecutive cross-issue "
                f"failures (threshold {self.circuit_breaker_threshold}) — "
                "halting all dispatch",
                scope="circuit",
            )
        return led

    # -- the runtime gate --------------------------------------------------- #
    def should_continue(self, issue_id: str, *, cost: float = 1.0) -> Continuation:
        """May the loop (re)dispatch ``issue_id`` at projected ``cost``? Non-mutating.

        Mirrors the guard order the spec fixes: arming → fleet breakers → per-issue
        disposition → retry ceiling → per-issue budget → global budget. The caller
        acts on :attr:`Continuation.action` (escalate this item, halt the fleet, or
        proceed)."""
        if not self.armed:
            return Continuation(
                False, ContinueAction.REFUSE, "auto-dispatch is not armed", "lights_out"
            )
        if self._halted:
            return Continuation(
                False, ContinueAction.HALT, self._halt_reason or "fleet halted", "global"
            )
        led = self.ledger(issue_id)
        if led.escalated:
            return Continuation(
                False, ContinueAction.ESCALATE, "issue already escalated", "guardrail"
            )
        if led.done:
            return Continuation(
                False, ContinueAction.DONE, "issue already done (terminal)", "guardrail"
            )
        if led.attempts >= self.config.retry_ceiling:
            return Continuation(
                False,
                ContinueAction.ESCALATE,
                f"retry ceiling {self.config.retry_ceiling} reached",
                "retry",
            )
        if led.cost_spent + cost > self.config.per_issue_budget:
            return Continuation(
                False,
                ContinueAction.ESCALATE,
                f"per-issue budget cap {self.config.per_issue_budget} would be exceeded",
                "budget",
            )
        if self._global_cost + cost > self.config.global_budget:
            return Continuation(
                False,
                ContinueAction.HALT,
                f"global budget cap {self.config.global_budget} would be exceeded",
                "global",
            )
        return Continuation(True, ContinueAction.CONTINUE, "ok")

    def require_continuable(self, issue_id: str, *, cost: float = 1.0) -> None:
        """Fail-closed, exception form of :meth:`should_continue` (mirroring
        :meth:`LoopEngine.dispatch`). Raises the matching :class:`GuardrailTripped`
        subclass; returns ``None`` when the loop may continue. Non-mutating."""
        cont = self.should_continue(issue_id, cost=cost)
        if cont.ok:
            return
        if cont.action is ContinueAction.REFUSE:
            raise GuardrailTripped(cont.reason, scope="lights_out")
        if cont.action is ContinueAction.DONE:
            raise IllegalTransition(f"{issue_id}: already done (terminal)")
        if cont.action is ContinueAction.ESCALATE:
            if cont.scope == "retry":
                raise RetryCeilingExceeded(cont.reason, scope="retry")
            raise BudgetExceeded(cont.reason, scope="per_issue")
        # HALT — fleet-wide: distinguish the circuit-breaker from the budget breaker.
        if self._circuit_open:
            raise CircuitBreakerOpen(cont.reason, scope="circuit")
        raise BudgetExceeded(cont.reason, scope="global")

    # -- escalation (idempotent — emitted to the human exactly once) -------- #
    def escalate(
        self, issue_id: str, reason: str, *, scope: str = "guardrail"
    ) -> Escalation:
        """Hand ``issue_id`` to the human-accountable assignee. Idempotent: the
        escalation is emitted to the sink **exactly once**; a repeat call returns
        the same :class:`Escalation` without re-emitting."""
        led = self.ledger(issue_id)
        if led.escalated and led.escalation is not None:
            return led.escalation
        esc = Escalation(
            issue_id=issue_id,
            reason=reason,
            scope=scope,
            assignee=self.escalation_assignee,
            attempts=led.attempts,
            cost_spent=led.cost_spent,
            tokens_spent=led.tokens_spent,
            at=self._clock(),
        )
        led.escalated = True
        led.escalation = esc
        self._escalations.append(esc)
        self.sink.emit(esc)
        return esc

    def _trip_fleet(self, reason: str, *, scope: str) -> None:
        """Open a fleet-wide breaker and open a single fleet-level escalation."""
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
        if scope == "circuit":
            self._circuit_open = True
        if not self._fleet_escalated:
            self._fleet_escalated = True
            esc = Escalation(
                issue_id=None,
                reason=reason,
                scope=scope,
                assignee=self.escalation_assignee,
                attempts=0,
                cost_spent=self._global_cost,
                tokens_spent=self._global_tokens,
                at=self._clock(),
            )
            self._escalations.append(esc)
            self.sink.emit(esc)

    # -- the governed cycle ------------------------------------------------- #
    def run_cycle(
        self,
        issue_id: str,
        *,
        dispatch: Callable[[str], None],
        verify: Callable[[str], VerificationRun],
        close: Callable[[str], None],
        cost: float = 1.0,
    ) -> CycleResult:
        """One governed ``dispatch → verify → close`` cycle, enforcing every
        stop-condition. ``verify`` returns a :class:`VerificationRun` (its
        determinism properties + the judgment verdict — the injected seam). A
        breach escalates or halts **before** ``close`` can run; only a clean,
        deterministic pass reaches ``close``."""
        cont = self.should_continue(issue_id, cost=cost)
        if not cont:
            if cont.action is ContinueAction.ESCALATE:
                esc = self.escalate(issue_id, cont.reason, scope=cont.scope)
                return self._result(issue_id, Outcome.ESCALATED, cont.reason, esc)
            if cont.action is ContinueAction.REFUSE:
                return self._result(issue_id, Outcome.REFUSED, cont.reason, None)
            if cont.action is ContinueAction.DONE:
                return self._result(issue_id, Outcome.DONE, cont.reason, None)
            # HALT — fleet-wide. Ensure a single fleet escalation is open.
            if not self._fleet_escalated:
                self._trip_fleet(cont.reason, scope=cont.scope)
            return self._result(issue_id, Outcome.HALTED, cont.reason, None)

        # Guards passed — run the cycle and meter it.
        self.record_attempt(issue_id)
        dispatch(issue_id)
        run = verify(issue_id)
        self.record_cost(issue_id, cost)
        if self._halted:  # this cycle's recorded spend blew the fleet cap
            return self._result(issue_id, Outcome.HALTED, self._halt_reason, None)

        # STOP-CONDITION: determinism. A tainted "pass" can never close — escalate.
        try:
            self.engine.admit_verification(run)
        except DeterminismViolation as exc:
            esc = self.escalate(issue_id, str(exc), scope="determinism")
            return self._result(issue_id, Outcome.ESCALATED, str(exc), esc)

        if run.verdict is Verdict.PASS:
            close(issue_id)
            self.record_success(issue_id)
            return self._result(issue_id, Outcome.DONE, "verified pass — closed", None)

        if run.verdict is Verdict.AMBIGUOUS:
            reason = "ambiguous verification — escalating rather than guessing"
            esc = self.escalate(issue_id, reason, scope="ambiguous")
            return self._result(issue_id, Outcome.ESCALATED, reason, esc)

        # verified FAIL — advance the failure streaks, then decide.
        self.record_failure(issue_id)
        led = self.ledger(issue_id)
        if led.attempts >= self.config.retry_ceiling:
            reason = (
                f"retry ceiling {self.config.retry_ceiling} reached after repeated "
                "failure — escalating instead of another fix cycle"
            )
            esc = self.escalate(issue_id, reason, scope="retry")
            return self._result(issue_id, Outcome.ESCALATED, reason, esc)
        if self._halted:  # the failure tripped the cross-issue circuit-breaker
            return self._result(issue_id, Outcome.HALTED, self._halt_reason, None)
        return self._result(issue_id, Outcome.NEEDS_FIX, "verified fail — retry", None)

    def run_until_terminal(
        self,
        issue_id: str,
        *,
        dispatch: Callable[[str], None],
        verify: Callable[[str], VerificationRun],
        close: Callable[[str], None],
        cost: float = 1.0,
        max_cycles: int = 100,
    ) -> CycleResult:
        """Drive ``issue_id`` through repeated governed cycles until it reaches a
        terminal outcome (``done`` / ``escalated`` / ``halted`` / ``refused``).
        ``max_cycles`` is a belt-and-braces bound; the real bound is the retry
        ceiling, which must fire first."""
        result: Optional[CycleResult] = None
        for _ in range(max_cycles):
            result = self.run_cycle(
                issue_id, dispatch=dispatch, verify=verify, close=close, cost=cost
            )
            if result.outcome in _TERMINAL_OUTCOMES:
                return result
        raise loop.LoopError(
            f"{issue_id}: exceeded max_cycles={max_cycles} without terminating — "
            "a guardrail should have fired first"
        )

    def _result(
        self,
        issue_id: str,
        outcome: Outcome,
        reason: str,
        escalation: Optional[Escalation],
    ) -> CycleResult:
        led = self.ledger(issue_id)
        return CycleResult(
            issue_id=issue_id,
            outcome=outcome,
            reason=reason,
            attempts=led.attempts,
            cost_spent=led.cost_spent,
            escalation=escalation,
        )
