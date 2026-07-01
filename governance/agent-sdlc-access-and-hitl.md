# Agent SDLC access + human-in-the-loop (CANONICAL)

> Promoted into tc-pipelines governance (SGO-163) as the org-canonical
> SDLC-access + HITL standard. The per-agent App manifests it references live at
> [`agent-app-manifests/`](agent-app-manifests/); the enforcement templates
> (`main` ruleset, CODEOWNERS) live alongside in this `governance/` dir. Consuming
> repos (`tc-agent-zone`, `kairix`) converge up to this — do not fork a parallel
> standard.

How agents are granted GitHub access to run the software development lifecycle,
and how a human stays in the loop without becoming the mechanical bottleneck.

## The principle: capability vs. enforcement

Two independent layers. Conflating them is the usual mistake — including the one
that produced this standard.

| Layer | Owned by | Purpose |
|---|---|---|
| **Capability** — what an agent's token *can* do | The agent's GitHub App | Drive the SDLC: plan, branch, commit, PR, read CI, merge, release |
| **Enforcement** — what is *gated* | Repo settings agents cannot change | The human-in-the-loop: branch ruleset, CODEOWNERS, environments |

HITL is **not** achieved by starving capability. It is achieved by **gates an
agent cannot bypass**. So agents get a comprehensive capability set — generous
on reads (no security downside) and on the writes the SDLC needs — and the human
keeps the gates.

## Capability — the GitHub App permission set

Granted comprehensively. Listed alphabetically to map straight to the App /
fine-grained-token settings page. `write` implies `read`. This is the **Full
SDLC (Builder)** tier; the orchestration + contributor tiers are the same table
trimmed — the concrete per-tier encodings are the four
[`agent-app-manifests/*.json`](agent-app-manifests/).

| Permission | Level | Why |
|---|---|---|
| Actions | Read & Write | read runs/logs; re-run flaky jobs, cancel, dispatch |
| Administration | **Read** | *see* branch protection / rulesets / merge-queue config. **Never write** — governance boundary |
| Checks | Read & Write | check-run results; surface Sonar/review as checks |
| Code scanning alerts | Read & Write | triage + dismiss security findings |
| Codespaces (+ lifecycle/metadata/secrets) | No access | unused; secrets boundary |
| Commit statuses | Read & Write | status rollup / merge-queue state |
| Contents | Read & Write | branches, commits, tags, releases, merges |
| Custom properties | Read | repo metadata visibility |
| Dependabot alerts | Read & Write | SCA triage + dismiss |
| Dependabot secrets | No access | secrets boundary |
| Deployments | Read & Write | create/track deploys — gated by Environment reviewers |
| Discussions | Read & Write | if used |
| Environments | **Read** | see env config; protection rules stay human |
| Issues | Read & Write | plan/triage |
| Metadata | Read | mandatory baseline |
| Packages | Read & Write | publish/consume npm packages on release |
| Pages | No access | unused |
| Pull requests | Read & Write | create/update/comment/review/merge |
| Repository security advisories | Read | view advisories |
| Secret scanning alerts | Read | view leaked-secret findings |
| Secrets | **No access** | hard boundary — KV / GitHub-secrets, human-managed |
| Variables | Read & Write | non-secret Actions config |
| Webhooks | No access | integration/admin boundary |
| Workflows | Read & Write | edit `.github/workflows` — **CODEOWNERS-gated** |

**The whole HITL boundary** is five items: `Secrets`/`Dependabot secrets`/
`Codespaces secrets` = none; `Administration` = read-only; `Environments` =
read-only; `Webhooks` = none; `Workflows` = write but CODEOWNERS-gated.
Everything else is granted.

## Enforcement — the gates a human owns

Configured per consuming repo from the templates in this `governance/` dir. These
require `Administration: Write` to apply, which agents do not have — so applying
them is itself a human action.

1. **`main` ruleset** — [`rulesets/main.json`](rulesets/main.json):
   - **require CODEOWNERS review** (the core HITL gate) — the required-approval
     count is per-repo posture, not an org-wide constant: **CORE repos**
     (`tc-pipelines`, `tc-fitness`) keep **n+1 human approval** (D3), **product
     repos** run **0-approval auto-merge on green** with CODEOWNERS only on the
     control plane. See [`AUTONOMOUS-DELIVERY-STANDARD.md`](AUTONOMOUS-DELIVERY-STANDARD.md)
     (STD-MERGE) and [`README.md`](README.md).
   - Required status checks: `Quality gate`, `SonarCloud scan`,
     `SonarCloud Code Analysis`. Private Team-plan repos cannot use a GitHub
     merge queue; where available (public repos) a merge queue re-runs checks on
     the combined commit, else strict status checks stand in.
   - No deletion / no force-push.
   - `bypass_actors`: the human **admin role only** (emergency override). Agent
     Apps are **not** admins.
2. **CODEOWNERS** — [`CODEOWNERS`](CODEOWNERS): routes review to the human owner,
   and pins gate-critical + canon paths (the gate's own definition — CI,
   `[tool.tc_fitness]`, schemas, validators, dep pins, governance) to the human
   team — so an agent can never self-approve a change to the gates that constrain
   it.
3. **Environments** with required reviewers for prod deploys (vm-openclaw /
   hermes) — a human approves the deploy even though the App can create
   deployments.

## The merge model

> Agents prepare **everything** — branch, commits, green CI, PR body, self-review
> — and an agent may **merge**, but only a PR that is CI-green **and** (on the
> control plane, or on any CORE repo) human-approved. The approval is the
> human-in-the-loop. The human stops running `git merge` and starts saying "yes"
> to the diff.

This is strictly *more* HITL than a CI-only gate, while removing the human as the
mechanical bottleneck.

## Identity — one GitHub App per agent

Each agent authenticates as its **own GitHub App** (`tc-agent-builder`,
`tc-agent-shape`, `tc-agent-consultant`, `tc-agent-growth`) — see
[`agent-app-manifests/`](agent-app-manifests/). Why apps, not a shared PAT:

- **Distinct identity** — the audit log shows *which agent* did what (vs. one
  blurred `quanyeomans`/`openclaw-pat` actor).
- **Short-lived tokens** — installation tokens auto-expire (~1h); no long-lived
  PAT to leak or rotate by hand. Replaces the `*-openclaw-pat` secrets in KV.
- **Per-agent least-privilege** — tiers (full / orchestration / contributor)
  scope each agent to its role; Growth can't edit workflows, only Builder can.
- **Commit authorship unchanged** — the App is the *pusher* (clean audit); commit
  *author* stays per the no-LLM-attribution rule (GitHub separates the two). The
  mint helpers set `git config user.name/email` to the selected App's `[bot]`
  identity so the pusher and the recorded author both resolve to the App.

### Runtime: minting a token

Per GitHub operation the agent runtime:
1. Reads `App ID` + private key from Key Vault (`github-app-<agent>-id`,
   `github-app-<agent>-key`).
2. Signs a short-lived **App JWT** (RS256, ≤10 min).
3. Discovers the **installation** on the target repo and exchanges the JWT for an
   **installation token** scoped to it.
4. Uses that token for `git` / `gh` / the API; lets it expire.

Two canonical mint surfaces, both parametrised by agent, live in **this repo**:

| Surface | Where | Selector |
|---|---|---|
| **CI** composite action | [`.github/actions/github-app-token`](../.github/actions/github-app-token/action.yml) | `agent: builder\|shape\|consultant\|growth` (empty = canonical `three-cubes-agent`) |
| **Off-CI / local / MCP** CLI | [`tools/`](../tools/README.md) (`agent-token`) | `--agent builder\|shape\|consultant\|growth` (default = canonical); `--git-config` sets the `[bot]` author on mint |

The runtime consumer in `tc-agent-zone`
(`agentic/skills/technology-management/github-ops/`) sources App creds from KV via
these surfaces instead of the retired `*-openclaw-pat` PAT — tracked as the
runtime follow-up below.

## Setup checklist

**Human / org-owner (one-time) — the `Secrets = No access` boundary means an
agent cannot self-provision these:**
1. Apply the `main` ruleset + add CODEOWNERS from this `governance/` dir.
2. (public repos only) optionally add a merge queue to the ruleset; private
   Team-plan repos cannot, so their strict status checks stand in.
3. Create the 4 Apps from the manifests; generate a private key + install each.
4. Store each `App ID` + `.pem` in `kv-tc-agents` as `github-app-<agent>-id` and
   `github-app-<agent>-key`. Never commit the `.pem`.
5. Put vm-openclaw / hermes deploys behind a GitHub Environment with required
   reviewers.

**Platform (follow-up PRs):**
6. Point `tc-agent-zone`'s `github-ops` runtime at the parametrised
   `agent-token` / `github-app-token` mint surfaces (replace `*-openclaw-pat`).
7. Retire the `*-openclaw-pat` KV secrets once apps are live.

## Scope of this standard

Applies to every repo the agent platform touches — currently
`three-cubes/tc-agent-zone` and `three-cubes/kairix` (which carry the same
ruleset + CODEOWNERS, adapted to their layout), with the canonical templates +
manifests + mint surfaces owned here in `tc-pipelines`.
