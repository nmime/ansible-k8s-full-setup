#!/usr/bin/env bash
# test_elasticsearch_license_compliance.sh
# Static tests that FAIL if any Elasticsearch X-Pack license crack/bypass
# artifacts, platinum license payloads, or license evasion code remain.
set -euo pipefail

ROLE_DIR="$(cd "$(dirname "$0")/../roles/elasticsearch" && pwd)"
FAIL=0

fail() { FAIL=1; echo "FAIL: $1"; }
pass() { echo "PASS: $1"; }

# ── Test 1: platinum_license.json must not exist ──────────────────────────────
if [ -f "$ROLE_DIR/files/platinum_license.json" ]; then
  fail "platinum_license.json must be deleted"
else
  pass "platinum_license.json does not exist"
fi

# ── Test 2: No crack script ConfigMap in tasks ────────────────────────────────
if grep -riq 'crack-script\|crack-ready\|patch_xpack\|patch-xpack' \
   "$ROLE_DIR/tasks/" 2>/dev/null; then
  fail "tasks still contain crack-script / patch_xpack references"
else
  pass "no crack-script or patch_xpack references in tasks"
fi

# ── Test 3: No License.java / LicenseVerifier.java manipulation ───────────────
if grep -rq 'License\.java\|LicenseVerifier\.java\|TransportXPackInfoAction\.java' \
   "$ROLE_DIR/tasks/" 2>/dev/null; then
  fail "tasks still contain License.java / LicenseVerifier.java manipulation"
else
  pass "no License.java / LicenseVerifier.java references in tasks"
fi

# ── Test 4: No x-pack-core JAR replacement ────────────────────────────────────
if grep -rq 'x-pack-core.*\.jar' "$ROLE_DIR/tasks/" 2>/dev/null; then
  fail "tasks still contain x-pack-core JAR replacement logic"
else
  pass "no x-pack-core JAR replacement in tasks"
fi

# ── Test 5: No es-platinum-license secret ─────────────────────────────────────
if grep -rq 'es-platinum-license' "$ROLE_DIR/tasks/" 2>/dev/null; then
  fail "tasks still reference es-platinum-license secret"
else
  pass "no es-platinum-license secret reference"
fi

# ── Test 6: No license application Job ────────────────────────────────────────
if grep -rq 'Apply Platinum license\|es-apply-license' "$ROLE_DIR/tasks/" 2>/dev/null; then
  fail "tasks still contain Platinum license application Job"
else
  pass "no Platinum license application Job"
fi

# ── Test 7: License type must be 'basic' in defaults ──────────────────────────
LICENSE_TYPE=$(grep -oP 'es_license_type:\s*"?\K[^"]+' "$ROLE_DIR/defaults/main.yml" | head -1 | tr -d ' ')
if [ "$LICENSE_TYPE" != "basic" ]; then
  fail "es_license_type is '$LICENSE_TYPE', expected 'basic'"
else
  pass "es_license_type is 'basic'"
fi

# ── Test 8: xpack.license.self_generated.type must be set to basic ────────────
if grep -A1 'xpack\.license\.self_generated\.type' "$ROLE_DIR/tasks/main.yml" | grep -q "value:.*basic"; then
  pass "xpack.license.self_generated.type is set to 'basic'"
else
  fail "xpack.license.self_generated.type is not set to 'basic'"
fi

# ── Test 9: No init container named 'patch-xpack' or referencing crack ────────
if grep -q 'name:.*patch-xpack\|name:.*crack' "$ROLE_DIR/tasks/main.yml"; then
  fail "init containers still have crack-related names"
else
  pass "no crack-related init container names"
fi

# ── Test 10: No 'crack' word anywhere in elasticsearch role ───────────────────
if grep -riq 'crack' "$ROLE_DIR/" 2>/dev/null; then
  fail "the word 'crack' still appears in the elasticsearch role"
else
  pass "no 'crack' references in elasticsearch role"
fi

# ── Test 11: No 'javac' compilation of Elasticsearch source ───────────────────
if grep -rq 'javac\|jar\s*-xf\|jar\s*-cf' "$ROLE_DIR/tasks/" 2>/dev/null; then
  fail "tasks still contain JAR compilation/extraction commands"
else
  pass "no JAR compilation/extraction commands in tasks"
fi

# ── Test 12: No sed manipulation of License.validate() or verifyLicense() ─────
if grep -rq 'sed.*License\|sed.*verifyLicense\|sed.*validate' \
   "$ROLE_DIR/tasks/" 2>/dev/null; then
  fail "tasks still contain sed manipulation of license verification"
else
  pass "no sed manipulation of license verification"
fi

# ── Test 13: platinum_license.json must not be loaded via lookup ───────────────
if grep -rq 'lookup.*platinum_license\|platinum_license\.json' "$ROLE_DIR/" 2>/dev/null; then
  fail "tasks still load platinum_license.json via lookup"
else
  pass "no platinum_license.json lookup"
fi

# ── Test 14: Deployment summary must mention Basic (not Platinum) ─────────────
if grep -q 'Platinum' "$ROLE_DIR/tasks/main.yml"; then
  fail "deployment summary still mentions 'Platinum'"
else
  pass "deployment summary does not mention 'Platinum'"
fi

# ── Test 15: files/ directory must not exist (or be empty of license files) ───
if [ -d "$ROLE_DIR/files" ]; then
  LICENSE_FILES=$(find "$ROLE_DIR/files" -name '*license*' -o -name '*platinum*' -o -name '*gold*' 2>/dev/null | wc -l)
  if [ "$LICENSE_FILES" -gt 0 ]; then
    fail "license-related files still exist in files/ directory"
  else
    pass "files/ directory has no license artifacts"
  fi
else
  pass "files/ directory does not exist (clean)"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
if [ "$FAIL" -ne 0 ]; then
  echo "RESULT: FAILED — license crack/bypass artifacts detected"
  exit 1
fi
echo "RESULT: PASSED — all 15 license compliance checks passed"
exit 0
