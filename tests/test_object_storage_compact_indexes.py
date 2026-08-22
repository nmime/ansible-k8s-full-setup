from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_minimum_storage_colocates_seaweedfs_indexes_durably():
    runner = (ROOT / "run_tier.sh").read_text()
    normalize = (ROOT / "playbooks/tasks/normalize_profile.yml").read_text()
    tasks = (ROOT / "roles/object-storage/tasks/main.yml").read_text()
    prepare = (
        ROOT / "roles/object-storage/tasks/index_migration_prepare.yml"
    ).read_text()
    finalize = (
        ROOT / "roles/object-storage/tasks/index_migration_finalize.yml"
    ).read_text()

    assert '.storage.index_persistent = false' in runner
    assert "object_storage_index_persistent:" in normalize
    assert "storage.index_persistent | default(true)" in normalize
    assert "Detect persistent-to-colocated SeaweedFS index migration" in tasks
    assert "seaweedfs-index-migration" in tasks
    assert "Quiesce SeaweedFS writers and volume servers" in prepare
    assert "Create quiesced SeaweedFS index copy Jobs" in prepare
    assert "cmp \"${idx}\" \"/data/${idx##*/}\"" in prepare
    assert "--cascade=orphan" in prepare
    assert "else {}" in tasks
    assert "Recreate SeaweedFS volume pods one at a time on colocated durable indexes" in finalize
    assert "Verify migrated SeaweedFS volume pods use durable colocated indexes" in tasks
    assert "Require object storage bucket bootstrap and S3 smoke completion" in tasks
    assert "Read the pre-migration S3 sentinel" in finalize
    assert "propagationPolicy: Foreground" in finalize
    assert "Remove orphaned SeaweedFS index copy Pods" in finalize
    assert "Delete exact obsolete standalone SeaweedFS index claims" in finalize
    deletion = finalize.split(
        "- name: Delete exact obsolete standalone SeaweedFS index claims", 1
    )[1].split("\n- name:", 1)[0]
    assert "--selector" not in deletion
    assert "_seaweedfs_legacy_idx_claims" in deletion


def test_compact_storage_has_enough_logical_volume_slots_for_all_s3_consumers():
    defaults = (ROOT / "roles/object-storage/defaults/main.yml").read_text()
    normalize = (ROOT / "playbooks/tasks/normalize_profile.yml").read_text()
    tasks = (ROOT / "roles/object-storage/tasks/main.yml").read_text()

    assert "object_storage_volume_size_limit_mb: 256" in defaults
    assert "object_storage_volume_min_free_space_percent: 10" in defaults
    assert "storage.volume_min_free_space_percent" in normalize
    assert 'volumeSizeLimitMB: "{{ object_storage_volume_size_limit_mb | int }}"' in tasks
    assert 'minFreeSpacePercent: "{{ object_storage_volume_min_free_space_percent | int }}"' in tasks


def test_index_migration_checkpoint_supports_interrupted_resume():
    tasks = (ROOT / "roles/object-storage/tasks/main.yml").read_text()
    prepare = (
        ROOT / "roles/object-storage/tasks/index_migration_prepare.yml"
    ).read_text()
    finalize = (
        ROOT / "roles/object-storage/tasks/index_migration_finalize.yml"
    ).read_text()

    assert "_seaweedfs_index_migration_stage != 'new'" in tasks
    for stage in ("preparing", "sentinel-ready", "copied"):
        assert f"stage: {stage}" in prepare
    assert "stage: verified" in finalize
    assert 'remaining_pods=$(kubectl get pods --namespace "$ns"' in prepare
    assert 'if [[ -n "$remaining_pods" ]]' in prepare
    assert "_seaweedfs_index_migration_stage != 'verified'" in finalize
    verified_gate = "when: _seaweedfs_index_migration_stage != 'verified'"
    assert finalize.count(verified_gate) >= 5
    assert "Complete the SeaweedFS index migration checkpoint" in finalize


def test_index_migration_deletes_are_bounded_and_uid_aware():
    prepare = (
        ROOT / "roles/object-storage/tasks/index_migration_prepare.yml"
    ).read_text()
    finalize = (
        ROOT / "roles/object-storage/tasks/index_migration_finalize.yml"
    ).read_text()

    assert "--cascade=orphan --wait=false" in prepare
    assert "old_uid=" in prepare and "current_uid=" in prepare
    assert '"$current_uid" != "$old_uid"' in prepare
    assert "was not deleted within 5 minutes" in prepare
    assert "--wait=true" not in prepare

    assert 'kubectl delete pod --namespace "$ns" "$pod" --wait=false' in finalize
    assert "old_uid=" in finalize and "new_uid=" in finalize
    assert '"$new_uid" != "$old_uid"' in finalize
    assert "replacement_ready" in finalize
    assert "did not become Ready within 10 minutes" in finalize
    assert "--wait=true" not in finalize


def test_index_migration_quiesces_writes_before_copy_and_reads_sentinel_before_cleanup():
    prepare = (
        ROOT / "roles/object-storage/tasks/index_migration_prepare.yml"
    ).read_text()
    finalize = (
        ROOT / "roles/object-storage/tasks/index_migration_finalize.yml"
    ).read_text()

    assert prepare.index("Write an S3 sentinel before quiescing SeaweedFS") < prepare.index(
        "Quiesce SeaweedFS writers and volume servers before copying indexes"
    )
    assert prepare.index(
        "Quiesce SeaweedFS writers and volume servers before copying indexes"
    ) < prepare.index("Create quiesced SeaweedFS index copy Jobs")
    assert finalize.index("Read the pre-migration S3 sentinel") < finalize.index(
        "Delete exact obsolete standalone SeaweedFS index claims"
    )
