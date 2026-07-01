# tools/ — `tc-agent-tools`

Off-CI local agent tooling, installable by import (no per-repo copy). Single source of
truth lives here; consuming repos pin a tag and run it via `uvx`.

## `agent-token`

Mint a short-lived GitHub App **installation token** from `kv-tc-agents` (via `az`),
so a **local / MCP agent acts as the App, not a human** — PRs land for review with
clean App authorship and no shared personal credentials. The App key never leaves the
vault except as a ~9-minute in-memory assertion; the printed token is a ~1-hour
installation token. This is the off-CI complement to the CI
[`github-app-token`](../.github/actions/github-app-token/action.yml) composite action —
both mint the same App identities.

**Requires:** an `az login` session with **Key Vault Secrets User** on `kv-tc-agents`.

### Per-agent Apps (SGO-163)

`--agent builder|shape|consultant|growth` mints as that agent's **own** App
identity, resolving the Key Vault secrets `github-app-<agent>-id` /
`github-app-<agent>-key` and discovering the installation from the App JWT. Omit
`--agent` for the canonical `three-cubes-agent` org App (legacy secret names) —
backward compatible with existing consumers. See the canonical
[per-agent App set + SDLC-access standard](../governance/agent-sdlc-access-and-hitl.md).

| Flag | Effect |
|---|---|
| `--agent <name>` | select a per-agent App (`builder`/`shape`/`consultant`/`growth`); default = canonical `three-cubes-agent` |
| `--repo OWNER/REPO` | scope the installation lookup to one repo (per-agent Apps) |
| `--git-config` | also set `git config user.name/email` to the App's `[bot]` identity on mint |

### Use (pinned, single-source — nothing vendored into the consuming repo)

```bash
# Pin to a released tag (content-pinned); @v1 tracks the latest v1.x.

# canonical org App (default) — pure token on stdout:
export GH_TOKEN="$(uvx --from 'git+https://github.com/three-cubes/tc-pipelines@v1#subdirectory=tools' agent-token)"
git config user.name  'three-cubes-agent[bot]'
git config user.email '295831460+three-cubes-agent[bot]@users.noreply.github.com'

# a per-agent App, setting the [bot] git author in the same step:
export GH_TOKEN="$(uvx --from 'git+https://github.com/three-cubes/tc-pipelines@v1#subdirectory=tools' agent-token --agent builder --git-config)"
# now git push / gh pr create / gh pr merge act as the App
```

Consuming repos document this invocation in their agent guide (`CLAUDE.md` / `AGENTS.md`)
so an agent never raises a PR under a human's account.
