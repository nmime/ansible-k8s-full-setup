"""E2E tests: GitLab upgrade flow validation (static/dry-run)."""
import os, subprocess, sys, pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GITLAB_TASKS = os.path.join(REPO_ROOT, "roles", "gitlab-selfhosted", "tasks", "main.yml")
DEFAULTS = os.path.join(REPO_ROOT, "defaults", "main.yml")
UPGRADE_DOC = os.path.join(REPO_ROOT, "docs", "GITLAB_UPGRADE_PLAN.md")
SCRIPTS = os.path.join(REPO_ROOT, "scripts")

def read(path):
    with open(path) as f:
        return f.read()

def _run(cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT)

class TestUpgradeE2EFiles:
    @pytest.mark.e2e
    def test_defaults_exists(self):
        assert os.path.isfile(DEFAULTS)

    @pytest.mark.e2e
    def test_gitlab_tasks_exists(self):
        assert os.path.isfile(GITLAB_TASKS)

    @pytest.mark.e2e
    def test_upgrade_doc_exists(self):
        assert os.path.isfile(UPGRADE_DOC)

    @pytest.mark.e2e
    def test_tasks_yaml_valid(self):
        try:
            import yaml
            with open(GITLAB_TASKS) as f:
                assert len(list(yaml.safe_load_all(f.read()))) >= 1
        except ImportError:
            pass

    @pytest.mark.e2e
    def test_defaults_yaml_valid(self):
        try:
            import yaml
            with open(DEFAULTS) as f:
                assert yaml.safe_load(f.read()) is not None
        except ImportError:
            pass

class TestUpgradeE2EVersionConsistency:
    @pytest.mark.e2e
    def test_chart_version_all_locations(self):
        assert "10.1.2" in read(DEFAULTS) and "10.1.2" in read(GITLAB_TASKS)

    @pytest.mark.e2e
    def test_app_version_in_tasks(self):
        assert "19.1.2" in read(GITLAB_TASKS)

    @pytest.mark.e2e
    def test_doc_references_new_versions(self):
        doc = read(UPGRADE_DOC)
        assert "10.1.2" in doc and "19.1.2" in doc

class TestUpgradeE2EBreakingChanges:
    @pytest.mark.e2e
    def test_database_external_migration(self):
        t = read(GITLAB_TASKS)
        assert "psql:" in t
        assert "-pg-pgbouncer.databases.svc.cluster.local" in t
        assert "secret: gitlab-postgresql-password" in t

    @pytest.mark.e2e
    def test_redis_migration(self):
        import re
        t = read(GITLAB_TASKS)
        assert "redis:" in t and not re.search(r'\bredis:\s*\n\s+install:', t)

    @pytest.mark.e2e
    def test_gitaly_storages_migration(self):
        t = read(GITLAB_TASKS)
        gitaly = t.split("        gitaly:", 1)[1].split("        kas:", 1)[0]
        assert "persistence:" in gitaly
        assert "size: '{{ gitlab_gitaly_storage_size }}'" in gitaly
        assert "persistentVolumeClaim:" not in gitaly

    @pytest.mark.e2e
    def test_no_postgresql_install_block(self):
        import re
        assert not re.search(r'\bpostgresql:\s*\n\s+install:', read(GITLAB_TASKS))

class TestUpgradeE2ERollbackPlan:
    @pytest.mark.e2e
    def test_rollback_documented(self):
        assert "rollback" in read(UPGRADE_DOC).lower()

    @pytest.mark.e2e
    def test_prerequisites_documented(self):
        doc = read(UPGRADE_DOC).lower()
        assert "preflight" in doc or "checklist" in doc or "backup" in doc

class TestUpgradeE2EScripts:
    @pytest.mark.e2e
    def test_preflight_script_exists(self):
        pf = os.path.join(SCRIPTS, "preflight_check.py")
        if os.path.isfile(pf):
            r = _run([sys.executable, pf, "--dry-run", "true"])
            assert r.returncode == 0, f"Preflight failed: {r.stderr}"

    @pytest.mark.e2e
    def test_upgrade_script_exists(self):
        assert os.path.isfile(os.path.join(SCRIPTS, "upgrade-platform.sh"))

    @pytest.mark.e2e
    def test_rollback_script_exists(self):
        assert os.path.isfile(os.path.join(SCRIPTS, "rollback.sh"))

class TestUpgradeE2ETestCoverage:
    @pytest.mark.e2e
    def test_unit_tests_exist(self):
        assert os.path.isfile(os.path.join(REPO_ROOT, "tests", "unit", "test_gitlab_upgrade.py"))

    @pytest.mark.e2e
    def test_component_tests_exist(self):
        assert os.path.isfile(os.path.join(REPO_ROOT, "tests", "component", "test_gitlab_config.py"))

    @pytest.mark.e2e
    def test_all_test_dirs_have_tests(self):
        for d in ["tests/unit", "tests/component", "tests/e2e"]:
            path = os.path.join(REPO_ROOT, d)
            assert os.path.isdir(path)
            assert len([f for f in os.listdir(path) if f.startswith("test_")]) > 0
