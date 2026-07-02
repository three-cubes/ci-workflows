"""Tests for the SP-C backlog dispatcher (SP-C-3 / PLA-311).

Selection, ordering, the guardrail gate, repo/branch inference, the Linear
snapshot parser, and the CLI are all exercised offline — the Linear transport is
an injected fake, matching the stdlib-only / no-network discipline of the loop.

Run (with the guardrail harness) from the repo root::

    python3 -m unittest discover -s governance/loop/tests -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop_dispatcher as disp  # noqa: E402 — path shim above
import loop_state_machine as loop  # noqa: E402
from loop_dispatcher import (  # noqa: E402
    Blocker,
    CandidateIssue,
    Dispatcher,
    DispatchContract,
    HttpLinearSource,
    JsonIssueSource,
    StaticIssueSource,
    branch_for,
    infer_repo,
    parse_initiative_issues,
    parse_issue_description,
    ready_queue,
    slugify,
)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _issue(
    id="PLA-100",
    *,
    state_type="backlog",
    priority=2,
    created_at="2026-07-01T12:00:00.000Z",
    labels=("adp-wave-1",),
    description="",
    git_branch_name=None,
    url="",
    blockers=(),
    team_key="",
):
    return CandidateIssue(
        id=id,
        title=f"work for {id}",
        state_type=state_type,
        priority=priority,
        created_at=created_at,
        labels=tuple(labels),
        team_key=team_key or id.split("-", 1)[0],
        git_branch_name=git_branch_name,
        description=description,
        url=url,
        blockers=tuple(blockers),
    )


def _armed_dispatcher(issues, **kw):
    """A dispatcher whose guardrail proof is stubbed green (the harness itself is
    validated by test_loop_guardrails; here we test the *gating*, not re-run it)."""
    kw.setdefault("guardrails_validator", lambda: True)
    return Dispatcher(StaticIssueSource(issues), **kw)


def _armed_dispatcher_from_source(source, **kw):
    """As :func:`_armed_dispatcher` but over an arbitrary (armed) source — used to
    exercise a source that also exposes the ``fetch_description`` capability."""
    kw.setdefault("guardrails_validator", lambda: True)
    return Dispatcher(source, **kw)


# --------------------------------------------------------------------------- #
# Candidate filtering — Backlog only, not blocked
# --------------------------------------------------------------------------- #
class CandidateFilterTest(unittest.TestCase):
    def test_only_backlog_is_a_candidate(self):
        for st in ("started", "completed", "canceled", "unstarted", "triage"):
            self.assertFalse(_issue(state_type=st).is_candidate, st)
        self.assertTrue(_issue(state_type="backlog").is_candidate)

    def test_started_review_done_excluded_from_queue(self):
        issues = [
            _issue(id="PLA-1", state_type="backlog"),
            _issue(id="PLA-2", state_type="started"),  # In Progress / In Review
            _issue(id="PLA-3", state_type="completed"),  # Done
            _issue(id="PLA-4", state_type="canceled"),
        ]
        self.assertEqual([i.id for i in ready_queue(issues)], ["PLA-1"])

    def test_open_blocker_excludes_issue(self):
        blocked = _issue(id="PLA-5", blockers=(Blocker(id="PLA-9", active=True),))
        self.assertTrue(blocked.is_blocked)
        self.assertFalse(blocked.is_candidate)
        self.assertEqual(ready_queue([blocked]), [])

    def test_inert_blocker_does_not_block(self):
        # A Done/cancelled blocker no longer blocks — the item is dispatchable.
        ok = _issue(id="PLA-6", blockers=(Blocker(id="PLA-9", active=False),))
        self.assertFalse(ok.is_blocked)
        self.assertTrue(ok.is_candidate)
        self.assertEqual([i.id for i in ready_queue([ok])], ["PLA-6"])

    def test_blocker_with_unknown_state_fails_closed(self):
        # A blocked-by relation with no known state is assumed to still block.
        issue = CandidateIssue.from_linear(
            {"id": "PLA-7", "statusType": "backlog", "blockers": [{"id": "PLA-9"}]}
        )
        self.assertTrue(issue.is_blocked)


# --------------------------------------------------------------------------- #
# READY ordering — wave → priority → age
# --------------------------------------------------------------------------- #
class ReadyOrderingTest(unittest.TestCase):
    def test_wave_is_the_primary_key(self):
        issues = [
            _issue(id="B", labels=("adp-wave-2",), priority=1),
            _issue(id="A", labels=("adp-wave-0",), priority=4),
            _issue(id="C", labels=("adp-wave-1",), priority=1),
        ]
        # wave-0 first even though its priority is the lowest.
        self.assertEqual([i.id for i in ready_queue(issues)], ["A", "C", "B"])

    def test_priority_breaks_ties_within_a_wave(self):
        issues = [
            _issue(id="LOW", labels=("adp-wave-1",), priority=4),
            _issue(id="URG", labels=("adp-wave-1",), priority=1),
            _issue(id="MED", labels=("adp-wave-1",), priority=3),
        ]
        self.assertEqual([i.id for i in ready_queue(issues)], ["URG", "MED", "LOW"])

    def test_priority_none_sorts_after_prioritised(self):
        issues = [
            _issue(id="NONE", labels=("adp-wave-1",), priority=0),
            _issue(id="LOW", labels=("adp-wave-1",), priority=4),
        ]
        self.assertEqual([i.id for i in ready_queue(issues)], ["LOW", "NONE"])

    def test_age_breaks_priority_ties_oldest_first(self):
        issues = [
            _issue(id="NEW", priority=2, created_at="2026-07-02T00:00:00.000Z"),
            _issue(id="OLD", priority=2, created_at="2026-07-01T00:00:00.000Z"),
        ]
        self.assertEqual([i.id for i in ready_queue(issues)], ["OLD", "NEW"])

    def test_unlabelled_wave_sorts_last(self):
        issues = [
            _issue(id="NOWAVE", labels=(), priority=1),
            _issue(id="WAVE9", labels=("adp-wave-9",), priority=4),
        ]
        self.assertEqual([i.id for i in ready_queue(issues)], ["WAVE9", "NOWAVE"])

    def test_lowest_wave_label_wins_when_multiple(self):
        self.assertEqual(_issue(labels=("adp-wave-3", "adp-wave-1")).wave, 1)


# --------------------------------------------------------------------------- #
# Repo + branch inference
# --------------------------------------------------------------------------- #
class RepoInferenceTest(unittest.TestCase):
    def test_repo_label_wins(self):
        issue = _issue(
            labels=("adp-wave-1", "repo:tc-fitness"),
            description="**Repos:** kairix",
        )
        self.assertEqual(infer_repo(issue), "tc-fitness")

    def test_repos_line_in_description(self):
        issue = _issue(
            description="**Repos:** tc-pipelines (workflows/actions) CORE + tc-fitness"
        )
        self.assertEqual(infer_repo(issue), "tc-pipelines")

    def test_team_map_fallback(self):
        issue = _issue(id="SGO-1", description="no repos line here")
        self.assertEqual(infer_repo(issue), "tc-agent-zone")

    def test_unknown_when_nothing_resolves(self):
        issue = _issue(id="ZZZ-1", description="nothing", team_key="ZZZ")
        self.assertEqual(infer_repo(issue), "unknown")

    def test_custom_team_map(self):
        issue = _issue(id="XYZ-1", description="", team_key="XYZ")
        self.assertEqual(infer_repo(issue, team_repo_map={"XYZ": "myrepo"}), "myrepo")


# --------------------------------------------------------------------------- #
# Repo resolution — full-description fetch fallback (the live dry-run caveat)
#
# The bulk list_issues descriptions are truncated: an SGO issue whose author
# named tc-fitness on a **Repos:** line *below* the truncation point would
# mis-resolve to tc-agent-zone via the team-key fallback. Fetching the FULL
# description (get_issue) before the fallback recovers the real target.
# --------------------------------------------------------------------------- #
class RepoResolutionFetchTest(unittest.TestCase):
    def test_repo_label_wins_without_fetching(self):
        # The most explicit signal short-circuits BEFORE any full-description
        # fetch — a repo:<name> label never pays for a network round-trip.
        calls = []

        def resolver(issue):
            calls.append(issue.id)
            return "**Repos:** tc-agent-zone"

        issue = _issue(
            id="SGO-1",
            labels=("adp-wave-0", "repo:tc-fitness"),
            description="",  # truncated in the bulk list — no Repos line
        )
        self.assertEqual(infer_repo(issue, description_resolver=resolver), "tc-fitness")
        self.assertEqual(calls, [])  # label resolved it — resolver never called

    def test_local_repos_line_short_circuits_fetch(self):
        # An untruncated local **Repos:** line also resolves without a fetch.
        calls = []

        def resolver(issue):
            calls.append(issue.id)
            return "**Repos:** other-repo"

        issue = _issue(id="SGO-3", description="**Repos:** tc-fitness")
        self.assertEqual(infer_repo(issue, description_resolver=resolver), "tc-fitness")
        self.assertEqual(calls, [])

    def test_full_description_repos_line_beats_team_fallback(self):
        # The regression: the candidate-list description was truncated past the
        # **Repos:** line, so locally it resolves to the SGO->tc-agent-zone
        # team fallback; fetching the FULL description recovers tc-fitness.
        full = (
            "Long lead-in that the bulk list_issues call truncated away ...\n\n"
            "**Repos:** tc-fitness (worktree check) CORE"
        )
        issue = _issue(id="SGO-198", description="Long lead-in that the bulk")
        self.assertEqual(infer_repo(issue), "tc-agent-zone")  # truncated → fallback
        self.assertEqual(
            infer_repo(issue, description_resolver=lambda i: full), "tc-fitness"
        )

    def test_fallback_only_when_neither_label_nor_repos_line(self):
        # Full description ALSO carries no **Repos:** line and there is no label →
        # the team-key map is the genuine last resort.
        issue = _issue(id="SGO-2", description="truncated lead-in")
        self.assertEqual(
            infer_repo(issue, description_resolver=lambda i: "no repos line at all"),
            "tc-agent-zone",
        )

    def test_unknown_when_nothing_resolves_even_with_fetch(self):
        issue = _issue(id="ZZZ-9", description="", team_key="ZZZ")
        self.assertEqual(
            infer_repo(issue, description_resolver=lambda i: "still nothing"),
            "unknown",
        )


class BranchInferenceTest(unittest.TestCase):
    def test_prefers_linear_branch_name(self):
        issue = _issue(git_branch_name="dan/pla-311-sp-c-3-advance-the-loop")
        self.assertEqual(branch_for(issue), "dan/pla-311-sp-c-3-advance-the-loop")

    def test_synthesises_user_team_n_slug(self):
        issue = _issue(id="PLA-311", git_branch_name=None)
        self.assertEqual(
            branch_for(issue, default_user="dan"), "dan/pla-311-work-for-pla-311"
        )

    def test_slugify(self):
        self.assertEqual(
            slugify("SP-C-3: Advance the loop!"), "sp-c-3-advance-the-loop"
        )
        self.assertEqual(slugify("   Mixed  Case  "), "mixed-case")


# --------------------------------------------------------------------------- #
# from_linear tolerance
# --------------------------------------------------------------------------- #
class FromLinearTest(unittest.TestCase):
    def test_parses_mcp_shape(self):
        issue = CandidateIssue.from_linear(
            {
                "id": "PLA-311",
                "title": "SP-C-3",
                "priority": {"value": 2, "name": "High"},
                "createdAt": "2026-07-01T11:59:39.225Z",
                "statusType": "backlog",
                "labels": ["adp-wave-3"],
                "gitBranchName": "dan/pla-311-sp-c-3",
                "url": "https://linear.app/x/PLA-311",
            }
        )
        self.assertEqual(issue.id, "PLA-311")
        self.assertEqual(issue.priority, 2)
        self.assertEqual(issue.state_type, "backlog")
        self.assertEqual(issue.wave, 3)
        self.assertEqual(issue.team_key, "PLA")

    def test_parses_nested_state_and_dict_labels(self):
        issue = CandidateIssue.from_linear(
            {
                "identifier": "SGO-9",
                "state": {"type": "backlog", "name": "Backlog"},
                "labels": [{"name": "adp-wave-0"}],
            }
        )
        self.assertEqual(issue.state_type, "backlog")
        self.assertEqual(issue.wave, 0)
        self.assertEqual(issue.team_key, "SGO")

    def test_relations_blocked_by_becomes_blocker(self):
        issue = CandidateIssue.from_linear(
            {
                "id": "PLA-1",
                "statusType": "backlog",
                "relations": {
                    "blockedBy": [{"id": "PLA-2", "state": {"type": "started"}}]
                },
            }
        )
        self.assertTrue(issue.is_blocked)

    def test_relations_blocked_by_done_is_inert(self):
        issue = CandidateIssue.from_linear(
            {
                "id": "PLA-1",
                "statusType": "backlog",
                "relations": {
                    "blockedBy": [{"id": "PLA-2", "state": {"type": "completed"}}]
                },
            }
        )
        self.assertFalse(issue.is_blocked)


# --------------------------------------------------------------------------- #
# The guardrail gate — arming + budgets + circuit-breaker
# --------------------------------------------------------------------------- #
class DispatchGateTest(unittest.TestCase):
    def test_refuses_to_emit_when_not_armed(self):
        d = Dispatcher(
            StaticIssueSource([_issue(id="PLA-1")]),
            guardrails_validator=lambda: False,  # harness not green
        )
        plan = d.plan(limit=5)
        self.assertFalse(plan.armed)
        self.assertEqual(plan.contracts, ())
        self.assertIn("guardrail harness", plan.refusal or "")
        # The READY queue is still computed (a safe, side-effect-free view).
        self.assertEqual([i.id for i in plan.ready_queue], ["PLA-1"])

    def test_emits_up_to_limit_in_ready_order(self):
        issues = [
            _issue(id="W2", labels=("adp-wave-2",), priority=1),
            _issue(id="W0", labels=("adp-wave-0",), priority=2),
            _issue(id="W1", labels=("adp-wave-1",), priority=2),
        ]
        plan = _armed_dispatcher(issues).plan(limit=2)
        self.assertTrue(plan.armed)
        self.assertEqual([c.issue_id for c in plan.contracts], ["W0", "W1"])

    def test_per_issue_budget_is_enforced_before_emit(self):
        # cost 2.0 with a per-issue cap of 1.0 → every item is un-dispatchable.
        d = _armed_dispatcher(
            [_issue(id="PLA-1"), _issue(id="PLA-2")],
            config=loop.GuardrailConfig(per_issue_budget=1.0),
        )
        plan = d.plan(limit=5, cost_per_issue=2.0)
        self.assertEqual(plan.contracts, ())
        self.assertTrue(all("per-issue" in r for _, r in plan.skipped))

    def test_global_budget_circuit_breaker_halts_emission(self):
        issues = [_issue(id=f"PLA-{n}", labels=("adp-wave-1",)) for n in range(5)]
        d = _armed_dispatcher(
            issues, config=loop.GuardrailConfig(global_budget=2.0, per_issue_budget=1e9)
        )
        plan = d.plan(limit=5, cost_per_issue=1.0)
        # Only two fit under the global cap; the breaker then halts the cycle.
        self.assertEqual(len(plan.contracts), 2)
        self.assertLessEqual(plan.global_spent, 2.0)
        self.assertTrue(plan.skipped)

    def test_contract_shape(self):
        issue = _issue(
            id="PLA-311",
            labels=("adp-wave-3",),
            priority=2,
            description="**Repos:** tc-pipelines CORE",
            git_branch_name="dan/pla-311-sp-c-3",
            url="https://linear.app/three-cubes/issue/PLA-311",
        )
        plan = _armed_dispatcher([issue]).plan(limit=1)
        c = plan.contracts[0]
        self.assertIsInstance(c, DispatchContract)
        self.assertEqual(c.issue_id, "PLA-311")
        self.assertEqual(c.repo, "tc-pipelines")
        self.assertEqual(c.branch, "dan/pla-311-sp-c-3")
        self.assertEqual(c.wave, 3)
        self.assertEqual(
            c.acceptance_criteria, "https://linear.app/three-cubes/issue/PLA-311"
        )

    def test_plan_never_mutates_or_spawns(self):
        # The plan is a pure transform: emitting a contract has no external side
        # effect (no agent spawn, no Linear write) — only in-memory budget spend.
        issue = _issue(id="PLA-1")
        d = _armed_dispatcher([issue])
        d.plan(limit=1)
        self.assertEqual(issue.state_type, "backlog")  # source object untouched


# --------------------------------------------------------------------------- #
# GraphQL snapshot parser (pure — no network)
# --------------------------------------------------------------------------- #
class ParseInitiativeTest(unittest.TestCase):
    PAYLOAD = {
        "data": {
            "initiative": {
                "projects": {
                    "nodes": [
                        {
                            "issues": {
                                "nodes": [
                                    {
                                        "identifier": "PLA-311",
                                        "title": "SP-C-3",
                                        "priority": 2,
                                        "createdAt": "2026-07-01T11:59:39.225Z",
                                        "branchName": "dan/pla-311-sp-c-3",
                                        "url": "https://linear.app/x/PLA-311",
                                        "description": "**Repos:** tc-pipelines",
                                        "state": {"type": "backlog", "name": "Backlog"},
                                        "team": {"key": "PLA"},
                                        "labels": {"nodes": [{"name": "adp-wave-3"}]},
                                        "inverseRelations": {"nodes": []},
                                    },
                                    {
                                        "identifier": "PLA-400",
                                        "title": "blocked one",
                                        "priority": 1,
                                        "createdAt": "2026-07-01T10:00:00.000Z",
                                        "state": {"type": "backlog"},
                                        "team": {"key": "PLA"},
                                        "labels": {"nodes": [{"name": "adp-wave-3"}]},
                                        "inverseRelations": {
                                            "nodes": [
                                                {
                                                    "type": "blocks",
                                                    "relatedIssue": {
                                                        "identifier": "PLA-399",
                                                        "state": {"type": "started"},
                                                    },
                                                }
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

    def test_parses_and_filters_blocked(self):
        issues = parse_initiative_issues(self.PAYLOAD)
        self.assertEqual({i.id for i in issues}, {"PLA-311", "PLA-400"})
        rq = ready_queue(issues)
        # PLA-400 is blocked by a started issue → excluded from the READY queue.
        self.assertEqual([i.id for i in rq], ["PLA-311"])
        self.assertEqual(rq[0].wave, 3)

    def test_related_but_not_blocks_relation_ignored(self):
        payload = {
            "data": {
                "initiative": {
                    "projects": {
                        "nodes": [
                            {
                                "issues": {
                                    "nodes": [
                                        {
                                            "identifier": "PLA-1",
                                            "state": {"type": "backlog"},
                                            "team": {"key": "PLA"},
                                            "labels": {"nodes": []},
                                            "inverseRelations": {
                                                "nodes": [
                                                    {
                                                        "type": "related",
                                                        "relatedIssue": {
                                                            "identifier": "PLA-2",
                                                            "state": {
                                                                "type": "started"
                                                            },
                                                        },
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }
        self.assertFalse(parse_initiative_issues(payload)[0].is_blocked)

    def test_graphql_errors_raise(self):
        with self.assertRaises(ValueError):
            parse_initiative_issues({"errors": [{"message": "nope"}]})


# --------------------------------------------------------------------------- #
# Full-description resolution: parser + HttpLinearSource + Dispatcher wiring
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """A minimal context-manager stand-in for urllib's HTTP response."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ParseIssueDescriptionTest(unittest.TestCase):
    def test_parses_full_description(self):
        issue = {"identifier": "SGO-198", "description": "**Repos:** tc-fitness"}
        payload = {"data": {"issue": issue}}
        self.assertEqual(parse_issue_description(payload), "**Repos:** tc-fitness")

    def test_missing_issue_is_empty_string(self):
        self.assertEqual(parse_issue_description({"data": {"issue": None}}), "")
        self.assertEqual(parse_issue_description({}), "")

    def test_null_description_is_empty_string(self):
        self.assertEqual(
            parse_issue_description({"data": {"issue": {"description": None}}}), ""
        )

    def test_graphql_errors_raise(self):
        with self.assertRaises(ValueError):
            parse_issue_description({"errors": [{"message": "nope"}]})


class HttpSourceFetchDescriptionTest(unittest.TestCase):
    def test_fetch_description_posts_identifier_and_parses(self):
        captured = {}

        def fake_opener(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(
                json.dumps(
                    {"data": {"issue": {"description": "**Repos:** tc-fitness CORE"}}}
                ).encode("utf-8")
            )

        src = HttpLinearSource("lin_api_x", opener=fake_opener)
        desc = src.fetch_description("SGO-198")
        self.assertEqual(desc, "**Repos:** tc-fitness CORE")
        # The identifier is passed through as the GraphQL variable.
        self.assertEqual(captured["body"]["variables"], {"id": "SGO-198"})
        # ... and that description resolves to the right repo via infer_repo.
        self.assertEqual(
            infer_repo(_issue(id="SGO-198", description=""),
                       description_resolver=lambda i: desc),
            "tc-fitness",
        )


class DescriptionResolverWiringTest(unittest.TestCase):
    def test_dispatcher_adapts_source_fetch_description(self):
        # A source whose candidate list is truncated but which exposes
        # fetch_description → the contract resolves via the FULL description,
        # NOT the SGO->tc-agent-zone team fallback.
        class TruncatingSource:
            def __init__(self):
                self.fetched = []

            def fetch(self, initiative_id):
                return [
                    _issue(id="SGO-198", labels=("adp-wave-0",), description="lead-in")
                ]

            def fetch_description(self, identifier):
                self.fetched.append(identifier)
                return "**Repos:** tc-fitness (worktree check) CORE"

        src = TruncatingSource()
        plan = _armed_dispatcher_from_source(src).plan(limit=1)
        self.assertEqual(plan.contracts[0].repo, "tc-fitness")
        self.assertEqual(src.fetched, ["SGO-198"])  # fetched lazily, once

    def test_explicit_resolver_wins_over_source(self):
        issue = _issue(id="SGO-9", labels=("adp-wave-0",), description="lead-in")
        d = _armed_dispatcher(
            [issue], description_resolver=lambda i: "**Repos:** tc-fitness"
        )
        self.assertEqual(d.plan(limit=1).contracts[0].repo, "tc-fitness")

    def test_no_resolver_falls_back_to_team_map(self):
        # StaticIssueSource exposes no fetch_description → resolution stops at the
        # local description + team-key map, exactly as before this change.
        issue = _issue(id="SGO-9", labels=("adp-wave-0",), description="lead-in")
        d = _armed_dispatcher([issue])
        self.assertIsNone(d.description_resolver)
        self.assertEqual(d.plan(limit=1).contracts[0].repo, "tc-agent-zone")


# --------------------------------------------------------------------------- #
# JSON source + CLI
# --------------------------------------------------------------------------- #
class JsonSourceTest(unittest.TestCase):
    def test_reads_issues_wrapper_and_bare_list(self):
        items = [{"id": "PLA-1", "statusType": "backlog", "labels": ["adp-wave-1"]}]
        for doc in ({"issues": items}, items):
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            ) as fh:
                json.dump(doc, fh)
                path = fh.name
            got = JsonIssueSource(Path(path)).fetch("init")
            self.assertEqual([i.id for i in got], ["PLA-1"])


class CliTest(unittest.TestCase):
    def _snapshot(self, items) -> str:
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({"issues": items}, fh)
        fh.close()
        return fh.name

    def test_dry_run_prints_queue_no_side_effects(self):
        path = self._snapshot(
            [
                {
                    "id": "SGO-198",
                    "title": "wave zero",
                    "statusType": "backlog",
                    "priority": {"value": 2},
                    "createdAt": "2026-07-01T21:44:39.154Z",
                    "labels": ["adp-wave-0"],
                    "description": "**Repos:** tc-fitness",
                    "gitBranchName": "dan/sgo-198-wave-zero",
                    "url": "https://linear.app/x/SGO-198",
                },
                {
                    "id": "PLA-311",
                    "title": "wave three",
                    "statusType": "started",  # already in progress → excluded
                    "labels": ["adp-wave-3"],
                },
            ]
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = disp.main(["--dry-run", "--limit", "1", "--issues-file", path])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("READY queue", out)
        self.assertIn("SGO-198", out)
        self.assertNotIn("PLA-311", out)  # non-backlog filtered out
        self.assertIn("WOULD DISPATCH", out)
        self.assertIn("no side effects", out)

    def test_json_output_is_machine_contract(self):
        path = self._snapshot(
            [
                {
                    "id": "SGO-198",
                    "title": "wave zero",
                    "statusType": "backlog",
                    "priority": {"value": 2},
                    "labels": ["adp-wave-0"],
                    "description": "**Repos:** tc-fitness",
                    "url": "https://linear.app/x/SGO-198",
                }
            ]
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = disp.main(["--json", "--limit", "1", "--issues-file", path])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertTrue(doc["armed"])
        self.assertEqual(doc["dispatch"][0]["issue_id"], "SGO-198")
        self.assertEqual(doc["dispatch"][0]["repo"], "tc-fitness")

    def test_missing_source_errors(self):
        # No --issues-file and no LINEAR_API_KEY → a clear, non-crashing error.
        import os

        saved = os.environ.pop("LINEAR_API_KEY", None)
        try:
            with self.assertRaises(SystemExit):
                disp.main(["--dry-run"])
        finally:
            if saved is not None:
                os.environ["LINEAR_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
