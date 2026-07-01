"""Unit tests for the per-agent selection + identity logic of `agent-token`.

Network/`az`/`git` side effects are intentionally NOT exercised here — these
cover the pure, per-agent-parametrisation surface added for SGO-163: selector
resolution, the KV secret-name contract, JWT claims, and the [bot] git identity.
"""

from __future__ import annotations

import pytest

from tc_agent_tools import agent_token as at


def test_default_agent_is_canonical():
    agent = at.resolve_agent(None)
    assert agent.key == at.CANONICAL
    assert agent.bot_slug == "three-cubes-agent"
    # canonical keeps its legacy secret names + explicit installation-id secret
    assert agent.app_id_secret == "github-threecubes-agent-app-id"
    assert agent.private_key_secret == "github-threecubes-agent-private-key"
    assert agent.installation_id_secret == "github-threecubes-agent-installation-id"
    assert agent.bot_user_id == 295831460


@pytest.mark.parametrize(
    ("selector", "slug"),
    [
        ("builder", "tc-agent-builder"),
        ("shape", "tc-agent-shape"),
        ("consultant", "tc-agent-consultant"),
        ("growth", "tc-agent-growth"),
    ],
)
def test_per_agent_secret_name_contract(selector, slug):
    agent = at.resolve_agent(selector)
    assert agent.app_id_secret == f"github-app-{selector}-id"
    assert agent.private_key_secret == f"github-app-{selector}-key"
    assert agent.bot_slug == slug
    # per-agent Apps discover the installation + bot id at runtime
    assert agent.installation_id_secret is None
    assert agent.bot_user_id is None


def test_unknown_agent_is_actionable():
    with pytest.raises(SystemExit) as exc:
        at.resolve_agent("nope")
    msg = str(exc.value)
    assert "unknown --agent 'nope'" in msg
    assert "builder" in msg and "growth" in msg


def test_jwt_claims_window():
    claims = at.jwt_claims("12345", now=1_000_000)
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "12345"}
    # never exceeds GitHub's 10-minute ceiling
    assert claims["exp"] - claims["iat"] <= 600


def test_bot_identity_format():
    agent = at.resolve_agent("builder")
    name, email = at.bot_identity(agent, 424242)
    assert name == "tc-agent-builder[bot]"
    assert email == "424242+tc-agent-builder[bot]@users.noreply.github.com"


def test_parse_args_defaults_and_choices():
    args = at.parse_args([])
    assert args.agent is None and args.git_config is False and args.repo is None

    args = at.parse_args(["--agent", "shape", "--git-config", "--repo", "three-cubes/kairix"])
    assert args.agent == "shape" and args.git_config is True
    assert args.repo == "three-cubes/kairix"

    # the canonical key is not a --agent choice (it's the default, selector-free)
    with pytest.raises(SystemExit):
        at.parse_args(["--agent", at.CANONICAL])
