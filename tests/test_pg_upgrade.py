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
        assert len(self.text) > 5000

    def test_current_operator_version(self):
        assert "3.0.0" in self.text

    def test_current_resource_contract(self):
        assert "pgv2.percona.com/v2" in self.text
        assert "PerconaPGCluster" in self.text
        assert "postgresql.percona.com/v2" in self.text
        assert "PostgresCluster" in self.text

    def test_rejects_obsolete_backup_fields(self):
        assert "configuration" in self.text
        assert "repo2-path" in self.text
        assert "repoConfiguration" in self.text
        assert "s3.keyPrefix" in self.text

    def test_preflight_section(self):
        assert "Preflight" in self.text

    def test_pgbackrest_backup_prerequisite(self):
        assert "pgBackRest" in self.text

    def test_replica_lag_prerequisite(self):
        assert "replica" in self.text.lower() and "lag" in self.text.lower()

    def test_s3_prerequisite(self):
        assert "S3" in self.text

    def test_capacity_prerequisite(self):
        assert "PVC capacity" in self.text

    def test_pgbouncer_inventory(self):
        assert "PgBouncer" in self.text and "connectivity" in self.text.lower()

    def test_operational_sections(self):
        for section in (
            "Safety rules",
            "Restore drill",
            "Operator chart upgrade",
            "In-place data restore",
            "Rollback",
            "Completion checklist",
        ):
            assert section in self.text, f"{section} missing"

    def test_rollback_section(self):
        assert "Rollback" in self.text or "rollback" in self.text

    def test_rollback_from_backup(self):
        assert "restore" in self.text.lower() and "backup" in self.text.lower()

    def test_quiesced_cutover(self):
        assert "quiesced" in self.text.lower()
        assert "cutover" in self.text.lower()

    def test_checklist(self):
        assert "- [ ]" in self.text

    def test_helm_rollback_is_not_data_recovery(self):
        assert "A Helm rollback does not roll database data back" in self.text

    def test_source_resources_are_preserved(self):
        assert "Never delete the source cluster" in self.text

    def test_references_preflight_script(self):
        assert "pg-upgrade-check.sh" in self.text

    def test_references_restore_drill(self):
        assert "pg-restore-drill.sh" in self.text


class TestAnsibleRoleVersion:
    """Verify Ansible role has been updated for PG Operator 3.0.0."""

    @pytest.fixture(autouse=True)
    def load(self):
        defaults_path = REPO_ROOT / "defaults" / "main.yml"
        tasks_path = REPO_ROOT / "roles" / "k8s-databases" / "tasks" / "main.yml"
        self.defaults_text = defaults_path.read_text()
        self.tasks_text = tasks_path.read_text()

    def test_defaults_has_pg_operator_version(self):
        assert 'pg_operator_version' in self.defaults_text
        assert '"3.0.0"' in self.defaults_text

    def test_tasks_uses_pg_operator_version(self):
        assert 'pg_operator_version' in self.tasks_text or 'pg_operator_ver' in self.tasks_text

    def test_tasks_cr_version_3(self):
        # The tasks should reference version 3.0.0 as default
        assert '"3.0.0"' in self.tasks_text or '3.0.0' in self.tasks_text

    def test_cr_has_v3_labels(self):
        assert 'app.kubernetes.io/managed-by' in self.tasks_text

    def test_cr_omits_ignored_custom_libraries(self):
        assert 'customLibraries' not in self.tasks_text

    def test_pgbouncer_current_global_config(self):
        # Operator 3.x passes PgBouncer's native snake_case keys under
        # proxy.pgBouncer.config.global.
        assert 'global:' in self.tasks_text
        assert 'pool_mode:' in self.tasks_text
        assert 'max_client_conn:' in self.tasks_text
        assert 'default_pool_size:' in self.tasks_text

    def test_backup_repo_configuration(self):
        assert 'configuration:' in self.tasks_text
        assert 'repoConfiguration' not in self.tasks_text

    def test_s3_key_prefix(self):
        assert 'repo2-path' in self.tasks_text
        assert 'keyPrefix' not in self.tasks_text

    def test_no_ignored_camel_case_pgbouncer_config(self):
        assert 'poolMode:' not in self.tasks_text
        assert 'maxClientConn:' not in self.tasks_text
        assert 'defaultPoolSize:' not in self.tasks_text

    def test_postgresql_and_pgbouncer_replicas_use_hard_node_anti_affinity(self):
        assert self.tasks_text.count(
            'requiredDuringSchedulingIgnoredDuringExecution:'
        ) >= 2
        assert 'postgres-operator.crunchydata.com/cluster:' in self.tasks_text
        assert 'postgres-operator.crunchydata.com/data: postgres' in self.tasks_text
        assert 'postgres-operator.crunchydata.com/role: pgbouncer' in self.tasks_text

    def test_mongodb_pmm3_token_is_written_to_the_operator_source_secret(self):
        assert 'users: percona-server-mongodb-users' in self.tasks_text
        assert 'Patch MongoDB users source secret with PMM_SERVER_TOKEN' in self.tasks_text
        assert 'PMM_SERVER_TOKEN:' in self.tasks_text
        assert 'percona/pmm-client:3.8.1' in self.tasks_text
        assert 'Remove obsolete PMM 2 keys from the MongoDB users source secret' in self.tasks_text
        assert 'PMM_SERVER_API_KEY: null' in self.tasks_text
        assert "name: internal-{{ project_name | default('k8s') }}-mongo-users" not in self.tasks_text

    def test_postgresql_wait_requires_all_data_and_proxy_replicas(self):
        assert 'status.postgres.ready' in self.tasks_text
        assert 'status.pgbouncer.ready' in self.tasks_text
        assert 'Discover unscheduled PostgreSQL pods left from an obsolete template' in self.tasks_text

    def test_database_pmm3_network_path_is_explicitly_allowed(self):
        assert 'allow-pmm-egress' in self.tasks_text
        assert 'allow-pmm-database-ingress' in self.tasks_text
        assert "k8s:app: pmm-server" in self.tasks_text
        assert "port: '8443'" in self.tasks_text


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
        assert 'PG_CLUSTER="k8s-pg"' in self.text

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
        assert len(sections) >= 10, f"Expected ≥10 sections, got {len(sections)}"

    def test_checks_operator_version(self):
        assert "PG Operator" in self.text or "operator" in self.text.lower()

    def test_checks_version_3_0_0(self):
        assert "3.0.0" in self.text or "3.0" in self.text

    def test_checks_cr(self):
        assert "PerconaPGCluster" in self.text

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

    def test_checks_v3_chart(self):
        assert "3.0" in self.text

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

    def test_disposable_storage_size_is_configurable(self):
        assert "--storage-size" in self.text
        assert "storage: ${STORAGE_SIZE}" in self.text

    def test_restore_images_are_inherited_from_the_source_cluster(self):
        assert "SOURCE_JSON=$(kubectl get perconapgcluster" in self.text
        assert ".spec.image // $fallback" in self.text
        assert ".spec.backups.pgbackrest.image // $fallback" in self.text
        assert "image: ${POSTGRES_IMAGE}" in self.text
        assert "image: ${PGBACKREST_IMAGE}" in self.text

    def test_cleanup_keeps_operator_alive_until_database_finalizers_are_removed(self):
        cleanup = self.text.split("cleanup_drill()", 1)[1].split("cleanup_on_exit()", 1)[0]
        assert cleanup.index("kubectl delete perconapgcluster") < cleanup.index("helm uninstall")
        assert cleanup.index("kubectl wait --for=delete postgrescluster") < cleanup.index(
            "helm uninstall"
        )
        assert "kubectl get perconapgbackup" in cleanup
        assert "--timeout=2m" in cleanup
        assert "This force-finalization is scoped only to the drill namespace" in cleanup
        assert cleanup.index("helm uninstall") < cleanup.index("kubectl delete namespace")

    def test_v3_primary_and_generated_user_credentials_are_used(self):
        assert "postgres-operator.crunchydata.com/role=primary" in self.text
        assert "postgres-operator.crunchydata.com/role=pguser" in self.text
        assert "pguser-postgres" not in self.text

    def test_quota_does_not_reject_operator_generated_restore_containers(self):
        quota = self.text.split("kind: ResourceQuota", 1)[1].split("EOF", 1)[0]
        assert "requests.storage" in quota
        assert "requests.cpu" not in quota
        assert "limits.memory" not in quota

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
        assert "kubectl delete ns" in self.text

    def test_deploys_operator(self):
        assert "helm install" in self.text and "percona/pg-operator" in self.text

    def test_copies_credentials(self):
        assert "pgbackrest" in self.text.lower() and "credential" in self.text.lower()

    def test_v2_cluster_spec(self):
        assert "pgv2.percona.com/v2" in self.text
        assert "PerconaPGCluster" in self.text

    def test_restore_enabled(self):
        assert "dataSource:" in self.text and "pgbackrest:" in self.text

    def test_data_integrity_checks(self):
        for kw in ("database", "table", "extension", "version"):
            assert kw in self.text.lower(), f"Missing integrity check: {kw}"

    def test_replication_check(self):
        assert "replication" in self.text.lower()
        assert "No streaming replica became available within 10m" in self.text

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
            "tooling", "PG Operator", "PerconaPGCluster", "primary",
            "replica", "pgBackRest", "S3", "disk", "PgBouncer", "chart",
        ]
        lower = text.lower()
        for kw in required:
            assert kw.lower() in lower, f"Missing check category: {kw}"

    def test_replica_lag_query(self, text):
        assert "pg_wal_lsn_diff" in text or "pg_stat_replication" in text

    def test_default_values(self, text):
        assert "PG_NS=\"databases\"" in text
        assert "PG_CLUSTER=\"k8s-pg\"" in text
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
            "Step 4: Deploy PerconaPGCluster",
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
        assert "requests.storage" in text
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
        assert 'PG_CLUSTER="k8s-pg"' in CHECK_SH.read_text()
        assert 'PG_CLUSTER="k8s-pg"' in DRILL_SH.read_text()

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


class TestVersionConsistency:
    """Verify version consistency across all files."""

    def test_defaults_matches_tasks(self):
        defaults_text = (REPO_ROOT / "defaults" / "main.yml").read_text()
        tasks_text = (REPO_ROOT / "roles" / "k8s-databases" / "tasks" / "main.yml").read_text()
        # Both should reference 3.0.0
        assert "3.0.0" in defaults_text
        assert "3.0.0" in tasks_text

    def test_drill_script_default_version(self):
        drill_text = DRILL_SH.read_text()
        assert "3.0.0" in drill_text


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
        assert "PerconaPGCluster" in r.stdout
        assert "pgv2.percona.com/v2" in r.stdout

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


def test_production_database_replicas_can_use_reserved_control_plane_capacity():
    tasks = (REPO_ROOT / "roles" / "k8s-databases" / "tasks" / "main.yml").read_text()
    assert tasks.count('node-role.kubernetes.io/control-plane') >= 2
    assert tasks.count('if tier == "production" else []') >= 2
