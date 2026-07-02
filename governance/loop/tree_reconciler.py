"""Delegation-tree ⇄ Linear sub-issue-tree mirror + drift/orphan reconciler (SP-C-7 / PLA-315).

This is the **reconcile** leg of the autonomous-delivery loop: the recursive
`specify → decompose → delegate` machinery materialises a Linear issue/sub-issue
tree that is meant to *mirror* the live agent delegation tree (parent issue = the
delegating agent's work item, sub-issue = the delegated task, ``assignee`` =
human-accountable, ``delegate`` = the agent). Over a long-running fleet those two
trees **drift**: an agent delegation dies but its sub-issue stays In Progress; a
sub-issue gets reparented; work is picked up with no delegation or no code behind
it; a branch/PR exists with nothing to close.

Given the Linear issue tree for the *Autonomous Delivery Platform* initiative,
the delegation state (the outcome-recorder ledger), and the agent branch/PR list,
:func:`reconcile` produces a **reconciliation report** flagging:

* **ORPHAN work** — an in-flight (In Progress / In Review) issue with **no parent
  delegation** owning it, or with **no linked PR/branch** behind it;
* **UNLINKED branch/PR** — an agent branch or PR that maps to **no live work
  item** (pairs with PLA-313's branch⇄work-item linkage);
* **DRIFT** — a sub-issue whose Linear parent relation does **not** match the
  delegation that spawned it;
* **STALE** — a started sub-issue whose delegation is **no longer live** (killed);
* **MISSING sub-issue** — an in-flight delegation with **no mirror** sub-issue;
* **OVERDUE** — an in-flight issue past its SLA (In Progress > 3d, For Review > 2d).

It **REPORTS** — a reconciliation report plus *proposed* Linear annotations — and
never mutates the tree destructively: no reparenting, no state changes, no
deletes. Wiring the proposed annotations to Linear ``save_comment`` (a comment is
a non-destructive annotation) is an explicit, optional live step kept **out** of
this pure module. This promotes the ``delegation-tracker`` Board.md overdue scan
onto the Linear adapter; it runs on the stuck-session cron cadence.

Design mirrors the backlog dispatcher (:mod:`loop_dispatcher`): it **reuses** that
module's tolerant :class:`~loop_dispatcher.CandidateIssue` parser for the base
issue fields and follows the same injected-transport discipline — selection/
detection are pure transforms over an injected snapshot, so everything is testable
offline and the same code runs live against Linear's GraphQL. The live read uses
the **secret-free** Linear key path (fetched from Key Vault via WIF at run time,
never a stored secret — see ``governance/loop/verify-and-close.md``): the CLI
reads ``LINEAR_API_KEY`` from the environment the workflow populates from Key
Vault, scoped to the reconcile step and never written to an output.

Stdlib only, no third-party deps — the determinism the loop enforces.

Run the CLI (from ``governance/loop/``)::

    python3 -m tree_reconciler --dry-run --snapshot tree.json

or against live Linear (needs ``LINEAR_API_KEY``, KV-fetched; the delegation
ledger + branch list come from exported snapshots)::

    LINEAR_API_KEY=lin_api_… python3 -m tree_reconciler --dry-run \
        --delegations-file ledger.json --branches-file branches.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

# ``loop_dispatcher`` is a sibling module (this dir is not a package — the tests
# use the same path shim). Reuse its tolerant issue parser so the delegation
# mirror and the backlog dispatcher cannot drift on how a Linear issue is read.
try:  # pragma: no cover - exercised both ways depending on invocation
    import loop_dispatcher as dispatcher
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import loop_dispatcher as dispatcher


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: The Autonomous Delivery Platform initiative (the roadmap whose tree we mirror).
ADP_INITIATIVE_ID = dispatcher.ADP_INITIATIVE_ID

#: Linear's GraphQL endpoint (the live :class:`HttpTreeSource` transport).
LINEAR_GRAPHQL_ENDPOINT = dispatcher.LINEAR_GRAPHQL_ENDPOINT

#: The state *type* that counts as "in-flight" work — Linear folds both
#: In Progress and In Review under ``started`` (the human status name
#: distinguishes them, which is what the overdue thresholds key off).
STARTED_STATE_TYPE = "started"

#: Overdue thresholds (the ``delegation-tracker`` Board.md overdue scan, promoted).
IN_PROGRESS_MAX_DAYS = 3.0
FOR_REVIEW_MAX_DAYS = 2.0

#: A Linear identifier embedded in a branch name, per the org convention
#: ``<user>/<team>-<number>-<slug>`` (e.g. ``dan/pla-311-…`` / ``agent/pla-315-…``).
#: The FIRST ``<letters>-<digits>`` wins so a ``sp-c-3`` slug is never mistaken
#: for the work item.
_BRANCH_ISSUE_RE = re.compile(r"([A-Za-z]{2,})-(\d+)")

#: A GitHub pull-request URL (evidence real code is linked to an issue — note
#: Linear's *suggested* ``gitBranchName`` is NOT such evidence and is ignored).
_PR_URL_RE = re.compile(r"github\.com/.+/pull/\d+", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Finding taxonomy
# --------------------------------------------------------------------------- #
class FindingKind:
    """The reconciliation finding kinds (the drift/orphan/stale vocabulary)."""

    ORPHAN_WORK = "orphan-work"          # (a) started issue: no delegation / no link
    UNLINKED_BRANCH = "unlinked-branch"  # (b) agent branch/PR with no work item
    DRIFT = "drift"                      # (c) sub-issue parent != delegation parent
    STALE = "stale"                      # started sub-issue whose delegation is dead
    MISSING_SUBISSUE = "missing-subissue"  # in-flight delegation with no mirror
    OVERDUE = "overdue"                  # In Progress >3d / For Review >2d


#: Report ordering — most-actionable / most-structural first.
_SEVERITY: dict[str, int] = {
    FindingKind.MISSING_SUBISSUE: 0,
    FindingKind.STALE: 1,
    FindingKind.DRIFT: 2,
    FindingKind.ORPHAN_WORK: 3,
    FindingKind.UNLINKED_BRANCH: 4,
    FindingKind.OVERDUE: 5,
}


@dataclass(frozen=True)
class Finding:
    """One reconciliation finding. Report-only — it describes a mismatch and the
    annotation that *would* be posted; it never carries out a mutation."""

    kind: str
    subject: str                         # the id/name the finding is anchored to
    detail: str = ""                     # human-readable explanation
    reason: str = ""                     # short machine sub-reason code
    issue_id: Optional[str] = None
    delegation_id: Optional[str] = None
    branch: Optional[str] = None

    @property
    def severity(self) -> int:
        return _SEVERITY.get(self.kind, 99)

    def annotation(self) -> str:
        """The comment text a live run *would* post as a Linear annotation."""
        return f"[reconciler] {self.kind}: {self.detail}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "reason": self.reason,
            "detail": self.detail,
            "issue_id": self.issue_id,
            "delegation_id": self.delegation_id,
            "branch": self.branch,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    """The result of one reconcile pass: the counts and the ranked findings."""

    initiative: str
    issue_count: int
    delegation_count: int
    branch_count: int
    findings: tuple[Finding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings

    def by_kind(self, kind: str) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.kind == kind)

    def proposed_annotations(self) -> tuple[tuple[str, str], ...]:
        """The ``(issue_id, comment-text)`` annotations a live run *could* post.

        Pure data — nothing is written. Only findings anchored to an issue get an
        annotation (a branch-only finding has no issue to comment on)."""
        return tuple(
            (f.issue_id, f.annotation()) for f in self.findings if f.issue_id
        )

    def to_dict(self) -> dict:
        return {
            "initiative": self.initiative,
            "clean": self.clean,
            "counts": {
                "issues": self.issue_count,
                "delegations": self.delegation_count,
                "branches": self.branch_count,
                "findings": len(self.findings),
            },
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# Value objects — the three snapshots the reconciler diffs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TreeIssue:
    """A Linear issue node in the delegation-mirror tree.

    Built via :meth:`from_linear`, which **reuses**
    :meth:`loop_dispatcher.CandidateIssue.from_linear` for the base fields (id,
    state type, title, url) and augments it with the tree-specific fields the
    reconciler needs: the parent-issue relation, the human status name (to tell
    In Progress from In Review for the overdue signal), the started / updated
    timestamps, and any linked PR URLs.
    """

    id: str
    state_type: str = ""
    status_name: str = ""
    parent_id: Optional[str] = None
    started_at: str = ""
    updated_at: str = ""
    pr_urls: tuple[str, ...] = ()
    title: str = ""
    url: str = ""

    @property
    def is_started(self) -> bool:
        """In-flight — In Progress / In Review (Linear's ``started`` type)."""
        return self.state_type == STARTED_STATE_TYPE

    @classmethod
    def from_linear(cls, raw: dict) -> "TreeIssue":
        base = dispatcher.CandidateIssue.from_linear(raw)  # reuse tolerant parse
        return cls(
            id=base.id,
            state_type=base.state_type,
            status_name=_status_name(raw),
            parent_id=_parent_identifier(raw),
            started_at=str(raw.get("started_at") or raw.get("startedAt") or ""),
            updated_at=str(raw.get("updated_at") or raw.get("updatedAt") or ""),
            pr_urls=_pr_urls(raw),
            title=base.title,
            url=base.url,
        )


@dataclass(frozen=True)
class Delegation:
    """A node in the live agent delegation tree (the outcome-recorder ledger).

    ``parent_id`` is the *parent delegation* id (whose ``issue_id`` should be this
    sub-issue's Linear parent); ``live`` is ``True`` while the delegation is still
    in-flight (a killed delegation is ``live=False``)."""

    id: str
    issue_id: str = ""
    parent_id: Optional[str] = None
    agent: Optional[str] = None
    live: bool = True
    branch: Optional[str] = None
    pr: Optional[str] = None

    @property
    def has_link(self) -> bool:
        return bool(self.branch or self.pr)

    @classmethod
    def from_dict(cls, raw: dict) -> "Delegation":
        return cls(
            id=str(raw.get("id") or ""),
            issue_id=str(raw.get("issue_id") or raw.get("issueId") or ""),
            parent_id=raw.get("parent_id") or raw.get("parentId"),
            agent=raw.get("agent") or raw.get("delegate"),
            live=_as_live(raw),
            branch=raw.get("branch"),
            pr=raw.get("pr") or raw.get("pr_url") or raw.get("prUrl"),
        )


@dataclass(frozen=True)
class AgentBranch:
    """An agent branch / PR from the git side (pairs with PLA-313).

    ``issue_id`` is the work item it links to when known; otherwise it is resolved
    from the branch name via the org convention (:func:`issue_id_from_branch`)."""

    name: str
    pr_url: Optional[str] = None
    issue_id: Optional[str] = None

    def linked_issue_id(self) -> Optional[str]:
        """The work item this branch/PR maps to — explicit id wins, else parsed
        from the branch name, else ``None`` (nothing to close)."""
        if self.issue_id:
            return self.issue_id
        return issue_id_from_branch(self.name)

    @classmethod
    def from_dict(cls, raw: dict) -> "AgentBranch":
        return cls(
            name=str(raw.get("name") or raw.get("branch") or ""),
            pr_url=raw.get("pr_url") or raw.get("prUrl") or raw.get("url"),
            issue_id=raw.get("issue_id") or raw.get("issueId"),
        )


@dataclass(frozen=True)
class ReconcilerInput:
    """The three snapshots one reconcile pass diffs."""

    issues: tuple[TreeIssue, ...] = ()
    delegations: tuple[Delegation, ...] = ()
    branches: tuple[AgentBranch, ...] = ()


# --------------------------------------------------------------------------- #
# Pure helpers — parsing (deterministic, no side effects)
# --------------------------------------------------------------------------- #
def issue_id_from_branch(name: str) -> Optional[str]:
    """``"dan/pla-311-sp-c-3-…"`` → ``"PLA-311"`` (the first ``<team>-<n>`` token).

    Returns ``None`` when the branch carries no work-item identifier (e.g.
    ``main`` or ``experiment/scratch``)."""
    m = _BRANCH_ISSUE_RE.search(name or "")
    return f"{m.group(1).upper()}-{m.group(2)}" if m else None


def _status_name(raw: dict) -> str:
    """The human status name (``"In Progress"`` / ``"In Review"`` / …).

    Tolerant of both the MCP ``get_issue`` shape (``status`` is a plain string)
    and Linear's GraphQL shape (``state.name``)."""
    status = raw.get("status")
    if isinstance(status, str):
        return status
    if isinstance(status, dict) and status.get("name"):
        return str(status["name"])
    state = raw.get("state")
    if isinstance(state, dict) and state.get("name"):
        return str(state["name"])
    return ""


def _parent_identifier(raw: dict) -> Optional[str]:
    """The parent issue's identifier, from any of the shapes Linear/MCP emit."""
    parent = raw.get("parent")
    if isinstance(parent, dict):
        ident = parent.get("identifier") or parent.get("id")
        return str(ident) if ident else None
    if isinstance(parent, str):
        return parent
    pid = raw.get("parentId") or raw.get("parent_id")
    return str(pid) if pid else None


def _pr_urls(raw: dict) -> tuple[str, ...]:
    """Linked GitHub PR URLs — explicit ``pr_urls`` or PR-shaped attachments.

    Linear's *suggested* ``gitBranchName`` is deliberately NOT treated as
    evidence of a real branch (every issue has one), so only true PR attachments
    count as a link."""
    explicit = raw.get("pr_urls") or raw.get("prUrls")
    if isinstance(explicit, (list, tuple)):
        return tuple(str(u) for u in explicit if u)

    attachments = raw.get("attachments")
    nodes: Sequence = ()
    if isinstance(attachments, dict):
        nodes = attachments.get("nodes") or ()
    elif isinstance(attachments, (list, tuple)):
        nodes = attachments
    urls = []
    for node in nodes:
        url = node.get("url") if isinstance(node, dict) else None
        if url and _PR_URL_RE.search(str(url)):
            urls.append(str(url))
    return tuple(urls)


def _as_live(raw: dict) -> bool:
    """A delegation is live unless explicitly killed. Accepts ``live`` (bool) or a
    ``status``/``state`` of ``killed`` / ``dead`` / ``done`` / ``complete``."""
    if "live" in raw:
        return bool(raw["live"])
    status = str(raw.get("status") or raw.get("state") or "").lower()
    if not status:
        return True  # unknown → assume in-flight (fail toward flagging)
    return status not in {"killed", "dead", "done", "complete", "completed", "closed"}


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse a Linear ISO-8601 timestamp; ``None`` if missing/garbage."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_days(value: str, now: datetime) -> Optional[float]:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return (now - ts).total_seconds() / 86400.0


def _overdue_reason(issue: TreeIssue, now: datetime) -> Optional[str]:
    """The overdue reason for an in-flight issue, or ``None`` if within SLA.

    In Review keys off the last-update time (~ time in review); In Progress (the
    default for any other started status) keys off the started time."""
    name = issue.status_name.lower()
    if "review" in name:
        age = _age_days(issue.updated_at or issue.started_at, now)
        if age is not None and age > FOR_REVIEW_MAX_DAYS:
            return f"For Review >{FOR_REVIEW_MAX_DAYS:g}d (in review {age:.1f}d)"
        return None
    age = _age_days(issue.started_at or issue.updated_at, now)
    if age is not None and age > IN_PROGRESS_MAX_DAYS:
        return f"In Progress >{IN_PROGRESS_MAX_DAYS:g}d (in progress {age:.1f}d)"
    return None


def _issue_has_link(
    issue: TreeIssue,
    deleg: Optional[Delegation],
    branch_index: dict[str, list[AgentBranch]],
) -> bool:
    """Whether any real code artifact ties to the issue: a PR attachment, the
    delegation's own branch/PR, or an agent branch resolving to this issue."""
    if issue.pr_urls:
        return True
    if deleg is not None and deleg.has_link:
        return True
    return bool(branch_index.get(issue.id))


# --------------------------------------------------------------------------- #
# The reconciler — a pure transform (report-only, non-destructive)
# --------------------------------------------------------------------------- #
def reconcile(
    issues: Sequence[TreeIssue],
    delegations: Sequence[Delegation],
    *,
    branches: Sequence[AgentBranch] = (),
    initiative: str = ADP_INITIATIVE_ID,
    now: Optional[datetime] = None,
) -> ReconciliationReport:
    """Reconcile the Linear issue tree against the delegation state + branch list.

    Pure and deterministic for a given snapshot — it reads the three inputs and
    returns a :class:`ReconciliationReport`. It never mutates the inputs, never
    writes to Linear, and never reparents/relabels/deletes: findings are reported,
    annotations are only *proposed*."""
    now = now or datetime.now(timezone.utc)

    issue_by_id: dict[str, TreeIssue] = {i.id: i for i in issues}
    deleg_by_id: dict[str, Delegation] = {d.id: d for d in delegations}
    deleg_by_issue: dict[str, Delegation] = {}
    for d in delegations:
        if d.issue_id:
            deleg_by_issue.setdefault(d.issue_id, d)  # first delegation wins

    branch_index: dict[str, list[AgentBranch]] = {}
    for b in branches:
        linked = b.linked_issue_id()
        if linked:
            branch_index.setdefault(linked, []).append(b)

    findings: list[Finding] = []

    # (orphan delegation) an in-flight delegation must have a mirror sub-issue.
    for d in delegations:
        if d.live and (not d.issue_id or d.issue_id not in issue_by_id):
            findings.append(
                Finding(
                    kind=FindingKind.MISSING_SUBISSUE,
                    subject=d.id,
                    delegation_id=d.id,
                    issue_id=d.issue_id or None,
                    reason="no-subissue",
                    detail=(
                        f"in-flight delegation {d.id} "
                        f"({d.agent or 'unknown agent'}) has no mirror sub-issue"
                        + (f" for {d.issue_id}" if d.issue_id else "")
                    ),
                )
            )

    # per in-flight issue: orphan (no delegation / no link), stale, overdue.
    for i in issues:
        if not i.is_started:
            continue
        d = deleg_by_issue.get(i.id)

        if d is None:
            findings.append(
                Finding(
                    kind=FindingKind.ORPHAN_WORK,
                    subject=i.id,
                    issue_id=i.id,
                    reason="no-delegation",
                    detail=(
                        f"{i.id} is {i.status_name or 'in-flight'} but no delegation "
                        "owns it (no parent delegation in the tree)"
                    ),
                )
            )
        elif not d.live:
            findings.append(
                Finding(
                    kind=FindingKind.STALE,
                    subject=i.id,
                    issue_id=i.id,
                    delegation_id=d.id,
                    reason="delegation-killed",
                    detail=(
                        f"{i.id} is {i.status_name or 'in-flight'} but its delegation "
                        f"{d.id} is no longer live (killed) — stale"
                    ),
                )
            )

        if not _issue_has_link(i, d, branch_index):
            findings.append(
                Finding(
                    kind=FindingKind.ORPHAN_WORK,
                    subject=i.id,
                    issue_id=i.id,
                    reason="no-link",
                    detail=(
                        f"{i.id} is {i.status_name or 'in-flight'} but has no linked "
                        "PR/branch behind it"
                    ),
                )
            )

        overdue = _overdue_reason(i, now)
        if overdue is not None:
            findings.append(
                Finding(
                    kind=FindingKind.OVERDUE,
                    subject=i.id,
                    issue_id=i.id,
                    reason="sla",
                    detail=f"{i.id} overdue: {overdue}",
                )
            )

    # (drift) a sub-issue's Linear parent must match its delegation-derived parent.
    for i in issues:
        d = deleg_by_issue.get(i.id)
        if d is None or not d.parent_id:
            continue
        parent_deleg = deleg_by_id.get(d.parent_id)
        expected_parent = parent_deleg.issue_id if parent_deleg else None
        if expected_parent and i.parent_id != expected_parent:
            findings.append(
                Finding(
                    kind=FindingKind.DRIFT,
                    subject=i.id,
                    issue_id=i.id,
                    delegation_id=d.id,
                    reason="parent-mismatch",
                    detail=(
                        f"{i.id} should be parented to {expected_parent} (the issue "
                        f"of spawning delegation {d.parent_id}) but its Linear parent "
                        f"is {i.parent_id or 'none'}"
                    ),
                )
            )

    # (unlinked branch) an agent branch/PR must map to a live work item.
    for b in branches:
        linked = b.linked_issue_id()
        if not linked:
            findings.append(
                Finding(
                    kind=FindingKind.UNLINKED_BRANCH,
                    subject=b.name,
                    branch=b.name,
                    reason="no-work-item",
                    detail=f"branch/PR {b.name!r} maps to no work item",
                )
            )
        elif linked not in issue_by_id:
            findings.append(
                Finding(
                    kind=FindingKind.UNLINKED_BRANCH,
                    subject=b.name,
                    branch=b.name,
                    issue_id=linked,
                    reason="unknown-issue",
                    detail=(
                        f"branch/PR {b.name!r} points at {linked}, which is not in "
                        "the reconciled tree"
                    ),
                )
            )

    findings.sort(key=lambda f: (f.severity, f.subject))
    return ReconciliationReport(
        initiative=initiative,
        issue_count=len(issues),
        delegation_count=len(delegations),
        branch_count=len(branches),
        findings=tuple(findings),
    )


# --------------------------------------------------------------------------- #
# Snapshot loading — the offline / dry-run seam
# --------------------------------------------------------------------------- #
def load_snapshot(data: dict) -> ReconcilerInput:
    """Build a :class:`ReconcilerInput` from a combined ``{issues, delegations,
    branches}`` snapshot (the shape the dry-run reads and the tests fixture)."""
    issues = tuple(TreeIssue.from_linear(i) for i in data.get("issues", ()) or ())
    delegations = tuple(
        Delegation.from_dict(d) for d in data.get("delegations", ()) or ()
    )
    branches = tuple(
        AgentBranch.from_dict(b) for b in data.get("branches", ()) or ()
    )
    return ReconcilerInput(issues=issues, delegations=delegations, branches=branches)


# --------------------------------------------------------------------------- #
# Live Linear transport — the injected seam (secret-free key, KV-fetched)
# --------------------------------------------------------------------------- #
class TreeSource(Protocol):
    """The seam the reconciler pulls the Linear issue tree from — keeps detection
    testable offline and lets the same code read a snapshot or live Linear."""

    def fetch(self, initiative_id: str) -> list[TreeIssue]: ...


@dataclass
class HttpTreeSource:
    """Live Linear GraphQL transport for the issue tree (stdlib ``urllib`` only).

    The network call is a thin wrapper; the query builder and
    :func:`parse_tree_issues` response parser are pure and unit-tested against a
    canned payload, so detection is verified without touching the wire. The API
    key is the **secret-free**, KV-fetched Linear key (see the module docstring
    and ``verify-and-close.md``): sent verbatim in the ``Authorization`` header,
    no ``Bearer`` prefix (the Linear personal/workspace-key form)."""

    api_key: str
    endpoint: str = LINEAR_GRAPHQL_ENDPOINT
    opener: Callable[..., object] = urllib.request.urlopen
    timeout: float = 30.0

    def fetch(self, initiative_id: str) -> list[TreeIssue]:
        payload = self._post(INITIATIVE_TREE_QUERY, {"id": initiative_id})
        return parse_tree_issues(payload)

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


#: GraphQL: the initiative's projects' issues with the tree fields the reconciler
#: needs — state type + name, the parent relation, the started/updated timestamps,
#: and PR attachments (the "linked PR" evidence).
INITIATIVE_TREE_QUERY = """
query ReconcilerTree($id: String!) {
  initiative(id: $id) {
    projects {
      nodes {
        issues(first: 250) {
          nodes {
            identifier
            title
            url
            startedAt
            updatedAt
            state { type name }
            parent { identifier }
            attachments { nodes { url } }
          }
        }
      }
    }
  }
}
""".strip()


def parse_tree_issues(payload: dict) -> list[TreeIssue]:
    """Parse the :data:`INITIATIVE_TREE_QUERY` response into :class:`TreeIssue`\\ s.

    Pure — no network. Raises on GraphQL errors (a transport fault must not
    masquerade as an empty tree, which would fabricate orphan/missing findings)."""
    if payload.get("errors"):
        raise ValueError(f"Linear GraphQL errors: {payload['errors']}")

    initiative = (payload.get("data") or {}).get("initiative") or {}
    projects = (initiative.get("projects") or {}).get("nodes") or []

    out: list[TreeIssue] = []
    for project in projects:
        for node in (project.get("issues") or {}).get("nodes") or []:
            out.append(TreeIssue.from_linear(node))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _read_json_list(path: str) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # tolerate a wrapper key ({"delegations": [...]} / {"branches": [...]}).
        for key in ("delegations", "branches", "issues", "items"):
            if key in data:
                return list(data[key])
        return list(data.values())
    return list(data)


def _load_input(args: argparse.Namespace) -> ReconcilerInput:
    """Resolve the three snapshots: a combined ``--snapshot`` file (offline) or a
    live Linear tree read (``LINEAR_API_KEY``, KV-fetched) plus the exported
    delegation ledger / branch list."""
    if args.snapshot:
        data = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        return load_snapshot(data)

    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise SystemExit(
            "error: no source — pass --snapshot <tree.json> or set LINEAR_API_KEY "
            "(the secret-free, KV-fetched Linear key) for the live tree read."
        )
    if not args.delegations_file:
        raise SystemExit(
            "error: a live read needs --delegations-file (the delegation-ledger "
            "export) to reconcile the Linear tree against."
        )
    issues = tuple(HttpTreeSource(api_key).fetch(args.initiative))
    delegations = tuple(
        Delegation.from_dict(d) for d in _read_json_list(args.delegations_file)
    )
    branches = (
        tuple(AgentBranch.from_dict(b) for b in _read_json_list(args.branches_file))
        if args.branches_file
        else ()
    )
    return ReconcilerInput(issues=issues, delegations=delegations, branches=branches)


def render_report(report: ReconciliationReport, *, dry_run: bool) -> str:
    """Human-readable render of the reconciliation report."""
    lines: list[str] = []
    c = report
    lines.append(f"Reconciliation report — Autonomous Delivery Platform ({c.initiative})")
    lines.append(
        f"  {c.issue_count} issue(s), {c.delegation_count} delegation(s), "
        f"{c.branch_count} branch(es) reconciled"
    )
    lines.append("")
    if report.clean:
        lines.append("  ✓ tree is clean — delegation tree and Linear tree agree.")
    else:
        lines.append(f"  {len(report.findings)} finding(s):")
        for f in report.findings:
            ref = f.issue_id or f.delegation_id or f.branch or f.subject
            lines.append(f"    - [{f.kind}] {ref}: {f.detail}")

        annotations = report.proposed_annotations()
        if annotations:
            lines.append("")
            lines.append("  Proposed Linear annotations (NOT posted):")
            for issue_id, text in annotations:
                lines.append(f"    → {issue_id}: {text}")

    if dry_run:
        lines.append("")
        lines.append("[dry-run] report-only — no Linear writes, no side effects.")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tree_reconciler",
        description=(
            "SP-C-7 delegation-tree ⇄ Linear sub-issue-tree reconciler — flags "
            "orphan work, unlinked branches/PRs, parent drift, stale sub-issues, "
            "missing sub-issues, and overdue work. Report-only; never mutates."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the reconciliation report with no side effects (report-only).",
    )
    parser.add_argument(
        "--snapshot",
        help="Combined snapshot JSON {issues, delegations, branches}. If omitted, "
        "LINEAR_API_KEY drives the live tree read (+ --delegations-file).",
    )
    parser.add_argument(
        "--delegations-file",
        help="Delegation-ledger export (JSON list or {'delegations': [...]}). "
        "Required on the live path.",
    )
    parser.add_argument(
        "--branches-file",
        help="Agent branch/PR export (JSON list or {'branches': [...]}). Optional.",
    )
    parser.add_argument(
        "--initiative",
        default=ADP_INITIATIVE_ID,
        help="Initiative id whose tree to reconcile (default: ADP initiative).",
    )
    parser.add_argument(
        "--now",
        help="Override the reconcile clock (ISO-8601) for the overdue signal; "
        "defaults to now. Deterministic snapshots use this.",
    )
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit non-zero (4) when the report is not clean (for CI/cron gating). "
        "By default the reconciler is a pure reporter and always exits 0.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine reconciliation report as JSON.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    inp = _load_input(args)
    now = _parse_ts(args.now) if args.now else None
    report = reconcile(
        inp.issues,
        inp.delegations,
        branches=inp.branches,
        initiative=args.initiative,
        now=now,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_report(report, dry_run=args.dry_run))

    if args.fail_on_findings and not report.clean:
        return 4
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
