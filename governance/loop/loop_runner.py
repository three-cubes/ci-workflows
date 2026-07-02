"""Continuous-dispatch DRIVER — the loop RUNNER (SP-C / Autonomous Delivery Platform).

This is the piece that replaces the human hand-cranking the autonomous-delivery
loop. On a cadence (a scheduled GitHub-Actions tick — see
``.github/workflows/loop-dispatch.yml``) it self-selects the next READY work item,
guardrail-gates it, and — **only when explicitly ARMED** — hands it to an
agent-spawn seam (a :class:`DispatchSink`). It never spawns agents itself: the
spawn is delegated to an injected sink, and the safe default sink
(:class:`LoggingDispatchSink`) spawns nothing at all.

It is **fail-safe by construction**:

* **DISARMED by default.** Nothing dispatches unless a governor was explicitly
  armed (:meth:`loop_governor.Governor.arm`) *and* the guardrail harness
  re-validates *this tick* (the :meth:`loop_state_machine.LoopEngine.arm_auto_dispatch`
  precondition, re-checked live — not trusted from arm-time).
* **Every guardrail breach short-circuits.** A red harness → ``REFUSED``; a
  fleet-wide breaker (global budget / cross-issue circuit) → ``HALTED``; an
  item over its per-issue budget or retry ceiling is skipped. None of these
  reach the spawn seam.
* **Dry-run and disarmed both record-only.** In either state the runner computes
  and *records* the :class:`~loop_dispatcher.DispatchContract` it WOULD dispatch,
  but never calls ``sink.dispatch`` — so an operator sees "what would happen"
  with zero side effects.

It composes the two existing halves of the loop rather than re-implementing them,
so the runtime cannot drift from the spec:

* :mod:`loop_dispatcher` — the deterministic "pull the next READY issue" leg
  (selection, ordering, repo/branch inference, contract construction). Pure over
  an injected Linear-adapter snapshot.
* :mod:`loop_governor` — the runtime that ENFORCES the hard stop-conditions
  (retry ceiling, per-issue + global budget, cross-issue circuit-breaker,
  determinism admission, lights-out arming). The governor holds the budget/retry
  ledger; the runner consults it *before* selecting and only advances it on a
  real dispatch.

Cross-tick durability (why the guardrails actually bite): every scheduled tick is
a **fresh process**, so an in-memory-only ledger would reset each tick and the
retry / budget / circuit-breaker ceilings would NEVER accumulate — the guardrails
would be inert (fail-open). The runner therefore loads a **durable**
:class:`~loop_governor.StateStore` (a versioned JSON blob the workflow materialises
from a dedicated ``loop-state`` git ref / Azure blob — never the evictable Actions
cache) into the governor at the START of every tick, before any guardrail check,
and writes it back on the dispatch path. The persisted blob — not process memory —
is the authoritative cross-tick ledger, so the ceilings trip ACROSS ticks. An
armed + live tick against a NON-durable store is refused (that would leave the
guardrails inert). See :class:`~loop_governor.JsonFileStateStore` and ARMING.md.

Soak before live (no cold live tick): even once armed + live, the runner keeps the
first ``soak_ticks`` armed ticks in record-only mode (the soak counter lives in the
same durable blob), so an operator reviews the would-dispatch decisions before any
real dispatch — the very first armed cron tick can never go live.

Determinism note: the determinism guardrail (no ``--reruns`` / network / unpinned
seed) is a *verification-time* admission (:meth:`Governor` / ``admit_verification``
on the close side, ``verify-and-close``), not a pre-dispatch selection guard.
The runner's pre-select gate therefore covers arming → fleet breakers → retry
ceiling → per-issue budget → global budget; determinism is enforced where the
verdict is rendered, so the two cannot be conflated.

Stdlib only, no third-party deps — the same discipline the rest of ``loop/``
keeps. The Linear transport is an injected seam (via the :class:`Dispatcher`), so
selection/gating/dispatch decisions are fully testable offline.

Run the CLI (from ``governance/loop/``), dry-run + record-only by default::

    python3 -m loop_runner --json --limit 1 --issues-file snapshot.json

or against live Linear (needs ``LINEAR_API_KEY``)::

    LINEAR_API_KEY=lin_api_… python3 -m loop_runner --json

Arming is a deliberate act (``--armed --live``). The DEFAULT sink is still the
safe :class:`LoggingDispatchSink` (records, spawns nothing); the REAL runtime is
:class:`GitHubActionsDispatchSink` (GHA-hosted headless ``claude -p`` per item),
opt-in via ``--sink github-actions`` and reached ONLY on the armed + live +
past-soak path — see ``governance/loop/ARMING.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Protocol, Tuple, runtime_checkable

# ``loop_dispatcher`` / ``loop_governor`` / ``loop_state_machine`` are sibling
# modules (this dir is not a package — the tests use the same path shim). Import
# them whether we run as ``-m loop_runner`` (cwd on path) or as a script from the
# repo root.
try:  # pragma: no cover - exercised both ways depending on invocation
    import loop_dispatcher as dispatch_mod
    import loop_governor as governor_mod
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import loop_dispatcher as dispatch_mod
    import loop_governor as governor_mod

from loop_dispatcher import (
    ADP_INITIATIVE_ID,
    CandidateIssue,
    Dispatcher,
    DispatchContract,
    HttpLinearSource,
    IssueSource,
    JsonIssueSource,
    guardrails_validated,
)
from loop_governor import (
    ContinueAction,
    Governor,
    JsonFileStateStore,
    NullStateStore,
    StateStore,
    StateStoreError,
)


# --------------------------------------------------------------------------- #
# The dispatch sink — the pluggable agent-spawn seam
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DispatchResult:
    """What a :class:`DispatchSink` reports back after being handed a contract.

    ``spawned`` is the load-bearing bit: it is ``True`` only if the sink actually
    started an agent. The safe default sink (:class:`LoggingDispatchSink`) records
    the contract and returns ``spawned=False`` — the seam was reached, but nothing
    ran.
    """

    spawned: bool
    sink: str
    issue_id: str
    detail: str = ""
    ref: Optional[str] = None  # a run URL / spawn id / delegate id, when a real sink sets one

    def to_dict(self) -> dict:
        return {
            "spawned": self.spawned,
            "sink": self.sink,
            "issue_id": self.issue_id,
            "detail": self.detail,
            "ref": self.ref,
        }


@runtime_checkable
class DispatchSink(Protocol):
    """The seam the runner hands a gated :class:`DispatchContract` to when (and
    ONLY when) the loop is armed and running live.

    An implementation is where "spawn an agent for this work item" actually
    happens. The runner treats this as an opaque boundary: it decides *whether*
    to dispatch (all the guardrails), the sink decides *how*. Keeping this a
    protocol lets the org pick the runtime later without touching the driver —
    the three candidate seams below are the documented options.
    """

    def dispatch(self, contract: DispatchContract) -> DispatchResult: ...


@dataclass
class LoggingDispatchSink:
    """The SAFE DEFAULT sink: records the contract, spawns **nothing**.

    This is what makes arming currently safe. Even on the fully-armed, live path
    (``armed AND not dry_run``), if this sink is wired the runner reaches the seam
    and the contract is merely appended to :attr:`records` — no agent is started,
    no external system is touched. It is the intended dry-run recorder and the
    intentional "no runtime chosen yet" placeholder until one of the stub sinks
    below is implemented.
    """

    records: list[DispatchContract] = field(default_factory=list)

    def dispatch(self, contract: DispatchContract) -> DispatchResult:
        self.records.append(contract)
        return DispatchResult(
            spawned=False,
            sink="logging",
            issue_id=contract.issue_id,
            detail=(
                "recorded the dispatch contract; spawned nothing (LoggingDispatchSink "
                "is the safe default — no runtime wired)"
            ),
        )


# --------------------------------------------------------------------------- #
# The REAL spawn seam — GHA-hosted headless `claude -p` per item.
#
# This is the concrete, wired agent-execution runtime that turns a selected work
# item into a working bot PR. It triggers ``.github/workflows/loop-implement.yml``
# via a ``workflow_dispatch`` REST call, passing the contract's issue id, branch,
# and repo as inputs; the workflow then federates to Azure (WIF), reads the model
# key + Linear key from Key Vault, mints the bot App token, checks out the target
# repo on the issue branch, and runs ``claude -p`` headless to implement the one
# item and open the bot PR — mirroring the KV+WIF secret-free pattern in
# ``verify-and-close.yml``. NOTHING here spawns until the runner has already
# decided armed + live + past-soak (see :meth:`Runner.run_once`); this class is
# the seam the runner hands the gated contract to on that single path.
# --------------------------------------------------------------------------- #

#: Injectable HTTP transport seam: ``(url, body_bytes, headers) -> (status, text)``.
#: The default (:func:`_default_dispatch_transport`) uses stdlib ``urllib``; tests
#: inject a fake so the dispatch is fully exercisable offline (no network).
DispatchTransport = Callable[[str, bytes, dict], Tuple[int, str]]

# INJECTION-SAFE validation: every value that reaches the GitHub REST call (the
# contract's issue id / branch / repo AND the orchestrator repo + workflow file)
# is matched against a strict allowlist pattern BEFORE it is placed in the URL or
# the JSON body. Anything outside the pattern raises — nothing is ever spliced
# into a request raw.
_ISSUE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")  # e.g. SGO-76
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")  # a Linear gitBranchName, e.g. dan/sgo-76-slug
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")  # owner/name (the orchestrator repo)
# The TARGET repo the executor runs against: the dispatcher infers a BARE name
# (e.g. ``kairix``) or an ``owner/name``; loop-implement.yml qualifies a bare name
# with the org owner. Either shape is accepted; ``unknown`` (the dispatcher's
# "could not resolve" sentinel) is rejected so we never spawn against no repo.
_TARGET_REPO_RE = re.compile(r"^(?:[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+$")
_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")  # a workflow file name


def _default_dispatch_transport(url: str, body: bytes, headers: dict) -> Tuple[int, str]:
    """POST ``body`` to ``url`` via stdlib ``urllib`` (no third-party deps).

    Returns ``(status, text)``. An ``HTTPError`` is caught and surfaced as its
    status code so the caller decides how to treat a non-2xx (the sink fails
    CLOSED on anything but 201/204)."""
    req = urllib.request.Request(  # noqa: S310 - fixed GitHub API host, values validated
        url, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 - fixed GitHub API host
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # pragma: no cover - network error shape
        return exc.code, exc.read().decode("utf-8", "replace")


@dataclass
class GitHubActionsDispatchSink:
    """REAL, WIRED spawn seam: trigger ``loop-implement.yml`` for the contract.

    On :meth:`dispatch` it POSTs a ``workflow_dispatch`` to the GitHub REST API
    against the orchestrator repo's ``.github/workflows/loop-implement.yml``,
    passing the contract's ``issue_id`` / ``branch`` / ``repo`` as workflow inputs.
    A 201/204 means the GHA-hosted executor run was spawned; anything else fails
    CLOSED (raises) so the runner never records a phantom spawn.

    The ``token`` is the bot App installation token (minted secret-free via
    WIF + Key Vault by the caller — never a human PAT); the HTTP transport is an
    injected seam (:data:`DispatchTransport`), so the dispatch decision is fully
    testable offline. This sink is only ever REACHED on the runner's single
    armed + live + past-soak path — the gating lives in :meth:`Runner.run_once`,
    not here.
    """

    token: str
    repo: str  # orchestrator repo hosting loop-implement.yml, "owner/name"
    workflow: str = "loop-implement.yml"
    ref: str = "main"
    api_url: str = "https://api.github.com"
    transport: DispatchTransport = _default_dispatch_transport
    # H1: auto-merge is STRICTLY OPT-IN. This is passed as the workflow's
    # `enable-auto-merge` input; loop-implement.yml only auto-merges a product-repo
    # PR when it is true. The caller (_build_sink) sets it True ONLY on the armed +
    # live dispatch path, so a manually-triggered or logging dispatch never
    # auto-merges. Default False keeps every other construction review-only.
    enable_auto_merge: bool = False

    def _validate(self, contract: DispatchContract) -> None:
        """Reject anything outside the strict allowlist BEFORE it reaches a
        request — the contract fields AND this sink's own repo/workflow config."""
        if not _ISSUE_ID_RE.match(contract.issue_id or ""):
            raise ValueError(f"GitHubActionsDispatchSink: refusing unsafe issue id {contract.issue_id!r}")
        if not _BRANCH_RE.match(contract.branch or ""):
            raise ValueError(f"GitHubActionsDispatchSink: refusing unsafe branch {contract.branch!r}")
        if (contract.repo or "") == "unknown" or not _TARGET_REPO_RE.match(contract.repo or ""):
            raise ValueError(f"GitHubActionsDispatchSink: refusing unresolved/unsafe target repo {contract.repo!r}")
        if not _REPO_RE.match(self.repo or ""):
            raise ValueError(f"GitHubActionsDispatchSink: refusing unsafe orchestrator repo {self.repo!r}")
        if not _WORKFLOW_RE.match(self.workflow or ""):
            raise ValueError(f"GitHubActionsDispatchSink: refusing unsafe workflow {self.workflow!r}")
        if not self.token:
            raise ValueError("GitHubActionsDispatchSink: no dispatch token (fail-closed)")

    def dispatch(self, contract: DispatchContract) -> DispatchResult:
        self._validate(contract)
        url = f"{self.api_url}/repos/{self.repo}/actions/workflows/{self.workflow}/dispatches"
        payload = {
            "ref": self.ref,
            "inputs": {
                "issue-id": contract.issue_id,
                "issue-branch": contract.branch,
                "repo": contract.repo,
                # H1: workflow_dispatch inputs are strings; GitHub coerces the
                # boolean input. Auto-merge stays opt-in — this is "true" only when
                # the sink was built on the armed+live path (see _build_sink); the
                # workflow itself also defaults it false for any other caller.
                "enable-auto-merge": "true" if self.enable_auto_merge else "false",
            },
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "tc-loop-dispatch",
        }
        status, text = self.transport(url, body, headers)
        # workflow_dispatch returns 204 No Content on success (some proxies 201).
        if status not in (201, 204):
            raise RuntimeError(
                f"GitHubActionsDispatchSink: workflow_dispatch for {contract.issue_id} "
                f"failed — HTTP {status}: {text[:200]!r}"
            )
        # The dispatch API returns no run id, so the best available run ref is the
        # workflow's runs page (filter by branch/actor there).
        run_ref = f"https://github.com/{self.repo}/actions/workflows/{self.workflow}"
        return DispatchResult(
            spawned=True,
            sink="github-actions",
            issue_id=contract.issue_id,
            detail=(
                f"triggered {self.workflow} (workflow_dispatch @ {self.ref}) on {self.repo} "
                f"for {contract.issue_id} → {contract.repo}@{contract.branch}"
            ),
            ref=run_ref,
        )


# --------------------------------------------------------------------------- #
# STUB spawn seams — the OTHER pluggable candidates the org considered.
#
# Each is a clearly-marked STUB: a documented candidate for the live agent-spawn
# runtime that was NOT chosen. Every one raises NotImplementedError with a
# one-paragraph wiring note. The chosen + realized runtime is
# :class:`GitHubActionsDispatchSink` above; these remain as the historical menu
# so a future reviewer can see the alternatives (see ARMING.md).
# --------------------------------------------------------------------------- #
@dataclass
class GitHubActionsHeadlessSink:
    """STUB · the ORIGINAL, un-parameterised description of the GHA-hosted spawn
    seam — **now REALIZED as :class:`GitHubActionsDispatchSink`** (above).

    Wiring note (as chosen + built): on dispatch, this seam triggers a per-item
    GitHub-Actions run (``workflow_dispatch`` against
    ``.github/workflows/loop-implement.yml``) that checks out the contract's
    ``repo`` at the contract's ``branch`` and runs ``claude -p`` in headless/print
    mode with the acceptance criteria as the prompt, the agent's own App token
    minted via WIF + Key Vault (never a human PAT), and the model-provider API key
    read from Key Vault at run time (never a stored GitHub secret) — mirroring the
    KV+WIF pattern in ``verify-and-close.yml``. It needs no standing host (each item
    is an ephemeral GitHub-hosted runner) and inherits Actions' concurrency + audit
    log, and it respects the governor's one-item-per-tick cadence so a fan-out
    cannot trip the shared per-actor Linear/API quota.

    Kept as the historical, un-parameterised menu entry; the concrete wired
    implementation is :class:`GitHubActionsDispatchSink`. It deliberately still
    raises so nothing selects the un-parameterised stub by accident.
    """

    def dispatch(self, contract: DispatchContract) -> DispatchResult:
        raise NotImplementedError(
            "GitHubActionsHeadlessSink is the historical STUB description — the live "
            "GHA-hosted `claude -p` spawn is now realized by GitHubActionsDispatchSink "
            "(parameterised with the dispatch token + orchestrator repo). See that "
            "class and governance/loop/ARMING.md."
        )


@dataclass
class AgentPlatformSink:
    """STUB · candidate spawn seam: hand off to the standing agent-platform runner.

    Wiring note: on dispatch, this seam would POST the contract to the standing
    tc-agent-zone / vm-openclaw agent-platform runner (the persistent OpenClaw
    gateway), which owns agent lifecycle, the subagent-spawning decision hook
    (SP-C-5: no delegation without a linked work item), and the token-logger spend
    path (SGO-44) that feeds the governor's budget counters back. It is attractive
    because the platform already enforces identity + the delegation guardrail at
    the spawn boundary and keeps warm capacity (no per-item cold start), but it
    introduces a standing host to operate and its availability becomes a loop
    dependency (the capability-health probe would gate dispatch on it). Not
    implemented here on purpose — the live spawn is deferred until the org picks a
    runtime.
    """

    def dispatch(self, contract: DispatchContract) -> DispatchResult:
        raise NotImplementedError(
            "AgentPlatformSink is a documented STUB candidate — the live hand-off "
            "to the tc-agent-zone / vm-openclaw runner is intentionally not wired. "
            "See the class docstring and governance/loop/ARMING.md."
        )


@dataclass
class LinearDelegationSink:
    """STUB · candidate spawn seam: set the Linear delegate to trigger the native
    agent integration.

    Wiring note: on dispatch, this seam would set the work item's Linear
    ``delegate = agent`` (the assignee-is-human / delegate-is-agent convention),
    letting Linear's own native agent integration pick the item up — so dispatch
    becomes a single idempotent Linear mutation and the delegation tree is
    authored where the roadmap already lives (mirrors the SP-C-7 delegation-tree
    ⇄ sub-issue mirror). It is attractive because it adds no bespoke runtime and
    keeps one source of truth, but it couples the loop to Linear's integration
    availability + semantics and needs the same write-scoped Linear key the close
    side uses (KV+WIF), plus care that re-delegating an in-flight item is a no-op.
    Not implemented here on purpose — the live spawn is deferred until the org
    picks a runtime.
    """

    def dispatch(self, contract: DispatchContract) -> DispatchResult:
        raise NotImplementedError(
            "LinearDelegationSink is a documented STUB candidate — the live "
            "Linear delegate=agent mutation is intentionally not wired. See the "
            "class docstring and governance/loop/ARMING.md."
        )


# --------------------------------------------------------------------------- #
# Run result
# --------------------------------------------------------------------------- #
class RunDecision(str, Enum):
    """The terminal disposition of one :meth:`Runner.run_once` tick."""

    REFUSED = "refused"  # a hard precondition failed (harness red / cannot arm) — nothing selected
    HALTED = "halted"  # a fleet-wide runtime breaker is open (global budget / circuit) — no dispatch
    IDLE = "idle"  # no READY, dispatchable work item this tick — nothing to do
    RECORDED = "recorded"  # a contract was selected + recorded but NOT dispatched (dry-run / disarmed)
    DISPATCHED = "dispatched"  # the spawn seam was invoked for the selected contract (armed AND live)


#: Decisions in which the spawn seam (``sink.dispatch``) was definitely NOT reached.
_NO_SEAM = frozenset(
    {RunDecision.REFUSED, RunDecision.HALTED, RunDecision.IDLE, RunDecision.RECORDED}
)


@dataclass(frozen=True)
class RunResult:
    """The record of one governed dispatch tick.

    ``contract`` is the selected work item's contract, recorded whenever one was
    selected — even when it was NOT dispatched (dry-run / disarmed), so an
    operator can always see "what would dispatch". ``dispatched`` is ``True`` iff
    the runner actually invoked ``sink.dispatch`` (the ``DISPATCHED`` decision);
    ``dispatch_result.spawned`` then says whether that sink really started an
    agent (``False`` for the safe :class:`LoggingDispatchSink`).
    """

    decision: RunDecision
    reason: str
    armed: bool
    harness_validated: bool
    dry_run: bool
    dispatched: bool
    initiative: str
    contract: Optional[DispatchContract] = None
    dispatch_result: Optional[DispatchResult] = None
    ready: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def spawned(self) -> bool:
        """``True`` only if a real agent was started (never for the logging sink)."""
        return bool(self.dispatch_result and self.dispatch_result.spawned)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "armed": self.armed,
            "harness_validated": self.harness_validated,
            "dry_run": self.dry_run,
            "dispatched": self.dispatched,
            "spawned": self.spawned,
            "initiative": self.initiative,
            "contract": self.contract.to_dict() if self.contract else None,
            "dispatch_result": (
                self.dispatch_result.to_dict() if self.dispatch_result else None
            ),
            "ready": list(self.ready),
            "skipped": [{"id": i, "reason": r} for i, r in self.skipped],
        }


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
class Runner:
    """The continuous-dispatch driver: one guardrail-gated tick per call.

    It wires the deterministic selection leg (:class:`Dispatcher`) to the runtime
    enforcement leg (:class:`Governor`) and, only when every precondition holds,
    to an injected agent-spawn seam (:class:`DispatchSink`). It holds no live
    spawn logic of its own — that is the sink's job, and the default sink spawns
    nothing.

    The guardrail harness validator is shared with the dispatcher by default
    (``dispatcher.guardrails_validator``), so the runner's own live self-check and
    the dispatcher's arming proof cannot disagree — one proof, checked once per
    tick, gates both.
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        governor: Governor,
        *,
        guardrails_validator: Optional[Callable[[], bool]] = None,
        initiative: str = ADP_INITIATIVE_ID,
        cost_per_issue: float = 1.0,
        state_store: Optional[StateStore] = None,
        soak_ticks: int = 0,
    ) -> None:
        self._dispatcher = dispatcher
        self._governor = governor
        # Default to the dispatcher's proof so the two never diverge; an explicit
        # override is honoured (the tests inject a stub, the CLI a memoised one).
        self._validator: Callable[[], bool] = (
            guardrails_validator
            if guardrails_validator is not None
            else dispatcher.guardrails_validator
        )
        self._initiative = initiative
        self._cost = cost_per_issue
        # The durable cross-tick ledger. The default is the NON-durable null store
        # (in-process only) — fine for a single long-lived Governor and for unit
        # tests, but an armed + live tick requires a durable store (the CLI
        # refuses the non-durable combination). See FIX 1 / ARMING.md.
        self._state_store: StateStore = (
            state_store if state_store is not None else NullStateStore()
        )
        # Soak gate (FIX 3): the first ``soak_ticks`` armed ticks stay record-only
        # even when live is requested, so a cold armed tick never dispatches.
        self._soak_required = max(0, int(soak_ticks))
        self._soak_recorded = 0

    @property
    def state_store(self) -> StateStore:
        """The durable ledger store this runner loads/persists each tick."""
        return self._state_store

    def _persist(self) -> None:
        """Write the governor ledger + soak counter back to the durable store.

        A no-op for the null store; an atomic blob write for the durable store.
        Called on the dispatch path (after ``record_attempt`` / ``record_cost``)
        and on an armed record-only tick (to advance the soak counter), so the
        ledger and the soak window survive process death — the one write per tick
        that the one-item-per-tick cadence needs."""
        snap = self._governor.snapshot()
        snap.soak_ticks = self._soak_recorded
        self._state_store.save(snap)

    # -- the tick ----------------------------------------------------------- #
    def run_once(
        self, sink: DispatchSink, *, dry_run: bool = True
    ) -> RunResult:
        """Run one fail-safe dispatch tick.

        Order (each earlier gate short-circuits, so a breach can never reach the
        seam):

        0. **Load the durable ledger.** Hydrate the governor from the
           :class:`~loop_governor.StateStore` BEFORE any guardrail check, so
           retry/budget/breaker counters (and the soak counter) accumulate across
           ticks. An unreadable durable blob is fail-CLOSED → ``REFUSED``.
        1. **Self-confirm the guardrail harness** (the ``arm_auto_dispatch``
           precondition, re-checked live). Red → ``REFUSED``, select nothing,
           dispatch nothing.
        2. **Fleet-wide runtime breaker.** If the governor is halted (global
           budget / cross-issue circuit) → ``HALTED``, dispatch nothing. This now
           reflects the *persisted* breaker state, so a tick opened last cycle
           halts this cycle too.
        3. **Query READY** via the dispatcher (a pure, side-effect-free view).
           Empty → ``IDLE``.
        4. **Apply the governor before selecting.** Walk the READY queue; the
           first item the governor admits (retry ceiling + per-issue + global
           budget, all read from the persisted ledger) is selected. A per-issue
           breach skips that item; a global breach → ``HALTED``.
        5. **Record the contract** for the selected item — always, even when it
           is not dispatched.
        6. **Dispatch only when armed AND not dry_run AND past the soak window.**
           Otherwise ``RECORDED`` (dry-run / disarmed / soaking, the safe
           default). When it does dispatch, it advances the governor ledger and
           **persists it before** invoking ``sink.dispatch`` (so an attempt is
           durably counted even if the spawn crashes — fail-closed).
        """
        # (0) DURABLE LEDGER — load first so every guardrail below sees the
        #     accumulated cross-tick state. Fail CLOSED on an unreadable blob.
        try:
            persisted = self._state_store.load()
        except StateStoreError as exc:
            return self._result(
                RunDecision.REFUSED,
                f"durable loop-state is unreadable ({exc}) — refusing the tick "
                "rather than running on a reset ledger (which would fail open)",
                armed=self._governor.armed,
                harness_ok=False,
                dry_run=dry_run,
            )
        if persisted is not None:
            self._governor.restore(persisted)
            self._soak_recorded = persisted.soak_ticks

        armed = self._governor.armed
        harness_ok = bool(self._validator())

        # (1) The lights-out precondition, re-validated THIS tick. If the harness
        #     is not green we cannot trust the guardrails at all — refuse outright
        #     and select nothing. This holds even for a dry-run: a red harness is
        #     a stop, not a preview.
        if not harness_ok:
            return self._result(
                RunDecision.REFUSED,
                "guardrail harness did not self-validate this tick — the "
                "arm_auto_dispatch precondition is unmet; dispatching nothing",
                armed=armed,
                harness_ok=False,
                dry_run=dry_run,
            )

        # (2) Fleet-wide breaker — the hardest runtime stop. Never dispatch while
        #     the global budget circuit or the cross-issue circuit-breaker is open.
        if self._governor.halted:
            return self._result(
                RunDecision.HALTED,
                "fleet-wide breaker is open (global budget / cross-issue "
                "circuit) — all dispatch halted",
                armed=armed,
                harness_ok=True,
                dry_run=dry_run,
            )

        # (3) Query READY (pure — no side effects, deterministic for a snapshot).
        queue = self._dispatcher.ready_queue(self._initiative)
        ready_ids = tuple(i.id for i in queue)
        if not queue:
            return self._result(
                RunDecision.IDLE,
                "no READY work items in the initiative",
                armed=armed,
                harness_ok=True,
                dry_run=dry_run,
                ready=ready_ids,
            )

        # (4) Apply the governor BEFORE selecting. Walk the READY queue for the
        #     first item the runtime guardrails admit. A fleet breach halts the
        #     whole tick; a per-issue breach (retry ceiling / per-issue budget /
        #     already-escalated / already-done) skips just that item.
        selected, skipped, halt = self._select(queue, armed)
        if halt is not None:
            return self._result(
                RunDecision.HALTED,
                halt,
                armed=armed,
                harness_ok=True,
                dry_run=dry_run,
                ready=ready_ids,
                skipped=tuple(skipped),
            )
        if selected is None:
            return self._result(
                RunDecision.IDLE,
                "no dispatchable item — every READY candidate was skipped by a "
                "runtime guardrail (retry ceiling / per-issue budget / terminal)",
                armed=armed,
                harness_ok=True,
                dry_run=dry_run,
                ready=ready_ids,
                skipped=tuple(skipped),
            )

        # (5) Record the contract — always, even if we will not dispatch it.
        contract = self._dispatcher.contract_for(selected)

        # (6) Dispatch ONLY when armed AND not dry_run AND past the soak window.
        #     Every other path records the contract and stops (the safe default).
        #     The soak gate (FIX 3) keeps the first ``soak_required`` armed ticks
        #     record-only even when live is requested, so an operator reviews the
        #     would-dispatch decisions before any real dispatch — a cold armed
        #     tick can never go live.
        in_soak = armed and not dry_run and self._soak_recorded < self._soak_required
        if dry_run or not armed or in_soak:
            if in_soak:
                why = (
                    f"soak window ({self._soak_recorded + 1} of {self._soak_required} "
                    "armed record-only ticks before live dispatch)"
                )
            elif dry_run:
                why = "dry-run"
            else:
                why = "auto-dispatch not armed (lights-out gate closed)"
            # An armed record-only tick advances + persists the soak counter, so
            # the soak window elapses across process boundaries. A disarmed tick
            # never touches the durable state (a pure preview).
            if armed:
                self._soak_recorded += 1
                self._persist()
            return self._result(
                RunDecision.RECORDED,
                f"recorded the dispatch contract for {contract.issue_id} but did "
                f"NOT dispatch ({why}) — no agent spawned, no side effects",
                armed=armed,
                harness_ok=True,
                dry_run=dry_run,
                ready=ready_ids,
                skipped=tuple(skipped),
                contract=contract,
            )

        # armed AND not dry_run AND soak satisfied — the ONLY path that reaches
        # the spawn seam. Advance the governor's cross-tick ledger (attempt +
        # projected spend) and PERSIST it BEFORE handing off to the sink, so the
        # attempt/cost is durably counted even if the spawn crashes (fail-closed:
        # a lost spawn counts against the retry ceiling, never re-runs free).
        self._governor.record_attempt(contract.issue_id)
        self._governor.record_cost(contract.issue_id, self._cost)
        self._persist()
        dispatch_result = sink.dispatch(contract)
        return self._result(
            RunDecision.DISPATCHED,
            f"invoked the dispatch seam for {contract.issue_id} via "
            f"{dispatch_result.sink} sink (spawned={dispatch_result.spawned})",
            armed=True,
            harness_ok=True,
            dry_run=False,
            ready=ready_ids,
            skipped=tuple(skipped),
            contract=contract,
            dispatch_result=dispatch_result,
            dispatched=True,
        )

    # -- selection ---------------------------------------------------------- #
    def _select(
        self, queue: list[CandidateIssue], armed: bool
    ) -> tuple[Optional[CandidateIssue], list[tuple[str, str]], Optional[str]]:
        """Pick the next dispatchable item, applying the governor's runtime gate.

        Returns ``(selected, skipped, halt_reason)``. ``halt_reason`` is non-None
        iff a fleet-wide breaker tripped mid-walk (the caller returns ``HALTED``).

        When armed, the governor's :meth:`Governor.should_continue` is the gate
        (the full runtime ledger). When disarmed — the safe default — the runner
        is only producing a record-only preview and nothing can dispatch, so it
        selects the head of the (already budget/priority-ordered) READY queue
        without advancing or emitting anything; disarmed selection never has a
        side effect.
        """
        skipped: list[tuple[str, str]] = []
        if not armed:
            # Disarmed: a preview only. The dispatcher already ordered the queue
            # (wave → priority → age); the head is the would-be selection.
            return (queue[0] if queue else None), skipped, None

        for cand in queue:
            cont = self._governor.should_continue(cand.id, cost=self._cost)
            if cont.action is ContinueAction.CONTINUE:
                return cand, skipped, None
            if cont.action is ContinueAction.HALT:
                # Fleet-wide (global budget / circuit) — stop the whole tick.
                return None, skipped, cont.reason
            # ESCALATE (retry ceiling / per-issue budget), DONE, or REFUSE:
            # this item is not dispatchable — skip it and try the next. The
            # runner does not itself escalate (that is the verify/close side's
            # job, idempotent there); it simply refuses to dispatch.
            skipped.append((cand.id, cont.reason))
        return None, skipped, None

    # -- result helper ------------------------------------------------------ #
    def _result(
        self,
        decision: RunDecision,
        reason: str,
        *,
        armed: bool,
        harness_ok: bool,
        dry_run: bool,
        ready: tuple[str, ...] = (),
        skipped: tuple[tuple[str, str], ...] = (),
        contract: Optional[DispatchContract] = None,
        dispatch_result: Optional[DispatchResult] = None,
        dispatched: bool = False,
    ) -> RunResult:
        # Defence in depth: only a DISPATCHED decision may report dispatched=True.
        if decision in _NO_SEAM:
            dispatched = False
        return RunResult(
            decision=decision,
            reason=reason,
            armed=armed,
            harness_validated=harness_ok,
            dry_run=dry_run,
            dispatched=dispatched,
            initiative=self._initiative,
            contract=contract,
            dispatch_result=dispatch_result,
            ready=ready,
            skipped=skipped,
        )


# --------------------------------------------------------------------------- #
# CLI — dry-run + record-only by default; arming is a deliberate, extra act
# --------------------------------------------------------------------------- #
def _source_from(issues_file: Optional[str]) -> Optional[IssueSource]:
    """Resolve the Linear-adapter source: a snapshot file, else live Linear via
    ``LINEAR_API_KEY``, else ``None`` (the caller reports a documented skip so a
    scheduled dry-run stays green in a bootstrap repo with no source wired)."""
    if issues_file:
        return JsonIssueSource(Path(issues_file))
    api_key = os.environ.get("LINEAR_API_KEY")
    if api_key:
        return HttpLinearSource(api_key)
    return None


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from the environment, tolerating unset/blank."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return default


def _state_store_from(state_file: Optional[str]) -> StateStore:
    """A DURABLE :class:`JsonFileStateStore` when a state file is configured, else
    the non-durable :class:`NullStateStore`. The workflow always passes a state
    file (materialised from the ``loop-state`` git ref), so a scheduled tick is
    always durable; an armed + live tick against the null store is refused."""
    if state_file:
        return JsonFileStateStore(Path(state_file))
    return NullStateStore()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop_runner",
        description=(
            "Continuous-dispatch driver for the autonomous-delivery loop. "
            "Selects the next READY item, guardrail-gates it, and — only when "
            "ARMED and LIVE — hands it to a dispatch sink. Dry-run + record-only "
            "by default; the default sink (LoggingDispatchSink) spawns nothing."
        ),
    )
    parser.add_argument(
        "--issues-file",
        help="Path to a Linear-adapter snapshot JSON (list or {'issues': [...]}). "
        "If omitted, LINEAR_API_KEY drives the live transport.",
    )
    parser.add_argument(
        "--initiative",
        default=ADP_INITIATIVE_ID,
        help="Initiative id to pull candidates from (default: ADP initiative).",
    )
    parser.add_argument(
        "--user",
        default="dan",
        help="Default branch owner when Linear supplies no gitBranchName.",
    )
    parser.add_argument(
        "--per-issue-budget",
        type=float,
        default=governor_mod.loop.GuardrailConfig.per_issue_budget,
        help="Per-issue budget cap the governor enforces before each dispatch.",
    )
    parser.add_argument(
        "--global-budget",
        type=float,
        default=governor_mod.loop.GuardrailConfig.global_budget,
        help="Global budget cap (fleet circuit-breaker) the governor enforces.",
    )
    parser.add_argument(
        "--cost",
        type=float,
        default=1.0,
        help="Notional cost charged per dispatched item (default: 1.0).",
    )
    parser.add_argument(
        "--armed",
        action="store_true",
        help="Arm auto-dispatch (refused unless the guardrail harness validates). "
        "Default: DISARMED. Even armed, the default sink is LoggingDispatchSink "
        "(logs only); pass --sink github-actions to wire the real spawn runtime "
        "(still gated on armed+live+soak — see ARMING.md).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live (not dry-run). Only combined with --armed does this reach "
        "the dispatch seam. Default: DRY-RUN (record-only, no side effects).",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("LOOP_STATE_FILE"),
        help="Path to the DURABLE governor-ledger blob (versioned JSON). The "
        "workflow materialises it from the dedicated `loop-state` git ref so the "
        "retry/budget/circuit-breaker counters accumulate ACROSS ticks. Omitted "
        "-> a NON-durable in-process ledger; an armed+live tick then REFUSES "
        "(guardrails would be inert). Env: LOOP_STATE_FILE.",
    )
    parser.add_argument(
        "--soak-ticks",
        type=int,
        default=_env_int("LOOP_SOAK_TICKS", 0),
        help="Number of armed record-only ticks to SOAK before any live dispatch "
        "(the counter persists in the state blob). The first N armed ticks record "
        "their would-dispatch decision for review; only tick N+1 can go live — so "
        "a cold armed tick never dispatches. Env: LOOP_SOAK_TICKS (default 0).",
    )
    parser.add_argument(
        "--sink",
        choices=["logging", "github-actions"],
        default=os.environ.get("LOOP_SINK", "logging"),
        help="Which dispatch sink to wire. 'logging' (default, safe) records the "
        "contract and spawns nothing. 'github-actions' wires the REAL "
        "GitHubActionsDispatchSink, which triggers loop-implement.yml via "
        "workflow_dispatch — reached ONLY on the runner's armed+live+past-soak "
        "path, and requires LOOP_DISPATCH_TOKEN + --dispatch-repo. Env: LOOP_SINK.",
    )
    parser.add_argument(
        "--dispatch-repo",
        default=os.environ.get("LOOP_DISPATCH_REPO"),
        help="Orchestrator repo (owner/name) hosting loop-implement.yml, for the "
        "github-actions sink. Env: LOOP_DISPATCH_REPO.",
    )
    parser.add_argument(
        "--dispatch-workflow",
        default=os.environ.get("LOOP_DISPATCH_WORKFLOW", "loop-implement.yml"),
        help="Workflow file the github-actions sink dispatches. Env: "
        "LOOP_DISPATCH_WORKFLOW (default loop-implement.yml).",
    )
    parser.add_argument(
        "--dispatch-ref",
        default=os.environ.get("LOOP_DISPATCH_REF", "main"),
        help="Git ref the dispatched workflow runs on. Env: LOOP_DISPATCH_REF "
        "(default main).",
    )
    parser.add_argument(
        "--dispatch-api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        help="GitHub REST API base. Env: GITHUB_API_URL (GHES override).",
    )
    parser.add_argument(
        "--enable-auto-merge",
        action="store_true",
        default=os.environ.get("LOOP_ENABLE_AUTO_MERGE") == "true",
        help="H1 opt-in: let the github-actions sink pass enable-auto-merge=true "
        "to loop-implement.yml, so the executor MAY auto-merge a PRODUCT-repo PR "
        "(core repos still take n+1 review). Honoured ONLY on the armed+live "
        "dispatch path — loop-dispatch.yml passes it ONLY on the go-live tick; "
        "without it (the default) every dispatched run opens a review PR. Env: "
        "LOOP_ENABLE_AUTO_MERGE.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the run result as JSON instead of the human-readable view.",
    )
    return parser


def _build_runner(
    args: argparse.Namespace,
    source: IssueSource,
    validator: Callable[[], bool],
    state_store: StateStore,
) -> tuple[Runner, Governor]:
    """Assemble a dispatcher + governor + runner that all share one harness proof
    (``validator``), so the harness is run at most once per tick and the runner's
    self-check cannot disagree with the dispatcher's arming. The ``state_store`` is
    the durable cross-tick ledger the runner loads at tick start and persists on
    the dispatch path."""
    config = governor_mod.loop.GuardrailConfig(
        per_issue_budget=args.per_issue_budget,
        global_budget=args.global_budget,
    )
    dispatcher = Dispatcher(
        source,
        config=config,
        default_user=args.user,
        guardrails_validator=validator,
    )
    governor = Governor(config)
    runner = Runner(
        dispatcher,
        governor,
        guardrails_validator=validator,
        initiative=args.initiative,
        cost_per_issue=args.cost,
        state_store=state_store,
        soak_ticks=args.soak_ticks,
    )
    return runner, governor


def _build_sink(args: argparse.Namespace) -> DispatchSink:
    """Resolve the dispatch sink from the CLI.

    Default is the SAFE :class:`LoggingDispatchSink` (records, spawns nothing).
    ``--sink github-actions`` wires the REAL :class:`GitHubActionsDispatchSink`,
    which the caller (loop-dispatch.yml) only passes on the go-live path — a
    belt-and-suspenders selection on top of the runner's own armed+live+soak gate.
    The real sink is fail-CLOSED to build: it needs the App dispatch token (env
    ``LOOP_DISPATCH_TOKEN``, minted secret-free via WIF+KV) and the orchestrator
    repo, or the CLI refuses rather than silently degrading to a no-op."""
    if args.sink == "github-actions":
        token = os.environ.get("LOOP_DISPATCH_TOKEN", "")
        if not token:
            raise SystemExit(
                "loop_runner: --sink github-actions requires LOOP_DISPATCH_TOKEN "
                "(the bot App installation token, minted secret-free via WIF+KV). "
                "Refusing to run the real sink without it."
            )
        if not args.dispatch_repo:
            raise SystemExit(
                "loop_runner: --sink github-actions requires --dispatch-repo "
                "(the owner/name hosting loop-implement.yml)."
            )
        # H1: auto-merge is opt-in AND only ever on the armed+live path. Even if
        # --enable-auto-merge is passed, it does NOT propagate unless this is the
        # armed live dispatch — belt-and-suspenders with the workflow's own FALSE
        # default, so nothing but the go-live tick can ever set enable-auto-merge.
        enable_auto_merge = bool(args.enable_auto_merge and args.armed and args.live)
        return GitHubActionsDispatchSink(
            token=token,
            repo=args.dispatch_repo,
            workflow=args.dispatch_workflow,
            ref=args.dispatch_ref,
            api_url=args.dispatch_api_url,
            enable_auto_merge=enable_auto_merge,
        )
    return LoggingDispatchSink()


def render_result(result: RunResult) -> str:
    """A human-readable render of one tick — the safe operator view."""
    lines: list[str] = []
    lines.append(f"loop-runner tick — Autonomous Delivery Platform ({result.initiative})")
    lines.append(
        f"  armed={result.armed}  harness_validated={result.harness_validated}  "
        f"dry_run={result.dry_run}"
    )
    lines.append(f"  decision: {result.decision.value.upper()}  —  {result.reason}")
    lines.append(f"  READY ({len(result.ready)}): {', '.join(result.ready) or '(none)'}")
    if result.contract:
        c = result.contract
        verb = "DISPATCHED" if result.dispatched else "SELECTED (recorded, not dispatched)"
        lines.append(f"  {verb}: {c.issue_id}  repo={c.repo}  branch={c.branch}")
        lines.append(f"      acceptance-criteria: {c.acceptance_criteria}")
    if result.dispatch_result:
        d = result.dispatch_result
        lines.append(f"  sink={d.sink}  spawned={d.spawned}  {d.detail}")
    if result.skipped:
        lines.append("  skipped (runtime guardrail):")
        for issue_id, reason in result.skipped:
            lines.append(f"    - {issue_id}: {reason}")
    if not result.dispatched:
        lines.append("  [no dispatch] no agent spawned, no Linear writes, no side effects.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    dry_run = not args.live

    # The durable cross-tick ledger. An armed + LIVE tick MUST run against a
    # durable store — otherwise the retry/budget/circuit-breaker counters reset
    # every tick and the guardrails are inert (fail-OPEN). Refuse that combination
    # up front (exit non-zero so a scheduled tick surfaces the misconfiguration).
    state_store = _state_store_from(args.state_file)
    if args.armed and args.live and not state_store.durable:
        msg = (
            "loop-runner: REFUSED — an armed + live tick requires a DURABLE state "
            "store (pass --state-file / set LOOP_STATE_FILE). Without it the "
            "governor ledger resets every tick and the retry/budget/circuit-"
            "breaker guardrails never accumulate (fail-open). Nothing dispatched."
        )
        if args.json:
            print(json.dumps({"decision": "refused", "reason": msg, "armed": True}, indent=2))
        else:
            print(msg)
        return 3

    source = _source_from(args.issues_file)
    if source is None:
        # No source wired. A dry-run tick stays green (documented skip, mirroring
        # verify-and-close's never-guess rule); an armed live tick is a real
        # misconfiguration and exits non-zero so it surfaces.
        msg = (
            "loop-runner: no Linear source — pass --issues-file <snapshot.json> "
            "or set LINEAR_API_KEY (secret-free via KV+WIF in loop-dispatch.yml). "
            "Nothing selected, nothing dispatched."
        )
        if args.json:
            print(json.dumps({"decision": "idle", "reason": msg, "armed": args.armed}, indent=2))
        else:
            print(msg)
        return 3 if (args.armed and args.live) else 0

    # Run the guardrail harness AT MOST ONCE this tick and share the result with
    # both the dispatcher (its arming proof) and the runner (its live self-check).
    harness_ok = guardrails_validated()
    validator: Callable[[], bool] = lambda: harness_ok  # noqa: E731 - a tiny memoised proof

    runner, governor = _build_runner(args, source, validator, state_store)

    # Arming is a deliberate act, and STILL refused unless the harness validates.
    if args.armed:
        try:
            governor.arm(guardrails_validated=harness_ok)
        except governor_mod.GuardrailTripped:
            # Harness red at arm time — leave disarmed; run_once will REFUSE.
            pass

    # Resolve the sink. Default is the SAFE LoggingDispatchSink (logs only);
    # --sink github-actions wires the REAL GitHubActionsDispatchSink. The sink is
    # only ever REACHED on the runner's single armed+live+past-soak path — the
    # gating in run_once is unchanged, so selecting the real sink cannot itself
    # dispatch anything unless every guardrail has already opened.
    sink = _build_sink(args)
    result = runner.run_once(sink, dry_run=dry_run)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(render_result(result))

    # Exit non-zero only when a LIVE armed tick was REFUSED by the gate (harness
    # red) — so the schedule surfaces a closed gate — never for a normal dry-run.
    if args.armed and args.live and result.decision is RunDecision.REFUSED:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
