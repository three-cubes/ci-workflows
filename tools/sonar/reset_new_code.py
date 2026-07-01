"""Reset a SonarCloud/SonarQube project's "New Code" period for a branch.

CORE driver — promoted from kairix's ``scripts/sonar/reset_new_code.py`` so
every three-cubes repo shares one reviewed transport + read->set->read driver
instead of forking a copy. The repo-specific values (project key, branch,
New Code type, server base URL) are CLI/workflow inputs, not module constants,
so the same driver resets any project.

Why this exists
---------------
SonarCloud's per-branch "New Code" definition drifts: pre-existing code that
passed at the prior green analysis ends up inside the "new code" window, so the
server-side Quality Gate fails the branch on accumulated debt
(``new_reliability_rating``, ``new_code_smells``). Because
``sonar.qualitygate.wait=true`` makes the in-CI scan exit WITH the gate verdict,
that flips the whole CI run to ``failure``.

The durable fix is a one-time reset of the New Code reference to
``PREVIOUS_VERSION`` so each release rolls the window forward (the scanner emits
``sonar.projectVersion`` per release). Setting "Previous version" / "Specific
analysis" / "Specific date" is a Web-API-or-UI-only action — it is NOT a scanner
property — so this script drives the API directly with a Bearer token, keeping
the decision reviewable in git history rather than a hand-typed UI click.

Usage (locally with an admin SONAR_TOKEN env var):
    SONAR_TOKEN=xxxx python3 tools/sonar/reset_new_code.py \
        --project-key three-cubes_kairix --branch main

Usage (CI):
    Triggered via the reusable .github/workflows/sonar-new-code-reset.yml
    (workflow_call). The caller passes project-key/branch as inputs and
    SONAR_TOKEN via secrets.

Idempotent: prints the New Code period before and after the set, so a re-run is
a no-op observation when already in the requested state.

References:
- https://sonarcloud.io/web_api/api/new_code_periods
- POST /api/new_code_periods/set  — set a branch's New Code definition.
- GET  /api/new_code_periods/show — read the current definition.

Exit codes:
  0 — reset applied (or already in the requested state)
  1 — the API call failed (non-2xx) — fails closed, nothing was changed
  2 — usage error (SONAR_TOKEN unset)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol, Sequence

DEFAULT_SONAR_BASE = "https://sonarcloud.io"
DEFAULT_BRANCH = "main"
# "Previous version" — the New Code mode drivable purely by a scanner property
# (sonar.projectVersion, emitted per release). Clean for trunk-based single-main
# analysis; self-maintaining via release bumps.
DEFAULT_NEW_CODE_TYPE = "PREVIOUS_VERSION"
_USER_AGENT = "tc-sonar-reset/1.0"


@dataclass(frozen=True)
class ResetConfig:
    """Per-invocation reset target — the values that used to be module constants."""

    project_key: str
    branch: str = DEFAULT_BRANCH
    new_code_type: str = DEFAULT_NEW_CODE_TYPE


class ApiCaller(Protocol):
    """Transport seam: ``(method, path, **form_params) -> parsed JSON``.

    Injected so the reset logic is exercised with a fake in tests (no real
    network, no monkeypatch). The production default is ``default_caller``.
    """

    def __call__(self, method: str, path: str, **params: str) -> dict: ...


def default_caller(token: str, sonar_base: str = DEFAULT_SONAR_BASE) -> ApiCaller:
    """Real SonarCloud transport — Bearer-token urllib POST/GET.

    Raises ``urllib.error.HTTPError`` on non-2xx; the caller fails closed.
    """

    def _call(method: str, path: str, **params: str) -> dict:
        url = sonar_base + path
        body: bytes | None = None
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": _USER_AGENT,
        }
        if method == "GET" and params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        elif params:
            body = urllib.parse.urlencode(params).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed Sonar API base, form-encoded params
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    return _call


def show_period(caller: ApiCaller, cfg: ResetConfig) -> dict:
    """Return the current New Code period for the branch (GET /show)."""
    return caller(
        "GET",
        "/api/new_code_periods/show",
        project=cfg.project_key,
        branch=cfg.branch,
    )


def set_period(caller: ApiCaller, cfg: ResetConfig) -> None:
    """Set the New Code period for the branch to ``cfg.new_code_type`` (POST /set).

    The /set endpoint returns an empty 204 body on success.
    """
    caller(
        "POST",
        "/api/new_code_periods/set",
        project=cfg.project_key,
        branch=cfg.branch,
        type=cfg.new_code_type,
    )


def reset_new_code(caller: ApiCaller, cfg: ResetConfig) -> int:
    """Read -> set -> read the New Code period. Fails closed on any HTTP error.

    Returns the process exit code (0 ok, 1 API failure).
    """
    try:
        before = show_period(caller, cfg)
        print(f"before: {json.dumps(before, sort_keys=True)}")
        set_period(caller, cfg)
        print(
            f"set: project={cfg.project_key} branch={cfg.branch} "
            f"type={cfg.new_code_type}"
        )
        after = show_period(caller, cfg)
        print(f"after: {json.dumps(after, sort_keys=True)}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        print(
            f"::error::SonarCloud new-code reset failed — HTTP {exc.code} "
            f"{exc.reason}: {detail}",
            file=sys.stderr,
        )
        print(
            "::error::fix: confirm SONAR_TOKEN has Administer permission on the "
            "project; next: re-run the workflow once the token scope is corrected",
            file=sys.stderr,
        )
        return 1
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-key",
        required=True,
        help="SonarCloud project key (e.g. three-cubes_kairix).",
    )
    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help=f"Branch whose New Code period is reset (default: {DEFAULT_BRANCH}).",
    )
    parser.add_argument(
        "--new-code-type",
        default=DEFAULT_NEW_CODE_TYPE,
        help=f"New Code period type to set (default: {DEFAULT_NEW_CODE_TYPE}).",
    )
    parser.add_argument(
        "--sonar-base",
        default=DEFAULT_SONAR_BASE,
        help=f"SonarQube/SonarCloud base URL (default: {DEFAULT_SONAR_BASE}).",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    caller: ApiCaller | None = None,
    token: str | None = None,
) -> int:
    """Run the reset.

    ``caller`` injects the transport (a fake in tests). When omitted, the real
    ``default_caller`` is built from ``token`` — which itself defaults to the
    ``SONAR_TOKEN`` env var at the boundary. Tests drive the missing-token branch
    by passing ``token=""`` explicitly, so no process-env mutation is needed.
    """
    args = _parse_args(argv)
    cfg = ResetConfig(
        project_key=args.project_key,
        branch=args.branch,
        new_code_type=args.new_code_type,
    )
    if caller is None:
        if token is None:
            token = os.environ.get("SONAR_TOKEN", "")
        if not token:
            print("ERROR: SONAR_TOKEN env var not set", file=sys.stderr)
            return 2
        caller = default_caller(token, args.sonar_base)
    return reset_new_code(caller, cfg)


if __name__ == "__main__":
    sys.exit(main())
