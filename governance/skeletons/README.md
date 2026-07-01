# governance/skeletons/ — canonical agent-affordance skeleton set

Placeholder-driven templates a repo renders to get the standard **agent-affordance layer** — the files that
let an agent (or a human) understand *how this repo is shaped and how work is done here* without re-deriving it.
These are **repo-agnostic structure with placeholders**, not another repo's files copied in. Distributed by
[`../scripts/bootstrap-repo-governance.sh`](../scripts/bootstrap-repo-governance.sh).

## The set

| Skeleton | Renders to (repo root) | What it is |
|---|---|---|
| [`CLAUDE.md`](CLAUDE.md) | `CLAUDE.md` | Pointer-reference index — routes every task to its canonical standard; carries the "How to commit" section pointing at `scripts/safe-commit.sh`. |
| [`AGENTS.md`](AGENTS.md) | `AGENTS.md` | Agent entrypoint — the authoring-vs-runtime boundary + the D1 no-attribution rule. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | `CONTRIBUTING.md` | Contributor loop — setup, branch/commit/PR mechanics, the gate-is-the-contract rule. |
| [`ETHOS.md`](ETHOS.md) | `ETHOS.md` | The 7 platform principles (verbatim Rules; repo fills the "How to apply" examples). |
| [`RESOLVER.md`](RESOLVER.md) | `RESOLVER.md` | Intent → location map — keeps the "whose 3am incident is this?" model; repo authors its own domain table. |
| [`SCORECARD.md`](SCORECARD.md) | `SCORECARD.md` | The 5-pillar / 50-binary-check health frame; repo-specific checks marked `TODO`. |
| [`_canonical-standards-banner.md`](_canonical-standards-banner.md) | *(inlined)* | The shared "🛑 Canonical standards" banner, factored out and `INCLUDE`-d by every skeleton. |
| [`claude-settings.postToolUse.json`](claude-settings.postToolUse.json) | `.claude/settings.json` *(jq-merged)* | The `PostToolUse` hook block bootstrap deep-merges (idempotent, not appended). |

## Rendering

Two tokens resolve at render time (from `--repo`):

- `{{REPO}}` — the repo slug, e.g. `three-cubes/<name>`.
- `{{CANONICAL_HOMES}}` — the canonical-homes line, default `` `tc-fitness` (gate engine) · `tc-pipelines` (reusable CI + governance templates) ``.

Rendering is two steps:

1. **Resolve includes** — replace every `<!-- INCLUDE: _canonical-standards-banner.md -->` line with the banner file's contents.
2. **Substitute tokens** — replace `{{REPO}}` and `{{CANONICAL_HOMES}}`.

After both steps a rendered skeleton contains **zero unresolved `{{...}}`** and no `INCLUDE:` marker — asserted by
[`tests/test_render_skeletons.py`](tests/test_render_skeletons.py). `bootstrap-repo-governance.sh` performs the
identical two-step render (banner inline + `sed` token substitution) so the bash and Python renders never drift.

## Genericising discipline

The transferable content is **structure**, not specifics. `ETHOS` Rules stay verbatim; its path examples are
placeholders. `RESOLVER` keeps the mental model + decision-tree shape but the domain table is the repo's own.
`SCORECARD` keeps the 5 pillars + scoring/honesty rules; the 50 checks are `TODO` until the repo authors them.
Never copy another repo's domain table, checks, or paths — derive them from the "whose 3am incident is this?" test.
