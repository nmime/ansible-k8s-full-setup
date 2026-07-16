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
    def test_webservice_configured(self):
        assert "webservice:" in self.content and "replicaCount:" in self.content

    @pytest.mark.component
    def test_sidekiq_configured(self):
        assert "sidekiq:" in self.content

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
