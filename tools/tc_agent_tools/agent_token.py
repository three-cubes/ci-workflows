#!/usr/bin/env python3
"""Mint a short-lived per-agent GitHub App installation token.

Reads the App credentials from Azure Key Vault (kv-tc-agents) via `az` — so a local
or MCP agent that is `az login`'d (as a Key Vault Secrets reader) authenticates to
GitHub AS THE APP, never as a human. The App key never leaves the vault except as a
~9-minute in-memory assertion; the printed token is a ~1-hour installation token.

This is the off-CI complement to the `github-app-token` composite action (which mints
the same App identities inside GitHub Actions over WIF).

Per-agent Apps (SGO-163): pass `--agent builder|shape|consultant|growth` to mint as
that agent's own App identity, resolving the Key Vault secrets
`github-app-<agent>-id` / `github-app-<agent>-key`. With no `--agent`, the canonical
org App (`three-cubes-agent`) is used from its legacy secret names — backward
compatible with existing `uvx ... agent-token` consumers.

Install + use (pinned, single-source — no per-repo copy):

    # canonical org App (default), pure token on stdout:
    export GH_TOKEN="$(uvx --from 'git+https://github.com/three-cubes/tc-pipelines@v1#subdirectory=tools' agent-token)"

    # a per-agent App, and set the git author to its [bot] identity in one step:
    export GH_TOKEN="$(uvx --from 'git+https://github.com/three-cubes/tc-pipelines@v1#subdirectory=tools' agent-token --agent builder --git-config)"

    git push / gh pr create / gh pr merge ...   # now act as the App, not a human
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import jwt  # PyJWT

VAULT = "kv-tc-agents"
API = "https://api.github.com"
CANONICAL = "three-cubes-agent"


@dataclass(frozen=True)
class AgentApp:
    """A GitHub App identity + how to source its credentials from Key Vault."""

    key: str  # CLI selector
    app_id_secret: str  # KV secret holding the App ID
    private_key_secret: str  # KV secret holding the App private key (.pem)
    bot_slug: str  # App slug; the git author login is f"{bot_slug}[bot]"
    installation_id_secret: str | None = None  # explicit id secret; None => discover
    bot_user_id: int | None = None  # known numeric id; None => discover via the API


def _per_agent(key: str, slug: str) -> AgentApp:
    """Build a per-agent App descriptor from the github-app-<agent>-{id,key} contract."""
    return AgentApp(
        key=key,
        app_id_secret=f"github-app-{key}-id",
        private_key_secret=f"github-app-{key}-key",
        bot_slug=slug,
    )


# The canonical org App keeps its legacy secret names (back-compat); the four
# per-agent Apps follow the github-app-<agent>-{id,key} convention from
# governance/agent-sdlc-access-and-hitl.md.
AGENTS: dict[str, AgentApp] = {
    CANONICAL: AgentApp(
        key=CANONICAL,
        app_id_secret="github-threecubes-agent-app-id",
        private_key_secret="github-threecubes-agent-private-key",
        installation_id_secret="github-threecubes-agent-installation-id",
        bot_slug="three-cubes-agent",
        bot_user_id=295831460,
    ),
    "builder": _per_agent("builder", "tc-agent-builder"),
    "shape": _per_agent("shape", "tc-agent-shape"),
    "consultant": _per_agent("consultant", "tc-agent-consultant"),
    "growth": _per_agent("growth", "tc-agent-growth"),
}


def resolve_agent(name: str | None) -> AgentApp:
    """Map a CLI selector to its AgentApp; default = the canonical org App."""
    key = name or CANONICAL
    try:
        return AGENTS[key]
    except KeyError:
        choices = ", ".join(k for k in AGENTS if k != CANONICAL)
        raise SystemExit(
            f"agent-token: unknown --agent '{key}' (expected one of: {choices}; "
            f"omit --agent for the canonical {CANONICAL} App)"
        ) from None


def jwt_claims(app_id: str, now: int) -> dict[str, int | str]:
    """The App-JWT claim set: 1-min backdated iat, 9-min exp (GitHub caps at 10)."""
    return {"iat": now - 60, "exp": now + 540, "iss": app_id}


def bot_identity(agent: AgentApp, user_id: int) -> tuple[str, str]:
    """The git author (name, email) for an App's [bot] account."""
    login = f"{agent.bot_slug}[bot]"
    return login, f"{user_id}+{login}@users.noreply.github.com"


def kv(name: str) -> str:
    """Read a secret value from the agent Key Vault via the local `az` login."""
    return subprocess.check_output(
        [
            "az", "keyvault", "secret", "show",
            "--vault-name", VAULT, "--name", name, "--query", "value", "-o", "tsv",
        ],
        text=True,
    ).strip()


def _api(path: str, token: str, *, bearer: bool = False) -> dict | list:
    """GET a GitHub API resource with an App JWT (bearer) or installation token."""
    scheme = "Bearer" if bearer else "token"
    req = urllib.request.Request(  # noqa: S310 — fixed api.github.com base, not user input
        f"{API}{path}",
        headers={
            "Authorization": f"{scheme} {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as r:  # noqa: S310 — fixed api.github.com base
        return json.load(r)


def _post(path: str, assertion: str) -> dict:
    """POST to a GitHub API resource with an App JWT bearer assertion."""
    req = urllib.request.Request(  # noqa: S310 — fixed api.github.com base, not user input
        f"{API}{path}",
        method="POST",
        headers={
            "Authorization": f"Bearer {assertion}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as r:  # noqa: S310 — fixed api.github.com base
        return json.load(r)


def resolve_installation_id(agent: AgentApp, assertion: str, repo: str | None) -> str:
    """Find the installation id: explicit KV secret, else discover via the App JWT."""
    if agent.installation_id_secret:
        return kv(agent.installation_id_secret)
    if repo:
        return str(_api(f"/repos/{repo}/installation", assertion, bearer=True)["id"])
    installs = _api("/app/installations", assertion, bearer=True)
    if not installs:
        raise SystemExit(
            f"agent-token: App '{agent.bot_slug}' has no installations — install it on "
            f"the target repo first (see governance/agent-app-manifests/README.md)"
        )
    return str(installs[0]["id"])


def apply_git_config(agent: AgentApp, token: str) -> None:
    """Set git user.name/email to the App's [bot] identity in the current repo."""
    user_id = agent.bot_user_id
    if user_id is None:
        login = f"{agent.bot_slug}[bot]"
        try:
            user = _api(f"/users/{urllib.parse.quote(login)}", token)
            user_id = int(user["id"])
        except Exception as exc:  # noqa: BLE001 — surface an actionable message
            raise SystemExit(
                f"agent-token: could not resolve the bot user id for '{login}' "
                f"({exc}). Create + install the App first "
                f"(governance/agent-app-manifests/README.md), then retry --git-config."
            ) from None
    name, email = bot_identity(agent, user_id)
    try:
        subprocess.run(["git", "config", "user.name", name], check=True)
        subprocess.run(["git", "config", "user.email", email], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(
            f"agent-token: --git-config failed to set the git author ({exc}); "
            f"run inside a git repo, or drop --git-config and set it by hand."
        ) from None
    print(f"agent-token: git author set to {name} <{email}>", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="agent-token",
        description="Mint a short-lived per-agent GitHub App installation token.",
    )
    p.add_argument(
        "--agent",
        choices=[k for k in AGENTS if k != CANONICAL],
        default=None,
        help=f"per-agent App to mint as (default: the canonical {CANONICAL} App)",
    )
    p.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/REPO",
        help="scope the installation lookup to this repo (per-agent Apps only)",
    )
    p.add_argument(
        "--git-config",
        action="store_true",
        help="also set git user.name/email to the App's [bot] identity",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    agent = resolve_agent(args.agent)

    app_id = kv(agent.app_id_secret)
    pem = kv(agent.private_key_secret)

    assertion = jwt.encode(jwt_claims(app_id, int(time.time())), pem, algorithm="RS256")
    inst_id = resolve_installation_id(agent, assertion, args.repo)
    token = _post(f"/app/installations/{inst_id}/access_tokens", assertion)["token"]

    if args.git_config:
        apply_git_config(agent, token)

    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
