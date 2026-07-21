"""Component tests: GitLab Helm values structure vs chart 10.x."""
import os, re, pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GITLAB_TASKS_PATH = os.path.join(REPO_ROOT, "roles", "gitlab-selfhosted", "tasks", "main.yml")
DEFAULTS_PATH = os.path.join(REPO_ROOT, "defaults", "main.yml")

def read(path):
    with open(path) as f:
        return f.read()

class TestChart10ValuesStructure:
    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(GITLAB_TASKS_PATH)

    @pytest.mark.component
    def test_global_edition_present(self):
        assert "edition:" in self.content

    @pytest.mark.component
    def test_global_hosts_present(self):
        assert "hosts:" in self.content and "domain:" in self.content

    @pytest.mark.component
    def test_global_email_present(self):
        assert "email:" in self.content

    @pytest.mark.component
    def test_chart_gateway_and_issuer_are_disabled_for_platform_gateway(self):
        assert "gatewayApi:" in self.content
        assert "installEnvoy: false" in self.content
        assert self.content.count("configureCertmanager: false") >= 2

    @pytest.mark.component
    def test_webservice_configured(self):
        assert "webservice:" in self.content and "replicaCount:" in self.content

    @pytest.mark.component
    def test_sidekiq_configured(self):
        assert "sidekiq:" in self.content

    def test_heavy_rails_workloads_prefer_different_nodes(self):
        assert self.content.count("topologySpreadConstraints:") >= 2
        assert self.content.count("whenUnsatisfiable: ScheduleAnyway") >= 2
        assert self.content.count("- webservice") >= 2
        assert self.content.count("- sidekiq") >= 2
        assert self.content.count("topologyKey: kubernetes.io/hostname") >= 2
        assert "Add cross-component anti-affinity to GitLab Rails workloads" in self.content
        assert "weight: 100" in self.content
        assert "Rebalance GitLab Rails workloads when a rolling update co-locates them" in self.content

    @pytest.mark.component
    def test_gitlab_shell_configured(self):
        assert "gitlab-shell:" in self.content

    @pytest.mark.component
    def test_kas_enabled(self):
        assert "kas:" in self.content

    @pytest.mark.component
    def test_toolbox_enabled(self):
        assert "toolbox:" in self.content

    @pytest.mark.component
    def test_object_store_configured(self):
        assert "object_store:" in self.content or "objectStorage:" in self.content
        for b in ["gitlab-lfs", "gitlab-artifacts", "gitlab-uploads"]:
            assert b in self.content

    @pytest.mark.component
    def test_registry_storage_secret(self):
        assert "gitlab-registry-storage" in self.content

    @pytest.mark.component
    def test_helm_chart_ref(self):
        assert "chart_ref: gitlab/gitlab" in self.content

    @pytest.mark.component
    def test_helm_timeout(self):
        assert "timeout: 30m0s" in self.content

    def test_helm_reclaims_server_side_apply_fields_before_post_patches(self):
        install = self.content.split("- name: Install GitLab with Helm", 1)[1].split(
            "- name: Add cross-component anti-affinity", 1
        )[0]
        assert "force_conflicts: true" in install

    def test_failed_helm_revision_recovery_deletes_only_exact_failed_history(self):
        recovery = self.content.split(
            "- name: Recover a failed GitLab Helm revision without deleting release workloads",
            1,
        )[1].split("- name: Discover failed GitLab database migration Jobs", 1)[0]
        for contract in (
            "owner=helm",
            "name=gitlab",
            "status == 'failed'",
            "item.type == 'helm.sh/release.v1'",
            "sh.helm.release.v1.gitlab.v",
            "item.metadata.labels.owner == 'helm'",
            "item.metadata.labels.name == 'gitlab'",
            "item.metadata.ownerReferences | default([]) | length",
            "item.data.release | default('') | length",
            "uid: '{{ item.metadata.uid }}'",
            "resourceVersion: '{{ item.metadata.resourceVersion }}'",
            "Delete only failed GitLab Helm revisions newer than the deployed predecessor",
            "Require failed revision cleanup to expose the deployed predecessor",
        ):
            assert contract in recovery

        history_path = recovery.split(
            "- name: Discover GitLab Helm release history Secrets", 1
        )[1].split(
            "- name: Recheck GitLab Helm status after failed revision cleanup", 1
        )[0]
        assert "kind: Secret" in history_path
        assert "kind: StatefulSet" not in history_path
        assert "kind: PersistentVolumeClaim" not in history_path
        assert "release_state: absent" not in history_path

    def test_failed_first_install_uninstall_requires_absent_or_data_free_gitaly(self):
        recovery = self.content.split(
            "- name: Recover a failed GitLab Helm revision without deleting release workloads",
            1,
        )[1].split("- name: Discover failed GitLab database migration Jobs", 1)[0]
        first_install = recovery.split(
            "- name: Discover first-install Gitaly StatefulSets", 1
        )[1]
        for contract in (
            "_gitlab_deployed_release_secrets | length == 0",
            "status.readyReplicas",
            "status.phase",
            "spec.volumeName",
            "default(0) | int) == 0",
            "default('')) ==\n             'Pending'",
            "default('') | length) == 0",
            "metadata.name ==\n             'gitlab-gitaly'",
            "metadata.name ==\n             'repo-data-gitlab-gitaly-0'",
            "metadata.labels.heritage",
            "'app.kubernetes.io/managed-by'] == 'Helm'",
            "'meta.helm.sh/release-namespace'] | default('')) == gitlab_namespace",
            "Remove only a proven data-free failed first GitLab release",
            "No workload or PVC was deleted",
        ):
            assert contract in first_install
        assert "release_state: absent" in first_install
        assert "Remove failed GitLab Helm release before reinstall" not in self.content

    def test_post_reconcile_gate_requires_every_gitlab_pvc_bound(self):
        gate = self.content.split("- name: Enforce GitLab post-reconcile readiness", 1)[1].split(
            "- name: Add cross-component anti-affinity", 1
        )[0]
        assert "kind: PersistentVolumeClaim" in gate
        assert "release=gitlab" in gate
        assert "Require GitLab persistent volume claims to exist" in gate
        assert "--for=jsonpath={.status.phase}=Bound" in gate
        assert "--timeout={{ gitlab_readiness_timeout }}" in gate

    def test_post_reconcile_gate_selects_critical_controllers_by_labels(self):
        gate = self.content.split("- name: Enforce GitLab post-reconcile readiness", 1)[1].split(
            "- name: Add cross-component anti-affinity", 1
        )[0]
        for kind, app in (
            ("StatefulSet", "gitaly"),
            ("Deployment", "webservice"),
            ("Deployment", "sidekiq"),
            ("Deployment", "toolbox"),
        ):
            assert f"kind: {kind}\n            app: {app}" in gate
        assert "Require every critical GitLab controller to exist" in gate
        assert "kubectl rollout status" in gate
        assert "release=gitlab,app=" in gate

    def test_readiness_failure_is_sanitized_and_fail_closed(self):
        gate = self.content.split("- name: Enforce GitLab post-reconcile readiness", 1)[1].split(
            "- name: Add cross-component anti-affinity", 1
        )[0]
        assert "Collect sanitized GitLab PVC readiness metadata" in gate
        assert "Collect sanitized GitLab controller readiness metadata" in gate
        assert "Collect sanitized GitLab warning event metadata" in gate
        assert "Fail closed when GitLab is not fully ready" in gate
        assert "OBJECT_NAME:.involvedObject.name" in gate
        assert ".message" not in gate
        assert "kubectl describe" not in gate

    def test_readiness_timeout_is_configurable_with_bounded_default(self):
        assert "gitlab_readiness_timeout:" in self.content
        assert "gitlab.readiness_timeout | default(''30m'')" in self.content

    def test_chart_10_gitaly_persistence_contract_uses_rendered_values_path(self):
        gitaly = self.content.split("        gitaly:", 1)[1].split("        kas:", 1)[0]
        assert "          persistence:" in gitaly
        assert "            enabled: true" in gitaly
        assert "            size: '{{ gitlab_gitaly_storage_size }}'" in gitaly
        assert "            storageClass: '{{ storage_class" in gitaly
        assert "persistentVolumeClaim:" not in gitaly

    def test_unbound_gitaly_size_drift_has_conservative_recovery(self):
        recovery = self.content.split("- name: Inspect existing Gitaly storage", 1)[1].split(
            "- name: Install GitLab with Helm", 1
        )[0]
        assert "rejectattr('spec.resources.requests.storage'" in recovery
        assert "(item.status.phase | default('')) == 'Pending'" in recovery
        assert "(item.spec.volumeName | default('') | length) == 0" in recovery
        assert "(_gitlab_existing_gitaly_ready_replicas | int) == 0" in recovery
        assert "Remove never-ready Gitaly StatefulSets" in recovery
        assert "Remove unbound Gitaly claims" in recovery
        assert "Bound, previously ready, or otherwise potentially data-bearing" in recovery

    def test_gitaly_recovery_requires_one_exact_helm_owned_storage_pair(self):
        recovery = self.content.split("- name: Inspect existing Gitaly storage", 1)[1].split(
            "- name: Install GitLab with Helm", 1
        )[0]
        assert "Require an unambiguous existing Gitaly storage inventory" in recovery
        assert "does not have the exact GitLab Helm" in recovery
        for contract in (
            "metadata.name == 'gitlab-gitaly'",
            "metadata.labels.release == 'gitlab'",
            "metadata.labels.app == 'gitaly'",
            "metadata.labels.heritage == 'Helm'",
            "metadata.labels['app.kubernetes.io/managed-by'] == 'Helm'",
            "metadata.annotations['meta.helm.sh/release-name'] == 'gitlab'",
            "metadata.annotations['meta.helm.sh/release-namespace'] == gitlab_namespace",
            "metadata.ownerReferences | default([]) | length",
            "persistentVolumeClaimRetentionPolicy.whenDeleted == 'Retain'",
            "persistentVolumeClaimRetentionPolicy.whenScaled == 'Retain'",
            "metadata.name == 'repo-data-gitlab-gitaly-0'",
        ):
            assert contract in recovery

    def test_bound_gitaly_template_drift_has_fail_closed_sts_only_recovery(self):
        recovery = self.content.split("- name: Inspect existing Gitaly storage", 1)[1].split(
            "- name: Install GitLab with Helm", 1
        )[0]
        guard = recovery.split(
            "- name: Prove bound Gitaly PVC and controller are safe for StatefulSet-only recovery",
            1,
        )[1].split(
            "- name: Orphan only the drifted Gitaly StatefulSet", 1
        )[0]
        for contract in (
            "status.phase | default('')) == 'Bound'",
            "spec.volumeName | default('') | length) > 0",
            "spec.resources.requests.storage == gitlab_gitaly_storage_size",
            "status.capacity.storage == gitlab_gitaly_storage_size",
            "spec.storageClassName == _gitlab_desired_gitaly_storage_class",
            "status.observedGeneration",
            "status.readyReplicas",
            "status.currentReplicas",
            "status.updatedReplicas",
            "status.availableReplicas",
            "status.currentRevision ==",
            "status.updateRevision",
            "spec.replicas | default(1) | int) == 0",
        ):
            assert contract in guard
        assert "neither fully healthy nor safely scaled to zero" in guard

        orphan = recovery.split(
            "- name: Orphan only the drifted Gitaly StatefulSet", 1
        )[1].split(
            "- name: Re-read retained Gitaly PVC", 1
        )[0]
        assert "kind: StatefulSet" in orphan
        assert "propagationPolicy: Orphan" in orphan
        assert "uid: '{{ _gitlab_existing_gitaly_statefulset.metadata.uid }}'" in orphan
        assert "resourceVersion: '{{ _gitlab_existing_gitaly_statefulset.metadata.resourceVersion }}'" in orphan
        assert "kind: PersistentVolumeClaim" not in orphan

    def test_bound_gitaly_recovery_proves_same_pvc_uid_and_volume_after_orphaning(self):
        recovery = self.content.split("- name: Inspect existing Gitaly storage", 1)[1].split(
            "- name: Install GitLab with Helm", 1
        )[0]
        proof = recovery.split(
            "- name: Prove StatefulSet-only recovery preserved the exact Bound Gitaly PVC",
            1,
        )[1]
        assert "metadata.uid == _gitlab_existing_gitaly_pvc.metadata.uid" in proof
        assert "status.phase == 'Bound'" in proof
        assert "spec.volumeName == _gitlab_existing_gitaly_pvc.spec.volumeName" in proof
        assert "spec.resources.requests.storage == gitlab_gitaly_storage_size" in proof
        assert "status.capacity.storage == gitlab_gitaly_storage_size" in proof
        assert "spec.storageClassName == _gitlab_desired_gitaly_storage_class" in proof
        assert "state: absent" not in proof

class TestDefaultsTasksConsistency:
    @pytest.fixture(autouse=True)
    def _read_all(self):
        self.defaults_raw = read(DEFAULTS_PATH)
        self.tasks_raw = read(GITLAB_TASKS_PATH)

    @pytest.mark.component
    def test_chart_version_in_sync(self):
        d = re.search(r'gitlab_chart_version:\s*["\']?([^"\'\n#]+)', self.defaults_raw)
        t = re.search(r'gitlab_chart_version:\s*([^\n]+)', self.tasks_raw)
        assert d and t
        assert d.group(1).strip("'\"") == t.group(1).strip()

    @pytest.mark.component
    def test_storage_class_used(self):
        assert "storage_class" in self.tasks_raw or "storageClass" in self.tasks_raw

    @pytest.mark.component
    def test_tier_logic_preserved(self):
        assert "gitlab_mode" in self.tasks_raw

    @pytest.mark.component
    def test_toolbox_skips_database_covered_by_native_percona_backup(self):
        assert "--skip db" in self.tasks_raw
        assert "--s3tool awscli" in self.tasks_raw

    @pytest.mark.component
    def test_toolbox_awscli_receives_minio_credentials(self):
        for token in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_DEFAULT_REGION",
            "AWS_REQUEST_CHECKSUM_CALCULATION",
            "accesskey",
            "secretkey",
        ):
            assert token in self.tasks_raw

    @pytest.mark.component
    def test_toolbox_backup_scratch_persistence_is_configurable(self):
        assert "gitlab_backup_persistence_enabled:" in self.tasks_raw
        assert "gitlab.backup_persistence_enabled | default(true)" in self.tasks_raw
        assert "enabled: '{{ gitlab_backup_persistence_enabled | bool }}'" in self.tasks_raw

    @pytest.mark.component
    def test_every_toolbox_backup_bucket_is_bootstrapped(self):
        buckets = read(os.path.join(REPO_ROOT, "roles", "object-storage", "defaults", "main.yml"))
        for bucket in (
            "gitlab-artifacts",
            "gitlab-registry",
            "gitlab-lfs",
            "gitlab-uploads",
            "gitlab-packages",
            "gitlab-mr-diffs",
            "gitlab-terraform-state",
            "gitlab-pages",
            "gitlab-ci-secure-files",
            "gitlab-agent-plan-content",
            "gitlab-backups",
            "gitlab-tmp",
        ):
            assert f"- {bucket}" in buckets

    @pytest.mark.component
    def test_kas_gateway_ingress_is_explicitly_allowed(self):
        assert "Allow GitLab KAS ingress from gateway" in self.tasks_raw
        assert "name: allow-kas-ingress" in self.tasks_raw
        assert "app: kas" in self.tasks_raw
        assert "port: '8150'" in self.tasks_raw

class TestBackupCompatibility:
    @pytest.fixture(autouse=True)
    def _content(self):
        path = os.path.join(REPO_ROOT, "roles", "backup-restore", "tasks", "gitlab.yml")
        self.content = read(path) if os.path.isfile(path) else ""

    @pytest.mark.component
    def test_backup_task_exists(self):
        assert os.path.isfile(os.path.join(REPO_ROOT, "roles", "backup-restore", "tasks", "gitlab.yml"))

    @pytest.mark.component
    def test_backup_cronjob_present(self):
        if self.content:
            assert "CronJob" in self.content

    @pytest.mark.component
    def test_backup_credentials_secret(self):
        if self.content:
            assert "gitlab-rails-backup-credentials" in self.content

    @pytest.mark.component
    def test_official_toolbox_backup_is_required(self):
        if self.content:
            assert "gitlab-toolbox-backup" in self.content

    @pytest.mark.component
    def test_external_database_backup_contract_is_documented(self):
        if self.content:
            assert "external Percona" in self.content
            assert "version-matched backup" in self.content

class TestNoDeprecatedKeys:
    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(GITLAB_TASKS_PATH)

    @pytest.mark.component
    def test_external_postgresql_uses_global_psql(self):
        assert re.search(r'^\s+psql:\s*$', self.content, re.MULTILINE)
        assert "-pg-pgbouncer.databases.svc.cluster.local" in self.content

    @pytest.mark.component
    def test_no_obsolete_database_external_key(self):
        assert not re.search(r'^\s+database:\s*\n\s+external:', self.content, re.MULTILINE)

    @pytest.mark.component
    def test_no_postgresql_install(self):
        assert not re.search(r'\bpostgresql:\s*\n\s+install:', self.content)

    @pytest.mark.component
    def test_no_redis_install_key(self):
        in_redis = False
        indent_level = None
        for line in self.content.splitlines():
            if re.match(r'^\s+redis:\s*$', line):
                in_redis = True
                indent_level = len(line) - len(line.lstrip())
                continue
            if in_redis:
                ci = len(line) - len(line.lstrip()) if line.strip() else indent_level + 1
                if line.strip() and ci <= indent_level and line.strip() != "redis:":
                    break
                s = line.strip()
                if not s.startswith("#") and re.match(r'install:', s):
                    pytest.fail(f"Deprecated redis.install: {s}")
