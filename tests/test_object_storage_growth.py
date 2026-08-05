from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_seaweedfs_volume_growth_is_tier_aware():
    tasks = (ROOT / "roles/object-storage/tasks/main.yml").read_text()
    defaults = (ROOT / "roles/object-storage/defaults/main.yml").read_text()

    growth = tasks.split("[master.volume_growth]", 1)[1].split(
        "# HashiCorp Raft", 1
    )[0]
    for copies in ("copy_1", "copy_2", "copy_3", "copy_other"):
        assert f"{copies} = {{{{ object_storage_volume_growth_{copies}" in growth
    assert "threshold = {{ object_storage_volume_growth_threshold | float }}" in growth
    assert "disable = false" in growth

    # Compact profiles retain one-at-a-time growth. Medium and production use
    # the upstream replicated batch sizes and an earlier threshold so bursty
    # Kopia maintenance cannot outrun just-in-time volume placement.
    assert 'object_storage_volume_growth_copy_1: "{{ 7 if (object_storage_volume_replicas | int) > 1 else 1 }}"' in defaults
    assert 'object_storage_volume_growth_copy_2: "{{ 6 if (object_storage_volume_replicas | int) > 1 else 1 }}"' in defaults
    assert 'object_storage_volume_growth_copy_3: "{{ 3 if (object_storage_volume_replicas | int) > 1 else 1 }}"' in defaults
    assert "object_storage_volume_growth_copy_other: 1" in defaults
    assert 'object_storage_volume_growth_threshold: "{{ 0.8 if (object_storage_volume_replicas | int) > 1 else 0.9 }}"' in defaults

    reclaim = tasks.split(
        "Reclaim stale empty SeaweedFS volumes from the legacy growth policy", 1
    )[1].split("\n    - name:", 1)[0]
    assert "lock\\nvolume.deleteEmpty -quietFor=10m -apply\\nunlock" in reclaim
    assert "changed_when:" in reclaim
    assert "retries: 3" in reclaim


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
