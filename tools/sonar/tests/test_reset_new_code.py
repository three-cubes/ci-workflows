"""Unit tests for the CORE SonarCloud new-code reset driver.

Run: ``python3 -m unittest discover -s tools/sonar/tests`` (stdlib only, no deps,
no network — the transport seam is injected as a fake).
"""

from __future__ import annotations

import io
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reset_new_code as rnc  # noqa: E402 — path shim above


class FakeCaller:
    """Records calls and returns canned responses; can raise on a chosen call."""

    def __init__(self, responses=None, raise_on=None):
        self.calls: list[tuple[str, str, dict]] = []
        self._responses = responses or {}
        self._raise_on = raise_on or set()

    def __call__(self, method: str, path: str, **params: str) -> dict:
        self.calls.append((method, path, params))
        if (method, path) in self._raise_on:
            raise urllib.error.HTTPError(
                path, 403, "Forbidden", {}, io.BytesIO(b"insufficient privileges")
            )
        return self._responses.get((method, path), {})


class ResetHappyPathTest(unittest.TestCase):
    def test_read_set_read_sequence_and_params(self):
        caller = FakeCaller(
            responses={
                ("GET", "/api/new_code_periods/show"): {"type": "PREVIOUS_VERSION"},
            }
        )
        rc = rnc.main(
            argv=["--project-key", "three-cubes_kairix", "--branch", "release"],
            caller=caller,
        )
        self.assertEqual(rc, 0)
        methods = [(m, p) for m, p, _ in caller.calls]
        self.assertEqual(
            methods,
            [
                ("GET", "/api/new_code_periods/show"),
                ("POST", "/api/new_code_periods/set"),
                ("GET", "/api/new_code_periods/show"),
            ],
        )
        _, _, set_params = caller.calls[1]
        self.assertEqual(set_params["project"], "three-cubes_kairix")
        self.assertEqual(set_params["branch"], "release")
        self.assertEqual(set_params["type"], "PREVIOUS_VERSION")

    def test_defaults_branch_and_type(self):
        caller = FakeCaller()
        rc = rnc.main(argv=["--project-key", "some_proj"], caller=caller)
        self.assertEqual(rc, 0)
        _, _, set_params = caller.calls[1]
        self.assertEqual(set_params["branch"], "main")
        self.assertEqual(set_params["type"], "PREVIOUS_VERSION")

    def test_custom_new_code_type_flows_through(self):
        caller = FakeCaller()
        rc = rnc.main(
            argv=[
                "--project-key",
                "p",
                "--new-code-type",
                "NUMBER_OF_DAYS",
            ],
            caller=caller,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(caller.calls[1][2]["type"], "NUMBER_OF_DAYS")


class ResetFailureTest(unittest.TestCase):
    def test_missing_token_is_usage_error(self):
        # No caller injected and an explicitly empty token => usage error (2).
        rc = rnc.main(argv=["--project-key", "p"], token="")
        self.assertEqual(rc, 2)

    def test_http_error_fails_closed(self):
        caller = FakeCaller(raise_on={("POST", "/api/new_code_periods/set")})
        rc = rnc.main(argv=["--project-key", "p"], caller=caller)
        self.assertEqual(rc, 1)
        # Failed closed: only the initial read + the failing set were attempted.
        self.assertEqual(len(caller.calls), 2)


if __name__ == "__main__":
    unittest.main()
