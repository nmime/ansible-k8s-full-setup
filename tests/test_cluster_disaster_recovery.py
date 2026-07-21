"""Contract and offline workflow tests for full-cluster DR and migration."""

import hashlib
import json
import os
import re
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
STORAGE_CAPACITY = SCRIPTS / "profile-storage-capacity.py"
VAULT_MIGRATE = SCRIPTS / "vault-storage-migrate.sh"
CAPTURE_REPOSITORY = SCRIPTS / "capture-repository-state.sh"
VELERO = ROOT / "roles" / "backup-restore" / "tasks" / "velero.yml"


@pytest.mark.parametrize(
    "script", (BACKUP, RESTORE, MIGRATE, VAULT_MIGRATE, CAPTURE_REPOSITORY)
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
    assert "UNTRACKED_ARCHIVE_SHA256=" in content
    assert 'tracked_patch_scope:"HEAD-to-working-tree-including-index"' in content
    assert 'untracked_archive_path:"config/repository-untracked.tar"' in content
    assert "untracked_file_count:$untrackedFileCount" in content


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
    assert 'Velero filesystem backup(s) failed before the backup completed' in content
    assert '[[ -f "$POD_ANNOTATIONS_FILE" && -s "$POD_ANNOTATIONS_FILE" ]]' in content


def test_complete_backup_rejects_unbound_or_unmounted_live_pvcs_with_json_evidence():
    content = BACKUP.read_text(encoding="utf-8")
    assert "pvc-protection-evidence.json" in content
    assert "persistentvolumeclaims --all-namespaces -o json" in content
    assert '$claim.metadata.deletionTimestamp == null' not in content
    assert "select(.metadata.deletionTimestamp == null)" in content
    assert '($claim.status.phase // "") == "Bound"' in content
    assert 'if ($claim_mounts | length) == 0 then "unmounted"' in content
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
    assert evidence["summary"] == {"evaluated": 3, "protected": 1, "failures": 2}
    claims = {claim["name"]: claim for claim in evidence["claims"]}
    assert claims["mounted"]["protected"] is True
    assert claims["orphan"]["failures"] == ["unmounted"]
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
    assert "Hetzner accepted the type change" in content


def test_migration_waits_for_etcd_after_a_control_plane_restart():
    content = MIGRATE.read_text(encoding="utf-8")
    assert "PROFILE_MIGRATION_ETCD_HEALTH_TIMEOUT_SECONDS" in content
    assert "etcd quorum is not fully ready yet" in content
    assert "etcd cluster did not become healthy within" in content


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


def _make_encrypted_fixture(
    tmp_path: Path,
    passphrase: str,
    vault_init_state: str = "legacy",
) -> Path:
    bundle_name = "test-cluster-20260716T000000Z"
    bundle = tmp_path / bundle_name
    bundle.mkdir()
    manifest = {
        "schema_version": 1 if vault_init_state == "legacy" else 2,
        "backup_id": bundle_name,
        "project": "test",
        "source_context": "source",
        "completeness": "complete",
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
    (bundle / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    checksum_lines = []
    for path in sorted(item for item in bundle.rglob("*") if item.is_file()):
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        checksum_lines.append(f"{checksum}  ./{path.relative_to(bundle)}")
    (bundle / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
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
            "schema_version": 1,
            "receipt_type": "encrypted-cluster-backup",
            "backup_id": bundle_name,
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
        "source-declared\tcpx22\ttarget-requested\tcx23\tretained\tcpx22\n"
    )
    capacity = json.loads((state / "volume-capacity-plan.json").read_text())
    assert capacity["source"]["persistent_total_gib"] == 240
    assert capacity["target"]["persistent_total_gib"] == 1310
    assert capacity["target_delta_gib"] == 1100
    assert capacity["migration_scratch_gib"] == 50
    assert capacity["required_additional_gib"] == 1150
    assert capacity["planning_inputs"] == {
        "configured_account_quota_gib": None,
        "safety_margin_gib": 100,
        "live_account_usage_required": True,
    }
    assert capacity["minimum_required_headroom_gib"] == 1250
    assert capacity["offline_result"] == "quota-required-before-execute"
    assert "Required additional capacity before safety margin: 1150 GiB" in result.stdout


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
    assert capacity["offline_result"] == "quota-required-before-execute"


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
    assert "seaweedfs-volume\t100Gi\t40Gi" in retention
    assert "seaweedfs-index\t4Gi\t2Gi" in retention
    assert "postgresql\t50Gi\t30Gi" in retention
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
    assert 'hcloud server change-type "$node" "$target_type"' in content
    assert 'change-type --keep-disk "$node"' not in content
    assert 'growpart "/dev/$parent" "$partnum"' in content
    assert 'resize2fs "$root_source"' in content
    assert "SSH did not recover" in content
    resize_node = content.split("resize_node()", 1)[1].split("stage_resize()", 1)[0]
    assert 'check_platform_health "$SOURCE_CONFIG"' in resize_node
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
    change_type = resize.index('hcloud server change-type "$node" "$target_type"')
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
    assert 'hcloud server poweroff "$node"' not in resize
    assert 'kubectl uncordon "$node"' in resize


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


def test_mutating_migrations_are_single_writer_and_use_process_unique_temp_files():
    content = MIGRATE.read_text(encoding="utf-8")
    assert 'MIGRATION_LOCK="${STATE_BASE}/.${PROJECT}-profile-migration.lock"' in content
    assert 'another migration process is active for $PROJECT' in content
    assert "kill -0 \"$lock_pid\"" in content
    assert "trap 'rm -rf \"$MIGRATION_LOCK\"' EXIT INT TERM" in content
    assert '${STATE_FILE}.tmp.$$' in content
    assert '$STATE_FILE.tmp"' not in content


def test_migration_rollback_removes_target_only_components_fail_closed():
    content = MIGRATE.read_text(encoding="utf-8")
    removal = content.split("rollback_components_to_remove()", 1)[1].split(
        "restore_helm_baseline_without_vault()", 1
    )[0]
    expected_order = (
        "daytona blackbox apm glitchtip temporal postal tracing coroot "
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
    assert "Would skip disabled component mongodb" in result.stdout
    assert "Would skip disabled component gitlab" in result.stdout


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
