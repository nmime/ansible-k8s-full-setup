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
