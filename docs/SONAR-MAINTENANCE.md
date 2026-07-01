# Sonar maintenance — new-code reset and hotspot triage

**What this is:** two shared, reusable workflows that keep a repo's SonarCloud
project healthy without hand-typing changes into the Sonar web UI:

| Reusable workflow | What it does | When to run |
|---|---|---|
| [`sonar-new-code-reset.yml`](../.github/workflows/sonar-new-code-reset.yml) | Resets a project/branch's **New Code period** (default `PREVIOUS_VERSION`) so pre-existing debt stops being counted as "new". | Rarely — a manual one-off when the New Code window has drifted. |
| [`sonar-triage.yml`](../.github/workflows/sonar-triage.yml) | Marks each security **hotspot** whose location matches a repo-owned rationale as **Reviewed / SAFE**, recording the rationale in Sonar's audit trail. | **Weekly**, on a schedule, so hotspots don't accrue silently. |

**Why they exist:** both actions are Sonar **web-API-or-UI-only** operations —
they are not scanner properties you can set in `sonar-project.properties`. Doing
them by hand is tedious and the audit trail is hand-typed. These workflows do
them from a reviewed driver so the decision lives in git history, and one fix in
CORE improves every repo at once.

**What is shared vs. what each repo owns:** the CORE drivers
(`tools/sonar/reset_new_code.py`, `tools/sonar/triage_hotspots.py`) hold only the
Bearer-token transport and the read/set/read (or list/resolve/acknowledge) loop.
The **decisions** — your project key, and which `(rule, file)` hotspots you
accept and *why* — stay in your repo. The triage driver fails closed: any hotspot
with no rationale is left visible and the run exits non-zero.

---

## 1. Weekly hotspot triage (the scheduled cadence)

A reusable workflow **cannot schedule itself** in your repo — GitHub only fires
`on: schedule` from the repo that owns the workflow file. So wire the cadence in
a thin caller in **your** repo. Recommended cron: **`0 8 * * 1`** (Mondays 08:00
UTC) — early in the work week so a person can look at any failure the same week.

`.github/workflows/sonar-triage.yml` (in your repo):

```yaml
name: sonar-triage
on:
  schedule:
    - cron: "0 8 * * 1"   # Mondays 08:00 UTC — weekly, so hotspots don't accrue
  workflow_dispatch: {}    # plus on-demand
permissions:
  contents: read
jobs:
  triage:
    uses: three-cubes/tc-pipelines/.github/workflows/sonar-triage.yml@v1
    with:
      project-key: three-cubes_kairix
      rationales-path: .sonar/hotspot-rationales.json
      driver-ref: v1
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

Pin the caller to `@v1` and set `driver-ref: v1` to match, so the driver version
tracks the workflow version.

### The repo-owned rationale file

`rationales-path` points at a JSON file **in your repo**. It is a list of
entries; `line` is optional and, when present, wins over a whole-file entry for
the same rule:

```json
[
  {
    "rule": "python:S5852",
    "path": "kairix/core/temporal/chunker.py",
    "rationale": "Bounded input — chunk size is capped; no untrusted path. Reviewed and accepted."
  },
  {
    "rule": "docker:S6471",
    "path": "Dockerfile",
    "line": 28,
    "rationale": "Runtime stays root because host bind-mount ownership varies. Reviewed and accepted."
  }
]
```

Each entry must say **why** the hotspot is a false positive (or why the risk is
accepted). A hotspot that matches no entry is left `TO_REVIEW` and turns the run
red — that is the signal to review it and add a rationale (or fix the code).

---

## 2. New-code baseline reset (manual, one-off)

Run this only when the New Code window has drifted and the Quality Gate is
failing on old debt. Wire it behind `workflow_dispatch` — never on push.

`.github/workflows/sonar-new-code-reset.yml` (in your repo):

```yaml
name: sonar-new-code-reset
on:
  workflow_dispatch: {}
permissions:
  contents: read
jobs:
  reset:
    uses: three-cubes/tc-pipelines/.github/workflows/sonar-new-code-reset.yml@v1
    with:
      project-key: three-cubes_kairix
      branch: main
      new-code-type: PREVIOUS_VERSION
      driver-ref: v1
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

`SONAR_TOKEN` must have **Administer** permission on the project to set the New
Code period. The driver is idempotent: it prints the period before and after, so
a re-run on an already-reset project is a harmless no-op.

---

## Notes

- **Secrets stay in the caller.** The reusable workflows never hard-code a token;
  the caller passes `SONAR_TOKEN` via `secrets:`.
- **Run the drivers locally** from a tc-pipelines checkout, e.g.
  `SONAR_TOKEN=xxx python3 tools/sonar/reset_new_code.py --project-key <key>`.
  They use only the Python standard library — no install needed.
