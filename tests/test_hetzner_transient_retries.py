from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_resource_discovery_distinguishes_absence_from_transient_api_failure():
    tasks = (ROOT / "roles/hetzner-infra/tasks/main.yml").read_text()

    for result in (
        "ssh_key_check",
        "network_check",
        "pg_check",
        "fw_bastion_check",
        "fw_nodes_check",
        "bastion_check",
        "master_check",
        "worker_check",
        "lb_check",
        "dns_zone_check",
    ):
        assert f"register: {result}" in tasks
        assert (
            f"{result}.rc == 0 or\n"
            f"    'not found' in ({result}.stderr | default('') | lower)"
        ) in tasks
        assert (
            f"{result}.rc != 0 and\n"
            f"    'not found' not in ({result}.stderr | default('') | lower)"
        ) in tasks


def test_all_authoritative_hcloud_reads_retry_and_pipeline_reads_use_pipefail():
    tasks = (ROOT / "roles/hetzner-infra/tasks/main.yml").read_text()
    defaults = (ROOT / "roles/hetzner-infra/defaults/main.yml").read_text()

    assert "hcloud_read_retries: 12" in defaults
    assert "hcloud_read_delay: 10" in defaults
    for result in (
        "fw_bastion_current",
        "fw_nodes_current",
        "bastion_ip_result",
        "bastion_private_ip_result",
        "project_servers_raw",
        "master_ip_results",
        "worker_ip_results",
        "lb_config",
        "lb_info",
    ):
        block = tasks.split(f"register: {result}", 1)[1].split("\n- name:", 1)[0]
        assert 'retries: "{{ hcloud_read_retries }}"' in block
        assert 'delay: "{{ hcloud_read_delay }}"' in block
        assert f"until: {result}.rc == 0" in block

    assert tasks.count("set -o pipefail;") >= 3


def test_firewall_creation_retries_transient_hcloud_failures():
    tasks = (ROOT / "roles/hetzner-infra/tasks/main.yml").read_text()

    for result in ("fw_bastion_create", "fw_nodes_create"):
        assert f"register: {result}" in tasks
        block = tasks.split(f"register: {result}", 1)[1].split("\n- name:", 1)[0]
        assert 'retries: "{{ hcloud_write_retries }}"' in block
        assert f"{result}.rc == 0" in block

    assert tasks.count('retries: "{{ hcloud_read_retries }}"') >= 19
    assert tasks.count('delay: "{{ hcloud_read_delay }}"') >= 19


def test_hcloud_mutations_retry_and_managed_dns_fails_closed():
    tasks = (ROOT / "roles/hetzner-infra/tasks/main.yml").read_text()
    defaults = (ROOT / "roles/hetzner-infra/defaults/main.yml").read_text()

    assert "hcloud_write_retries: 12" in defaults
    assert "hcloud_write_delay: 10" in defaults
    for result in (
        "ssh_key_create",
        "network_create",
        "subnet_infra",
        "subnet_cp",
        "subnet_workers",
        "placement_group_create",
        "fw_bastion_create",
        "fw_bastion_replace",
        "fw_nodes_create",
        "fw_nodes_replace",
        "bastion_create",
        "master_create",
        "worker_create",
        "lb_create",
        "lb_network",
        "lb_https_create",
        "lb_http_create",
        "lb_target",
        "dns_zone_create",
        "dns_wildcard",
        "dns_root",
        "dns_vpn",
    ):
        block = tasks.split(f"register: {result}", 1)[1].split("\n- name:", 1)[0]
        assert 'retries: "{{ hcloud_write_retries }}"' in block
        assert 'delay: "{{ hcloud_write_delay }}"' in block
        assert f"{result}.rc == 0" in block

    assert "register: dns_zone_converged" in tasks
    assert "until: dns_zone_converged.rc == 0" in tasks
    assert "Warn if DNS zone creation failed" not in tasks
    assert "failed_when: false" not in tasks.split("- name: Create DNS zone", 1)[1].split("\n- name:", 1)[0]
