#!/usr/bin/env bash
#
# validate-local.sh — Local validation suite (replaces GitHub Actions CI)
#
# Runs available linting and validation tools. Skips tools that are not
# installed with actionable messages. Exit code 0 unless --fail-fast is set
# and a check fails, or if a required check fails.
#
# Usage:
#   bash scripts/validate-local.sh              # Run all checks, report summary
#   bash scripts/validate-local.sh --fail-fast   # Exit non-zero on first failure
#   bash scripts/validate-local.sh --help        # Show usage
#

set -uo pipefail

# ── Colours (disabled if stdout is not a tty) ────────────────────────────────
if [ -t 1 ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi

FAIL_FAST=false

# Counters
PASS=0
FAIL=0
SKIP=0
TOTAL=0

# ── Helpers ───────────────────────────────────────────────────────────────────

usage() {
  cat <<'EOF'
Usage: validate-local.sh [OPTIONS]

Run local validation checks that replace the removed GitHub Actions CI pipeline.

Options:
  --fail-fast   Stop on first failed check and exit non-zero
  --help        Show this help message

Checks run (if the tool is available):
  1. yamllint          — YAML syntax & style validation
  2. pre-commit        — Pre-commit hooks (check-json, check-merge-conflict, check-symlinks)
  3. ansible-lint      — Ansible best practices & anti-patterns
  4. shellcheck        — Shell script static analysis
  5. version-matrix    — Version compatibility validation
  6. python-tests      — Unit, component, and E2E tests (pytest)
EOF
}

log_pass()  { echo -e "  ${GREEN}✓${NC} $1"; }
log_fail()  { echo -e "  ${RED}✗${NC} $1"; }
log_skip()  { echo -e "  ${YELLOW}⊘${NC} $1"; }

check_tool() {
  local tool_name="$1"
  local tool_cmd="$2"
  if command -v "$tool_cmd" &>/dev/null; then
    return 0
  fi
  log_skip "$tool_name not found ($tool_cmd). Install it to enable this check."
  ((SKIP++)) || true
  ((TOTAL++)) || true
  return 1
}

run_check() {
  local name="$1"
  shift
  ((TOTAL++)) || true

  if ! "$@"; then
    ((FAIL++)) || true
    log_fail "$name"
    if [ "$FAIL_FAST" = true ]; then
      echo ""
      echo -e "${RED}--fail-fast: stopping at first failure${NC}"
      exit 1
    fi
    return 1
  fi
  ((PASS++)) || true
  log_pass "$name"
  return 0
}

# ── Parse args ────────────────────────────────────────────────────────────────

while [ $# -gt 0 ]; do
  case "$1" in
    --fail-fast) FAIL_FAST=true ;;
    --help)      usage; exit 0 ;;
    *)           echo "Unknown option: $1"; usage; exit 1 ;;
  esac
  shift
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Local Validation Suite (replaces GitHub Actions CI)   ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. yamllint ───────────────────────────────────────────────────────────────

echo -e "${BLUE}[1/6] yamllint${NC} — YAML syntax & style validation"
if check_tool "yamllint" "yamllint"; then
  if [ -f .yamllint.yaml ]; then
    run_check "yamllint" yamllint -c .yamllint.yaml . 2>&1 | tail -5
  else
    run_check "yamllint" yamllint . 2>&1 | tail -5
  fi
fi
echo ""

# ── 2. pre-commit ─────────────────────────────────────────────────────────────

echo -e "${BLUE}[2/6] pre-commit${NC} — Pre-commit hooks"
if check_tool "pre-commit" "pre-commit"; then
  if [ -f .pre-commit-config.yaml ]; then
    run_check "pre-commit" pre-commit run --all-files --hook-stage manual 2>&1 | tail -10
  else
    log_skip "pre-commit: no .pre-commit-config.yaml found"
    ((SKIP++)) || true
    ((TOTAL++)) || true
  fi
fi
echo ""

# ── 3. ansible-lint ───────────────────────────────────────────────────────────

echo -e "${BLUE}[3/6] ansible-lint${NC} — Ansible best practices"
if check_tool "ansible-lint" "ansible-lint"; then
  if [ -f .ansible-lint.yml ]; then
    run_check "ansible-lint" ansible-lint -c .ansible-lint.yml 2>&1 | tail -10
  else
    run_check "ansible-lint" ansible-lint 2>&1 | tail -10
  fi
fi
echo ""

# ── 4. shellcheck ─────────────────────────────────────────────────────────────

echo -e "${BLUE}[4/6] shellcheck${NC} — Shell script static analysis"
if check_tool "shellcheck" "shellcheck"; then
  sh_files=""
  sh_files=$(find . -name '*.sh' -not -path './.git/*' -not -path './.venv/*' -not -path './node_modules/*' 2>/dev/null || true)
  if [ -n "$sh_files" ]; then
    run_check "shellcheck" sh -c "echo '$sh_files' | xargs shellcheck" 2>&1 | tail -10
  else
    log_skip "shellcheck: no .sh files found"
    ((SKIP++)) || true
    ((TOTAL++)) || true
  fi
fi
echo ""

# ── 5. version-matrix ────────────────────────────────────────────────────────

echo -e "${BLUE}[5/6] version-matrix${NC} — Version compatibility validation"
if [ -f scripts/verify-version-matrix.py ]; then
  if command -v python3 &>/dev/null; then
    run_check "version-matrix" python3 scripts/verify-version-matrix.py 2>&1 | tail -10
  else
    log_skip "version-matrix: python3 not found. Install Python 3 to enable this check."
    ((SKIP++)) || true
    ((TOTAL++)) || true
  fi
else
  log_skip "version-matrix: scripts/verify-version-matrix.py not found"
  ((SKIP++)) || true
  ((TOTAL++)) || true
fi
echo ""

# ── 6. python-tests (pytest) ─────────────────────────────────────────────────

echo -e "${BLUE}[6/6] python-tests${NC} — Unit, component, and E2E tests"
if [ -d tests ] && [ "$(find tests -name 'test_*.py' 2>/dev/null | head -1)" != "" ]; then
  if command -v pytest &>/dev/null; then
    run_check "pytest" pytest tests/ -v --no-header --tb=short 2>&1 | tail -20
  elif command -v python3 &>/dev/null; then
    run_check "pytest" python3 -m pytest tests/ -v --no-header --tb=short 2>&1 | tail -20
  else
    log_skip "pytest: not found. Install it (pip install pytest) to run Python tests."
    ((SKIP++)) || true
    ((TOTAL++)) || true
  fi
else
  log_skip "python-tests: no tests/ directory or test files found"
  ((SKIP++)) || true
  ((TOTAL++)) || true
fi
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────

echo -e "${BLUE}══════════════════════════════════════════════════════════${NC}"
echo -e "  Summary: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped out of ${TOTAL} checks"
echo -e "${BLUE}══════════════════════════════════════════════════════════${NC}"

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo -e "${RED}Validation FAILED — ${FAIL} check(s) did not pass.${NC}"
  exit 1
fi

if [ "$SKIP" -gt 0 ]; then
  echo ""
  echo -e "${YELLOW}Validation PASSED with ${SKIP} skipped check(s).${NC}"
  echo "  Install missing tools for complete coverage:"
  echo "    pip install yamllint pre-commit ansible-lint pytest"
  echo "    shellcheck: https://www.shellcheck.net/"
fi

echo ""
echo -e "${GREEN}All available checks passed.${NC}"
exit 0
