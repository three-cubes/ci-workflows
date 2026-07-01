---
type: adr-index
id: ADR-INDEX
title: Cross-repo ADR index (Three Cubes paved road)
status: living
date: 2026-07-02
owner: "@three-cubes/maintainers"
scanned: 2026-07-02
related:
  - governance/decisions/ADR-POLICY.md
linear:
  - SGO-176
  - SGO-179
---

# ADR-INDEX — every ADR across the paved road, in one place

The cross-repo view that [`ADR-POLICY.md`](ADR-POLICY.md) makes possible. It lists the org-level
register (canonical here) plus every **product** ADR across tc-agent-zone, kairix, and kata by its
org-unique **alias** (`TAZ-ADR-###` / `KAI-ADR-###` / `KATA-ADR-###`), so a decision can be named
unambiguously without any repo renumbering.

**How to read it.** *Home repo / home path* is where the record lives and is owned; the alias is the
stable cross-repo handle (see `ADR-POLICY.md` §Decision). Status is the value declared in the ADR's
own frontmatter/header at the scan date (`—` = the ADR does not declare a status). This index is
**hand-maintained today**; SGO-179 adds a small in-repo walker (`docs/decisions/INDEX.md` per repo)
that generates the per-repo slice mechanically.

> **Scan basis (2026-07-02).** Ranges reflect what is actually on disk in each repo's `record_dir`,
> which supersedes the approximate ranges in the tracking issues: tc-agent-zone `docs/decisions/`
> **ADR-001..046** (011–012 unused), kairix `docs/architecture/` **ADR-017..036** (033–034 unused),
> kata `docs/adr/` **ADR-001..013**. Plus the tc-agent-zone `docs/architecture/` collision pair,
> pending migration.

## Org-level register (home: tc-pipelines)

Canonical in this repo, prefixed IDs, **not** `ADR-###`. Full statements + sources of record are in
[`ADR-POLICY.md`](ADR-POLICY.md#the-org-level-register-canonical-here):

`STD-IDENTITY` · `STD-MERGE` · `RULESET-D1` · `CODEOWNERS-D1` · `GATE-HARDEN` · `QG-CONVERGE` ·
`SONAR-HANDOFF` · `MUT-RATCHET` · `DEP-D1` · `REPO-MERGE` · `VERS-D1` · `WIF-D1..D5` · `COST-D1/2`.

## tc-agent-zone — `docs/decisions/` (alias `TAZ-ADR-###`)

| Alias | Home path | Status | Title / notes |
|---|---|---|---|
| TAZ-ADR-001 | `docs/decisions/ADR-001-shell-cron-migration.md` | — | Replace LLM-wrapped cron jobs with shell scripts |
| TAZ-ADR-002 | `docs/decisions/ADR-002-vm-privileged-access.md` | — | VM privileged access management — defence in depth |
| TAZ-ADR-003 | `docs/decisions/ADR-003-agent-memory-architecture-defaults.md` | Adopted | Agent memory architecture defaults |
| TAZ-ADR-004 | `docs/decisions/ADR-004-openclaw-config-canonical-state.md` | Adopted | OpenClaw config canonical state |
| TAZ-ADR-005 | `docs/decisions/ADR-005-llm-as-judge-fitness-function.md` | Adopted | LLM-as-judge as an architectural fitness function |
| TAZ-ADR-006 | `docs/decisions/ADR-006-imds-block-and-per-agent-secret-injection.md` | Accepted | Block IMDS from containers + per-agent secret injection |
| TAZ-ADR-007 | `docs/decisions/ADR-007-workspace-tier-storage-policy.md` | Proposed | Workspace-tier storage policy |
| TAZ-ADR-008 | `docs/decisions/ADR-008-kairix-context-bridge-plugin.md` | Proposed | Kairix-context-bridge plugin — per-event context augmentation |
| TAZ-ADR-009 | `docs/decisions/ADR-009-policies-as-code-identities-as-text.md` | Proposed | Platform direction — policies become code, identities stay text |
| TAZ-ADR-010 | `docs/decisions/ADR-010-toolpack-and-capability-pattern.md` | Adopted | ToolPack manifest + capability pattern + route-test harness |
| TAZ-ADR-013 | `docs/decisions/ADR-013-agent-constitution-conformance.md` | Proposed | Agent constitution + two-tier conformance scoring · ⚠ number collides with `docs/architecture/adr-013` (see follow-ups) |
| TAZ-ADR-014 | `docs/decisions/ADR-014-quality-ratchet.md` | Adopted | Touch-based quality ratchet |
| TAZ-ADR-015 | `docs/decisions/ADR-015-js-ts-tooling-baseline.md` | Proposed | JS/TS tooling baseline — pnpm workspace + flat-config eslint |
| TAZ-ADR-016 | `docs/decisions/ADR-016-python-dependency-locking-with-uv.md` | Proposed | Python dependency locking with uv workspace |
| TAZ-ADR-017 | `docs/decisions/ADR-017-canonical-mcp-tooling.md` | Adopted | Canonical MCP tooling pattern |
| TAZ-ADR-018 | `docs/decisions/ADR-018-openclaw-plugin-install-script-policy.md` | Proposed | OpenClaw plugin install-script policy |
| TAZ-ADR-019 | `docs/decisions/ADR-019-kairix-memory-provider.md` | Adopted | openclaw memory CLI surface is Kairix-backed |
| TAZ-ADR-020 | `docs/decisions/ADR-020-agent-identity-and-telemetry-propagation.md` | Adopted | Agent identity + telemetry via W3C Baggage |
| TAZ-ADR-021 | `docs/decisions/ADR-021-openclaw-baggage-injection-patch.md` | Proposed | Patch openclaw locally to inject W3C Baggage |
| TAZ-ADR-022 | `docs/decisions/ADR-022-ppt-skill-stack-composition.md` | Adopted | PowerPoint skill stack composition |
| TAZ-ADR-023 | `docs/decisions/ADR-023-ppt-rendering-tooling-python-pptx-and-pptxgenjs-hybrid.md` | Superseded | PPT rendering tooling — hybrid python-pptx / pptxgenjs |
| TAZ-ADR-024 | `docs/decisions/ADR-024-two-track-agent-team.md` | Superseded | Two-track agent team · superseded_by `docs/operating-model.md` (SGO-111) |
| TAZ-ADR-025 | `docs/decisions/ADR-025-llm-judge-ci-strategy.md` | Adopted | LLM-judge CI strategy |
| TAZ-ADR-026 | `docs/decisions/ADR-026-agent-retry-discipline.md` | Proposed | Subagent retry discipline |
| TAZ-ADR-027 | `docs/decisions/ADR-027-toolpack-phase-2-discovery-findability-linkage.md` | Adopted | ToolPack Phase 2 — discovery + findability + linkage |
| TAZ-ADR-028 | `docs/decisions/ADR-028-agentic-mece-reorganisation.md` | Superseded | agentic/ MECE reorganisation by professional specialism |
| TAZ-ADR-029 | `docs/decisions/ADR-029-public-names-describe-work-not-language.md` | Accepted | Public names describe work, not implementation language |
| TAZ-ADR-030 | `docs/decisions/ADR-030-contract-coverage-of-workflow-and-config-surfaces.md` | Accepted | Contract coverage of workflow + configuration surfaces |
| TAZ-ADR-031 | `docs/decisions/ADR-031-capability-package-portable-agentic-pattern.md` | Accepted | Capability Package as the portable agentic pattern |
| TAZ-ADR-032 | `docs/decisions/ADR-032-sfia-subcategory-skill-ia.md` | Accepted | SFIA subcategory-first information architecture |
| TAZ-ADR-033 | `docs/decisions/ADR-033-vm-disk-encryption.md` | Adopted | VM disk encryption — platform-managed keys baseline |
| TAZ-ADR-034 | `docs/decisions/ADR-034-capability-runtime-cold-start.md` | Accepted | Capability runtime cold-start contract |
| TAZ-ADR-035 | `docs/decisions/ADR-035-state-tier-architecture.md` | Adopted | State-tier architecture — stateful core, hydrate/harvest |
| TAZ-ADR-036 | `docs/decisions/ADR-036-hermes-configuration-baseline.md` | Adopted | Hermes configuration baseline |
| TAZ-ADR-037 | `docs/decisions/ADR-037-render-from-source-template.md` | Proposed | Render decks from the source brand template |
| TAZ-ADR-038 | `docs/decisions/ADR-038-session-harvest-lifecycle.md` | — | Session-harvest lifecycle (T3/T4 harvest primitive) |
| TAZ-ADR-039 | `docs/decisions/ADR-039-autonomous-agent-continuation.md` | — | Agent platform architecture — per-team Hermes compute VMs |
| TAZ-ADR-040 | `docs/decisions/ADR-040-language-aware-pre-publish-acceptance.md` | Proposed | Language-aware pre-publish acceptance (editorial / keigo gate) |
| TAZ-ADR-041 | `docs/decisions/ADR-041-claude-capability-bundle-architecture.md` | — | Capability-bundle target architecture (consulting bundle) |
| TAZ-ADR-042 | `docs/decisions/ADR-042-delivery-management-operating-model.md` | — | Delivery-management operating model |
| TAZ-ADR-043 | `docs/decisions/ADR-043-linear-reference-architecture-agent-primary.md` | — | Linear reference architecture for an agent-primary org |
| TAZ-ADR-044 | `docs/decisions/ADR-044-operational-code-immutability-and-deploy-control-plane.md` | — | Operational code immutability + auditable deploy control plane |
| TAZ-ADR-045 | `docs/decisions/ADR-045-one-capability-per-job-no-implementation-splits.md` | — | One capability per job — consolidate agent-facing surfaces |
| TAZ-ADR-046 | `docs/decisions/ADR-046-capability-verification-at-the-rule-layer.md` | — | Capability verification at the rule layer (validate ≠ runtime) |

*Numbers 011 and 012 are unused in this series (never allocated in `docs/decisions/`).*

### tc-agent-zone — `docs/architecture/` (collision pair, pending migration)

These two records live in a *second* directory and are the one real `adr_number_unique` violation
(`adr-013` clashes with `docs/decisions/ADR-013`). They are slated to move into `docs/decisions/` at
fresh next-free numbers (**≥ 047**) via `git mv` under **SGO-176**; aliases are withheld until then.

| Current path | Status | Title | Follow-up |
|---|---|---|---|
| `docs/architecture/adr-012-repo-consolidation.md` | Proposed | Repository consolidation & layered architecture | → `git mv` to `docs/decisions/ADR-047` (SGO-176) |
| `docs/architecture/adr-013-infrastructure-scaling-triggers.md` | Accepted | Infrastructure scaling triggers — reverse proxy + second VM | → **collides** with `docs/decisions/ADR-013`; `git mv` to `docs/decisions/ADR-048` (SGO-176) |

## kairix — `docs/architecture/` (alias `KAI-ADR-###`)

| Alias | Home path | Status | Title / notes |
|---|---|---|---|
| KAI-ADR-017 | `docs/architecture/ADR-017-deployment-architecture.md` | Active | Deployment architecture |
| KAI-ADR-018 | `docs/architecture/ADR-018-dlt-connector-framework.md` | Superseded | Adopt dlt as the connector ingestion framework · superseded by outcome — Wave 1 shipped, Waves 2–3 abandoned; dlt thesis rejected |
| KAI-ADR-019 | `docs/architecture/ADR-019-compose-resource-governance.md` | Accepted | Resource governance at the docker-compose layer |
| KAI-ADR-020 | `docs/architecture/ADR-020-connector-tick-budget-watermark.md` | Accepted | Connector tick budget + disk-watermark gating |
| KAI-ADR-021 | `docs/architecture/ADR-021-per-source-metadata-normalisation.md` | Accepted | Per-source metadata normalisation |
| KAI-ADR-022 | `docs/architecture/ADR-022-container-secret-readiness-gate.md` | Accepted | Container-level secret readiness gate · replaces `kairix.service` unit |
| KAI-ADR-023 | `docs/architecture/ADR-023-vector-index-write-architecture.md` | Accepted | Vector index write architecture (post-#335) |
| KAI-ADR-024 | `docs/architecture/ADR-024-test-pyramid-redesign.md` | Accepted | Test pyramid redesign — coverage % → defect-class coverage |
| KAI-ADR-025 | `docs/architecture/ADR-025-pipeline-observability-and-status-surface.md` | Accepted | Pipeline observability + agent-actionable status surface (Phase 1A validated) |
| KAI-ADR-026 | `docs/architecture/ADR-026-cross-cutting-primitive-abstractions.md` | Proposed | Cross-cutting primitive abstractions |
| KAI-ADR-027 | `docs/architecture/ADR-027-entity-enrichment-worker-stage.md` | Accepted | Entity-enrichment worker stage |
| KAI-ADR-028 | `docs/architecture/ADR-028-per-type-chunking-and-evaluation.md` | Accepted | Per-type chunking strategy + chunking quality evaluation |
| KAI-ADR-029 | `docs/architecture/ADR-029-agent-query-queue-and-carry-along-delivery.md` | Accepted | Agent-facing query queue + carry-along delivery · intends to supersede the ColdStart pattern (not yet effected) |
| KAI-ADR-030 | `docs/architecture/ADR-030-embedding-cache-and-atomic-save.md` | — | Persistent embedding cache + atomic vec-index save |
| KAI-ADR-031 | `docs/architecture/ADR-031-canonical-credential-naming.md` | — | Canonical credential naming + SecretsLoader |
| KAI-ADR-032 | `docs/architecture/ADR-032-oauth2-connect-flow.md` | Accepted | `kairix connect <service>` OAuth2 flow |
| KAI-ADR-035 | `docs/architecture/ADR-035-search-pipeline-async-assessment.md` | Investigation | SearchPipeline async-ification assessment |
| KAI-ADR-036 | `docs/architecture/ADR-036-entity-summary-indexing-surface.md` | Proposed | Entity-summary indexing surface |

*Numbers 033–034 are unused in this series; the record_dir series starts at 017.*

## kata — `docs/adr/` (alias `KATA-ADR-###`)

| Alias | Home path | Status | Title / notes |
|---|---|---|---|
| KATA-ADR-001 | `docs/adr/ADR-001-portable-core-and-host-adapters.md` | Accepted | Portable CORE + per-host adapters |
| KATA-ADR-002 | `docs/adr/ADR-002-mcp-as-portable-tool-spine.md` | Accepted | MCP is the portable tool spine |
| KATA-ADR-003 | `docs/adr/ADR-003-bok-default-binding.md` | Accepted | BOK default binding = NDD .NET MCP / Foundry KS; kairix fork |
| KATA-ADR-004 | `docs/adr/ADR-004-capability-spec-not-skill-md.md` | Accepted | The CORE primitive is a capability spec, not a SKILL.md |
| KATA-ADR-005 | `docs/adr/ADR-005-provenance-and-ip-boundary.md` | Accepted | Provenance & IP boundary |
| KATA-ADR-006 | `docs/adr/ADR-006-bok-ingestion-and-licensing.md` | Accepted | BOK ingestion & licensing policy |
| KATA-ADR-007 | `docs/adr/ADR-007-canonical-artefact-catalogue.md` | Accepted | Canonical artefact catalogue & operational/backlog status |
| KATA-ADR-008 | `docs/adr/ADR-008-operating-model-roles-and-services.md` | Accepted | Operating-model role & deployable-service registries |
| KATA-ADR-009 | `docs/adr/ADR-009-sfia-capability-package-baseline.md` | Accepted | SFIA-9 + Capability-Package pattern as the skills baseline |
| KATA-ADR-010 | `docs/adr/ADR-010-focus-internal-delivery-and-universal-eval.md` | Accepted | Focus — internal agent-human delivery layer + universal eval |
| KATA-ADR-011 | `docs/adr/ADR-011-kata-as-detachable-apac-delivery-bundle.md` | Accepted | kata as a detachable APAC-delivery bundle |
| KATA-ADR-012 | `docs/adr/ADR-012-quality-gate-convergence-on-three-cubes-fitness.md` | Accepted | Converge kata's quality gate onto the three-cubes fitness engine · aligns with `QG-CONVERGE` |
| KATA-ADR-013 | `docs/adr/ADR-013-autonomous-gate-enforced-merge-governance.md` | Accepted | Autonomous, gate-enforced merge governance + dedicated agent identity · reconcile against `STD-MERGE` via amendment banner (follow-up) |

## Collisions & follow-ups (owned elsewhere)

This index is **additive**; the fixes below are tracked on their own issues and are out of scope for
these new `governance/decisions/` files:

1. **tc-agent-zone `ADR-013` cross-directory collision** — `docs/decisions/ADR-013` vs
   `docs/architecture/adr-013`. Remediation: SGO-179 hardens `adr_number_unique` (list of
   `record_dirs` + case-insensitive prefix, with a regression fixture from this collision); SGO-176
   `git mv`s both `docs/architecture` ADRs into `docs/decisions/` at fresh numbers **≥ 047** and
   sweeps inbound references. Aliases `TAZ-ADR-047/048` are reserved for them.
2. **kata `ADR-013` vs `STD-MERGE`** — kata's merge-governance ADR overlaps the org `STD-MERGE`
   decision; reconcile via a superseded-by/amendment banner on the kata ADR pointing at the
   tc-pipelines `STD-MERGE` record (SGO-176).
3. **Per-repo `INDEX.md` walkers** — SGO-179 adds a small generator so each repo emits its own slice
   of this table mechanically, keeping the hand-maintained view honest.
