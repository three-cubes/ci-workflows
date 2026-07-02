"""Hardening harness for the continuous-dispatch driver (SP-C · PR #49 verify).

These tests PROVE the fail-open holes the adversarial verify found are closed:

* **FIX 1 — durable cross-tick ledger.** Each cron tick is a FRESH PROCESS. The
  guardrails only bite if the governor ledger survives process death. Every test
  here drives SEPARATE ``run_once`` ticks with a **fresh Runner + fresh Governor
  each time**, sharing only a durable :class:`JsonFileStateStore` on disk — the
  same shape as the real workflow. It asserts the retry ceiling, the per-issue
  budget, the global budget, and the cross-issue circuit-breaker all trip ACROSS
  process boundaries (and a control with the non-durable null store shows they
  would NOT — the exact bug being fixed).
* **FIX 2 — guardrails_validated fails CLOSED.** A missing / renamed / truncated
  guardrail harness must NOT read as vacuously green, and arming must refuse.
* **FIX 3 — soak before live.** The first N armed ticks stay record-only even when
  live is requested, so a cold armed tick can never dispatch.

Stdlib only, no network — the discipline the loop enforces. Run from the repo root::

    python3 -m unittest discover -s governance/loop/tests -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop_dispatcher import (  # noqa: E402 — path shim above
    CandidateIssue,
    Dispatcher,
    StaticIssueSource,
    guardrails_validated,
)
from loop_governor import (  # noqa: E402
    DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    ContinueAction,
    Governor,
    GovernorState,
    JsonFileStateStore,
    NullStateStore,
    StateStoreError,
)
from loop_runner import (  # noqa: E402
    LoggingDispatchSink,
    RunDecision,
    Runner,
)
from loop_state_machine import GuardrailConfig, GuardrailTripped  # noqa: E402


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _issue(id="PLA-1"):
    return CandidateIssue(
        id=id,
        title=f"work for {id}",
        state_type="backlog",
        priority=2,
        created_at="2026-07-01T12:00:00.000Z",
        labels=("adp-wave-1",),
        team_key=id.split("-", 1)[0],
        description="**Repos:** kairix",
        url=f"https://linear.app/x/{id}",
    )


def _fresh_runner(issues, store, *, config=None, soak_ticks=0, armed=True,
                  cost=1.0, validator=lambda: True):
    """A BRAND-NEW Runner + Governor (as a fresh process would build) bound to the
    given (possibly shared, on-disk) state store. Arming is re-decided here every
    call — exactly as the workflow re-arms from LOOP_ARMED each tick — so nothing
    but the durable store carries state between ticks."""
    dispatcher = Dispatcher(
        StaticIssueSource(issues), config=config, guardrails_validator=validator
    )
    governor = Governor(config)
    if armed:
        governor.arm(guardrails_validated=True)
    runner = Runner(
        dispatcher,
        governor,
        guardrails_validator=validator,
        state_store=store,
        soak_ticks=soak_ticks,
        cost_per_issue=cost,
    )
    return runner, governor


class _HardeningCase(unittest.TestCase):
    def _tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _state_file(self) -> Path:
        return self._tmpdir() / "loop-state.json"


# --------------------------------------------------------------------------- #
# FIX 1 — the retry ceiling trips ACROSS process boundaries
# --------------------------------------------------------------------------- #
class RetryCeilingCrossTickTest(_HardeningCase):
    def test_retry_ceiling_trips_across_fresh_processes(self):
        cfg = GuardrailConfig(retry_ceiling=3, per_issue_budget=1e9, global_budget=1e9)
        store = JsonFileStateStore(self._state_file())
        issues = [_issue("PLA-1")]

        # Ticks 1-3: each a fresh Runner+Governor sharing only the on-disk ledger.
        for tick in range(1, 4):
            runner, _ = _fresh_runner(issues, store, config=cfg)
            res = runner.run_once(LoggingDispatchSink(), dry_run=False)
            self.assertEqual(res.decision, RunDecision.DISPATCHED, f"tick {tick}")

        # By the 3rd dispatch the persisted ledger sits AT the ceiling: a fresh
        # governor restored from the store escalates (retry_ceiling reached).
        armed = Governor(cfg)
        armed.arm(guardrails_validated=True)
        armed.restore(store.load())
        self.assertEqual(armed.ledger("PLA-1").attempts, 3)
        cont = armed.should_continue("PLA-1")
        self.assertIs(cont.action, ContinueAction.ESCALATE)
        self.assertIn("retry ceiling", cont.reason)

        # Tick 4: yet another fresh process REFUSES to dispatch — the ceiling
        # actually bounds a persistently-failing item across ticks.
        runner, _ = _fresh_runner(issues, store, config=cfg)
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.IDLE)
        self.assertFalse(res.dispatched)
        self.assertTrue(any("retry ceiling" in r for _, r in res.skipped))

    def test_control_without_durable_store_never_trips(self):
        # The bug FIX 1 closes: with the NON-durable null store, a fresh governor
        # each tick forgets every attempt, so the ceiling never trips and a
        # persistently-failing item is re-dispatched forever.
        cfg = GuardrailConfig(retry_ceiling=3, per_issue_budget=1e9, global_budget=1e9)
        store = NullStateStore()
        issues = [_issue("PLA-1")]
        for _ in range(6):  # well past the ceiling
            runner, _ = _fresh_runner(issues, store, config=cfg)
            res = runner.run_once(LoggingDispatchSink(), dry_run=False)
            self.assertEqual(res.decision, RunDecision.DISPATCHED)


# --------------------------------------------------------------------------- #
# FIX 1 — the per-issue + global budget accumulate ACROSS ticks
# --------------------------------------------------------------------------- #
class BudgetCrossTickTest(_HardeningCase):
    def test_per_issue_budget_accumulates_across_fresh_processes(self):
        cfg = GuardrailConfig(per_issue_budget=2.5, retry_ceiling=100, global_budget=1e9)
        store = JsonFileStateStore(self._state_file())
        issues = [_issue("PLA-1")]

        # cost=1.0/tick: ticks 1-2 dispatch (spend 1, then 2).
        for tick in range(1, 3):
            runner, _ = _fresh_runner(issues, store, config=cfg)
            self.assertEqual(
                runner.run_once(LoggingDispatchSink(), dry_run=False).decision,
                RunDecision.DISPATCHED,
                f"tick {tick}",
            )
        # Tick 3 would push spend to 3.0 > 2.5 — the per-issue cap trips across ticks.
        runner, _ = _fresh_runner(issues, store, config=cfg)
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.IDLE)
        self.assertTrue(any("per-issue" in r for _, r in res.skipped))

    def test_global_budget_accumulates_across_fresh_processes(self):
        # DISTINCT issue each tick so ONLY the fleet-wide global counter accrues
        # (no per-issue / retry interference).
        cfg = GuardrailConfig(global_budget=2.5, per_issue_budget=1e9, retry_ceiling=100)
        store = JsonFileStateStore(self._state_file())

        for iid in ("PLA-A", "PLA-B"):
            runner, _ = _fresh_runner([_issue(iid)], store, config=cfg)
            self.assertEqual(
                runner.run_once(LoggingDispatchSink(), dry_run=False).decision,
                RunDecision.DISPATCHED,
                iid,
            )
        # Global spend is now 2.0; a third distinct item would exceed 2.5 -> HALT.
        runner, _ = _fresh_runner([_issue("PLA-C")], store, config=cfg)
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.HALTED)
        self.assertFalse(res.dispatched)


# --------------------------------------------------------------------------- #
# FIX 1 — the cross-issue circuit-breaker opens ACROSS ticks
# --------------------------------------------------------------------------- #
class CircuitBreakerCrossTickTest(_HardeningCase):
    def test_circuit_breaker_opens_across_fresh_processes(self):
        cfg = GuardrailConfig(per_issue_budget=1e9, global_budget=1e9, retry_ceiling=100)
        store = JsonFileStateStore(self._state_file())

        # Simulate the verify/close side recording a verified-FAIL each tick, each
        # in a fresh process that restores from + saves to the shared store. After
        # `threshold` consecutive cross-issue failures the breaker must be open in
        # the DURABLE state — not just in some long-lived process's memory.
        for i in range(DEFAULT_CIRCUIT_BREAKER_THRESHOLD):
            gov = Governor(cfg)
            loaded = store.load()
            if loaded is not None:
                gov.restore(loaded)
            gov.record_failure(f"ISS-{i}")
            store.save(gov.snapshot())

        restored = Governor(cfg)
        restored.restore(store.load())
        self.assertTrue(restored.circuit_open)
        self.assertTrue(restored.halted)

        # A fresh armed Runner tick now HALTS fleet-wide off the persisted breaker.
        runner, _ = _fresh_runner([_issue("PLA-1")], store, config=cfg)
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.HALTED)
        self.assertFalse(res.dispatched)
        self.assertIn("circuit", res.reason.lower())


# --------------------------------------------------------------------------- #
# FIX 1 — the durable store itself: round-trip, atomicity, fail-closed
# --------------------------------------------------------------------------- #
class JsonFileStateStoreTest(_HardeningCase):
    def test_missing_file_is_a_clean_empty_ledger(self):
        store = JsonFileStateStore(self._state_file())
        state = store.load()
        self.assertIsInstance(state, GovernorState)
        self.assertEqual(state.global_cost, 0.0)
        self.assertEqual(state.ledgers, {})

    def test_empty_file_is_a_clean_empty_ledger(self):
        path = self._state_file()
        path.write_text("", encoding="utf-8")
        self.assertEqual(JsonFileStateStore(path).load().global_cost, 0.0)

    def test_round_trips_the_full_ledger(self):
        cfg = GuardrailConfig(per_issue_budget=1e9, global_budget=1e9, retry_ceiling=100)
        gov = Governor(cfg)
        gov.record_attempt("PLA-1")
        gov.record_attempt("PLA-1")
        gov.record_cost("PLA-1", 3.5, tokens=42)
        gov.record_failure("PLA-2")
        store = JsonFileStateStore(self._state_file())
        store.save(gov.snapshot())

        # A brand-new governor rebuilt from disk resumes EXACTLY where we left off.
        rebuilt = Governor(cfg)
        rebuilt.restore(store.load())
        self.assertEqual(rebuilt.ledger("PLA-1").attempts, 2)
        self.assertEqual(rebuilt.ledger("PLA-1").cost_spent, 3.5)
        self.assertEqual(rebuilt.ledger("PLA-1").tokens_spent, 42)
        self.assertEqual(rebuilt.global_cost, 3.5)
        self.assertEqual(rebuilt.ledger("PLA-2").consecutive_failures, 1)

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        path = self._state_file()
        store = JsonFileStateStore(path)
        store.save(GovernorState(global_cost=7.0))
        self.assertTrue(path.is_file())
        # os.replace cleaned up the temp; no torn ".loop-state.*.tmp" left behind.
        leftovers = list(path.parent.glob(".loop-state.*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertEqual(JsonFileStateStore(path).load().global_cost, 7.0)

    def test_corrupt_json_fails_closed(self):
        path = self._state_file()
        path.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(StateStoreError):
            JsonFileStateStore(path).load()

    def test_unknown_version_fails_closed(self):
        path = self._state_file()
        path.write_text('{"version": 9999}', encoding="utf-8")
        with self.assertRaises(StateStoreError):
            JsonFileStateStore(path).load()

    def test_null_store_is_non_durable_and_keeps_memory(self):
        store = NullStateStore()
        self.assertFalse(store.durable)
        self.assertIsNone(store.load())  # None => "keep whatever is in memory"
        store.save(GovernorState(global_cost=1.0))  # no-op, no raise

    def test_runner_refuses_on_unreadable_durable_state(self):
        path = self._state_file()
        path.write_text("{ corrupt", encoding="utf-8")
        cfg = GuardrailConfig()
        runner, _ = _fresh_runner([_issue("PLA-1")], JsonFileStateStore(path), config=cfg)
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.REFUSED)
        self.assertFalse(res.dispatched)


# --------------------------------------------------------------------------- #
# FIX 3 — soak before live: the first armed ticks never dispatch
# --------------------------------------------------------------------------- #
class SoakBeforeLiveTest(_HardeningCase):
    def test_first_armed_ticks_record_only_then_go_live(self):
        cfg = GuardrailConfig(retry_ceiling=100, per_issue_budget=1e9, global_budget=1e9)
        store = JsonFileStateStore(self._state_file())
        issues = [_issue("PLA-1")]

        # soak_ticks=2: even armed + LIVE requested, the first TWO fresh-process
        # ticks stay record-only (the very first armed cron tick never dispatches).
        for tick in range(1, 3):
            runner, _ = _fresh_runner(issues, store, config=cfg, soak_ticks=2)
            res = runner.run_once(LoggingDispatchSink(), dry_run=False)
            self.assertEqual(res.decision, RunDecision.RECORDED, f"soak tick {tick}")
            self.assertFalse(res.dispatched)
            self.assertIn("soak", res.reason)
            self.assertIsNotNone(res.contract)  # the would-dispatch pick is recorded

        # No attempt was consumed during the soak (nothing dispatched).
        probe = Governor(cfg)
        probe.restore(store.load())
        self.assertEqual(probe.ledger("PLA-1").attempts, 0)
        self.assertEqual(store.load().soak_ticks, 2)

        # Tick 3: the soak window has elapsed -> the fresh tick dispatches.
        runner, _ = _fresh_runner(issues, store, config=cfg, soak_ticks=2)
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.DISPATCHED)
        self.assertTrue(res.dispatched)

    def test_zero_soak_dispatches_immediately(self):
        cfg = GuardrailConfig(retry_ceiling=100, per_issue_budget=1e9, global_budget=1e9)
        store = JsonFileStateStore(self._state_file())
        runner, _ = _fresh_runner([_issue("PLA-1")], store, config=cfg, soak_ticks=0)
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.DISPATCHED)

    def test_disarmed_soak_never_advances_counter(self):
        # A disarmed tick is a pure preview: it must not burn a soak tick.
        cfg = GuardrailConfig()
        store = JsonFileStateStore(self._state_file())
        runner, _ = _fresh_runner(
            [_issue("PLA-1")], store, config=cfg, soak_ticks=2, armed=False
        )
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.RECORDED)
        self.assertEqual(store.load().soak_ticks, 0)  # unchanged — disarmed preview


# --------------------------------------------------------------------------- #
# FIX 2 — guardrails_validated fails CLOSED (no vacuous green)
# --------------------------------------------------------------------------- #
_STUB_FEWER_TESTS = """\
import unittest


class Stub(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertTrue(True)
"""


class GuardrailsValidatedFailClosedTest(_HardeningCase):
    def test_missing_harness_is_not_validated(self):
        # An empty tests dir (harness file absent) must NOT read as green.
        empty = self._tmpdir()
        self.assertFalse(guardrails_validated(test_dir=empty))

    def test_renamed_harness_is_not_validated(self):
        # Plenty of tests, but under the WRONG name -> the default pattern misses
        # -> empty discovery -> must fail closed (renaming defeats the gate).
        d = self._tmpdir()
        body = "import unittest\n\n\nclass Many(unittest.TestCase):\n" + "".join(
            f"    def test_{i}(self):\n        self.assertTrue(True)\n" for i in range(30)
        )
        (d / "test_renamed_guardrails.py").write_text(body, encoding="utf-8")
        self.assertFalse(guardrails_validated(test_dir=d))

    def test_too_few_tests_is_not_validated(self):
        # A truncated harness (green, but fewer than the known-minimum) fails closed.
        d = self._tmpdir()
        (d / "test_stub_guardrails.py").write_text(_STUB_FEWER_TESTS, encoding="utf-8")
        self.assertFalse(
            guardrails_validated(test_dir=d, pattern="test_stub_guardrails.py")
        )

    def test_real_harness_validates(self):
        # The real 25-test stop-condition harness IS green and above the floor.
        self.assertTrue(guardrails_validated())

    def test_arm_and_runner_refuse_when_harness_missing(self):
        empty = self._tmpdir()

        def validator() -> bool:
            return guardrails_validated(test_dir=empty)

        self.assertFalse(validator())
        # arm_auto_dispatch refuses on the (missing-harness) red proof...
        gov = Governor(GuardrailConfig())
        with self.assertRaises(GuardrailTripped):
            gov.arm(guardrails_validated=validator())
        # ...and the runner REFUSES the tick (nothing selected, nothing dispatched).
        dispatcher = Dispatcher(
            StaticIssueSource([_issue("PLA-1")]), guardrails_validator=validator
        )
        runner = Runner(
            dispatcher, gov, guardrails_validator=validator, state_store=NullStateStore()
        )
        res = runner.run_once(LoggingDispatchSink(), dry_run=True)
        self.assertEqual(res.decision, RunDecision.REFUSED)
        self.assertFalse(res.harness_validated)
        self.assertIsNone(res.contract)


if __name__ == "__main__":
    unittest.main()
