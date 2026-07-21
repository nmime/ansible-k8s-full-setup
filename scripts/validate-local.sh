#!/usr/bin/env bash
#
# validate-local.sh — Local validation suite used by developers and CI
#
# Every declared check is mandatory. Missing tooling is a failure so the suite
# cannot report success without having executed the advertised validation.
#
# Usage:
#   bash scripts/validate-local.sh              # Run all checks, report summary
#   bash scripts/validate-local.sh --fail-fast   # Exit non-zero on first failure
#   bash scripts/validate-local.sh --help        # Show usage
#

set -uo pipefail

# ── Colours (disabled if stdout is not a tty) ────────────────────────────────
if [ -t 1 ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
else
  RED=''; GREEN=''; BLUE=''; NC=''
fi

FAIL_FAST=false

# Counters
PASS=0
FAIL=0
TOTAL=0

# ── Helpers ───────────────────────────────────────────────────────────────────

usage() {
  cat <<'EOF'
Usage: validate-local.sh [OPTIONS]

Run the same mandatory validation checks used by GitHub Actions.

Options:
  --fail-fast   Stop on first failed check and exit non-zero
  --help        Show this help message

Checks run:
  1. yamllint          — YAML syntax & style validation
  2. pre-commit        — Pre-commit hooks (check-json, check-merge-conflict, check-symlinks)
  3. ansible-lint      — Ansible best practices & anti-patterns
  4. shellcheck        — Shell script static analysis
  5. version-matrix    — Version compatibility validation
  6. ansible-syntax    — Parse deployment and removal entry-point playbooks
  7. python-tests      — Unit and static component-contract tests (pytest)
EOF
}

log_pass()  { echo -e "  ${GREEN}✓${NC} $1"; }
log_fail()  { echo -e "  ${RED}✗${NC} $1"; }
check_tool() {
  local tool_name="$1"
  local tool_cmd="$2"
  if command -v "$tool_cmd" &>/dev/null; then
    return 0
  fi
  log_fail "$tool_name not found ($tool_cmd). Install the required tool."
  ((FAIL++)) || true
  ((TOTAL++)) || true
  return 1
}

run_check() {
  local name="$1"
  shift
  local output_file
  output_file="$(mktemp)"
  ((TOTAL++)) || true

  if ! "$@" >"$output_file" 2>&1; then
    ((FAIL++)) || true
    log_fail "$name"
    tail -20 "$output_file"
    rm -f "$output_file"
    if [ "$FAIL_FAST" = true ]; then
      echo ""
      echo -e "${RED}--fail-fast: stopping at first failure${NC}"
      exit 1
    fi
    return 1
  fi
  rm -f "$output_file"
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
cd "$REPO_ROOT" || exit 1
if [ -d "$REPO_ROOT/.venv/bin" ]; then
  PATH="$REPO_ROOT/.venv/bin:$PATH"
  export PATH
fi

echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       Local and CI Validation Suite                    ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. yamllint ───────────────────────────────────────────────────────────────

echo -e "${BLUE}[1/7] yamllint${NC} — YAML syntax & style validation"
if check_tool "yamllint" "yamllint"; then
  if [ -f .yamllint.yaml ]; then
    run_check "yamllint" yamllint -c .yamllint.yaml .
  else
    run_check "yamllint" yamllint .
  fi
fi
echo ""

# ── 2. pre-commit ─────────────────────────────────────────────────────────────

echo -e "${BLUE}[2/7] pre-commit${NC} — Pre-commit hooks"
if check_tool "pre-commit" "pre-commit"; then
  if [ -f .pre-commit-config.yaml ]; then
    run_check "pre-commit" pre-commit run --all-files --hook-stage manual
  else
    log_fail "pre-commit: no .pre-commit-config.yaml found"
    ((FAIL++)) || true
    ((TOTAL++)) || true
  fi
fi
echo ""

# ── 3. ansible-lint ───────────────────────────────────────────────────────────

echo -e "${BLUE}[3/7] ansible-lint${NC} — Ansible best practices"
if check_tool "ansible-lint" "ansible-lint"; then
  if [ -f .ansible-lint.yml ]; then
    run_check "ansible-lint" ansible-lint -c .ansible-lint.yml
  else
    run_check "ansible-lint" ansible-lint
  fi
fi
echo ""

# ── 4. shellcheck ─────────────────────────────────────────────────────────────

echo -e "${BLUE}[4/7] shellcheck${NC} — Shell script static analysis"
if check_tool "shellcheck" "shellcheck"; then
  sh_files=""
  sh_files=$(find . -name '*.sh' \
    -not -path './.git/*' \
    -not -path './.venv/*' \
    -not -path './.campaign-state/*' \
    -not -path './.campaign-runtime/*' \
    -not -path './.migration-state/*' \
    -not -path './node_modules/*' \
    -not -path './playbooks/kubespray/*' 2>/dev/null || true)
  if [ -n "$sh_files" ]; then
    run_check "shellcheck" sh -c "printf '%s\n' \"$sh_files\" | xargs shellcheck"
  else
    log_fail "shellcheck: no .sh files found"
    ((FAIL++)) || true
    ((TOTAL++)) || true
  fi
fi
echo ""

# ── 5. version-matrix ────────────────────────────────────────────────────────

echo -e "${BLUE}[5/7] version-matrix${NC} — Version compatibility validation"
if [ -f scripts/verify-version-matrix.py ]; then
  if command -v python3 &>/dev/null; then
    run_check "version-matrix" python3 scripts/verify-version-matrix.py
  else
    log_fail "version-matrix: python3 not found. Install Python 3."
    ((FAIL++)) || true
    ((TOTAL++)) || true
  fi
else
  log_fail "version-matrix: scripts/verify-version-matrix.py not found"
  ((FAIL++)) || true
  ((TOTAL++)) || true
fi
echo ""

# ── 6. ansible-syntax ────────────────────────────────────────────────────────

echo -e "${BLUE}[6/7] ansible-syntax${NC} — Deployment playbook parser checks"
if check_tool "ansible-playbook" "ansible-playbook"; then
  run_check "deploy-platform-syntax" ansible-playbook playbooks/deploy_platform.yml --syntax-check
  run_check "remove-component-syntax" ansible-playbook playbooks/remove_component.yml --syntax-check
  run_check "continue-post-kubespray-syntax" ansible-playbook playbooks/continue_post_kubespray.yml --syntax-check
  run_check "edge-cdn-syntax" ansible-playbook playbooks/edge-cdn.yml --syntax-check
fi
echo ""

# ── 7. python-tests (pytest) ─────────────────────────────────────────────────

echo -e "${BLUE}[7/7] python-tests${NC} — Unit and static component-contract tests"
if [ -d tests ] && [ "$(find tests -name 'test_*.py' 2>/dev/null | head -1)" != "" ]; then
  if command -v pytest &>/dev/null; then
    run_check "pytest" pytest tests/ -v --no-header --tb=short
  elif command -v python3 &>/dev/null; then
    run_check "pytest" python3 -m pytest tests/ -v --no-header --tb=short
  else
    log_fail "pytest: not found. Install it with the project requirements."
    ((FAIL++)) || true
    ((TOTAL++)) || true
  fi
else
  log_fail "python-tests: no tests/ directory or test files found"
  ((FAIL++)) || true
  ((TOTAL++)) || true
fi
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────

echo -e "${BLUE}══════════════════════════════════════════════════════════${NC}"
echo -e "  Summary: ${PASS} passed, ${FAIL} failed out of ${TOTAL} checks"
echo -e "${BLUE}══════════════════════════════════════════════════════════${NC}"

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo -e "${RED}Validation FAILED — ${FAIL} check(s) did not pass.${NC}"
  exit 1
fi

echo ""
echo -e "${GREEN}All mandatory checks passed.${NC}"
exit 0
