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
            "--target",
            "production",
            "plan",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = tmp_path / "state" / "offline-plan-minimal-to-production-plan"
    target = yaml.safe_load((state / "target-platform.yaml").read_text())
    expansion = yaml.safe_load((state / "expansion-platform.yaml").read_text())
    assert target["platform_profile"] == "production"
    assert target["global"]["domain"] == "cluster.example"
    assert target["backup"]["disaster_recovery"]["bucket"] == "cluster-dr"
    assert expansion["platform_profile"] == "custom"
    assert expansion["tier"] == "minimal"
    assert expansion["infrastructure"]["control_plane"]["count"] == 3
    assert expansion["infrastructure"]["workers"]["count"] == 3


PROFILES = ("minimal", "small", "medium", "medium-optimized", "production")


@pytest.mark.parametrize(
    ("source", "target"),
    [(source, target) for source in PROFILES for target in PROFILES if source != target],
)
def test_migration_plan_supports_every_ordered_profile_pair(tmp_path, source, target):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / f"{source}.yaml").read_text()
    )
    project = f"matrix-{source}-to-{target}"
    profile["global"].update(
        {"project": project, "domain": "cluster.example", "email": "ops@example.com"}
    )
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    env = os.environ.copy()
    env["PROFILE_MIGRATION_STATE_DIR"] = str(tmp_path / "state")
    env["PROFILE_MIGRATION_SKIP_ANSIBLE_VALIDATION"] = "true"
    result = subprocess.run(
        [
            "bash",
            str(MIGRATE),
            "--config",
            str(config),
            "--target",
            target,
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
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = tmp_path / "state" / f"{project}-{source}-to-{target}-plan"
    source_config = yaml.safe_load((state / "source-platform.yaml").read_text())
    target_config = yaml.safe_load((state / "target-platform.yaml").read_text())
    expansion = yaml.safe_load((state / "expansion-platform.yaml").read_text())
    transition = yaml.safe_load((state / "target-transition-platform.yaml").read_text())
    rollback = yaml.safe_load((state / "rollback-platform.yaml").read_text())
    expected_cp = max(
        source_config["infrastructure"]["control_plane"]["count"],
        target_config["infrastructure"]["control_plane"]["count"],
    )
    expected_workers = max(
        source_config["infrastructure"]["workers"]["count"],
        target_config["infrastructure"]["workers"]["count"],
    )
    assert target_config["platform_profile"] == target
    assert expansion["platform_profile"] == "custom"
    assert transition["platform_profile"] == "custom"
    assert rollback["platform_profile"] == source
    for generated in (expansion, transition, rollback):
        assert generated["infrastructure"]["control_plane"]["count"] == expected_cp
        assert generated["infrastructure"]["workers"]["count"] == expected_workers


def test_downgrade_plan_retains_only_non_shrinkable_pvc_requests(tmp_path):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / "production.yaml").read_text()
    )
    profile["global"].update(
        {"project": "storage-safe", "domain": "cluster.example", "email": "ops@example.com"}
    )
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PROFILE_MIGRATION_STATE_DIR": str(tmp_path / "state"),
            "PROFILE_MIGRATION_SKIP_ANSIBLE_VALIDATION": "true",
        }
    )
    result = subprocess.run(
        [
            "bash",
            str(MIGRATE),
            "--config",
            str(config),
            "--target",
            "medium-optimized",
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
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = tmp_path / "state" / "storage-safe-production-to-medium-optimized-plan"
    target = yaml.safe_load((state / "target-platform.yaml").read_text())
    retention = (state / "storage-retention.tsv").read_text()
    stateful_retention = (state / "stateful-retention.tsv").read_text()
    assert target["storage"]["size_per_replica"] == "150Gi"
    assert target["databases"]["postgresql"]["storage_size"] == "100Gi"
    assert target["observability"]["metrics"]["storage_size"] == "150Gi"
    assert "seaweedfs-volume\t150Gi\t40Gi" in retention
    assert "postgresql\t100Gi\t30Gi" in retention
    assert target["storage"]["master_replicas"] == 4
    assert target["storage"]["volume_replicas"] == 4
    assert target["observability"]["metrics"]["replicas"] == 2
    assert "seaweedfs-master\t4\t3" in stateful_retention
    assert "victoriametrics-cluster\t2\t1" in stateful_retention


def test_execute_dry_run_creates_no_active_migration_state(tmp_path):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml").read_text()
    )
    profile["global"].update(
        {"project": "dry-execute", "domain": "cluster.example", "email": "ops@example.com"}
    )
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    state_base = tmp_path / "state"
    env = os.environ.copy()
    env["PROFILE_MIGRATION_STATE_DIR"] = str(state_base)
    result = subprocess.run(
        [
            "bash",
            str(MIGRATE),
            "--config",
            str(config),
            "--target",
            "production",
            "--dr-endpoint",
            "https://s3.external.example",
            "--dr-bucket",
            "cluster-dr",
            "--dry-run",
            "execute",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    dry_state = state_base / "dry-execute-minimal-to-production-dry-run"
    assert dry_state.is_dir()
    assert not (dry_state / "state.json").exists()
    assert not (state_base / "dry-execute-active-profile-migration").exists()


def test_migration_is_checkpointed_backup_gated_and_destructive_only_at_finalize():
    content = MIGRATE.read_text(encoding="utf-8")
    for stage in (
        "preflight",
        "backup",
        "expand",
        "resize",
        "apply-target",
        "migrate-data",
        "validate",
        "post-backup",
    ):
        assert stage in content
    assert "cluster-backup.sh" in content
    assert "kubectl drain" in content
    assert "etcdctl endpoint health --cluster" in content
    assert "Nodes were deliberately retained".lower() in content.lower()
    assert "FINALIZE_STAGES" in content
    assert "remove-node.yml" in content
    assert content.index("remove-node.yml") < content.index('hcloud server delete "$node"')
    assert "hetzner_allow_destructive_reconcile=true" in content
    assert "kubectl delete vmsingle" in content
    assert "kubectl delete vmcluster" in content
    assert "helm uninstall" in content
    assert "--vm-native-src-addr=${source_addr}" in content
    assert "--vm-native-dst-addr=${target_addr}" in content
    assert "http://vmselect-vmcluster.monitoring.svc:8481/select/0/prometheus" in content
    assert "http://vmsingle-vmsingle.monitoring.svc:8429" in content
    assert "--vm-native-filter-time-start" in content
    assert "            - -s" in content
    assert "            - --disable-progress-bar" in content
    assert "storage-retention.tsv" in content
    assert "stateful-retention.tsv" in content
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
    (state / "state.json").write_text(
        '{"status":"completed","project":"offline-finalize",'
        '"source_profile":"minimal","target_profile":"production"}',
        encoding="utf-8",
    )
    shutil.copy(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml", state / "source-platform.yaml")
    shutil.copy(ROOT / "platform-orchestrator" / "profiles" / "production.yaml", state / "target-platform.yaml")
    expansion = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / "production.yaml").read_text()
    )
    expansion["platform_profile"] = "custom"
    (state / "expansion-platform.yaml").write_text(yaml.safe_dump(expansion), encoding="utf-8")
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
    assert "finalize stage: retire-services" in result.stdout
    assert "finalize stage: scale-in" in result.stdout
    assert "finalize stage: final-backup" in result.stdout
    assert "remove excess workers 3->3 and control planes 3->3" in result.stdout


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
