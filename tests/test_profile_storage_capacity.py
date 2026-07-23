"""Cross-source contracts for provider-billable persistent capacity."""

import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ESTIMATOR_PATH = ROOT / "scripts" / "profile-storage-capacity.py"
PROFILES = ROOT / "platform-orchestrator" / "profiles"

spec = importlib.util.spec_from_file_location("profile_storage_capacity", ESTIMATOR_PATH)
assert spec and spec.loader
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


def load_profile(name: str) -> dict:
    return yaml.safe_load((PROFILES / f"{name}.yaml").read_text())


def test_medium_optimized_capacity_includes_every_persistent_index_volume():
    profile = load_profile("medium-optimized")
    estimate = capacity.estimate(profile)
    index = estimate["claims"]["object-storage/index"]

    assert profile["storage"]["index_persistent"] is True
    assert index == {
        "replicas": 3,
        "requested_size": "2Gi",
        "billable_per_volume_gib": 10,
        "total_gib": 30,
        "source": "SeaweedFS volume indexes",
    }
    assert estimate["persistent_total_gib"] == 730
    assert estimate["backup_scratch_gib"] == 20
    assert sum(claim["replicas"] for claim in estimate["claims"].values()) == 41
    assert "750 GiB billable persistent volumes" in profile["cost_estimate"]
    assert "730 GiB data claims" in profile["cost_estimate"]


def test_capacity_index_presence_matches_normalized_helm_pvc_contract():
    normalizer = (ROOT / "playbooks/tasks/normalize_profile.yml").read_text()
    object_storage = (ROOT / "roles/object-storage/tasks/main.yml").read_text()

    assert "storage.index_persistent | default(true)" in normalizer
    assert "object_storage_idx_size:" in normalizer
    assert "if (object_storage_index_persistent | default(true) | bool)" in object_storage
    assert "'type': 'persistentVolumeClaim'" in object_storage

    for profile_path in sorted(PROFILES.glob("*.yaml")):
        profile = yaml.safe_load(profile_path.read_text())
        if not profile.get("storage", {}).get("enabled", True):
            continue
        estimate = capacity.estimate(profile)
        storage = profile["storage"]
        persistent = storage.get("index_persistent", True)
        volumes = storage.get(
            "volume_replicas", storage.get("master_replicas", storage.get("replicas", 1))
        )
        if persistent:
            index = estimate["claims"]["object-storage/index"]
            assert index["replicas"] == volumes, profile_path.name
            assert index["requested_size"] == storage.get("index_size", "4Gi")
            assert index["billable_per_volume_gib"] >= 10
        else:
            assert "object-storage/index" not in estimate["claims"]


def test_explicit_compact_index_mode_removes_only_separate_index_claims():
    profile = load_profile("medium-optimized")
    persistent = capacity.estimate(profile)
    profile["storage"]["index_persistent"] = False
    compact = capacity.estimate(profile)

    assert "object-storage/index" not in compact["claims"]
    assert persistent["persistent_total_gib"] - compact["persistent_total_gib"] == 30
    assert compact["backup_scratch_gib"] == persistent["backup_scratch_gib"] == 20


def test_medium_optimized_cost_document_tracks_capacity_source_of_truth():
    cost = (ROOT / "docs/COST_MODEL.md").read_text()
    readme = (ROOT / "README.md").read_text()
    deployment = (ROOT / "DEPLOYMENT.md").read_text()

    for document in (cost, readme, deployment):
        assert "750 GiB" in document
        assert "€318.81" in document
        assert "€115.81" in document
    assert "730 GiB" in cost
    assert "SeaweedFS" in cost and "index" in cost
    assert "€0.0572" in cost
