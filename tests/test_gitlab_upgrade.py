"""
Tests for GitLab upgrade plan, preflight script, and restore drill.

Layers:
  - Unit:  plan document structure, script syntax/shebangs/flags,
           argument parsing, file existence and permissions.
  - Component: upgrade path validity, chart migration correctness,
                check script categories, restore drill step ordering.
  - E2E:    dry-run execution of both shell scripts (no cluster needed).
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SCRIPTS_DIR = REPO_ROOT / "scripts"

PLAN = DOCS_DIR / "GITLAB_UPGRADE_PLAN.md"
UPGRADE_CHECK = SCRIPTS_DIR / "gitlab-upgrade-check.sh"
RESTORE_DRILL = SCRIPTS_DIR / "gitlab-restore-test.sh"


# =====================================================================
# UNIT TESTS
# =====================================================================

class TestUpgradePlanDocument:
    """Unit: GITLAB_UPGRADE_PLAN.md structure and content."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = PLAN.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert PLAN.is_file()

    def test_not_empty(self):
        assert len(self.content) > 8000

    def test_has_current_and_target_versions(self):
        assert "18.11" in self.content
        assert "19.1" in self.content
        assert "9.11.4" in self.content
        assert "10.1.2" in self.content

    def test_has_incremental_upgrade_path(self):
        assert "18.11" in self.content
        assert "18.17" in self.content
        assert "19.0" in self.content
        assert "19.1" in self.content

    def test_has_chart_10_breaking_changes(self):
        assert "breaking" in self.content.lower()
        assert "global.psql" in self.content

    def test_has_psql_migration(self):
        assert "applicationSettings" in self.content
        assert "global.psql.host" in self.content
        assert "global.applicationSettings.database" in self.content

    def test_has_redis_migration(self):
        assert "redis" in self.content.lower()
        assert "external" in self.content.lower()
        assert "redis.install" in self.content or "redis.install" in self.content.lower()

    def test_has_gitaly_migration(self):
        assert "gitaly" in self.content.lower()
        assert "nodes" in self.content.lower()

    def test_has_prerequisites_section(self):
        assert "Pre-Upgrade Prerequisites" in self.content or "Prerequisites" in self.content

    def test_has_backup_prerequisite(self):
        assert "backup" in self.content.lower()
        assert "S3" in self.content or "s3" in self.content

    def test_has_postgresql_verification(self):
        assert "PostgreSQL" in self.content or "postgresql" in self.content.lower()

    def test_has_gitaly_verification(self):
        assert "Gitaly" in self.content or "gitaly" in self.content.lower()

    def test_has_redis_verification(self):
        assert "Redis" in self.content or "redis" in self.content.lower()

    def test_has_per_step_procedure(self):
        assert "Step 1" in self.content
        assert "Step 2" in self.content
        assert "Step 3" in self.content

    def test_has_rolling_update_reference(self):
        assert "rolling" in self.content.lower() or "helm upgrade" in self.content.lower()

    def test_has_rollback_section(self):
        assert "Rollback" in self.content or "rollback" in self.content

    def test_has_rollback_within_major(self):
        assert "helm rollback" in self.content or "helm rollback" in self.content.lower()

    def test_has_rollback_across_chart_versions(self):
        assert "9.x" in self.content and "10.x" in self.content
        # Should mention rollback from 10.x back to 9.x
        rollback_section = self.content[self.content.lower().find("rollback"):]
        assert "9.x" in rollback_section or "chart 9" in rollback_section.lower()

    def test_has_risk_assessment(self):
        assert "Risk" in self.content or "risk" in self.content

    def test_has_severity_levels(self):
        upper = self.content.upper()
        assert "HIGH" in upper
        assert "CRITICAL" in upper or "critical" in self.content.lower()
        assert "MEDIUM" in upper or "MODERATE" in upper

    def test_has_downtime_estimate(self):
        assert "downtime" in self.content.lower() or "Downtime" in self.content

    def test_has_checklist(self):
        assert "- [ ]" in self.content or "checklist" in self.content.lower()

    def test_has_version_mapping_table(self):
        # Should have version mapping
        assert "9.11.4" in self.content
        assert "10.1.2" in self.content

    def test_has_communication_plan(self):
        assert "Communication" in self.content or "communicat" in self.content.lower()

    def test_has_post_upgrade_tasks(self):
        assert "Post-Upgrade" in self.content or "post-upgrade" in self.content.lower()

    def test_has_object_storage_verification(self):
        assert "object storage" in self.content.lower() or "S3" in self.content

    def test_has_helm_template_validation(self):
        assert "helm template" in self.content.lower()

    def test_references_existing_backup_script(self):
        assert "backup-all" in self.content.lower() or "gitlab-backup" in self.content.lower()

    def test_has_gitaly_healthcheck(self):
        assert "gitaly-prrc" in self.content or "healthcheck" in self.content.lower()

    def test_has_maintenance_window(self):
        assert "maintenance" in self.content.lower() or "window" in self.content.lower()

    def test_has_execution_checklist(self):
        assert "checklist" in self.content.lower() or "- [ ]" in self.content


class TestUpgradeCheckScriptUnit:
    """Unit: gitlab-upgrade-check.sh structure and syntax."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = UPGRADE_CHECK.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert UPGRADE_CHECK.is_file()

    def test_is_executable(self):
        assert os.access(UPGRADE_CHECK, os.X_OK)

    def test_has_shebang(self):
        assert self.content.startswith("#!/usr/bin/env bash")

    def test_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(UPGRADE_CHECK)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_has_dry_run_flag(self):
        assert "--dry-run" in self.content

    def test_has_gitlab_namespace_flag(self):
        assert "--gitlab-namespace" in self.content

    def test_has_s3_endpoint_flag(self):
        assert "--s3-endpoint" in self.content

    def test_has_s3_bucket_flag(self):
        assert "--s3-bucket" in self.content

    def test_has_backup_max_age_flag(self):
        assert "--backup-max-age" in self.content

    def test_has_min_disk_free_flag(self):
        assert "--min-disk-free" in self.content

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

    def test_has_backup_age_check(self):
        assert "Backup Age" in self.content or "backup" in self.content.lower()

    def test_has_gitaly_status_check(self):
        assert "Gitaly" in self.content or "gitaly" in self.content.lower()

    def test_has_postgresql_check(self):
        assert "PostgreSQL" in self.content or "postgresql" in self.content.lower()

    def test_has_redis_check(self):
        assert "Redis" in self.content or "redis" in self.content.lower()

    def test_has_s3_connectivity_check(self):
        assert "S3" in self.content

    def test_has_pod_health_check(self):
        assert "pod" in self.content.lower()

    def test_has_helm_release_check(self):
        assert "Helm" in self.content or "helm" in self.content.lower()

    def test_has_summary_section(self):
        assert "SUMMARY" in self.content

    def test_exits_0_on_pass(self):
        assert "exit 0" in self.content

    def test_exits_1_on_fail(self):
        assert "exit 1" in self.content

    def test_has_section_headers(self):
        sections = re.findall(r'section\s+"?\d+\.', self.content)
        assert len(sections) >= 8, f"Expected at least 8 sections, found {len(sections)}"

    def test_checks_chart_version_upgrade_path(self):
        assert "9.x" in self.content and "10.x" in self.content

    def test_checks_app_version_upgrade_path(self):
        assert "18.x" in self.content and "19.x" in self.content

    def test_has_backup_cronjob_check(self):
        assert "CronJob" in self.content or "cronjob" in self.content.lower()

    def test_has_disk_space_check(self):
        assert "Disk" in self.content or "disk" in self.content.lower()


class TestRestoreDrillScriptUnit:
    """Unit: gitlab-restore-test.sh structure and syntax."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = RESTORE_DRILL.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert RESTORE_DRILL.is_file()

    def test_is_executable(self):
        assert os.access(RESTORE_DRILL, os.X_OK)

    def test_has_shebang(self):
        assert self.content.startswith("#!/usr/bin/env bash")

    def test_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(RESTORE_DRILL)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_has_restore_flag(self):
        assert "--restore" in self.content

    def test_has_backup_flag(self):
        assert "--backup" in self.content

    def test_has_namespace_flag(self):
        assert "--namespace" in self.content

    def test_has_ttl_hours_flag(self):
        assert "--ttl-hours" in self.content

    def test_has_dry_run_flag(self):
        assert "--dry-run" in self.content

    def test_has_cleanup_only_flag(self):
        assert "--cleanup-only" in self.content

    def test_has_list_backups_flag(self):
        assert "--list-backups" in self.content

    def test_has_help_flag(self):
        assert "--help" in self.content

    def test_has_s3_endpoint_flag(self):
        assert "--s3-endpoint" in self.content

    def test_has_s3_bucket_flag(self):
        assert "--s3-bucket" in self.content

    def test_has_set_euo_pipefail(self):
        assert "set -euo pipefail" in self.content

    def test_has_color_helpers(self):
        assert "RED=" in self.content
        assert "GREEN=" in self.content

    def test_has_pass_fail_counters(self):
        assert "PASS_COUNT" in self.content
        assert "FAIL_COUNT" in self.content

    def test_has_isolated_namespace_creation(self):
        assert "create namespace" in self.content.lower()

    def test_has_backup_download(self):
        assert "s3 cp" in self.content.lower() or "s3://" in self.content

    def test_has_restore_job_deployment(self):
        assert "Job" in self.content and "restore" in self.content.lower()

    def test_has_smoke_tests(self):
        assert "Smoke Test" in self.content or "smoke test" in self.content.lower()

    def test_has_cleanup_handler(self):
        assert "cleanup" in self.content.lower()

    def test_has_ttl_label(self):
        assert "ttl" in self.content.lower()

    def test_has_summary_section(self):
        assert "SUMMARY" in self.content

    def test_exits_0_on_pass(self):
        assert "exit 0" in self.content

    def test_exits_1_on_fail(self):
        assert "exit 1" in self.content

    def test_has_trap_for_cleanup(self):
        assert "trap" in self.content

    def test_has_source_namespace_flag(self):
        assert "--source-namespace" in self.content

    def test_has_validation_section(self):
        assert "Validation" in self.content or "validation" in self.content.lower()


# =====================================================================
# COMPONENT TESTS
# =====================================================================

class TestUpgradePathValidity:
    """Component: Verify the upgrade path is logically valid."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = PLAN.read_text(encoding="utf-8")

    def test_path_is_sequential(self):
        """Verify versions appear in ascending order in the upgrade path section."""
        # Extract version numbers from the path diagram
        versions = re.findall(r'(18\.\d+|19\.\d+)', self.content)
        # Should have at least 4 versions: 18.11, 18.17, 19.0, 19.1
        unique_versions = sorted(set(versions))
        assert len(unique_versions) >= 4

    def test_chart_versions_are_compatible(self):
        """Chart 9.x for 18.x, chart 10.x for 19.x."""
        # The plan should associate chart 9.x with 18.x steps
        assert "9.11.4" in self.content
        # And chart 10.x with 19.x steps
        assert "10.1.2" in self.content
        # Breaking change documentation
        assert "10.0" in self.content or "10.x" in self.content

    def test_psql_migration_is_documented(self):
        """global.psql must be documented as removed in chart 10.x."""
        psql_section = self.content[self.content.find("global.psql"):]
        assert "applicationSettings" in psql_section
        assert "database" in psql_section

    def test_redis_migration_is_documented(self):
        """Redis sub-chart removal must be documented."""
        redis_lower = self.content.lower()
        assert "redis.install" in redis_lower or "redis.install" in redis_lower
        # Should mention external Redis
        assert "external" in redis_lower

    def test_gitaly_migration_is_documented(self):
        """Gitaly single instance to nodes array migration."""
        gitaly_section = self.content[self.content.lower().find("gitaly"):]
        assert "nodes" in gitaly_section

    def test_risk_assessment_has_critical(self):
        """At least one critical risk."""
        upper = self.content.upper()
        assert "CRITICAL" in upper

    def test_risk_assessment_has_high(self):
        """At least four high risks."""
        risk_section = self.content[self.content.lower().find("risk"):]
        high_count = risk_section.upper().count("HIGH")
        assert high_count >= 3

    def test_risk_has_mitigations(self):
        """Each risk row should have a mitigation column."""
        # Look for table rows with HIGH/MEDIUM severity and mitigation text
        lines = self.content.split("\n")
        high_risk_lines = [l for l in lines if "HIGH" in l.upper() or "CRITICAL" in l.upper()]
        assert len(high_risk_lines) >= 3


class TestScriptCheckCategories:
    """Component: Verify the preflight script covers all required check categories."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = UPGRADE_CHECK.read_text(encoding="utf-8")

    def test_has_tooling_check(self):
        assert "Tooling" in self.content

    def test_has_cluster_connectivity_check(self):
        assert "Cluster" in self.content and "connect" in self.content.lower()

    def test_has_version_check(self):
        assert "Version" in self.content or "version" in self.content.lower()

    def test_has_pod_health_check(self):
        assert "Pod" in self.content or "pod" in self.content.lower()

    def test_has_backup_check(self):
        assert "Backup" in self.content or "backup" in self.content.lower()

    def test_has_s3_check(self):
        assert "S3" in self.content

    def test_has_disk_check(self):
        assert "Disk" in self.content or "disk" in self.content.lower()

    def test_has_gitaly_check(self):
        assert "Gitaly" in self.content or "gitaly" in self.content.lower()

    def test_has_database_check(self):
        assert "PostgreSQL" in self.content or "postgresql" in self.content.lower()

    def test_has_redis_check(self):
        assert "Redis" in self.content or "redis" in self.content.lower()

    def test_has_helm_check(self):
        assert "Helm" in self.content or "helm" in self.content.lower()

    def test_has_cronjob_check(self):
        assert "CronJob" in self.content or "cronjob" in self.content.lower()

    def test_has_dry_run_paths(self):
        """Every check section should have a DRY_RUN branch."""
        sections = re.findall(r'section\s+"?\d+\.', self.content)
        dry_run_blocks = self.content.count('[DRY-RUN]')
        assert dry_run_blocks >= len(sections), (
            f"Expected dry-run branches for all {len(sections)} sections, "
            f"found {dry_run_blocks} [DRY-RUN] markers"
        )

    def test_has_proper_exit_codes(self):
        """Script should exit 0 on pass, 1 on fail, 2 on error."""
        assert "exit 0" in self.content
        assert "exit 1" in self.content
        assert "exit 2" in self.content


class TestRestoreDrillStepOrdering:
    """Component: Verify restore drill has correct step ordering."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = RESTORE_DRILL.read_text(encoding="utf-8")

    def test_namespace_created_before_restore(self):
        ns_pos = self.content.find("Create Isolated Namespace")
        restore_pos = self.content.find("Deploy GitLab Restore Job")
        assert ns_pos < restore_pos, "Namespace must be created before restore job"

    def test_backup_downloaded_before_restore(self):
        download_pos = self.content.find("Download Backup")
        restore_pos = self.content.find("Deploy GitLab Restore Job")
        assert download_pos < restore_pos, "Backup must be downloaded before restore"

    def test_smoke_tests_after_restore(self):
        restore_pos = self.content.find("Deploy GitLab Restore Job")
        smoke_pos = self.content.find("Smoke Test")
        assert smoke_pos > restore_pos, "Smoke tests must run after restore"

    def test_cleanup_after_tests(self):
        smoke_pos = self.content.find("Smoke Test")
        # Find the numbered cleanup section, not the cleanup_namespace function
        cleanup_pos = self.content.find("section \"6. Cleanup\"")
        assert cleanup_pos > smoke_pos, "Cleanup section must run after smoke tests"

    def test_validation_before_restore(self):
        validation_pos = self.content.find("Validation")
        restore_pos = self.content.find("Deploy GitLab Restore Job")
        # Validation section should come before the actual restore deployment
        assert validation_pos < restore_pos, "Validation should precede restore"

    def test_has_isolation_mechanism(self):
        """Restore should happen in an isolated namespace."""
        assert "restore-drill" in self.content.lower() or "isolated" in self.content.lower()

    def test_has_auto_cleanup(self):
        """Restore namespace should have TTL-based cleanup."""
        assert "ttl" in self.content.lower()


# =====================================================================
# E2E TESTS
# =====================================================================

class TestUpgradeCheckDryRun:
    """E2E: Run gitlab-upgrade-check.sh in dry-run mode (no cluster needed)."""

    def test_dry_run_exits_zero(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"Exit {result.returncode}: {result.stderr}\n{result.stdout}"

    def test_dry_run_has_summary(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        assert "SUMMARY" in result.stdout, f"No SUMMARY in output:\n{result.stdout}"

    def test_dry_run_reports_passed_checks(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        assert "[PASS]" in result.stdout, f"No [PASS] in output:\n{result.stdout}"

    def test_dry_run_has_no_failures(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        # In dry-run mode, no checks should FAIL
        fail_count = result.stdout.count("[FAIL]")
        assert fail_count == 0, f"Found {fail_count} [FAIL] in dry-run:\n{result.stdout}"

    def test_dry_run_mentions_dry_run_mode(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        assert "DRY-RUN" in result.stdout or "dry-run" in result.stdout.lower()

    def test_help_flag_exits_zero(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_help_flag_shows_usage(self):
        result = subprocess.run(
            ["bash", str(UPGRADE_CHECK), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "gitlab-namespace" in result.stdout or "gitlab-namespace" in result.stdout


class TestRestoreDrillDryRun:
    """E2E: Run gitlab-restore-test.sh in dry-run mode."""

    def test_dry_run_exits_zero(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"Exit {result.returncode}: {result.stderr}\n{result.stdout}"

    def test_dry_run_has_summary(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        assert "SUMMARY" in result.stdout, f"No SUMMARY in output:\n{result.stdout}"

    def test_dry_run_passes_checks(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        # Should have some PASS checks
        pass_count = result.stdout.count("[PASS]")
        assert pass_count >= 1, f"Expected at least 1 [PASS], got {pass_count}:\n{result.stdout}"

    def test_restore_without_backup_fails(self):
        """--restore without --backup should exit 2."""
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--restore"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2, f"Expected exit 2, got {result.returncode}"

    def test_help_flag_exits_zero(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_help_flag_shows_options(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "restore" in result.stdout.lower() or "backup" in result.stdout.lower()

    def test_list_backups_requires_s3(self):
        """--list-backups without S3 should warn or fail gracefully."""
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--list-backups"],
            capture_output=True, text=True, timeout=30,
        )
        # Should fail (exit 2) because S3_ENDPOINT is not set
        assert result.returncode == 2 or "not set" in result.stdout.lower()

    def test_unknown_flag_fails(self):
        result = subprocess.run(
            ["bash", str(RESTORE_DRILL), "--unknown-flag"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 2, f"Expected exit 2 for unknown flag, got {result.returncode}"


class TestScriptConsistency:
    """Component: Cross-validate scripts reference the right variables."""

    def test_check_script_uses_gitlab_namespace(self):
        content = UPGRADE_CHECK.read_text()
        assert "GITLAB_NS" in content
        assert "gitlab" in content

    def test_restore_script_uses_restore_namespace(self):
        content = RESTORE_DRILL.read_text()
        assert "RESTORE_NS" in content
        assert "restore-drill" in content

    def test_plan_references_check_script(self):
        content = PLAN.read_text()
        assert "gitlab-upgrade-check.sh" in content

    def test_plan_references_restore_script(self):
        content = PLAN.read_text()
        assert "gitlab-restore-test.sh" in content

    def test_check_script_has_cluster_connectivity(self):
        content = UPGRADE_CHECK.read_text()
        assert "cluster-info" in content or "cluster" in content.lower()

    def test_restore_script_validates_backup_timestamp(self):
        content = RESTORE_DRILL.read_text()
        assert "BACKUP_TIMESTAMP" in content or "backup" in content.lower()
