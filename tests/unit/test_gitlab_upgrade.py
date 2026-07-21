"""Unit tests: GitLab 19 upgrade - version consistency and chart 10.x structure."""
import os, re, pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULTS_PATH = os.path.join(REPO_ROOT, "defaults", "main.yml")
GITLAB_TASKS_PATH = os.path.join(REPO_ROOT, "roles", "gitlab-selfhosted", "tasks", "main.yml")
UPGRADE_DOC_PATH = os.path.join(REPO_ROOT, "docs", "GITLAB_UPGRADE_PLAN.md")

def read(path):
    with open(path) as f:
        return f.read()

def parse_version(line):
    m = re.match(r"^\s*(\w+)\s*:\s*['\"]?([^'\"#\n]+?)['\"]?\s*$", line)
    return (m.group(1), m.group(2).strip()) if m else (None, None)

class TestDefaultsVersions:
    @pytest.fixture(autouse=True)
    def _versions(self):
        content = read(DEFAULTS_PATH)
        self.versions = {}
        for line in content.splitlines():
            key, val = parse_version(line)
            if key and val and not val.startswith("{{"):
                self.versions[key] = val

    @pytest.mark.unit
    def test_gitlab_chart_version_is_10_x(self):
        assert self.versions.get("gitlab_chart_version") == "10.1.2"

    @pytest.mark.unit
    def test_gitlab_chart_version_format(self):
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions.get("gitlab_chart_version", ""))

    @pytest.mark.unit
    def test_gitlab_chart_major_is_10(self):
        major = int(self.versions.get("gitlab_chart_version", "0.0.0").split(".")[0])
        assert major == 10

class TestGitLabTasksChart10:
    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(GITLAB_TASKS_PATH)
        self.lines = self.content.splitlines()

    @pytest.mark.unit
    def test_file_exists(self):
        assert os.path.isfile(GITLAB_TASKS_PATH)

    @pytest.mark.unit
    def test_gitlabVersion_is_19(self):
        assert "gitlabVersion: 19.1.2" in self.content

    @pytest.mark.unit
    def test_chart_version_in_set_fact(self):
        assert "gitlab_chart_version: 10.1.2" in self.content

    @pytest.mark.unit
    def test_external_postgresql_uses_global_psql(self):
        assert re.search(r'^\s+psql:\s*$', self.content, re.MULTILINE)

    @pytest.mark.unit
    def test_obsolete_database_external_absent(self):
        assert not re.search(r'^\s+database:\s*\n\s+external:', self.content, re.MULTILINE)

    @pytest.mark.unit
    def test_external_database_secret_present(self):
        assert "secret: gitlab-postgresql-password" in self.content

    @pytest.mark.unit
    def test_no_postgresql_install_key(self):
        assert not re.search(r'^\s+postgresql:\s*\n\s+install:', self.content, re.MULTILINE)

    @pytest.mark.unit
    def test_redis_no_install_key(self):
        # Extract just the redis block by indentation
        redis_block = []
        in_redis = False
        base_indent = None
        for line in self.lines:
            m = re.match(r'^(\s+)redis:\s*$', line)
            if m:
                in_redis = True
                base_indent = len(m.group(1))
                redis_block.append(line)
                continue
            if in_redis:
                stripped = line.lstrip()
                cur_indent = len(line) - len(stripped)
                if stripped and cur_indent <= base_indent:
                    break
                redis_block.append(line)
        block_text = "\n".join(redis_block)
        assert "install:" not in block_text, f"redis.install found in: {block_text[:200]}"

    @pytest.mark.unit
    def test_redis_enabled_true(self):
        assert "enabled: true" in self.content

    @pytest.mark.unit
    def test_gitaly_chart_10_persistence_values(self):
        gitaly = self.content.split("        gitaly:", 1)[1].split("        kas:", 1)[0]
        assert "persistence:" in gitaly
        assert "size: '{{ gitlab_gitaly_storage_size }}'" in gitaly
        assert "storageClass: '{{ storage_class" in gitaly
        assert "persistentVolumeClaim:" not in gitaly

    @pytest.mark.unit
    def test_external_pg_host(self):
        assert "pg-pgbouncer.databases.svc.cluster.local" in self.content

    @pytest.mark.unit
    def test_external_pg_port(self):
        assert "port: 5432" in self.content

    @pytest.mark.unit
    def test_external_pg_database(self):
        assert "database: gitlabhq_production" in self.content

    @pytest.mark.unit
    def test_external_pg_username(self):
        assert "username: gitlab" in self.content

    @pytest.mark.unit
    def test_password_secret_reference(self):
        assert "secret: gitlab-postgresql-password" in self.content
        assert "key: postgresql-password" in self.content

    @pytest.mark.unit
    def test_prepared_statements_false(self):
        assert "preparedStatements: false" in self.content

    @pytest.mark.unit
    def test_object_storage_configured(self):
        assert "gitlab-object-storage" in self.content

    @pytest.mark.unit
    def test_registry_enabled(self):
        assert "registry:" in self.content

    @pytest.mark.unit
    def test_http_route_present(self):
        assert "HTTPRoute" in self.content

    @pytest.mark.unit
    def test_tls_certificate_present(self):
        assert "Certificate" in self.content
        assert "gitlab-tls" in self.content

    @pytest.mark.unit
    def test_network_policies_present(self):
        assert "CiliumNetworkPolicy" in self.content

class TestUpgradeDoc:
    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(UPGRADE_DOC_PATH)

    @pytest.mark.unit
    def test_doc_exists(self):
        assert os.path.isfile(UPGRADE_DOC_PATH)

    @pytest.mark.unit
    def test_mentions_chart_10(self):
        assert "10.1.2" in self.content

    @pytest.mark.unit
    def test_mentions_gitlab_19(self):
        assert "19.1.2" in self.content

    @pytest.mark.unit
    def test_mentions_database_migration(self):
        assert "database" in self.content.lower() and "external" in self.content.lower()

    @pytest.mark.unit
    def test_mentions_redis_migration(self):
        assert "redis" in self.content.lower()

    @pytest.mark.unit
    def test_mentions_gitaly_migration(self):
        assert "gitaly" in self.content.lower() and "storages" in self.content.lower()

    @pytest.mark.unit
    def test_has_rollback_section(self):
        assert "rollback" in self.content.lower()

class TestCrossFileConsistency:
    @pytest.fixture(autouse=True)
    def _read_all(self):
        self.defaults = read(DEFAULTS_PATH)
        self.tasks = read(GITLAB_TASKS_PATH)

    @pytest.mark.unit
    def test_defaults_and_tasks_chart_version_match(self):
        d = re.search(r'gitlab_chart_version:\s*["\']?([^"\'\n]+)', self.defaults)
        t = re.search(r'gitlab_chart_version:\s*([^\n]+)', self.tasks)
        assert d and t
        assert d.group(1).strip().strip("'\"") == t.group(1).strip()

    @pytest.mark.unit
    def test_defaults_and_tasks_gitlab_version_match(self):
        t = re.search(r'gitlabVersion:\s*([\d.]+)', self.tasks)
        assert t and t.group(1) == "19.1.2"
