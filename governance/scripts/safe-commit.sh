#!/usr/bin/env bash
# safe-commit.sh — commit only if the canonical gate passes. Repo-agnostic.
#
# The gate is the Three Cubes inner-loop contract (STANDARDS.md §5): replay
# EXACTLY what CI runs —
#   uv run pre-commit run [--all-files]   (cheap hygiene: lint/format/actionlint/
#                                           shell-lint/secret-scan/no-attribution)
#   uv run tc-fitness run   [--staged]     (the fitness catalogue: typing, honest
#                                           coverage, mutation, architecture)
# — then commit only on green. `make check == CI` by construction because both
# read this repo's [tool.tc_fitness] block. There are NO repo-specific stages in
# this script: the catalogue lives in tc-fitness, the hygiene set in
# .pre-commit-config.yaml. Promote a new check into tc-fitness, never inline here.
#
# Usage:
#   bash scripts/safe-commit.sh "commit message"            # full gate (the merge bar)
#   bash scripts/safe-commit.sh --check "commit message"    # warm staged inner loop
#   bash scripts/safe-commit.sh --fast  "commit message"    # hygiene-only fast path
#   bash scripts/safe-commit.sh --pre-pr                    # verify-only full replay (no commit)
#   bash scripts/safe-commit.sh --help
#
# Modes:
#   (default)  uv run pre-commit run --all-files  +  uv run tc-fitness run        -> commit
#   --check    uv run pre-commit run (staged)     +  uv run tc-fitness run --staged -> commit
#              The warm, staged-scoped inner loop. Warm-daemon mypy (dmypy) is
#              OPTIONAL and repo-configured — wire it as a pre-commit hook if you
#              want it; this script does not hardcode any type checker.
#   --fast     uv run pre-commit run (staged)                                     -> commit
#              Hygiene-only, for commits that genuinely can't touch the product
#              surface (workflow YAML, docs, config tweaks). The full gate is
#              still the merge bar; --fast is just the iteration loop.
#   --pre-pr   uv run pre-commit run --all-files  +  uv run tc-fitness run        -> VERIFY ONLY
#              The pre-push confirmation. Commits nothing, needs nothing staged.
#              Run it after the normal gate has committed, before you push.
#
# Escape hatch: SAFE_COMMIT_SKIP_COVERAGE=1 appends `--skip-coverage` to the
# tc-fitness invocation (the neutral generalisation of the old per-repo
# KAIRIX_SKIP_COVERAGE). Do NOT push a commit whose gate only passed because
# coverage was skipped — CI still enforces it.

set -euo pipefail

# ── mode parse ───────────────────────────────────────────────────────────────
FAST_MODE=0
CHECK_MODE=0
PRE_PR_MODE=0
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --fast) FAST_MODE=1 ;;
        --check) CHECK_MODE=1 ;;
        --pre-pr) PRE_PR_MODE=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) ARGS+=("$arg") ;;
    esac
done
set -- "${ARGS[@]:+${ARGS[@]}}"

# --pre-pr is verify-only and needs no message; every other mode needs one.
if [[ "$PRE_PR_MODE" != "1" && $# -lt 1 ]]; then
    echo "fix: pass a commit message: bash scripts/safe-commit.sh [--fast | --check | --pre-pr] \"message\"" >&2
    exit 1
fi
MESSAGE="${1:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# ── #483 guard: command substitution under `set -e` ──────────────────────────
# `VAR=$(cmd)` kills the whole script AT THE ASSIGNMENT when cmd exits non-zero
# — no FAIL line, no captured output. Every gate that captures output MUST run
# through run_gate so the exit code lands in GATE_RC for explicit handling
# instead of tripping `set -e`.
GATE_OUT=""
GATE_RC=0
run_gate() {
    GATE_RC=0
    GATE_OUT=$("$@" 2>&1) || GATE_RC=$?
}

# Named-stage death report. Prints the tail of the captured output, a FAIL line
# carrying the stage name + exit code, and fix:/next: markers, then exits.
gate_died() {
    local stage="$1" rc="$2" rerun="$3"
    echo -e "${RED}FAIL${NC} (${stage} stage died, rc=${rc})"
    echo "----- last 50 lines of ${stage} output -----"
    echo "$GATE_OUT" | tail -50
    echo "----- end ${stage} output -----"
    echo "fix: read the ${stage} tail above — the stage exited before producing a verdict (crash, collection error, coverage floor, or a concurrent run colliding on shared artifacts)."
    echo "next: re-run the stage standalone: ${rerun}"
    exit "$rc"
}

# Neutral coverage escape hatch (generalises KAIRIX_SKIP_COVERAGE).
FITNESS_EXTRA=()
if [[ "${SAFE_COMMIT_SKIP_COVERAGE:-0}" == "1" ]]; then
    FITNESS_EXTRA+=(--skip-coverage)
fi

# ── shared stage runners ─────────────────────────────────────────────────────
run_precommit() {
    # $1: "all" -> --all-files ; "staged" -> default (staged files only)
    local scope="$1" label="$2"
    echo -n "  pre-commit (${label})... "
    if [[ "$scope" == "all" ]]; then
        run_gate uv run pre-commit run --all-files
    else
        run_gate uv run pre-commit run
    fi
    if [[ "$GATE_RC" -ne 0 ]]; then
        echo -e "${RED}FAIL${NC}"
        echo "$GATE_OUT" | tail -40
        echo "fix: the failing hooks are listed above (lint/format/secret-scan/etc.)."
        echo "next: uv run pre-commit run --all-files"
        exit 1
    fi
    echo -e "${GREEN}OK${NC}"
}

run_fitness() {
    # $1: "full" -> uv run tc-fitness run ; "staged" -> ... run --staged
    local scope="$1" label="$2"
    local cmd=(uv run tc-fitness run)
    [[ "$scope" == "staged" ]] && cmd+=(--staged)
    [[ "${#FITNESS_EXTRA[@]}" -gt 0 ]] && cmd+=("${FITNESS_EXTRA[@]}")
    echo -n "  tc-fitness (${label})... "
    run_gate "${cmd[@]}"
    if [[ "$GATE_RC" -ne 0 ]]; then
        echo -e "${RED}FAIL${NC}"
        echo "$GATE_OUT" | tail -40
        echo "fix: the failing fitness rules are listed above; each FAIL line names its fix."
        echo "next: re-run standalone: ${cmd[*]}"
        exit 1
    fi
    echo -e "${GREEN}OK${NC}"
}

# ── --pre-pr: verify-only full replay (runs BEFORE the staged guard) ─────────
if [[ "$PRE_PR_MODE" == "1" ]]; then
    echo "=== Pre-PR gate (--pre-pr — verify-only full replay of pre-commit + tc-fitness) ==="
    run_precommit all "all-files"
    run_fitness full "full catalogue"
    echo ""
    echo -e "${GREEN}--pre-pr complete: the full gate is green. Safe to push / open a PR.${NC}"
    exit 0
fi

# ── empty-stage guard ────────────────────────────────────────────────────────
# safe-commit.sh does not auto-stage; running it without `git add` produced
# silent no-op "commits" that masked real failures. Fail loud here.
if git diff --cached --quiet; then
    echo -e "${RED}FAIL${NC}: nothing staged for commit"
    echo "fix: stage files with 'git add <paths>' before running safe-commit.sh"
    echo "next: 'git status' to see what's modified but not yet staged"
    exit 1
fi

# ── --fast: hygiene-only fast path ───────────────────────────────────────────
if [[ "$FAST_MODE" == "1" ]]; then
    echo "=== Fast gate (--fast — pre-commit on staged files only; the full gate remains the merge bar) ==="
    run_precommit staged "staged"
    echo ""
    echo -e "${GREEN}--fast complete. The full gate remains the merge bar. Committing.${NC}"
    git commit -m "$MESSAGE"
    exit $?
fi

# ── --check: warm staged inner loop ──────────────────────────────────────────
if [[ "$CHECK_MODE" == "1" ]]; then
    echo "=== Check gate (--check — staged pre-commit + staged tc-fitness, warm inner loop) ==="
    run_precommit staged "staged"
    run_fitness staged "staged scope"
    echo ""
    echo -e "${GREEN}--check complete (warm inner loop). The full gate remains the merge bar. Committing.${NC}"
    git commit -m "$MESSAGE"
    exit $?
fi

# ── default: the full gate (the merge bar) ───────────────────────────────────
echo "=== Quality gate (full — the merge bar) ==="
run_precommit all "all-files"
run_fitness full "full catalogue"
echo ""
echo -e "${GREEN}All gates passed. Committing.${NC}"
git commit -m "$MESSAGE"
