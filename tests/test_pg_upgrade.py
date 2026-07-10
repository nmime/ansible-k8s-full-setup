"""
Tests for PG Operator 2→3 upgrade: plan, preflight checks, restore drill.

Layers:
  - Unit:    file existence, syntax, shebangs, required sections / flags.
  - Component: logical completeness (check categories, step ordering, cross-file refs).
  - E2E:     dry-run execution of both shell scripts (no cluster required).
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
SCRIPTS = REPO_ROOT / "scripts"

PLAN = DOCS / "PG_OPERATOR_UPGRADE_PLAN.md"
CHECK_SH = SCRIPTS / "pg-upgrade-check.sh"
DRILL_SH = SCRIPTS / "pg-restore-drill.sh"


# ================================================================
#  UNIT TESTS
# ================================================================

class TestPlanStructure:
    """PG_OPERATOR_UPGRADE_PLAN.md — existence and required sections."""

    @pytest.fixture(autouse=True)
    def load(self):
        self.text = PLAN.read_text()

    def test_file_exists(self):
        assert PLAN.is_file()

    def test_substantial(self):
        assert len(self.text) > 8000

    def test_current_and_target_versions(self):
        assert "2.8.2" in self.text
        assert "3.0.0" in self.text

    def test_crd_version_change(self):
        assert "postgresql.percona.com/v1" in self.text
        assert "postgresql.percona.com/v2" in self.text

    def test_complete_rewrite_mentioned(self):
        assert "complete rewrite" in self.text.lower()

    def test_prerequisites_section(self):
        assert "Prerequisites" in self.text

    def test_pgbackrest_backup_prerequisite(self):
        assert "pgBackRest" in self.text

    def test_replica_lag_prerequisite(self):
        assert "replica" in self.text.lower() and "lag" in self.text.lower()

    def test_s3_prerequisite(self):
        assert "S3" in self.text

    def test_disk_space_prerequisite(self):
        assert "disk" in self.text.lower() and "space" in self.text.lower()

    def test_pgbouncer_inventory(self):
        assert "PgBouncer" in self.text and "connection" in self.text.lower()

    def test_migration_phases(self):
        for i in range(1, 6):
            assert f"Phase {i}" in self.text, f"Phase {i} missing"

    def test_backup_phase(self):
        assert "Backup" in self.text

    def test_staging_phase(self):
        assert "Staging" in self.text

    def test_deploy_phase(self):
        assert "Deploy" in self.text

    def test_cutover_phase(self):
        assert "Cutover" in self.text

    def test_decommission_phase(self):
        assert "Decommission" in self.text

    def test_rollback_section(self):
        assert "Rollback" in self.text or "rollback" in self.text

    def test_rollback_from_backup(self):
        assert "restore" in self.text.lower() and "backup" in self.text.lower()

    def test_pgbouncer_migration(self):
        assert "PgBouncer" in self.text

    def test_risk_assessment(self):
        assert "Risk" in self.text

    def test_severity_levels(self):
        t = self.text.upper()
        assert "CRITICAL" in t or "HIGH" in t
        assert "MEDIUM" in t
        assert "LOW" in t

    def test_downtime_estimate(self):
        assert "downtime" in self.text.lower()

    def test_checklist(self):
        assert "- [ ]" in self.text

    def test_communication_plan(self):
        assert "Communication" in self.text

    def test_emergency_restore(self):
        assert "Emergency" in self.text or "emergency" in self.text

    def test_do_not_change_role(self):
        assert "Do **not** modify" in self.text or "do not change" in self.text.lower()

    def test_references_ansible_role(self):
        assert "roles/k8s-databases" in self.text

    def test_references_preflight_script(self):
        assert "pg-upgrade-check.sh" in self.text

    def test_references_restore_drill(self):
        assert "pg-restore-drill.sh" in self.text


class TestCheckScriptUnit:
    """pg-upgrade-check.sh — syntax and required structure."""

    @pytest.fixture(autouse=True)
    def load(self):
        self.text = CHECK_SH.read_text()

    def test_exists(self):
        assert CHECK_SH.is_file()

    def test_executable(self):
        assert os.access(CHECK_SH, os.X_OK)

    def test_shebang(self):
        assert self.text.startswith("#!/usr/bin/env bash")

    def test_syntax(self):
        r = subprocess.run(["bash", "-n", str(CHECK_SH)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, f"Syntax error: {r.stderr}"

    def test_set_pipefail(self):
        assert "set -euo pipefail" in self.text

    def test_dry_run_flag(self):
        assert "--dry-run" in self.text

    def test_help_flag(self):
        assert "--help" in self.text

    def test_pg_namespace_flag(self):
        assert "--pg-namespace" in self.text

    def test_pg_cluster_flag(self):
        assert "--pg-cluster" in self.text

    def test_s3_endpoint_flag(self):
        assert "--s3-endpoint" in self.text

    def test_s3_bucket_flag(self):
        assert "--s3-bucket" in self.text

    def test_backup_max_age_flag(self):
        assert "--backup-max-age" in self.text

    def test_default_namespace(self):
        assert 'PG_NS="databases"' in self.text

    def test_default_cluster(self):
        assert 'PG_CLUSTER="postgres-operator"' in self.text

    def test_colour_helpers(self):
        assert "RED=" in self.text and "GREEN=" in self.text

    def test_counters(self):
        for c in ("PASS_COUNT", "FAIL_COUNT", "WARN_COUNT"):
            assert c in self.text

    def test_exit_codes(self):
        assert "exit 0" in self.text
        assert "exit 1" in self.text

    def test_summary(self):
        assert "SUMMARY" in self.text

    def test_section_count(self):
        sections = re.findall(r'section\s+"?\d+\.', self.text)
        assert len(sections) >= 8, f"Expected ≥8 sections, got {len(sections)}"

    def test_checks_operator_version(self):
        assert "PG Operator" in self.text or "operator" in self.text.lower()

    def test_checks_cr(self):
        assert "PostgresCluster" in self.text

    def test_checks_primary_pod(self):
        assert "primary" in self.text.lower() and "Running" in self.text

    def test_checks_replica_lag(self):
        assert "pg_stat_replication" in self.text

    def test_checks_backup(self):
        assert "pgBackRest" in self.text or "pgbackrest" in self.text

    def test_checks_s3(self):
        assert "S3" in self.text or "s3://" in self.text

    def test_checks_disk(self):
        assert "disk" in self.text.lower() and "space" in self.text.lower()

    def test_checks_pgbouncer(self):
        assert "PgBouncer" in self.text or "pgbouncer" in self.text

    def test_checks_chart_availability(self):
        assert "helm search" in self.text or "helm repo" in self.text

    def test_dry_run_blocks(self):
        dry_runs = re.findall(r'DRY_RUN.*?true.*?skipped', self.text, re.IGNORECASE | re.DOTALL)
        assert len(dry_runs) >= 5, f"Expected ≥5 DRY_RUN skips, got {len(dry_runs)}"


class TestDrillScriptUnit:
    """pg-restore-drill.sh — syntax and required structure."""

    @pytest.fixture(autouse=True)
    def load(self):
        self.text = DRILL_SH.read_text()

    def test_exists(self):
        assert DRILL_SH.is_file()

    def test_executable(self):
        assert os.access(DRILL_SH, os.X_OK)

    def test_shebang(self):
        assert self.text.startswith("#!/usr/bin/env bash")

    def test_syntax(self):
        r = subprocess.run(["bash", "-n", str(DRILL_SH)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, f"Syntax error: {r.stderr}"

    def test_set_pipefail(self):
        assert "set -euo pipefail" in self.text

    def test_dry_run_flag(self):
        assert "--dry-run" in self.text

    def test_help_flag(self):
        assert "--help" in self.text

    def test_namespace_flag(self):
        assert "--namespace" in self.text

    def test_ttl_flag(self):
        assert "--ttl-hours" in self.text

    def test_skip_cleanup_flag(self):
        assert "--skip-cleanup" in self.text

    def test_operator_version_flag(self):
        assert "--operator-version" in self.text

    def test_backup_set_flag(self):
        assert "--backup-set" in self.text

    def test_colour_helpers(self):
        assert "RED=" in self.text and "GREEN=" in self.text

    def test_drill_counters(self):
        assert "DRILL_PASS" in self.text and "DRILL_FAIL" in self.text

    def test_step_count(self):
        steps = re.findall(r'Step \d+', self.text)
        assert len(steps) >= 7, f"Expected ≥7 steps, got {len(steps)}"

    def test_creates_namespace(self):
        assert "create ns" in self.text or "create namespace" in self.text

    def test_resource_quota(self):
        assert "ResourceQuota" in self.text

    def test_auto_cleanup_cronjob(self):
        assert "CronJob" in self.text

    def test_deploys_operator(self):
        assert "helm install" in self.text and "percona/pg-operator" in self.text

    def test_copies_credentials(self):
        assert "pgbackrest" in self.text.lower() and "credential" in self.text.lower()

    def test_v2_cluster_spec(self):
        assert "postgresql.percona.com/v2" in self.text
        assert "PostgresCluster" in self.text

    def test_restore_enabled(self):
        assert "restore:" in self.text and "enabled: true" in self.text

    def test_data_integrity_checks(self):
        for kw in ("database", "table", "extension", "version"):
            assert kw in self.text.lower(), f"Missing integrity check: {kw}"

    def test_replication_check(self):
        assert "replication" in self.text.lower()

    def test_connectivity_check(self):
        assert "connectivity" in self.text.lower()

    def test_postgresql_version(self):
        assert "18" in self.text

    def test_exit_codes(self):
        assert "exit 0" in self.text
        assert "exit 1" in self.text

    def test_cleanup_temp_file(self):
        assert "rm -f" in self.text

    def test_dry_run_plan(self):
        assert "execution plan" in self.text.lower() or "DRY-RUN" in self.text


# ================================================================
#  COMPONENT TESTS
# ================================================================

class TestCheckScriptCategories:
    """Verify pg-upgrade-check.sh covers all required check categories."""

    @pytest.fixture
    def text(self):
        return CHECK_SH.read_text()

    def test_all_categories_present(self, text):
        required = [
            "tooling", "PG Operator", "PostgresCluster", "primary",
            "replica", "pgBackRest", "S3", "disk", "PgBouncer", "chart",
        ]
        lower = text.lower()
        for kw in required:
            assert kw.lower() in lower, f"Missing check category: {kw}"

    def test_replica_lag_query(self, text):
        assert "pg_wal_lsn_diff" in text or "pg_stat_replication" in text

    def test_default_values(self, text):
        assert "PG_NS=\"databases\"" in text
        assert "PG_CLUSTER=\"postgres-operator\"" in text
        assert "BACKUP_MAX_AGE=24" in text


class TestDrillScriptStepOrder:
    """Verify restore drill steps are in logical order."""

    @pytest.fixture
    def text(self):
        return DRILL_SH.read_text()

    def test_step_ordering(self, text):
        order = [
            "Step 0: Prerequisites",
            "Step 1: Isolated Namespace",
            "Step 2: Deploy PG Operator",
            "Step 3: pgBackRest Credentials",
            "Step 4: Deploy v2",
            "Step 5: Data Integrity",
            "Step 6: Replication",
            "Step 7: Connectivity",
        ]
        positions = [text.index(s) for s in order]
        for i in range(len(positions) - 1):
            assert positions[i] < positions[i + 1], (
                f"'{order[i]}' (pos {positions[i]}) should precede "
                f"'{order[i+1]}' (pos {positions[i+1]})"
            )

    def test_resource_quota_limits(self, text):
        assert "requests.cpu" in text
        assert "requests.memory" in text
        assert "pods:" in text


class TestPlanCrossReferences:
    """Verify the plan cross-references scripts and Ansible role."""

    def test_plan_references_check_script(self):
        assert "pg-upgrade-check.sh" in PLAN.read_text()

    def test_plan_references_drill_script(self):
        assert "pg-restore-drill.sh" in PLAN.read_text()

    def test_scripts_agree_on_namespace(self):
        assert 'PG_NS="databases"' in CHECK_SH.read_text()
        assert 'PG_NS="databases"' in DRILL_SH.read_text()

    def test_scripts_agree_on_cluster(self):
        assert 'PG_CLUSTER="postgres-operator"' in CHECK_SH.read_text()
        assert 'PG_CLUSTER="postgres-operator"' in DRILL_SH.read_text()

    def test_all_mention_pgbackrest(self):
        for p in (PLAN, CHECK_SH, DRILL_SH):
            txt = p.read_text().lower()
            assert "pgbackrest" in txt, f"{p.name} missing pgBackRest reference"

    def test_no_hardcoded_secrets(self):
        for p in (CHECK_SH, DRILL_SH):
            txt = p.read_text()
            assert "password123" not in txt.lower()


# ================================================================
#  E2E TESTS (dry-run, no cluster)
# ================================================================

class TestCheckScriptDryRun:
    """pg-upgrade-check.sh --dry-run should succeed."""

    def test_exits_0(self):
        r = subprocess.run(
            [str(CHECK_SH), "--dry-run"], capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"dry-run failed: {r.stderr}\n{r.stdout}"

    def test_has_passes(self):
        r = subprocess.run(
            [str(CHECK_SH), "--dry-run"], capture_output=True, text=True, timeout=30,
        )
        assert "[PASS]" in r.stdout

    def test_no_failures(self):
        r = subprocess.run(
            [str(CHECK_SH), "--dry-run"], capture_output=True, text=True, timeout=30,
        )
        assert "[FAIL]" not in r.stdout

    def test_help_exits_0(self):
        r = subprocess.run(
            [str(CHECK_SH), "--help"], capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0
        assert "--dry-run" in r.stdout

    def test_unknown_flag_exits_2(self):
        r = subprocess.run(
            [str(CHECK_SH), "--bogus"], capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 2


class TestDrillScriptDryRun:
    """pg-restore-drill.sh --dry-run should succeed."""

    def test_exits_0(self):
        r = subprocess.run(
            [str(DRILL_SH), "--dry-run"], capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"dry-run failed: {r.stderr}\n{r.stdout}"

    def test_shows_steps(self):
        r = subprocess.run(
            [str(DRILL_SH), "--dry-run"], capture_output=True, text=True, timeout=30,
        )
        assert "execution plan" in r.stdout.lower()

    def test_shows_v2_spec(self):
        r = subprocess.run(
            [str(DRILL_SH), "--dry-run"], capture_output=True, text=True, timeout=30,
        )
        assert "PostgresCluster" in r.stdout
        assert "postgresql.percona.com/v2" in r.stdout

    def test_no_failures(self):
        r = subprocess.run(
            [str(DRILL_SH), "--dry-run"], capture_output=True, text=True, timeout=30,
        )
        assert "[FAIL]" not in r.stdout

    def test_help_exits_0(self):
        r = subprocess.run(
            [str(DRILL_SH), "--help"], capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0

    def test_unknown_flag_exits_2(self):
        r = subprocess.run(
            [str(DRILL_SH), "--bogus"], capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 2
