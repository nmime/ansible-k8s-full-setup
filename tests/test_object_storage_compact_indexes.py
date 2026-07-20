from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_minimum_storage_uses_rebuildable_seaweedfs_indexes():
    runner = (ROOT / "run_tier.sh").read_text()
    normalize = (ROOT / "playbooks/tasks/normalize_profile.yml").read_text()
    tasks = (ROOT / "roles/object-storage/tasks/main.yml").read_text()

    assert '.storage.index_persistent = false' in runner
    assert "object_storage_index_persistent:" in normalize
    assert "storage.index_persistent | default(true)" in normalize
    assert "Detect persistent-to-rebuildable SeaweedFS index migration" in tasks
    assert "--cascade=orphan" in tasks
    assert "else {'type': 'emptyDir'}" in tasks
    assert "Verify rebuilt SeaweedFS volume pods no longer mount index claims" in tasks
    assert "Delete obsolete rebuildable SeaweedFS index claims" in tasks
    assert tasks.index("Verify rebuilt SeaweedFS volume pods") < tasks.index(
        "Delete obsolete rebuildable SeaweedFS index claims"
    )
