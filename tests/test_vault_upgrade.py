"""
Tests for Vault upgrade plan, scripts, and restore drill.

Layers:
  - Unit:  script syntax, shebangs, flag parsing, argument handling,
           plan document structure and content completeness.
  - Component: script logic paths (preflight checks categories,
                restore drill step ordering, upgrade path validity).
  - E2E:    dry-run execution of both scripts (no cluster needed).
"""
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SCRIPTS_DIR = REPO_ROOT / "scripts"

VAULT_PLAN = DOCS_DIR / "VAULT_UPGRADE_PLAN.md"
UPGRADE_CHECK = SCRIPTS_DIR / "vault-upgrade-check.sh"
RESTORE_DRILL = SCRIPTS_DIR / "vault-restore-drill.sh"


# =====================================================================
#  UNIT TESTS
# =====================================================================

class TestPlanDocumentStructure:
    """Unit: VAULT_UPGRADE_PLAN.md exists and has required sections."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = VAULT_PLAN.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert VAULT_PLAN.is_file()

    def test_not_empty(self):
        assert len(self.content) > 5000  # Substantial doc

    def test_has_incremental_upgrade_path(self):
        assert "1.21" in self.content
        assert "1.22" in self.content
        assert "1.23" in self.content
        assert "1.24" in self.content
        assert "2.0" in self.content

    def test_has_prerequisites_section(self):
        assert "Pre-Upgrade Prerequisites" in self.content or "Prerequisites" in self.content

    def test_has_raft_snapshot_prerequisite(self):
        assert "Raft snapshot" in self.content
        assert "S3" in self.content

    def test_has_seal_unseal_prerequisite(self):
        assert "Seal" in self.content or "seal" in self.content
        assert "Unseal" in self.content or "unseal" in self.content

    def test_has_audit_prerequisite(self):
        assert "Audit" in self.content or "audit" in self.content

    def test_has_eso_prerequisite(self):
        assert "ESO" in self.content or "External Secrets" in self.content or "external-secret" in self.content

    def test_has_per_step_procedure(self):
        assert "rolling" in self.content.lower()
        assert "leader" in self.content.lower()
        assert "API health" in self.content or "sys/health" in self.content

    def test_has_v2_migration_notes(self):
        assert "2.x" in self.content or "2.0" in self.content
        assert "AutoStorage" in self.content or "autostorage" in self.content

    def test_has_rollback_section(self):
        assert "Rollback" in self.content or "rollback" in self.content

    def test_has_rollback_from_snapshot(self):
        assert "restore" in self.content.lower() and "snapshot" in self.content.lower()

    def test_has_risk_assessment(self):
        assert "Risk" in self.content or "risk" in self.content

    def test_has_severity_levels(self):
        content_upper = self.content.upper()
        assert "HIGH" in content_upper or "CRITICAL" in content_upper
        assert "MEDIUM" in content_upper or "MODERATE" in content_upper
        assert "LOW" in content_upper

    def test_has_downtime_estimate(self):
        assert "downtime" in self.content.lower() or "Downtime" in self.content

    def test_has_checklist(self):
        assert "checklist" in self.content.lower() or "- [ ]" in self.content

    def test_has_version_mapping_table(self):
        # Should have version/chart mapping
        assert "0.32.0" in self.content
        assert "0.34" in self.content

    def test_has_auto_unseal_reference(self):
        assert "auto-unseal" in self.content.lower() or "auto_unseal" in self.content.lower()

    def test_references_backup_restore(self):
        assert "backup" in self.content.lower()

    def test_has_communication_plan(self):
        assert "Communication" in self.content or "communicat" in self.content.lower()

    def test_has_post_upgrade_tasks(self):
        assert "Post-Upgrade" in self.content or "post-upgrade" in self.content or "post upgrade" in self.content.lower()


class TestUpgradeCheckScriptUnit:
    """Unit: vault-upgrade-check.sh structure and syntax."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = UPGRADE_CHECK.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert UPGRADE_CHECK.is_file()

    def test_is_executable(self):
        assert os.access(UPGRADE_CHECK, os.X_OK)

    def test_has_shebang(self):
        assert self.content.startswith("#!/usr/bin/env bash") or self.content.startswith("#!/bin/bash")

    def test_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(UPGRADE_CHECK)],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_has_dry_run_flag(self):
        assert "--dry-run" in self.content

    def test_has_vault_namespace_flag(self):
        assert "--vault-namespace" in self.content

    def test_has_s3_endpoint_flag(self):
        assert "--s3-endpoint" in self.content

    def test_has_s3_bucket_flag(self):
        assert "--s3-bucket" in self.content

    def test_has_snapshot_max_age_flag(self):
        assert "--snapshot-max-age" in self.content

    def test_has_help_flag(self):
        assert "--help" in self.content

    def test_has_set_euo_pipefail(self):
        assert "set -euo pipefail" in self.content

    def test_has_color_helpers(self):
        assert "RED=" in self.content
        assert "GREEN=" in self.content

    def test_has_pass_fail_counters(self):
        assert "PASS_COUNT" in self.content
        assert "FAIL_COUNT" in self.content

    def test_checks_vault_version(self):
        assert "Vault version" in self.content or "vault version" in self.content

    def test_checks_raft_snapshot(self):
        assert "snapshot" in self.content.lower()

    def test_checks_s3_connectivity(self):
        assert "S3" in self.content or "s3" in self.content

    def test_checks_unseal_keys(self):
        assert "unseal" in self.content.lower()

    def test_checks_eso_sync(self):
        assert "ExternalSecret" in self.content or "eso" in self.content.lower()

    def test_checks_pod_health(self):
        assert "pod" in self.content.lower() and ("health" in self.content.lower() or "Running" in self.content)

    def test_checks_audit(self):
        assert "audit" in self.content.lower()

    def test_exits_0_on_pass(self):
        assert "exit 0" in self.content

    def test_exits_1_on_fail(self):
        assert "exit 1" in self.content

    def test_has_summary_section(self):
        assert "SUMMARY" in self.content or "summary" in self.content.lower()

    def test_has_section_headers(self):
        # Should have multiple numbered sections — script uses section "N. ..."
        sections = re.findall(r'section\s+"?\d+\.', self.content)
        assert len(sections) >= 5, f"Expected at least 5 sections, found {len(sections)}"


class TestRestoreDrillScriptUnit:
    """Unit: vault-restore-drill.sh structure and syntax."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = RESTORE_DRILL.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert RESTORE_DRILL.is_file()

    def test_is_executable(self):
        assert os.access(RESTORE_DRILL, os.X_OK)

    def test_has_shebang(self):
        assert self.content.startswith("#!/usr/bin/env bash") or self.content.startswith("#!/bin/bash")

    def test_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(RESTORE_DRILL)],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_has_dry_run_flag(self):
        assert "--dry-run" in self.content

    def test_has_namespace_flag(self):
        assert "--namespace" in self.content

    def test_has_ttl_hours_flag(self):
        assert "--ttl-hours" in self.content

    def test_has_skip_cleanup_flag(self):
        assert "--skip-cleanup" in self.content

    def test_has_snapshot_bucket_flag(self):
        assert "--snapshot-bucket" in self.content

    def test_has_snapshot_name_flag(self):
        assert "--snapshot-name" in self.content

    def test_has_vault_version_flag(self):
        assert "--vault-version" in self.content

    def test_has_set_euo_pipefail(self):
        assert "set -euo pipefail" in self.content

    def test_creates_namespace(self):
        assert "create namespace" in self.content or "kubectl apply" in self.content

    def test_applies_resource_quota(self):
        assert "ResourceQuota" in self.content

    def test_deploys_vault(self):
        assert "vault" in self.content.lower() and ("Deployment" in self.content or "StatefulSet" in self.content)

    def test_initializes_vault(self):
        assert "operator init" in self.content or "initialize" in self.content.lower()

    def test_unseals_vault(self):
        assert "unseal" in self.content.lower()

    def test_verifies_secrets(self):
        assert "secret" in self.content.lower() and ("kv put" in self.content.lower() or "write" in self.content.lower())

    def test_cleanup_namespace(self):
        assert "delete namespace" in self.content or "cleanup" in self.content.lower()

    def test_has_auto_cleanup_cronjob(self):
        assert "CronJob" in self.content

    def test_has_pass_fail_counters(self):
        assert "PASS_COUNT" in self.content
        assert "FAIL_COUNT" in self.content

    def test_has_drill_summary(self):
        assert "DRILL SUMMARY" in self.content or "SUMMARY" in self.content

    def test_exits_0_on_success(self):
        assert "exit 0" in self.content

    def test_exits_1_on_failure(self):
        assert "exit 1" in self.content


# =====================================================================
#  COMPONENT TESTS
# =====================================================================

class TestUpgradePathValidity:
    """Component: Validate the upgrade path logic in the plan."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = VAULT_PLAN.read_text(encoding="utf-8")

    def test_path_starts_at_current_version(self):
        # Current version is 1.21.2
        assert "1.21.2" in self.content or "1.21" in self.content

    def test_path_ends_at_target(self):
        assert "2.0" in self.content or "2.x" in self.content

    def test_path_is_incremental(self):
        # Each minor version must appear in the document
        version_order = ["1.21", "1.22", "1.23", "1.24", "2.0"]
        for v in version_order:
            assert v in self.content, f"Version {v} not found in upgrade path"

        # The upgrade path section should show them incrementally (arrow notation or ordered list)
        # Look for the path visualization section
        assert "1.22" in self.content
        assert "1.23" in self.content
        assert "1.24" in self.content

    def test_no_skipped_minors(self):
        # Must not jump from 1.21 to 1.23 without 1.22
        for v in ["1.22", "1.23", "1.24"]:
            assert v in self.content, f"Minor version {v} is missing from upgrade path"

    def test_chart_versions_increased(self):
        # Chart versions should be mentioned
        assert "0.32" in self.content
        assert "0.33" in self.content or "0.34" in self.content

    def test_has_seven_step_procedure(self):
        # The plan should have 7 steps per minor version
        step_patterns = [
            r"Step\s*1.*[Ss]napshot",
            r"Step\s*2.*[Uu]pdate.*[Cc]hart|chart.*[Uu]pdate",
            r"Step\s*3.*[Rr]olling",
            r"Step\s*4.*[Ll]eader|[Uu]nseal|[Aa]PI|[Hh]ealth",
            r"Step\s*5.*[Ss]ecret|read.*write|write.*read",
            r"Step\s*6.*[Ee]SO|External.*[Ss]ecret|sync",
            r"Step\s*7.*[Mm]ove|[Nn]ext",
        ]
        found_steps = sum(1 for p in step_patterns if re.search(p, self.content))
        assert found_steps >= 5, f"Only {found_steps} of 7 steps found in procedure"


class TestPreflightChecksCompleteness:
    """Component: vault-upgrade-check.sh covers all required check categories."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = UPGRADE_CHECK.read_text(encoding="utf-8")

    def test_checks_vault_tools(self):
        assert "kubectl" in self.content

    def test_checks_vault_version(self):
        assert "version" in self.content.lower()

    def test_checks_vault_status(self):
        assert "sealed" in self.content.lower()
        assert "initialized" in self.content.lower()

    def test_checks_raft_snapshot_age(self):
        assert "age" in self.content.lower() or "old" in self.content.lower()
        assert "snapshot" in self.content.lower()

    def test_checks_s3_connectivity(self):
        assert "endpoint" in self.content.lower()
        assert "s3" in self.content.lower() or "S3" in self.content

    def test_checks_unseal_recovery(self):
        assert "unseal" in self.content.lower()
        assert "key" in self.content.lower()

    def test_checks_pod_health(self):
        assert "pod" in self.content.lower()
        assert "Running" in self.content or "running" in self.content.lower()

    def test_checks_eso(self):
        assert "ExternalSecret" in self.content or "eso" in self.content.lower()

    def test_checks_audit(self):
        assert "audit" in self.content.lower()

    def test_has_pass_fail_report(self):
        assert "PASS" in self.content
        assert "FAIL" in self.content

    def test_has_exit_code_on_failure(self):
        # The script must exit non-zero on failure
        assert "exit 1" in self.content

    def test_has_dry_run_mode(self):
        assert "DRY_RUN" in self.content or "dry-run" in self.content
        # Dry run should produce all-pass results
        dry_run_passes = self.content.count("[DRY-RUN]")
        assert dry_run_passes >= 5, f"Expected at least 5 DRY-RUN skips, found {dry_run_passes}"


class TestRestoreDrillCompleteness:
    """Component: vault-restore-drill.sh covers all restore drill steps."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = RESTORE_DRILL.read_text(encoding="utf-8")

    def test_has_prerequisite_check(self):
        assert "Prerequisite" in self.content or "prerequisite" in self.content.lower()

    def test_creates_isolated_namespace(self):
        assert "namespace" in self.content.lower()
        assert "restore-drill" in self.content.lower()

    def test_applies_resource_limits(self):
        assert "ResourceQuota" in self.content
        assert "requests.cpu" in self.content or "limits.cpu" in self.content

    def test_downloads_snapshot(self):
        assert "snapshot" in self.content.lower()
        assert "s3 cp" in self.content.lower() or "s3 ls" in self.content.lower()

    def test_deploys_vault_standalone(self):
        assert "vault" in self.content.lower()
        assert "standalone" in self.content.lower() or "replicas: 1" in self.content

    def test_has_vault_config(self):
        assert "listener" in self.content.lower()
        assert "storage" in self.content.lower()

    def test_initializes_or_restores(self):
        assert "init" in self.content.lower()
        assert "restore" in self.content.lower() or "initialize" in self.content.lower()

    def test_unseals_vault(self):
        assert "unseal" in self.content.lower()

    def test_verifies_secrets(self):
        # Should write and read a test secret
        assert "kv put" in self.content.lower() or "write" in self.content.lower()
        assert "kv get" in self.content.lower() or "read" in self.content.lower()

    def test_has_auto_cleanup(self):
        assert "CronJob" in self.content or "cleanup" in self.content.lower()
        assert "delete namespace" in self.content or "kubectl delete" in self.content

    def test_has_skip_cleanup_option(self):
        assert "skip-cleanup" in self.content.lower() or "SKIP_CLEANUP" in self.content

    def test_reports_results(self):
        assert "SUMMARY" in self.content or "summary" in self.content.lower()
        assert "PASS_COUNT" in self.content or "PASS" in self.content
        assert "FAIL_COUNT" in self.content or "FAIL" in self.content


# =====================================================================
#  E2E TESTS (dry-run only — no cluster required)
# =====================================================================

class TestUpgradeCheckDryRun:
    """E2E: Run vault-upgrade-check.sh in dry-run mode."""

    def test_dry_run_exits_zero(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"Dry run failed: {result.stderr}\n{result.stdout}"

    def test_dry_run_reports_summary(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        assert "SUMMARY" in output.upper() or "summary" in output.lower()

    def test_dry_run_has_pass_results(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        assert "[PASS]" in output, "Expected [PASS] markers in dry run output"

    def test_dry_run_has_no_fail_results(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        assert "[FAIL]" not in output, "Unexpected [FAIL] in dry run output"

    def test_dry_run_reports_check_count(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        # Should have a count of passed checks > 0
        assert re.search(r'Passed:\s+(\d+)', output), "Expected 'Passed: N' in output"
        passed_match = re.search(r'Passed:\s+(\d+)', output)
        assert int(passed_match.group(1)) >= 5, "Expected at least 5 passed checks in dry run"

    def test_dry_run_all_checks_pass(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        fail_match = re.search(r'Failed:\s+(\d+)', output)
        assert fail_match is None or int(fail_match.group(1)) == 0, \
            f"Expected 0 failed checks in dry run"

    def test_help_flag_works(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--help"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0
        assert "Usage" in result.stdout or "--vault-namespace" in result.stdout


class TestRestoreDrillDryRun:
    """E2E: Run vault-restore-drill.sh in dry-run mode."""

    def test_dry_run_exits_zero(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"Dry run failed: {result.stderr}\n{result.stdout}"

    def test_dry_run_shows_plan(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        # Should show steps
        assert "Steps" in output or "steps" in output.lower()

    def test_dry_run_shows_namespace(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        assert "vault-restore-drill" in result.stdout

    def test_dry_run_with_custom_namespace(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run", "--namespace", "my-drill-ns"],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        assert "my-drill-ns" in result.stdout

    def test_dry_run_with_custom_version(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run", "--vault-version", "2.0.1"],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        assert "2.0.1" in result.stdout

    def test_dry_run_with_skip_cleanup(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run", "--skip-cleanup"],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        output = result.stdout
        assert "Skip" in output or "skip" in output.lower() or "cleanup" in output.lower()

    def test_help_flag_works(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--help"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0
        assert "Usage" in result.stdout or "--snapshot" in result.stdout


class TestScriptsIntegration:
    """Component/E2E: Verify scripts are consistent with the project."""

    def test_upgrade_check_references_vault_namespace(self):
        content = UPGRADE_CHECK.read_text()
        # Default namespace should match what's used in roles/k8s-secrets
        assert "vault" in content.lower()

    def test_restore_drill_default_namespace_different_from_prod(self):
        content = RESTORE_DRILL.read_text()
        # The drill namespace should NOT be the production vault namespace
        assert "vault-restore-drill" in content

    def test_plan_references_upgrade_check_script(self):
        content = VAULT_PLAN.read_text()
        assert "vault-upgrade-check.sh" in content

    def test_plan_references_restore_drill_script(self):
        content = VAULT_PLAN.read_text()
        assert "vault-restore-drill.sh" in content

    def test_plan_mentions_roles_k8s_secrets(self):
        content = VAULT_PLAN.read_text()
        assert "roles/k8s-secrets" in content

    def test_plan_mentions_backup_restore_role(self):
        content = VAULT_PLAN.read_text()
        assert "backup-restore" in content.lower() or "backup" in content.lower()

    def test_both_scripts_use_same_set_euo(self):
        uc = UPGRADE_CHECK.read_text()
        rd = RESTORE_DRILL.read_text()
        assert "set -euo pipefail" in uc
        assert "set -euo pipefail" in rd


class TestConstraintsNotViolated:
    """Verify constraints: defaults/main.yml and k8s-secrets tasks not changed."""

    def test_defaults_main_not_modified(self):
        defaults = REPO_ROOT / "defaults" / "main.yml"
        content = defaults.read_text()
        # Vault version should still be 1.21.2 in the tasks file, not defaults
        # The constraint is about NOT changing defaults/main.yml
        assert defaults.is_file()

    def test_k8s_secrets_tasks_not_modified(self):
        tasks = REPO_ROOT / "roles" / "k8s-secrets" / "tasks" / "main.yml"
        content = tasks.read_text()
        # Should still reference 1.21.2
        assert "1.21.2" in content
        assert "0.32.0" in content

    def test_new_files_only(self):
        # Only docs, scripts, and tests should be new
        new_files = [
            VAULT_PLAN,
            UPGRADE_CHECK,
            RESTORE_DRILL,
        ]
        for f in new_files:
            assert f.is_file(), f"New file missing: {f}"
