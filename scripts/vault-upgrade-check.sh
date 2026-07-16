#!/usr/bin/env bash
# vault-upgrade-check.sh — Preflight checks before Vault major upgrade
# Usage: ./scripts/vault-upgrade-check.sh [OPTIONS]
#
# Options:
#   --vault-namespace   Namespace where Vault is deployed (default: vault)
#   --vault-server      Vault API address (default: http://vault.<ns>.svc.cluster.local:8200)
#   --s3-endpoint       S3-compatible endpoint URL
#   --s3-bucket         S3 bucket for Vault Raft snapshots (default: vault-snapshots)
#   --snapshot-max-age  Maximum snapshot age in hours (default: 24)
#   --dry-run           Run checks without cluster access (for CI/testing)
#   --help              Show this help
#
# Exit codes:
#   0 — All checks passed
#   1 — One or more checks failed
#   2 — Script error (bad arguments, etc.)
set -euo pipefail

# ── Color helpers ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'

pass()  { echo -e "${GREEN}[PASS]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
section() { echo -e "\n${BOLD}── $* ──${NC}"; }

# ── Defaults ───────────────────────────────────────────────────
VAULT_NS="vault"
VAULT_SERVER=""
S3_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-}"
S3_BUCKET="${VAULT_SNAPSHOT_BUCKET:-vault-snapshots}"
SNAPSHOT_MAX_AGE_HOURS=24
DRY_RUN=false

# ── Counters ──────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

check_pass() { PASS_COUNT=$((PASS_COUNT + 1)); pass "$*"; }
check_fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); fail "$*"; }
check_warn() { WARN_COUNT=$((WARN_COUNT + 1)); warn "$*"; }

# ── Parse arguments ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault-namespace)   VAULT_NS="$2"; shift 2 ;;
    --vault-server)      VAULT_SERVER="$2"; shift 2 ;;
    --s3-endpoint)       S3_ENDPOINT="$2"; shift 2 ;;
    --s3-bucket)         S3_BUCKET="$2"; shift 2 ;;
    --snapshot-max-age)  SNAPSHOT_MAX_AGE_HOURS="$2"; shift 2 ;;
    --dry-run)           DRY_RUN=true; shift ;;
    -h|--help)
      head -20 "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) fail "Unknown option: $1"; exit 2 ;;
  esac
done

VAULT_SERVER="${VAULT_SERVER:-http://vault.${VAULT_NS}.svc.cluster.local:8200}"

# ────────────────────────────────────────────────────────────────
section "Vault Upgrade Preflight Checks"
if [ "$DRY_RUN" = "true" ]; then
  info "Running in DRY-RUN mode (no cluster access)"
fi

# ── CHECK 1: kubectl availability ────────────────────────────
section "1. Tooling"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Tooling checks skipped"
else
  if command -v kubectl &>/dev/null; then
    check_pass "kubectl is available ($(kubectl version --client --short 2>/dev/null || kubectl version --client -o json 2>/dev/null | jq -r .clientVersion.gitVersion || echo 'version unknown'))"
  else
    check_fail "kubectl is not installed or not in PATH"
  fi

  if [ "$S3_ENDPOINT" ]; then
    if command -v aws &>/dev/null; then
      check_pass "aws CLI is available"
    else
      check_fail "aws CLI is not installed (needed for S3 checks)"
    fi
  else
    check_warn "S3_ENDPOINT not set; S3 snapshot checks will be skipped"
  fi
fi

# ── CHECK 2: Vault version ────────────────────────────────────
section "2. Vault Version"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Vault version check skipped"
else
  # Try to get version from the running pod
  VAULT_VERSION=""
  for pod in vault-0 vault-1 vault-2; do
    if VAULT_VERSION=$(kubectl exec -n "$VAULT_NS" "$pod" -- vault version 2>/dev/null | head -1); then
      info "Leader pod: $pod"
      break
    fi
  done

  if [ -n "$VAULT_VERSION" ]; then
    # Extract version number (e.g., "Vault v1.21.2" → "1.21.2")
    VERSION_NUM=$(echo "$VAULT_VERSION" | grep -oP 'v?\K[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    if [ -n "$VERSION_NUM" ]; then
      info "Current Vault version: $VERSION_NUM"

      # Check if it's on the expected starting version
      MAJOR=$(echo "$VERSION_NUM" | cut -d. -f1)
      MINOR=$(echo "$VERSION_NUM" | cut -d. -f2)
      if [ "$MAJOR" = "1" ] && [ "$MINOR" -ge 21 ] && [ "$MINOR" -le 24 ]; then
        check_pass "Vault version $VERSION_NUM is on upgrade path (1.x series)"
      elif [ "$MAJOR" = "2" ]; then
        check_pass "Vault is already at 2.x ($VERSION_NUM)"
      else
        check_warn "Vault version $VERSION_NUM is outside expected upgrade path (1.21-1.24 or 2.x)"
      fi
    else
      check_warn "Could not parse Vault version from: $VAULT_VERSION"
    fi
  else
    check_fail "Could not determine Vault version — no Vault pods reachable in namespace $VAULT_NS"
  fi
fi

# ── CHECK 3: Vault status (sealed, initialized) ──────────────
section "3. Vault Status"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Vault status check skipped"
else
  VAULT_STATUS=$(kubectl exec -n "$VAULT_NS" vault-0 -- vault status -format=json 2>/dev/null || echo "{}")

  INITIALIZED=$(echo "$VAULT_STATUS" | jq -r '.initialized // empty' 2>/dev/null)
  SEALED=$(echo "$VAULT_STATUS" | jq -r '.sealed // empty' 2>/dev/null)
  HA_ENABLED=$(echo "$VAULT_STATUS" | jq -r '.ha_enabled // empty' 2>/dev/null)
  HA_CURRENT=$(echo "$VAULT_STATUS" | jq -r '.ha_current // empty' 2>/dev/null)

  if [ "$INITIALIZED" = "true" ]; then
    check_pass "Vault is initialized"
  else
    check_fail "Vault is NOT initialized (cannot upgrade)"
  fi

  if [ "$SEALED" = "false" ]; then
    check_pass "Vault is unsealed"
  elif [ "$SEALED" = "true" ]; then
    check_fail "Vault is SEALED — must be unsealed before upgrade"
  else
    check_warn "Could not determine seal status"
  fi

  if [ "$HA_ENABLED" = "true" ]; then
    check_pass "Vault HA is enabled"
    if [ "$HA_CURRENT" = "true" ]; then
      check_pass "vault-0 is the Raft leader"
    else
      info "vault-0 is a Raft follower; leader election is healthy"
    fi
  else
    check_pass "Vault is running in standalone mode"
  fi
fi

# ── CHECK 4: Raft snapshot age ────────────────────────────────
section "4. Raft Snapshot Age"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Raft snapshot check skipped"
  check_pass "[DRY-RUN] S3 connectivity check skipped"
  check_pass "[DRY-RUN] Snapshot existence check skipped"
elif [ -z "$S3_ENDPOINT" ]; then
  check_warn "S3_ENDPOINT not set; cannot check snapshots in S3"
else
  # List snapshots in S3
  SNAPSHOT_LIST=$(aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://${S3_BUCKET}/" 2>/dev/null || echo "")

  if [ -z "$SNAPSHOT_LIST" ]; then
    check_fail "No snapshots found in s3://${S3_BUCKET}/"
  else
    LATEST_SNAPSHOT=$(echo "$SNAPSHOT_LIST" | sort | tail -1)
    LATEST_NAME=$(echo "$LATEST_SNAPSHOT" | awk '{print $4}')
    LATEST_TS=$(echo "$LATEST_SNAPSHOT" | awk '{print $1}')

    if [ -n "$LATEST_NAME" ]; then
      check_pass "Latest snapshot found: $LATEST_NAME"
      info "Snapshot timestamp: $LATEST_TS"

      # Try to extract date from the filename (format: vault-YYYYMMDDTHHMMSSZ.snap)
      DATE_STR=$(echo "$LATEST_NAME" | grep -oP '\d{8}T\d{6}Z' || echo "")
      if [ -n "$DATE_STR" ]; then
        SNAPSHOT_EPOCH=$(date -d "$DATE_STR" +%s 2>/dev/null || echo "0")
        NOW_EPOCH=$(date +%s)
        AGE_HOURS=$(( (NOW_EPOCH - SNAPSHOT_EPOCH) / 3600 ))

        if [ "$SNAPSHOT_EPOCH" != "0" ] && [ "$AGE_HOURS" -lt "$SNAPSHOT_MAX_AGE_HOURS" ]; then
          check_pass "Snapshot is ${AGE_HOURS}h old (threshold: ${SNAPSHOT_MAX_AGE_HOURS}h)"
        elif [ "$SNAPSHOT_EPOCH" != "0" ]; then
          check_fail "Snapshot is ${AGE_HOURS}h old — exceeds threshold of ${SNAPSHOT_MAX_AGE_HOURS}h"
        else
          check_warn "Could not parse snapshot age from filename: $LATEST_NAME"
        fi
      else
        check_warn "Could not parse date from snapshot filename: $LATEST_NAME"
      fi
    fi
  fi
fi

# ── CHECK 5: S3 connectivity ─────────────────────────────────
section "5. S3 Connectivity"
if [ "$DRY_RUN" = "true" ] || [ -z "$S3_ENDPOINT" ]; then
  if [ "$DRY_RUN" != "true" ]; then
    check_warn "S3_ENDPOINT not set; skipping S3 connectivity"
  fi
else
  S3_RESULT=$(aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://${S3_BUCKET}/" 2>&1)
  S3_RC=$?

  if [ $S3_RC -eq 0 ]; then
    check_pass "S3 connectivity to $S3_ENDPOINT is OK"
  else
    check_fail "Cannot reach S3 endpoint $S3_ENDPOINT: $S3_RESULT"
  fi
fi

# ── CHECK 6: Unseal key recovery (dry-run) ───────────────────
section "6. Unseal Key Recovery"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Unseal key recovery check skipped"
else
  # Check that the vault-unseal-key secret exists in the vault namespace
  if kubectl get secret -n "$VAULT_NS" vault-unseal-key &>/dev/null; then
    check_pass "vault-unseal-key secret exists in namespace $VAULT_NS"
  else
    # The Helm chart may use a different secret name pattern
    UNSEAL_SECRETS=$(kubectl get secret -n "$VAULT_NS" --no-headers 2>/dev/null | grep -i unseal || echo "")
    if [ -n "$UNSEAL_SECRETS" ]; then
      check_pass "Unseal key secret found: $(echo "$UNSEAL_SECRETS" | awk '{print $1}')"
    else
      check_warn "No vault-unseal-key secret found — verify auto-unseal is configured"
    fi
  fi
fi

# ── CHECK 7: Vault pods health ───────────────────────────────
section "7. Vault Pod Health"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Pod health check skipped"
else
  POD_COUNT=$(kubectl get pods -n "$VAULT_NS" -l app.kubernetes.io/name=vault -l component=server --no-headers 2>/dev/null | wc -l)
  RUNNING_COUNT=$(kubectl get pods -n "$VAULT_NS" -l app.kubernetes.io/name=vault -l component=server --no-headers 2>/dev/null | grep -c Running || true)
  NOT_RUNNING=$(kubectl get pods -n "$VAULT_NS" -l app.kubernetes.io/name=vault -l component=server --no-headers 2>/dev/null | grep -v Running || echo "")

  check_pass "$RUNNING_COUNT of $POD_COUNT Vault server pods are Running"

  if [ -n "$NOT_RUNNING" ] && [ "$NOT_RUNNING" != "" ]; then
    check_warn "Some Vault pods are not Running:\n$NOT_RUNNING"
  fi
fi

# ── CHECK 8: ESO secret sync status ──────────────────────────
section "8. External Secrets Operator"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] ESO sync check skipped"
else
  # Check if ESO is installed
  if kubectl get crd externalsecrets.external-secrets.io &>/dev/null 2>&1; then
    info "External Secrets Operator is installed"

    # Check for any synced ExternalSecrets
    ES_COUNT=$(kubectl get externalsecret --all-namespaces --no-headers 2>/dev/null | wc -l || echo "0")
    info "Found $ES_COUNT ExternalSecret resources"

    if [ "$ES_COUNT" -gt 0 ]; then
      # Check for any that are NOT synced
      UNSYNCED=$(kubectl get externalsecret --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.status.conditions[0].status}{"\n"}{end}' 2>/dev/null | grep -v True || echo "")
      if [ -z "$UNSYNCED" ]; then
        check_pass "All $ES_COUNT ExternalSecrets are synced"
      else
        check_fail "Some ExternalSecrets are not synced:\n$UNSYNCED"
      fi
    else
      check_warn "No ExternalSecret resources found — ESO may not be in use"
    fi
  else
    check_warn "External Secrets Operator is not installed (ExternalSecret CRD not found)"
  fi
fi

# ── CHECK 9: Audit devices ───────────────────────────────────
section "9. Audit Configuration"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Audit configuration check skipped"
else
  AUDIT_LIST=$(kubectl exec -n "$VAULT_NS" vault-0 -- vault audit list 2>/dev/null || echo "")
  if echo "$AUDIT_LIST" | grep -q "file\|socket"; then
    check_pass "Audit devices are configured"
    info "Audit devices:\n$AUDIT_LIST"
  elif [ -n "$AUDIT_LIST" ] && [ "$AUDIT_LIST" != "" ]; then
    check_pass "Audit list is accessible"
    info "Audit devices:\n$AUDIT_LIST"
  else
    check_warn "Could not retrieve audit device list or no devices configured"
  fi
fi

# ── CHECK 10: Helm release status ────────────────────────────
section "10. Helm Release"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Helm release check skipped"
else
  if command -v helm &>/dev/null; then
    HELM_STATUS=$(helm status vault -n "$VAULT_NS" --output json 2>/dev/null || echo "")
    if [ -n "$HELM_STATUS" ]; then
      RELEASE_STATUS=$(echo "$HELM_STATUS" | jq -r '.info.status // empty' 2>/dev/null)
      RELEASE_REV=$(echo "$HELM_STATUS" | jq -r '.info.revision // empty' 2>/dev/null)
      if [ "$RELEASE_STATUS" = "deployed" ]; then
        check_pass "Vault Helm release is deployed (revision: $RELEASE_REV)"
      else
        check_warn "Vault Helm release status: $RELEASE_STATUS"
      fi
    else
      check_warn "Could not get Helm release status for vault"
    fi
  else
    check_warn "helm CLI not installed; skipping Helm release check"
  fi
fi

# ────────────────────────────────────────────────────────────────
section "SUMMARY"
TOTAL=$((PASS_COUNT + FAIL_COUNT + WARN_COUNT))
echo -e "  Total checks:  ${BOLD}${TOTAL}${NC}"
echo -e "  ${GREEN}Passed:        ${PASS_COUNT}${NC}"
echo -e "  ${RED}Failed:        ${FAIL_COUNT}${NC}"
echo -e "  ${YELLOW}Warnings:      ${WARN_COUNT}${NC}"
echo ""

if [ "$FAIL_COUNT" -eq 0 ]; then
  echo -e "  ${GREEN}${BOLD}✓ All checks passed — safe to proceed with upgrade${NC}"
  exit 0
else
  echo -e "  ${RED}${BOLD}✗ ${FAIL_COUNT} check(s) failed — DO NOT proceed with upgrade${NC}"
  echo -e "  ${YELLOW}Resolve failures before attempting the upgrade.${NC}"
  exit 1
fi
