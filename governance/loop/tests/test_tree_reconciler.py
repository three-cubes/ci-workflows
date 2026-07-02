"""Tests for the delegation-tree ⇄ Linear sub-issue-tree reconciler (SP-C-7 / PLA-315).

The reconciler is a pure transform over three injected snapshots — the Linear
issue/sub-issue tree, the delegation state (the outcome-recorder ledger), and the
agent branch/PR list — so drift/orphan/stale detection is exercised entirely
offline, matching the stdlib-only / no-network discipline of the loop. The Linear
transport (:class:`HttpTreeSource`) is verified against a canned GraphQL payload,
never the wire.

Run (with the rest of the loop harness) from the repo root::

    python3 -m unittest discover -s governance/loop/tests -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tree_reconciler as tr  # noqa: E402 — path shim above
from tree_reconciler import (  # noqa: E402
    AgentBranch,
    Delegation,
    Finding,
    FindingKind,
    HttpTreeSource,
    ReconcilerInput,
    TreeIssue,
    issue_id_from_branch,
    load_snapshot,
    parse_tree_issues,
    reconcile,
)

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _issue(
    id="PLA-100",
    *,
    state_type="started",
    status_name="In Progress",
    parent_id=None,
    started_at="2026-07-02T00:00:00.000Z",
    updated_at="2026-07-02T00:00:00.000Z",
    pr_urls=(),
    title="",
):
    return TreeIssue(
        id=id,
        state_type=state_type,
        status_name=status_name,
        parent_id=parent_id,
        started_at=started_at,
        updated_at=updated_at,
        pr_urls=tuple(pr_urls),
        title=title or f"work for {id}",
    )


def _deleg(id, issue_id, *, parent_id=None, agent="agent-x", live=True, branch=None, pr=None):
    return Delegation(
        id=id,
        issue_id=issue_id,
        parent_id=parent_id,
        agent=agent,
        live=live,
        branch=branch,
        pr=pr,
    )


def _kinds(report):
    return sorted(f.kind for f in report.findings)


def _for(report, kind):
    return [f for f in report.findings if f.kind == kind]


# --------------------------------------------------------------------------- #
# Branch → issue-id parsing (pairs with PLA-313 linkage)
# --------------------------------------------------------------------------- #
class BranchParseTest(unittest.TestCase):
    def test_org_convention_user_branch(self):
        self.assertEqual(
            issue_id_from_branch("dan/pla-311-sp-c-3-advance-the-loop"), "PLA-311"
        )

    def test_agent_branch(self):
        self.assertEqual(
            issue_id_from_branch("agent/pla-315-delegation-tree-mirror"), "PLA-315"
        )

    def test_takes_first_team_number_not_slug_digits(self):
        # sp-c-3 in the slug must not be mistaken for the work item.
        self.assertEqual(issue_id_from_branch("dan/sgo-198-sp-c-3-thing"), "SGO-198")

    def test_no_team_number_is_none(self):
        self.assertIsNone(issue_id_from_branch("main"))
        self.assertIsNone(issue_id_from_branch("experiment/scratch"))


# --------------------------------------------------------------------------- #
# The happy path: a 2-level delegation mirrors a 2-level issue tree → CLEAN
# (the acceptance-criteria test)
# --------------------------------------------------------------------------- #
class TwoLevelMirrorTest(unittest.TestCase):
    def _two_level(self):
        root_issue = _issue(id="PLA-1", parent_id=None)
        sub_issue = _issue(id="PLA-2", parent_id="PLA-1")
        root_deleg = _deleg("d-root", "PLA-1", parent_id=None, branch="dan/pla-1-root")
        sub_deleg = _deleg(
            "d-sub", "PLA-2", parent_id="d-root", branch="agent/pla-2-sub"
        )
        return [root_issue, sub_issue], [root_deleg, sub_deleg]

    def test_matching_two_level_tree_is_clean(self):
        issues, delegations = self._two_level()
        report = reconcile(issues, delegations, now=NOW)
        self.assertTrue(report.clean, report.to_dict())
        self.assertEqual(report.findings, ())

    def test_killing_a_delegation_marks_its_subissue_stale(self):
        # The AC: killing a delegation marks its sub-issue stale in one pass.
        issues, delegations = self._two_level()
        # kill the sub delegation
        delegations[1] = _deleg(
            "d-sub", "PLA-2", parent_id="d-root", branch="agent/pla-2-sub", live=False
        )
        report = reconcile(issues, delegations, now=NOW)
        stale = _for(report, FindingKind.STALE)
        self.assertEqual([f.issue_id for f in stale], ["PLA-2"])


# --------------------------------------------------------------------------- #
# (a) ORPHAN work — started issue with no delegation OR no linked PR/branch
# --------------------------------------------------------------------------- #
class OrphanWorkTest(unittest.TestCase):
    def test_started_issue_with_no_delegation_is_orphan(self):
        issue = _issue(id="PLA-9", pr_urls=("https://github.com/x/y/pull/1",))
        report = reconcile([issue], [], now=NOW)
        orphans = _for(report, FindingKind.ORPHAN_WORK)
        self.assertEqual([f.issue_id for f in orphans], ["PLA-9"])
        self.assertEqual(orphans[0].reason, "no-delegation")

    def test_started_issue_with_no_link_is_orphan(self):
        # A live delegation exists but nothing ties code to the issue → orphan.
        issue = _issue(id="PLA-9")  # no pr_urls
        deleg = _deleg("d1", "PLA-9")  # no branch / pr
        report = reconcile([issue], [deleg], now=NOW)
        reasons = {f.reason for f in _for(report, FindingKind.ORPHAN_WORK)}
        self.assertIn("no-link", reasons)

    def test_pr_attachment_satisfies_the_link(self):
        issue = _issue(id="PLA-9", pr_urls=("https://github.com/x/y/pull/3",))
        deleg = _deleg("d1", "PLA-9")
        report = reconcile([issue], [deleg], now=NOW)
        self.assertEqual(_for(report, FindingKind.ORPHAN_WORK), [])

    def test_delegation_branch_satisfies_the_link(self):
        issue = _issue(id="PLA-9")
        deleg = _deleg("d1", "PLA-9", branch="agent/pla-9-x")
        report = reconcile([issue], [deleg], now=NOW)
        self.assertEqual(_for(report, FindingKind.ORPHAN_WORK), [])

    def test_agent_branch_satisfies_the_link(self):
        issue = _issue(id="PLA-9")
        deleg = _deleg("d1", "PLA-9")
        branches = [AgentBranch(name="agent/pla-9-x")]
        report = reconcile([issue], [deleg], branches=branches, now=NOW)
        self.assertEqual(_for(report, FindingKind.ORPHAN_WORK), [])

    def test_backlog_issue_is_not_orphan_work(self):
        # Only in-flight (started) issues are candidates for orphan-work.
        issue = _issue(id="PLA-9", state_type="backlog", status_name="Backlog")
        report = reconcile([issue], [], now=NOW)
        self.assertEqual(_for(report, FindingKind.ORPHAN_WORK), [])

    def test_done_issue_is_not_orphan_work(self):
        issue = _issue(id="PLA-9", state_type="completed", status_name="Done")
        report = reconcile([issue], [], now=NOW)
        self.assertTrue(report.clean)


# --------------------------------------------------------------------------- #
# (b) an agent branch/PR with no linked work item (pairs with PLA-313)
# --------------------------------------------------------------------------- #
class UnlinkedBranchTest(unittest.TestCase):
    def test_branch_that_parses_to_no_work_item(self):
        branches = [AgentBranch(name="experiment/scratch")]
        report = reconcile([], [], branches=branches, now=NOW)
        u = _for(report, FindingKind.UNLINKED_BRANCH)
        self.assertEqual([f.branch for f in u], ["experiment/scratch"])
        self.assertEqual(u[0].reason, "no-work-item")

    def test_branch_pointing_at_unknown_issue(self):
        # Parses to PLA-404 but that issue is not in the tree → still unlinked.
        branches = [AgentBranch(name="agent/pla-404-ghost")]
        report = reconcile([_issue(id="PLA-1")], [], branches=branches, now=NOW)
        u = _for(report, FindingKind.UNLINKED_BRANCH)
        self.assertEqual(u[0].reason, "unknown-issue")

    def test_branch_linked_to_known_issue_is_clean(self):
        issue = _issue(id="PLA-1", pr_urls=("https://github.com/x/y/pull/1",))
        deleg = _deleg("d1", "PLA-1")
        branches = [AgentBranch(name="agent/pla-1-x")]
        report = reconcile([issue], [deleg], branches=branches, now=NOW)
        self.assertEqual(_for(report, FindingKind.UNLINKED_BRANCH), [])

    def test_explicit_issue_id_on_branch_is_respected(self):
        branches = [AgentBranch(name="weird-branch", issue_id="PLA-1")]
        report = reconcile([_issue(id="PLA-1")], [_deleg("d", "PLA-1")], branches=branches, now=NOW)
        self.assertEqual(_for(report, FindingKind.UNLINKED_BRANCH), [])


# --------------------------------------------------------------------------- #
# (c) DRIFT — sub-issue whose Linear parent != the delegation-derived parent
# --------------------------------------------------------------------------- #
class DriftTest(unittest.TestCase):
    def test_wrong_parent_relation_is_drift(self):
        issues = [
            _issue(id="PLA-1", parent_id=None),
            _issue(id="PLA-2", parent_id="PLA-1"),
            # PLA-3 was spawned by the PLA-2 delegation but is parented to PLA-1
            _issue(id="PLA-3", parent_id="PLA-1"),
        ]
        delegations = [
            _deleg("d1", "PLA-1", branch="b1"),
            _deleg("d2", "PLA-2", parent_id="d1", branch="b2"),
            _deleg("d3", "PLA-3", parent_id="d2", branch="b3"),  # parent should be PLA-2
        ]
        report = reconcile(issues, delegations, now=NOW)
        drift = _for(report, FindingKind.DRIFT)
        self.assertEqual([f.issue_id for f in drift], ["PLA-3"])
        self.assertIn("PLA-2", drift[0].detail)  # expected parent
        self.assertIn("PLA-1", drift[0].detail)  # actual parent

    def test_missing_parent_relation_is_drift(self):
        issues = [_issue(id="PLA-1"), _issue(id="PLA-2", parent_id=None)]
        delegations = [
            _deleg("d1", "PLA-1", branch="b1"),
            _deleg("d2", "PLA-2", parent_id="d1", branch="b2"),
        ]
        report = reconcile(issues, delegations, now=NOW)
        self.assertEqual([f.issue_id for f in _for(report, FindingKind.DRIFT)], ["PLA-2"])

    def test_root_delegation_no_parent_is_not_drift(self):
        report = reconcile(
            [_issue(id="PLA-1", parent_id=None)],
            [_deleg("d1", "PLA-1", branch="b1")],
            now=NOW,
        )
        self.assertEqual(_for(report, FindingKind.DRIFT), [])


# --------------------------------------------------------------------------- #
# in-flight delegation with no sub-issue → MISSING_SUBISSUE (orphan delegation)
# --------------------------------------------------------------------------- #
class MissingSubissueTest(unittest.TestCase):
    def test_live_delegation_without_a_subissue(self):
        report = reconcile([], [_deleg("d1", "PLA-404", branch="b")], now=NOW)
        m = _for(report, FindingKind.MISSING_SUBISSUE)
        self.assertEqual([f.delegation_id for f in m], ["d1"])

    def test_dead_delegation_without_a_subissue_is_ignored(self):
        # Only *in-flight* delegations require a mirror.
        report = reconcile([], [_deleg("d1", "PLA-404", live=False)], now=NOW)
        self.assertEqual(_for(report, FindingKind.MISSING_SUBISSUE), [])

    def test_delegation_with_empty_issue_id_is_missing(self):
        report = reconcile([], [_deleg("d1", "", branch="b")], now=NOW)
        self.assertEqual(len(_for(report, FindingKind.MISSING_SUBISSUE)), 1)


# --------------------------------------------------------------------------- #
# OVERDUE — In Progress > 3d, For Review > 2d
# --------------------------------------------------------------------------- #
class OverdueTest(unittest.TestCase):
    def _healthy_deleg(self, issue_id):
        return _deleg("d", issue_id, branch="b")

    def test_in_progress_over_three_days_is_overdue(self):
        issue = _issue(
            id="PLA-1",
            status_name="In Progress",
            started_at="2026-06-28T00:00:00.000Z",  # ~4.5d before NOW
        )
        report = reconcile([issue], [self._healthy_deleg("PLA-1")], now=NOW)
        self.assertEqual([f.issue_id for f in _for(report, FindingKind.OVERDUE)], ["PLA-1"])

    def test_in_progress_under_three_days_is_ok(self):
        issue = _issue(
            id="PLA-1", status_name="In Progress", started_at="2026-07-01T00:00:00.000Z"
        )
        report = reconcile([issue], [self._healthy_deleg("PLA-1")], now=NOW)
        self.assertEqual(_for(report, FindingKind.OVERDUE), [])

    def test_in_review_over_two_days_is_overdue(self):
        issue = _issue(
            id="PLA-1",
            status_name="In Review",
            updated_at="2026-06-29T00:00:00.000Z",  # ~3.5d before NOW
        )
        report = reconcile([issue], [self._healthy_deleg("PLA-1")], now=NOW)
        self.assertEqual([f.issue_id for f in _for(report, FindingKind.OVERDUE)], ["PLA-1"])

    def test_in_review_under_two_days_is_ok(self):
        issue = _issue(
            id="PLA-1", status_name="In Review", updated_at="2026-07-01T06:00:00.000Z"
        )
        report = reconcile([issue], [self._healthy_deleg("PLA-1")], now=NOW)
        self.assertEqual(_for(report, FindingKind.OVERDUE), [])


# --------------------------------------------------------------------------- #
# The reconciler never mutates its inputs (report-only, non-destructive)
# --------------------------------------------------------------------------- #
class NonDestructiveTest(unittest.TestCase):
    def test_inputs_are_untouched(self):
        issues = [_issue(id="PLA-1")]
        delegations = [_deleg("d", "PLA-2", branch="b")]  # missing sub-issue
        branches = [AgentBranch(name="orphan/scratch")]
        before = (list(issues), list(delegations), list(branches))
        report = reconcile(issues, delegations, branches=branches, now=NOW)
        self.assertFalse(report.clean)
        self.assertEqual((issues, delegations, branches), before)

    def test_report_proposes_annotations_but_does_not_post(self):
        issue = _issue(id="PLA-1")  # started, no delegation
        report = reconcile([issue], [], now=NOW)
        ann = report.proposed_annotations()
        self.assertTrue(any(a[0] == "PLA-1" for a in ann))
        # annotations are (issue_id, text) proposals — pure data, nothing posted.
        self.assertTrue(all(isinstance(a[1], str) and a[1] for a in ann))


# --------------------------------------------------------------------------- #
# TreeIssue.from_linear tolerance (reuses CandidateIssue parsing)
# --------------------------------------------------------------------------- #
class TreeIssueFromLinearTest(unittest.TestCase):
    def test_parses_mcp_get_issue_shape(self):
        node = TreeIssue.from_linear(
            {
                "id": "PLA-2",
                "title": "sub task",
                "status": "In Progress",
                "statusType": "started",
                "startedAt": "2026-07-01T00:00:00.000Z",
                "updatedAt": "2026-07-02T00:00:00.000Z",
                "parent": {"identifier": "PLA-1"},
                "url": "https://linear.app/x/PLA-2",
            }
        )
        self.assertEqual(node.id, "PLA-2")
        self.assertEqual(node.state_type, "started")
        self.assertEqual(node.status_name, "In Progress")
        self.assertEqual(node.parent_id, "PLA-1")
        self.assertTrue(node.is_started)

    def test_parses_nested_state_name_and_parent_id(self):
        node = TreeIssue.from_linear(
            {
                "identifier": "SGO-9",
                "state": {"type": "started", "name": "In Review"},
                "parentId": "SGO-1",
            }
        )
        self.assertEqual(node.status_name, "In Review")
        self.assertEqual(node.parent_id, "SGO-1")

    def test_pr_urls_pulled_from_attachments(self):
        node = TreeIssue.from_linear(
            {
                "id": "PLA-3",
                "statusType": "started",
                "attachments": [
                    {"url": "https://github.com/o/r/pull/7"},
                    {"url": "https://example.com/doc"},  # not a PR — ignored
                ],
            }
        )
        self.assertEqual(node.pr_urls, ("https://github.com/o/r/pull/7",))


# --------------------------------------------------------------------------- #
# GraphQL tree parser (pure — no network)
# --------------------------------------------------------------------------- #
class ParseTreeTest(unittest.TestCase):
    PAYLOAD = {
        "data": {
            "initiative": {
                "projects": {
                    "nodes": [
                        {
                            "issues": {
                                "nodes": [
                                    {
                                        "identifier": "PLA-1",
                                        "title": "root",
                                        "url": "https://linear.app/x/PLA-1",
                                        "startedAt": "2026-07-01T00:00:00.000Z",
                                        "updatedAt": "2026-07-01T00:00:00.000Z",
                                        "state": {"type": "started", "name": "In Progress"},
                                        "parent": None,
                                        "attachments": {"nodes": []},
                                    },
                                    {
                                        "identifier": "PLA-2",
                                        "title": "sub",
                                        "state": {"type": "started", "name": "In Review"},
                                        "parent": {"identifier": "PLA-1"},
                                        "attachments": {
                                            "nodes": [
                                                {"url": "https://github.com/o/r/pull/9"}
                                            ]
                                        },
                                    },
                                ]
                            }
                        }
                    ]
                }
            }
        }
    }

    def test_parses_parent_state_and_pr(self):
        issues = {i.id: i for i in parse_tree_issues(self.PAYLOAD)}
        self.assertEqual(set(issues), {"PLA-1", "PLA-2"})
        self.assertIsNone(issues["PLA-1"].parent_id)
        self.assertEqual(issues["PLA-2"].parent_id, "PLA-1")
        self.assertEqual(issues["PLA-2"].status_name, "In Review")
        self.assertEqual(issues["PLA-2"].pr_urls, ("https://github.com/o/r/pull/9",))

    def test_graphql_errors_raise(self):
        with self.assertRaises(ValueError):
            parse_tree_issues({"errors": [{"message": "nope"}]})


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpTreeSourceTest(unittest.TestCase):
    def test_posts_initiative_id_and_parses(self):
        captured = {}

        def fake_opener(req, timeout=None):
            captured["auth"] = req.headers.get("Authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(json.dumps(ParseTreeTest.PAYLOAD).encode("utf-8"))

        src = HttpTreeSource("lin_api_x", opener=fake_opener)
        issues = src.fetch("init-123")
        self.assertEqual({i.id for i in issues}, {"PLA-1", "PLA-2"})
        # raw-key auth, no Bearer prefix (Linear personal/workspace key form).
        self.assertEqual(captured["auth"], "lin_api_x")
        self.assertEqual(captured["body"]["variables"], {"id": "init-123"})


# --------------------------------------------------------------------------- #
# Snapshot loading + combined ReconcilerInput
# --------------------------------------------------------------------------- #
class SnapshotTest(unittest.TestCase):
    SNAP = {
        "issues": [
            {"id": "PLA-1", "statusType": "started", "status": "In Progress"},
            {"id": "PLA-2", "statusType": "started", "status": "In Progress",
             "parent": {"identifier": "PLA-1"}},
        ],
        "delegations": [
            {"id": "d1", "issue_id": "PLA-1", "branch": "b1"},
            {"id": "d2", "issue_id": "PLA-2", "parent_id": "d1", "branch": "b2"},
        ],
        "branches": [{"name": "agent/pla-1-x"}, {"name": "agent/pla-2-y"}],
    }

    def test_load_snapshot_builds_input(self):
        inp = load_snapshot(self.SNAP)
        self.assertIsInstance(inp, ReconcilerInput)
        self.assertEqual({i.id for i in inp.issues}, {"PLA-1", "PLA-2"})
        self.assertEqual({d.id for d in inp.delegations}, {"d1", "d2"})
        self.assertEqual(len(inp.branches), 2)

    def test_snapshot_reconciles_clean(self):
        inp = load_snapshot(self.SNAP)
        report = reconcile(inp.issues, inp.delegations, branches=inp.branches, now=NOW)
        self.assertTrue(report.clean, report.to_dict())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class CliTest(unittest.TestCase):
    def _write(self, doc) -> str:
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(doc, fh)
        fh.close()
        return fh.name

    def test_dry_run_reports_findings_no_side_effects(self):
        snap = {
            "issues": [
                {"id": "PLA-9", "statusType": "started", "status": "In Progress"}
            ],
            "delegations": [],
            "branches": [{"name": "experiment/scratch"}],
        }
        path = self._write(snap)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tr.main(["--dry-run", "--snapshot", path])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Reconciliation report", out)
        self.assertIn("PLA-9", out)  # orphan work (no delegation)
        self.assertIn("experiment/scratch", out)  # unlinked branch
        self.assertIn("no side effects", out)

    def test_json_output_is_machine_report(self):
        snap = {
            "issues": [
                {"id": "PLA-9", "statusType": "started", "status": "In Progress"}
            ],
            "delegations": [],
            "branches": [],
        }
        path = self._write(snap)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tr.main(["--json", "--snapshot", path])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertFalse(doc["clean"])
        self.assertTrue(any(f["issue_id"] == "PLA-9" for f in doc["findings"]))

    def test_clean_snapshot_reports_clean(self):
        path = self._write(SnapshotTest.SNAP)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tr.main(["--dry-run", "--snapshot", path, "--now", "2026-07-02T12:00:00Z"])
        self.assertEqual(rc, 0)
        self.assertIn("clean", buf.getvalue().lower())

    def test_fail_on_findings_sets_exit_code(self):
        snap = {
            "issues": [
                {"id": "PLA-9", "statusType": "started", "status": "In Progress"}
            ],
            "delegations": [],
            "branches": [],
        }
        path = self._write(snap)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tr.main(["--snapshot", path, "--fail-on-findings"])
        self.assertEqual(rc, 4)

    def test_missing_source_errors(self):
        import os

        saved = os.environ.pop("LINEAR_API_KEY", None)
        try:
            with self.assertRaises(SystemExit):
                tr.main(["--dry-run"])
        finally:
            if saved is not None:
                os.environ["LINEAR_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
