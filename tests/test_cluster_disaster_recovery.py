"""Contract and offline workflow tests for full-cluster DR and migration."""

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BACKUP = SCRIPTS / "cluster-backup.sh"
RESTORE = SCRIPTS / "cluster-restore.sh"
NATIVE_RESTORE = SCRIPTS / "native-restore.sh"
MIGRATE = SCRIPTS / "migrate-profile.sh"
STORAGE_CAPACITY = SCRIPTS / "profile-storage-capacity.py"
VAULT_MIGRATE = SCRIPTS / "vault-storage-migrate.sh"
CAPTURE_REPOSITORY = SCRIPTS / "capture-repository-state.sh"
RESTORE_UNTRACKED = SCRIPTS / "restore-repository-untracked.sh"
VELERO = ROOT / "roles" / "backup-restore" / "tasks" / "velero.yml"
VELERO_DISABLED = (
    ROOT / "roles" / "backup-restore" / "tasks" / "velero_disabled.yml"
)
BACKUP_TASKS = ROOT / "roles" / "backup-restore" / "tasks" / "main.yml"
TEARDOWN = ROOT / "teardown.sh"


def test_disabled_dr_removes_the_stale_velero_runtime():
    tasks = BACKUP_TASKS.read_text(encoding="utf-8")
    cleanup = VELERO_DISABLED.read_text(encoding="utf-8")

    assert "velero_disabled.yml" in tasks
    assert "not (backup_dr_enabled | bool)" in tasks
    assert 'name: velero' in cleanup
    assert "state: absent" in cleanup
    assert "PodVolumeBackup" in cleanup
    assert "finalizers: []" in cleanup
    assert 'name: "{{ backup_dr_namespace }}"' in cleanup


@pytest.mark.parametrize(
    "script",
    (BACKUP, RESTORE, MIGRATE, VAULT_MIGRATE, CAPTURE_REPOSITORY, RESTORE_UNTRACKED),
)
def test_scripts_are_executable_and_parse(script):
    assert script.is_file()
    assert os.access(script, os.X_OK)
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_repository_capture_recovers_index_worktree_and_safe_untracked_files(tmp_path):
    repository = tmp_path / "repository"
    destination = tmp_path / "capture"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    (repository / ".gitignore").write_text(".env*\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True
    )

    (repository / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "tracked.txt"], check=True
    )
    with (repository / "tracked.txt").open("a", encoding="utf-8") as tracked:
        tracked.write("unstaged\n")
    (repository / "roles").mkdir()
    (repository / "roles" / "new.yml").write_text("---\nsafe: true\n", encoding="utf-8")
    (repository / ".env.local").write_text("TOKEN=ignored\n", encoding="utf-8")

    result = subprocess.run(
        [str(CAPTURE_REPOSITORY), str(repository), str(destination)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    patch = (destination / "worktree.patch").read_text(encoding="utf-8")
    assert "+staged" in patch and "+unstaged" in patch
    assert (destination / "repository-untracked-files.txt").read_text().splitlines() == [
        "roles/new.yml"
    ]
    with tarfile.open(destination / "repository-untracked.tar", "r:") as archive:
        assert archive.getnames() == ["roles/new.yml"]
    assert ".env.local" not in (destination / "git-status.txt").read_text()

    (repository / "private.pem").write_text("credential material\n", encoding="utf-8")
    rejected = subprocess.run(
        [str(CAPTURE_REPOSITORY), str(repository), str(tmp_path / "rejected")],
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "credential-like untracked file" in rejected.stderr


@pytest.mark.parametrize("kind", ("symlink", "fifo"))
def test_repository_capture_never_archives_untracked_links_or_special_files(tmp_path, kind):
    repository = tmp_path / "repository"
    destination = tmp_path / "capture"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    unsafe = repository / "nested" / "unsafe"
    unsafe.parent.mkdir()
    if kind == "symlink":
        unsafe.symlink_to(repository / "tracked.txt")
    else:
        os.mkfifo(unsafe)

    result = subprocess.run(
        [str(CAPTURE_REPOSITORY), str(repository), str(destination)],
        capture_output=True,
        text=True,
    )
    if kind == "symlink":
        assert result.returncode != 0
        assert "non-regular untracked file: nested/unsafe" in result.stderr
    else:
        # Git does not report filesystem special files in its untracked-file
        # inventory; successful capture must still prove the FIFO was omitted.
        assert result.returncode == 0, result.stderr
        assert (destination / "repository-untracked-count.txt").read_text() == "0\n"
        with tarfile.open(destination / "repository-untracked.tar", "r:") as archive:
            assert archive.getmembers() == []


def test_repository_untracked_replay_restores_nested_files_and_refuses_collisions(tmp_path):
    source = tmp_path / "source"
    capture = tmp_path / "capture"
    checkout = tmp_path / "checkout"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    nested = source / "roles" / "new" / "script.sh"
    nested.parent.mkdir(parents=True)
    nested.write_text("#!/bin/sh\necho recovered\n", encoding="utf-8")
    nested.chmod(0o755)
    subprocess.run([str(CAPTURE_REPOSITORY), str(source), str(capture)], check=True)
    subprocess.run(["git", "clone", "-q", str(capture / "repository.bundle"), str(checkout)], check=True)

    restored = subprocess.run(
        [str(RESTORE_UNTRACKED), str(capture), str(checkout)],
        capture_output=True,
        text=True,
    )
    assert restored.returncode == 0, restored.stderr
    restored_nested = checkout / "roles" / "new" / "script.sh"
    assert restored_nested.read_text(encoding="utf-8").endswith("echo recovered\n")
    assert stat.S_IMODE(restored_nested.stat().st_mode) == 0o755

    collision = tmp_path / "collision"
    collision.mkdir()
    (collision / "repository-untracked-files.txt").write_text("tracked.txt\n", encoding="utf-8")
    (collision / "repository-untracked-count.txt").write_text("1\n", encoding="utf-8")
    with tarfile.open(collision / "repository-untracked.tar", "w:") as archive:
        archive.add(source / "tracked.txt", arcname="tracked.txt")
    refused = subprocess.run(
        [str(RESTORE_UNTRACKED), str(collision), str(checkout)],
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "destination path is tracked: 'tracked.txt'" in refused.stderr
    assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "base\n"


@pytest.mark.parametrize("member_name", ("../outside.txt", "nested/link"))
def test_repository_untracked_replay_rejects_traversal_and_link_members(tmp_path, member_name):
    checkout = tmp_path / "checkout"
    state = tmp_path / "state"
    checkout.mkdir()
    state.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (state / "repository-untracked-files.txt").write_text(f"{member_name}\n", encoding="utf-8")
    (state / "repository-untracked-count.txt").write_text("1\n", encoding="utf-8")
    with tarfile.open(state / "repository-untracked.tar", "w:") as archive:
        info = tarfile.TarInfo(member_name)
        if member_name == "nested/link":
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
        else:
            payload = b"outside\n"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if member_name == "nested/link":
            archive.addfile(info)

    result = subprocess.run(
        [str(RESTORE_UNTRACKED), str(state), str(checkout)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    expected = "unsafe path" if member_name.startswith("..") else "not a regular file"
    assert expected in result.stderr
    assert not (tmp_path / "outside.txt").exists()


def test_repository_untracked_replay_rejects_symlinked_destination_parent(tmp_path):
    checkout = tmp_path / "checkout"
    outside = tmp_path / "outside"
    state = tmp_path / "state"
    checkout.mkdir()
    outside.mkdir()
    state.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / "nested").symlink_to(outside, target_is_directory=True)
    member_name = "nested/file.txt"
    (state / "repository-untracked-files.txt").write_text(
        f"{member_name}\n", encoding="utf-8"
    )
    (state / "repository-untracked-count.txt").write_text("1\n", encoding="utf-8")
    with tarfile.open(state / "repository-untracked.tar", "w:") as archive:
        payload = b"must stay contained\n"
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    result = subprocess.run(
        [str(RESTORE_UNTRACKED), str(state), str(checkout)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "destination parent is not a real directory" in result.stderr
    assert not (outside / "file.txt").exists()


def test_repository_untracked_replay_rejects_deleted_tracked_parent(tmp_path):
    checkout = tmp_path / "checkout"
    state = tmp_path / "state"
    checkout.mkdir()
    state.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    tracked_parent = checkout / "nested"
    tracked_parent.symlink_to("tracked-target")
    subprocess.run(["git", "-C", str(checkout), "add", "nested"], check=True)
    tracked_parent.unlink()
    member_name = "nested/file.txt"
    (state / "repository-untracked-files.txt").write_text(
        f"{member_name}\n", encoding="utf-8"
    )
    (state / "repository-untracked-count.txt").write_text("1\n", encoding="utf-8")
    with tarfile.open(state / "repository-untracked.tar", "w:") as archive:
        payload = b"must not replace tracked parent\n"
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    result = subprocess.run(
        [str(RESTORE_UNTRACKED), str(state), str(checkout)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "destination parent is tracked: 'nested'" in result.stderr
    assert not tracked_parent.exists()


@pytest.mark.parametrize(
    "credential_path",
    (
        ".npmrc",
        "config/.pypirc",
        "home/.netrc",
        ".aws/credentials",
        "user/.docker/config.json",
        "operator/.kube/config",
        "secrets/credentials",
        "config/credentials.json",
        "gcp/application_default_credentials.json",
        "gcp/build-service-account.json",
        "gcp/service_account.json",
        "terraform/production.tfvars",
        "terraform/production.tfvars.json",
        "vpn/client.ovpn",
        "ssh/id_dsa",
        "ssh/id_ecdsa.backup",
    ),
)
def test_repository_capture_rejects_nested_credential_paths(tmp_path, credential_path):
    repository = tmp_path / "repository"
    destination = tmp_path / "capture"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True
    )

    credential = repository / credential_path
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("credential material\n", encoding="utf-8")
    result = subprocess.run(
        [str(CAPTURE_REPOSITORY), str(repository), str(destination)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert f"credential-like untracked file: {credential_path}" in result.stderr


@pytest.mark.parametrize(
    "safe_path",
    (
        "docs/npmrc.example",
        "config/.npmrc.example",
        "config/credentials.example.json",
        "terraform/production.tfvars.example",
        "vpn/client.ovpn.example",
        "docs/service-account.md",
    ),
)
def test_repository_capture_accepts_credential_name_lookalikes(tmp_path, safe_path):
    repository = tmp_path / "repository"
    destination = tmp_path / "capture"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True
    )

    safe_file = repository / safe_path
    safe_file.parent.mkdir(parents=True, exist_ok=True)
    safe_file.write_text("placeholder documentation\n", encoding="utf-8")
    result = subprocess.run(
        [str(CAPTURE_REPOSITORY), str(repository), str(destination)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (destination / "repository-untracked-files.txt").read_text().splitlines() == [
        safe_path
    ]


def test_cluster_manifest_records_repository_recovery_checksums():
    content = BACKUP.read_text(encoding="utf-8")
    assert '"$SCRIPT_DIR/capture-repository-state.sh"' in content
    assert "REPOSITORY_BUNDLE_SHA256=" in content
    assert "WORKTREE_PATCH_SHA256=" in content
    assert "GIT_REVISION_SHA256=" in content
    assert "UNTRACKED_ARCHIVE_SHA256=" in content
    assert 'tracked_patch_scope:"HEAD-to-working-tree-including-index"' in content
    assert 'untracked_archive_path:"config/repository-untracked.tar"' in content
    assert 'revision_path:"config/git-revision.txt"' in content
    assert "untracked_file_count:$untrackedFileCount" in content
    assert "provider_machine_types:{bastion:$bastionType,control_plane:$controlPlaneType" in content


def test_operational_shell_scripts_do_not_require_bash_4_case_conversion():
    incompatible = re.compile(r"\$\{[^}]+(?:,,|\^\^)")
    for script in SCRIPTS.glob("*.sh"):
        assert incompatible.search(script.read_text(encoding="utf-8")) is None, script


def test_vault_offline_migration_supports_ondelete_statefulset():
    content = VAULT_MIGRATE.read_text(encoding="utf-8")
    assert "uses an OnDelete StatefulSet" in content
    assert "kubectl wait --for=delete pod/vault-0" in content
    assert "kubectl rollout status statefulset/vault" not in content
    assert "resuming from an already stopped Vault StatefulSet" in content
    assert "backup.platform.io/pvc" in content
    assert "backup.platform.io/image" in content


@pytest.mark.parametrize("script", (BACKUP, RESTORE, MIGRATE, VAULT_MIGRATE))
def test_scripts_have_help(script):
    result = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def _write_backup_vault_inputs(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    if shutil.which("ansible-vault") is None:
        pytest.skip("ansible-vault is required for the encrypted fixture")
    secrets = tmp_path / "platform-secrets.yml"
    secrets.write_text("---\nfixture: true\n", encoding="utf-8")
    vault_init = tmp_path / "vault-init.json"
    vault_init.write_text(
        json.dumps(
            {
                "root_token": "fixture-root-token",
                "unseal_keys_b64": ["fixture-unseal-share"],
            }
        ),
        encoding="utf-8",
    )
    password = tmp_path / "vault-password"
    password.write_text("fixture-password\n", encoding="utf-8")
    encrypted = subprocess.run(
        [
            "ansible-vault",
            "encrypt",
            "--vault-password-file",
            str(password),
            str(vault_init),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert encrypted.returncode == 0, encrypted.stderr
    env = os.environ.copy()
    env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(password)
    return secrets, vault_init, password, env


def test_backup_dry_run_describes_every_recovery_layer(tmp_path):
    secrets, vault_init, _password, env = _write_backup_vault_inputs(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(BACKUP),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml"),
            "--secrets-file",
            str(secrets),
            "--vault-init-file",
            str(vault_init),
            "--output-dir",
            str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    for layer in ("application backups", "Velero", "etcd", "control-plane PKI", "Hetzner"):
        assert layer in result.stdout
    assert "Ansible Vault-encrypted Vault initialization material: true" in result.stdout


def test_backup_requires_exact_encrypted_vault_init_when_vault_is_enabled(tmp_path):
    secrets = tmp_path / "platform-secrets.yml"
    secrets.write_text("---\nfixture: true\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            str(BACKUP),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml"),
            "--secrets-file",
            str(secrets),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "--vault-init-file is required" in result.stderr


def test_backup_rejects_plaintext_vault_init_without_exposing_content(tmp_path):
    secrets = tmp_path / "platform-secrets.yml"
    secrets.write_text("---\nfixture: true\n", encoding="utf-8")
    vault_init = tmp_path / "vault-init.json"
    marker = "must-not-appear-in-output"
    vault_init.write_text(
        json.dumps({"root_token": marker, "unseal_keys_b64": [marker]}),
        encoding="utf-8",
    )
    password = tmp_path / "vault-password"
    password.write_text("fixture-password\n", encoding="utf-8")
    env = os.environ.copy()
    env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(password)
    result = subprocess.run(
        [
            "bash",
            str(BACKUP),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml"),
            "--secrets-file",
            str(secrets),
            "--vault-init-file",
            str(vault_init),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode != 0
    assert "not Ansible Vault encrypted" in result.stderr
    assert marker not in result.stdout + result.stderr


def test_backup_rejects_undecryptable_vault_init_without_exposing_content(tmp_path):
    secrets, vault_init, password, env = _write_backup_vault_inputs(tmp_path)
    password.write_text("wrong-fixture-password\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            str(BACKUP),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml"),
            "--secrets-file",
            str(secrets),
            "--vault-init-file",
            str(vault_init),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode != 0
    assert "failed encrypted structure validation" in result.stderr
    assert "fixture-root-token" not in result.stdout + result.stderr


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
    assert "BACKUP_ALLOW_VELERO_VAULT_FALLBACK=true" in content
    assert 'backup.velero.io/backup-volumes=${volumes}' in content
    assert "restore_backup_annotations" in content
    assert "defaultVolumesToFsBackup: false" in content
    assert "mounted-pod-volumes.expected.tsv" in content
    assert "mounted-pod-volumes.completed.tsv" in content
    assert "pod-volume-backups.json" in content
    assert "comm -23" in content
    assert content.count('select($pod.status.phase == "Running")') >= 2
    assert 'failed_volume_backups=$(kubectl get podvolumebackups' in content
    assert "if ! failed_volume_backups=" in content
    assert '[[ "$failed_volume_backups" =~ ^[0-9]+$ ]]' in content
    assert 'Velero filesystem backup(s) failed before the backup completed' in content
    assert '[[ -f "$POD_ANNOTATIONS_FILE" && -s "$POD_ANNOTATIONS_FILE" ]]' in content


def test_complete_backup_rejects_unbound_or_unmounted_live_pvcs_with_json_evidence():
    content = BACKUP.read_text(encoding="utf-8")
    assert "pvc-protection-evidence.json" in content
    assert "persistentvolumeclaims --all-namespaces -o json" in content
    assert '$claim.metadata.deletionTimestamp == null' not in content
    assert "select(.metadata.deletionTimestamp == null)" in content
    assert '($claim.status.phase // "") == "Bound"' in content
    assert 'platform.n0xeid.xyz/backup-scratch' in content
    assert 'if (($backup_scratch | not) and ($claim_mounts | length) == 0)' in content
    assert 'if [[ "$ALLOW_INCOMPLETE" != true ]]' in content
    assert "non-terminating PVC(s) are non-Bound or unmounted" in content
    assert 'chmod 600 "$PVC_EVIDENCE"' in content
    assert "pvc_protection_gate" in content


def test_pvc_evidence_policy_classifies_live_claims(tmp_path):
    if shutil.which("jq") is None:
        pytest.skip("jq is required by cluster-backup.sh")
    content = BACKUP.read_text(encoding="utf-8")
    marker = '  --slurpfile pvc "$PVC_SNAPSHOT" --slurpfile pods "$PVC_PODS_SNAPSHOT" \'\n'
    jq_filter = content.split(marker, 1)[1].split(
        '\n  \' > "$PVC_EVIDENCE_TMP"', 1
    )[0]
    pvc = {
        "items": [
            {
                "metadata": {"namespace": "data", "name": "mounted"},
                "spec": {"volumeName": "pv-a", "resources": {"requests": {"storage": "1Gi"}}},
                "status": {"phase": "Bound"},
            },
            {
                "metadata": {"namespace": "data", "name": "orphan"},
                "spec": {"volumeName": "pv-b", "resources": {"requests": {"storage": "1Gi"}}},
                "status": {"phase": "Bound"},
            },
            {
                "metadata": {
                    "namespace": "data",
                    "name": "completed-backup-scratch",
                    "labels": {"platform.n0xeid.xyz/backup-scratch": "true"},
                },
                "spec": {"volumeName": "pv-c", "resources": {"requests": {"storage": "1Gi"}}},
                "status": {"phase": "Bound"},
            },
            {
                "metadata": {"namespace": "data", "name": "pending"},
                "spec": {"resources": {"requests": {"storage": "1Gi"}}},
                "status": {"phase": "Pending"},
            },
            {
                "metadata": {"namespace": "data", "name": "terminating", "deletionTimestamp": "2026-07-21T00:00:00Z"},
                "spec": {},
                "status": {"phase": "Pending"},
            },
        ]
    }
    pods = {
        "items": [
            {
                "metadata": {"namespace": "data", "name": "database"},
                "spec": {
                    "containers": [{"name": "db", "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "mounted"}}],
                },
                "status": {"phase": "Running"},
            }
        ]
    }
    pvc_path = tmp_path / "pvc.json"
    pods_path = tmp_path / "pods.json"
    pvc_path.write_text(json.dumps(pvc), encoding="utf-8")
    pods_path.write_text(json.dumps(pods), encoding="utf-8")
    result = subprocess.run(
        [
            "jq", "-n", "--arg", "backupId", "fixture", "--arg", "project", "test",
            "--arg", "context", "test-context", "--slurpfile", "pvc", str(pvc_path),
            "--slurpfile", "pods", str(pods_path), jq_filter,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "incomplete"
    assert evidence["backup_id"] == "fixture"
    assert evidence["summary"] == {"evaluated": 4, "protected": 2, "failures": 2}
    claims = {claim["name"]: claim for claim in evidence["claims"]}
    assert claims["mounted"]["protected"] is True
    assert claims["orphan"]["failures"] == ["unmounted"]
    assert claims["completed-backup-scratch"]["backup_scratch"] is True
    assert claims["completed-backup-scratch"]["protected"] is True
    assert claims["completed-backup-scratch"]["failures"] == []
    assert claims["pending"]["failures"] == ["not_bound", "unmounted"]
    assert "terminating" not in claims


def test_encrypted_bundle_is_remote_verified_before_manifest_last_receipt():
    content = BACKUP.read_text(encoding="utf-8")
    assert 'PATH="${PROJECT_ROOT}/.venv/bin:${PATH}"' in content
    archive_upload = 'aws_dr s3 cp "$FINAL_ARCHIVE"'
    checksum_upload = 'aws_dr s3 cp "${FINAL_ARCHIVE}.sha256"'
    archive_download = 'aws_dr s3 cp "s3://${DR_BUCKET}/${REMOTE_ARCHIVE_KEY}"'
    receipt_upload = 'aws_dr s3 cp "$FINAL_RECEIPT"'
    assert "BACKUP_DR_ACCESS_KEY is required for remote bundle publishing" in content
    assert "BACKUP_DR_SECRET_KEY is required for remote bundle publishing" in content
    assert "downloaded remote recovery archive failed SHA-256 verification" in content
    assert 'receipt_uploaded_last:$receiptUploadedLast' in content
    assert content.index(archive_upload) < content.index(checksum_upload)
    assert content.index(checksum_upload) < content.index(archive_download)
    assert content.index(archive_download) < content.index(receipt_upload)
    assert 'cmp -s "$FINAL_RECEIPT" "$REMOTE_RECEIPT_VERIFY"' in content


def test_controller_can_publish_through_a_distinct_route_to_the_same_dr_store():
    content = BACKUP.read_text(encoding="utf-8")
    assert 'DR_CLIENT_ENDPOINT="${BACKUP_DR_CLIENT_ENDPOINT:-}"' in content
    assert '[[ -n "$DR_CLIENT_ENDPOINT" ]] || DR_CLIENT_ENDPOINT="$DR_ENDPOINT"' in content
    assert 'aws --endpoint-url "$DR_CLIENT_ENDPOINT" "$@"' in content
    assert "--arg clientEndpoint \"$DR_CLIENT_ENDPOINT\"" in content
    assert "client_endpoint:$clientEndpoint" in content


def test_backup_signals_cleanup_and_exit_instead_of_continuing():
    content = BACKUP.read_text(encoding="utf-8")
    assert "handle_signal() {" in content
    assert "trap - EXIT INT TERM" in content
    assert "exit 130" in content
    assert "trap cleanup EXIT" in content
    assert "trap handle_signal INT TERM" in content
    assert "trap cleanup EXIT INT TERM" not in content


def test_schema2_receipt_binds_project_uid_prefix_and_uses_rfc3339_time():
    content = BACKUP.read_text(encoding="utf-8")
    assert "RECEIPT_CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)" in content
    assert '{schema_version:2,receipt_type:"encrypted-cluster-backup"' in content
    assert "source_cluster_uid:$sourceClusterUid" in content
    assert "velero_storage_prefix:$veleroPrefix" in content
    assert "project:$project" in content


def test_complete_bundle_requires_a_structured_native_backup_catalog():
    backup = BACKUP.read_text(encoding="utf-8")
    orchestrator = (SCRIPTS / "backup-all.sh").read_text(encoding="utf-8")
    assert '--result-json "$STAGE_DIR/application-backups/native-backups.json"' in backup
    assert "structured native backup catalog is missing" in backup
    assert 'bundle_path:(if $app == "completed" then "application-backups/native-backups.json"' in backup
    assert "--result-json" in orchestrator
    assert "restore_contract:$contract" in orchestrator
    assert "artifact_locator:$locator" in orchestrator
    assert "repository:$repository" in orchestrator
    assert "native_backup_catalog_sha256:$nativeCatalogSha" in backup
    assert "completion receipt does not bind the exact native catalog" in RESTORE.read_text()


def test_backup_all_emits_process_isolated_structured_catalog_in_dry_run(tmp_path):
    catalog = tmp_path / "native-backups.json"
    result = subprocess.run(
        [
            str(SCRIPTS / "backup-all.sh"),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "production.yaml"),
            "--result-json",
            str(catalog),
            "--dry-run",
            "--force",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(catalog.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert data["backup_id"]
    assert data["restore_order"] == [
        "seaweedfs",
        "vault",
        "postgresql",
        "mongodb",
        "gitlab-secrets",
        "gitlab",
    ]
    assert data["summary"]["expected"] == len(data["artifacts"]) == 6
    assert data["completeness"] == "incomplete"
    assert {artifact["component"] for artifact in data["artifacts"]} == {
        "postgresql",
        "mongodb",
        "vault",
        "seaweedfs",
        "gitlab",
        "gitlab-secrets",
    }
    assert all(artifact["restore_contract"] for artifact in data["artifacts"])
    assert stat.S_IMODE(catalog.stat().st_mode) == 0o600


def test_local_receipt_stays_pending_until_remote_receipt_is_uploaded_and_verified():
    content = BACKUP.read_text(encoding="utf-8")
    pending_archive = (
        'write_completion_receipt "$RECEIPT_PATH" false false pending_archive'
    )
    pending_receipt = (
        'write_completion_receipt "$RECEIPT_PATH" false false pending_receipt'
    )
    completed_candidate = (
        'write_completion_receipt "$FINAL_RECEIPT" true true complete'
    )
    receipt_upload = 'aws_dr s3 cp "$FINAL_RECEIPT"'
    receipt_download = (
        'aws_dr s3 cp "s3://${DR_BUCKET}/${REMOTE_RECEIPT_KEY}"'
    )
    receipt_compare = 'cmp -s "$FINAL_RECEIPT" "$REMOTE_RECEIPT_VERIFY"'
    local_promotion = 'mv "$FINAL_RECEIPT" "$RECEIPT_PATH"'

    assert 'publication_state:$publicationState' in content
    assert 'remote:{published:$remotePublished' in content
    assert pending_archive in content
    assert pending_receipt in content
    assert completed_candidate in content
    assert content.index(pending_archive) < content.index(receipt_upload)
    assert content.index(pending_receipt) < content.index(receipt_upload)
    assert content.index(completed_candidate) < content.index(receipt_upload)
    assert content.index(receipt_upload) < content.index(receipt_download)
    assert content.index(receipt_download) < content.index(receipt_compare)
    assert content.index(receipt_compare) < content.index(local_promotion)
    assert 'write_completion_receipt "$RECEIPT_PATH" false false not_requested' in content


def test_remote_bundle_skip_is_fail_closed():
    result = subprocess.run(
        [
            "bash",
            str(BACKUP),
            "--config",
            str(ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml"),
            "--skip-remote-publish",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--allow-incomplete" in result.stderr


def test_compact_velero_has_low_requests_but_working_memory_burst_limit():
    content = VELERO.read_text(encoding="utf-8")
    assert "'64Mi' if resource_tier in ['minimal', 'small']" in content
    assert "'512Mi' if resource_tier in ['minimal', 'small']" in content
    assert "Full API exports can exceed 256Mi" in content


def test_backup_uses_an_explicit_noninteractive_ssh_identity():
    content = BACKUP.read_text(encoding="utf-8")
    assert "--ssh-identity" in content
    assert "CLUSTER_BACKUP_SSH_IDENTITY" in content
    assert "-o IdentitiesOnly=yes" in content
    assert 'ProxyCommand=${proxy_command}' in content
    assert "-W %h:%p" in content
    assert 'fail "SSH identity is missing:' in content


def test_migration_prepares_private_network_before_kubespray_expansion():
    content = MIGRATE.read_text(encoding="utf-8")
    assert "--tags infrastructure,network,security,cluster" in content
    assert "Newly created private-only nodes require the bastion NAT" in content


def test_migration_captures_rollback_baseline_from_isolated_source_config():
    content = MIGRATE.read_text(encoding="utf-8")
    backup_stage = content.split("stage_backup()", 1)[1].split(
        "requires_spread()", 1
    )[0]
    assert 'snapshot_root="$STATE_DIR/rollback-snapshots"' in backup_stage
    assert '"$SCRIPT_DIR/snapshot-helm-baseline.sh"' in backup_stage
    assert '--config "$SOURCE_CONFIG" --snapshot-dir "$snapshot_root"' in backup_stage
    assert 'source "$SCRIPT_DIR/snapshot-helm-baseline.sh"' not in backup_stage


def test_migration_recovers_from_an_accepted_hcloud_action_after_client_failure():
    content = MIGRATE.read_text(encoding="utf-8")
    assert "PROFILE_MIGRATION_HCLOUD_CLIENT_TIMEOUT_SECONDS" in content
    assert "PROFILE_MIGRATION_HCLOUD_STATE_TIMEOUT_SECONDS" in content
    assert "run_with_timeout" in content
    assert "wait_for_server_settled" in content
    assert "authoritative post-action shape" in content
    assert 'server_json=$(hcloud server describe "$node" -o json)' in content


def test_migration_waits_for_etcd_after_a_control_plane_restart():
    content = MIGRATE.read_text(encoding="utf-8")
    assert "PROFILE_MIGRATION_ETCD_HEALTH_TIMEOUT_SECONDS" in content
    assert "etcd quorum is not fully ready yet" in content
    assert "etcd cluster did not become healthy within" in content


def test_migration_reconciles_complete_etcd_clients_after_control_plane_expansion():
    content = MIGRATE.read_text(encoding="utf-8")
    expansion = content.split("stage_expand()", 1)[1].split(
        "wait_for_node_runtime()", 1
    )[0]
    resize = content.split("stage_resize()", 1)[1].split(
        "control_plane_nodes()", 1
    )[0]
    reconcile = content.split("reconcile_control_plane_etcd_clients()", 1)[1].split(
        "ensure_control_plane_etcd_ha()", 1
    )[0]

    assert 'ensure_control_plane_etcd_ha "$EXPANSION_CONFIG"' in expansion
    assert 'ensure_control_plane_etcd_ha "$EXPANSION_CONFIG"' in resize
    assert "kubeadm init phase upload-config kubeadm" in reconcile
    assert "kubeadm init phase control-plane apiserver" in reconcile
    assert reconcile.index("for ((i=2; i<=count; i++))") < reconcile.rindex(
        'node="${PROJECT}-master-1"'
    )
    assert "old_id=" in reconcile
    assert '"$new_id" != "$old_id"' in reconcile
    assert "etcdctl member list -w json" in content
    assert ".clientURLs | sort" in content
    assert ".peerURLs | sort" in content
    assert "learners" in content
    assert "control-plane API servers do not all use the complete etcd endpoint set" in content


def test_migration_proves_control_plane_survivors_after_poweroff_before_mutation():
    content = MIGRATE.read_text(encoding="utf-8")
    resize = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]
    survivor = content.split("check_control_plane_survivors()", 1)[1].split(
        "wait_for_csi_detach()", 1
    )[0]

    stopped = resize.index('ensure_server_stopped "$node"')
    survivor_gate = resize.index('check_control_plane_survivors "$node"')
    placement = resize.index('hcloud server add-to-placement-group')
    assert stopped < survivor_gate < placement
    assert "surviving control-plane endpoint" in survivor
    assert "surviving etcd members cannot commit" in survivor
    assert "etcdctl endpoint health" in survivor
    assert "wait_for_api_ready" in survivor
    assert "restoring the stopped master before aborting" in resize
    assert resize.index("control-plane survivor gate failed") < resize.index(
        'ensure_server_running "$node"', survivor_gate
    )


def test_migration_waits_for_csi_detach_after_drain_before_poweroff():
    content = MIGRATE.read_text(encoding="utf-8")
    resize = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]
    detach = content.split("wait_for_csi_detach()", 1)[1].split(
        "run_with_timeout()", 1
    )[0]

    assert "PROFILE_MIGRATION_CSI_DETACH_TIMEOUT_SECONDS" in content
    assert "volumeattachments.storage.k8s.io" in detach
    assert ".spec.nodeName == $node" in detach
    assert 'hcloud server describe "$node" -o json' in detach
    assert ".volumes | length" in detach
    assert resize.index('kubectl drain "$node"') < resize.index(
        'wait_for_csi_detach "$node"'
    )
    assert resize.index('wait_for_csi_detach "$node"') < resize.index(
        'ensure_server_stopped "$node"'
    )


def test_migration_temporarily_overrides_and_restores_singleton_gitaly_pdb():
    content = MIGRATE.read_text(encoding="utf-8")
    prepare = content.split("prepare_gitaly_pdb_override_for_node()", 1)[1].split(
        "resize_node()", 1
    )[0]
    restore = content.split("restore_gitaly_pdb_override()", 1)[1].split(
        "prepare_gitaly_pdb_override_for_node()", 1
    )[0]
    resize = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]

    assert 'GITALY_PDB_OVERRIDE_FILE="${STATE_DIR}/gitaly-pdb-override.json"' in content
    assert '.spec.maxUnavailable == 0 and .spec.minAvailable == null' in prepare
    assert '.items | length == 1' in prepare
    assert '.status.disruptionsAllowed // 0' in prepare
    assert 'value:1' in prepare
    assert 'phase:"prepared"' in prepare
    assert '.phase="overridden"' in prepare
    assert 'chmod 0600 "$tmp"' in prepare

    assert 'value:0' in restore
    assert '.metadata.uid == $uid' in restore
    assert 'gitaly-pdb-restored.json' in restore
    assert 'rm -f "$GITALY_PDB_OVERRIDE_FILE"' in restore

    drain = resize.split('if [[ "$interrupted" == false ]]', 1)[1].split(
        "ensure_server_stopped", 1
    )[0]
    assert drain.index('prepare_gitaly_pdb_override_for_node "$node"') < drain.index(
        'kubectl drain "$node"'
    )
    assert drain.index('kubectl drain "$node"') < drain.index(
        "restore_gitaly_pdb_override"
    )
    assert "drain_status=$?" in drain
    assert "checkpoint retained" in drain

    assert "trap migration_process_cleanup EXIT" in content
    assert "restore_gitaly_pdb_override" in content.split(
        "migration_process_cleanup()", 1
    )[1].split("trap migration_process_cleanup", 1)[0]
    recovery = content.split(
        '# A controller can be provisioned with a CLI-only bastion override', 1
    )[1].split('if [[ "$COMMAND" == rollback ]]', 1)[0]
    assert 'restore_gitaly_pdb_override' in recovery
    assert 'before $COMMAND' in recovery


def test_scale_in_reuses_checkpointed_gitaly_pdb_override_before_node_removal():
    content = MIGRATE.read_text(encoding="utf-8")
    remove = content.split("remove_cluster_node()", 1)[1].split(
        "scale_in_nodes()", 1
    )[0]

    prepare = remove.index('prepare_gitaly_pdb_override_for_node "$node"')
    drain = remove.index('kubectl drain "$node"')
    restore = remove.index("restore_gitaly_pdb_override")
    remove_node = remove.index('"$kubespray_dir/remove-node.yml"')
    delete_server = remove.index('hcloud server delete "$node"')
    assert prepare < drain < restore < remove_node < delete_server
    assert "drain_status=0" in remove
    assert "drain_status=$?" in remove
    assert "checkpoint retained" in remove
    assert "after restoring the Gitaly PDB" in remove

    # A signal uses the same EXIT restoration path, while a resumed finalize
    # or rollback restores the durable checkpoint before entering scale-in.
    cleanup = content.split("migration_process_cleanup()", 1)[1].split(
        "trap migration_process_cleanup EXIT", 1
    )[0]
    assert "restore_gitaly_pdb_override" in cleanup
    assert "Gitaly PDB restoration needs resume/rollback" in cleanup
    before_commands = content.split(
        '# A controller can be provisioned with a CLI-only bastion override', 1
    )[1].split('if [[ "$COMMAND" == rollback ]]', 1)[0]
    assert '"$COMMAND" == rollback || "$COMMAND" == finalize' in before_commands
    assert "restore_gitaly_pdb_override" in before_commands


def test_migration_enforces_control_plane_schedulability_in_both_directions():
    content = MIGRATE.read_text(encoding="utf-8")
    assert 'reconcile_control_plane_schedulability "$TARGET_CONFIG"' in content
    assert 'reconcile_control_plane_schedulability "$ROLLBACK_CONFIG"' in content
    assert 'check_control_plane_schedulability_contract "$STEADY_CONFIG"' in content
    assert 'check_control_plane_schedulability_contract "$TARGET_CONFIG"' in content
    assert "node-role.kubernetes.io/control-plane=:NoSchedule --overwrite" in content
    assert "node-role.kubernetes.io/control-plane:NoSchedule-" in content
    assert "node-role.kubernetes.io/master:NoSchedule-" in content
    assert 'schedulability-${node}.done' in content
    assert "Taint the complete control plane before the first eviction" in content
    assert "Apply the target's resource envelope before evacuating" in content
    assert content.index('run_playbook "$CONFIG_FILE" --skip-tags') < content.index(
        'reconcile_control_plane_schedulability "$TARGET_CONFIG"'
    )
    assert 'kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data' in content
    assert 'failed to evacuate ordinary workloads from dedicated control-plane node $node' in content
    assert "PROFILE_MIGRATION_VAULT_MEMBER_TIMEOUT_SECONDS" in content
    assert "while (( SECONDS < deadline ))" in content
    assert "Vault member $pod did not report initialized within" in content
    assert "etcdctl endpoint health --cluster' </dev/null" in content
    assert 'apply-target-platform.done' in content
    drain_reconcile = content.split("reconcile_control_plane_schedulability()", 1)[1].split(
        "stage_migrate_vault_storage()", 1
    )[0]
    assert drain_reconcile.index("unseal_vault_members") < drain_reconcile.index("check_etcd_health")


def test_cluster_management_checks_control_plane_taints_structurally():
    content = (
        ROOT / "roles" / "k8s-cluster-management" / "tasks" / "main.yml"
    ).read_text(encoding="utf-8")
    task = content.split("- name: Enforce dedicated control-plane scheduling contract", 1)[1]
    task = task.split("- name: Enforce schedulable control-plane contract", 1)[0]
    assert "kubectl get node \"$node\" -o json | jq -e" in task
    assert 'any(.spec.taints[]?;' in task
    assert '.key == "node-role.kubernetes.io/control-plane"' in task
    assert '.key == "node-role.kubernetes.io/master"' in task
    assert '.effect == "NoSchedule"' in task
    assert "jsonpath='{.spec.taints}'" not in task


def test_resume_and_finalize_accept_the_generated_target_as_active_config():
    content = MIGRATE.read_text(encoding="utf-8")
    assert 'persist_active_config "$TARGET_CONFIG"' in content
    assert 'persist_active_config "$ROLLBACK_CONFIG"' in content
    assert "finalize-reconcile-kubespray.done" in content
    assert "expand-kubespray.done" in content
    assert "-e skip_kubespray=true" in content
    assert '--start-at-task "' not in content
    cluster = (ROOT / "roles" / "k8s-cluster-management" / "tasks" / "main.yml").read_text()
    assert "Record the completed Kubespray reconciliation checkpoint" in cluster


def test_backup_retries_api_exports_and_never_keeps_partial_yaml():
    content = BACKUP.read_text(encoding="utf-8")
    assert "capture_with_retry()" in content
    assert "Helm release inventory failed after retries" in content
    assert "Helm manifest capture failed after retries" in content
    assert "kubectl_export()" in content
    assert "--request-timeout=90s" in content
    assert "for attempt in 1 2 3" in content
    assert 'temporary="${destination}.tmp"' in content


def test_replacement_restore_compares_cluster_uid_not_only_context_name():
    backup = BACKUP.read_text(encoding="utf-8")
    restore = RESTORE.read_text(encoding="utf-8")
    assert "SOURCE_CLUSTER_UID=" in backup
    assert "source_cluster_uid:$sourceClusterUid" in backup
    assert "TARGET_CLUSTER_UID=" in restore
    assert '[[ "$TARGET_CLUSTER_UID" != "$SOURCE_CLUSTER_UID" ]]' in restore
    assert "legacy bundle has no source cluster UID" in restore


def test_cloud_capture_is_portable_to_bash_32_with_nounset():
    content = BACKUP.read_text(encoding="utf-8")
    assert 'if [[ "$kind" == load-balancer ]]' in content
    assert "describe_args" not in content
    assert "describe_rc" in content


def test_migration_never_advances_after_a_failed_cluster_bundle():
    content = MIGRATE.read_text(encoding="utf-8")
    assert "encrypted cluster backup gate failed" in content
    backup_call = '"$SCRIPT_DIR/cluster-backup.sh" "${args[@]}"'
    failure_gate = '|| fail "encrypted cluster backup gate failed'
    assert backup_call in content
    assert content.index(backup_call) < content.index(failure_gate)
    assert '--vault-init-file "$VAULT_INIT_FILE"' in content


def test_migration_persists_and_revalidates_exact_backup_receipts_before_mutation():
    content = MIGRATE.read_text(encoding="utf-8")
    assert "record_backup_checkpoint()" in content
    assert "verify_recorded_backup()" in content
    assert ".backup_checkpoints[$checkpoint]" in content
    assert "archive_sha256:$archiveSha" in content
    assert "receipt_sha256:$receiptSha" in content
    assert 'migration_aws s3 cp "s3://${bucket}/${receipt_key}"' in content
    assert 'migration_aws s3 cp "s3://${bucket}/${checksum_key}"' in content
    assert 'migration_aws s3 cp "s3://${bucket}/${archive_key}"' in content
    assert 'cmp -s "$receipt" "${tmp_dir}/receipt.json"' in content
    assert 'cmp -s "${archive}.sha256" "${tmp_dir}/archive.sha256"' in content
    assert 'verify_recorded_backup pre-migration' in content
    assert 'verify_recorded_backup post-migration' in content
    rollback = content.split('if [[ "$COMMAND" == rollback ]]', 1)[0]
    assert rollback.rindex("verify_recorded_backup post-migration") > rollback.rindex(
        'if [[ "$DRY_RUN" != true'
    )


def test_finalization_refreshes_and_verifies_backup_before_destructive_retirement():
    content = MIGRATE.read_text(encoding="utf-8")
    declaration = re.search(r"FINALIZE_STAGES=\(([^)]*)\)", content)
    assert declaration
    stages = declaration.group(1).split()
    assert stages[0] == "final-backup"
    assert "PROFILE_MIGRATION_FINAL_BACKUP_MAX_AGE_SECONDS" in content
    finalize = content.split('if [[ "$COMMAND" == finalize ]]', 1)[1]
    refresh = finalize.index("refreshing verified finalization backup")
    status_transition = finalize.index('.status="finalizing"')
    retirement_loop = finalize.index(
        'for stage in "${FINALIZE_STAGES[@]}"', status_transition
    )
    assert refresh < status_transition < retirement_loop
    assert 'finalize_stage final-backup' in finalize[:status_transition]
    cluster_backup = content.split("cluster_backup()", 1)[1].split(
        "capture_live_bastion_type()", 1
    )[0]
    assert 'if [[ "$checkpoint" == finalization ]]' in cluster_backup
    assert 'verify_recorded_backup "$checkpoint" true' in cluster_backup


def _make_encrypted_fixture(
    tmp_path: Path,
    passphrase: str,
    vault_init_state: str = "legacy",
    unsafe_member: bool = False,
    receipt_schema: int = 2,
) -> Path:
    bundle_name = "test-cluster-20260716T000000Z"
    bundle = tmp_path / bundle_name
    bundle.mkdir()
    manifest = {
        "schema_version": 1 if vault_init_state == "legacy" else 2,
        "backup_id": bundle_name,
        "project": "test",
        "domain": "test.example.invalid",
        "profile": "production",
        "source_context": "source",
        "source_cluster_uid": "11111111-2222-3333-4444-555555555555",
        "provider_machine_types": {
            "bastion": "cpx22",
            "control_plane": "cpx42",
            "worker": "cpx42",
        },
        "completeness": "complete",
        "velero_backup": "completed",
        "velero_backup_name": "test-backup",
        "velero_storage_prefix": "test/velero",
        "pvc_protection_gate": {
            "status": "complete",
            "failures": 0,
            "evidence": "application-backups/pvc-protection-evidence.json",
        },
        "native_backup_catalog": {
            "included": True,
            "bundle_path": "application-backups/native-backups.json",
        },
        "contains": {"etcd_snapshot": True},
        "restore_order": ["infrastructure", "control-plane", "velero"],
    }
    if vault_init_state != "legacy":
        manifest["contains"]["vault_init_material"] = True
        manifest["recovery_dependencies"] = {
            "vault_init": {
                "required": True,
                "included": True,
                "encryption": "ansible-vault",
                "bundle_path": "config/vault-init.json.vault",
            }
        }
    if vault_init_state in {"included", "plaintext"}:
        config = bundle / "config"
        config.mkdir()
        value = (
            "$ANSIBLE_VAULT;1.1;AES256\nfixture-ciphertext\n"
            if vault_init_state == "included"
            else "plaintext-must-not-appear-in-output\n"
        )
        (config / "vault-init.json.vault").write_text(value, encoding="utf-8")
    config = bundle / "config"
    config.mkdir(exist_ok=True)
    (config / "platform.yaml").write_text(
        "global:\n  project: test\n  domain: test.example.invalid\n  email: ops@example.invalid\n"
        "network:\n  bastion:\n    server_type: cpx22\n"
        "infrastructure:\n  control_plane:\n    type: cpx42\n  workers:\n    type: cpx42\n",
        encoding="utf-8",
    )
    (config / "platform-secrets.yml").write_text(
        "$ANSIBLE_VAULT;1.1;AES256\nfixture-platform-secrets\n", encoding="utf-8"
    )
    for name, value in {
        "repository.bundle": "fixture bundle\n",
        "worktree.patch": "",
        "repository-untracked.tar": "fixture tar\n",
        "repository-untracked-files.txt": "",
        "repository-untracked-count.txt": "0\n",
        "git-status.txt": "",
        "git-revision.txt": "0123456789abcdef0123456789abcdef01234567\n",
    }.items():
        (config / name).write_text(value, encoding="utf-8")
    application = bundle / "application-backups"
    application.mkdir()
    (application / "native-backups.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "test",
                "completeness": "complete",
                "summary": {"expected": 0, "passed": 0, "failed": 0, "skipped": 0},
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    (application / "pvc-protection-evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "summary": {"failures": 0},
                "claims": [
                    {
                        "namespace": "data",
                        "name": "db-data",
                        "protected": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (application / "pod-volume-backups.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "spec": {
                            "pod": {"namespace": "data", "name": "db-0"},
                            "volume": "data",
                        },
                        "status": {"phase": "Completed"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    mounted = "data\tdb-0\tdata\n"
    (application / "mounted-pod-volumes.expected.tsv").write_text(
        mounted, encoding="utf-8"
    )
    (application / "mounted-pod-volumes.completed.tsv").write_text(
        mounted, encoding="utf-8"
    )
    (bundle / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    checksum_lines = []
    for path in sorted(item for item in bundle.rglob("*") if item.is_file()):
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        checksum_lines.append(f"{checksum}  ./{path.relative_to(bundle)}")
    (bundle / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    if unsafe_member:
        (bundle / "unsafe-link").symlink_to("/etc/passwd")
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
    if manifest["schema_version"] == 2:
        receipt = {
            "schema_version": receipt_schema,
            "receipt_type": "encrypted-cluster-backup",
            "backup_id": bundle_name,
            "project": "test",
            "domain": "test.example.invalid",
            "profile": "production",
            "source_context": "source",
            "source_cluster_uid": "11111111-2222-3333-4444-555555555555",
            "velero_backup_name": "test-backup",
            "velero_storage_prefix": "test/velero",
            "archive": encrypted.name,
            "sha256": digest,
            "completeness": "complete",
            "remote": {
                "published": True,
                "download_sha256_verified": True,
                "receipt_uploaded_last": True,
                "publication_state": "complete",
                "endpoint": "https://dr.example.test",
                "bucket": "test-dr",
                "archive_key": f"bundles/{bundle_name}/{encrypted.name}",
                "checksum_key": f"bundles/{bundle_name}/{encrypted.name}.sha256",
                "receipt_key": f"bundles/{bundle_name}/{encrypted.name}.manifest.json",
            },
        }
        if receipt_schema == 1:
            for identity_field in (
                "project",
                "domain",
                "profile",
                "source_context",
                "source_cluster_uid",
                "velero_storage_prefix",
            ):
                receipt.pop(identity_field)
        Path(f"{encrypted}.manifest.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
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


def test_restore_verifies_schema2_bundle_with_encrypted_vault_dependency(tmp_path):
    encrypted = _make_encrypted_fixture(
        tmp_path, "correct horse battery staple", vault_init_state="included"
    )
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
    assert '"vault_init_material": true' in result.stdout
    assert '"encryption": "ansible-vault"' in result.stdout


def test_restore_verify_accepts_legacy_schema1_completion_receipt(tmp_path):
    encrypted = _make_encrypted_fixture(
        tmp_path,
        "correct horse battery staple",
        vault_init_state="included",
        receipt_schema=1,
    )
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
    assert "verified bundle" in result.stdout


def test_restore_materializes_exact_private_operator_state(tmp_path):
    encrypted = _make_encrypted_fixture(
        tmp_path, "correct horse battery staple", vault_init_state="included"
    )
    output = tmp_path / "operator-state"
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    result = subprocess.run(
        [
            "bash",
            str(RESTORE),
            "--archive",
            str(encrypted),
            "--mode",
            "operator-state",
            "--output-dir",
            str(output),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    expected = {
        "platform.yaml",
        ".platform-secrets.yml",
        ".vault-init-test.json",
        "MANIFEST.json",
        "native-backups.json",
        "recovery-state.json",
    }
    assert expected <= {path.name for path in output.iterdir()}
    assert (output / "repository" / "git-revision.txt").read_text() == (
        "0123456789abcdef0123456789abcdef01234567\n"
    )
    recovery_state = json.loads((output / "recovery-state.json").read_text())
    assert recovery_state["provider_machine_types"] == {
        "bastion": "cpx22",
        "control_plane": "cpx42",
        "worker": "cpx42",
    }
    for path in output.rglob("*"):
        expected_mode = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode, path
    assert (output / ".platform-secrets.yml").read_text().endswith(
        "fixture-platform-secrets\n"
    )
    assert (output / ".vault-init-test.json").read_text().startswith(
        "$ANSIBLE_VAULT;"
    )
    assert "fixture-platform-secrets" not in result.stdout + result.stderr


def test_restore_operator_state_refuses_to_overwrite_existing_destination(tmp_path):
    encrypted = _make_encrypted_fixture(
        tmp_path, "correct horse battery staple", vault_init_state="included"
    )
    output = tmp_path / "operator-state"
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    result = subprocess.run(
        [
            "bash",
            str(RESTORE),
            "--archive",
            str(encrypted),
            "--mode",
            "operator-state",
            "--output-dir",
            str(output),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert sentinel.read_text() == "keep"
    assert "destination already exists" in result.stderr


def test_restore_rejects_link_members_before_extraction(tmp_path):
    encrypted = _make_encrypted_fixture(
        tmp_path, "correct horse battery staple", unsafe_member=True
    )
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    result = subprocess.run(
        ["bash", str(RESTORE), "--archive", str(encrypted), "--mode", "verify"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "unsafe path or member type" in result.stderr


def test_restore_rejects_schema2_bundle_without_atomic_completion_receipt(tmp_path):
    encrypted = _make_encrypted_fixture(
        tmp_path, "correct horse battery staple", vault_init_state="included"
    )
    Path(f"{encrypted}.manifest.json").unlink()
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    result = subprocess.run(
        ["bash", str(RESTORE), "--archive", str(encrypted), "--mode", "verify"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "completion receipt is missing" in result.stderr


def test_restore_rejects_schema2_bundle_with_nonfinal_receipt(tmp_path):
    encrypted = _make_encrypted_fixture(
        tmp_path, "correct horse battery staple", vault_init_state="included"
    )
    receipt_path = Path(f"{encrypted}.manifest.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["remote"]["publication_state"] = "pending_receipt"
    receipt["remote"]["receipt_uploaded_last"] = False
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    result = subprocess.run(
        ["bash", str(RESTORE), "--archive", str(encrypted), "--mode", "verify"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "completion receipt is incomplete or does not match" in result.stderr


@pytest.mark.parametrize(
    ("vault_init_state", "expected_error"),
    (
        ("missing", "missing required encrypted Vault initialization material"),
        ("plaintext", "not Ansible Vault encrypted"),
    ),
)
def test_restore_rejects_invalid_required_vault_dependency_without_exposure(
    tmp_path, vault_init_state, expected_error
):
    encrypted = _make_encrypted_fixture(
        tmp_path,
        "correct horse battery staple",
        vault_init_state=vault_init_state,
    )
    env = os.environ.copy()
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    result = subprocess.run(
        ["bash", str(RESTORE), "--archive", str(encrypted), "--mode", "verify"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert expected_error in result.stderr
    assert "plaintext-must-not-appear-in-output" not in result.stdout + result.stderr


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


def _mock_strict_restore_runtime(tmp_path: Path, backup_warnings: int = 0) -> tuple[Path, dict[str, str]]:
    runtime = tmp_path / "restore-runtime"
    scripts = runtime / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE, scripts / "cluster-restore.sh")
    shutil.copy2(SCRIPTS / "load-project-env.sh", scripts / "load-project-env.sh")
    health_marker = runtime / "health-called"
    (scripts / "health-gates.sh").write_text(
        f"#!/usr/bin/env bash\nset -eu\nprintf called > {health_marker!s}\nexit 99\n",
        encoding="utf-8",
    )
    (scripts / "health-gates.sh").chmod(0o755)
    bin_dir = runtime / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        """#!/usr/bin/env bash
set -eu
args="$*"
case "$args" in
  "cluster-info") exit 0 ;;
  "config current-context") printf 'replacement\\n' ;;
  "get namespace kube-system -o jsonpath={.metadata.uid}") printf 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' ;;
  "wait backupstoragelocation/default -n velero --for=jsonpath={.status.phase}=Available --timeout=300s") exit 0 ;;
  get\\ backup\\ test-backup\\ -n\\ velero\\ -o\\ json)
    printf '{"status":{"phase":"Completed","errors":0,"warnings":%s}}\\n' "${MOCK_BACKUP_WARNINGS:-0}" ;;
  "apply -f -") cat >/dev/null ;;
  get\\ restore*"-o jsonpath={.metadata.uid}") printf 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff' ;;
  get\\ restore*"-o jsonpath={.status.phase}") printf 'Completed' ;;
  get\\ restore*"-o json") printf '{"metadata":{"uid":"bbbbbbbb-cccc-dddd-eeee-ffffffffffff"},"spec":{"backupName":"test-backup"},"status":{"phase":"Completed","errors":0,"warnings":0}}\\n' ;;
  get\\ podvolumerestores*) printf '{"items":[{"status":{"phase":"Completed"}}]}\\n' ;;
  "get persistentvolumeclaims --all-namespaces -o json")
    printf '{"items":[{"metadata":{"namespace":"data","name":"db-data"},"status":{"phase":"Bound"}}]}\\n' ;;
  "get pods --all-namespaces -o json")
    printf '%s\\n' '{"items":[{"metadata":{"namespace":"data","name":"db-0"},"status":{"phase":"Running"},"spec":{"containers":[{"name":"db","volumeMounts":[{"name":"data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"db-data"}}]}}]}' ;;
  "wait nodes --all --for=condition=Ready --timeout=900s") exit 0 ;;
  "rollout status deployment/velero -n velero --timeout=10m") exit 0 ;;
  "rollout status daemonset/node-agent -n velero --timeout=10m") exit 0 ;;
  "get backupstoragelocation/default -n velero -o json")
    printf '{"status":{"phase":"Available"}}\\n' ;;
  *) printf 'unexpected kubectl invocation: %s\\n' "$args" >&2; exit 9 ;;
esac
""",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MOCK_BACKUP_WARNINGS"] = str(backup_warnings)
    env["CLUSTER_BACKUP_PASSPHRASE"] = "correct horse battery staple"
    env["CLUSTER_RESTORE_TIMEOUT_SECONDS"] = "5"
    return scripts / "cluster-restore.sh", env


def test_strict_velero_restore_requires_warning_review_and_full_coverage(tmp_path):
    encrypted = _make_encrypted_fixture(
        tmp_path, "correct horse battery staple", vault_init_state="included"
    )
    restore, env = _mock_strict_restore_runtime(tmp_path, backup_warnings=1)
    rejected = subprocess.run(
        [
            str(restore),
            "--archive",
            str(encrypted),
            "--mode",
            "velero",
            "--confirm",
            "RESTORE_test",
            "--force",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rejected.returncode != 0
    assert "explicitly allowed warning count" in rejected.stderr

    accepted = subprocess.run(
        [
            str(restore),
            "--archive",
            str(encrypted),
            "--mode",
            "velero",
            "--confirm",
            "RESTORE_test",
            "--force",
            "--allow-backup-warnings",
            "1",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert not (tmp_path / "restore-runtime" / "health-called").exists()
    evidence = list(tmp_path.glob("*.pod-volume-restores.json"))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())["items"][0]["status"]["phase"] == "Completed"
    storage_evidence = list(tmp_path.glob("*.backup-storage-location.json"))
    assert len(storage_evidence) == 1
    assert json.loads(storage_evidence[0].read_text())["status"]["phase"] == "Available"


def test_velero_restore_defers_full_health_until_native_replay():
    content = RESTORE.read_text(encoding="utf-8")
    assert "RESTORE_UID=$(kubectl get restore" in content
    assert 'velero.io/restore-uid=${RESTORE_UID}' in content
    assert 'velero.io/restore-name=${RESTORE_NAME}' not in content
    assert "--resume-restore" in content
    assert "resuming validation of existing Velero Restore" in content
    assert ".spec.backupName == $backup" in content
    assert ".spec.restorePVs == true" in content
    assert '.spec.existingResourcePolicy == "none"' in content
    assert '.spec.itemOperationTimeout == "8h0m0s"' in content
    assert "(.spec.excludedResources | sort) == ($excluded | sort)" in content
    assert content.count("leases.coordination.k8s.io") == 2
    assert "does not bind the exact full-scope restore contract" in content
    velero_mode = content.split('if [[ "$MODE" == velero ]]', 1)[1].split(
        'command -v kubectl >/dev/null || fail "kubectl is required for etcd safety checks"',
        1,
    )[0]
    assert "health-gates.sh" not in velero_mode
    assert "kubectl wait nodes --all --for=condition=Ready" in velero_mode
    assert "kubectl rollout status deployment/velero" in velero_mode
    assert "kubectl rollout status daemonset/node-agent" in velero_mode
    assert "full platform health remains blocked" in velero_mode
    native = NATIVE_RESTORE.read_text(encoding="utf-8")
    assert '"${SCRIPT_DIR}/health-gates.sh" --config "$CONFIG"' in native


def _mock_teardown_gate(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    archive = tmp_path / "test-cluster.tar.gz.enc"
    archive.write_bytes(b"verified encrypted recovery bundle")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = Path(f"{archive}.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    receipt = Path(f"{archive}.manifest.json")
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "receipt_type": "encrypted-cluster-backup",
                "backup_id": "test-cluster-20260722T000000Z",
                "created_at": subprocess.run(
                    ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip(),
                "project": "test",
                "source_cluster_uid": "11111111-2222-3333-4444-555555555555",
                "archive": archive.name,
                "sha256": digest,
                "completeness": "complete",
                "velero_backup_name": "test-backup",
                "velero_storage_prefix": "test/velero",
                "remote": {
                    "published": True,
                    "download_sha256_verified": True,
                    "receipt_uploaded_last": True,
                    "publication_state": "complete",
                    "endpoint": "https://dr.example.invalid",
                    "bucket": "test-dr",
                    "archive_key": f"bundles/{archive.name}",
                    "checksum_key": f"bundles/{archive.name}.sha256",
                    "receipt_key": f"bundles/{archive.name}.manifest.json",
                },
            }
        ),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "hcloud-called"
    (bin_dir / "kubectl").write_text(
        """#!/usr/bin/env bash
set -eu
case "$*" in
  "get namespace kube-system -o jsonpath={.metadata.uid}") printf '%s' "$MOCK_SOURCE_UID" ;;
  "get nodes -o json") printf '{"items":[]}' ;;
  *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    (bin_dir / "hcloud").write_text(
        f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> {marker!s}
case "${{2:-}}" in
  list) printf '[]\\n' ;;
  describe) exit 1 ;;
  *) exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    (bin_dir / "aws").write_text(
        """#!/usr/bin/env bash
set -eu
source_path="$5"
destination="$6"
case "$source_path" in
  *.manifest.json) cp "$MOCK_REMOTE_RECEIPT" "$destination" ;;
  *.sha256) cp "$MOCK_REMOTE_CHECKSUM" "$destination" ;;
  *) cp "$MOCK_REMOTE_ARCHIVE" "$destination" ;;
esac
""",
        encoding="utf-8",
    )
    for executable in bin_dir.iterdir():
        executable.chmod(0o755)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(home),
            "HCLOUD_TOKEN": "test-token",
            "BACKUP_DR_ENDPOINT": "https://dr.example.invalid",
            "BACKUP_DR_BUCKET": "test-dr",
            "BACKUP_DR_ACCESS_KEY": "test-access-key",
            "BACKUP_DR_SECRET_KEY": "test-secret-key-value",
            "MOCK_SOURCE_UID": "11111111-2222-3333-4444-555555555555",
            "MOCK_REMOTE_RECEIPT": str(receipt),
            "MOCK_REMOTE_CHECKSUM": str(checksum),
            "MOCK_REMOTE_ARCHIVE": str(archive),
            "PROJECT_ENV_FILE": str(tmp_path / "absent.env"),
        }
    )
    return receipt, marker, env


@pytest.mark.parametrize("mutation", ("project", "source_uid", "stale"))
def test_teardown_receipt_gate_fails_before_any_hcloud_call(tmp_path, mutation):
    receipt, marker, env = _mock_teardown_gate(tmp_path)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if mutation == "project":
        data["project"] = "another-project"
    elif mutation == "source_uid":
        data["source_cluster_uid"] = "ffffffff-2222-3333-4444-555555555555"
    else:
        data["created_at"] = "2000-01-01T00:00:00Z"
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = subprocess.run(
        [
            str(TEARDOWN),
            "test",
            "--confirm",
            "test",
            "--require-backup-receipt",
            str(receipt),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert not marker.exists()


def test_teardown_receipt_gate_rechecks_remote_bytes_before_hcloud(tmp_path):
    receipt, marker, env = _mock_teardown_gate(tmp_path)
    tampered = tmp_path / "tampered-remote-archive"
    tampered.write_bytes(b"tampered")
    env["MOCK_REMOTE_ARCHIVE"] = str(tampered)
    result = subprocess.run(
        [
            str(TEARDOWN),
            "test",
            "--confirm",
            "test",
            "--require-backup-receipt",
            str(receipt),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "remote encrypted backup archive failed" in result.stderr
    assert not marker.exists()


def test_teardown_accepts_recent_remote_verified_receipt(tmp_path):
    receipt, marker, env = _mock_teardown_gate(tmp_path)
    result = subprocess.run(
        [
            str(TEARDOWN),
            "test",
            "--confirm",
            "test",
            "--require-backup-receipt",
            str(receipt),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    assert "Verified recent local and remote recovery bundle" in result.stdout


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
    profile["network"]["bastion"]["server_type"] = "cpx22"
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    env = os.environ.copy()
    env["PROFILE_MIGRATION_STATE_DIR"] = str(tmp_path / "state")
    env["PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB"] = ""
    env["PROFILE_MIGRATION_VOLUME_SAFETY_MARGIN_GIB"] = "100"
    ssh_key = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts-offline-plan"
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
            "--ssh-key-path",
            str(ssh_key),
            "--ssh-known-hosts",
            str(known_hosts),
            "--api-port",
            "16444",
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
    assert target["infrastructure"]["ssh_key_path"] == str(ssh_key)
    assert target["k8s_api_local_port"] == 16444
    assert yaml.safe_load((state / "source-platform.yaml").read_text())["infrastructure"][
        "ssh_key_path"
    ] == str(ssh_key)
    assert yaml.safe_load((state / "source-platform.yaml").read_text())[
        "k8s_api_local_port"
    ] == 16444
    assert expansion["platform_profile"] == "custom"
    assert expansion["tier"] == "minimal"
    assert expansion["infrastructure"]["control_plane"]["count"] == 3
    assert expansion["infrastructure"]["workers"]["count"] == 3
    for generated_name in (
        "source-platform.yaml",
        "target-platform.yaml",
        "target-transition-platform.yaml",
        "expansion-platform.yaml",
        "backup-platform.yaml",
        "rollback-platform.yaml",
    ):
        generated = yaml.safe_load((state / generated_name).read_text())
        assert generated["network"]["bastion"]["server_type"] == "cpx22"
    assert (state / "bastion-type-retention.tsv").read_text() == (
        "source-declared\tcpx22\ttarget-requested\tcpx22\tretained\tcpx22\n"
    )
    capacity = json.loads((state / "volume-capacity-plan.json").read_text())
    assert capacity["source"]["persistent_total_gib"] == 250
    assert capacity["target"]["persistent_total_gib"] == 1360
    # Migration delta is claim-by-claim: the 30 GiB source Loki/VMSingle
    # claims remain retained during cutover rather than offsetting new claims.
    assert capacity["target_delta_gib"] == 1140
    assert capacity["migration_scratch_gib"] == 50
    assert capacity["required_additional_gib"] == 1190
    assert capacity["planning_inputs"] == {
        "configured_account_quota_gib": None,
        "safety_margin_gib": 100,
        "live_account_usage_required": True,
    }
    assert capacity["minimum_required_headroom_gib"] == 1290
    assert capacity["offline_result"] == (
        "replacement-restore-required"
        if capacity["storage_class_changes"]
        else "quota-required-before-execute"
    )
    assert "Required additional capacity before safety margin: 1190 GiB" in result.stdout


@pytest.mark.parametrize(
    ("option", "field", "message"),
    (
        ("--ssh-key-path", "ssh_key_path", "--ssh-key-path does not match"),
        (
            "--ssh-known-hosts",
            "ssh_known_hosts_file",
            "--ssh-known-hosts does not match",
        ),
    ),
)
def test_migration_rejects_explicit_ssh_state_drift(tmp_path, option, field, message):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml").read_text()
    )
    profile["global"].update(
        {"project": "ssh-state", "domain": "cluster.example", "email": "ops@example.com"}
    )
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    state_base = tmp_path / "state"
    state = state_base / "ssh-state-minimal-to-production"
    state.mkdir(parents=True)
    recorded = str(tmp_path / f"recorded-{field}")
    (state / "state.json").write_text(
        json.dumps(
            {
                "project": "ssh-state",
                "source_profile": "minimal",
                "target_profile": "production",
                "operator_state": {field: recorded},
            }
        ),
        encoding="utf-8",
    )
    (state_base / "ssh-state-active-profile-migration").write_text(
        str(state), encoding="utf-8"
    )
    env = os.environ.copy()
    env["PROFILE_MIGRATION_STATE_DIR"] = str(state_base)
    result = subprocess.run(
        [
            "bash",
            str(MIGRATE),
            "--config",
            str(config),
            option,
            str(tmp_path / f"different-{field}"),
            "status",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert message in result.stderr


def test_migration_rejects_explicit_api_port_state_drift(tmp_path):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml").read_text()
    )
    profile["global"].update(
        {"project": "api-state", "domain": "cluster.example", "email": "ops@example.com"}
    )
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    state_base = tmp_path / "state"
    state = state_base / "api-state-minimal-to-production"
    state.mkdir(parents=True)
    (state / "state.json").write_text(
        json.dumps(
            {
                "project": "api-state",
                "source_profile": "minimal",
                "target_profile": "production",
                "operator_state": {"k8s_api_local_port": 16444},
            }
        ),
        encoding="utf-8",
    )
    (state_base / "api-state-active-profile-migration").write_text(
        str(state), encoding="utf-8"
    )
    env = os.environ.copy()
    env["PROFILE_MIGRATION_STATE_DIR"] = str(state_base)
    result = subprocess.run(
        [
            "bash",
            str(MIGRATE),
            "--config",
            str(config),
            "--api-port",
            "16445",
            "status",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "--api-port does not match" in result.stderr


def test_migration_uses_persisted_identity_and_project_known_hosts_everywhere():
    content = MIGRATE.read_text(encoding="utf-8")
    assert "--ssh-key-path FILE" in content
    assert "--ssh-known-hosts FILE" in content
    assert "--api-port PORT" in content
    assert ".operator_state.ssh_key_path=$sshKey" in content
    assert ".operator_state.ssh_known_hosts_file=$knownHosts" in content
    assert ".operator_state.k8s_api_local_port=$apiPort" in content
    assert '-e "ssh_key_path=$SSH_KEY_PATH"' in content
    assert '-e "k8s_api_local_port=$K8S_API_LOCAL_PORT"' in content
    assert '--ssh-identity "$SSH_KEY_PATH" --ssh-known-hosts "$SSH_KNOWN_HOSTS_FILE"' in content
    assert '-o "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}"' in content
    assert "UserKnownHostsFile=${quoted_known_hosts}" in content
    assert 'set_yaml_string "$SOURCE_CONFIG"' in content
    assert 'set_yaml_string "$TARGET_CONFIG"' in content


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
    env["PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB"] = ""
    env["PROFILE_MIGRATION_VOLUME_SAFETY_MARGIN_GIB"] = "100"
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
    capacity = json.loads((state / "volume-capacity-plan.json").read_text())
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
    assert capacity["source"]["profile"] == source
    assert capacity["target"]["profile"] == target
    assert capacity["required_additional_gib"] == (
        capacity["target_delta_gib"] + capacity["migration_scratch_gib"]
    )
    assert capacity["required_additional_gib"] >= 0
    assert capacity["offline_result"] == (
        "replacement-restore-required"
        if capacity["storage_class_changes"]
        else "quota-required-before-execute"
    )


def test_live_migration_requires_explicit_hetzner_volume_quota(tmp_path):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml").read_text()
    )
    profile["global"].update(
        {"project": "capacity-quota", "domain": "cluster.example", "email": "ops@example.com"}
    )
    profile["secrets"]["enabled"] = False
    profile["secrets"]["eso"]["enabled"] = False
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    secrets = tmp_path / "secrets.yml"
    secrets.write_text("placeholder: true\n", encoding="utf-8")
    env = os.environ.copy()
    env["PROFILE_MIGRATION_STATE_DIR"] = str(tmp_path / "state")
    env["PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB"] = ""
    env["PROFILE_MIGRATION_VOLUME_SAFETY_MARGIN_GIB"] = "100"
    result = subprocess.run(
        [
            "bash", str(MIGRATE), "--config", str(config), "--target", "small",
            "--dr-endpoint", "https://s3.external.example", "--dr-bucket", "cluster-dr",
            "--secrets-file", str(secrets), "--force", "execute",
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "set an explicit positive Hetzner account volume quota" in result.stderr
    assert not (tmp_path / "state" / "capacity-quota-active-profile-migration").exists()


def test_volume_capacity_estimator_fails_on_unknown_quantity(tmp_path):
    source = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / "minimal.yaml").read_text()
    )
    source["storage"]["size"] = "100GB"
    source_file = tmp_path / "source.yaml"
    target_file = tmp_path / "target.yaml"
    source_file.write_text(yaml.safe_dump(source), encoding="utf-8")
    target_file.write_text(
        (ROOT / "platform-orchestrator" / "profiles" / "small.yaml").read_text(),
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(STORAGE_CAPACITY), "--source", str(source_file), "--target", str(target_file)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "unsupported Kubernetes storage quantity" in result.stderr


def test_migration_capacity_gate_is_authoritative_persisted_and_resume_aware():
    content = MIGRATE.read_text(encoding="utf-8")
    assert 'VOLUME_QUOTA_GIB="${PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB:-}"' in content
    assert 'hcloud volume list -o json' in content
    assert 'kubectl get pv -o json' in content
    assert 'volume-capacity-plan.json' in content
    assert 'baseline.volumes' in content
    assert 'migration_capacity_consumed_gib' in content
    assert 'projected_peak_with_margin_gib' in content
    assert 'if [[ "$COMMAND" == resume' in content
    assert content.count("check_volume_capacity") >= 4


@pytest.mark.parametrize(
    ("source", "target", "path", "active_value"),
    (
        ("minimal", "production", ("applications", "daytona", "enabled"), True),
        ("minimal", "production", ("compliance", "hipaa", "enabled"), True),
        (
            "minimal",
            "production",
            ("backup", "disaster_recovery", "enabled"),
            True,
        ),
        ("production", "medium", ("blackbox", "enabled"), False),
    ),
)
def test_migration_preserves_explicit_component_selection_overrides(
    tmp_path, source, target, path, active_value
):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / f"{source}.yaml").read_text()
    )
    project = f"selection-{source}-to-{target}-{path[-2]}"
    profile["global"].update(
        {"project": project, "domain": "cluster.example", "email": "ops@example.com"}
    )
    selected = profile
    for key in path[:-1]:
        selected = selected[key]
    selected[path[-1]] = active_value
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")
    state_base = tmp_path / "state"
    env = os.environ.copy()
    env.update(
        {
            "PROFILE_MIGRATION_STATE_DIR": str(state_base),
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
    state = state_base / f"{project}-{source}-to-{target}-plan"
    generated = yaml.safe_load((state / "target-platform.yaml").read_text())
    selected = generated
    for key in path:
        selected = selected[key]
    assert selected is active_value
    retention = (state / "selection-retention.tsv").read_text()
    assert "." + ".".join(path) in retention


def test_profile_migration_never_schedules_generic_hipaa_retirement():
    content = MIGRATE.read_text(encoding="utf-8")
    removal_order = content.split("components_to_remove()", 1)[1].split(
        "refuse_automatic_hipaa_retirement()", 1
    )[0]
    assert "for component in daytona hipaa" not in removal_order
    assert "HIPAA-oriented hardening cannot be retired by profile migration" in content
    remove_disabled = content.split("remove_disabled_components()", 1)[1].split(
        "capture_observability_pvcs()", 1
    )[0]
    assert "refuse_automatic_hipaa_retirement" in remove_disabled


def test_disabled_source_dependency_disables_new_target_dependants(tmp_path):
    config = tmp_path / "platform.yaml"
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator" / "profiles" / "small.yaml").read_text()
    )
    profile["global"].update(
        {
            "project": "selection-closure",
            "domain": "cluster.example",
            "email": "ops@example.com",
        }
    )
    profile["dragonfly"]["enabled"] = False
    profile["gitlab"]["enabled"] = False
    profile["gitlab"]["runner"]["enabled"] = False
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
            "plan",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    target = yaml.safe_load(
        (
            state_base
            / "selection-closure-small-to-production-plan"
            / "target-platform.yaml"
        ).read_text()
    )
    assert target["dragonfly"]["enabled"] is False
    assert target["gitlab"]["enabled"] is False
    assert target["gitlab"]["runner"]["enabled"] is False
    assert target["postal"]["enabled"] is False
    assert target["glitchtip"]["enabled"] is False


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
    assert target["storage"]["size_per_replica"] == "100Gi"
    assert target["databases"]["postgresql"]["storage_size"] == "50Gi"
    assert target["observability"]["metrics"]["storage_size"] == "100Gi"
    assert target["alerting"]["storage_size"] == "10Gi"
    assert "seaweedfs-volume\t100Gi\t40Gi" in retention
    assert "seaweedfs-index\t4Gi\t2Gi" in retention
    assert "postgresql\t50Gi\t30Gi" in retention
    assert "alertmanager\t10Gi\t5Gi" in retention
    assert target["storage"]["master_replicas"] == 3
    assert target["storage"]["volume_replicas"] == 3
    assert target["observability"]["metrics"]["replicas"] == 2
    assert target["observability"]["metrics"]["replication_factor"] == 2
    assert "seaweedfs-master" not in stateful_retention
    assert "victoriametrics-cluster\t2\t1" in stateful_retention
    assert "victoriametrics-replication-factor\t2\t1" in stateful_retention


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
        "migrate-vault-storage",
        "apply-target",
        "migrate-data",
        "validate",
        "post-backup",
    ):
        assert stage in content
    assert "cluster-backup.sh" in content
    assert "kubectl drain" in content
    assert "wait_for_node_runtime" in content
    assert "maintain_node_root_disk" in content
    assert "expand_node_root_disk" in content
    assert "primary_disk_size" in content
    assert "preserve_non_shrinking_node_types" in content
    assert "node-type-retention.tsv" in content
    assert "bastion-type-retention.tsv" in content
    capture_bastion = content.split("capture_live_bastion_type()", 1)[1].split(
        "preserve_non_shrinking_node_types()", 1
    )[0]
    assert 'hcloud server describe "${PROJECT}-bastion" -o json' in capture_bastion
    assert 'set_yaml_string "$config" \'.network.bastion.server_type\' "$live_type"' in capture_bastion
    assert '.bastion={server:$server' in capture_bastion
    assert "resize_supported:false" in capture_bastion
    assert "cannot shrink" in content
    assert 'for config in "$TARGET_CONFIG" "$STEADY_CONFIG" "$ROLLBACK_CONFIG"' in content
    assert 'current_cores" != "$target_cores' in content
    assert "ensure_server_stopped" in content
    assert 'change_args=(server change-type "$node" "$target_type")' in content
    assert 'change_args+=(--keep-disk)' in content
    assert 'growpart "/dev/$parent" "$partnum"' in content
    assert 'resize2fs "$root_source"' in content
    assert "SSH did not recover" in content
    resize_node = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]
    assert 'wait_for_platform_convergence "$SOURCE_CONFIG"' in resize_node
    assert 'check_platform_health "$TARGET_CONFIG"' not in resize_node
    assert "condition=DiskPressure=False" in content
    assert "crictl rmi --prune" in content
    assert "wait_for_api_ready" in content
    assert "unseal_vault_members" in content
    assert "ANSIBLE_VAULT_PASSWORD_FILE" in content
    assert "vault operator unseal" in content
    assert 'spec.nodeName=${node}' in content
    assert 'csi.hetzner.cloud' in content
    assert "etcdctl endpoint health --cluster" in content
    cluster_management = (ROOT / "roles/k8s-cluster-management/tasks/main.yml").read_text()
    assert "Bound Kubespray SSH multiplex lifetime and detect dead bastion paths" in cluster_management
    assert "ServerAliveInterval=15" in cluster_management
    assert "ServerAliveCountMax=3" in cluster_management
    assert "ConnectionAttempts=10" in cluster_management
    assert "Nodes were deliberately retained".lower() in content.lower()
    assert "FINALIZE_STAGES" in content
    assert "active_config:$activeConfig" in content
    assert 'persist_active_config "$TARGET_CONFIG"' in content
    assert 'persist_active_config "$ROLLBACK_CONFIG"' in content
    assert "remove-node.yml" in content
    assert content.index("remove-node.yml") < content.index('hcloud server delete "$node"')
    assert "hetzner_allow_destructive_reconcile=true" in content
    placement_group = content.split("ensure_spread_placement_group()", 1)[1].split(
        "stage_expand()", 1
    )[0]
    assert '--label "project=$PROJECT"' in placement_group
    assert 'add-label --overwrite "$placement_group" "project=$PROJECT"' in placement_group
    assert "grep -qi 'not found' <<<\"$result\"" in placement_group
    assert "kubectl delete vmsingle" in content
    assert "kubectl delete vmcluster" in content
    assert "helm uninstall" in content
    assert "helm status promtail -n logging-agents" in content
    assert "--vm-native-src-addr=${source_addr}" in content
    assert "--vm-native-dst-addr=${target_addr}" in content
    assert "http://vmselect-vmcluster.monitoring.svc:8481/select/0/prometheus" in content
    assert "http://vmsingle-vmsingle.monitoring.svc:8429" in content
    assert "--vm-native-filter-time-start" in content
    assert "${4:-1970-01-01T00:00:00Z}" in content
    assert "            - -s" in content
    assert "            - --disable-progress-bar" in content
    assert "storage-retention.tsv" in content
    assert "stateful-retention.tsv" in content
    assert "selection-retention.tsv" in content
    assert "--operator-state-root" in content
    assert "--secrets-file" in content
    assert "--vault-init-file" in content
    assert "operator_state:{root:$operatorStateRoot,secrets_file:$secretsFile" in content
    assert 'restore_persisted_operator_state' in content
    assert 'validate_operator_state_inputs' in content
    assert '--secrets-file "$SECRETS_FILE"' in content
    run_playbook = content.split("run_playbook()", 1)[1].split(
        "persist_active_config()", 1
    )[0]
    assert '-e "secrets_file=$SECRETS_FILE"' in run_playbook
    assert '-e "vault_init_output_file=$VAULT_INIT_FILE"' in run_playbook
    unseal = content.split("unseal_vault_members()", 1)[1].split(
        "resize_node()", 1
    )[0]
    assert 'init_file="$VAULT_INIT_FILE"' in unseal
    assert "VAULT_INIT_OUTPUT_FILE" not in unseal
    remove_components = content.split("remove_disabled_components()", 1)[1].split(
        "capture_observability_pvcs()", 1
    )[0]
    assert remove_components.count('-e "secrets_file=$SECRETS_FILE"') == 3
    assert remove_components.count('-e "vault_init_output_file=$VAULT_INIT_FILE"') == 3
    assert "deployments,statefulsets,daemonsets" in content
    assert 'if ! HEALTH_REQUIRE_ARGOCD="$require_argocd"' in content
    assert '"$SCRIPT_DIR/health-gates.sh"; then' in content
    assert "return 1" in content
    assert "certificates --all-namespaces" in content
    assert "PerconaPGCluster".lower() in content.lower()
    assert "PerconaServerMongoDB".lower() in content.lower()
    assert "-o IdentitiesOnly=yes" in content
    assert 'ProxyCommand=${proxy_command}' in content
    assert "ETCDCTL_API=3" not in content
    assert "vault-storage-migrate.sh" in content
    assert "--skip-tags infrastructure,network,security,cluster" in content
    assert "post-target-backup-platform.yaml" in content
    assert 'run_playbook "$POST_BACKUP_CONFIG" --tags databases,gitlab,backup' in content
    assert 'run_playbook "$BACKUP_CONFIG" --tags databases,gitlab,backup' in content
    final_backup = content.split("final-backup)", 1)[1].split("retire-backup)", 1)[0]
    assert 'run_playbook "$POST_BACKUP_CONFIG" --tags databases,gitlab,backup' in final_backup
    assert 'cluster_backup "$POST_BACKUP_CONFIG"' in final_backup
    assert 'if ! component_enabled "$TARGET_CONFIG" backup' in content
    assert "-e target_component=backup -e confirm_component_removal=backup" in content
    assert 'if ! component_enabled "$TARGET_CONFIG" disaster-recovery' in content
    assert (
        "-e target_component=disaster-recovery "
        "-e confirm_component_removal=disaster-recovery"
    ) in content
    assert (
        '[[ "$component" == backup || "$component" == disaster-recovery ]] '
        "&& continue"
    ) in content


def test_migration_resize_rechecks_provider_off_after_placement_before_type_change():
    content = MIGRATE.read_text(encoding="utf-8")
    resize = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]
    mutation = resize.index('hcloud server add-to-placement-group')
    change_type = resize.index('change_server_type_with_retry "$node" "$target_type"')
    stop_checks = []
    offset = 0
    while True:
        position = resize.find('ensure_server_stopped "$node"', offset)
        if position < 0:
            break
        stop_checks.append(position)
        offset = position + 1

    assert len(stop_checks) == 2
    assert stop_checks[0] < mutation < stop_checks[1] < change_type
    retry = content.split("change_server_type_with_retry()", 1)[1].split(
        "ensure_server_running()", 1
    )[0]
    assert 'hcloud "${change_args[@]}"' in retry
    assert 'hcloud server poweroff "$node"' not in resize
    assert 'kubectl uncordon "$node"' in resize


def test_migration_resize_waits_for_stateful_data_paths_before_next_drain():
    content = MIGRATE.read_text(encoding="utf-8")
    resize = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]
    converged = resize.split(
        'if [[ "$current_type" == "$target_type"', 1
    )[1].split("return 0", 1)[0]

    assert "PROFILE_MIGRATION_PLATFORM_CONVERGENCE_TIMEOUT_SECONDS" in content
    assert "PROFILE_MIGRATION_PLATFORM_CONVERGENCE_INTERVAL_SECONDS" in content
    assert 'wait_for_platform_convergence "$SOURCE_CONFIG" "pre-resize health for $node"' in resize
    assert 'wait_for_platform_convergence "$SOURCE_CONFIG" "post-resize health for $node"' in resize
    assert resize.index('"pre-resize health for $node"') < resize.index('kubectl drain "$node"')
    assert resize.index('kubectl uncordon "$node"') < resize.rindex('"post-resize health for $node"')
    assert '"post-resize health for $node"' in converged
    assert 'in_progress="${node_state_dir}/${node}.in-progress"' in resize
    assert 'done_marker="${node_state_dir}/${node}.done"' in resize
    assert '[[ -f "$in_progress" || "$node_unschedulable" == true ]]' in resize
    assert '"$node_unschedulable" == true' in resize
    assert 'rm -f "$in_progress"' in converged

    probes = content.split("check_stateful_data_paths()", 1)[1].split(
        "wait_for_platform_convergence()", 1
    )[0]
    assert "SeaweedFS S3 write/read/delete probe failed" in probes
    assert "s3 cp /tmp/value" in probes
    assert 'test \\"$(cat /tmp/read)\\" = \\"$HOSTNAME\\"' in probes
    assert "curlimages/curl:8.17.0" in probes
    assert "loki/api/v1/push" in probes
    assert "loki/api/v1/query_range" in probes
    assert "X-Scope-OrgID: fake" in probes
    assert "profile-migration-probe" in probes
    assert "Loki fresh push/query probe failed" in probes
    assert "cleanup_probe_pod storage" in probes
    assert "cleanup_probe_pod monitoring" in probes
    assert '"app.kubernetes.io/component":"data-path-probe"' in probes
    assert "cleanup_stale_data_path_probes" in content


def test_platform_convergence_retries_transient_failures(tmp_path):
    content = MIGRATE.read_text(encoding="utf-8")
    helper = "wait_for_platform_convergence()" + content.split(
        "wait_for_platform_convergence()", 1
    )[1].split("ssh_args_for_facts()", 1)[0]
    calls = tmp_path / "calls"
    harness = f'''set -euo pipefail
PLATFORM_CONVERGENCE_TIMEOUT=5
PLATFORM_CONVERGENCE_INTERVAL=1
CALLS={str(calls)!r}
printf '0\\n' > "$CALLS"
warn() {{ :; }}
log() {{ :; }}
fail() {{ echo "$*" >&2; exit 1; }}
check_platform_health() {{
  count=$(cat "$CALLS")
  count=$((count + 1))
  printf '%s\\n' "$count" > "$CALLS"
  (( count >= 3 ))
}}
check_stateful_data_paths() {{ :; }}
cleanup_stale_data_path_probes() {{ :; }}
{helper}
wait_for_platform_convergence /unused "test convergence"
test "$(cat "$CALLS")" = 3
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ensure_server_stopped_handles_provider_restart_after_placement(tmp_path):
    content = MIGRATE.read_text(encoding="utf-8")
    helper = "ensure_server_stopped()" + content.split(
        "ensure_server_stopped()", 1
    )[1].split("ensure_server_running()", 1)[0]
    status_file = tmp_path / "status"
    calls_file = tmp_path / "poweroff-calls"
    timeout_file = tmp_path / "simulate-client-timeout"
    harness = f'''set -euo pipefail
HCLOUD_CLIENT_TIMEOUT=2
HCLOUD_STATE_TIMEOUT=2
STATUS_FILE={str(status_file)!r}
CALLS_FILE={str(calls_file)!r}
TIMEOUT_FILE={str(timeout_file)!r}
printf 'running\\n' > "$STATUS_FILE"
printf '0\\n' > "$CALLS_FILE"
printf '0\\n' > "$TIMEOUT_FILE"
server_status() {{ cat "$STATUS_FILE"; }}
wait_for_server_settled() {{ server_status "$1"; }}
run_with_timeout() {{
  count=$(cat "$CALLS_FILE")
  printf '%s\\n' "$((count + 1))" > "$CALLS_FILE"
  printf 'off\\n' > "$STATUS_FILE"
  [[ $(cat "$TIMEOUT_FILE") == 0 ]]
}}
warn() {{ :; }}
fail() {{ printf '%s\\n' "$*" >&2; exit 1; }}
sleep() {{ :; }}
{helper}
ensure_server_stopped worker-1
# Placement-group reconciliation reproduced the live provider transition.
printf 'running\\n' > "$STATUS_FILE"
# The provider accepts the second power-off even though the client times out.
printf '1\\n' > "$TIMEOUT_FILE"
ensure_server_stopped worker-1
[[ $(cat "$STATUS_FILE") == off ]]
[[ $(cat "$CALLS_FILE") == 2 ]]
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_migration_retries_transient_provider_capacity_before_failing():
    content = MIGRATE.read_text(encoding="utf-8")
    helper = "change_server_type_with_retry()" + content.split(
        "change_server_type_with_retry()", 1
    )[1].split("ensure_server_running()", 1)[0]
    resize = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]

    assert "PROFILE_MIGRATION_HCLOUD_CAPACITY_RETRY_ATTEMPTS" in content
    assert "PROFILE_MIGRATION_HCLOUD_CAPACITY_RETRY_INTERVAL_SECONDS" in content
    assert 'ensure_server_stopped "$node"' in helper
    assert 'change_args=(server change-type "$node" "$target_type")' in helper
    assert 'hcloud "${change_args[@]}"' in helper
    assert helper.index('ensure_server_stopped "$node"') < helper.index(
        'hcloud "${change_args[@]}"'
    )
    assert "attempt<=HCLOUD_CAPACITY_RETRY_ATTEMPTS" in helper
    assert "delay > 60" in helper
    assert 'change_server_type_with_retry "$node" "$target_type" "$target_disk" "$keep_disk"' in resize


def test_migration_records_only_authorized_equivalent_capacity_fallbacks():
    content = MIGRATE.read_text(encoding="utf-8")
    selector = content.split("select_equivalent_fallback_type()", 1)[1].split(
        "change_server_type_with_retry()", 1
    )[0]
    resize = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]

    assert "PROFILE_MIGRATION_HCLOUD_EQUIVALENT_FALLBACK_TYPES" in content
    assert "server_type_available_for_node" in selector
    assert "requested_cores" in selector
    assert "requested_memory" in selector
    assert "requested_arch" in selector
    assert "requested_cpu" in selector
    assert "node-type-overrides.tsv" in content
    assert ".infrastructure.node_type_overrides[strenv(NODE)]" in content
    assert "equivalent-capacity-fallback" in content
    assert "no equivalent fallback was authorized" in resize
    assert 'target_disk="$current_disk"' in resize
    assert 'change_args+=(--keep-disk)' in content


def test_resize_stage_recovers_exact_in_progress_node_before_ordered_loop():
    content = MIGRATE.read_text(encoding="utf-8")
    stage = content.split("stage_resize()", 1)[1].split(
        "control_plane_nodes()", 1
    )[0]

    assert "*.in-progress" in stage
    assert "multiple resize nodes are marked in progress" in stage
    assert "resize marker references unknown node" in stage
    assert 'resume_node=$(basename "$marker" .in-progress)' in stage
    assert stage.index('resize_node "${nodes[$i]}"') < stage.rindex(
        'for i in "${!nodes[@]}"'
    )
    assert '[[ "${nodes[$i]}" == "$resume_node" ]] && continue' in stage


def test_interrupted_resize_skips_pre_drain_ssh_until_server_is_recovered():
    content = MIGRATE.read_text(encoding="utf-8")
    resize = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]
    interrupted = resize.split('if [[ "$interrupted" == false ]]', 1)[1].split(
        "ensure_server_stopped", 1
    )[0]

    assert "interrupted=false" in resize
    assert "interrupted=true" in resize
    assert 'maintain_node_root_disk "$node"' in interrupted
    assert 'kubectl drain "$node"' in interrupted
    assert "skipping completed pre-drain work for interrupted node" in interrupted
    assert resize.index('if [[ "$interrupted" == false ]]') < resize.index(
        'maintain_node_root_disk "$node"', resize.index('if [[ "$interrupted" == false ]]')
    )


def test_vmctl_migration_job_uses_restricted_pod_security():
    content = MIGRATE.read_text(encoding="utf-8")
    vmctl_job = content.split("run_vmctl_migration()", 1)[1].split(
        "vm_addresses()", 1
    )[0]
    for contract in (
        "automountServiceAccountToken: false",
        "runAsNonRoot: true",
        "runAsUser: 1000",
        "seccompProfile:",
        "allowPrivilegeEscalation: false",
        'drop: ["ALL"]',
        "readOnlyRootFilesystem: true",
    ):
        assert contract in vmctl_job


def test_victoriametrics_migration_requires_exact_historical_data_proof():
    content = MIGRATE.read_text(encoding="utf-8")
    proof = content.split("migrate_vm_data_with_proof()", 1)[1].split(
        "stage_migrate_data()", 1
    )[0]
    query = content.split("query_vm_sentinel()", 1)[1].split(
        "write_and_verify_vm_sentinel()", 1
    )[0]
    retire = content.split("retire_observability_source()", 1)[1].split(
        "kubespray_inventory()", 1
    )[0]

    assert 'VM_PROBE_IMAGE="curlimages/curl:8.17.0"' in content
    assert "profile_migration_data_proof" in content
    assert "epoch=$((epoch - 3600))" in content
    assert '"${address}/api/v1/import/prometheus"' in content
    assert '"${address}/api/v1/query"' in content
    probe = content.split("run_vm_probe_pod()", 1)[1].split(
        "prepare_vm_sentinel()", 1
    )[0]
    for contract in (
        "automountServiceAccountToken:false",
        "runAsNonRoot:true",
        "seccompProfile:{type:\"RuntimeDefault\"}",
        "allowPrivilegeEscalation:false",
        "capabilities:{drop:[\"ALL\"]}",
        "readOnlyRootFilesystem:true",
    ):
        assert contract in probe
    assert '(.data.result | length) == 1' in query
    assert 'sample_timestamp_ms:$timestamp' in query
    assert 'sample_value:$value' in query
    assert proof.index("write_and_verify_vm_sentinel") < proof.index(
        "run_vmctl_migration"
    )
    assert proof.index("run_vmctl_migration") < proof.index(
        'target_evidence=$(query_vm_sentinel'
    )
    assert proof.index('target_evidence=$(query_vm_sentinel') < proof.index(
        'status:"verified"'
    )
    assert "fromdateiso8601" in proof
    assert "sentinel_epoch >= filter_epoch" in proof
    assert 'filter_time_start:$filterTimeStart' in proof
    assert '.sentinel == ($descriptor[0] | del(.source_evidence))' in content
    assert "verified VictoriaMetrics $direction migration proof is missing or invalid" in content
    assert retire.index("verify_vm_migration_proof forward") < retire.index(
        "capture_observability_pvcs"
    )
    assert retire.index("verify_vm_migration_proof forward") < retire.index(
        "kubectl delete pvc"
    )
    assert retire.rindex("verify_vm_migration_proof forward") < retire.index(
        "kubectl delete pvc"
    )
    assert retire.count("verify_vm_migration_proof forward") == 2
    migrate_stage = content.split("stage_migrate_data()", 1)[1].split(
        "stage_validate()", 1
    )[0]
    assert migrate_stage.count("migrate_vm_data_with_proof forward") == 1


def test_victoriametrics_rollback_delta_is_proven_before_helm_restore():
    content = MIGRATE.read_text(encoding="utf-8")
    rollback = content.split('if [[ "$COMMAND" == rollback ]]', 1)[1].split(
        "remove_disabled_components()", 1
    )[0]

    assert "migrate_vm_data_with_proof rollback" in rollback
    assert "target_write_switch_started_at" in rollback
    assert "-proof-v1" in rollback
    assert rollback.index("migrate_vm_data_with_proof rollback") < rollback.index(
        "restore_helm_baseline_without_vault"
    )
    assert rollback.index("migrate_vm_data_with_proof rollback") < rollback.index(
        '"$SCRIPT_DIR/rollback.sh"'
    )


def test_mutating_migrations_are_single_writer_and_use_process_unique_temp_files():
    content = MIGRATE.read_text(encoding="utf-8")
    assert 'MIGRATION_LOCK="${STATE_BASE}/.${PROJECT}-profile-migration.lock"' in content
    assert 'another migration process is active for $PROJECT' in content
    assert "kill -0 \"$lock_pid\"" in content
    assert "migration_process_cleanup()" in content
    cleanup = content.split("migration_process_cleanup()", 1)[1].split(
        "trap migration_process_cleanup EXIT", 1
    )[0]
    assert 'rm -rf "$MIGRATION_LOCK"' in cleanup
    assert "restore_gitaly_pdb_override" in cleanup
    assert "trap migration_process_cleanup EXIT" in content
    assert "trap 'exit 130' INT" in content
    assert "trap 'exit 143' TERM" in content
    assert '${STATE_FILE}.tmp.$$' in content
    assert '$STATE_FILE.tmp"' not in content


def test_migration_rollback_removes_target_only_components_fail_closed():
    content = MIGRATE.read_text(encoding="utf-8")
    removal = content.split("rollback_components_to_remove()", 1)[1].split(
        "restore_helm_baseline_without_vault()", 1
    )[0]
    expected_order = (
        "daytona blackbox apm glitchtip temporal postal tempo tracing coroot "
        "gitlab-runner gitlab mongodb eso elasticsearch dragonfly "
        "disaster-recovery backup autoscaling gitops observability "
        "postgresql databases secrets object-storage"
    )
    assert f"for component in {expected_order}; do" in removal
    assert 'component_enabled "$SOURCE_CONFIG" "$component" && continue' in removal
    assert 'component_enabled "$TARGET_CONFIG" "$component"' in removal
    assert '[[ -f "$STATE_DIR/stage-backup.done" ]]' in removal
    assert '[[ -f "$STATE_DIR/stage-post-backup.done" ]] && delete_data=true' in removal
    assert '-e "@$ROLLBACK_CONFIG"' in removal
    assert '-e "delete_component_data=$delete_data"' in removal
    assert "HIPAA-oriented hardening introduced by the target is retained" in removal

    rollback = content.split('if [[ "$COMMAND" == rollback ]]', 1)[1].split(
        "remove_disabled_components()", 1
    )[0]
    assert rollback.count("remove_target_only_components_for_rollback") == 1
    assert rollback.index("remove_target_only_components_for_rollback") > rollback.index(
        'if [[ -f "$STATE_DIR/stage-migrate-vault-storage.done" ]]'
    )
    assert rollback.index("remove_target_only_components_for_rollback") < rollback.index(
        'run_playbook "$ROLLBACK_CONFIG"'
    )


def test_migration_rollback_restores_non_vault_helm_baselines_after_vault_conversion():
    content = MIGRATE.read_text(encoding="utf-8")
    restore = content.split("restore_helm_baseline_without_vault()", 1)[1].split(
        'if [[ "$COMMAND" == rollback ]]', 1
    )[0]
    assert '[[ -s "$baseline" ]]' in restore
    assert '[[ "$namespace" == vault ]]' in restore
    assert 'helm rollback "$release" "$revision" -n "$namespace"' in restore

    rollback = content.split('if [[ "$COMMAND" == rollback ]]', 1)[1].split(
        "remove_disabled_components()", 1
    )[0]
    vault_branch = rollback.split(
        'if [[ -f "$STATE_DIR/stage-migrate-vault-storage.done" ]]', 1
    )[1].split("else", 1)[0]
    assert 'restore_helm_baseline_without_vault "$snapshot"' in vault_branch
    assert '"$SCRIPT_DIR/rollback.sh" --snapshot "$snapshot" --force' in rollback


def test_vault_storage_migration_is_offline_retains_pvc_and_is_fail_closed():
    content = VAULT_MIGRATE.read_text(encoding="utf-8")
    assert "vault operator migrate" in content
    assert "kubectl scale statefulset vault" in content
    assert "replicas=0" in content
    assert "storage_source \"file\"" in content
    assert "storage_destination \"raft\"" in content
    assert "/vault/data/raft" in content
    assert "helm uninstall vault" in content
    assert "Vault remains stopped" in content
    assert "kubectl get pvc" in content


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


def test_project_env_dr_location_is_used_as_blank_profile_fallback_without_secret_argv():
    root_defaults = (ROOT / "defaults" / "main.yml").read_text(encoding="utf-8")
    role_defaults = (
        ROOT / "roles" / "backup-restore" / "defaults" / "main.yml"
    ).read_text(encoding="utf-8")
    normalize = (
        ROOT / "playbooks" / "tasks" / "normalize_profile.yml"
    ).read_text(encoding="utf-8")
    orchestrator = (
        ROOT / "platform-orchestrator" / "platform.sh"
    ).read_text(encoding="utf-8")

    for content in (root_defaults, role_defaults):
        for variable in (
            "BACKUP_DR_ENDPOINT",
            "BACKUP_DR_REGION",
            "BACKUP_DR_BUCKET",
            "BACKUP_DR_PREFIX",
            "BACKUP_DR_ACCESS_KEY",
            "BACKUP_DR_SECRET_KEY",
        ):
            assert f"lookup('env', '{variable}')" in content
    endpoint = normalize.split("backup_dr_storage_endpoint:", 1)[1].split(
        "backup_dr_storage_region:", 1
    )[0]
    bucket = normalize.split("backup_dr_storage_bucket:", 1)[1].split(
        "backup_dr_storage_prefix:", 1
    )[0]
    assert "default(backup_dr_storage_endpoint, true)" in endpoint
    assert "default(backup_dr_storage_bucket, true)" in bucket
    assert "load-project-env.sh" in orchestrator
    assert 'backup_dr_storage_access_key=' not in orchestrator
    assert 'backup_dr_storage_secret_key=' not in orchestrator


def test_application_backup_orchestrator_triggers_postgresql_full_backup():
    content = (SCRIPTS / "backup-all.sh").read_text(encoding="utf-8")
    assert "PerconaPGBackup" in content
    assert "--type=full" in content
    assert "for c in postgresql mongodb vault seaweedfs gitlab" in content
    assert "component_expected" in content
    assert "--config" in content
    assert 'elif any(.status.conditions[]?; .type == "Failed"' in content
    assert 'failed|missing) return 1' in content


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
    for component in ("mongodb", "gitlab"):
        assert f"Would skip disabled component {component}" in result.stdout


def test_platform_cli_exposes_backup_restore_and_migration():
    content = (ROOT / "platform-orchestrator" / "platform.sh").read_text(encoding="utf-8")
    for command in ("backup-cluster", "restore-cluster", "migrate"):
        assert command in content


def test_backup_removal_cleans_local_velero_but_retains_remote_objects():
    content = (ROOT / "playbooks" / "remove_component.yml").read_text(encoding="utf-8")
    assert "disaster-recovery:" in content
    assert "namespaces: [velero]" in content
    assert "{name: velero, namespace: velero}" in content
    assert "Remote\n          object-storage backup" in content


def test_infrastructure_role_cannot_bulk_delete_or_resize_cluster_nodes():
    content = (ROOT / "roles" / "hetzner-infra" / "tasks" / "main.yml").read_text(
        encoding="utf-8"
    )
    assert "Refuse unsafe server deletion or bulk type convergence" in content
    assert "migrate-profile.sh" in content
    assert "hcloud server change-type --keep-disk {{ item.name }}" not in content
    assert "hcloud server delete {{ item }}" not in content


def test_profile_scale_in_refuses_bound_node_local_data():
    content = MIGRATE.read_text(encoding="utf-8")
    guard = content.split("prepare_local_pvs_for_node_removal()", 1)[1].split(
        "remove_cluster_node()", 1
    )[0]
    assert '.local_storage.enabled // false' in guard
    assert '.status.phase == "Bound"' in guard
    assert "refusing to remove $node: bound local PVs remain node-affine" in guard
    assert '.status.phase == "Available" or .status.phase == "Released"' in guard
    remove_node = content.split("remove_cluster_node()", 1)[1].split(
        "scale_in_nodes()", 1
    )[0]
    assert 'prepare_local_pvs_for_node_removal "$node"' in remove_node
    assert remove_node.index("prepare_local_pvs_for_node_removal") < remove_node.index(
        "kubectl drain"
    )


def test_cluster_backup_cloud_capture_is_fail_closed_and_uses_managed_dns_zone():
    content = (ROOT / "scripts" / "cluster-backup.sh").read_text(encoding="utf-8")

    assert '"zone:${DNS_ZONE}"' in content
    assert '"zone:${DOMAIN}"' not in content
    assert "if grep -qi 'not found'" in content
    assert "refusing to record a transient API failure as resource absence" in content
    hcloud_safe = content.split("hcloud_safe() {", 1)[1].split("\n}", 1)[0]
    assert "for attempt in 1 2 3 4 5 6 7 8 9 10" in hcloud_safe
    assert "grep -qi 'not found'" in hcloud_safe
    assert "hcloud_safe zone describe" not in content
    assert "dns_zone_state_file=" in content


def test_cluster_backup_accepts_the_exact_cluster_secrets_file():
    content = (ROOT / "scripts" / "cluster-backup.sh").read_text(encoding="utf-8")

    assert "--secrets-file FILE" in content
    assert '--secrets-file) SECRETS_FILE="$2"' in content
    assert 'copy_required "$SECRETS_FILE"' in content


def test_cluster_backup_supports_an_isolated_persistent_ssh_trust_file():
    content = (ROOT / "scripts" / "cluster-backup.sh").read_text(encoding="utf-8")

    assert "--ssh-known-hosts FILE" in content
    assert '--ssh-known-hosts) SSH_KNOWN_HOSTS_FILE="$2"' in content
    assert 'chmod 0600 "$SSH_KNOWN_HOSTS_FILE"' in content
    assert 'SSH_ARGS+=(-o "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}")' in content
    assert 'proxy_command+=" -o UserKnownHostsFile=${quoted_known_hosts}"' in content


def test_cluster_bundle_objects_are_kept_outside_the_velero_prefix():
    content = (ROOT / "scripts" / "cluster-backup.sh").read_text(encoding="utf-8")

    assert 'VELERO_DR_PREFIX=$(yq -r' in content
    assert 'cluster-bundles/${PROJECT}' in content
    assert '"$DR_PREFIX" == "$VELERO_DR_PREFIX/"*' in content
    assert "cluster bundle prefix must be outside the Velero storage prefix" in content


def test_cluster_backup_bundles_only_validated_encrypted_vault_init_material():
    backup = BACKUP.read_text(encoding="utf-8")
    restore = RESTORE.read_text(encoding="utf-8")

    assert "--vault-init-file FILE" in backup
    assert '--vault-init-file) VAULT_INIT_FILE="$2"' in backup
    assert "ansible-vault view" in backup
    assert '[[ "$vault_init_header" == "\\$ANSIBLE_VAULT;"* ]]' in backup
    assert 'copy_required "$VAULT_INIT_FILE" "$STAGE_DIR/config/vault-init.json.vault"' in backup
    assert "vault_init_material:$hasVaultInit" in backup
    assert "recovery_dependencies:{vault_init:{required:$vaultInitRequired" in backup
    assert 'encryption:(if $hasVaultInit then "ansible-vault" else null end)' in backup
    assert ".schema_version == 1 or .schema_version == 2" in restore
    assert "bundle is missing required encrypted Vault initialization material" in restore
    assert "bundled Vault initialization material is not Ansible Vault encrypted" in restore
    assert 'RECEIPT_PATH="${ARCHIVE}.manifest.json"' in restore
    assert '.remote.publication_state == "complete"' in restore
