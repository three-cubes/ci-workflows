# Per-agent GitHub App manifests (CANONICAL)

> Promoted into tc-pipelines governance (SGO-163) as the org-canonical per-agent
> App set. Consuming repos (tc-agent-zone, kairix) converge up to these rather
> than forking a parallel set.

Each agent gets its **own GitHub App** so its actions carry a distinct bot
identity in the audit log, mint **short-lived auto-rotating installation
tokens** (no long-lived PATs), and can be scoped to a **least-privilege tier**.
Full rationale + the runtime wiring: [`../agent-sdlc-access-and-hitl.md`](../agent-sdlc-access-and-hitl.md).

## Permission tiers

| App | Agent | Tier | What it can do |
|---|---|---|---|
| `tc-agent-builder` | Builder | **Full SDLC** | branch/commit/PR/merge, read+manage CI, edit workflows (CODEOWNERS-gated), triage security alerts, create deployments |
| `tc-agent-shape` | Shape | **Orchestration** | open + merge PRs, manage issues, read CI + branch protection. No workflow/security/deploy write |
| `tc-agent-consultant` | Consultant | **Contributor** | contribute docs/deliverables via PRs + issues |
| `tc-agent-growth` | Growth | **Contributor** | contribute content/collateral via PRs + issues |

No app is a repo **admin** — none can bypass the `main` ruleset or edit branch
protection. That boundary is what keeps a human in the loop.

## Create each app (org-owner action — one click per app)

These are **created via the GitHub App Manifest flow**, which pre-fills the
permissions from the JSON so you don't hand-tick them:

1. Open: `https://github.com/organizations/three-cubes/settings/apps/new`
2. Or use the manifest flow to pre-load a manifest: submit the matching
   `*.json` here as the App manifest (a small POST form — see the canon doc's
   "Create the apps" section for a ready-to-open HTML snippet).
3. After creation: **Generate a private key** (downloads a `.pem`), note the
   **App ID**, then **Install** the app on `three-cubes/tc-agent-zone`
   (and `three-cubes/kairix` if the agent works there) — note the **Installation ID**.
4. Hand the `App ID` + `.pem` to the platform owner to store in Key Vault
   (`kv-tc-agents`, as `github-app-<agent>-id` and `github-app-<agent>-key`
   — e.g. `github-app-builder-id`, `github-app-builder-key`).
   **Never commit the `.pem`.**

The agent runtime then mints an installation token per operation from
`App ID` + private key (discovering the installation on the target repo) — via
the [`agent-token`](../../tools/README.md) CLI (off-CI / local / MCP agents)
or the [`github-app-token`](../../.github/actions/github-app-token/action.yml)
composite action (CI), both parametrised with `--agent` / `agent:`. See the
canon doc.

## Secret-name contract

| Secret in `kv-tc-agents` | Holds |
|---|---|
| `github-app-<agent>-id` | the App ID (numeric) |
| `github-app-<agent>-key` | the App private key (`.pem` contents) |

`<agent>` is one of `builder`, `shape`, `consultant`, `growth`. The installation
id is discovered at mint time from the App JWT (no per-agent installation-id
secret to provision). The canonical org App (`three-cubes-agent`) keeps its
legacy secret names (`github-threecubes-agent-{app-id,installation-id,private-key}`)
for back-compat and remains the default when no agent is selected.
