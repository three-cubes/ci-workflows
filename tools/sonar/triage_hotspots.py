"""Mark SonarCloud security hotspots as Reviewed/SAFE with a recorded rationale.

CORE driver — promoted from kairix's ``scripts/sonar/triage_hotspots.py``. Only
the Bearer-token urllib transport + the list->resolve->acknowledge driver loop
is shared here. The *triage decisions themselves* (which (rule, file) pairs are
accepted, and why) are REPO-OWNED: each consuming repo ships its own rationale
file and passes its path via ``--rationales``. This keeps the security judgement
where the code owner can review it in their own repo's git history, while the
mechanical transport is single-sourced in CORE.

Rationale file format (JSON — a list of entries)::

    [
      {
        "rule": "python:S5852",
        "path": "kairix/core/temporal/chunker.py",
        "rationale": "Bounded input — ... Reviewed and accepted."
      },
      {
        "rule": "docker:S6471",
        "path": "Dockerfile",
        "line": 28,
        "rationale": "Runtime stays as root because ... Reviewed and accepted."
      }
    ]

``line`` is optional. A hotspot matches the most specific entry: a
(rule, path, line) entry is preferred over a (rule, path) entry for the same
location. Hotspots whose location matches NO entry are left unchanged so they
stay visible for manual review (and the run exits non-zero).

Idempotent: already-Reviewed hotspots drop out of the TO_REVIEW search, so
re-running only touches hotspots that still need triage.

Usage (locally with SONAR_TOKEN env var):
    SONAR_TOKEN=xxxx python3 tools/sonar/triage_hotspots.py \
        --project-key three-cubes_kairix --rationales .sonar/hotspot-rationales.json

Usage (CI):
    Triggered via the reusable .github/workflows/sonar-triage.yml (workflow_call),
    ideally on a weekly schedule so hotspots don't accrue silently. The caller
    passes project-key + the rationale-file path as inputs and SONAR_TOKEN via
    secrets.

References:
- https://docs.sonarcloud.io/improving/security-hotspots/
- GET  /api/hotspots/search        — list hotspots for a project.
- POST /api/hotspots/change_status — change status of a hotspot.

Exit codes:
  0 — every TO_REVIEW hotspot was triaged (none unmapped, none failed)
  1 — one or more hotspots are unmapped (need a rationale) or failed to update
  2 — usage error (SONAR_TOKEN unset, or a malformed rationale file)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Protocol, Sequence

DEFAULT_SONAR_BASE = "https://sonarcloud.io"
_USER_AGENT = "tc-sonar-triage/1.0"
_PAGE_SIZE = 500

# A rationale lookup maps a hotspot location to the text Sonar records. Keys are
# either (rule, path) or (rule, path, line); the resolver prefers the triple.
RationaleKey = tuple  # (rule, path) or (rule, path, line)
Rationales = dict[RationaleKey, str]


class ApiCaller(Protocol):
    """Transport seam: ``(method, path, **form_params) -> parsed JSON``.

    Injected so the driver is exercised with a fake in tests (no real network).
    The production default is ``default_caller``.
    """

    def __call__(self, method: str, path: str, **params: str) -> dict: ...


def default_caller(token: str, sonar_base: str = DEFAULT_SONAR_BASE) -> ApiCaller:
    """Real SonarCloud transport — Bearer-token urllib POST/GET.

    Raises ``urllib.error.HTTPError`` on non-2xx; the caller handles it.
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


def load_rationales(path: str) -> Rationales:
    """Parse the repo-owned rationale JSON into a location->text lookup.

    Raises ``ValueError`` on a malformed file so ``main`` can fail as a usage
    error rather than silently triaging nothing.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON list of rationale entries")
    out: Rationales = {}
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: entry {i} is not an object")
        try:
            rule = str(entry["rule"])
            file_path = str(entry["path"])
            rationale = str(entry["rationale"])
        except KeyError as exc:
            raise ValueError(f"{path}: entry {i} missing key {exc}") from exc
        line = entry.get("line")
        key: RationaleKey = (
            (rule, file_path, int(line)) if line is not None else (rule, file_path)
        )
        out[key] = rationale
    return out


def _list_hotspots(caller: ApiCaller, project_key: str) -> list[dict]:
    """Return all TO_REVIEW hotspots for the project (paginated)."""
    out: list[dict] = []
    page = 1
    while True:
        data = caller(
            "GET",
            "/api/hotspots/search",
            projectKey=project_key,
            status="TO_REVIEW",
            ps=str(_PAGE_SIZE),
            p=str(page),
        )
        hotspots = data.get("hotspots", [])
        out.extend(hotspots)
        if len(hotspots) < _PAGE_SIZE:
            break
        page += 1
    return out


def _file_path(component: str) -> str:
    """SonarCloud component is ``project_key:path/to/file`` — extract the path."""
    return component.split(":", 1)[-1] if ":" in component else component


def _resolve_rationale(
    rationales: Rationales, rule: str, path: str, line: int
) -> str | None:
    """Pick the most specific rationale: (rule, path, line) then (rule, path)."""
    triple = rationales.get((rule, path, line))
    if triple is not None:
        return triple
    return rationales.get((rule, path))


def _acknowledge(caller: ApiCaller, hotspot_key: str, comment: str) -> bool:
    """Mark a hotspot as REVIEWED + SAFE with the rationale comment.

    SonarCloud exposes two resolutions: ``FIXED`` (you changed the code) and
    ``SAFE`` (you reviewed and accepted the risk). For a triaged false positive,
    ``SAFE`` is correct. Returns True on success, False on failure (reason printed).
    """
    try:
        caller(
            "POST",
            "/api/hotspots/change_status",
            hotspot=hotspot_key,
            status="REVIEWED",
            resolution="SAFE",
            comment=comment,
        )
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        print(f"  HTTP {exc.code} — {exc.reason}: {detail}", file=sys.stderr)
        return False


def triage(
    caller: ApiCaller,
    project_key: str,
    rationales: Rationales,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """List -> resolve -> acknowledge every TO_REVIEW hotspot. Fails closed.

    Returns 0 only when every hotspot was triaged (none unmapped, none failed).
    ``sleep`` is injected so tests don't pause.
    """
    hotspots = _list_hotspots(caller, project_key)
    print(f"Found {len(hotspots)} TO_REVIEW hotspots")

    triaged = 0
    skipped_unmapped: list[tuple[str, str, int]] = []
    failed: list[str] = []

    for h in hotspots:
        rule = h.get("ruleKey", "")
        path = _file_path(h.get("component", ""))
        line = int(h.get("line", 0) or 0)
        key = h.get("key", "")
        rationale = _resolve_rationale(rationales, rule, path, line)

        if rationale is None:
            skipped_unmapped.append((rule, path, line))
            continue

        if _acknowledge(caller, key, rationale):
            triaged += 1
            print(f"ACK   {rule:30s} {path}:{line}")
        else:
            failed.append(key)

        # Brief pause to be polite to the API.
        sleep(0.2)

    print()
    print(f"Triaged: {triaged}")
    print(f"Failed:  {len(failed)}")
    print(f"Unmapped (need rationale entry): {len(skipped_unmapped)}")
    for rule, path, line in skipped_unmapped:
        print(f"  - {rule}  {path}:{line}")

    return 0 if not failed and not skipped_unmapped else 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-key",
        required=True,
        help="SonarCloud project key (e.g. three-cubes_kairix).",
    )
    parser.add_argument(
        "--rationales",
        required=True,
        help="Path to the repo-owned rationale JSON file.",
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
    """Run the triage.

    ``caller`` injects the transport (a fake in tests). When omitted, the real
    ``default_caller`` is built from ``token`` — which defaults to the
    ``SONAR_TOKEN`` env var at the boundary.
    """
    args = _parse_args(argv)
    try:
        rationales = load_rationales(args.rationales)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read rationale file: {exc}", file=sys.stderr)
        return 2

    if caller is None:
        if token is None:
            token = os.environ.get("SONAR_TOKEN", "")
        if not token:
            print("ERROR: SONAR_TOKEN env var not set", file=sys.stderr)
            return 2
        caller = default_caller(token, args.sonar_base)
    return triage(caller, args.project_key, rationales)


if __name__ == "__main__":
    sys.exit(main())
