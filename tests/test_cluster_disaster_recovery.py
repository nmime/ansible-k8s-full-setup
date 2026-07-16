"""Contract and offline workflow tests for full-cluster DR and migration."""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BACKUP = SCRIPTS / "cluster-backup.sh"
RESTORE = SCRIPTS / "cluster-restore.sh"
MIGRATE = SCRIPTS / "migrate-profile.sh"
VELERO = ROOT / "roles" / "backup-restore" / "tasks" / "velero.yml"


@pytest.mark.parametrize("script", (BACKUP, RESTORE, MIGRATE))
def test_scripts_are_executable_and_parse(script):
    assert script.is_file()
    assert os.access(script, os.X_OK)
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", (BACKUP, RESTORE, MIGRATE))
def test_scripts_have_help(script):
    result = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_backup_dry_run_describes_every_recovery_layer(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(BACKUP),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml"),
            "--output-dir",
            str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    for layer in ("application backups", "Velero", "etcd", "control-plane PKI", "Hetzner"):
        assert layer in result.stdout


def test_backup_skips_are_fail_closed_without_explicit_incomplete_mode():
    result = subprocess.run(
        [
            "bash",
            str(BACKUP),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml"),
            "--skip-cloud",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--allow-incomplete" in result.stderr


def test_backup_requires_filesystem_copy_for_every_mounted_pvc_volume():
    content = BACKUP.read_text(encoding="utf-8")
    assert "mounted-pod-volumes.expected.tsv" in content
    assert "mounted-pod-volumes.completed.tsv" in content
    assert "pod-volume-backups.json" in content
    assert "comm -23" in content


def _make_encrypted_fixture(tmp_path: Path, passphrase: str) -> Path:
    bundle_name = "test-cluster-20260716T000000Z"
    bundle = tmp_path / bundle_name
    bundle.mkdir()
    manifest = {
        "schema_version": 1,
        "backup_id": bundle_name,
        "project": "test",
        "source_context": "source",
        "completeness": "complete",
        "contains": {"etcd_snapshot": True},
        "restore_order": ["infrastructure", "control-plane", "velero"],
    }
    (bundle / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    checksum = hashlib.sha256((bundle / "MANIFEST.json").read_bytes()).hexdigest()
    (bundle / "SHA256SUMS").write_text(f"{checksum}  ./MANIFEST.json\n", encoding="utf-8")
    plain = tmp_path / f"{bundle_name}.tar.gz"
    with tarfile.open(plain, "w:gz") as archive:
        archive.add(bundle, arcname=bundle_name)
    encrypted = tmp_path / f"{bundle_name}.tar.gz.enc"
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = passphrase
    result = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "600000",
            "-salt",
            "-pass",
            "env:CLUSTER_BACKUP_PASSPHRASE",
            "-in",
            str(plain),
            "-out",
            str(encrypted),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256(encrypted.read_bytes()).hexdigest()
    Path(f"{encrypted}.sha256").write_text(f"{digest}  {encrypted.name}\n", encoding="utf-8")
    return encrypted


def test_restore_verifies_encryption_and_both_checksum_layers(tmp_path):
    encrypted = _make_encrypted_fixture(tmp_path, "correct horse battery staple")
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    result = subprocess.run(
        ["bash", str(RESTORE), "--archive", str(encrypted), "--mode", "verify"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert '"backup_id"' in result.stdout
    assert "verified bundle" in result.stdout


def test_restore_rejects_a_tampered_encrypted_archive(tmp_path):
    encrypted = _make_encrypted_fixture(tmp_path, "correct horse battery staple")
    encrypted.write_bytes(encrypted.read_bytes() + b"tamper")
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    result = subprocess.run(
        ["bash", str(RESTORE), "--archive", str(encrypted)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr


def test_etcd_recovery_supports_replacement_inventory_and_member_identity():
    content = RESTORE.read_text(encoding="utf-8")
    assert "--inventory" in content
    assert "broken_etcd" in content
    assert "broken_kube_control_plane" in content
    assert "ETCD_MEMBER_NAME" in content
    assert ".all.children.kube_control_plane.hosts" in content


def test_migration_plan_generates_valid_target_and_expansion_configs(tmp_path):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load((ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml").read_text())
    profile["global"].update({"project": "offline-plan", "domain": "cluster.example", "email": "ops@example.com"})
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    env = os.environ.copy()
    env["PROFILE_MIGRATION_STATE_DIR"] = str(tmp_path / "state")
    result = subprocess.run(
        [
            "bash",
            str(MIGRATE),
            "--config",
            str(config),
            "--dr-endpoint",
            "https://s3.external.example",
            "--dr-bucket",
            "cluster-dr",
            "plan",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = tmp_path / "state" / "offline-plan-minimal-to-production"
    target = yaml.safe_load((state / "target-platform.yaml").read_text())
    expansion = yaml.safe_load((state / "expansion-platform.yaml").read_text())
    assert target["platform_profile"] == "production"
    assert target["global"]["domain"] == "cluster.example"
    assert target["backup"]["disaster_recovery"]["bucket"] == "cluster-dr"
    assert expansion["platform_profile"] == "custom"
    assert expansion["tier"] == "minimal"
    assert expansion["infrastructure"]["control_plane"]["count"] == 3
    assert expansion["infrastructure"]["workers"]["count"] == 3


def test_migration_is_checkpointed_backup_gated_and_non_destructive_on_rollback():
    content = MIGRATE.read_text(encoding="utf-8")
    for stage in (
        "preflight",
        "backup",
        "expand",
        "resize",
        "apply-production",
        "migrate-data",
        "validate",
        "post-backup",
    ):
        assert stage in content
    assert "cluster-backup.sh" in content
    assert "kubectl drain" in content
    assert "etcdctl endpoint health --cluster" in content
    assert "Nodes were deliberately retained".lower() in content.lower()
    assert "hcloud server delete" not in content
    assert "finalize" in content
    assert "kubectl delete vmsingle" in content
    assert "helm uninstall" in content
    assert "deployments,statefulsets,daemonsets" in content
    assert "certificates --all-namespaces" in content
    assert "PerconaPGCluster".lower() in content.lower()
    assert "PerconaServerMongoDB".lower() in content.lower()


def test_completed_migration_has_an_explicit_dry_run_finalization(tmp_path):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load((ROOT / "platform-orchestrator" / "profiles" / "production.yaml").read_text())
    profile["global"].update({"project": "offline-finalize", "domain": "cluster.example", "email": "ops@example.com"})
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    state_base = tmp_path / "state"
    state = state_base / "offline-finalize-minimal-to-production"
    state.mkdir(parents=True)
    (state / "state.json").write_text('{"status":"completed"}', encoding="utf-8")
    (state / "stage-post-backup.done").write_text("done\n", encoding="utf-8")
    env = os.environ.copy()
    env["PROFILE_MIGRATION_STATE_DIR"] = str(state_base)
    result = subprocess.run(
        ["bash", str(MIGRATE), "--config", str(config), "--dry-run", "finalize"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "superseded VMSingle, Loki, and Promtail" in result.stdout
    assert "another encrypted full-cluster backup" in result.stdout


def test_velero_role_uses_external_storage_and_filesystem_backups():
    tasks = yaml.safe_load(VELERO.read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    helm_task = next(task for task in tasks if "kubernetes.core.helm" in task)
    values = helm_task["kubernetes.core.helm"]["values"]
    assert values["configuration"]["defaultVolumesToFsBackup"] is True
    assert "defaultVolumesToFsBackup" not in values
    content = VELERO.read_text(encoding="utf-8")
    for contract in (
        "Reject an in-cluster disaster-recovery target",
        "backup_dr_storage_endpoint",
        "velero-plugin-for-aws",
        "defaultVolumesToFsBackup: true",
        "deployNodeAgent: true",
        "snapshotsEnabled: false",
        "backupstoragelocation/default",
    ):
        assert contract in content


def test_production_capable_profiles_enable_external_dr():
    for name in ("medium", "medium-optimized", "production"):
        profile = yaml.safe_load((ROOT / "platform-orchestrator" / "profiles" / f"{name}.yaml").read_text())
        assert profile["backup"]["disaster_recovery"]["enabled"] is True
        assert profile["backup"]["disaster_recovery"]["endpoint"] == ""
        assert profile["backup"]["disaster_recovery"]["bucket"] == ""


def test_deployment_fails_before_provisioning_without_external_dr_contract():
    content = (ROOT / "playbooks" / "deploy_platform.yml").read_text(encoding="utf-8")
    assert "Fail early when external disaster-recovery storage is incomplete" in content
    assert "BACKUP_DR_ACCESS_KEY/BACKUP_DR_SECRET_KEY before provisioning" in content
    assert "backup_dr_enabled | bool" in content


def test_application_backup_orchestrator_triggers_postgresql_full_backup():
    content = (SCRIPTS / "backup-all.sh").read_text(encoding="utf-8")
    assert "PerconaPGBackup" in content
    assert "--type=full" in content
    assert "for c in postgresql mongodb vault seaweedfs gitlab" in content
    assert "component_expected" in content
    assert "--config" in content


def test_application_backup_dry_run_honors_profile_selection():
    result = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "backup-all.sh"),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml"),
            "--dry-run",
            "--force",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "Would create PerconaPGBackup" in result.stdout
    assert "Would skip disabled component mongodb" in result.stdout
    assert "Would skip disabled component gitlab" in result.stdout


def test_platform_cli_exposes_backup_restore_and_migration():
    content = (ROOT / "platform-orchestrator" / "platform.sh").read_text(encoding="utf-8")
    for command in ("backup-cluster", "restore-cluster", "migrate"):
        assert command in content


def test_backup_removal_cleans_local_velero_but_retains_remote_objects():
    content = (ROOT / "playbooks" / "remove_component.yml").read_text(encoding="utf-8")
    assert "namespaces: [backups, velero]" in content
    assert "{name: velero, namespace: velero}" in content
    assert "Remote\n          object-storage backup" in content
