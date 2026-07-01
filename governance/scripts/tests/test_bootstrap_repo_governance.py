"""Contract test for the bootstrap-repo-governance.sh operator entrypoint (SGO-164).

Exercises the CLI contract (the public surface an operator/agent relies on) —
not the live `gh`/`az` calls. The help branch, arg-parse, and the local
affordance render run before any network write, so these cases are hermetic:
the affordance render sources governance/skeletons/ from this checkout, and the
live sections are toggled off (--no-secrets/--no-ruleset/--no-files) so the
--dry-run path prints its payload without pushing.

Interface: shell-entrypoint.governance.scripts.bootstrap-repo-governance
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = "governance/scripts/bootstrap-repo-governance.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    # The script path is a literal here (not a Path var) so an outcome-test gate
    # can match the surface name in the subprocess call; cwd resolves it.
    return subprocess.run(
        ["bash", "governance/scripts/bootstrap-repo-governance.sh", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )


def test_help_exits_zero_and_documents_the_flags() -> None:
    result = _run("--help")
    assert result.returncode == 0
    out = result.stdout
    assert "bootstrap-repo-governance" in out
    for flag in (
        "--repo",
        "--dry-run",
        "--no-secrets",
        "--no-ruleset",
        "--no-files",
        "--no-affordance",
    ):
        assert flag in out, f"help text omits {flag}"


def test_missing_repo_is_an_actionable_error() -> None:
    result = _run()  # no --repo
    assert result.returncode == 2
    assert "--repo" in result.stderr
    assert "fix:" in result.stderr


def test_unknown_flag_is_rejected() -> None:
    result = _run("--bogus")
    assert result.returncode == 2
    assert "unknown flag" in result.stderr


def test_dry_run_covers_the_affordance_payload() -> None:
    # The extended payload (SGO-164): rendered skeletons + sonar-sqaa hook +
    # idempotent settings.json jq-merge + safe-commit/preflight — all named in
    # the --dry-run output. Live sections are toggled off so this stays hermetic.
    result = _run(
        "--repo", "three-cubes/sample", "--dry-run",
        "--no-secrets", "--no-ruleset", "--no-files",
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout

    # Section header present.
    assert "affordance + harness payload" in out

    # All six rendered skeletons named.
    for skel in ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "ETHOS.md", "RESOLVER.md", "SCORECARD.md"):
        assert skel in out, f"affordance payload omits {skel}"

    # The hook + the idempotent settings.json merge.
    assert "sonar-sqaa" in out
    assert "PostToolUse" in out
    assert "unique" in out, "settings.json merge must be idempotent (jq unique), not an append"

    # The harness scripts.
    assert "scripts/safe-commit.sh" in out
    assert "scripts/preflight.sh" in out

    # Placeholders resolved from --repo at render time (no unrendered token, and
    # the repo slug appears in the rendered instructions).
    assert "{{REPO}}" not in out, "an affordance skeleton rendered with an unresolved {{REPO}} token"
    assert "three-cubes/sample" in out


def test_no_affordance_toggle_suppresses_the_payload() -> None:
    result = _run(
        "--repo", "three-cubes/sample", "--dry-run", "--no-affordance",
        "--no-secrets", "--no-ruleset", "--no-files",
    )
    assert result.returncode == 0, result.stderr
    assert "affordance + harness payload" not in result.stdout
