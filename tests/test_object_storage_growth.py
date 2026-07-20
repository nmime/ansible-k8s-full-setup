from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_resource_efficient_seaweedfs_grows_one_logical_volume_at_a_time():
    tasks = (ROOT / "roles/object-storage/tasks/main.yml").read_text()

    growth = tasks.split("[master.volume_growth]", 1)[1].split(
        "# HashiCorp Raft", 1
    )[0]
    for copies in ("copy_1", "copy_2", "copy_3", "copy_other"):
        assert f"{copies} = 1" in growth
    assert "threshold = 0.9" in growth
    assert "disable = false" in growth


def test_s3_load_verification_uses_tools_available_in_pinned_aws_image():
    load_test = (ROOT / "scripts/tier-load-test.sh").read_text()

    assert 'actual=\\$(cat "\\$readback" 2>/dev/null || true)' in load_test
    assert '[ "\\$actual" = "\\$expected" ]' in load_test
    assert "cmp -s" not in load_test
