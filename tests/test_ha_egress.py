from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles" / "ha-egress"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_role_is_billable_opt_in_and_two_phase() -> None:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert "default(false)" in defaults["ha_egress_enabled"]
    assert "default(false)" in defaults["ha_egress_activate"]
    assert "default(false)" in defaults["ha_egress_manage_mail_dns"]
    profile = yaml.safe_load(read("platform-orchestrator/profiles/medium-optimized.yaml"))
    assert profile["network"]["egress"]["enabled"] is False
    assert profile["network"]["egress"]["activate"] is False


def test_role_creates_protected_floating_ipv4_and_fixed_standby() -> None:
    tasks = read("roles/ha-egress/tasks/main.yml")
    assert "Attach standby gateway to the private network at its stable address" in tasks
    assert "--enable-protection" in tasks
    assert "floating-ip\n      - assign" in tasks
    assert "Refuse an unexpected standby private address" in tasks
    assert "Refusing a destructive detach/reattach" in tasks


def test_static_snat_is_explicit_and_staging_preserves_masquerade() -> None:
    nat = read("roles/ha-egress/templates/n0xeid-egress-nat.sh.j2")
    assert 'SNAT --to-source "$FLOATING_IP"' in nat
    assert 'if [ "$MODE" = activate ]; then' in nat
    activate = nat.split('if [ "$MODE" = activate ]; then', 1)[1].split("else", 1)[0]
    prepare = nat.split("else", 1)[1]
    assert "-j MASQUERADE" in activate
    assert "-j MASQUERADE" not in prepare


def test_controller_owns_both_provider_mutations_and_rolls_back() -> None:
    controller = read("roles/ha-egress/templates/n0xeid-egressctl.py.j2")
    assert "/actions/assign" in controller
    assert "/actions/delete_route" in controller
    assert "/actions/add_route" in controller
    assert "self.assign_floating(previous[\"server_id\"])" in controller
    assert "self.change_route(current[\"route_gateway\"], previous[\"private_ip\"])" in controller
    assert "expected exactly one default route" in controller
    assert "target {target_name} is not ready; refusing failover" in controller


def test_watchdog_requires_consecutive_failures_and_uses_lock() -> None:
    controller = read("roles/ha-egress/templates/n0xeid-egressctl.py.j2")
    service = read("roles/ha-egress/templates/n0xeid-egress-watchdog.service.j2")
    assert "consecutive_failures" in controller
    assert "failure_threshold" in controller
    assert "fcntl.flock(lock, fcntl.LOCK_EX)" in controller
    assert "server[\"status\"] != \"running\"" in controller
    assert "ExecStartPre=/usr/local/sbin/n0xeid-egress-nat activate" in service
    assert service.index("ExecStartPre=") < service.index("ExecStart=")


def test_controller_secret_is_root_only_and_hidden_from_ansible_output() -> None:
    gateway = read("roles/ha-egress/tasks/configure_gateway.yml")
    assert "n0xeid-hcloud-token" in gateway
    assert "mode: '0600'" in gateway
    assert "no_log: true" in gateway
    assert "install -o root -g root -m 0600" in gateway


def test_monitoring_and_backup_cover_both_gateways_and_floating_ip() -> None:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    gateway = read("roles/ha-egress/tasks/configure_gateway.yml")
    observability = read("roles/k8s-observability/tasks/main.yml")
    monitoring = read("roles/ha-egress/tasks/monitoring.yml")
    backup = read("scripts/cluster-backup.sh")
    assert "k8s_pod_cidr" in defaults["ha_egress_monitoring_pod_cidr"]
    assert "ufw allow from {{ ha_egress_monitoring_pod_cidr }}" in gateway
    assert "ha_egress.gateway_1_private_ip" in observability
    assert "ha_egress.gateway_2_private_ip" in observability
    assert "HAEgressGatewayNotReady" in observability
    assert "HAEgressActiveGatewayCountInvalid" in observability
    assert "ha_egress_active_private_ip" in monitoring
    assert "ha_egress_private_ip" in monitoring
    assert "allow-vmagent-to-ha-egress" in monitoring
    assert "discoveryRole: endpointslices" in monitoring
    assert "HAEgressGatewayFlapping" in monitoring
    assert '"floating-ip:${SERVER_NAME_PREFIX}-egress-ipv4"' in backup
    assert "egress_standby:$egressStandbyType" in backup


def test_main_playbook_integrates_ha_egress_before_kubernetes() -> None:
    playbook = read("playbooks/deploy_platform.yml")
    egress_position = playbook.index("name: ha-egress")
    cluster_position = playbook.index("name: k8s-cluster-management")
    assert egress_position < cluster_position
    assert "tags: [egress, network, infrastructure]" in playbook


def test_gateway_reconcile_pauses_both_watchdogs_until_active_health_passes() -> None:
    tasks = read("roles/ha-egress/tasks/main.yml")
    pause = tasks.index("Pause both watchdogs during the gateway update transaction")
    configure = tasks.index("Configure both HA egress gateways")
    health = tasks.index("Require the provider-active gateway to pass full egress health")
    start = tasks.index("Start both watchdogs after the complete gateway transaction")
    assert pause < configure < health < start
    gateway = read("roles/ha-egress/tasks/configure_gateway.yml")
    assert "enable --now" not in gateway


def test_postal_live_credential_adoption_is_explicit_encrypted_and_fail_closed() -> None:
    playbook = read("playbooks/adopt_postal_live_credentials.yml")
    assert "postal_adoption_confirmed | bool" in playbook
    assert "where(type: \"SMTP\").pluck(:name, :key).to_h" in playbook
    assert "does not exactly match the" in playbook
    assert "ansible-vault" in playbook
    assert ".pre-postal-live-adoption" in playbook
    assert "not postal_adoption_rollback_state.stat.exists" in playbook
    assert "Remove all plaintext and staged adoption material" in playbook
    assert "no_log: true" in playbook


def test_postal_shared_lb_follows_provider_name_prefix() -> None:
    defaults = read("roles/postal/defaults/main.yml")
    assert "infrastructure.server_name_prefix" in defaults
    assert "postal_shared_load_balancer_name" in defaults


def test_mail_dns_publishes_and_verifies_spf_for_the_helo_identity() -> None:
    tasks = read("roles/ha-egress/tasks/main.yml")
    helo_spf = tasks.split("Publish SPF for the SMTP HELO identity", 1)[1].split(
        "Publish transitional or final shared mail SPF", 1
    )[0]
    alignment = tasks.split(
        "Wait for forward-confirmed reverse DNS before static SNAT activation", 1
    )[1].split("Create controller staging directory", 1)[0]
    assert "ha_egress_mail_helo_record" in helo_spf
    assert "ha_egress_mail_spf_value" in helo_spf
    assert "TXT" in helo_spf
    assert "TXT {{ ha_egress_ptr | quote }}" in alignment


def test_postal_dns_exports_retry_transient_exec_stream_failures() -> None:
    tasks = read("roles/postal/tasks/main.yml")
    return_path = tasks.split("Export the global return-path DKIM record", 1)[1].split(
        "Export public sender-domain DNS requirements", 1
    )[0]
    public = tasks.split("Export public sender-domain DNS requirements", 1)[1].split(
        "Publish the public Postal DNS plan", 1
    )[0]
    for section in (return_path, public):
        assert "retries: 5" in section
        assert "delay: 5" in section
        assert "until:" in section
