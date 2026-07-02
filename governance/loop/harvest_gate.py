# SPDX-License-Identifier: Apache-2.0
"""Harvest-present close-invariant for the autonomous-delivery loop (SP-C-6 / PLA-314).

Promotes ADR-038 (harvest-then-decay / harvest-before-destroy) into a CORE
close-invariant: an ADP work item may not sit in a terminal ``done``/``completed``
state unless it carries a distilled **harvest artifact** — the structured record
of what shipped, the verification result, and the key decisions — recorded on the
issue BEFORE the close transition. No close / teardown without a harvest.

This is the machine-checkable predicate half of the invariant that
``.github/workflows/verify-and-close.yml`` enforces at run time (it distils +
posts the harvest comment, and REFUSES to close if the harvest cannot be
recorded). Keeping the predicate here, next to the loop state machine, means the
spec, the workflow, and this check cannot drift: all three key off the SAME
provenance-stamped marker.

The harvest marker embedded in every artifact is, canonically::

    <!-- adp-harvest issue=<ISSUE-ID> -->

Provenance-stamped (issue id) and idempotent: the marker keys on the issue id, so
a re-run detects the existing harvest and never double-records, and a harvest
stamped for a DIFFERENT issue never satisfies this one. Keep this format
byte-identical to the marker the HARVEST step writes in the workflow.

Stdlib only, no network — matching the determinism the loop harness enforces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# Terminal (closed) Linear state types an ADP issue can land in — the close
# boundary this invariant guards. Mirrors ``loop_state_machine.TERMINAL``'s
# ``done`` at the Linear-status-type layer (``escalated`` is terminal-to-the-loop
# but NOT a delivery close, so it does not require a harvest).
CLOSED_STATE_TYPES = frozenset({"completed", "done"})

# The provenance-stamped harvest marker. Tolerates arbitrary inner whitespace so
# a hand-written harvest still counts, but the issue id must be an exact match.
_MARKER_RE = re.compile(r"<!--\s*adp-harvest\s+issue=(?P<id>[A-Za-z]+-\d+)\s*-->")


def harvest_marker(issue_id: str) -> str:
    """Return the canonical provenance-stamped harvest marker for ``issue_id``.

    Must stay byte-identical to the marker the HARVEST step writes in
    ``.github/workflows/verify-and-close.yml`` (``<!-- adp-harvest issue=... -->``).
    """
    return f"<!-- adp-harvest issue={issue_id} -->"


def has_harvest(comments: Iterable[str], issue_id: str) -> bool:
    """Return True iff some comment carries a harvest artifact stamped for ``issue_id``.

    Idempotent (any number of duplicate harvests still reads as present) and
    provenance-checked (a harvest stamped for another issue does NOT count).
    ``None`` / empty comment bodies are tolerated.
    """
    wanted = issue_id.strip().upper()
    for body in comments:
        for match in _MARKER_RE.finditer(body or ""):
            if match.group("id").upper() == wanted:
                return True
    return False


class MissingHarvest(Exception):
    """A closed ADP issue is missing its harvest artifact — the fail-closed refusal."""

    def __init__(self, issue_id: str) -> None:
        self.issue_id = issue_id
        super().__init__(
            f"close-invariant violated: {issue_id} is closed with no harvest artifact. "
            "run the harvest first — distil what shipped + the verification result + the "
            "key decisions onto the issue (ADR-038 harvest-then-decay), then close. "
            f"expected marker: {harvest_marker(issue_id)}"
        )


@dataclass(frozen=True)
class Issue:
    """The minimal issue shape the close-invariant reads (a Linear-adapter snapshot slice)."""

    id: str
    state_type: str
    comments: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_closed(self) -> bool:
        return self.state_type.strip().lower() in CLOSED_STATE_TYPES

    @property
    def carries_harvest(self) -> bool:
        return has_harvest(self.comments, self.id)


def assert_closed_carries_harvest(issue: Issue) -> None:
    """Enforce the close boundary: a *closed* ADP issue MUST carry a harvest artifact.

    No-op for a still-open issue — the gate fires ONLY at the close boundary, not
    by convention on every state. Raises :class:`MissingHarvest` (an actionable,
    run-harvest-first refusal) for a closed issue with no harvest artifact.
    """
    if issue.is_closed and not issue.carries_harvest:
        raise MissingHarvest(issue.id)
