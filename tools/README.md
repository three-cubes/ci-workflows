# tools/ — `tc-agent-tools`

Off-CI local agent tooling, installable by import (no per-repo copy). Single source of
truth lives here; consuming repos pin a tag and run it via `uvx`.

## `agent-token`

Mint a short-lived `three-cubes-agent` GitHub App **installation token** from
`kv-tc-agents` (via `az`), so a **local / MCP agent acts as the App, not a human** —
PRs land for review with clean App authorship and no shared personal credentials. The
App key never leaves the vault except as a ~9-minute in-memory assertion; the printed
token is a ~1-hour installation token. This is the off-CI complement to the CI
[`github-app-token`](../.github/actions/github-app-token/action.yml) composite action —
both mint the same App identity.

**Requires:** an `az login` session with **Key Vault Secrets User** on `kv-tc-agents`.

### Use (pinned, single-source — nothing vendored into the consuming repo)

```bash
# Pin to a released tag (content-pinned); @v1 tracks the latest v1.x.
export GH_TOKEN="$(uvx --from 'git+https://github.com/three-cubes/tc-pipelines@v1#subdirectory=tools' agent-token)"
git config user.name  'three-cubes-agent[bot]'
git config user.email '295831460+three-cubes-agent[bot]@users.noreply.github.com'
# now git push / gh pr create / gh pr merge act as the App
```

Consuming repos document this invocation in their agent guide (`CLAUDE.md` / `AGENTS.md`)
so an agent never raises a PR under a human's account.
