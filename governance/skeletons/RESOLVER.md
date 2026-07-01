---
title: {{REPO}} Resolver
audience: contributors, Claude Code, sub-agents
purpose: map intent → location
last_reviewed: TODO
---

# {{REPO}} — RESOLVER

Canonical "where does X belong" map. Read this when you don't know where to put a new file or where to find an existing one.

<!-- INCLUDE: _canonical-standards-banner.md -->

## Mental model

The repo is organised around **first-class domains**. If we moved off the monorepo tomorrow, each top-level directory would be its own repo with its own lifecycle and operator. Keep that in mind when placing files — if a file straddles two domains, it almost always belongs in the one that owns its lifecycle.

Two ops disciplines are commonly first-class:

- **DevSecOps** — design, build, secure, deploy, operate, sustain **infra / applications / developer experience**. Success = uptime, security posture, MTTR on platform incidents.
- **AgentOps** — same lifecycle for **agentic systems**. Success = behaviour quality, attribution, eval-driven iteration, MTTR on behavioural drift.

Conflating them is the most common placement mistake. Ask: *whose 3am incident is this?*

## Target top-level domains

> **TODO (repo authors its own):** replace this block with your repo's actual top-level layout. Each row is a
> first-class domain with its own lifecycle owner. Keep the set small; every top-level dir carries a `README.md`
> resolver. Do not copy another repo's domain table — derive yours from the "whose 3am incident is this?" test.

```
/<domain-a>   TODO — what lives here, and whose lifecycle owns it
/<domain-b>   TODO
/docs         Architecture docs, ADRs, standards
/scripts      Repo-developer utilities (generators, fitness functions, CI helpers)
/tests        Tests (cross-domain when they straddle, otherwise co-located)
```

## Jobs-to-be-done → location

Use these as decision trees. The first one that answers your job is correct. The examples are illustrative
placeholders — retarget each to your repo's real paths.

### "I need to find or place a domain artefact"
→ Place it in the first-class domain whose lifecycle owns it. If it straddles two, split the change or place it with the owner of its *incident* (see mental model).

### "I need to find or place a deploy/apply script"
→ Group by *whose* lifecycle the script serves (infra/app vs agentic), not by language or tool. TODO: name your apply roots.

### "I need to find or place a cron job / scheduled task"
→ Whose lifecycle does the job own? Route infra/app jobs and agent jobs to their respective ops roots. Never reference the repo path directly from the crontab — reference an installed path or a skill.

### "I need to find or place a runbook"
→ Whose incident is this? Agent-acting-wrong, infra/cross-app problem, or one specific service degraded — each has its own runbook home. TODO: name them.

### "I need to find or place documentation"
- Architecture / ADR / standard: `/docs/`
- Domain-specific runbook: co-located with its domain (see the runbook rule above)
- Service-specific README: `/<service>/README.md`

### "I need to add a configuration value, environment variable, or deployment identifier"
→ Keep a single source of truth (e.g. a `deployment-targets.yaml`). Never hardcode the same value in two places.

## How this gets enforced

A fitness rule (run as part of `uv run tc-fitness run`) should diff the PR's added files against `main` and fail
net-new files that land under a deprecated or unsanctioned path. A genuine *move* (git status `R`) is allowed
because the content is just relocating; a net-new file under a retired prefix is what the gate catches. TODO: wire
the check your repo needs (orphan-top-dir guard, deprecated-path guard) into `[tool.tc_fitness]`.

## How RESOLVER composes with the standards

- The canonical standards index answers *what's the rule?*; the RESOLVER answers *where does the file live?*
- Naming rules govern *syntax* (kebab-case / snake_case / file conventions); RESOLVER governs *intent → location*.
- When intent → location is genuinely ambiguous, re-apply the *"whose 3am incident is this?"* test.

## When this table is incomplete

If you can't find your intent here, your work likely:
- crosses two concerns (consider whether it should be two PRs);
- introduces a new concern (propose a RESOLVER update in the same PR);
- is a refactor that legitimately moves things (sweep tool + migration plan in one PR).
