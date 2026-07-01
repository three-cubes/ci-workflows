"""Render contract for the governance/skeletons/ set (SGO-157).

Every skeleton must render — banner includes resolved, `{{REPO}}`/`{{CANONICAL_HOMES}}`
substituted — against a sample repo context with **zero unresolved `{{...}}`** and no
leftover `INCLUDE:` marker. This is the gate that keeps a placeholder from shipping into a
bootstrapped repo unrendered.

The render here is the reference implementation of the two-step render that
`scripts/bootstrap-repo-governance.sh` performs in bash (banner inline + token
substitution), so the two can never drift.

Interface: governance.skeletons.render
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

SKELETON_DIR = Path(__file__).resolve().parents[1]
BANNER = "_canonical-standards-banner.md"
INCLUDE_RE = re.compile(r"^<!--\s*INCLUDE:\s*(\S+)\s*-->\s*$", re.MULTILINE)

# The six rendered skeletons (the banner + JSON are dependencies, not standalone docs).
SKELETONS = (
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "ETHOS.md",
    "RESOLVER.md",
    "SCORECARD.md",
)

SAMPLE_CONTEXT = {
    "REPO": "three-cubes/sample-repo",
    "CANONICAL_HOMES": (
        "`tc-fitness` (gate engine) · `tc-pipelines` (reusable CI + governance templates)"
    ),
}


def _resolve_includes(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        target = SKELETON_DIR / match.group(1)
        return target.read_text(encoding="utf-8").rstrip("\n")

    return INCLUDE_RE.sub(_sub, text)


def render(name: str, context: dict[str, str]) -> str:
    text = (SKELETON_DIR / name).read_text(encoding="utf-8")
    text = _resolve_includes(text)
    for token, value in context.items():
        text = text.replace("{{" + token + "}}", value)
    return text


@pytest.mark.parametrize("name", SKELETONS)
def test_skeleton_exists(name: str) -> None:
    assert (SKELETON_DIR / name).is_file(), f"missing skeleton: {name}"


@pytest.mark.parametrize("name", SKELETONS)
def test_renders_with_no_unresolved_tokens(name: str) -> None:
    rendered = render(name, SAMPLE_CONTEXT)
    leftovers = re.findall(r"\{\{.*?\}\}", rendered)
    assert not leftovers, f"{name} has unresolved placeholders: {leftovers}"


@pytest.mark.parametrize("name", SKELETONS)
def test_include_marker_is_resolved(name: str) -> None:
    rendered = render(name, SAMPLE_CONTEXT)
    assert "INCLUDE:" not in rendered, f"{name} left an unresolved INCLUDE marker"


@pytest.mark.parametrize("name", SKELETONS)
def test_banner_is_present_after_render(name: str) -> None:
    rendered = render(name, SAMPLE_CONTEXT)
    assert "🛑 Canonical standards" in rendered, f"{name} is missing the canonical-standards banner"


@pytest.mark.parametrize("name", SKELETONS)
def test_repo_token_substituted(name: str) -> None:
    rendered = render(name, SAMPLE_CONTEXT)
    assert SAMPLE_CONTEXT["REPO"] in rendered, f"{name} did not substitute {{{{REPO}}}}"


def test_banner_carries_canonical_homes_token() -> None:
    # The banner is the single home of the {{CANONICAL_HOMES}} token; guard the factoring.
    banner = (SKELETON_DIR / BANNER).read_text(encoding="utf-8")
    assert "{{CANONICAL_HOMES}}" in banner


def test_every_skeleton_includes_the_banner_source() -> None:
    # Each skeleton must INCLUDE the factored banner rather than inlining its own copy.
    for name in SKELETONS:
        raw = (SKELETON_DIR / name).read_text(encoding="utf-8")
        assert BANNER in raw, f"{name} does not INCLUDE {BANNER}"
