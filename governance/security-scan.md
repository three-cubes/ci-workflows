# Security scan — reusable workflow + allowlist/baseline convention

`tc-pipelines/.github/workflows/security-scan.yml` is the org's reusable
(`workflow_call`) security scan (SGO-186). It runs three independently
toggleable legs and is designed to be **adoptable without a false-positive
flood**:

| Leg | Tool | Cost model | Notes |
| --- | --- | --- | --- |
| `run-gitleaks` | gitleaks **binary** in the free `zricethezav/gitleaks` Docker image | Free on private repos too (the org-licensed *gitleaks-action* is not) | Full-history scan on push/schedule; redacted **PR-diff** scan on `pull_request`. Honours `.gitleaks.toml` + a committed baseline. |
| `run-semgrep` | `semgrep ci --oss-only` | Free — **no `SEMGREP_APP_TOKEN`** | `p/default p/python p/github-actions p/secrets`. Blocking exit on findings; emits SARIF. |
| `run-codeql` | `github/codeql-action` init+analyze | Code-scanning SARIF upload free on **public** repos only | Gated on the public-repo signal (default from `github.event.repository.private == false`). |

All three default to `true`. Every leg's shell body is injection-safe (event
data is env-bound, never interpolated) and every third-party action is
SHA-pinned with a `# vX.Y.Z` comment.

## Adopting it in a consumer repo

Add a thin caller (the caller owns the `on:` triggers):

```yaml
# .github/workflows/security-scan.yml (consumer)
name: security-scan
on:
  pull_request:
  push:
    branches: [main]
  schedule:
    - cron: "0 6 * * 1"   # weekly full-history sweep
permissions:
  contents: read
  security-events: write  # for code-scanning SARIF upload (public repos)
  actions: read           # for CodeQL
jobs:
  security:
    uses: three-cubes/tc-pipelines/.github/workflows/security-scan.yml@v1
    with:
      codeql-languages: '["python", "javascript"]'
```

Private repos: leave `run-codeql` on (it self-skips off the public-repo
signal) — gitleaks + Semgrep still run and hand back SARIF as a build
artifact.

## The allowlist / baseline convention

Two committed files keep the scan quiet on known-safe content:

1. **`.gitleaks.toml`** (repo root) — copy from
   [`governance/.gitleaks.toml`](.gitleaks.toml). It extends gitleaks'
   defaults and adds a **path-pattern allowlist** for the file classes that
   are safe to skip by policy: `tests/` · `fixtures/` · `node_modules/` ·
   lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `uv.lock`,
   …). This is the CI promotion of tc-agent-zone's proven
   `policy/security/vm-secret-allowlist.yml` model. Keep entries **narrow** —
   allowlist a file *class*, not a broad directory.

2. **A committed baseline** — copy the empty
   [`governance/gitleaks-baseline.template.json`](gitleaks-baseline.template.json)
   to `.gitleaks-baseline.json` at the repo root. Individual *accepted*
   findings (a reviewed fixture secret, a redacted historical hit) go here as
   redacted fingerprints — **not** in `.gitleaks.toml`. Regenerate with
   `gitleaks detect --report-path .gitleaks-baseline.json` after triage. A
   finding in the baseline no longer blocks; a genuinely new secret still
   does.

Path allowlist → whole file *classes*. Baseline → individual triaged
findings. Use the right one so a single accepted finding never widens the
allowlist for an entire directory.

## Runtime devsecops (out of scope here)

The tc-agent-zone hourly-VM gitleaks sweep and the per-agent `/run/secrets`
isolation on vm-openclaw are **runtime** controls on the VM, not CI. They are
referenced by, but not rebuilt in, this reusable.
