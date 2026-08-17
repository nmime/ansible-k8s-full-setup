from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_replicated_seaweedfs_keeps_a_writable_runway_for_cache_bursts():
    tasks = (ROOT / "roles/object-storage/tasks/main.yml").read_text()

    growth = tasks.split("[master.volume_growth]", 1)[1].split(
        "# HashiCorp Raft", 1
    )[0]
    assert "copy_1 = 1" in growth
    assert "copy_2 = 4" in growth
    assert "copy_3 = 1" in growth
    assert "copy_other = 1" in growth
    assert "threshold = 0.8" in growth
    assert "disable = false" in growth
    assert "replicated (001) cache/archive write" in tasks
    assert "sparse" in tasks

    reclaim = tasks.split(
        "Reclaim stale empty SeaweedFS volumes from the legacy growth policy", 1
    )[1].split("\n    - name:", 1)[0]
    assert "lock\\nvolume.deleteEmpty -quietFor=10m -apply\\nunlock" in reclaim
    assert "changed_when:" in reclaim
    assert "retries: 3" in reclaim


def test_seaweedfs_filer_can_be_pinned_off_schedulable_control_planes():
    defaults = (ROOT / "roles/object-storage/defaults/main.yml").read_text()
    tasks = (ROOT / "roles/object-storage/tasks/main.yml").read_text()

    assert "object_storage_filer_node_selector: {}" in defaults
    assert "object_storage_filer_node_selector" in tasks
    assert "nodeSelector: |" in tasks


def test_s3_load_verification_uses_tools_available_in_pinned_aws_image():
    load_test = (ROOT / "scripts/tier-load-test.sh").read_text()
    s3_phase = load_test.split("phase_s3()", 1)[1].split("phase_postgresql()", 1)[0]

    # The pinned AWS image supplies /bin/sh and the AWS CLI. Readback content
    # verification therefore uses the shell read builtin rather than assuming
    # optional cmp/diff packages are installed in the image.
    assert 'IFS= read -r actual <"\\$readback/\\$batch/object-\\$object" || true' in s3_phase
    assert '[ "\\$actual" = "\\$RUN_ID:\\$object" ]' in s3_phase
    assert "cmp -s" not in s3_phase

    # Preserve the full batched contract: every object is uploaded, downloaded,
    # content-verified, deleted, and included in the exact operation total.
    assert s3_phase.count("--recursive --only-show-errors --no-progress") == 2
    assert 'operations=\\$((operations+1))' in s3_phase
    assert '[ "\\$operations" -eq "\\$((OBJECTS*4))" ]' in s3_phase
    assert 's3 ls "\\$prefix/\\$batch/" --recursive' in s3_phase
