from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def test_headscale_defaults_are_short_lived_and_least_privilege():
    defaults = yaml.safe_load(read("defaults/main.yml"))

    assert defaults["headscale_version"] == "0.28.0"
    assert defaults["tailscale_version"] == "1.98.10"
    assert defaults["headscale_subnet_router_tag"] == "tag:subnet-router"
    assert defaults["headscale_preauth_key_expiration"] == "1h"
    assert defaults["headscale_developer_https_enabled"] is False
    assert defaults["headscale_admin_private_tcp_ports"] == [
        22,
        80,
        443,
        6443,
    ]
    assert defaults["headscale_admin_private_icmp_enabled"] is True


def test_headscale_loads_deny_by_default_policy_and_private_dns_records():
    config = read(
        "roles/network-security/templates/headscale-config.yaml.j2"
    )
    policy = read(
        "roles/network-security/templates/headscale-policy.hujson.j2"
    )
    records = read(
        "roles/network-security/templates/headscale-extra-records.json.j2"
    )

    assert "mode: file" in config
    assert "path: /etc/headscale/policy.hujson" in config
    assert "override_local_dns: false" in config
    assert "global: []" in config
    assert "extra_records_path: /var/lib/headscale/extra-records.json" in config
    assert "write_ahead_log: true" in config
    assert "logtail:\n  enabled: false" in config
    assert "taildrop:\n  enabled: false" in config

    assert '"acls": [' in policy
    assert '"tagOwners"' in policy
    assert '"autoApprovers"' in policy
    assert "{{ private_network_cidr }}" in policy
    assert "headscale_admin_private_tcp_ports | map('string')" in policy
    assert '"proto": "icmp"' in policy
    assert "headscale_dns_zones" in records


def test_headscale_vpn_dns_can_target_the_bastion_admin_edge():
    network_tasks = read("roles/network-security/tasks/main.yml")
    cluster_tasks = read("roles/k8s-cluster-management/tasks/main.yml")

    assert "network.vpn.internal_dns.zones" in network_tasks
    assert "else network.internal_dns.zones" in network_tasks
    assert "# managed-by-ansible-k8s-admin-edge" in network_tasks
    assert "# managed-by-ansible-k8s-admin-edge" in cluster_tasks
    assert "bind {{ admin_edge_tailnet_ip }}:443" in cluster_tasks
    assert "bind {{ bastion_public_ip }}:443" in cluster_tasks
    assert "{{ node_ip }}:{{ admin_gateway_node_port }}" in cluster_tasks
    assert "actual_tailnet_ip" in cluster_tasks


def test_headscale_runtime_does_not_persist_or_ignore_auth_key_failures():
    tasks = read("roles/network-security/tasks/main.yml")
    compose = read(
        "roles/network-security/templates/headscale-docker-compose.yml.j2"
    )

    assert '"127.0.0.1:9090:9090"' in compose
    assert '"9090:9090"' not in compose.replace(
        '"127.0.0.1:9090:9090"', ""
    )
    assert "no-new-privileges:true" in compose
    assert "read_only: true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert 'test: ["CMD", "headscale", "health"]' in compose
    assert "headscale\", \"healthcheck" not in compose

    validation = tasks.split(
        "- name: Validate Headscale configuration and Compose before rollout",
        1,
    )[1].split("- name: Start Headscale container", 1)[0]
    assert "mktemp -d /tmp/headscale-configtest" in validation
    assert '"$validation_state:/var/lib/headscale"' in validation
    assert "-v /var/lib/headscale:/var/lib/headscale" not in validation

    registration = tasks.split(
        "- name: Connect and converge the bastion subnet router", 1
    )[1].split("- name: Require the bastion subnet router", 1)[0]
    assert "no_log: true" in registration
    assert "--reusable" not in tasks
    assert "720h" not in tasks
    assert "|| true" not in registration
    assert "--expiration '{{ headscale_preauth_key_expiration }}'" in registration
    assert "mktemp /run/headscale-bastion-key" in registration
    assert "trap 'rm -f \"$key_file\"' EXIT" in registration
    assert "--tags '{{ headscale_subnet_router_tag }}'" in registration
    assert "--advertise-routes '{{ private_network_cidr }}'" in registration
    assert "--snat-subnet-routes=true" in registration

    assert "headscale policy check" in tasks
    assert "docker kill --signal HUP headscale" in tasks
    assert "tailscale={{ tailscale_version }}" in tasks
    assert "nodes list-routes" in tasks
