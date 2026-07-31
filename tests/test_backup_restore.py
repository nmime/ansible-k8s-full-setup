"""Test suite for backup-restore Ansible role and scripts."""
import shutil, subprocess, yaml
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLE_DIR = REPO_ROOT / "roles" / "backup-restore"
TASKS_DIR = ROLE_DIR / "tasks"
DEFAULTS_FILE = ROLE_DIR / "defaults" / "main.yml"
PROJECT_DEFAULTS = REPO_ROOT / "defaults" / "main.yml"
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup-all.sh"
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore-drill.sh"
VAULT_RESTORE_SCRIPT = REPO_ROOT / "scripts" / "vault-restore-drill.sh"
MONGODB_RESTORE_SCRIPT = REPO_ROOT / "scripts" / "mongodb-restore-drill.sh"
SEAWEEDFS_RESTORE_SCRIPT = REPO_ROOT / "scripts" / "seaweedfs-restore-drill.sh"
BACKUP_DOC = REPO_ROOT / "BACKUP_RESTORE.md"

def load_yaml(path):
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    return docs[0] if len(docs) == 1 else docs

class TestRoleStructure:
    def test_role_directory_exists(self): assert ROLE_DIR.is_dir()
    def test_defaults_main_exists(self): assert DEFAULTS_FILE.is_file()
    def test_tasks_main_exists(self): assert (TASKS_DIR / "main.yml").is_file()
    def test_mongodb_task_exists(self): assert (TASKS_DIR / "mongodb_pbm.yml").is_file()
    def test_postgresql_task_exists(self): assert (TASKS_DIR / "postgresql_pgbackrest.yml").is_file()
    def test_vault_task_exists(self): assert (TASKS_DIR / "vault_raft.yml").is_file()
    def test_seaweedfs_task_exists(self): assert (TASKS_DIR / "seaweedfs.yml").is_file()
    def test_gitlab_task_exists(self): assert (TASKS_DIR / "gitlab.yml").is_file()
    def test_verification_task_exists(self): assert (TASKS_DIR / "verification.yml").is_file()
    def test_alerts_task_exists(self): assert (TASKS_DIR / "alerts.yml").is_file()
    def test_readme_exists(self): assert (ROLE_DIR / "README.md").is_file()

class TestVariableDefaults:
    @pytest.fixture(autouse=True)
    def _d(self): self.d = load_yaml(DEFAULTS_FILE)
    def test_storage_type(self): assert self.d["backup_storage_type"] == "s3"
    def test_schedule(self): assert "0 2 * * *" in str(self.d["backup_schedule"])
    def test_namespace(self): assert self.d["backup_namespace"] == "backups"
    def test_mongo_on(self): assert self.d["backup_mongodb_enabled"] is True
    def test_postgresql_on(self): assert self.d["backup_postgresql_enabled"] is True
    def test_vault_on(self): assert self.d["backup_vault_enabled"] is True
    def test_sw_on(self): assert self.d["backup_seaweedfs_enabled"] is True
    def test_gl_on(self): assert self.d["backup_gitlab_enabled"] is True
    def test_verify(self): assert self.d["backup_verify_all"] is True
    def test_alert_off(self): assert self.d["backup_alert_enabled"] is False
    def test_webhook_empty(self): assert self.d["backup_alert_webhook_url"] == ""
    def test_restore_ns(self): assert self.d["restore_drill_namespace"] == "restore-drill"
    def test_restore_cleanup(self): assert self.d["restore_drill_auto_cleanup"] is True
    def test_restore_hours(self): assert self.d["restore_drill_cleanup_after_hours"] == 24
    def test_tz(self): assert self.d["backup_cron_timezone"] == "UTC"
    def test_concurrency(self): assert self.d["backup_cron_concurrency_policy"] == "Forbid"
    def test_resource_limits(self):
        for k in ("backup_job_cpu_request","backup_job_cpu_limit","backup_job_memory_request","backup_job_memory_limit"):
            assert k in self.d
    def test_images(self):
        for k in ("backup_alpine_image","backup_vault_image","backup_mongo_image","backup_s3cli_image"):
            assert k in self.d

class TestProjectDefaults:
    def test_schedule(self): assert "backup_schedule" in load_yaml(PROJECT_DEFAULTS)
    def test_retention(self): assert "backup_retention_days" in load_yaml(PROJECT_DEFAULTS)
    def test_bucket(self): assert "backup_storage_bucket" in load_yaml(PROJECT_DEFAULTS)
    def test_alert_vars(self):
        d = load_yaml(PROJECT_DEFAULTS)
        assert all(k in d for k in ("backup_alert_enabled","backup_alert_webhook_url","backup_alert_channel"))
    def test_restore_vars(self):
        d = load_yaml(PROJECT_DEFAULTS)
        assert all(k in d for k in ("restore_drill_namespace","restore_drill_cleanup_after_hours","restore_safety_gate_confirm_required"))

TASK_FILES = ["main.yml","postgresql_pgbackrest.yml","mongodb_pbm.yml","vault_raft.yml","seaweedfs.yml","gitlab.yml","verification.yml","alerts.yml","velero.yml"]

class TestTaskYAML:
    @pytest.mark.parametrize("f", TASK_FILES)
    def test_valid(self, f): assert load_yaml(TASKS_DIR / f) is not None

class TestMainInclusion:
    def _c(self): return (TASKS_DIR / "main.yml").read_text()
    def test_mongodb(self): assert "mongodb_pbm" in self._c()
    def test_postgresql(self): assert "postgresql_pgbackrest" in self._c()
    def test_vault(self): assert "vault_raft" in self._c()
    def test_seaweedfs(self): assert "seaweedfs" in self._c()
    def test_gitlab(self): assert "gitlab" in self._c()
    def test_verification(self): assert "verification" in self._c()
    def test_alerts(self): assert "alerts" in self._c()
    def test_facts(self):
        c = self._c()
        assert "set_fact" in c and "_backup_project" in c and "_backup_bucket" in c
    def test_namespace(self):
        c = self._c()
        assert "kind: Namespace" in c and "state: present" in c
    def test_secret(self): assert "backup-storage-credentials" in self._c()


def test_velero_upgrade_handles_controller_mutated_schedule_and_all_nodes():
    content = (TASKS_DIR / "velero.yml").read_text()
    assert "Replace the controller-mutated Helm schedule safely" in content
    assert "Detect whether the Velero Schedule CRD is installed" in content
    assert "when: backup_dr_schedule_crd.rc == 0" in content
    assert "name: velero-full-cluster" in content
    assert "node-role.kubernetes.io/control-plane" in content
    assert "node-role.kubernetes.io/master" in content
    assert "Prove node-agent coverage on every schedulable node" in content
    assert "backup_dr_velero_helm is succeeded" in content
    assert "force_conflicts: true" in content
    assert "retries: 3" in content
    assert "Classify the external disaster-recovery endpoint" in content
    assert "backup_dr_allow_literal_ip_endpoint" in content
    assert "Detect a literal endpoint collision with protected-cluster addresses" in content
    assert "kubectl get nodes,pods,services --all-namespaces -o json" in content
    assert "Allow external storage through its validated DNS name" in content
    assert "Allow an explicitly approved external storage IPv4 endpoint" in content
    assert '"{{ backup_dr_storage_hostname }}/32"' in content
    defaults = load_yaml(ROLE_DIR / "defaults" / "main.yml")
    assert "BACKUP_DR_ALLOW_LITERAL_IP" in str(
        defaults["backup_dr_allow_literal_ip_endpoint"]
    )


def test_velero_dynamic_include_propagates_tags_to_every_child_task():
    tasks = load_yaml(TASKS_DIR / "main.yml")
    include = next(
        task
        for task in tasks
        if task.get("name") == "Backup | Include full-cluster disaster-recovery tasks"
    )
    args = include["ansible.builtin.include_tasks"]
    assert args["file"] == "velero.yml"
    assert set(args["apply"]["tags"]) == {"backup", "backup-dr", "velero"}
    assert set(include["tags"]) == {"backup", "backup-dr", "velero"}


def test_velero_provider_init_container_has_complete_resource_bounds():
    defaults = yaml.safe_load(
        (REPO_ROOT / "roles" / "backup-restore" / "defaults" / "main.yml").read_text()
    )
    tasks = load_yaml(TASKS_DIR / "velero.yml")
    install = next(
        task
        for task in tasks
        if task.get("name")
        == "Backup-DR | Deploy Velero with node-agent filesystem backups"
    )
    init_containers = install["kubernetes.core.helm"]["values"]["initContainers"]
    provider = next(
        container
        for container in init_containers
        if container["name"] == "velero-plugin-for-aws"
    )

    resources = provider["resources"]
    assert set(resources) == {"requests", "limits"}
    assert set(resources["requests"]) == {"cpu", "memory"}
    assert set(resources["limits"]) == {"cpu", "memory"}
    expected = {
        "requests": {
            "cpu": "backup_dr_velero_plugin_cpu_request",
            "memory": "backup_dr_velero_plugin_memory_request",
        },
        "limits": {
            "cpu": "backup_dr_velero_plugin_cpu_limit",
            "memory": "backup_dr_velero_plugin_memory_limit",
        },
    }
    for category, bounds in expected.items():
        for resource, variable in bounds.items():
            assert variable in defaults
            assert resources[category][resource] == "{{ " + variable + " }}"


def test_velero_restore_helper_uses_explicit_numeric_non_root_identity():
    tasks = load_yaml(TASKS_DIR / "velero.yml")
    install = next(
        task
        for task in tasks
        if task.get("name")
        == "Backup-DR | Deploy Velero with node-agent filesystem backups"
    )
    values = install["kubernetes.core.helm"]["values"]
    helper = values["configMaps"]["fs-restore-action-config"]
    assert helper["labels"] == {
        "velero.io/plugin-config": "",
        "velero.io/pod-volume-restore": "RestoreItemAction",
    }
    assert helper["data"]["image"] == "docker.io/velero/velero:{{ backup_dr_velero_image_tag }}"
    sec_ctx = yaml.safe_load(helper["data"]["secCtx"])
    assert sec_ctx["runAsNonRoot"] is True
    assert sec_ctx["runAsUser"] == 1001
    assert sec_ctx["runAsGroup"] == 1001
    assert sec_ctx["allowPrivilegeEscalation"] is False
    assert sec_ctx["readOnlyRootFilesystem"] is True
    assert sec_ctx["capabilities"]["drop"] == ["ALL"]
    assert sec_ctx["seccompProfile"]["type"] == "RuntimeDefault"


def test_replacement_cluster_has_an_isolated_velero_only_bootstrap_path():
    play = load_yaml(REPO_ROOT / "playbooks" / "deploy_platform.yml")[0]
    bootstrap = next(
        task
        for task in play["post_tasks"]
        if task.get("name")
        == "Bootstrap only Velero disaster recovery on a replacement cluster"
    )
    include = bootstrap["ansible.builtin.include_role"]
    assert include["name"] == "backup-restore"
    assert include["tasks_from"] == "velero.yml"
    assert include["apply"]["tags"] == ["velero-bootstrap"]
    assert set(bootstrap["tags"]) == {"never", "velero-bootstrap"}
    assert "platform_backup_enabled | bool" in bootstrap["when"]
    assert "backup_dr_enabled | bool" in bootstrap["when"]

    normal = next(
        task
        for task in play["post_tasks"]
        if task.get("name") == "Deploy backup and restore automation"
    )
    assert "velero-bootstrap" not in normal["tags"]


def test_deployment_rechecks_velero_coverage_after_every_component():
    content = (REPO_ROOT / "playbooks" / "deploy_platform.yml").read_text()
    assert "Detect a retained Velero node-agent" in content
    assert "Prove final Velero node-agent coverage after all components" in content
    assert "desiredNumberScheduled" in content
    assert "numberUnavailable" in content
    assert "difference(['always', 'infrastructure', 'network', 'security', 'dns'])" in content


def test_database_native_schedules_follow_the_backup_selector_and_are_removable():
    databases = (
        REPO_ROOT / "roles" / "k8s-databases" / "tasks" / "main.yml"
    ).read_text()
    removal = (REPO_ROOT / "playbooks" / "remove_component.yml").read_text()
    orchestrator = (
        REPO_ROOT / "platform-orchestrator" / "platform.sh"
    ).read_text()

    assert databases.count("platform_backup_enabled | default(backup_enabled) | bool") >= 6
    assert "startingDeadlineSeconds: '{{ mongodb_backup_starting_deadline_seconds | int }}'" in databases
    defaults = yaml.safe_load((REPO_ROOT / "defaults" / "main.yml").read_text())
    assert defaults["mongodb_backup_starting_deadline_seconds"] == 1800
    assert defaults["mongodb_backup_image"] == "percona/percona-backup-mongodb:2.15.0"
    assert "image: '{{ mongodb_backup_image }}'" in databases
    assert "Read the active SeaweedFS backup identity" in databases
    assert "Reject unilateral database backup credential rotation" in databases
    assert "seaweedfs-backup-credentials" in databases
    assert (
        "_active_seaweedfs_backup_identity.resources[0].data.AWS_ACCESS_KEY_ID"
        in databases
    )
    assert (
        "_active_seaweedfs_backup_identity.resources[0].data.AWS_SECRET_ACCESS_KEY"
        in databases
    )
    assert "Remove PostgreSQL operator backup schedules" in removal
    assert 'path: "/spec/backups/pgbackrest/repos/{{ item }}/schedules"' in removal
    assert "Disable MongoDB operator backup and point-in-time recovery" in removal
    assert "path: /spec/backup/tasks" in removal
    assert "Verify database-native backup automation is disabled" in removal
    assert orchestrator.count("--tags databases,gitlab,backup") == 2

CJ = [("vault_raft.yml","vault-raft-snapshot"),
      ("seaweedfs.yml","seaweedfs-backup-check"),("gitlab.yml","gitlab-rails-secrets-backup"),
      ("verification.yml","backup-verification")]

def test_mongodb_uses_operator_backup_contract():
    content = (TASKS_DIR / "mongodb_pbm.yml").read_text()
    assert "PerconaServerMongoDB" in content
    assert "mongodb-backup" in content and "state: absent" in content

def test_postgresql_uses_operator_backup_contract():
    content = (TASKS_DIR / "postgresql_pgbackrest.yml").read_text()
    assert "PerconaPGCluster" in content
    assert "pgbackrest" in content and "repo1" in content and "repo2" in content

def test_verification_uses_bounded_recursive_object_checks_and_fails_closed():
    content = (TASKS_DIR / "verification.yml").read_text()
    assert "s3api list-objects-v2" in content
    assert "--max-items 1" in content
    assert "FAIL: unable to verify ${comp} artifacts" in content
    assert "FAIL: invalid ${comp} verification response" in content
    assert "wc -l || echo 0" not in content

class TestCronJob:
    @pytest.mark.parametrize("f,n", CJ)
    def test_kind(self, f, n): assert "kind: CronJob" in (TASKS_DIR / f).read_text()
    @pytest.mark.parametrize("f,n", CJ)
    def test_name(self, f, n): assert n in (TASKS_DIR / f).read_text()
    @pytest.mark.parametrize("f,n", CJ)
    def test_schedule(self, f, n): assert "schedule:" in (TASKS_DIR / f).read_text()
    @pytest.mark.parametrize("f,n", CJ)
    def test_concurrency(self, f, n): assert "concurrencyPolicy" in (TASKS_DIR / f).read_text()
    @pytest.mark.parametrize("f,n", CJ)
    def test_ttl(self, f, n): assert "ttlSecondsAfterFinished" in (TASKS_DIR / f).read_text()
    @pytest.mark.parametrize("f,n", CJ)
    def test_requests(self, f, n): assert "requests:" in (TASKS_DIR / f).read_text()
    @pytest.mark.parametrize("f,n", CJ)
    def test_limits(self, f, n): assert "limits:" in (TASKS_DIR / f).read_text()


def test_vault_backup_containers_use_restricted_security_contexts():
    content = (TASKS_DIR / "vault_raft.yml").read_text()
    assert content.count("allowPrivilegeEscalation: false") >= 2
    assert content.count('drop: ["ALL"]') >= 2
    assert "runAsNonRoot: true" in content


def test_vault_snapshot_failure_stops_the_uploader_without_hanging():
    content = (TASKS_DIR / "vault_raft.yml").read_text()

    assert "/backup/failed" in content
    assert "Vault snapshot container failed with exit code" in content
    assert "type: RuntimeDefault" in content
    assert 'chmod 0640 "${SF}"' in content
    assert '] && aws --endpoint-url=' not in content


def test_vault_snapshot_targets_the_active_raft_service():
    defaults = (ROLE_DIR / "defaults" / "main.yml").read_text()
    tasks = (TASKS_DIR / "vault_raft.yml").read_text()

    assert "vault-active.{{ backup_vault_namespace }}.svc.cluster.local:8200" in defaults
    assert "vault-active.vault.svc.cluster.local:8200" in tasks


def test_backup_component_detection_is_pipefail_safe_for_multiple_pods():
    content = (REPO_ROOT / "scripts" / "backup-all.sh").read_text()

    assert "jq -e '.items | length > 0'" in content
    assert "-o name 2>/dev/null | grep -q" not in content


def test_vault_raft_schedule_is_suspended_for_legacy_file_storage():
    content = (TASKS_DIR / "vault_raft.yml").read_text()
    assert "Detect active Vault storage backend" in content
    assert "vault status -format=json" in content
    assert "backup_vault_raft_snapshot_suspended" in content
    assert 'suspend: "{{ backup_vault_raft_snapshot_suspended }}"' in content


def test_seaweedfs_backup_namespace_is_rendered_by_ansible():
    content = (TASKS_DIR / "seaweedfs.yml").read_text()
    assert "seaweedfs-master.{{ backup_seaweedfs_namespace }}.svc.cluster.local" in content
    assert "${backup_seaweedfs_namespace}" not in content
    assert ":9333/dir/status" in content
    assert "/volume/topology" not in content
    assert '] && aws --endpoint-url=' not in content

class TestSecrets:
    def test_main_creds(self):
        c = (TASKS_DIR / "main.yml").read_text()
        assert "AWS_ACCESS_KEY_ID" in c and "AWS_SECRET_ACCESS_KEY" in c
    def test_vault(self): assert "vault-backup-credentials" in (TASKS_DIR / "vault_raft.yml").read_text()
    def test_sw(self): assert "seaweedfs-backup-credentials" in (TASKS_DIR / "seaweedfs.yml").read_text()
    def test_gl(self): assert "gitlab-rails-backup-credentials" in (TASKS_DIR / "gitlab.yml").read_text()
    def test_gl_toolbox(self): assert "gitlab-toolbox-backup" in (TASKS_DIR / "gitlab.yml").read_text()
    def test_alert(self):
        c = (TASKS_DIR / "alerts.yml").read_text()
        assert "backup-alert-config" in c and "WEBHOOK_URL" in c
        assert "serviceAccountName: backup-alert-check" in c
        assert "resources: [\"pods\"]" in c
        assert 'verbs: ["get", "list"]' in c
        assert "KUBERNETES_SERVICE_HOST" in c
        assert "apk add" not in c
        assert "allowPrivilegeEscalation: false" in c
        assert 'drop: ["ALL"]' in c

class TestBackupScript:
    def test_exists(self): assert BACKUP_SCRIPT.is_file()
    def test_shebang(self): assert BACKUP_SCRIPT.read_text().startswith("#!")
    def test_syntax(self):
        r = subprocess.run(["bash","-n",str(BACKUP_SCRIPT)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stderr
    def test_flags(self):
        c = BACKUP_SCRIPT.read_text()
        for f in ("--help","--dry-run","--force","--component"): assert f in c
    def test_kubectl_gate(self): assert "kubectl cluster-info" in BACKUP_SCRIPT.read_text()
    def test_storage_probe_is_restricted(self):
        c = BACKUP_SCRIPT.read_text()
        assert "probe_overrides=" in c
        assert "allowPrivilegeEscalation:false" in c
    def test_postgresql_gate_defaults_to_object_storage_repo(self):
        c = BACKUP_SCRIPT.read_text()
        assert "PROJECT_NAME=$(yq -r '.global.project" in c
        assert 'global.project is required in $CONFIG_FILE' in c
        assert 'BACKUP_POSTGRESQL_REPO:-repo2' in c
        assert 'repoName: ${repo}' in c
        assert "FAIL-missing-${repo}" in c
        assert 'drop:["ALL"]' in c
        assert "seccompProfile" in c
        assert "runAsUser:100" in c
        assert 'BACKUP_POSTGRESQL_TIMEOUT_SECONDS:-1800' in c
        assert "-o jsonpath='{.status.jobName}'" in c
        assert '.type == "Failed" and .status == "True"' in c
        assert 'kubectl logs "job/${job_name}"' in c
    def test_gitlab_gate_rejects_silently_skipped_buckets(self):
        c = BACKUP_SCRIPT.read_text()
        assert "GitLab Toolbox completed after skipping" in c
        assert "FAIL-incomplete" in c
        assert "Unable to check existence of bucket" in c
    def test_confirm_gate(self):
        c = BACKUP_SCRIPT.read_text()
        assert "read" in c and "yes" in c.lower()
    def test_components(self):
        c = BACKUP_SCRIPT.read_text()
        for x in ("postgresql","mongodb","vault","seaweedfs","gitlab"): assert x in c
    def test_file_backed_vault_fallback_is_explicit_and_opt_in(self):
        c = BACKUP_SCRIPT.read_text()
        assert "BACKUP_ALLOW_VELERO_VAULT_FALLBACK" in c
        assert "vault: VELERO-FALLBACK" in c
        assert "native backup requires integrated Raft" in c
    def test_summary(self): assert "SUMMARY" in BACKUP_SCRIPT.read_text()
    def test_parallel_result_logs_are_project_and_process_unique(self):
        c = BACKUP_SCRIPT.read_text()
        assert "RESULT_PROJECT=" in c
        assert '.backup-results-${RESULT_PROJECT}-${TS}-$$.log' in c
        assert '.backup-results-${TS}.log' not in c

class TestRestoreScript:
    def test_exists(self): assert RESTORE_SCRIPT.is_file()
    def test_shebang(self): assert RESTORE_SCRIPT.read_text().startswith("#!")
    def test_syntax(self):
        r = subprocess.run(["bash","-n",str(RESTORE_SCRIPT)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stderr
    def test_component(self):
        c = RESTORE_SCRIPT.read_text()
        assert "--component" in c and "COMPONENT" in c
    def test_backup(self): assert "--backup" in RESTORE_SCRIPT.read_text()
    def test_force_dryrun(self):
        c = RESTORE_SCRIPT.read_text()
        assert "FORCE" in c and "DRY_RUN" in c
    def test_namespace(self): assert "restore-drill" in RESTORE_SCRIPT.read_text()
    def test_quota(self):
        assert "ResourceQuota" in (REPO_ROOT / "scripts" / "vault-restore-drill.sh").read_text()
        assert "ResourceQuota" in (REPO_ROOT / "scripts" / "pg-restore-drill.sh").read_text()
        assert "ResourceQuota" in MONGODB_RESTORE_SCRIPT.read_text()
        assert "ResourceQuota" in SEAWEEDFS_RESTORE_SCRIPT.read_text()
    def test_cleanup(self): assert "cleanup" in RESTORE_SCRIPT.read_text().lower()
    def test_components(self):
        c = RESTORE_SCRIPT.read_text()
        for x in ("mongodb","vault","seaweedfs","gitlab"): assert x in c
    def test_summary(self): assert "SUMMARY" in RESTORE_SCRIPT.read_text()
    def test_dispatcher_is_bash_32_nounset_safe(self):
        c = RESTORE_SCRIPT.read_text()
        assert "common_args=(" not in c
        assert 'set -- "$@"' in c
        assert '"${common_args[@]}"' not in c
    def test_dispatcher_executes_with_no_optional_arguments(self, tmp_path):
        dispatcher = tmp_path / "restore-drill.sh"
        shutil.copy2(RESTORE_SCRIPT, dispatcher)
        (tmp_path / "load-project-env.sh").write_text("#!/usr/bin/env bash\n")
        for script in (
            "pg-restore-drill.sh",
            "mongodb-restore-drill.sh",
            "vault-restore-drill.sh",
            "gitlab-restore-test.sh",
            "seaweedfs-restore-drill.sh",
        ):
            target = tmp_path / script
            target.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
            target.chmod(0o755)
        result = subprocess.run(
            ["bash", str(dispatcher), "--component", "vault", "--backup", "snapshot.snap", "--force"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[-2:] == ["--snapshot-name", "snapshot.snap"]
    def test_mongodb_dispatch(self):
        c = RESTORE_SCRIPT.read_text()
        assert "mongodb-restore-drill.sh" in c
        assert MONGODB_RESTORE_SCRIPT.is_file()
        # The cluster-wide CRDs are reconciled by the platform role. A
        # namespace-scoped drill operator must reuse them instead of trying to
        # take field ownership from Ansible.
        mongodb = MONGODB_RESTORE_SCRIPT.read_text()
        assert "--skip-crds" in mongodb
        assert 'OPERATOR_VERSION="1.23.0"' in mongodb
        assert 'PBM_IMAGE="percona/percona-backup-mongodb:2.15.0"' in mongodb
        assert "--pbm-image" in mongodb
        assert ".spec.backup.image = $pbm_image" in mongodb
        assert 'PBM_MEMORY_LIMIT="2Gi"' in mongodb
        assert "--pbm-memory-limit" in mongodb
        assert ".spec.backup.resources.requests.memory = \"256Mi\"" in mongodb
        assert ".spec.backup.resources.limits.memory = $pbm_memory_limit" in mongodb
        assert 'STORAGE_CLASS="hcloud-volumes"' in mongodb
        assert "--storage-class" in mongodb
        assert ".volumeSpec.persistentVolumeClaim.storageClassName = $storage_class" in mongodb
        assert "s3: ($storage.s3 + {credentialsSecret: $credential})" in mongodb
        assert "storageName: $storage_name" not in mongodb
        assert "backupSource: $source" in mongodb
        assert 'TARGET_USERS_SECRET="restore-${TARGET_CLUSTER}-users"' in mongodb
        assert 'TARGET_USERS_SECRET="internal-${TARGET_CLUSTER}-users"' not in mongodb
        assert "copy_users_secret" in mongodb
        assert "source users secret does not contain the canonical PSMDB system-user keys" in mongodb
        assert 'MONGODB_DATABASE_ADMIN_PASSWORD",' in mongodb
        assert "wait_for_backup_agent_stability" in mongodb
        assert "pbm status" in mongodb
        assert "restartCount" in mongodb
        assert "PBM backup-agent did not become stable" in mongodb
        assert "wait_for_restore_completion" in mongodb
        assert "backup-agent restarted or became unready during restore" in mongodb
        assert "lastState.terminated.reason" in mongodb
    def test_postgresql_dispatch(self):
        c = RESTORE_SCRIPT.read_text()
        assert "pg-restore-drill.sh" in c
        assert "--backup-set" in c
        assert '--project' in c
        assert 'PG_CLUSTER="${PROJECT}-pg"' in c
        assert '--pg-cluster "$PG_CLUSTER"' in c
    def test_postgresql_dispatch_passes_project_cluster(self, tmp_path):
        dispatcher = tmp_path / "restore-drill.sh"
        shutil.copy2(RESTORE_SCRIPT, dispatcher)
        (tmp_path / "load-project-env.sh").write_text("#!/usr/bin/env bash\n")
        target = tmp_path / "pg-restore-drill.sh"
        target.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
        target.chmod(0o755)
        result = subprocess.run(
            [
                "bash", str(dispatcher), "--component", "postgresql",
                "--backup", "20260721-020042F", "--project", "load5-minimal",
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[-4:] == [
            "--backup-set", "20260721-020042F",
            "--pg-cluster", "load5-minimal-pg",
        ]
    def test_postgresql_exact_backup_set_contract(self):
        c = (REPO_ROOT / "scripts" / "pg-restore-drill.sh").read_text()
        assert '.status.backupName == $set' in c
        assert '.spec.repoName == "repo2"' in c
        assert "--set=%s" in c
        assert "specific --backup-set is not supported" not in c
    def test_seaweedfs_dispatch(self):
        c = RESTORE_SCRIPT.read_text()
        assert "seaweedfs-restore-drill.sh" in c
        assert SEAWEEDFS_RESTORE_SCRIPT.is_file()
    def test_seaweedfs_syntax(self):
        r = subprocess.run(["bash", "-n", str(SEAWEEDFS_RESTORE_SCRIPT)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stderr
    def test_seaweedfs_restore_contract(self):
        c = SEAWEEDFS_RESTORE_SCRIPT.read_text()
        for required in (
            "PodVolumeBackup",
            "PodVolumeRestore",
            "snapshotID",
            "restore-wait",
            "existingResourcePolicy: none",
            "restorePVs: false",
            "kind: NetworkPolicy",
            "automountServiceAccountToken: false",
            "readOnly: true",
            "allowPrivilegeEscalation: false",
            'capabilities:',
            'drop: ["ALL"]',
            "readOnlyRootFilesystem: true",
            "requests.storage",
            "allowed_warning_count",
        ):
            assert required in c
        assert "pvr_count=$(jq '.items | length'" in c
        assert '[[ "$pvr_count" -eq 1 ]]' in c
        assert '[[ "$restore_errors" -eq 0 ]]' in c
        assert '"$pvr_bytes" -eq "$backup_bytes"' in c
        assert "--skip-cleanup" in c
    def test_mongodb_syntax(self):
        r = subprocess.run(["bash", "-n", str(MONGODB_RESTORE_SCRIPT)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stderr
    def test_mongodb_restore_contract(self):
        c = MONGODB_RESTORE_SCRIPT.read_text()
        assert "PerconaServerMongoDBRestore" in c
        assert "backupSource" in c
        assert "mongosh" in c
        assert "ResourceQuota" in c
        assert "--storage-size" in c
        assert "requests.storage = $storage_size" in c
        assert ".tolerations = []" in c
        assert ".spec.backup.enabled = true" in c
        assert ".spec.backup.tasks = []" in c
        assert ".spec.backup.pitr.enabled = false" in c
        assert 'SOURCE_USERS_SECRET="internal-${SOURCE_CLUSTER}-users"' in c
        assert ".spec.secrets.users // $fallback" in c
        assert "del(.spec.sharding.mongos, .spec.sharding.configsvrReplSet)" in c
        assert ".spec.mongos" not in c
        quota = c.split("kind: ResourceQuota", 1)[1].split("EOF", 1)[0]
        assert "requests.cpu" not in quota
        cleanup = c.split("cleanup()", 1)[1].split("trap cleanup EXIT", 1)[0]
        assert cleanup.index("kubectl delete perconaservermongodbrestore") < cleanup.index(
            "helm uninstall"
        )
        assert cleanup.index("kubectl delete perconaservermongodb") < cleanup.index(
            "helm uninstall"
        )


def test_vault_restore_storage_defaults_to_validated_10_gib():
    content = VAULT_RESTORE_SCRIPT.read_text()
    assert 'VAULT_RESTORE_STORAGE_SIZE:-10Gi' in content
    assert "--storage-size" in content
    assert 'requests.storage: ${STORAGE_SIZE}' in content
    assert 'storage: ${STORAGE_SIZE}' in content
    assert "positive Kubernetes storage quantity" in content
    assert "strategy:\n    type: Recreate" in content


def test_vault_restore_rejects_invalid_storage_before_dry_run():
    result = subprocess.run(
        ["bash", str(VAULT_RESTORE_SCRIPT), "--storage-size", "$(bad)", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "positive Kubernetes storage quantity" in result.stderr


def test_vault_restore_dry_run_reports_default_storage():
    result = subprocess.run(
        ["bash", str(VAULT_RESTORE_SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "Storage size: 10Gi" in result.stdout

class TestDocumentation:
    def test_exists(self): assert BACKUP_DOC.is_file()
    def test_quick_start(self): assert "Quick Start" in BACKUP_DOC.read_text()
    def test_configuration(self): assert "Configuration" in BACKUP_DOC.read_text()
    def test_safety_gates(self): assert "Safety Gates" in BACKUP_DOC.read_text()
    def test_components(self):
        c = BACKUP_DOC.read_text()
        for x in ("MongoDB","Vault","SeaweedFS","GitLab"): assert x in c
    def test_role_readme(self): assert (ROLE_DIR / "README.md").is_file()

class TestIntegration:
    def test_discoverable(self):
        assert "roles_path" in (REPO_ROOT / "ansible.cfg").read_text()
        assert ROLE_DIR.is_dir()
    def test_defaults_valid(self): assert isinstance(load_yaml(DEFAULTS_FILE), dict)
    def test_current_version_matrix_is_preserved(self):
        d = load_yaml(PROJECT_DEFAULTS)
        assert d.get("k8s_version") == "v1.35.4"
        assert d.get("cilium_version") == "v1.19.5"
        assert d.get("es_version") == "9.4.3"
        assert d.get("gitlab_chart_version") == "10.1.2"
        assert d.get("argocd_chart_version") == "9.5.14"
        assert d.get("object_storage_chart_version") == "4.25.1"
        assert d.get("keda_chart_version") == "2.20.1"
    def test_idempotent(self):
        for f in TASKS_DIR.glob("*.yml"):
            c = f.read_text()
            if "kubernetes.core.k8s:" in c:
                assert "state: present" in c or "state: absent" in c
    def test_existing_roles_valid(self):
        for f in ("roles/k8s-databases/tasks/main.yml","roles/k8s-secrets/tasks/reconcile.yml",
                   "roles/object-storage/tasks/main.yml","roles/gitlab-selfhosted/tasks/main.yml"):
            p = REPO_ROOT / f
            if p.is_file(): assert load_yaml(p) is not None
    def test_not_gitignored(self):
        gi = (REPO_ROOT / ".gitignore").read_text()
        assert "scripts/backup-all.sh" not in gi
        assert "scripts/restore-drill.sh" not in gi
        assert "BACKUP_RESTORE.md" not in gi
