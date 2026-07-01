#!/usr/bin/env bash
# bootstrap-repo-governance.sh — one-command governance onboarding for a
# three-cubes repo.
#
# Given a repo, this sets the standard repo variables, wires the standard
# secrets from Azure Key Vault, applies the canonical branch ruleset (the
# contract: required checks Quality gate + SonarCloud scan + SonarCloud Code
# Analysis, 1 review, code-owner review, thread resolution), installs the
# pre-commit hook config + CODEOWNERS + dependabot.yml from the template repo,
# distributes the agent-affordance + harness payload (rendered skeletons +
# sonar-sqaa hook + safe-commit/preflight), and prints a verification summary.
#
# Precedence note: GitHub resolves variables/secrets repo-over-org. The
# org-level AZURE_*/SONAR_TOKEN/CODECOV_TOKEN are the default; this script only
# sets a REPO-level override when it differs from the org value. For the common
# case the org inheritance is enough and this script writes no repo variables.
#
# Usage:
#   scripts/bootstrap-repo-governance.sh \
#     --repo three-cubes/<name> \
#     [--kv-name kv-tc-agents] \
#     [--no-secrets] [--no-ruleset] [--no-files] [--no-affordance] \
#     [--dry-run]
#
# Requires: gh (admin:org + repo + workflow scopes), az (logged in, read on the
# KV). No clone needed — templates (incl. the ruleset + skeletons) are fetched
# from three-cubes/tc-pipelines/governance/; pass --template-dir <clone> to use a
# local copy instead. When this script runs from inside a tc-pipelines checkout
# it sources governance/skeletons/ locally (no network) for the affordance render.

set -euo pipefail

# ── defaults ─────────────────────────────────────────────────────────────────
REPO=""
KV_NAME="kv-tc-agents"
TEMPLATE_REPO="three-cubes/tc-pipelines"
TEMPLATE_DIR=""
DO_SECRETS=1
DO_RULESET=1
DO_FILES=1
DO_AFFORDANCE=1
DRY_RUN=0

# Canonical-homes line the affordance skeletons carry ({{CANONICAL_HOMES}}).
# shellcheck disable=SC2016  # literal backticks are intentional (markdown code spans, no expansion)
CANONICAL_HOMES='`tc-fitness` (gate engine) · `tc-pipelines` (reusable CI + governance templates)'

# The rendered affordance skeleton set (source name -> repo-root target).
AFFORDANCE_SKELETONS=(CLAUDE.md AGENTS.md CONTRIBUTING.md ETHOS.md RESOLVER.md SCORECARD.md)

# Canonical repo variables every governed repo carries. Pulled from the org (so
# this is a no-op when org inheritance already covers them).
STANDARD_VARS=(AZURE_CLIENT_ID AZURE_SUBSCRIPTION_ID AZURE_TENANT_ID)

# Canonical secrets wired from KV: secret-name -> kv-secret-name.
declare -a SECRET_MAP=(
  "SONAR_TOKEN:sonarcloud-ci-analysis-token"
  "CODECOV_TOKEN:codecov-upload-token"
)

# ── arg parse ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --kv-name) KV_NAME="$2"; shift 2 ;;
    --template-dir) TEMPLATE_DIR="$2"; shift 2 ;;
    --no-secrets) DO_SECRETS=0; shift ;;
    --no-ruleset) DO_RULESET=0; shift ;;
    --no-files) DO_FILES=0; shift ;;
    --no-affordance) DO_AFFORDANCE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "fix: unknown flag '$1'" >&2
      echo "next: scripts/bootstrap-repo-governance.sh --help" >&2
      exit 2 ;;
  esac
done

if [[ -z "$REPO" ]]; then
  echo "fix: --repo three-cubes/<name> is required" >&2
  echo "next: scripts/bootstrap-repo-governance.sh --repo three-cubes/<name>" >&2
  exit 2
fi

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN  $*"
  else
    "$@"
  fi
}

# Locate governance/skeletons/ — prefer --template-dir, else the tc-pipelines
# checkout this script lives in (governance/scripts/../skeletons), else empty
# (affordance section then prints the gh-api fetch sequence instead).
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
SKELETON_DIR=""
if [[ -n "$TEMPLATE_DIR" && -d "${TEMPLATE_DIR}/governance/skeletons" ]]; then
  SKELETON_DIR="${TEMPLATE_DIR}/governance/skeletons"
elif [[ -d "${SCRIPT_DIR}/../skeletons" ]]; then
  SKELETON_DIR="$(cd -- "${SCRIPT_DIR}/../skeletons" && pwd)"
fi

# Render one skeleton to stdout: inline the banner INCLUDE, then resolve the two
# tokens from --repo. This is the bash twin of governance/skeletons/tests/
# test_render_skeletons.py — both do banner-inline + {{REPO}}/{{CANONICAL_HOMES}}
# substitution, so they can never drift.
render_skeleton() {
  local src="$1" banner="${SKELETON_DIR}/_canonical-standards-banner.md"
  awk -v banner="$banner" '
    /<!--[[:space:]]*INCLUDE:[[:space:]]*_canonical-standards-banner\.md[[:space:]]*-->/ {
      while ((getline line < banner) > 0) print line
      close(banner)
      next
    }
    { print }
  ' "${SKELETON_DIR}/${src}" \
    | sed -e "s|{{REPO}}|${REPO}|g" -e "s|{{CANONICAL_HOMES}}|${CANONICAL_HOMES}|g"
}

echo "== bootstrap-repo-governance for ${REPO} (dry-run=${DRY_RUN}) =="

# ── 1. repo variables (org-inheritance-aware) ───────────────────────────────
echo "-- variables --"
for v in "${STANDARD_VARS[@]}"; do
  org_val="$(gh api "orgs/three-cubes/actions/variables/${v}" --jq '.value' 2>/dev/null || true)"
  if [[ -n "$org_val" ]]; then
    echo "ok: ${v} inherited from org (repo override not written)"
  else
    echo "warn: ${v} not set org-level; set it manually or org-promote it" >&2
    echo "      next: gh variable set ${v} --org three-cubes --visibility all --body <value>" >&2
  fi
done

# ── 2. secrets from Key Vault ───────────────────────────────────────────────
if [[ "$DO_SECRETS" == "1" ]]; then
  echo "-- secrets (from ${KV_NAME}) --"
  for pair in "${SECRET_MAP[@]}"; do
    gh_name="${pair%%:*}"
    kv_name="${pair##*:}"
    if gh api "orgs/three-cubes/actions/secrets/${gh_name}" >/dev/null 2>&1; then
      echo "ok: ${gh_name} inherited from org (repo override not written)"
      continue
    fi
    val="$(az keyvault secret show --vault-name "${KV_NAME}" --name "${kv_name}" --query value -o tsv 2>/dev/null || true)"
    if [[ -z "$val" ]]; then
      echo "warn: KV secret '${kv_name}' not found in ${KV_NAME}; ${gh_name} NOT wired" >&2
      echo "      fix: create the KV secret (value from the owner / SonarCloud / Codecov), then re-run" >&2
      continue
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "DRY-RUN  gh secret set ${gh_name} --repo ${REPO} --body <redacted from ${kv_name}>"
    else
      printf '%s' "$val" | gh secret set "${gh_name}" --repo "${REPO}" --body -
      echo "ok: ${gh_name} wired from ${kv_name}"
    fi
  done
fi

# ── 3. canonical branch ruleset ─────────────────────────────────────────────
if [[ "$DO_RULESET" == "1" ]]; then
  echo "-- ruleset (main) --"
  ruleset_json=""
  cleanup_ruleset=""
  if [[ -n "$TEMPLATE_DIR" && -f "${TEMPLATE_DIR}/governance/rulesets/main.json" ]]; then
    ruleset_json="${TEMPLATE_DIR}/governance/rulesets/main.json"
  elif [[ -f ".github/rulesets/main.json" ]]; then
    ruleset_json=".github/rulesets/main.json"
  else
    ruleset_json="$(mktemp)"
    cleanup_ruleset="$ruleset_json"
    gh api "repos/${TEMPLATE_REPO}/contents/governance/rulesets/main.json" \
      --jq '.content' 2>/dev/null | base64 -d > "$ruleset_json" || true
    if [[ ! -s "$ruleset_json" ]]; then
      echo "warn: could not fetch governance/rulesets/main.json from ${TEMPLATE_REPO}" >&2
      echo "      fix: pass --template-dir <tc-pipelines clone>, or --no-ruleset to skip" >&2
      rm -f "$cleanup_ruleset"; ruleset_json=""; cleanup_ruleset=""
    fi
  fi
  if [[ -n "$ruleset_json" ]]; then
    existing="$(gh api "repos/${REPO}/rulesets" --jq '.[] | select(.name=="main" and .source_type=="Repository") | .id' 2>/dev/null || true)"
    if [[ -n "$existing" ]]; then
      run gh api "repos/${REPO}/rulesets/${existing}" --method DELETE
    fi
    run gh api "repos/${REPO}/rulesets" --method POST --input "$ruleset_json"
    echo "ok: applied main ruleset from ${TEMPLATE_REPO}/governance/rulesets/main.json"
    [[ -n "$cleanup_ruleset" ]] && rm -f "$cleanup_ruleset"
  fi
fi

# ── 4. governance files (CODEOWNERS, dependabot, pre-commit) ─────────────────
if [[ "$DO_FILES" == "1" ]]; then
  echo "-- governance files --"
  echo "note: file installation opens a PR — this script prints the fetch+commit"
  echo "      sequence rather than pushing (no live git from the bootstrap)."
  cat <<EOF
  run (in a clone of ${REPO}, on a branch):
    gh api repos/${TEMPLATE_REPO}/contents/governance/CODEOWNERS \\
      --jq '.content' | base64 -d > .github/CODEOWNERS
    gh api repos/${TEMPLATE_REPO}/contents/governance/dependabot.yml \\
      --jq '.content' | base64 -d > .github/dependabot.yml
    gh api repos/${TEMPLATE_REPO}/contents/governance/pre-commit-config.yaml \\
      --jq '.content' | base64 -d > .pre-commit-config.yaml
    uv run pre-commit install
    git add .github/CODEOWNERS .github/dependabot.yml .pre-commit-config.yaml
    git commit -m "chore(governance): bootstrap CODEOWNERS + dependabot + pre-commit"
    gh pr create --fill
EOF
fi

# ── 5. agent-affordance + harness payload ────────────────────────────────────
# Renders the skeleton set with {{REPO}}/{{CANONICAL_HOMES}} resolved from
# --repo, installs the sonar-sqaa hook + jq-merges the settings.json PostToolUse
# block (idempotent — a re-run neither duplicates the block nor rewrites an
# unchanged render), and ships scripts/safe-commit.sh + scripts/preflight.sh.
# Same no-live-git design as section 4: it renders/verifies locally and prints
# the commit sequence rather than pushing. Toggle off with --no-affordance.
if [[ "$DO_AFFORDANCE" == "1" ]]; then
  echo "-- affordance + harness payload --"
  echo "note: skeletons rendered locally for ${REPO}; installation opens a PR (no live git)."

  if [[ -n "$SKELETON_DIR" ]]; then
    for s in "${AFFORDANCE_SKELETONS[@]}"; do
      rendered="$(render_skeleton "$s")"
      if printf '%s' "$rendered" | grep -q '{{'; then
        echo "warn: ${s} still has unresolved placeholders after render" >&2
      else
        echo "ok: render ${s} -> ${s} (placeholders resolved from --repo)"
      fi
    done
    echo "  (rendered from ${SKELETON_DIR}; sed resolves the REPO and CANONICAL_HOMES tokens)"
  else
    echo "note: no local governance/skeletons/ — fetch each skeleton from ${TEMPLATE_REPO} and render:"
    for s in "${AFFORDANCE_SKELETONS[@]}"; do
      echo "  gh api repos/${TEMPLATE_REPO}/contents/governance/skeletons/${s} --jq '.content' | base64 -d \\"
      echo "    | sed -e 's|{{REPO}}|${REPO}|g' > ${s}   # then inline the _canonical-standards-banner.md INCLUDE"
    done
  fi

  cat <<EOF
  run (in a clone of ${REPO}, on a branch):
    # 1. the six rendered affordance docs (CLAUDE/AGENTS/CONTRIBUTING/ETHOS/RESOLVER/SCORECARD) at repo root
    # 2. the sonar-sqaa PostToolUse hook
    mkdir -p .claude/hooks/sonar-sqaa/build-scripts
    gh api repos/${TEMPLATE_REPO}/contents/governance/skeletons/hooks/sonar-sqaa/build-scripts/posttool-sqaa.sh \\
      --jq '.content' | base64 -d > .claude/hooks/sonar-sqaa/build-scripts/posttool-sqaa.sh
    chmod +x .claude/hooks/sonar-sqaa/build-scripts/posttool-sqaa.sh
    # 3. idempotent jq-merge of the PostToolUse block into .claude/settings.json
    #    (deep-merge + unique — re-running does NOT duplicate the block or append)
    gh api repos/${TEMPLATE_REPO}/contents/governance/skeletons/claude-settings.postToolUse.json \\
      --jq '.content' | base64 -d > /tmp/postToolUse.json
    [ -f .claude/settings.json ] || echo '{}' > .claude/settings.json
    jq -s '
      .[0] as \$cur | .[1] as \$blk
      | \$cur
      | .hooks.PostToolUse = ((\$cur.hooks.PostToolUse // []) + \$blk.hooks.PostToolUse | unique)
    ' .claude/settings.json /tmp/postToolUse.json > .claude/settings.json.next
    mv .claude/settings.json.next .claude/settings.json
    # 4. the harness scripts
    mkdir -p scripts
    gh api repos/${TEMPLATE_REPO}/contents/governance/scripts/safe-commit.sh \\
      --jq '.content' | base64 -d > scripts/safe-commit.sh
    gh api repos/${TEMPLATE_REPO}/contents/governance/scripts/preflight.sh \\
      --jq '.content' | base64 -d > scripts/preflight.sh
    chmod +x scripts/safe-commit.sh scripts/preflight.sh
    git add CLAUDE.md AGENTS.md CONTRIBUTING.md ETHOS.md RESOLVER.md SCORECARD.md \\
      .claude/settings.json .claude/hooks/sonar-sqaa scripts/safe-commit.sh scripts/preflight.sh
    git commit -m "chore(governance): bootstrap agent-affordance + harness payload"
    gh pr create --fill
EOF
fi

echo "== done. Verify: gh api repos/${REPO}/rulesets ; gh secret list --repo ${REPO} =="
