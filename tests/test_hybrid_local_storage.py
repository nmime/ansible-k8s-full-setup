"""Contracts for the medium-optimized hybrid local/CSI storage policy."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "platform-orchestrator" / "profiles" / "medium-optimized.yaml"
ESTIMATOR = ROOT / "scripts" / "profile-storage-capacity.py"

spec = importlib.util.spec_from_file_location("profile_storage_capacity", ESTIMATOR)
assert spec and spec.loader
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


def load_profile() -> dict:
    return yaml.safe_load(PROFILE.read_text())


def test_only_application_replicated_claims_use_local_ssd():
    estimate = capacity.estimate(load_profile())
    local = {
        key
        for key, claim in estimate["claims"].items()
        if claim["storage_class"] == "platform-local"
    }

    assert local == {
        "object-storage/master",
        "object-storage/volume",
        "object-storage/index",
        "vault/data",
        "postgresql/data",
    }
    assert estimate["local_reserved_gib"] == 300
    assert estimate["provider_persistent_gib"] == 240
    assert estimate["provider_backup_scratch_gib"] == 0
    assert all(
        claim["storage_class"] == "hcloud-volumes"
        for key, claim in estimate["claims"].items()
        if key not in local
    )


def test_static_local_pool_is_retained_capacity_aware_and_gated():
    tasks = (
        ROOT / "roles" / "k8s-cluster-management" / "tasks" / "main.yml"
    ).read_text()

    assert "provisioner: kubernetes.io/no-provisioner" in tasks
    assert "reclaimPolicy: Retain" in tasks
    assert "volumeBindingMode: WaitForFirstConsumer" in tasks
    assert "platform-local-storage-initializer" in tasks
    assert "local_storage_pv_slots" in tasks
    assert 'test "$(jq \'.items | length\' <<<"$pool")" -eq 24' in tasks
    assert 'tonumber] | add\' <<<"$pool")" -eq 450' in tasks
    assert "three-plus-three-v1" in tasks
    assert "local_storage_min_free_gib_per_node" in tasks
    assert (
        'select(.metadata.labels["workload.n0xeid.xyz/ci-docker"] != "true")'
        in tasks
    )
    assert (
        'select(.metadata.labels["workload.n0xeid.xyz/ci-build"] != "true")'
        in tasks
    )
    assert "key: workload.n0xeid.xyz/ci-docker" in tasks
    assert "key: workload.n0xeid.xyz/ci-build" in tasks
    assert "operator: DoesNotExist" in tasks
    assert "workload.n0xeid.xyz/ci-docker=true:NoSchedule" in tasks
    assert (
        "if [ ! -f /storage/.platform-static-local-pv-ready ]; then"
        in tasks
    )
    assert "capacity_kib >= (70 * 1024 * 1024)" in tasks
    assert "capacity_kib >= (140 * 1024 * 1024)" in tasks
    assert "kind: PriorityClass" in tasks
    assert "preemptionPolicy: Never" in tasks
    assert "local_storage_cache_priority_class" in tasks


def test_component_roles_use_independent_storage_classes():
    object_storage = (ROOT / "roles/object-storage/tasks/main.yml").read_text()
    vault = (ROOT / "roles/k8s-secrets/tasks/reconcile.yml").read_text()
    databases = (ROOT / "roles/k8s-databases/tasks/main.yml").read_text()
    elasticsearch = (ROOT / "roles/elasticsearch/tasks/main.yml").read_text()

    for variable in (
        "object_storage_master_storage_class",
        "object_storage_volume_storage_class",
        "object_storage_index_storage_class",
        "object_storage_filer_storage_class",
    ):
        assert variable in object_storage
    assert "vault_data_storage_class" in vault
    assert "vault_audit_storage_class" in vault
    assert "pg_data_storage_class" in databases
    assert "pg_repo_storage_class" in databases
    assert "mongo_data_storage_class" in databases
    assert "es_master_storage_class_resolved" in elasticsearch
    assert "es_data_storage_class_resolved" in elasticsearch
    assert object_storage.count("requiredDuringSchedulingIgnoredDuringExecution") >= 2
    assert elasticsearch.count("requiredDuringSchedulingIgnoredDuringExecution") >= 2


def test_medium_optimized_filer_survives_large_multipart_backups():
    profile = load_profile()
    normalizer = (ROOT / "playbooks/tasks/normalize_profile.yml").read_text()
    object_storage = (ROOT / "roles/object-storage/tasks/main.yml").read_text()

    assert profile["storage"]["filer_resources"] == {
        "cpu_request": "100m",
        "cpu_limit": "1",
        "memory_request": "512Mi",
        "memory_limit": "2Gi",
    }
    for variable in (
        "object_storage_filer_cpu_request",
        "object_storage_filer_cpu_limit",
        "object_storage_filer_memory_request",
        "object_storage_filer_memory_limit",
    ):
        assert variable in normalizer
        assert object_storage.count(variable) >= 2


def test_storage_class_change_requires_full_target_capacity_and_replacement():
    target = load_profile()
    source = copy.deepcopy(target)
    source["local_storage"]["enabled"] = False
    source["storage"]["master_storage_class"] = "hcloud-volumes"
    source["storage"]["volume_storage_class"] = "hcloud-volumes"
    source["storage"]["index_storage_class"] = "hcloud-volumes"
    source["secrets"]["vault"]["data_storage_class"] = "hcloud-volumes"
    source["databases"]["postgresql"]["data_storage_class"] = "hcloud-volumes"
    source["databases"]["mongodb"]["data_storage_class"] = "hcloud-volumes"
    source["elasticsearch"]["master"]["storage_class"] = "hcloud-volumes"
    source["elasticsearch"]["data"]["storage_class"] = "hcloud-volumes"

    plan = capacity.migration(source, target)

    assert set(plan["storage_class_changes"]) == {
        "object-storage/master",
        "object-storage/volume",
        "object-storage/index",
        "vault/data",
        "postgresql/data",
    }
    assert plan["target_delta_gib"] == 300
    assert plan["required_additional_gib"] == 300

    migrator = (ROOT / "scripts/migrate-profile.sh").read_text()
    assert "PVC StorageClass is immutable" in migrator
    assert "cluster-backup.sh, cluster-restore.sh, and native-restore.sh" in migrator
