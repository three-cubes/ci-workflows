"""Executable guardrails for the loop-implement executor workflow.

These tests execute the ACTUAL `run:` body of `loop-implement.yml`'s validate
step (extracted from the committed workflow, no YAML dependency) so the
security invariants are proven against the real shell logic, not a paraphrase:

* LOWER — the resolved TARGET repo is validated against an explicit org-repo
  allowlist BEFORE any App token is minted; a non-allowlisted repo fails the
  job. The repo name is inferred from attacker-influenceable Linear content, so
  this is the boundary that stops it selecting an arbitrary repo.

The auto-merge opt-in (H1) is proven on the Python side in
`test_loop_runner.py` (`BuildSinkAutoMergeTest` + the sink input tests).
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

# Repo root: governance/loop/tests -> parents[3].
_WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github" / "workflows" / "loop-implement.yml"
)
_DEFAULT_ALLOWED = "kairix kata tc-agent-zone tc-pipelines data-visualisation"


def _extract_run_block(text: str, name_substr: str) -> str:
    """Return the dedented `run: |` script body of the first step whose name
    contains ``name_substr``. Pure text parsing — no YAML dependency, so the
    test never fails for a missing optional import in a minimal CI image."""
    lines = text.splitlines()
    start = None
    for idx, ln in enumerate(lines):
        if ln.lstrip().startswith("- name:") and name_substr in ln:
            start = idx
            break
    if start is None:
        raise AssertionError(f"step containing {name_substr!r} not found in {_WORKFLOW}")

    run_idx = None
    for idx in range(start + 1, len(lines)):
        # Stop if we reach the next step without finding a run: block.
        if lines[idx].lstrip().startswith("- name:"):
            break
        if lines[idx].strip().startswith("run: |"):
            run_idx = idx
            break
    if run_idx is None:
        raise AssertionError(f"no `run: |` block in step {name_substr!r}")

    run_indent = len(lines[run_idx]) - len(lines[run_idx].lstrip())
    body: list[str] = []
    for idx in range(run_idx + 1, len(lines)):
        ln = lines[idx]
        if ln.strip() == "":
            body.append("")
            continue
        if (len(ln) - len(ln.lstrip())) <= run_indent:
            break
        body.append(ln)
    non_blank = [ln for ln in body if ln.strip()]
    dedent = min(len(ln) - len(ln.lstrip()) for ln in non_blank)
    return "\n".join(ln[dedent:] if ln.strip() else "" for ln in body)


class ValidateStepAllowlistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _extract_run_block(
            _WORKFLOW.read_text(encoding="utf-8"), "Validate inputs"
        )

    def _run(self, repo_in, *, issue="SGO-76", branch="dan/sgo-76-x",
             owner="three-cubes", allowed=_DEFAULT_ALLOWED):
        env = dict(os.environ)
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            github_env = fh.name
        env.update(
            ISSUE_ID_IN=issue,
            ISSUE_BRANCH_IN=branch,
            REPO_IN=repo_in,
            OWNER=owner,
            ALLOWED_REPOS=allowed,
            GITHUB_ENV=github_env,
        )
        try:
            return subprocess.run(
                ["bash", "-c", self.script],
                env=env, capture_output=True, text=True,
            )
        finally:
            os.unlink(github_env)

    def test_extraction_found_the_allowlist_logic(self):
        # Guard against a silent refactor that removes the allowlist enforcement.
        self.assertIn("ALLOWED_REPOS", self.script)
        self.assertIn("not in the", self.script)

    def test_allowlisted_bare_repo_passes(self):
        proc = self._run("kairix")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_allowlisted_owner_qualified_repo_passes(self):
        proc = self._run("three-cubes/data-visualisation")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_non_allowlisted_bare_repo_is_rejected(self):
        proc = self._run("attacker-repo")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not in the", proc.stdout + proc.stderr)

    def test_non_allowlisted_owner_qualified_repo_is_rejected(self):
        # A well-formed owner/name that is NOT an allowlisted org repo still fails.
        proc = self._run("three-cubes/secrets-exfil")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not in the", proc.stdout + proc.stderr)

    def test_unknown_sentinel_still_rejected(self):
        proc = self._run("unknown")
        self.assertNotEqual(proc.returncode, 0)

    def test_allowlist_is_overridable(self):
        # An operator override (LOOP_ALLOWED_REPOS) that omits kairix rejects it.
        proc = self._run("kairix", allowed="kata tc-pipelines")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not in the", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
