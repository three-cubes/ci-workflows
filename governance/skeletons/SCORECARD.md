---
title: {{REPO}} Scorecard
audience: contributors, CI gate
target: ">=90/100 for production-ready state"
last_reviewed: TODO
---

# {{REPO}} — SCORECARD.md

> 50 binary checks x 2 points = 100. Target **>=90/100**. Per-pillar passes are independent — a 0 in one pillar
> doesn't zero the others. Wire the runner into `uv run tc-fitness run` (or a `tools/scorecard/run.py`) so the
> grade is reproducible and the regression gate bites.

<!-- INCLUDE: _canonical-standards-banner.md -->

> **Genericising for your repo:** the 5-pillar frame and the scoring/honesty rules are canon — keep them. The
> ten checks under each pillar are **repo-specific**; each is a `TODO` placeholder below. Replace them with binary,
> evidence-backed checks your repo actually enforces (each check names the artefact it inspects and the standard it
> operationalises). Do not copy another repo's checks verbatim — they encode that repo's structure.

## Pillar 1 — Contract surface (manifest + boundaries)
1. TODO — a manifest/inventory exists and validates against its schema
2. TODO — every first-class artefact appears in the manifest
3. TODO — no artefact is declared in the manifest but missing on disk
4. TODO — the authoring/runtime boundary is named (root `AGENTS.md`)
5. TODO — `ETHOS.md` exists and is honoured by the surfaces that consume it
6. TODO — `RESOLVER.md` exists and covers every top-level concern
7. TODO — no top-level dirs outside the allowed set
8. TODO — every top-level dir has a `README.md` (or is exempt)
9. TODO — the secret baseline is current
10. TODO — repo-specific contract check

## Pillar 2 — Runtime composition (skills, agents, configs)
1. TODO — every runtime unit has its canonical source-of-truth config
2. TODO — generated identity/artefacts stay within their size/shape caps
3. TODO — every skill/tool has its required definition file
4. TODO — every definition carries its required canonical sections
5. TODO — scheduled jobs live with the unit that owns them, not in a root dump
6. TODO — permission/approval policy uses semantic categories, not name allowlists (ETHOS-4)
7. TODO — context/model/compaction defaults are pinned and sane
8. TODO — no runtime branch keyed off agent/model/task type (ETHOS-2)
9. TODO — skill auto-update is disabled where trust requires pinning
10. TODO — repo-specific composition check

## Pillar 3 — Observability + eval
1. TODO — a single observability/eval root exists (no shadow trees)
2. TODO — the token/usage audit runs and writes evidence
3. TODO — the model/state monitor runs and writes evidence
4. TODO — the telemetry collector is installed and enabled
5. TODO — every unit has an eval suite
6. TODO — every active skill has at least one scenario
7. TODO — baseline runs are committed with date + model in the filename
8. TODO — the scorecard runner is itself runnable (meta-check)
9. TODO — a scheduled scorecard run is registered
10. TODO — a scorecard regression gate is wired into the quality harness

## Pillar 4 — Security + provenance
1. TODO — privileged config (sudoers, network policy) passes its validator
2. TODO — no hardcoded developer/VM absolute paths in durable-source domains (ETHOS-6)
3. TODO — the secret baseline has zero unaudited entries
4. TODO — untrusted auto-update is off
5. TODO — every third-party plugin/MCP is pinned to a commit or tag
6. TODO — network egress/endpoints are constrained where required
7. TODO — instance-metadata access is blocked from containers where required
8. TODO — the token-rotation runbook exists and is not stale
9. TODO — every regenerated artefact has a provenance manifest (ETHOS-7)
10. TODO — the storage/ownership policy validates (no hand-edits to generated paths, ETHOS-5)

## Pillar 5 — Operational hygiene
1. TODO — the bootstrap/apply scripts are idempotent (run twice, same result)
2. TODO — disk/swap/log layout follows the operational standard
3. TODO — cron entries reference installed paths, never the repo path directly
4. TODO — housekeeping jobs (prune, vacuum) exist and run on cadence
5. TODO — the config-apply step is integrated into the bootstrap
6. TODO — the required services are in the explicit enable-list
7. TODO — auto-update/renewal timers are deployed by bootstrap
8. TODO — the platform-cron apply is integrated into the bootstrap
9. TODO — the drift-check passes against current live state
10. TODO — repo-specific hygiene check

## Running the scorecard

```bash
# Wire your runner here — e.g.:
uv run tc-fitness run                         # if the scorecard checks are fitness rules
python3 tools/scorecard/run.py                # full grade
python3 tools/scorecard/run.py --pillar 4     # one pillar
python3 tools/scorecard/run.py --diff         # against baseline-YYYY-MM-DD.json
```

Output JSON to a stable path (e.g. `tools/scorecard/result-latest.json`) and append every run to a history log.

## Honesty rules

- Don't tweak the scorecard to make a state pass. If a check is wrong, propose a PR change to the check itself.
- Skipped checks (because their artefact doesn't exist yet) count as 0, not as N/A. The total still totals over 100.
- Don't run the scorecard from a dirty working tree — commit first.
