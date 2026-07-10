"""Test suite for backup-restore Ansible role and scripts."""
import subprocess, yaml
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLE_DIR = REPO_ROOT / "roles" / "backup-restore"
TASKS_DIR = ROLE_DIR / "tasks"
DEFAULTS_FILE = ROLE_DIR / "defaults" / "main.yml"
PROJECT_DEFAULTS = REPO_ROOT / "defaults" / "main.yml"
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup-all.sh"
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore-drill.sh"
BACKUP_DOC = REPO_ROOT / "BACKUP_RESTORE.md"

def load_yaml(path):
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    return docs[0] if len(docs) == 1 else docs

class TestRoleStructure:
    def test_role_directory_exists(self): assert ROLE_DIR.is_dir()
    def test_defaults_main_exists(self): assert DEFAULTS_FILE.is_file()
    def test_tasks_main_exists(self): assert (TASKS_DIR / "main.yml").is_file()
    def test_mongodb_task_exists(self): assert (TASKS_DIR / "mongodb_pbm.yml").is_file()
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

TASK_FILES = ["main.yml","mongodb_pbm.yml","vault_raft.yml","seaweedfs.yml","gitlab.yml","verification.yml","alerts.yml"]

class TestTaskYAML:
    @pytest.mark.parametrize("f", TASK_FILES)
    def test_valid(self, f): assert load_yaml(TASKS_DIR / f) is not None

class TestMainInclusion:
    def _c(self): return (TASKS_DIR / "main.yml").read_text()
    def test_mongodb(self): assert "mongodb_pbm" in self._c()
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

CJ = [("mongodb_pbm.yml","mongodb-backup"),("vault_raft.yml","vault-raft-snapshot"),
      ("seaweedfs.yml","seaweedfs-backup-check"),("gitlab.yml","gitlab-backup"),
      ("verification.yml","backup-verification")]

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

class TestSecrets:
    def test_main_creds(self):
        c = (TASKS_DIR / "main.yml").read_text()
        assert "AWS_ACCESS_KEY_ID" in c and "AWS_SECRET_ACCESS_KEY" in c
    def test_vault(self): assert "vault-backup-credentials" in (TASKS_DIR / "vault_raft.yml").read_text()
    def test_sw(self): assert "seaweedfs-backup-credentials" in (TASKS_DIR / "seaweedfs.yml").read_text()
    def test_gl(self): assert "gitlab-backup-credentials" in (TASKS_DIR / "gitlab.yml").read_text()
    def test_alert(self):
        c = (TASKS_DIR / "alerts.yml").read_text()
        assert "backup-alert-config" in c and "WEBHOOK_URL" in c

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
    def test_confirm_gate(self):
        c = BACKUP_SCRIPT.read_text()
        assert "read" in c and "yes" in c.lower()
    def test_components(self):
        c = BACKUP_SCRIPT.read_text()
        for x in ("mongodb","vault","seaweedfs","gitlab"): assert x in c
    def test_summary(self): assert "SUMMARY" in BACKUP_SCRIPT.read_text()

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
    def test_quota(self): assert "ResourceQuota" in RESTORE_SCRIPT.read_text()
    def test_cleanup(self): assert "cleanup" in RESTORE_SCRIPT.read_text().lower()
    def test_components(self):
        c = RESTORE_SCRIPT.read_text()
        for x in ("mongodb","vault","seaweedfs","gitlab"): assert x in c
    def test_summary(self): assert "SUMMARY" in RESTORE_SCRIPT.read_text()

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
    def test_no_version_changes(self):
        d = load_yaml(PROJECT_DEFAULTS)
        assert d.get("k8s_version") == "v1.35.4"
        assert d.get("cilium_version") == "v1.19.4"
        assert d.get("es_version") == "9.4.1"
        assert d.get("gitlab_chart_version") == "9.11.4"
        assert d.get("argocd_chart_version") == "9.5.14"
        assert d.get("object_storage_chart_version") == "4.25.1"
        assert d.get("keda_chart_version") == "2.19.0"
    def test_idempotent(self):
        for f in TASKS_DIR.glob("*.yml"):
            c = f.read_text()
            if "kubernetes.core.k8s:" in c: assert "state: present" in c
    def test_existing_roles_valid(self):
        for f in ("roles/k8s-databases/tasks/main.yml","roles/k8s-secrets/tasks/main.yml",
                   "roles/object-storage/tasks/main.yml","roles/gitlab-selfhosted/tasks/main.yml"):
            p = REPO_ROOT / f
            if p.is_file(): assert load_yaml(p) is not None
    def test_not_gitignored(self):
        gi = (REPO_ROOT / ".gitignore").read_text()
        assert "scripts/backup-all.sh" not in gi
        assert "scripts/restore-drill.sh" not in gi
        assert "BACKUP_RESTORE.md" not in gi
