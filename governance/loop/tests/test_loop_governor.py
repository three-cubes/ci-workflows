"""Tests for the runtime loop GOVERNOR (SP-C-4 / PLA-312).

The governor is the *runtime* that enforces, during a live loop, the hard
stop-conditions :mod:`loop_state_machine` defines. Where the state-machine harness
proves each guardrail *can* fire, this suite proves the governor actually fires it
while wrapping a dispatch → verify → close cycle: it halts + escalates on the
N-retry ceiling, the per-issue + global budget caps, a cross-issue circuit-breaker,
a non-deterministic (``--reruns`` / network / unpinned-seed) "pass", and an
ambiguous verdict — and that every escalation is emitted to the human **exactly
once** (idempotent). It also refuses to run at all unless auto-dispatch is armed.

Run (stdlib only, no deps, no network — the sinks/verifier seams are injected)::

    python3 -m unittest discover -s governance/loop/tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop_state_machine as loop  # noqa: E402 — path shim above
from loop_governor import (  # noqa: E402
    CircuitBreakerOpen,
    ContinueAction,
    Escalation,
    Governor,
    IssueLedger,
    Outcome,
    RecordingEscalationSink,
)
from loop_state_machine import (  # noqa: E402
    BudgetExceeded,
    GuardrailConfig,
    GuardrailTripped,
    RetryCeilingExceeded,
    Verdict,
    VerificationRun,
)


# --------------------------------------------------------------------------- #
# Test helpers — a deterministic clock + recording dispatch/verify/close seams
# --------------------------------------------------------------------------- #
class FakeClock:
    """Deterministic, injectable clock (the loop never reads the wall clock)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Cycle:
    """Records dispatch/verify/close calls and replays a scripted verdict.

    ``verify`` yields the next :class:`VerificationRun` from ``runs`` (repeating the
    last one forever), so a test can script "always fails" or "fails twice then
    passes". ``dispatched`` / ``closed`` capture that dispatch/close ran (or did
    NOT — e.g. a determinism trip must never call ``close``).
    """

    def __init__(self, *runs: VerificationRun) -> None:
        self._runs = list(runs) or [VerificationRun(verdict=Verdict.PASS)]
        self._i = 0
        self.dispatched: list[str] = []
        self.closed: list[str] = []

    def dispatch(self, issue_id: str) -> None:
        self.dispatched.append(issue_id)

    def verify(self, issue_id: str) -> VerificationRun:
        run = self._runs[min(self._i, len(self._runs) - 1)]
        self._i += 1
        return run

    def close(self, issue_id: str) -> None:
        self.closed.append(issue_id)

    def kwargs(self) -> dict:
        return {"dispatch": self.dispatch, "verify": self.verify, "close": self.close}


def _armed(config: GuardrailConfig | None = None, **kw) -> tuple[Governor, RecordingEscalationSink]:
    """An armed governor + its recording escalation sink (the human seam)."""
    sink = RecordingEscalationSink()
    gov = Governor(config, sink=sink, **kw)
    gov.arm(guardrails_validated=True)
    return gov, sink


def _clean_pass(**kw) -> VerificationRun:
    return VerificationRun(verdict=Verdict.PASS, **kw)


def _clean_fail(**kw) -> VerificationRun:
    return VerificationRun(verdict=Verdict.FAIL, **kw)


# --------------------------------------------------------------------------- #
# Arming — refuse to run unless arm_auto_dispatch validated (integration)
# --------------------------------------------------------------------------- #
class ArmingTest(unittest.TestCase):
    def test_governor_starts_disarmed(self):
        gov = Governor()
        self.assertFalse(gov.armed)

    def test_should_continue_refuses_when_not_armed(self):
        gov = Governor()
        cont = gov.should_continue("PLA-1")
        self.assertFalse(cont)
        self.assertEqual(cont.action, ContinueAction.REFUSE)

    def test_run_cycle_refuses_and_does_not_dispatch_when_not_armed(self):
        gov = Governor()
        cyc = Cycle(_clean_pass())
        result = gov.run_cycle("PLA-1", **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.REFUSED)
        self.assertEqual(cyc.dispatched, [])  # never dispatched while disarmed
        self.assertEqual(cyc.closed, [])

    def test_arm_refused_without_guardrail_proof(self):
        gov = Governor()
        with self.assertRaises(GuardrailTripped) as ctx:
            gov.arm(guardrails_validated=False)
        self.assertEqual(ctx.exception.scope, "lights_out")
        self.assertFalse(gov.armed)

    def test_arm_allowed_with_guardrail_proof(self):
        gov = Governor()
        gov.arm(guardrails_validated=True)
        self.assertTrue(gov.armed)


# --------------------------------------------------------------------------- #
# STOP-CONDITION 1 — N-retry ceiling halts + escalates (idempotent, once)
# --------------------------------------------------------------------------- #
class RetryCeilingTest(unittest.TestCase):
    def test_never_greening_issue_escalates_at_ceiling(self):
        gov, sink = _armed(GuardrailConfig(retry_ceiling=3, per_issue_budget=1e9))
        cyc = Cycle(_clean_fail())  # an agent that never fixes the defect
        result = gov.run_until_terminal("PLA-10", **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.ESCALATED)
        self.assertEqual(gov.ledger("PLA-10").attempts, 3)  # N attempts, not N+1
        self.assertEqual(cyc.closed, [])  # a failing issue is never closed
        # Escalation emitted to the human EXACTLY once.
        self.assertEqual(len(sink.emitted), 1)
        self.assertEqual(sink.emitted[0].scope, "retry")
        self.assertEqual(sink.emitted[0].issue_id, "PLA-10")

    def test_should_continue_reports_escalate_at_ceiling(self):
        gov, _ = _armed(GuardrailConfig(retry_ceiling=2, per_issue_budget=1e9))
        gov.record_attempt("PLA-11")
        gov.record_attempt("PLA-11")  # attempts == ceiling
        cont = gov.should_continue("PLA-11")
        self.assertFalse(cont)
        self.assertEqual(cont.action, ContinueAction.ESCALATE)

    def test_escalation_after_ceiling_is_idempotent_across_further_cycles(self):
        gov, sink = _armed(GuardrailConfig(retry_ceiling=2, per_issue_budget=1e9))
        cyc = Cycle(_clean_fail())
        gov.run_until_terminal("PLA-12", **cyc.kwargs())
        # Re-driving the terminal issue must NOT emit a second escalation.
        again = gov.run_cycle("PLA-12", **cyc.kwargs())
        self.assertEqual(again.outcome, Outcome.ESCALATED)
        self.assertEqual(len(sink.emitted), 1)


# --------------------------------------------------------------------------- #
# STOP-CONDITION 2 & 3 — per-issue cap escalates; global cap halts the fleet
# --------------------------------------------------------------------------- #
class BudgetCapTest(unittest.TestCase):
    def test_per_issue_budget_escalates_that_item(self):
        gov, sink = _armed(GuardrailConfig(per_issue_budget=2.0, global_budget=1e9))
        gov.record_cost("PLA-20", 2.0)  # spent up to the cap
        cont = gov.should_continue("PLA-20", cost=0.5)  # any more overshoots
        self.assertFalse(cont)
        self.assertEqual(cont.action, ContinueAction.ESCALATE)
        # And run_cycle escalates without dispatching more work.
        cyc = Cycle(_clean_pass())
        result = gov.run_cycle("PLA-20", cost=0.5, **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.ESCALATED)
        self.assertEqual(cyc.dispatched, [])
        self.assertEqual(len(sink.emitted), 1)
        self.assertEqual(sink.emitted[0].scope, "budget")

    def test_global_budget_halts_the_whole_fleet(self):
        gov, sink = _armed(GuardrailConfig(global_budget=2.0, per_issue_budget=1e9))
        gov.record_cost("A", 2.0)  # global spend at the cap
        # A brand-new, cheap, untouched issue is refused — the breaker is fleet-wide.
        cont = gov.should_continue("B", cost=0.5)
        self.assertFalse(cont)
        self.assertEqual(cont.action, ContinueAction.HALT)
        cyc = Cycle(_clean_pass())
        result = gov.run_cycle("B", cost=0.5, **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.HALTED)
        self.assertEqual(cyc.dispatched, [])
        self.assertTrue(gov.halted)
        # A fleet-level escalation is opened for the human — exactly once.
        self.assertEqual(len(sink.emitted), 1)
        self.assertIsNone(sink.emitted[0].issue_id)
        self.assertEqual(sink.emitted[0].scope, "global")

    def test_recorded_overspend_trips_the_global_breaker(self):
        # Actual spend can overshoot the projection; recording it opens the breaker.
        gov, sink = _armed(GuardrailConfig(global_budget=2.0, per_issue_budget=1e9))
        gov.record_cost("A", 3.0)  # blew straight past the fleet cap
        self.assertTrue(gov.halted)
        self.assertEqual(len(sink.emitted), 1)
        self.assertIsNone(sink.emitted[0].issue_id)

    def test_require_continuable_raises_budget_and_circuit_types(self):
        gov, _ = _armed(GuardrailConfig(global_budget=2.0, per_issue_budget=1.0))
        gov.record_cost("A", 1.0)  # A at per-issue cap
        with self.assertRaises(BudgetExceeded):
            gov.require_continuable("A", cost=0.5)  # per-issue overshoot


# --------------------------------------------------------------------------- #
# Global circuit-breaker — repeated CROSS-ISSUE failures halt all dispatch
# --------------------------------------------------------------------------- #
class CircuitBreakerTest(unittest.TestCase):
    def test_repeated_cross_issue_failures_open_the_circuit(self):
        gov, sink = _armed(
            GuardrailConfig(retry_ceiling=99, per_issue_budget=1e9, global_budget=1e9),
            circuit_breaker_threshold=3,
        )
        # Three DIFFERENT issues each fail once — a systemic red, not one flaky item.
        for issue in ("A", "B", "C"):
            gov.run_cycle(issue, **Cycle(_clean_fail()).kwargs())
        self.assertTrue(gov.circuit_open)
        self.assertTrue(gov.halted)
        # A fresh, untouched issue is now refused fleet-wide.
        cont = gov.should_continue("D")
        self.assertFalse(cont)
        self.assertEqual(cont.action, ContinueAction.HALT)
        with self.assertRaises(CircuitBreakerOpen):
            gov.require_continuable("D")
        # Exactly one fleet escalation for the circuit trip.
        circuit_escalations = [e for e in sink.emitted if e.scope == "circuit"]
        self.assertEqual(len(circuit_escalations), 1)

    def test_success_resets_the_cross_issue_failure_streak(self):
        gov, _ = _armed(
            GuardrailConfig(retry_ceiling=99, per_issue_budget=1e9, global_budget=1e9),
            circuit_breaker_threshold=3,
        )
        gov.run_cycle("A", **Cycle(_clean_fail()).kwargs())
        gov.run_cycle("B", **Cycle(_clean_fail()).kwargs())
        gov.run_cycle("C", **Cycle(_clean_pass()).kwargs())  # a green clears the streak
        gov.run_cycle("D", **Cycle(_clean_fail()).kwargs())
        gov.run_cycle("E", **Cycle(_clean_fail()).kwargs())
        # Only two consecutive failures since the reset — under the threshold.
        self.assertFalse(gov.circuit_open)
        self.assertFalse(gov.halted)


# --------------------------------------------------------------------------- #
# STOP-CONDITION 4 — determinism: a --reruns / network / unpinned "pass" escalates
# --------------------------------------------------------------------------- #
class DeterminismTest(unittest.TestCase):
    def test_reruns_pass_escalates_and_never_closes(self):
        gov, sink = _armed()
        cyc = Cycle(_clean_pass(used_reruns=True))  # retry-until-green
        result = gov.run_cycle("PLA-30", **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.ESCALATED)
        self.assertEqual(cyc.closed, [])  # a tainted "pass" must NEVER merge
        self.assertEqual(len(sink.emitted), 1)
        self.assertEqual(sink.emitted[0].scope, "determinism")

    def test_network_and_unpinned_seed_passes_escalate(self):
        for tainted in (
            _clean_pass(network_accessed=True),
            _clean_pass(seed_pinned=False),
        ):
            gov, sink = _armed()
            cyc = Cycle(tainted)
            result = gov.run_cycle("PLA-31", **cyc.kwargs())
            self.assertEqual(result.outcome, Outcome.ESCALATED)
            self.assertEqual(cyc.closed, [])
            self.assertEqual(sink.emitted[0].scope, "determinism")

    def test_clean_deterministic_pass_closes(self):
        gov, sink = _armed()
        cyc = Cycle(_clean_pass())
        result = gov.run_cycle("PLA-32", **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.DONE)
        self.assertEqual(cyc.closed, ["PLA-32"])  # verified + merged
        self.assertEqual(sink.emitted, [])  # nothing to escalate


# --------------------------------------------------------------------------- #
# STOP-CONDITION 5 — ambiguous verification escalates (do not guess)
# --------------------------------------------------------------------------- #
class AmbiguousVerificationTest(unittest.TestCase):
    def test_ambiguous_verdict_escalates_without_closing(self):
        gov, sink = _armed()
        cyc = Cycle(VerificationRun(verdict=Verdict.AMBIGUOUS))
        result = gov.run_cycle("PLA-40", **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.ESCALATED)
        self.assertEqual(cyc.closed, [])
        self.assertEqual(len(sink.emitted), 1)
        self.assertEqual(sink.emitted[0].scope, "ambiguous")


# --------------------------------------------------------------------------- #
# Escalation — idempotent, once, and carries the human-accountable assignee
# --------------------------------------------------------------------------- #
class EscalationTest(unittest.TestCase):
    def test_escalate_is_idempotent_and_emits_once(self):
        gov, sink = _armed()
        first = gov.escalate("PLA-50", "first reason", scope="retry")
        second = gov.escalate("PLA-50", "second reason", scope="budget")
        self.assertIs(first, second)  # same escalation returned
        self.assertEqual(len(sink.emitted), 1)  # emitted to the human exactly once
        self.assertEqual(sink.emitted[0].reason, "first reason")

    def test_escalation_carries_assignee_and_issue_state(self):
        gov, _ = _armed(escalation_assignee="dan@danmcmahon.com.au")
        gov.record_attempt("PLA-51")
        gov.record_cost("PLA-51", 1.5, tokens=1200)
        esc = gov.escalate("PLA-51", "over budget", scope="budget")
        self.assertIsInstance(esc, Escalation)
        self.assertEqual(esc.assignee, "dan@danmcmahon.com.au")
        self.assertEqual(esc.attempts, 1)
        self.assertEqual(esc.cost_spent, 1.5)
        self.assertEqual(esc.tokens_spent, 1200)

    def test_escalate_marks_ledger_and_lists_escalation(self):
        gov, _ = _armed()
        gov.escalate("PLA-52", "boom", scope="retry")
        self.assertTrue(gov.ledger("PLA-52").escalated)
        self.assertEqual(len(gov.escalations), 1)


# --------------------------------------------------------------------------- #
# Per-issue accounting — record_attempt / record_cost drive the ledger
# --------------------------------------------------------------------------- #
class AccountingTest(unittest.TestCase):
    def test_record_attempt_increments_per_issue(self):
        gov, _ = _armed()
        gov.record_attempt("PLA-60")
        led = gov.record_attempt("PLA-60")
        self.assertIsInstance(led, IssueLedger)
        self.assertEqual(led.attempts, 2)

    def test_record_cost_accumulates_per_issue_and_globally(self):
        gov, _ = _armed(GuardrailConfig(per_issue_budget=1e9, global_budget=1e9))
        gov.record_cost("PLA-61", 1.5, tokens=100)
        gov.record_cost("PLA-61", 0.5, tokens=50)
        gov.record_cost("PLA-62", 2.0, tokens=200)
        self.assertEqual(gov.ledger("PLA-61").cost_spent, 2.0)
        self.assertEqual(gov.ledger("PLA-61").tokens_spent, 150)
        self.assertEqual(gov.global_cost, 4.0)
        self.assertEqual(gov.global_tokens, 350)

    def test_should_continue_does_not_mutate(self):
        gov, _ = _armed()
        gov.record_attempt("PLA-63")
        before = (gov.ledger("PLA-63").attempts, gov.ledger("PLA-63").cost_spent, gov.global_cost)
        gov.should_continue("PLA-63", cost=1.0)
        after = (gov.ledger("PLA-63").attempts, gov.ledger("PLA-63").cost_spent, gov.global_cost)
        self.assertEqual(before, after)


# --------------------------------------------------------------------------- #
# The full governed cycle + driver (dispatch → verify → close)
# --------------------------------------------------------------------------- #
class GovernedCycleTest(unittest.TestCase):
    def test_fail_then_pass_reaches_done(self):
        gov, sink = _armed(GuardrailConfig(retry_ceiling=3, per_issue_budget=1e9))
        cyc = Cycle(_clean_fail(), _clean_pass())  # red once, then green
        result = gov.run_until_terminal("PLA-70", **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.DONE)
        self.assertEqual(gov.ledger("PLA-70").attempts, 2)
        self.assertEqual(cyc.closed, ["PLA-70"])
        self.assertEqual(sink.emitted, [])

    def test_happy_path_single_cycle_closes(self):
        gov, _ = _armed()
        cyc = Cycle(_clean_pass())
        result = gov.run_cycle("PLA-71", **cyc.kwargs())
        self.assertEqual(result.outcome, Outcome.DONE)
        self.assertEqual(cyc.dispatched, ["PLA-71"])
        self.assertEqual(cyc.closed, ["PLA-71"])
        self.assertTrue(gov.ledger("PLA-71").done)

    def test_uses_injected_deterministic_clock(self):
        clock = FakeClock(start=100.0)
        gov, sink = _armed(clock=clock)
        gov.escalate("PLA-72", "boom", scope="retry")
        self.assertEqual(sink.emitted[0].at, 100.0)


if __name__ == "__main__":
    unittest.main()
