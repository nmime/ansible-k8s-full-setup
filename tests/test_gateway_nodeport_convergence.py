from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CLUSTER_TASKS = ROOT / "roles/k8s-cluster-management/tasks/main.yml"
INFRA_TASKS = ROOT / "roles/hetzner-infra/tasks/main.yml"
NETWORK_TASKS = ROOT / "roles/network-security/tasks/main.yml"
HEADSCALE_COMPOSE = (
    ROOT / "roles/network-security/templates/headscale-docker-compose.yml.j2"
)
EVIDENCE_SCRIPT = ROOT / "scripts/collect-live-evidence.sh"
DEFAULTS = ROOT / "defaults/main.yml"
NORMALIZE_PROFILE = ROOT / "playbooks/tasks/normalize_profile.yml"


def test_cilium_gateway_nodeports_are_discovered_not_mutated():
    tasks = CLUSTER_TASKS.read_text()

    assert "Patch Gateway service to use fixed NodePorts" not in tasks
    assert "Patch admin Gateway service to use NodePort" not in tasks
    assert "Record the controller-owned main Gateway NodePorts" in tasks
    assert "Record the controller-owned admin Gateway NodePort" in tasks
    assert "gateway_http_node_port" in tasks
    assert "gateway_https_node_port" in tasks
    assert "admin_gateway_node_port" in tasks


def test_hetzner_lb_tracks_live_gateway_ports_and_fails_closed():
    tasks = CLUSTER_TASKS.read_text()

    http = tasks.index("Converge Hetzner HTTP service to the live Cilium NodePort")
    https = tasks.index("Converge Hetzner HTTPS service to the live Cilium NodePort")
    readback = tasks.index("Require Hetzner Gateway service port readback to match Cilium")
    health = tasks.index("Wait for every Hetzner Gateway target to become healthy")
    assert http < https < readback < health

    block = tasks[http:health]
    assert "{{ gateway_http_node_port | string }}" in block
    assert "{{ gateway_https_node_port | string }}" in block
    assert "--health-check-port" in block
    assert "gateway_lb_port_readback.rc == 0" in tasks[readback:health]

    gate = tasks[health:]
    assert "all($checks[]; .status == \"healthy\")" in gate
    assert "(dedicated_ci_worker_indices | length)" in gate
    assert "(worker_count | int)" in gate
    assert "retries: 40" in gate


def test_infrastructure_bootstrap_does_not_overwrite_converged_ports():
    tasks = INFRA_TASKS.read_text()

    assert "Add missing bootstrap LB service for HTTP" in tasks
    assert "Add missing bootstrap LB service for HTTPS" in tasks
    assert "Reconcile drifted LB service for HTTP" not in tasks
    assert "Reconcile drifted LB service for HTTPS" not in tasks


def test_minimal_tier_reuses_bastion_as_an_sni_aware_edge():
    cluster = CLUSTER_TASKS.read_text()
    network = NETWORK_TASKS.read_text()
    compose = HEADSCALE_COMPOSE.read_text()
    infra = INFRA_TASKS.read_text()

    assert "Converge the minimal-tier bastion edge to live Gateway NodePorts" in cluster
    assert "# managed-by-ansible-k8s-minimal-edge" in cluster
    assert "acl is-headscale req.ssl_sni -i vpn.{{ domain }}" in cluster
    assert "{{ first_master_ip }}:{{ gateway_http_node_port }}" in cluster
    assert "{{ first_master_ip }}:{{ gateway_https_node_port }}" in cluster
    assert "not (lb_enabled | default(false) | bool)" in cluster
    assert "cert_manager_cluster_issuer == 'letsencrypt-prod'" in cluster
    certificate = cluster.index("Create wildcard TLS certificate")
    ready = cluster.index("Wait for the selected wildcard certificate issuer to become ready")
    sync = cluster.index("Wait for Cilium to sync the current Gateway certificate")
    served = cluster.index("Wait for the public Gateway to serve the current certificate")
    strict_tls = cluster.index("Require public TLS ingress with the selected certificate issuer")
    assert certificate < ready < strict_tls
    assert ready < sync < served < strict_tls
    assert "selectattr('observedGeneration', 'defined')" in cluster[ready:strict_tls]
    assert "wildcard_tls_certificate.resources[0].metadata.generation" in cluster[ready:strict_tls]
    assert "namespace: gateway-secrets" in cluster
    assert "gateway-secrets-wildcard-tls" in cluster[sync:served]
    assert "expected_gateway_certificate_fingerprint.stdout" in cluster[served:strict_tls]
    assert "regex_replace('(?i)^sha256 fingerprint=', '')" in cluster[served:strict_tls]
    # openssl s_client can return a non-zero close-notify status after it has
    # emitted a valid certificate. The x509 consumer is the authoritative gate.
    assert "set -o pipefail" not in cluster[served:strict_tls]
    assert "not (lb_enabled | default(false) | bool)" not in cluster[strict_tls:]

    assert "127.0.0.1:8443:443" in compose
    assert "127.0.0.1:8080:80" in compose
    assert "Install HAProxy edge multiplexer on bastion" in network
    assert "Require the public Headscale edge to answer through HAProxy" in network

    assert "HTTP ACME and minimal-tier ingress" in infra
    assert "port: '80'" in infra


def test_live_evidence_captures_gateway_provider_parity_without_secrets():
    script = EVIDENCE_SCRIPT.read_text()

    assert "io.cilium.gateway/owning-gateway=main-gateway" in script
    assert 'hcloud load-balancer describe "${project}-lb" -o json' in script
    assert "ports_match" in script
    assert "healthy_checks" in script
    assert "$gateway_edge.valid" in script
    assert "HCLOUD_TOKEN" in script
    assert "load-balancer.json" in script
    assert "Secrets" in script


def test_public_gateway_supports_additional_project_certificates():
    cluster = CLUSTER_TASKS.read_text()
    defaults = yaml.safe_load(DEFAULTS.read_text())
    normalize = NORMALIZE_PROFILE.read_text()

    assert defaults["gateway_extra_certificate_refs"] == []
    assert defaults["gateway_https_hostname"] == ""
    assert defaults["gateway_extra_https_listeners"] == []
    assert "kubernetes.gateway" in normalize
    assert "extra_certificate_refs" in normalize
    assert "https_hostname" in normalize
    assert "extra_https_listeners" in normalize
    assert "gateway_extra_certificate_refs | default([])" in cluster
    assert "gateway_extra_https_listeners | default([])" in cluster
    assert "gateway_https_hostname" in cluster
    assert "{'name': 'wildcard-tls', 'namespace': 'gateway-secrets'}" in cluster
