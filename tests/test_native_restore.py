"""Offline contract tests for production-target native replay."""

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "native-restore.sh"


def test_native_restore_is_executable_and_parses():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    help_result = subprocess.run(
        [str(SCRIPT), "--help"], capture_output=True, text=True, check=False
    )
    assert help_result.returncode == 0
    assert "RESTORE_NATIVE_" in help_result.stdout


def _fixture(tmp_path: Path, schema: int = 2) -> tuple[dict[str, Path], dict[str, str]]:
    archive = tmp_path / "test-cluster-20260722.tar.gz.age"
    archive.write_bytes(b"encrypted fixture")
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    catalog = {
        "schema_version": schema,
        "project": "test-project",
        "backup_id": "test-cluster-20260722",
        "created_at": "2026-07-22T00:00:00Z",
        "summary": {"expected": 6, "passed": 0, "failed": 0, "skipped": 6},
        "completeness": "complete",
        "restore_order": [
            "seaweedfs",
            "vault",
            "postgresql",
            "mongodb",
            "gitlab-secrets",
            "gitlab",
        ],
        "artifacts": [
            {
                "component": component,
                "namespace": "disabled",
                "kind": "CronJob",
                "name": component,
                "state": "disabled",
                "restore_contract": "disabled",
                "artifact_locator": "",
                "repository": "",
            }
            for component in (
                "seaweedfs",
                "vault",
                "postgresql",
                "mongodb",
                "gitlab-secrets",
                "gitlab",
            )
        ],
    }
    catalog_path = tmp_path / "native-backups.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    catalog_sha = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    receipt = {
        "schema_version": 2,
        "receipt_type": "encrypted-cluster-backup",
        "project": "test-project",
        "backup_id": "test-cluster-20260722",
        "source_cluster_uid": "source-uid",
        "archive": archive.name,
        "sha256": archive_sha,
        "native_backup_catalog_sha256": catalog_sha,
        "completeness": "complete",
        "remote": {
            "published": True,
            "download_sha256_verified": True,
            "receipt_uploaded_last": True,
            "publication_state": "complete",
        },
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    config_path = tmp_path / "platform.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "global": {"project": "test-project"},
                "databases": {
                    "enabled": False,
                    "postgresql": {"enabled": False},
                    "mongodb": {"enabled": False},
                },
                "secrets": {"enabled": False},
                "storage": {"enabled": False},
                "gitlab": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    kubectl = fake_bin / "kubectl"
    kubectl.write_text(
        """#!/usr/bin/env bash
if [[ "$1 $2 $3" == "get namespace kube-system" ]]; then printf target-uid
elif [[ "$1 $2" == "config current-context" ]]; then printf target-context
else echo "unexpected kubectl call: $*" >&2; exit 2
fi
""",
        encoding="utf-8",
    )
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return {
        "archive": archive,
        "catalog": catalog_path,
        "receipt": receipt_path,
        "config": config_path,
        "state": tmp_path / "state.json",
    }, env


def _plan(paths: dict[str, Path], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(SCRIPT),
            "--catalog",
            str(paths["catalog"]),
            "--receipt",
            str(paths["receipt"]),
            "--archive",
            str(paths["archive"]),
            "--config",
            str(paths["config"]),
            "--state-file",
            str(paths["state"]),
            "--mode",
            "plan",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_plan_is_read_only_and_binds_confirmation_to_target_uid(tmp_path):
    paths, env = _fixture(tmp_path)
    result = _plan(paths, env)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["source_cluster_uid"] == "source-uid"
    assert plan["target_cluster_uid"] == "target-uid"
    assert plan["required_confirmation"].endswith("_target-uid")
    assert not paths["state"].exists()


def test_legacy_catalog_cannot_enter_production_replay(tmp_path):
    paths, env = _fixture(tmp_path, schema=1)
    result = _plan(paths, env)
    assert result.returncode != 0
    assert "schema-v2 exact-artifact catalog" in result.stderr


def test_receipt_must_bind_exact_catalog_hash(tmp_path):
    paths, env = _fixture(tmp_path)
    receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    receipt["native_backup_catalog_sha256"] = "0" * 64
    paths["receipt"].write_text(json.dumps(receipt), encoding="utf-8")
    result = _plan(paths, env)
    assert result.returncode != 0
    assert "not bound to the archive/catalog" in result.stderr


def test_selected_enabled_component_cannot_be_silently_skipped(tmp_path):
    paths, env = _fixture(tmp_path)
    config = yaml.safe_load(paths["config"].read_text(encoding="utf-8"))
    config["storage"]["enabled"] = True
    paths["config"].write_text(yaml.safe_dump(config), encoding="utf-8")
    result = _plan(paths, env)
    assert result.returncode != 0
    assert "enabled component seaweedfs has no completed native artifact" in result.stderr


def test_replay_contract_has_ordered_handlers_and_target_bound_checkpoints():
    content = SCRIPT.read_text(encoding="utf-8")
    positions = [
        content.index(f"{component}) restore_{component.replace('-', '_')}")
        for component in (
            "seaweedfs",
            "vault",
            "postgresql",
            "mongodb",
            "gitlab-secrets",
            "gitlab",
        )
    ]
    assert positions == sorted(positions)
    assert "archive_sha256:$archiveSha" in content
    assert "catalog_sha256:$catalogSha" in content
    assert "target_cluster_uid:$targetUid" in content
    assert "existing Vault restore Job is not a completed checkpoint" in content
    assert "existing GitLab restore Job is not a completed checkpoint" in content
    assert 'all(.artifacts[];' in content
    assert "source_ids-current_ids" in content
    assert "recovered topology is missing recorded identities" in content
    assert 'GITLAB_SCALED=true' in content
    cleanup = content.split("cleanup() {", 1)[1].split("trap cleanup", 1)[0]
    assert 'GITLAB_SCALED" == true' in cleanup
    assert "kubectl scale deployment" in cleanup
    assert "expected_secret_sha" in content and "actual_secret_sha" in content
    assert content.index('verify_seaweedfs_topology "$uri"') < content.index(
        'checkpoint "$component" completed verified'
    )


def test_temporary_native_restore_workloads_are_bounded_and_restricted():
    content = SCRIPT.read_text(encoding="utf-8")
    assert content.count("activeDeadlineSeconds:") >= 4
    assert content.count("allowPrivilegeEscalation: false") >= 6
    assert content.count('capabilities: {drop: ["ALL"]}') >= 6
    assert content.count("limits: {cpu:") >= 6
    assert "automountServiceAccountToken: false" in content


def test_backup_catalog_records_exact_operator_ids_and_job_object_uris():
    backup_all = (ROOT / "scripts" / "backup-all.sh").read_text(encoding="utf-8")
    assert "{schema_version:2" in backup_all
    assert "BACKUP_ARTIFACT_URI=" in backup_all
    assert "{.status.destination}" in backup_all
    assert 'completed pgbackrest "$backup_set" "$repo"' in backup_all
    for task in ("vault_raft.yml", "seaweedfs.yml", "gitlab.yml"):
        content = (ROOT / "roles" / "backup-restore" / "tasks" / task).read_text(
            encoding="utf-8"
        )
        assert "BACKUP_ARTIFACT_URI=" in content
