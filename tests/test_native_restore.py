"""Offline contract tests for production-target native replay."""

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "native-restore.sh"


def _function_body(name: str, next_name: str) -> str:
    content = SCRIPT.read_text(encoding="utf-8")
    end_marker = (
        next_name if next_name.startswith("for ") else f"{next_name}() {{"
    )
    return content.split(f"{name}() {{", 1)[1].split(end_marker, 1)[0]


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
    assert "config_sha256:$configSha" in content
    assert "receipt_sha256:$receiptSha" in content
    assert "target_cluster_uid:$targetUid" in content
    assert "existing Vault restore Job is not a completed checkpoint" in content
    assert "existing GitLab restore Job is not a completed checkpoint" in content
    assert 'all(.artifacts[];' in content
    assert "source_ids-current_ids" in content
    assert "recovered topology is missing recorded identities" in content
    assert "pgbackrest info --stanza=db" in content
    assert ".label == $set and .error == false" in content
    assert ".key == $repoIndex and .status.code == 0" in content
    assert ".status.backupName == $set" not in content
    assert 'GITLAB_SCALED=true' in content
    cleanup = content.split("cleanup() {", 1)[1].split("trap cleanup", 1)[0]
    assert 'GITLAB_SCALED" == true' in cleanup
    assert "kubectl scale deployment" in cleanup
    assert "expected_secret_sha" in content and "actual_secret_sha" in content
    assert ".sidecar_capture" in content
    assert "verify_sidecar_hash postgresql-pvcs" in content
    assert "verify_sidecar_hash gitlab-replicas" in content
    assert content.index('verify_seaweedfs_topology "$uri"') < content.index(
        'checkpoint "$component" completed verified'
    )


def test_temporary_native_restore_workloads_are_bounded_and_restricted():
    content = SCRIPT.read_text(encoding="utf-8")
    assert content.count("activeDeadlineSeconds:") >= 4
    assert len(re.findall(r"allowPrivilegeEscalation\s*:\s*false", content)) >= 6
    assert len(re.findall(r'capabilities\s*:\s*\{drop:\s*\["ALL"\]\}', content)) >= 6
    assert len(re.findall(r"limits\s*:\s*\{cpu:", content)) >= 6
    assert "automountServiceAccountToken: false" in content


def test_s3_uri_validation_is_portable_to_controller_bash():
    content = SCRIPT.read_text(encoding="utf-8")
    assert '${#S3_KEY} <= 1024' in content
    assert '[A-Za-z0-9._/+=:@-]*' in content
    assert "{0,1023}" not in content


def test_s3_endpoint_is_encoded_as_json_data_not_interpolated_yaml():
    head_s3 = _function_body("head_s3_object", "verify_seaweedfs_topology")
    assert 'jq -n --arg pod "$pod" --arg secret "$secret"' in head_s3
    assert '--arg key "$S3_KEY" --arg endpoint "$endpoint"' in head_s3
    assert '{name:"AWS_ENDPOINT_URL",value:$endpoint}' in head_s3
    assert 'aws --endpoint-url=\\"$AWS_ENDPOINT_URL\\"' in head_s3
    assert "| kubectl apply" in head_s3
    assert "-f -" in head_s3
    assert "<<EOF" not in head_s3
    assert "${endpoint}" not in head_s3


def test_vault_replay_checks_ansible_vault_before_decrypting():
    content = SCRIPT.read_text(encoding="utf-8")
    assert "command -v ansible-vault" in content
    assert "ansible-vault is required for Vault native restore" in content
    assert content.index("command -v ansible-vault") < content.index("ansible-vault view")


def test_vault_replay_unseals_before_snapshot_and_resumes_only_bound_failed_job():
    content = SCRIPT.read_text(encoding="utf-8")
    assert ".unseal_threshold" in content
    assert ".unseal_keys_b64[$i]" in content
    assert "kubectl exec -i -n vault" in content
    assert "vault operator unseal -format=json 2>/dev/null" in content
    assert 'vault operator unseal -format=json "$unseal_key"' not in content
    assert "Vault did not elect an active member after unseal" in content
    assert content.index("vault operator unseal -format=json 2>/dev/null") < content.index(
        "vault operator raft snapshot restore -force"
    )
    assert "secretName: vault-internal-tls" in content
    assert "VAULT_CACERT, value: /vault/tls/ca.crt" in content
    assert "name: vault-ca, mountPath: /vault/tls, readOnly: true" in content
    assert '.metadata.annotations["backup-restore.io/native-backup-id"] == $id' in content
    assert "(.status.failed // 0) > 0" in content
    assert '[[ "$RESUME" == true ]]' in content


def test_native_restore_never_routes_secret_payloads_through_logs_or_argv():
    content = SCRIPT.read_text(encoding="utf-8")
    gitlab_secret = content.split("restore_gitlab_secrets() {", 1)[1].split(
        "restore_gitlab() {", 1
    )[0]
    assert "kubectl logs" not in gitlab_secret
    assert "kubectl exec -n gitlab" in gitlab_secret
    assert "s3 cp" in gitlab_secret
    assert "vault operator unseal -format=json \"$unseal_key\"" not in content


def test_vault_root_token_never_becomes_a_shell_or_jq_argument():
    vault = _function_body("restore_vault", "restore_postgresql")
    assert 'stringData:{VAULT_TOKEN:.root_token}' in vault
    assert re.search(
        r"(?m)^\s*(?:local\s+)?(?:root_token|vault_token)(?:\s|=)", vault
    ) is None
    assert re.search(
        r"--arg(?:json)?\s+\S*(?:root|token)\S*\s+[\"$]", vault, re.IGNORECASE
    ) is None
    assert re.search(
        r"jq\s+-[^\n]*\.root_token[^\n]*\|\s*kubectl", vault
    ) is None


def _assert_exact_checkpoint_annotations(
    body: str, *, minimum_occurrences: int
) -> None:
    bindings = {
        "backup-restore.io/native-backup-id": ("id", "BACKUP_ID"),
        "backup-restore.io/native-target-cluster-uid": ("target", "TARGET_UID"),
        "backup-restore.io/native-catalog-sha256": ("catalog", "CATALOG_SHA"),
        "backup-restore.io/native-artifact-sha256": ("artifact", "artifact_sha"),
    }
    for annotation, (jq_name, shell_name) in bindings.items():
        assert body.count(annotation) >= minimum_occurrences
        assert f'--arg {jq_name} "${shell_name}"' in body
        assert f'.metadata.annotations["{annotation}"] == ${jq_name}' in body


def test_native_restore_checkpoints_bind_target_catalog_and_exact_artifact():
    vault = _function_body("restore_vault", "restore_postgresql")
    postgres = _function_body("restore_postgresql", "restore_mongodb")
    gitlab = _function_body("restore_gitlab", "for component in seaweedfs")

    _assert_exact_checkpoint_annotations(vault, minimum_occurrences=3)
    _assert_exact_checkpoint_annotations(postgres, minimum_occurrences=4)
    _assert_exact_checkpoint_annotations(gitlab, minimum_occurrences=3)

    assert "--arg set \"$set\" --arg repo \"$repo\"" in postgres
    assert '.metadata.annotations["backup-restore.io/postgresql-set"] == $set' in postgres
    assert (
        '.metadata.annotations["backup-restore.io/postgresql-repository"] '
        "== $repo"
    ) in postgres
    assert '.spec.dataSource.pgbackrest.options == ["--set="+$set]' in postgres
    assert ".spec.dataSource.pgbackrest.repo.name == $repo" in postgres


def _run_postgresql_restore_scenario(
    tmp_path: Path, scenario: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    artifact_text = json.dumps(
        {
            "name": "test-pg-backup",
            "artifact_locator": "20260723-010203F",
            "repository": "repo2",
        },
        separators=(",", ":"),
    )
    artifact_sha = hashlib.sha256(artifact_text.encode()).hexdigest()
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "sidecar_capture": {"postgresql": "completed"},
                "sidecars": {
                    "postgresql-cluster": "fixture",
                    "postgresql-secrets": "fixture",
                    "postgresql-pvcs": "fixture",
                },
            }
        ),
        encoding="utf-8",
    )
    Path(f"{state}.postgresql-cluster.json").write_text(
        json.dumps(
            {
                "apiVersion": "pgv2.percona.com/v2",
                "kind": "PerconaPGCluster",
                "metadata": {"name": "test-project-pg", "namespace": "databases"},
                "spec": {
                    "backups": {
                        "pgbackrest": {
                            "configuration": [],
                            "global": {},
                            "repos": [{"name": "repo2", "s3": {}}],
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    Path(f"{state}.postgresql-secrets.json").write_text(
        '{"apiVersion":"v1","kind":"List","items":[]}', encoding="utf-8"
    )
    Path(f"{state}.postgresql-pvcs.txt").write_text("", encoding="utf-8")
    live = tmp_path / "live.json"
    if scenario == "completed-not-ready":
        live.write_text(
            json.dumps(
                {
                    "metadata": {
                        "annotations": {
                            "backup-restore.io/native-backup-id": "test-backup",
                            "backup-restore.io/native-target-cluster-uid": "target-uid",
                            "backup-restore.io/native-catalog-sha256": "catalog-sha",
                            "backup-restore.io/native-artifact-sha256": artifact_sha,
                            "backup-restore.io/postgresql-set": "20260723-010203F",
                            "backup-restore.io/postgresql-repository": "repo2",
                            "backup-restore.io/native-restore-phase": "completed",
                        }
                    },
                    "spec": {},
                    "status": {"state": "initializing"},
                }
            ),
            encoding="utf-8",
        )
    actions = tmp_path / "actions.log"
    postgres_functions = (
        "prove_postgresql_repository() {"
        + _function_body("prove_postgresql_repository", "restore_postgresql")
        + "restore_postgresql() {"
        + _function_body("restore_postgresql", "restore_mongodb")
    )
    harness = r"""
set -euo pipefail
PROJECT=test-project
BACKUP_ID=test-backup
TARGET_UID=target-uid
CATALOG_SHA=catalog-sha
STATE_FILE="$TEST_DIR/state.json"
TIMEOUT_SECONDS=30
RESUME=true

artifact_json() { printf '%s' "$ARTIFACT_JSON"; }
verify_sidecar_hash() { :; }
fail() { printf '%s\n' "$*" >&2; exit 1; }
wait_json_state() {
  printf 'wait-ready\n' >> "$TEST_DIR/actions.log"
  jq '.status.state="ready"' "$TEST_DIR/live.json" > "$TEST_DIR/live.next"
  mv "$TEST_DIR/live.next" "$TEST_DIR/live.json"
}
kubectl() {
  if [[ "$1 $2" == "get perconapgbackup" ]]; then
    printf '%s\n' '{"spec":{"pgCluster":"test-project-pg","repoName":"repo2"}}'
  elif [[ "$1" == "wait" ]]; then
    printf 'repo-proof\n' >> "$TEST_DIR/actions.log"
    return 0
  elif [[ "$1" == "exec" && "$*" == *"pgbackrest info"* ]]; then
    printf '%s\n' '[{"backup":[{"label":"20260723-010203F","error":false}],"repo":[{"key":2,"status":{"code":0}}]}]'
  elif [[ "$1 $2" == "get perconapgcluster" ]]; then
    [[ -f "$TEST_DIR/live.json" ]] || return 1
    cat "$TEST_DIR/live.json"
  elif [[ "$1" == "apply" && "${3:-}" == "-" ]]; then
    cat > "$TEST_DIR/live.json"
    printf 'apply-cr\n' >> "$TEST_DIR/actions.log"
  elif [[ "$1" == "apply" ]]; then
    printf 'apply-sidecar\n' >> "$TEST_DIR/actions.log"
  elif [[ "$1 $2" == "delete perconapgcluster" ]]; then
    rm -f "$TEST_DIR/live.json"
    printf 'delete-cr\n' >> "$TEST_DIR/actions.log"
  elif [[ "$1 $2" == "get pods" ]]; then
    printf 'test-project-pg-primary-0'
  elif [[ "$1" == "exec" && "$*" == *"psql -U postgres"* ]]; then
    printf '1\n'
  else
    printf 'unexpected kubectl call: %s\n' "$*" >&2
    return 2
  fi
}
""" + postgres_functions + "\nrestore_postgresql\n"
    env = os.environ.copy()
    env.update(
        {
            "TEST_DIR": str(tmp_path),
            "ARTIFACT_JSON": artifact_text,
        }
    )
    result = subprocess.run(
        ["bash"],
        input=harness,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    action_trace = actions.read_text(encoding="utf-8") if actions.exists() else ""
    return result, action_trace


def test_postgresql_resume_recreates_cr_after_interrupted_destructive_delete(tmp_path):
    result, actions = _run_postgresql_restore_scenario(tmp_path, "absent")
    assert result.returncode == 0, f"{result.stderr}\nactions:\n{actions}"
    assert actions.count("apply-cr") == 2
    assert "delete-cr" in actions
    assert actions.index("apply-cr") < actions.index("repo-proof")


def test_postgresql_completed_checkpoint_waits_and_revalidates_ready(tmp_path):
    result, actions = _run_postgresql_restore_scenario(tmp_path, "completed-not-ready")
    assert result.returncode == 0, f"{result.stderr}\nactions:\n{actions}"
    assert actions == "wait-ready\nrepo-proof\n"


def test_sidecar_capture_is_atomic_and_resumable_per_component():
    content = SCRIPT.read_text(encoding="utf-8")
    postgres = _function_body("restore_postgresql", "restore_mongodb")
    gitlab = _function_body("restore_gitlab", "for component in seaweedfs")

    assert "sidecar_capture:{" in content
    assert 'postgresql:"pending"' in content
    assert 'gitlab:"pending"' in content
    for component, body, sidecars in (
        (
            "postgresql",
            postgres,
            ("postgresql-cluster", "postgresql-secrets", "postgresql-pvcs"),
        ),
        ("gitlab", gitlab, ("gitlab-replicas",)),
    ):
        assert f".sidecar_capture.{component}" in body
        assert body.count("write_state ") == 1
        assert ".capture.XXXXXX" in body
        finalize = "install " if "install " in body else "mv "
        assert finalize in body
        assert body.index(finalize) < body.index("write_state ")
        assert "record_sidecar_hash" not in body
        for sidecar in sidecars:
            assert f'.sidecars["{sidecar}"]' in body
            assert f"verify_sidecar_hash {sidecar}" in body


def test_mongodb_and_gitlab_failed_restore_objects_are_exact_bound_before_resume():
    content = SCRIPT.read_text(encoding="utf-8")
    mongodb = content.split("restore_mongodb() {", 1)[1].split(
        "restore_gitlab_secrets() {", 1
    )[0]
    assert "${destination}.pbm.json" in mongodb
    assert '.spec.backupSource.destination == $destination' in mongodb
    assert "kubectl delete perconaservermongodbrestore" in mongodb
    assert '[[ "$RESUME" == true ]]' in mongodb
    gitlab = content.split("restore_gitlab() {", 1)[1].split(
        "for component in seaweedfs", 1
    )[0]
    assert "(.status.failed // 0) > 0" in gitlab
    assert "(.status.active // 0) == 0" in gitlab
    assert "kubectl delete job" in gitlab
    assert 'del(.spec.template.spec.containers[0].command)' in gitlab
    assert "exec backup-utility --restore" in gitlab
    assert 'wait_job_complete "$job" gitlab' in gitlab
    assert "--for=condition=complete" not in gitlab


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
