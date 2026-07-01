"""Unit tests for the CORE SonarCloud hotspot triage driver.

Run: ``python3 -m unittest discover -s tools/sonar/tests`` (stdlib only, no deps,
no network — the transport seam is injected as a fake).
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import triage_hotspots as th  # noqa: E402 — path shim above


class FakeCaller:
    """Serves paginated hotspot search results and records change_status calls."""

    def __init__(self, pages=None, fail_keys=None):
        # pages: list of hotspot-lists returned for successive search calls.
        self._pages = list(pages or [[]])
        self._search_idx = 0
        self._fail_keys = fail_keys or set()
        self.acknowledged: list[str] = []

    def __call__(self, method: str, path: str, **params: str) -> dict:
        if path == "/api/hotspots/search":
            idx = int(params["p"]) - 1
            hotspots = self._pages[idx] if idx < len(self._pages) else []
            return {"hotspots": hotspots}
        if path == "/api/hotspots/change_status":
            key = params["hotspot"]
            if key in self._fail_keys:
                raise urllib.error.HTTPError(
                    path, 400, "Bad Request", {}, io.BytesIO(b"nope")
                )
            self.acknowledged.append(key)
            return {}
        raise AssertionError(f"unexpected path {path}")


def _hotspot(key, rule, path, line):
    return {
        "key": key,
        "ruleKey": rule,
        "component": f"three-cubes_kairix:{path}",
        "line": line,
    }


class LoadRationalesTest(unittest.TestCase):
    def _write(self, obj):
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(obj, fh)
        fh.close()
        return fh.name

    def test_pair_and_triple_keys(self):
        path = self._write(
            [
                {"rule": "python:S5852", "path": "a.py", "rationale": "bounded"},
                {
                    "rule": "docker:S6471",
                    "path": "Dockerfile",
                    "line": 28,
                    "rationale": "root ok",
                },
            ]
        )
        r = th.load_rationales(path)
        self.assertEqual(r[("python:S5852", "a.py")], "bounded")
        self.assertEqual(r[("docker:S6471", "Dockerfile", 28)], "root ok")

    def test_not_a_list_raises(self):
        path = self._write({"rule": "x"})
        with self.assertRaises(ValueError):
            th.load_rationales(path)

    def test_missing_key_raises(self):
        path = self._write([{"rule": "x", "path": "a.py"}])
        with self.assertRaises(ValueError):
            th.load_rationales(path)


class ResolveRationaleTest(unittest.TestCase):
    def test_triple_preferred_over_pair(self):
        rationales = {
            ("r", "f.py"): "generic",
            ("r", "f.py", 12): "line-specific",
        }
        self.assertEqual(
            th._resolve_rationale(rationales, "r", "f.py", 12), "line-specific"
        )
        self.assertEqual(th._resolve_rationale(rationales, "r", "f.py", 99), "generic")
        self.assertIsNone(th._resolve_rationale(rationales, "r", "other.py", 1))


class TriageDriverTest(unittest.TestCase):
    def test_all_mapped_returns_zero(self):
        caller = FakeCaller(
            pages=[[_hotspot("k1", "python:S5852", "a.py", 3)]]
        )
        rationales = {("python:S5852", "a.py"): "bounded"}
        rc = th.triage(caller, "three-cubes_kairix", rationales, sleep=lambda _s: None)
        self.assertEqual(rc, 0)
        self.assertEqual(caller.acknowledged, ["k1"])

    def test_unmapped_hotspot_returns_one_and_is_left_alone(self):
        caller = FakeCaller(
            pages=[
                [
                    _hotspot("k1", "python:S5852", "a.py", 3),
                    _hotspot("k2", "python:S2245", "b.py", 9),
                ]
            ]
        )
        rationales = {("python:S5852", "a.py"): "bounded"}
        rc = th.triage(caller, "p", rationales, sleep=lambda _s: None)
        self.assertEqual(rc, 1)
        # Only the mapped hotspot was acknowledged; the unmapped one stays visible.
        self.assertEqual(caller.acknowledged, ["k1"])

    def test_failed_ack_returns_one(self):
        caller = FakeCaller(
            pages=[[_hotspot("k1", "python:S5852", "a.py", 3)]],
            fail_keys={"k1"},
        )
        rationales = {("python:S5852", "a.py"): "bounded"}
        rc = th.triage(caller, "p", rationales, sleep=lambda _s: None)
        self.assertEqual(rc, 1)
        self.assertEqual(caller.acknowledged, [])

    def test_pagination_walks_all_pages(self):
        full = [_hotspot(f"k{i}", "python:S5852", "a.py", i) for i in range(500)]
        second = [_hotspot("k500", "python:S5852", "a.py", 500)]
        caller = FakeCaller(pages=[full, second])
        rationales = {("python:S5852", "a.py"): "bounded"}
        rc = th.triage(caller, "p", rationales, sleep=lambda _s: None)
        self.assertEqual(rc, 0)
        self.assertEqual(len(caller.acknowledged), 501)


class MainUsageTest(unittest.TestCase):
    def test_missing_token_is_usage_error(self):
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump([{"rule": "r", "path": "a.py", "rationale": "x"}], fh)
        fh.close()
        rc = th.main(
            argv=["--project-key", "p", "--rationales", fh.name], token=""
        )
        self.assertEqual(rc, 2)

    def test_malformed_rationale_file_is_usage_error(self):
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        fh.write("{ not json")
        fh.close()
        rc = th.main(argv=["--project-key", "p", "--rationales", fh.name])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
