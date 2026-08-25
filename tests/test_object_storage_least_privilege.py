from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def test_seaweedfs_runtime_identities_are_bucket_scoped():
    tasks = read("roles/object-storage/tasks/main.yml")
    auth = tasks.split("name: Create SeaweedFS S3 auth/config secret", 1)[1].split(
        "register: _r_seaweedfs_s3_secret", 1
    )[0]

    for identity in (
        "bucket-bootstrap",
        "gitlab",
        "backup",
        "observability",
        "gitlab-runner-cache",
        "nx-cache-protected",
        "nx-cache-development",
    ):
        assert f"'name': '{identity}'" in auth
    assert "'actions': ['Admin']" in auth
    assert "bootstrap_actions.append('Admin:' ~ bucket)" in auth
    assert "'Write:nx-cache-protected'" in auth
    assert "'Write:nx-cache-development'" in auth
    assert "AWS_ACCESS_KEY_ID" not in auth
    assert "AWS_SECRET_ACCESS_KEY" not in auth


def test_root_credentials_are_not_used_by_runtime_consumers():
    consumers = {
        "roles/gitlab-selfhosted/tasks/main.yml": "object_storage_gitlab_access_key",
        "roles/k8s-databases/tasks/main.yml": "object_storage_backup_access_key",
        "roles/k8s-observability/tasks/main.yml": (
            "object_storage_observability_access_key"
        ),
        "roles/k8s-observability/tasks/tracing.yml": (
            "object_storage_observability_access_key"
        ),
        "roles/backup-restore/defaults/main.yml": "object_storage_backup_access_key",
    }
    for path, scoped_key in consumers.items():
        content = read(path)
        assert scoped_key in content
        assert "{{ object_storage_access_key }}" not in content


def test_component_storage_reconcile_rotates_selected_database_consumers():
    orchestrator = read("platform-orchestrator/platform.sh")
    object_storage_case = next(
        line
        for line in orchestrator.splitlines()
        if line.lstrip().startswith("object-storage)")
        and "run_playbook" in line
    )
    assert (
        "--tags storage,object-storage,seaweedfs,databases"
        in object_storage_case
    )


def test_scoped_credentials_are_encrypted_and_persisted():
    tasks = read("roles/generate-secrets/tasks/main.yml")
    summary = tasks.split("- name: Display generated credentials summary", 1)[1]
    assert "Object storage credentials: configured" in summary
    assert "{{ object_storage_access_key }}" not in summary
    for group in (
        "bootstrap",
        "gitlab",
        "backup",
        "observability",
        "ci_cache",
        "nx_cache_protected",
        "nx_cache_development",
    ):
        access_key = f"object_storage_{group}_access_key"
        secret_key = f"object_storage_{group}_secret_key"
        assert tasks.count(access_key) >= 4
        assert tasks.count(secret_key) >= 4
    assert "Require distinct least-privilege object-storage credentials" in read(
        "roles/object-storage/tasks/main.yml"
    )


def test_storage_default_deny_is_enabled_with_explicit_callers():
    defaults = yaml.safe_load(read("roles/object-storage/defaults/main.yml"))
    tasks = read("roles/object-storage/tasks/main.yml")
    policy = tasks.split("name: Allow object storage ingress on SeaweedFS ports", 1)[
        1
    ].split("name: Display object storage summary", 1)[0]

    assert defaults["object_storage_network_policy_enabled"] is True
    assert "fromEntities: [cluster]" not in policy
    for namespace in (
        "gitlab",
        "monitoring",
        "databases",
        "vault",
        "production",
        "preproduction",
    ):
        assert f"pod.namespace: {namespace}" in policy
    assert "pod.namespace: \"{{ object_storage_namespace_resolved }}\"" in policy

    agents_policy = tasks.split(
        "name: Allow agents namespace to use the SeaweedFS S3 gateway", 1
    )[1].split("name: Display object storage summary", 1)[0]
    assert "name: allow-from-agents-s3" in agents_policy
    assert "app.kubernetes.io/component: filer" in agents_policy
    assert "kubernetes.io/metadata.name: agents" in agents_policy
    assert "port: 8333" in agents_policy
    assert "name: allow-agents-seaweedfs-s3" in agents_policy
    assert "toEntities: [host]" in agents_policy
    assert "toCIDR: [169.254.25.10/32]" in agents_policy
    assert "serviceName: seaweedfs-s3" in agents_policy
    assert "k8s:app.kubernetes.io/component: filer" in agents_policy


def test_backup_verification_reaches_only_the_s3_gateway():
    defaults = yaml.safe_load(read("roles/object-storage/defaults/main.yml"))
    tasks = read("roles/object-storage/tasks/main.yml")
    policy = tasks.split(
        "name: Allow backup verification to reach only the SeaweedFS S3 gateway",
        1,
    )[1].split("name: Display object storage summary", 1)[0]

    assert defaults["object_storage_backup_verification_namespace"] == "backups"
    assert "name: allow-backup-verification-to-seaweedfs" in policy
    assert "app.kubernetes.io/component: filer" in policy
    assert "pod.namespace: >-" in policy
    assert "object_storage_backup_verification_namespace" in policy
    assert "port: '8333'" in policy
    for forbidden_port in ("8888", "9333", "8080", "19333"):
        assert f"port: '{forbidden_port}'" not in policy


def test_declared_buckets_have_bounded_credential_groups():
    defaults = yaml.safe_load(read("roles/object-storage/defaults/main.yml"))
    buckets = {
        item["name"] if isinstance(item, dict) else item
        for item in defaults["object_storage_buckets"]
    }
    assert {
        "gitlab-runner-cache",
        "nx-cache-protected",
        "nx-cache-development",
    } <= buckets
    assert set(defaults["object_storage_gitlab_buckets"]) <= buckets
    assert set(defaults["object_storage_observability_buckets"]) <= buckets
    assert set(defaults["object_storage_backup_buckets"]) <= buckets
    assert set(defaults["object_storage_ci_cache_buckets"]) <= buckets


def test_nx_cache_retention_and_growth_are_enforced_by_seaweedfs():
    defaults = yaml.safe_load(read("roles/object-storage/defaults/main.yml"))
    tasks = read("roles/object-storage/tasks/main.yml")
    policies = {
        item["name"]: item
        for item in defaults["object_storage_managed_bucket_policies"]
    }

    assert defaults["object_storage_managed_bucket_policies_enabled"] is True
    assert defaults["object_storage_managed_bucket_policy_supported_chart_versions"] == [
        "4.25.1"
    ]
    assert policies["nx-cache-protected"]["ttl"] == "3d"
    assert policies["nx-cache-development"]["ttl"] == "1d"
    assert policies["gitlab-runner-cache"]["ttl"] == "2h"
    assert defaults["object_storage_retention_prune_schedule"] == "23 * * * *"
    assert "24576" in policies["gitlab-runner-cache"]["quota_mib"]
    assert "8192" in policies["gitlab-runner-cache"]["quota_mib"]
    assert "else 16384" in policies["gitlab-runner-cache"]["quota_mib"]
    assert "createBuckets:" in tasks
    assert "object_storage_managed_bucket_policies" in tasks
    assert "s3.bucket.quota -name={{ policy.name }} -op=set" in tasks
    assert "s3.bucket.quota.enforce -apply" in tasks
    assert "name: seaweedfs-cache-retention-pruner" in tasks
    assert 'retention%h} hours ago' in tasks
    assert "s3api delete-object" in tasks
    assert defaults["object_storage_cache_volume_compaction_schedule"] == "53 4 * * 0"
    assert defaults["object_storage_cache_volume_compaction_threshold"] == "0.3"
    assert defaults["object_storage_cache_volume_collections"] == [
        "gitlab-runner-cache",
        "nx-cache-protected",
        "nx-cache-development",
    ]
    assert "name: seaweedfs-cache-volume-compaction" in tasks
    assert "volume.vacuum -collection={{ collection }}" in tasks
    assert "garbageThreshold={{ object_storage_cache_volume_compaction_threshold }}" in tasks
    assert "volume.list -collectionPattern={{ collection }} -readonly -v=5" in tasks
    assert "name: seaweedfs-stale-multipart-cleaner" in tasks
    assert "name: seaweedfs-volume-vacuum" in tasks
    assert "name: seaweedfs-volume-reclaim" in tasks
    assert "object_storage_volume_vacuum_schedule" not in defaults
    assert "default('43 1 * * *')" in defaults["object_storage_volume_reclaim_schedule"]
    assert "activeDeadlineSeconds: 43200" in tasks
    assert "volume.vacuum -garbageThreshold 0.05" not in tasks
    assert "volume.deleteEmpty -quietFor=24h -apply" in tasks
    assert "name: seaweedfs-gitlab-backup-orphan-reclaim" in tasks
    assert defaults["object_storage_gitlab_backup_orphan_reclaim_schedule"] == "43 5 * * 0"
    assert defaults["object_storage_gitlab_backup_orphan_cutoff"] == "72h"
    assert "volume.fsck -collection=gitlab-backups" in tasks
    assert "-cutoffTimeAgo={{ object_storage_gitlab_backup_orphan_cutoff }}" in tasks
    assert "-skipEcVolumes -reallyDeleteFromVolume -forcePurging" in tasks
    assert "volume.vacuum -collection=gitlab-backups -garbageThreshold=0.3" in tasks
    assert tasks.count("run_locked()") == 2
    assert tasks.count("printf '%s\\n' 'lock' \"$1\" 'unlock' | /usr/bin/weed shell") == 2
    assert "trap unlock_maintenance" not in tasks
    assert "volume.list -collectionPattern=gitlab-backups -readonly -v=5" in tasks
    assert "ReadOnly:true" in tasks
    assert 'volume.vacuum -volumeId=$volume_id -garbageThreshold=0.01' in tasks
    assert "emptyDir:" in tasks
    assert "kind: CronJob" in tasks
    assert "concurrencyPolicy: Forbid" in tasks
    assert "automountServiceAccountToken: false" in tasks
    assert "readOnlyRootFilesystem: true" in tasks
    assert "@sha256:" in defaults["object_storage_policy_image"]
    assert "CACHE_TTL_HOURS" not in tasks
    assert "CACHE_MAX_BYTES" not in tasks


def test_gitlab_backup_growth_is_bounded_without_automatic_deletion():
    defaults = yaml.safe_load(read("roles/object-storage/defaults/main.yml"))
    tasks = read("roles/object-storage/tasks/main.yml")
    quotas = {
        item["name"]: item
        for item in defaults["object_storage_additional_bucket_quotas"]
    }

    assert "gitlab-backups" in quotas
    assert "92160" in quotas["gitlab-backups"]["quota_mib"]
    assert "object_storage_additional_bucket_quotas" in tasks
    assert "s3.bucket.quota.enforce -apply" in tasks
    assert "s3.rm" not in tasks
    assert "s3.bucket.delete" not in tasks
