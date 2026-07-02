"""SP-C backlog DISPATCHER — deterministic READY-issue selection (SP-C-3 / PLA-311).

This is the "pull next issue" leg of the autonomous-delivery loop. It is the
deterministic front-half of the loop that :mod:`loop_state_machine` drives: it
selects the next READY work item(s) from the Linear roadmap, gates the selection
through that module's guardrail engine, and emits a **dispatch contract** the
runtime (a scheduled routine or the Shape VM) consumes to actually spawn an agent.
It never spawns agents itself and never writes to Linear — selection is a pure,
side-effect-free transform over a Linear-adapter snapshot.

What it does (PLA-311 acceptance-criteria "pull the next unblocked issue"):

1. **Query** the Linear-adapter snapshot for candidate work items in the
   *Autonomous Delivery Platform* initiative — issues in the ``Backlog`` state,
   NOT already In Progress / In Review / Done, and NOT blocked (an open
   ``blocked-by`` relation excludes an item, fail-closed).
2. **Order** them into the READY queue by the ``adp-wave-N`` label (wave-0 before
   wave-1 …), then priority (Urgent → Low), then age (oldest first).
3. **Gate** the selection through :class:`loop_state_machine.LoopEngine`:
   ``arm_auto_dispatch`` must validate (the guardrail harness is the lights-out
   gate), and every emitted item is checked against the per-issue + global budget
   caps and the fleet circuit-breaker. It **refuses to emit** if not armed.
4. **Emit** the next N READY items as :class:`DispatchContract`\\ s — issue id,
   title, inferred repo, ``<user>/<team>-<n>-<slug>`` branch, and an
   acceptance-criteria pointer (the issue URL).
5. **CLI**: ``python -m loop_dispatcher --dry-run [--limit N]`` prints the READY
   queue + what WOULD be dispatched, with **zero side effects** — the
   bootstrap-phase view before lights-out.

Stdlib only, no third-party deps — the same discipline
:mod:`loop_state_machine` enforces. The Linear transport is an injected seam
(:class:`IssueSource`), so selection/ordering/gating are fully testable offline
and the same code runs live against Linear's GraphQL API.

Run the CLI (from ``governance/loop/``)::

    python3 -m loop_dispatcher --dry-run --limit 3 --issues-file snapshot.json

or against live Linear (needs ``LINEAR_API_KEY``)::

    LINEAR_API_KEY=lin_api_… python3 -m loop_dispatcher --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unittest
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol, Sequence

# ``loop_state_machine`` is a sibling module (this dir is not a package — the
# tests use the same path shim). Import it whether we are run as ``-m
# loop_dispatcher`` (cwd on path) or as a script from the repo root.
try:  # pragma: no cover - exercised both ways depending on invocation
    import loop_state_machine as loop
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import loop_state_machine as loop


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: The Autonomous Delivery Platform initiative (the roadmap the loop pulls from).
ADP_INITIATIVE_ID = "7e522d9b-8cb2-45d2-a8ca-4ee6f83d554d"

#: Linear's GraphQL endpoint (the live :class:`HttpLinearSource` transport).
LINEAR_GRAPHQL_ENDPOINT = "https://api.linear.app/graphql"

#: Only this state *type* is a fresh candidate for dispatch. An item that is
#: ``started`` (In Progress / In Review), ``completed`` (Done), or ``canceled``
#: is already picked up or finished and must never be re-dispatched.
CANDIDATE_STATE_TYPE = "backlog"

#: Blocker state types that no longer block (a Done/cancelled blocker is inert).
INERT_BLOCKER_STATE_TYPES = frozenset({"completed", "canceled"})

#: Last-resort team-key → repo map for repo inference (the description ``Repos:``
#: line and an explicit ``repo:<name>`` label both take precedence — see
#: :func:`infer_repo`). Grounded in the ADP team homes; override via the CLI/API.
DEFAULT_TEAM_REPO_MAP: dict[str, str] = {
    "PLA": "kairix",
    "SGO": "tc-agent-zone",
}

_WAVE_RE = re.compile(r"^adp-wave-(\d+)$", re.IGNORECASE)
_REPO_LABEL_RE = re.compile(r"^repo[:/]\s*([A-Za-z0-9._-]+)$")
_REPOS_LINE_RE = re.compile(r"\*\*Repos:\*\*\s*([A-Za-z0-9._-]+)")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

#: Sort sentinels: an item with no wave sorts after every waved item; a ``None``
#: (Linear priority 0 = "no priority") sorts after every prioritised item.
_NO_WAVE = float("inf")
_NO_PRIORITY_RANK = float("inf")


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Blocker:
    """A ``blocked-by`` relation on a candidate. ``active`` is ``True`` while the
    blocker still blocks (open); a Done/cancelled blocker is inert."""

    id: str
    active: bool = True


@dataclass(frozen=True)
class CandidateIssue:
    """A normalised Linear work item flowing into the READY queue.

    Built from the Linear-adapter snapshot via :meth:`from_linear`, which accepts
    both the raw Linear/MCP issue shape and this module's own export shape.
    """

    id: str
    title: str = ""
    state_type: str = CANDIDATE_STATE_TYPE
    priority: int = 0
    created_at: str = ""
    labels: tuple[str, ...] = ()
    team_key: str = ""
    assignee: Optional[str] = None
    git_branch_name: Optional[str] = None
    description: str = ""
    url: str = ""
    blockers: tuple[Blocker, ...] = ()

    # -- derived views ----------------------------------------------------- #
    @property
    def wave(self) -> Optional[int]:
        """The lowest ``adp-wave-N`` label as an int, or ``None`` if unlabelled."""
        waves = [int(m.group(1)) for lbl in self.labels if (m := _WAVE_RE.match(lbl))]
        return min(waves) if waves else None

    @property
    def open_blockers(self) -> tuple[Blocker, ...]:
        return tuple(b for b in self.blockers if b.active)

    @property
    def is_blocked(self) -> bool:
        return bool(self.open_blockers)

    @property
    def is_candidate(self) -> bool:
        """``True`` iff Backlog and not blocked — eligible for the READY queue."""
        return self.state_type == CANDIDATE_STATE_TYPE and not self.is_blocked

    @property
    def priority_rank(self) -> float:
        """Sort rank for priority: Urgent(1) first … Low(4), then None(0) last."""
        return self.priority if self.priority else _NO_PRIORITY_RANK

    # -- construction ------------------------------------------------------ #
    @classmethod
    def from_linear(cls, raw: dict) -> "CandidateIssue":
        """Build from a Linear/MCP issue dict (or this module's export shape).

        Tolerant of the shape differences between Linear's GraphQL, the MCP
        adapter, and a hand-written snapshot: priority may be an int or
        ``{"value": int}``; state may be flat ``statusType`` or nested
        ``state.type``; labels may be strings or ``{"name": …}``.
        """
        issue_id = str(raw.get("id") or raw.get("identifier") or "").strip()

        priority = raw.get("priority", 0)
        if isinstance(priority, dict):
            priority = priority.get("value", 0)
        priority = int(priority or 0)

        state_type = raw.get("state_type") or raw.get("statusType")
        if not state_type:
            state = raw.get("state")
            if isinstance(state, dict):
                state_type = state.get("type")
        state_type = str(state_type or "").lower()

        labels: list[str] = []
        for lbl in raw.get("labels", ()) or ():
            if isinstance(lbl, str):
                labels.append(lbl)
            elif isinstance(lbl, dict):
                name = lbl.get("name")
                if name:
                    labels.append(str(name))

        team_key = str(raw.get("team_key") or _team_key_from_identifier(issue_id))

        return cls(
            id=issue_id,
            title=str(raw.get("title") or ""),
            state_type=state_type,
            priority=priority,
            created_at=str(raw.get("created_at") or raw.get("createdAt") or ""),
            labels=tuple(labels),
            team_key=team_key,
            assignee=_assignee_name(raw.get("assignee")),
            git_branch_name=raw.get("git_branch_name") or raw.get("gitBranchName"),
            description=str(raw.get("description") or ""),
            url=str(raw.get("url") or ""),
            blockers=_parse_blockers(raw),
        )


@dataclass(frozen=True)
class DispatchContract:
    """The unit the dispatcher emits and the runtime consumes to spawn an agent.

    It carries everything needed to start work — and nothing that spawns it. The
    dispatcher produces it; consumption (agent spawn) is the runtime's job.
    """

    issue_id: str
    title: str
    repo: str
    branch: str
    acceptance_criteria: str
    wave: Optional[int]
    priority: int
    url: str

    def to_dict(self) -> dict:
        return {
            "issue_id": self.issue_id,
            "title": self.title,
            "repo": self.repo,
            "branch": self.branch,
            "acceptance_criteria": self.acceptance_criteria,
            "wave": self.wave,
            "priority": self.priority,
            "url": self.url,
        }


@dataclass(frozen=True)
class DispatchPlan:
    """The result of one dispatch cycle: the READY queue, the emitted contracts,
    what was skipped (and why), and whether the loop was armed."""

    initiative: str
    ready_queue: tuple[CandidateIssue, ...]
    contracts: tuple[DispatchContract, ...]
    skipped: tuple[tuple[str, str], ...]  # (issue_id, reason)
    armed: bool
    refusal: Optional[str]
    per_issue_budget: float
    global_budget: float
    global_spent: float

    def to_dict(self) -> dict:
        return {
            "initiative": self.initiative,
            "armed": self.armed,
            "refusal": self.refusal,
            "budget": {
                "per_issue": self.per_issue_budget,
                "global": self.global_budget,
                "global_spent": self.global_spent,
            },
            "ready_queue": [
                {
                    "id": i.id,
                    "title": i.title,
                    "wave": i.wave,
                    "priority": i.priority,
                    "created_at": i.created_at,
                }
                for i in self.ready_queue
            ],
            "dispatch": [c.to_dict() for c in self.contracts],
            "skipped": [{"id": i, "reason": r} for i, r in self.skipped],
        }


# --------------------------------------------------------------------------- #
# Helpers — parsing + inference (pure, deterministic)
# --------------------------------------------------------------------------- #
def _team_key_from_identifier(identifier: str) -> str:
    """``"PLA-311"`` → ``"PLA"``. The Linear identifier prefix IS the team key."""
    return identifier.split("-", 1)[0].upper() if "-" in identifier else ""


def _assignee_name(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        name = value.get("name") or value.get("displayName")
        return str(name) if name else None
    return str(value)


def _parse_blockers(raw: dict) -> tuple[Blocker, ...]:
    """Extract ``blocked-by`` relations. Fail-closed: a listed blocker with no
    state is assumed active (still blocking) unless it is explicitly inert."""
    out: list[Blocker] = []

    # This module's export shape: [{"id": …, "active": bool}] or ["ID", …].
    for b in raw.get("blockers", ()) or ():
        if isinstance(b, str):
            out.append(Blocker(id=b, active=True))
        elif isinstance(b, dict):
            out.append(Blocker(id=str(b.get("id", "")), active=_blocker_active(b)))

    # Linear/MCP relation shape: relations.blockedBy = [{"id": …, "state": …}].
    relations = raw.get("relations")
    if isinstance(relations, dict):
        for b in relations.get("blockedBy", ()) or ():
            if isinstance(b, dict):
                out.append(Blocker(id=str(b.get("id", "")), active=_blocker_active(b)))

    return tuple(b for b in out if b.id)


def _blocker_active(b: dict) -> bool:
    """A blocker still blocks unless it is explicitly Done/cancelled."""
    if "active" in b:
        return bool(b["active"])
    state = b.get("state") or b.get("statusType") or b.get("state_type")
    if isinstance(state, dict):
        state = state.get("type")
    if state is None:
        return True  # fail-closed: unknown state → assume it still blocks
    return str(state).lower() not in INERT_BLOCKER_STATE_TYPES


def slugify(text: str, *, max_len: int = 48) -> str:
    """A branch-safe slug: lowercase, non-alphanumerics collapsed to ``-``."""
    slug = _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


def _repo_from_repos_line(description: str) -> Optional[str]:
    """The repo named on the ``**Repos:** <name>`` line, or ``None`` if absent."""
    m = _REPOS_LINE_RE.search(description or "")
    return m.group(1) if m else None


def infer_repo(
    issue: CandidateIssue,
    *,
    team_repo_map: Optional[dict[str, str]] = None,
    description_resolver: Optional[Callable[["CandidateIssue"], str]] = None,
) -> str:
    """Infer the target repo for an issue (PLA-311: "infer from team/labels").

    Precedence (most explicit first):

    1. an explicit ``repo:<name>`` label;
    2. the ``**Repos:** <name>`` line in the description (the author's own
       statement — the first-named repo is the primary target). The candidate's
       own (possibly truncated) description is tried first; if it carries no
       ``**Repos:**`` line and a ``description_resolver`` is supplied, the FULL
       description is fetched (``get_issue``, NOT the truncated bulk
       ``list_issues``) and re-parsed;
    3. the ``team-key → repo`` map (last resort).

    The resolver step exists because the live dry-run mis-resolved an SGO issue
    to ``tc-agent-zone`` via the team-key fallback: the bulk ``list_issues``
    descriptions were truncated *before* the ``**Repos:**`` line, so the author's
    real target (e.g. ``tc-fitness``) was invisible. Fetching the full
    description recovers it. The resolver is only invoked when neither the label
    nor the local description resolves — so an explicit label or an untruncated
    ``**Repos:**`` line never pays for a network round-trip.

    Returns ``"unknown"`` if nothing resolves, so the contract never silently
    invents a repo.
    """
    team_repo_map = (
        team_repo_map if team_repo_map is not None else DEFAULT_TEAM_REPO_MAP
    )

    # 1. explicit repo:<name> label — the most authoritative signal, no fetch.
    for lbl in issue.labels:
        m = _REPO_LABEL_RE.match(lbl)
        if m:
            return m.group(1)

    # 2. the **Repos:** line — local (maybe-truncated) description first, then,
    #    only if it has none, the FULL description via the injected resolver.
    repo = _repo_from_repos_line(issue.description)
    if repo:
        return repo
    if description_resolver is not None:
        repo = _repo_from_repos_line(description_resolver(issue))
        if repo:
            return repo

    # 3. team-key → repo map (last resort).
    return team_repo_map.get(issue.team_key, "unknown")


def branch_for(issue: CandidateIssue, *, default_user: str = "dan") -> str:
    """The ``<user>/<team>-<n>-<slug>`` branch for an issue.

    Prefers Linear's own ``gitBranchName`` (already exactly this shape); else
    synthesises ``<user>/<identifier-lowercased>-<title-slug>``.
    """
    if issue.git_branch_name:
        return issue.git_branch_name
    slug = slugify(issue.title)
    stem = issue.id.lower()
    return f"{default_user}/{stem}-{slug}" if slug else f"{default_user}/{stem}"


def _parse_ts(value: str) -> datetime:
    """Parse a Linear ISO-8601 timestamp; missing/garbage sorts last (newest)."""
    if not value:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _ready_sort_key(issue: CandidateIssue) -> tuple[float, float, datetime]:
    """READY ordering: wave (0 first) → priority (Urgent first) → age (oldest)."""
    wave = issue.wave if issue.wave is not None else _NO_WAVE
    return (wave, issue.priority_rank, _parse_ts(issue.created_at))


def ready_queue(issues: Iterable[CandidateIssue]) -> list[CandidateIssue]:
    """Filter to Backlog + unblocked candidates and order them into the READY
    queue. Pure — no side effects, deterministic for a given snapshot."""
    candidates = [i for i in issues if i.is_candidate]
    return sorted(candidates, key=_ready_sort_key)


# --------------------------------------------------------------------------- #
# Guardrail validation — the lights-out gate proof
# --------------------------------------------------------------------------- #
def guardrails_validated(
    *,
    test_dir: Optional[Path] = None,
    pattern: str = "test_loop_guardrails.py",
    verbosity: int = 0,
) -> bool:
    """Run the guardrail harness and report whether it is green.

    This IS the proof :meth:`loop_state_machine.LoopEngine.arm_auto_dispatch`
    demands: auto-dispatch may not arm unless the guardrails are proven to fire.
    Runs the ``governance/loop/tests`` guardrail harness in-process (stdlib
    ``unittest``, no network), returning ``True`` only on a fully-green run. It
    targets the guardrail-validation module specifically (the lights-out gate),
    not the dispatcher's own tests, so a CLI invocation does not re-enter itself.
    """
    test_dir = test_dir or (Path(__file__).resolve().parent / "tests")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(test_dir), pattern=pattern)
    with open(os.devnull, "w", encoding="utf-8") as sink:
        result = unittest.TextTestRunner(stream=sink, verbosity=verbosity).run(suite)
    return result.wasSuccessful()


# --------------------------------------------------------------------------- #
# Issue sources — the injected Linear-adapter transport seam
# --------------------------------------------------------------------------- #
class IssueSource(Protocol):
    """The seam the dispatcher pulls candidates from. Keeps selection testable
    offline and lets the same code read a snapshot file or live Linear."""

    def fetch(self, initiative_id: str) -> list[CandidateIssue]: ...


class DescriptionSource(Protocol):
    """Optional capability a source may expose: fetch an issue's FULL description
    on demand (``get_issue``, not the truncated bulk list).

    A source that implements this lets the dispatcher recover a ``**Repos:**``
    line that the bulk ``list_issues`` snapshot truncated away, so repo
    resolution never mis-falls-through to the team-key map for want of a byte.
    """

    def fetch_description(self, identifier: str) -> str: ...


@dataclass
class StaticIssueSource:
    """An in-memory source — the primary fixture seam for the unit tests."""

    issues: Sequence[CandidateIssue]

    def fetch(self, initiative_id: str) -> list[CandidateIssue]:
        return list(self.issues)


@dataclass
class JsonIssueSource:
    """Reads a Linear-adapter snapshot (the SP-C-7 delegation-tree mirror export).

    Accepts either a bare JSON list of issues or ``{"issues": [...]}`` (the shape
    ``mcp … list_issues`` returns), so a snapshot can be piped straight in.
    """

    path: Path

    def fetch(self, initiative_id: str) -> list[CandidateIssue]:
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        items = data["issues"] if isinstance(data, dict) else data
        return [CandidateIssue.from_linear(i) for i in items]


@dataclass
class HttpLinearSource:
    """Live Linear GraphQL transport (stdlib ``urllib`` only).

    The network call is a thin wrapper; the query builder and response parser
    (:func:`parse_initiative_issues`) are pure and unit-tested against a canned
    payload, so the selection logic is verified without ever touching the wire.
    """

    api_key: str
    endpoint: str = LINEAR_GRAPHQL_ENDPOINT
    opener: Callable[..., object] = urllib.request.urlopen
    timeout: float = 30.0

    def fetch(self, initiative_id: str) -> list[CandidateIssue]:
        payload = self._post(INITIATIVE_ISSUES_QUERY, {"id": initiative_id})
        return parse_initiative_issues(payload)

    def fetch_description(self, identifier: str) -> str:
        """The issue's FULL description (``get_issue``, never truncated).

        Repo resolution calls this lazily, only for a selected READY item whose
        candidate-list description carried no ``**Repos:**`` line, to recover the
        author's real target before falling back to the team-key map. Linear's
        ``issue(id:)`` resolves the human identifier (e.g. ``"SGO-198"``).
        """
        payload = self._post(ISSUE_DESCRIPTION_QUERY, {"id": identifier})
        return parse_issue_description(payload)

    def _post(self, query: str, variables: dict) -> dict:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key,
            },
            method="POST",
        )
        with self.opener(req, timeout=self.timeout) as resp:  # type: ignore[call-arg]
            return json.loads(resp.read().decode("utf-8"))


#: GraphQL: the initiative's projects' issues, with the fields selection needs
#: (state type, labels, priority, age, branch, team key) and the ``blocked-by``
#: relations (an inverse "blocks" relation is a blocker on THIS issue).
INITIATIVE_ISSUES_QUERY = """
query DispatcherCandidates($id: String!) {
  initiative(id: $id) {
    projects {
      nodes {
        issues(first: 250) {
          nodes {
            identifier
            title
            priority
            createdAt
            branchName
            url
            description
            state { type name }
            team { key }
            labels { nodes { name } }
            inverseRelations { nodes { type relatedIssue { identifier state { type } } } }
          }
        }
      }
    }
  }
}
""".strip()


def parse_initiative_issues(payload: dict) -> list[CandidateIssue]:
    """Parse the :data:`INITIATIVE_ISSUES_QUERY` response into candidates.

    Pure — no network. An inverse relation of type ``"blocks"`` means the
    *related* issue blocks THIS one, so it becomes a :class:`Blocker` whose
    ``active`` flag reflects the blocker's own state.
    """
    if "errors" in payload and payload["errors"]:
        raise ValueError(f"Linear GraphQL errors: {payload['errors']}")

    initiative = (payload.get("data") or {}).get("initiative") or {}
    projects = (initiative.get("projects") or {}).get("nodes") or []

    out: list[CandidateIssue] = []
    for project in projects:
        for node in (project.get("issues") or {}).get("nodes") or []:
            blockers: list[dict] = []
            inverse = (node.get("inverseRelations") or {}).get("nodes") or []
            for rel in inverse:
                if rel.get("type") != "blocks":
                    continue
                related = rel.get("relatedIssue") or {}
                blockers.append(
                    {
                        "id": related.get("identifier", ""),
                        "state": (related.get("state") or {}).get("type"),
                    }
                )
            labels = [
                n.get("name")
                for n in (node.get("labels") or {}).get("nodes") or []
                if n.get("name")
            ]
            out.append(
                CandidateIssue.from_linear(
                    {
                        "identifier": node.get("identifier"),
                        "title": node.get("title"),
                        "priority": node.get("priority", 0),
                        "createdAt": node.get("createdAt"),
                        "gitBranchName": node.get("branchName"),
                        "url": node.get("url"),
                        "description": node.get("description"),
                        "state": node.get("state"),
                        "team_key": (node.get("team") or {}).get("key"),
                        "labels": labels,
                        "blockers": blockers,
                    }
                )
            )
    return out


#: GraphQL: one issue's FULL, untruncated description — the ``get_issue`` seam
#: repo resolution falls back to when the bulk candidate list truncated the
#: ``**Repos:**`` line. ``$id`` is the Linear identifier (e.g. ``"SGO-198"``).
ISSUE_DESCRIPTION_QUERY = """
query IssueDescription($id: String!) {
  issue(id: $id) {
    identifier
    description
  }
}
""".strip()


def parse_issue_description(payload: dict) -> str:
    """Parse the :data:`ISSUE_DESCRIPTION_QUERY` response to the raw description.

    Pure — no network. Returns ``""`` for a missing issue (so resolution simply
    falls through to the team-key map) and raises on GraphQL errors (a real
    transport fault must not masquerade as an empty description).
    """
    if payload.get("errors"):
        raise ValueError(f"Linear GraphQL errors: {payload['errors']}")
    issue = (payload.get("data") or {}).get("issue") or {}
    return str(issue.get("description") or "")


# --------------------------------------------------------------------------- #
# The dispatcher
# --------------------------------------------------------------------------- #
def _resolve_description_resolver(
    source: object,
    explicit: Optional[Callable[[CandidateIssue], str]],
) -> Optional[Callable[[CandidateIssue], str]]:
    """Pick the full-description resolver: an explicit callback wins; otherwise
    adapt a source's :class:`DescriptionSource` ``fetch_description`` capability
    (keyed by the issue identifier). ``None`` if neither is available — resolution
    then stops at the local description + team-key map, exactly as before."""
    if explicit is not None:
        return explicit
    fetch_description = getattr(source, "fetch_description", None)
    if callable(fetch_description):
        return lambda issue: fetch_description(issue.id)
    return None


class Dispatcher:
    """Selects, gates, and emits the next READY work-item(s) as contracts.

    The guardrail engine (:mod:`loop_state_machine`) is the single chokepoint:
    every emitted item passes ``arm_auto_dispatch`` (the lights-out gate) and the
    per-issue + global budget / circuit-breaker checks. If the loop cannot arm,
    the dispatcher emits **nothing** (fail-closed) and records the refusal.
    """

    def __init__(
        self,
        source: IssueSource,
        *,
        config: Optional[loop.GuardrailConfig] = None,
        team_repo_map: Optional[dict[str, str]] = None,
        default_user: str = "dan",
        guardrails_validator: Callable[[], bool] = guardrails_validated,
        clock: Callable[[], float] = time.monotonic,
        description_resolver: Optional[Callable[[CandidateIssue], str]] = None,
    ) -> None:
        self.source = source
        self.config = config or loop.GuardrailConfig()
        self.team_repo_map = (
            team_repo_map if team_repo_map is not None else dict(DEFAULT_TEAM_REPO_MAP)
        )
        self.default_user = default_user
        self.guardrails_validator = guardrails_validator
        self.clock = clock
        # Repo resolution fetches a selected item's FULL description only when its
        # candidate-list description carried no **Repos:** line (see infer_repo).
        # Prefer an explicit resolver; else adapt a source that exposes the
        # DescriptionSource capability (e.g. HttpLinearSource.fetch_description).
        self.description_resolver = _resolve_description_resolver(
            source, description_resolver
        )

    def candidates(
        self, initiative_id: str = ADP_INITIATIVE_ID
    ) -> list[CandidateIssue]:
        return self.source.fetch(initiative_id)

    def ready_queue(
        self, initiative_id: str = ADP_INITIATIVE_ID
    ) -> list[CandidateIssue]:
        return ready_queue(self.candidates(initiative_id))

    def contract_for(self, issue: CandidateIssue) -> DispatchContract:
        """Build the dispatch contract for a single READY issue."""
        return DispatchContract(
            issue_id=issue.id,
            title=issue.title,
            repo=infer_repo(
                issue,
                team_repo_map=self.team_repo_map,
                description_resolver=self.description_resolver,
            ),
            branch=branch_for(issue, default_user=self.default_user),
            acceptance_criteria=issue.url or f"linear:{issue.id}",
            wave=issue.wave,
            priority=issue.priority,
            url=issue.url,
        )

    def plan(
        self,
        initiative_id: str = ADP_INITIATIVE_ID,
        *,
        limit: int = 1,
        cost_per_issue: float = 1.0,
    ) -> DispatchPlan:
        """Compute the READY queue and the next ``limit`` dispatch contracts.

        The queue is always computed (a side-effect-free view). Emission is gated:
        the loop must arm, and each item is checked against the budget caps + the
        circuit-breaker. A global-budget trip halts emission for the rest of the
        cycle (fleet circuit-breaker); a per-issue trip or a rate-limit skips only
        that item. Emitting a contract is itself side-effect-free — it does not
        spawn an agent or touch Linear.
        """
        queue = self.ready_queue(initiative_id)

        engine = loop.LoopEngine(self.config, clock=self.clock)
        try:
            engine.arm_auto_dispatch(guardrails_validated=self.guardrails_validator())
        except loop.GuardrailTripped as exc:
            return DispatchPlan(
                initiative=initiative_id,
                ready_queue=tuple(queue),
                contracts=(),
                skipped=(),
                armed=False,
                refusal=str(exc),
                per_issue_budget=self.config.per_issue_budget,
                global_budget=self.config.global_budget,
                global_spent=engine.global_spent,
            )

        contracts: list[DispatchContract] = []
        skipped: list[tuple[str, str]] = []
        for issue in queue:
            if len(contracts) >= limit:
                break
            item = loop.WorkItem(id=issue.id)
            ok, reason = engine.can_dispatch(item, cost_per_issue)
            if not ok:
                skipped.append((issue.id, reason))
                # Global circuit-breaker → halt the whole cycle (fleet-wide);
                # a per-issue / rate-limit skip only drops this one item.
                if engine.halted or "global budget" in reason:
                    break
                continue
            engine.dispatch(item, cost_per_issue)  # consume budget (in-memory only)
            contracts.append(self.contract_for(issue))

        return DispatchPlan(
            initiative=initiative_id,
            ready_queue=tuple(queue),
            contracts=tuple(contracts),
            skipped=tuple(skipped),
            armed=True,
            refusal=None,
            per_issue_budget=self.config.per_issue_budget,
            global_budget=self.config.global_budget,
            global_spent=engine.global_spent,
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _priority_name(priority: int) -> str:
    return {1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}.get(priority, "None")


def _source_from_args(args: argparse.Namespace) -> IssueSource:
    if args.issues_file:
        return JsonIssueSource(Path(args.issues_file))
    api_key = os.environ.get("LINEAR_API_KEY")
    if api_key:
        return HttpLinearSource(api_key)
    raise SystemExit(
        "error: no issue source — pass --issues-file <snapshot.json> or set "
        "LINEAR_API_KEY for the live Linear transport."
    )


def render_plan(plan: DispatchPlan, *, dry_run: bool) -> str:
    """Human-readable render of the READY queue + what WOULD be dispatched."""
    lines: list[str] = []
    n_ready = len(plan.ready_queue)
    lines.append(f"READY queue — Autonomous Delivery Platform ({plan.initiative})")
    lines.append(f"  {n_ready} ready candidate(s) (Backlog, unblocked), wave-ordered:")
    if not plan.ready_queue:
        lines.append("    (none)")
    for rank, issue in enumerate(plan.ready_queue, start=1):
        wave = "-" if issue.wave is None else f"w{issue.wave}"
        created = issue.created_at[:10] or "?"
        lines.append(
            f"    {rank:>2}. [{wave:>3}] {_priority_name(issue.priority):<6} "
            f"{created}  {issue.id:<9} {issue.title[:64]}"
        )

    lines.append("")
    if plan.armed:
        lines.append("Guardrails validated: yes  →  auto-dispatch ARMED")
    else:
        lines.append("Guardrails validated: NO  →  auto-dispatch REFUSED")
        lines.append(f"  refusal: {plan.refusal}")
    lines.append(
        f"Budget: per-issue={plan.per_issue_budget}  global={plan.global_budget}  "
        f"spent-this-cycle={plan.global_spent}"
    )

    verb = "WOULD DISPATCH" if dry_run else "DISPATCH"
    lines.append("")
    lines.append(f"{verb} ({len(plan.contracts)} contract(s)):")
    if not plan.contracts:
        lines.append("    (nothing)")
    for c in plan.contracts:
        lines.append(f"    → {c.issue_id}  repo={c.repo}  branch={c.branch}")
        lines.append(f"        title: {c.title}")
        lines.append(f"        acceptance-criteria: {c.acceptance_criteria}")

    if plan.skipped:
        lines.append("")
        lines.append("Skipped (budget / circuit-breaker):")
        for issue_id, reason in plan.skipped:
            lines.append(f"    - {issue_id}: {reason}")

    if dry_run:
        lines.append("")
        lines.append("[dry-run] no agents spawned, no Linear writes, no side effects.")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop_dispatcher",
        description=(
            "SP-C backlog dispatcher — deterministic READY-issue selection for "
            "the autonomous-delivery loop. Emits a dispatch contract; never "
            "spawns agents or writes to Linear."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the READY queue + what WOULD be dispatched, with no side "
        "effects (the bootstrap-phase view before lights-out).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Max number of READY items to emit as contracts (default: 1).",
    )
    parser.add_argument(
        "--initiative",
        default=ADP_INITIATIVE_ID,
        help="Initiative id to pull candidates from (default: ADP initiative).",
    )
    parser.add_argument(
        "--issues-file",
        help="Path to a Linear-adapter snapshot JSON (list or {'issues': [...]}). "
        "If omitted, LINEAR_API_KEY drives the live transport.",
    )
    parser.add_argument(
        "--user",
        default="dan",
        help="Default branch owner when Linear supplies no gitBranchName.",
    )
    parser.add_argument(
        "--per-issue-budget",
        type=float,
        default=loop.GuardrailConfig.per_issue_budget,
        help="Per-issue budget cap enforced before emitting each contract.",
    )
    parser.add_argument(
        "--global-budget",
        type=float,
        default=loop.GuardrailConfig.global_budget,
        help="Global budget cap (fleet circuit-breaker) for the cycle.",
    )
    parser.add_argument(
        "--cost",
        type=float,
        default=1.0,
        help="Notional cost charged per emitted contract (default: 1.0).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the plan as JSON (the machine dispatch contract) instead of "
        "the human-readable view.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    source = _source_from_args(args)
    config = loop.GuardrailConfig(
        per_issue_budget=args.per_issue_budget,
        global_budget=args.global_budget,
    )
    dispatcher = Dispatcher(source, config=config, default_user=args.user)
    plan = dispatcher.plan(args.initiative, limit=args.limit, cost_per_issue=args.cost)

    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
    else:
        # This module never has external side effects; --dry-run makes the
        # no-side-effects contract explicit in the human view.
        print(render_plan(plan, dry_run=args.dry_run))

    # Exit non-zero only when a dispatch was requested but refused (not armed),
    # so the runtime can distinguish "nothing ready" from "gate closed".
    if not plan.armed and not args.dry_run:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
