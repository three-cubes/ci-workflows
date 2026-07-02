# SPDX-License-Identifier: Apache-2.0
"""Close-invariant harness: a closed ADP issue MUST carry a harvest artifact (SP-C-6 / PLA-314).

Encodes the PLA-314 sabotage test — force-close with no harvest is REFUSED; close
AFTER a harvest succeeds — and the provenance-stamped, idempotent harvest
predicate the workflow and the spec share. Stdlib only, no network; runs under the
same discovery as the loop guardrail harness:

    python3 -m unittest discover -s governance/loop/tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harvest_gate import (  # noqa: E402 — path shim above
    Issue,
    MissingHarvest,
    assert_closed_carries_harvest,
    harvest_marker,
    has_harvest,
)


def _harvest_comment(issue_id: str) -> str:
    """A realistic distilled-harvest comment carrying the provenance marker."""
    return (
        f"## Harvest — {issue_id} (ADR-038 harvest-then-decay)\n\n"
        "**Shipped:** did the thing (#42)\n\n"
        "**Verification:** PASS on `abc123`\n\n"
        "**Key decisions**\n\nWent with option A.\n\n"
        f"---\n_Provenance: issue={issue_id}._\n\n{harvest_marker(issue_id)}\n"
    )


# --------------------------------------------------------------------------- #
# The harvest predicate — provenance-stamped + idempotent
# --------------------------------------------------------------------------- #
class HarvestPredicateTest(unittest.TestCase):
    def test_marker_is_provenance_stamped_with_the_issue_id(self):
        self.assertEqual(harvest_marker("PLA-314"), "<!-- adp-harvest issue=PLA-314 -->")

    def test_present_when_a_matching_harvest_exists(self):
        self.assertTrue(has_harvest([_harvest_comment("PLA-314")], "PLA-314"))

    def test_absent_with_no_comments(self):
        self.assertFalse(has_harvest([], "PLA-314"))

    def test_a_plain_comment_is_not_a_harvest(self):
        self.assertFalse(
            has_harvest(["lgtm, merging", "verification-confirmed on abc123"], "PLA-314")
        )

    def test_provenance_checked_a_harvest_for_another_issue_does_not_satisfy(self):
        # A harvest stamped for a DIFFERENT issue must never count for this one.
        self.assertFalse(has_harvest([_harvest_comment("PLA-999")], "PLA-314"))

    def test_idempotent_duplicate_harvests_still_read_as_present(self):
        dupes = [_harvest_comment("PLA-314"), _harvest_comment("PLA-314")]
        self.assertTrue(has_harvest(dupes, "PLA-314"))

    def test_issue_id_match_is_case_insensitive(self):
        self.assertTrue(has_harvest([_harvest_comment("PLA-314")], "pla-314"))

    def test_none_and_empty_bodies_are_tolerated(self):
        self.assertFalse(has_harvest([None, ""], "PLA-314"))  # type: ignore[list-item]

    def test_marker_tolerates_inner_whitespace(self):
        self.assertTrue(has_harvest(["<!--   adp-harvest   issue=PLA-314   -->"], "PLA-314"))


# --------------------------------------------------------------------------- #
# The close-invariant — fail-closed at the close boundary (the sabotage test)
# --------------------------------------------------------------------------- #
class CloseInvariantTest(unittest.TestCase):
    def test_closed_with_harvest_passes(self):
        issue = Issue(
            id="PLA-314", state_type="completed", comments=(_harvest_comment("PLA-314"),)
        )
        assert_closed_carries_harvest(issue)  # no raise

    def test_force_close_with_no_harvest_is_refused(self):
        # SABOTAGE: force-close with no harvest -> refused, with an actionable message.
        issue = Issue(id="PLA-314", state_type="completed", comments=("lgtm",))
        with self.assertRaises(MissingHarvest) as ctx:
            assert_closed_carries_harvest(issue)
        self.assertEqual(ctx.exception.issue_id, "PLA-314")
        self.assertIn("run the harvest first", str(ctx.exception))
        self.assertIn(harvest_marker("PLA-314"), str(ctx.exception))

    def test_done_state_type_also_requires_a_harvest(self):
        issue = Issue(id="PLA-314", state_type="done", comments=())
        with self.assertRaises(MissingHarvest):
            assert_closed_carries_harvest(issue)

    def test_an_open_issue_is_not_required_to_carry_a_harvest(self):
        # The gate fires ONLY at the close boundary — an open issue is exempt.
        for state_type in ("backlog", "unstarted", "started"):
            issue = Issue(id="PLA-314", state_type=state_type, comments=())
            assert_closed_carries_harvest(issue)  # no raise
            self.assertFalse(issue.is_closed)

    def test_close_after_harvest_succeeds_end_to_end(self):
        # Before harvest: closing is refused. After harvest: closing is allowed.
        pre = Issue(id="PLA-314", state_type="completed", comments=())
        with self.assertRaises(MissingHarvest):
            assert_closed_carries_harvest(pre)
        post = Issue(
            id="PLA-314",
            state_type="completed",
            comments=("lgtm", _harvest_comment("PLA-314")),
        )
        assert_closed_carries_harvest(post)  # no raise
        self.assertTrue(post.carries_harvest)

    def test_a_harvest_for_a_sibling_issue_does_not_unlock_this_close(self):
        # Provenance is load-bearing at the boundary, not just in the predicate.
        issue = Issue(
            id="PLA-314", state_type="completed", comments=(_harvest_comment("PLA-999"),)
        )
        with self.assertRaises(MissingHarvest):
            assert_closed_carries_harvest(issue)


if __name__ == "__main__":
    unittest.main()
