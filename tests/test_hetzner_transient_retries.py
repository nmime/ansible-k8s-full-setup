from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_firewall_discovery_distinguishes_absence_from_transient_api_failure():
    tasks = (ROOT / "roles/hetzner-infra/tasks/main.yml").read_text()

    for result in ("fw_bastion_check", "fw_nodes_check"):
        assert f"register: {result}" in tasks
        assert (
            f"{result}.rc == 0 or\n"
            f"    'not found' in ({result}.stderr | default('') | lower)"
        ) in tasks


def test_firewall_creation_retries_transient_hcloud_failures():
    tasks = (ROOT / "roles/hetzner-infra/tasks/main.yml").read_text()

    for result in ("fw_bastion_create", "fw_nodes_create"):
        assert f"register: {result}" in tasks
        assert f"until: {result}.rc == 0" in tasks

    assert tasks.count("retries: 5") >= 5
    assert tasks.count("delay: 5") >= 5
