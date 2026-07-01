#!/usr/bin/env bash
# preflight.sh — run the full quality sweep before any push or Docker rebuild.
# Repo-agnostic. Exits non-zero on any failure.
#
# Per the Run-Preflight-Before-Rebuild standard: never push or rebuild an image
# over an unverified tree. This runs the SAST/code-smell sweep + the two
# canonical gates (pre-commit hygiene + the tc-fitness catalogue) so a rebuild
# can't ship a red tree. It hardcodes no repo paths or modules: the fitness
# catalogue lives in tc-fitness, the hygiene set in .pre-commit-config.yaml,
# and SAST tools are auto-detected (skipped-with-a-note when absent).
#
# Usage:
#   bash scripts/preflight.sh          # full sweep
#   bash scripts/preflight.sh --quick  # skip the slow SAST leg (gitleaks/semgrep)
#   bash scripts/preflight.sh --help

set -euo pipefail

QUICK=0
case "${1:-}" in
    --quick) QUICK=1 ;;
    -h|--help)
        grep '^#' "$0" | sed 's/^# \{0,1\}//'
        exit 0 ;;
    "" ) ;;
    * ) echo "fix: unknown flag '$1' (see --help)" >&2; exit 2 ;;
esac

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; exit 1; }

echo "preflight: running the pre-push / pre-rebuild sweep"

# 1. SAST / secret scan / code-smell (free-first: gitleaks + Semgrep OSS).
#    Auto-detected; a missing tool warns rather than fails so the sweep still
#    runs on a fresh machine. detect-secrets runs inside pre-commit below.
if [[ "$QUICK" == "0" ]]; then
    if command -v gitleaks >/dev/null 2>&1; then
        if gitleaks detect --no-banner --redact >/dev/null 2>&1; then
            pass "gitleaks (no leaks)"
        else
            fail "gitleaks — secret detected; run: gitleaks detect --no-banner --redact"
        fi
    else
        warn "gitleaks not installed — skipped (install: brew install gitleaks)"
    fi

    if command -v semgrep >/dev/null 2>&1; then
        if semgrep --error --quiet --config=auto . >/dev/null 2>&1; then
            pass "semgrep (no findings)"
        else
            fail "semgrep — findings; run: semgrep --config=auto ."
        fi
    else
        warn "semgrep not installed — skipped (install: pipx install semgrep)"
    fi
else
    warn "--quick: skipping the SAST leg (gitleaks/semgrep)"
fi

# 2. Pre-commit hygiene gate (what CI runs): lint, format, actionlint,
#    shell-lint, detect-secrets, no-attribution strip.
if uv run pre-commit run --all-files; then
    pass "pre-commit (hygiene)"
else
    fail "pre-commit — run: uv run pre-commit run --all-files"
fi

# 3. The fitness catalogue (what CI runs): typing, honest coverage, mutation,
#    architecture. `make check == CI` by construction.
if uv run tc-fitness run; then
    pass "tc-fitness (catalogue)"
else
    fail "tc-fitness — run: uv run tc-fitness run"
fi

echo ""
echo -e "${GREEN}preflight: all gates passed — safe to push / rebuild${NC}"
