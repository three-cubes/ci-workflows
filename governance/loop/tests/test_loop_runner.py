"""Tests for the continuous-dispatch DRIVER — the loop runner (SP-C).

The runner composes the deterministic selection leg (``loop_dispatcher``) with the
runtime enforcement leg (``loop_governor``) and an injected agent-spawn seam
(``DispatchSink``). These tests prove it is **fail-safe**: it dispatches ONLY on
the single path where every precondition holds (armed AND live AND harness green
AND no guardrail breach), and in every other state it records the contract but
spawns nothing. The Linear transport and the spawn seam are injected fakes —
stdlib only, no network — matching the discipline the loop enforces.

The load-bearing fail-safe cases (each asserts NO dispatch):
  * disarmed            → RECORDED, sink never called
  * harness-unvalidated → REFUSED, nothing selected
  * per-issue budget    → skipped, IDLE
  * global budget       → HALTED
  * dry-run             → RECORDED
  * armed + logging sink→ seam reached, but logs only (spawned=False)

Run (with the rest of the harness) from the repo root::

    python3 -m unittest discover -s governance/loop/tests -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop_runner as runner_mod  # noqa: E402 — path shim above
from loop_dispatcher import (  # noqa: E402
    CandidateIssue,
    Dispatcher,
    StaticIssueSource,
)
from loop_governor import Governor  # noqa: E402
from loop_runner import (  # noqa: E402
    AgentPlatformSink,
    DispatchResult,
    DispatchSink,
    GitHubActionsHeadlessSink,
    LinearDelegationSink,
    LoggingDispatchSink,
    RunDecision,
    Runner,
)
from loop_state_machine import GuardrailConfig  # noqa: E402


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _issue(
    id="PLA-1",
    *,
    state_type="backlog",
    priority=2,
    created_at="2026-07-01T12:00:00.000Z",
    labels=("adp-wave-1",),
    description="**Repos:** kairix",
    url="https://linear.app/x/PLA-1",
):
    return CandidateIssue(
        id=id,
        title=f"work for {id}",
        state_type=state_type,
        priority=priority,
        created_at=created_at,
        labels=tuple(labels),
        team_key=id.split("-", 1)[0],
        description=description,
        url=url,
    )


def _runner(issues, *, config=None, validator=lambda: True):
    """A runner + its governor over a static source, harness proof stubbed."""
    dispatcher = Dispatcher(
        StaticIssueSource(issues), config=config, guardrails_validator=validator
    )
    governor = Governor(config)
    runner = Runner(dispatcher, governor, guardrails_validator=validator)
    return runner, governor


def _armed_runner(issues, *, config=None):
    runner, governor = _runner(issues, config=config)
    governor.arm(guardrails_validated=True)
    return runner, governor


class _CountingSink:
    """A sink that counts calls and reports a real spawn (the not-safe seam)."""

    def __init__(self):
        self.calls: list[str] = []

    def dispatch(self, contract) -> DispatchResult:
        self.calls.append(contract.issue_id)
        return DispatchResult(
            spawned=True, sink="counting", issue_id=contract.issue_id, ref="spawn-1"
        )


# --------------------------------------------------------------------------- #
# Fail-safe: DISARMED never dispatches (records only)
# --------------------------------------------------------------------------- #
class DisarmedTest(unittest.TestCase):
    def test_disarmed_records_but_never_dispatches_even_when_live(self):
        runner, gov = _runner([_issue(id="PLA-1")])  # NOT armed
        self.assertFalse(gov.armed)
        sink = LoggingDispatchSink()
        res = runner.run_once(sink, dry_run=False)  # live, but disarmed
        self.assertEqual(res.decision, RunDecision.RECORDED)
        self.assertFalse(res.dispatched)
        self.assertEqual(sink.records, [])  # the seam was never reached
        # ...yet the contract WAS recorded, so an operator sees the would-be pick.
        self.assertIsNotNone(res.contract)
        self.assertEqual(res.contract.issue_id, "PLA-1")

    def test_disarmed_with_real_sink_still_never_spawns(self):
        runner, _ = _runner([_issue(id="PLA-1")])
        sink = _CountingSink()
        res = runner.run_once(sink, dry_run=False)
        self.assertEqual(res.decision, RunDecision.RECORDED)
        self.assertEqual(sink.calls, [])  # a real sink is NEVER called while disarmed
        self.assertFalse(res.spawned)


# --------------------------------------------------------------------------- #
# Fail-safe: a red guardrail harness REFUSES outright
# --------------------------------------------------------------------------- #
class HarnessGateTest(unittest.TestCase):
    def test_unvalidated_harness_refuses_and_selects_nothing(self):
        runner, gov = _runner([_issue(id="PLA-1")], validator=lambda: False)
        # Even attempting to arm is refused when the harness is red.
        with self.assertRaises(Exception):
            gov.arm(guardrails_validated=False)
        sink = LoggingDispatchSink()
        res = runner.run_once(sink, dry_run=False)
        self.assertEqual(res.decision, RunDecision.REFUSED)
        self.assertFalse(res.dispatched)
        self.assertIsNone(res.contract)  # nothing selected — cannot trust the gate
        self.assertFalse(res.harness_validated)
        self.assertEqual(sink.records, [])

    def test_red_harness_refuses_even_in_dry_run(self):
        # A red harness is a stop, not a preview: dry-run refuses too.
        runner, _ = _runner([_issue(id="PLA-1")], validator=lambda: False)
        res = runner.run_once(LoggingDispatchSink(), dry_run=True)
        self.assertEqual(res.decision, RunDecision.REFUSED)
        self.assertIsNone(res.contract)


# --------------------------------------------------------------------------- #
# Fail-safe: budget / circuit breaches never dispatch
# --------------------------------------------------------------------------- #
class BudgetGateTest(unittest.TestCase):
    def test_per_issue_budget_exceeded_skips_and_does_not_dispatch(self):
        cfg = GuardrailConfig(per_issue_budget=5.0)
        runner, gov = _armed_runner([_issue(id="PLA-1")], config=cfg)
        gov.record_cost("PLA-1", 5.0)  # already at the per-issue cap
        sink = LoggingDispatchSink()
        res = runner.run_once(sink, dry_run=False)  # armed + live
        self.assertEqual(res.decision, RunDecision.IDLE)
        self.assertFalse(res.dispatched)
        self.assertEqual(sink.records, [])
        self.assertTrue(any("per-issue" in r for _, r in res.skipped))

    def test_global_budget_would_exceed_halts_mid_walk(self):
        cfg = GuardrailConfig(global_budget=2.0, per_issue_budget=1e9)
        runner, gov = _armed_runner([_issue(id="PLA-1")], config=cfg)
        gov.record_cost("PLA-9", 2.0)  # global at the cap; next dispatch would exceed
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.HALTED)
        self.assertFalse(res.dispatched)

    def test_fleet_already_halted_short_circuits_before_selection(self):
        cfg = GuardrailConfig(global_budget=2.0, per_issue_budget=1e9)
        runner, gov = _armed_runner([_issue(id="PLA-1")], config=cfg)
        gov.record_cost("PLA-9", 3.0)  # blows the global cap -> fleet breaker opens
        self.assertTrue(gov.halted)
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.HALTED)
        self.assertFalse(res.dispatched)

    def test_retry_ceiling_bounds_a_runaway_source(self):
        # A mis-wired source that returns the SAME item forever must not loop:
        # the governor's retry ceiling stops re-dispatch after N attempts.
        cfg = GuardrailConfig(retry_ceiling=3, per_issue_budget=1e9, global_budget=1e9)
        runner, gov = _armed_runner([_issue(id="PLA-1")], config=cfg)
        sink = LoggingDispatchSink()
        for _ in range(3):
            self.assertEqual(
                runner.run_once(sink, dry_run=False).decision, RunDecision.DISPATCHED
            )
        # 4th tick: attempts have hit the ceiling -> escalate/skip -> no dispatch.
        res = runner.run_once(sink, dry_run=False)
        self.assertEqual(res.decision, RunDecision.IDLE)
        self.assertEqual(len(sink.records), 3)  # never a 4th spawn
        self.assertTrue(any("retry ceiling" in r for _, r in res.skipped))


# --------------------------------------------------------------------------- #
# Fail-safe: dry-run never dispatches (records only)
# --------------------------------------------------------------------------- #
class DryRunTest(unittest.TestCase):
    def test_dry_run_records_but_never_dispatches_even_when_armed(self):
        runner, _ = _armed_runner([_issue(id="PLA-1")])
        sink = LoggingDispatchSink()
        res = runner.run_once(sink, dry_run=True)  # armed, but dry-run
        self.assertEqual(res.decision, RunDecision.RECORDED)
        self.assertFalse(res.dispatched)
        self.assertEqual(sink.records, [])
        self.assertIsNotNone(res.contract)

    def test_dry_run_default_is_record_only(self):
        # run_once defaults to dry_run=True — the safe default.
        runner, _ = _armed_runner([_issue(id="PLA-1")])
        res = runner.run_once(LoggingDispatchSink())
        self.assertEqual(res.decision, RunDecision.RECORDED)
        self.assertTrue(res.dry_run)


# --------------------------------------------------------------------------- #
# The single dispatch path: armed AND live — and with the safe sink, logs only
# --------------------------------------------------------------------------- #
class DispatchPathTest(unittest.TestCase):
    def test_armed_live_with_logging_sink_reaches_seam_but_only_logs(self):
        runner, gov = _armed_runner([_issue(id="PLA-1")])
        sink = LoggingDispatchSink()
        res = runner.run_once(sink, dry_run=False)  # the ONLY dispatch path
        self.assertEqual(res.decision, RunDecision.DISPATCHED)
        self.assertTrue(res.dispatched)  # the seam WAS invoked
        self.assertFalse(res.spawned)  # ...but the safe sink spawned nothing
        self.assertEqual([c.issue_id for c in sink.records], ["PLA-1"])
        self.assertEqual(res.dispatch_result.sink, "logging")
        # The governor's cross-tick ledger advanced on the real dispatch.
        self.assertEqual(gov.ledger("PLA-1").attempts, 1)
        self.assertEqual(gov.ledger("PLA-1").cost_spent, 1.0)

    def test_armed_live_with_real_sink_spawns(self):
        runner, _ = _armed_runner([_issue(id="PLA-1")])
        sink = _CountingSink()
        res = runner.run_once(sink, dry_run=False)
        self.assertEqual(res.decision, RunDecision.DISPATCHED)
        self.assertTrue(res.spawned)
        self.assertEqual(sink.calls, ["PLA-1"])

    def test_selects_ready_queue_head_in_wave_priority_order(self):
        issues = [
            _issue(id="W2", labels=("adp-wave-2",), priority=1),
            _issue(id="W0", labels=("adp-wave-0",), priority=2),
            _issue(id="W1", labels=("adp-wave-1",), priority=2),
        ]
        runner, _ = _armed_runner(issues)
        sink = LoggingDispatchSink()
        res = runner.run_once(sink, dry_run=False)
        self.assertEqual(res.contract.issue_id, "W0")  # wave-0 first
        self.assertEqual(res.ready, ("W0", "W1", "W2"))


# --------------------------------------------------------------------------- #
# Idle / empty queue
# --------------------------------------------------------------------------- #
class IdleTest(unittest.TestCase):
    def test_empty_queue_is_idle(self):
        runner, _ = _armed_runner([])  # nothing ready
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.IDLE)
        self.assertFalse(res.dispatched)

    def test_non_backlog_only_is_idle(self):
        runner, _ = _armed_runner([_issue(id="PLA-1", state_type="started")])
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        self.assertEqual(res.decision, RunDecision.IDLE)


# --------------------------------------------------------------------------- #
# The sinks
# --------------------------------------------------------------------------- #
class SinkTest(unittest.TestCase):
    def test_logging_sink_records_and_reports_no_spawn(self):
        sink = LoggingDispatchSink()
        contract = _armed_runner([_issue(id="PLA-1")])[0]._dispatcher.contract_for(
            _issue(id="PLA-1")
        )
        result = sink.dispatch(contract)
        self.assertFalse(result.spawned)
        self.assertEqual(result.sink, "logging")
        self.assertEqual(sink.records, [contract])

    def test_stub_sinks_are_not_implemented(self):
        contract = _armed_runner([_issue(id="PLA-1")])[0]._dispatcher.contract_for(
            _issue(id="PLA-1")
        )
        for sink in (
            GitHubActionsHeadlessSink(),
            AgentPlatformSink(),
            LinearDelegationSink(),
        ):
            with self.assertRaises(NotImplementedError):
                sink.dispatch(contract)

    def test_all_sinks_satisfy_the_protocol(self):
        for sink in (
            LoggingDispatchSink(),
            GitHubActionsHeadlessSink(),
            AgentPlatformSink(),
            LinearDelegationSink(),
        ):
            self.assertIsInstance(sink, DispatchSink)


# --------------------------------------------------------------------------- #
# RunResult serialisation
# --------------------------------------------------------------------------- #
class RunResultTest(unittest.TestCase):
    def test_to_dict_is_json_serialisable(self):
        runner, _ = _armed_runner([_issue(id="PLA-1")])
        res = runner.run_once(LoggingDispatchSink(), dry_run=False)
        doc = res.to_dict()
        # round-trips through JSON without error
        reloaded = json.loads(json.dumps(doc))
        self.assertEqual(reloaded["decision"], "dispatched")
        self.assertEqual(reloaded["contract"]["issue_id"], "PLA-1")
        self.assertFalse(reloaded["spawned"])


# --------------------------------------------------------------------------- #
# CLI — dry-run + record-only by default; the harness proof is stubbed for speed
# --------------------------------------------------------------------------- #
class CliTest(unittest.TestCase):
    def _snapshot(self, items) -> str:
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({"issues": items}, fh)
        fh.close()
        return fh.name

    def _issue_doc(self, id="SGO-198", state="backlog"):
        return {
            "id": id,
            "title": "wave zero",
            "statusType": state,
            "priority": {"value": 2},
            "createdAt": "2026-07-01T21:44:39.154Z",
            "labels": ["adp-wave-0"],
            "description": "**Repos:** tc-fitness",
            "gitBranchName": "dan/sgo-198-wave-zero",
            "url": "https://linear.app/x/SGO-198",
        }

    def test_dry_run_default_records_no_side_effects(self):
        path = self._snapshot([self._issue_doc()])
        buf = io.StringIO()
        with mock.patch.object(runner_mod, "guardrails_validated", lambda: True):
            with redirect_stdout(buf):
                rc = runner_mod.main(["--issues-file", path])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("RECORDED", out)
        self.assertIn("SGO-198", out)
        self.assertIn("no dispatch", out)

    def test_json_output_is_machine_readable(self):
        path = self._snapshot([self._issue_doc()])
        buf = io.StringIO()
        with mock.patch.object(runner_mod, "guardrails_validated", lambda: True):
            with redirect_stdout(buf):
                rc = runner_mod.main(["--json", "--issues-file", path])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["decision"], "recorded")
        self.assertFalse(doc["armed"])
        self.assertFalse(doc["dispatched"])
        self.assertEqual(doc["contract"]["issue_id"], "SGO-198")

    def test_armed_live_over_snapshot_logs_only(self):
        path = self._snapshot([self._issue_doc()])
        state = os.path.join(tempfile.mkdtemp(), "loop-state.json")
        buf = io.StringIO()
        with mock.patch.object(runner_mod, "guardrails_validated", lambda: True):
            with redirect_stdout(buf):
                # A durable --state-file is REQUIRED for an armed+live tick (else
                # the guardrails would be inert). soak_ticks defaults to 0 here.
                rc = runner_mod.main(
                    ["--json", "--armed", "--live", "--issues-file", path,
                     "--state-file", state]
                )
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["decision"], "dispatched")
        self.assertTrue(doc["armed"])
        self.assertTrue(doc["dispatched"])
        self.assertFalse(doc["spawned"])  # CLI wires only LoggingDispatchSink
        self.assertEqual(doc["dispatch_result"]["sink"], "logging")

    def test_armed_live_without_state_file_refuses(self):
        # FIX 1 fail-closed: an armed+live tick with NO durable store is refused
        # up front (a non-durable ledger resets every tick -> inert guardrails).
        path = self._snapshot([self._issue_doc()])
        buf = io.StringIO()
        with mock.patch.object(runner_mod, "guardrails_validated", lambda: True):
            with redirect_stdout(buf):
                rc = runner_mod.main(
                    ["--json", "--armed", "--live", "--issues-file", path]
                )
        self.assertEqual(rc, 3)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["decision"], "refused")
        self.assertIn("durable", doc["reason"].lower())

    def test_armed_live_refused_when_harness_red_exits_nonzero(self):
        path = self._snapshot([self._issue_doc()])
        buf = io.StringIO()
        with mock.patch.object(runner_mod, "guardrails_validated", lambda: False):
            with redirect_stdout(buf):
                rc = runner_mod.main(["--json", "--armed", "--live", "--issues-file", path])
        self.assertEqual(rc, 3)  # a closed gate surfaces to the schedule
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["decision"], "refused")

    def test_no_source_dry_run_is_green_skip(self):
        saved = os.environ.pop("LINEAR_API_KEY", None)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = runner_mod.main([])  # no --issues-file, no key
            self.assertEqual(rc, 0)  # bootstrap dry-run stays green
            self.assertIn("no Linear source", buf.getvalue())
        finally:
            if saved is not None:
                os.environ["LINEAR_API_KEY"] = saved

    def test_no_source_armed_live_surfaces_nonzero(self):
        saved = os.environ.pop("LINEAR_API_KEY", None)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = runner_mod.main(["--armed", "--live"])
            self.assertEqual(rc, 3)  # a real armed tick with no source is a misconfig
        finally:
            if saved is not None:
                os.environ["LINEAR_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
