from pathlib import Path


TASKS = Path(__file__).parents[2] / "roles" / "postal" / "tasks" / "main.yml"
DEFAULTS = Path(__file__).parents[2] / "roles" / "postal" / "defaults" / "main.yml"
SECRETS = Path(__file__).parents[2] / "roles" / "generate-secrets" / "tasks" / "main.yml"


def test_postal_schema_is_reconciled_before_components_start() -> None:
    content = TASKS.read_text()

    schema = content.index("name: Initialize or update the Postal database schema")
    components = content.index("name: Deploy Postal components")
    assert schema < components
    assert "postal-schema-reconcile" in content
    assert "'update' if (_postal_schema_exists.stdout | int) > 0 else 'initialize'" in content
    assert "wait_condition:" in content


def test_postal_application_containers_are_restricted() -> None:
    content = TASKS.read_text()

    assert content.count("runAsNonRoot: true") >= 2
    assert content.count("allowPrivilegeEscalation: true") >= 4
    assert "The upstream image applies cap_net_bind_service to Ruby" in content
    assert content.count("drop: [ALL]") >= 2
    assert content.count("add: [NET_BIND_SERVICE]") >= 2
    assert content.count("type: RuntimeDefault") >= 2


def test_postal_smtp_uses_an_unprivileged_container_port() -> None:
    content = TASKS.read_text()

    assert "default_port: 2525" in content
    assert "containerPort: 2525" in content
    assert content.count("targetPort: 2525") == 2


def test_postal_storage_growth_preserves_the_mariadb_claim() -> None:
    content = TASKS.read_text()
    assert "reconcile_statefulset_storage.yml" in content
    assert "storage_reconcile_statefulset: postal-mariadb" in content


def test_postal_fails_closed_before_direct_delivery_is_enabled() -> None:
    content = TASKS.read_text()

    assert "name: Require forward-confirmed reverse DNS for direct delivery" in content
    assert "for resolver in 1.1.1.1 8.8.8.8" in content
    assert "postal_direct_dns.rc == 0" in content
    assert "name: Prove direct SMTP delivery is permitted from the cluster egress" in content
    assert "postal-outbound-port-preflight" in content
    assert "TCPSocket.new(ENV.fetch(\"PROBE_HOST\"), 25)" in content
    assert "{'name': postal_helo_hostname, 'type': 'TXT'" in content


def test_postal_smtp_starttls_uses_a_trusted_certificate() -> None:
    content = TASKS.read_text()

    assert "kind: Certificate" in content
    assert "name: postal-smtp" in content
    assert "tls_enabled: {{ postal_smtp_tls_enabled | bool | lower }}" in content
    assert "tls_certificate_path: /config/tls/tls.crt" in content
    assert "tls_private_key_path: /config/tls/tls.key" in content
    assert "secretName: \"{{ postal_smtp_tls_secret }}\"" in content


def test_postal_bootstraps_multiple_domains_without_rotating_credentials() -> None:
    content = TASKS.read_text()
    defaults = DEFAULTS.read_text()

    assert "postal_domains: \"{{ postal.domains | default([domain]) }}\"" in defaults
    assert "POSTAL_DOMAINS_B64" in content
    assert "POSTAL_INBOUND_ACCEPT_LOCAL_PARTS_B64" in content
    assert "server.domains.find_or_initialize_by(name: name)" in content
    assert "record.verification_method = \"DNS\"" in content
    assert "Persisted Postal SMTP credential differs; refusing implicit rotation" in content
    assert "postal-dns-requirements" in content
    assert "v=DMARC1; p=none; adkim=r; aspf=r; pct=100; rua=mailto:dmarc-reports@" in content
    assert "Require Postal to accept every sender-domain DNS configuration" in content
    assert "domains.each(&:check_dns)" in content
    assert "domains.reject(&:dns_ok?)" in content
    assert 'route.mode = "Accept"' in content
    assert 'route.spam_mode = "Quarantine"' in content
    assert "'postmaster', 'abuse', 'dmarc-reports'" in defaults


def test_postal_persists_required_signing_and_bootstrap_secrets() -> None:
    tasks = TASKS.read_text()
    secrets = SECRETS.read_text()

    assert "signing_key_path: /config/signing.key" in tasks
    assert 'signing.key: "{{ p_signing_key }}"' in tasks
    assert "generated_postal_signing_key" in secrets
    assert "postal_signing_key: {{ postal_signing_key | to_json }}" in secrets
    assert "generated_postal_smtp_credential" in secrets
    assert "argv: [openssl, rsa, -check, -noout]" in tasks


def test_postal_components_have_health_and_spread_guards() -> None:
    content = TASKS.read_text()
    defaults = DEFAULTS.read_text()

    assert "topologySpreadConstraints:" in content
    assert "postal_mail_node_label" in defaults
    assert "workload.n0xeid.xyz/mail" in defaults
    assert "{postal_mail_node_label: 'true'}" in content
    assert "'preferredDuringSchedulingIgnoredDuringExecution'" in content
    assert "Protect Postal data and serving components from voluntary eviction" in content
    assert "minAvailable: 1" in content
    assert "readinessProbe: \"{{ item.readiness_probe }}\"" in content
    assert "livenessProbe: \"{{ item.liveness_probe }}\"" in content
    assert content.count("httpGet: {path: /health") == 4


def test_postal_web_uses_the_public_gateway() -> None:
    content = TASKS.read_text()

    assert "name: main-gateway" in content
    assert "postal-allow-web-gateway" in content
    assert "Remove the obsolete Postal admin-Gateway policy name" in content
    assert "Create separate Postal web and tracking HTTPRoutes" in content
    assert "{name: postal-track, hostname:" in content
    assert 'gateway.n0xeid.xyz/n0xeid-route: "true"' in content
