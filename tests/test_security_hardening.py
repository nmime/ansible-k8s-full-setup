"""Security hardening tests for ansible-k8s-full-setup.

Tests validate:
1. ansible.cfg SSH hardening (host_key_checking, StrictHostKeyChecking)
2. defaults/main.yml security variables exist with correct defaults
3. generate-secrets role uses Ansible Vault encryption
4. k8s-secrets role has configurable Vault TLS
5. k8s-gitops ArgoCD AppProject has no wildcards
6. k8s-gitops ArgoCD --insecure is conditional
7. No cluster-admin bindings introduced
"""
import os
import re
from pathlib import Path
import yaml
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── Helpers ──────────────────────────────────────────────

def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


def load_yaml(path):
    with open(os.path.join(ROOT, path)) as f:
        return yaml.safe_load(f)


def test_service_network_policies_allow_cilium_gateway_ingress_identity():
    tasks = read("roles/k8s-cluster-management/tasks/main.yml")

    assert "name: allow-from-cilium-ingress" in tasks
    assert "fromEntities:" in tasks
    assert "- ingress" in tasks


def test_vault_tls_covers_the_required_short_raft_addresses() -> None:
    tls = read("roles/k8s-secrets/tasks/vault_tls.yml")
    tasks = read("roles/k8s-secrets/tasks/main.yml")

    assert '- "*.vault-internal"' in tls
    assert '- "vault-active.{{ vault_ns }}.svc.cluster.local"' in tls
    assert '- "vault-standby.{{ vault_ns }}.svc.cluster.local"' in tls
    assert "setNodeId: true" in tasks
    assert "VAULT_API_ADDR:" not in tasks
    assert "VAULT_CLUSTER_ADDR:" not in tasks
    assert "reset_values: true" in tasks
    assert "before a rolling reconcile" in tasks
    assert "vault-tls-resource-version" in tasks
    renderer = read("roles/k8s-secrets/files/vault-post-renderer.sh")
    assert 'select(.name == "VAULT_API_ADDR")' in renderer
    assert '$(VAULT_K8S_POD_NAME).vault-internal' in renderer
    reconcile = read("roles/k8s-secrets/tasks/reconcile_vault_pod.yml")
    assert "Unseal reconciled Vault member before continuing" in reconcile
    assert "vault_init_data.unseal_keys_b64" in reconcile


# ─── 1. ansible.cfg SSH Hardening ────────────────────────

class TestAnsibleCfg:
    def test_host_key_checking_is_true(self):
        cfg = read("ansible.cfg")
        assert "host_key_checking = true" in cfg, \
            "ansible.cfg must set host_key_checking = true"

    def test_strict_host_key_checking_not_no(self):
        cfg = read("ansible.cfg")
        assert "StrictHostKeyChecking=no" not in cfg, \
            "ansible.cfg must NOT use StrictHostKeyChecking=no"

    def test_strict_host_key_checking_accept_new(self):
        cfg = read("ansible.cfg")
        assert "StrictHostKeyChecking=accept-new" in cfg, \
            "ansible.cfg should use StrictHostKeyChecking=accept-new"


# ─── 2. defaults/main.yml Security Variables ─────────────

class TestDefaults:
    def test_vault_tls_disabled_exists(self):
        defs = load_yaml("defaults/main.yml")
        assert "vault_tls_disabled" in defs, \
            "defaults/main.yml must define vault_tls_disabled"

    def test_vault_verify_tls_is_true(self):
        defs = load_yaml("defaults/main.yml")
        assert defs.get("vault_verify_tls") is True, \
            "vault_verify_tls should default to true"

    def test_argocd_insecure_mode_is_false(self):
        defs = load_yaml("defaults/main.yml")
        assert defs.get("argocd_insecure_mode") is False, \
            "argocd_insecure_mode should default to false"

    def test_vault_encrypt_secrets_is_true(self):
        defs = load_yaml("defaults/main.yml")
        assert defs.get("vault_encrypt_secrets") is True, \
            "vault_encrypt_secrets should default to true"

    def test_argocd_allowed_source_repos_exists(self):
        defs = load_yaml("defaults/main.yml")
        repos = defs.get("argocd_allowed_source_repos", [])
        assert isinstance(repos, list) and len(repos) > 0, \
            "argocd_allowed_source_repos must be a non-empty list"

    def test_argocd_allowed_source_repos_no_wildcard_only(self):
        defs = load_yaml("defaults/main.yml")
        repos = defs.get("argocd_allowed_source_repos", [])
        assert repos != ["*"], \
            "argocd_allowed_source_repos should not be just ['*']"

    def test_argocd_allowed_namespaces_exists(self):
        defs = load_yaml("defaults/main.yml")
        ns = defs.get("argocd_allowed_namespaces", [])
        assert isinstance(ns, list) and len(ns) > 0, \
            "argocd_allowed_namespaces must be a non-empty list"

    def test_argocd_allowed_namespace_resources_exists(self):
        defs = load_yaml("defaults/main.yml")
        resources = defs.get("argocd_allowed_namespace_resources", [])
        assert isinstance(resources, list) and len(resources) > 0, \
            "argocd_allowed_namespace_resources must be a non-empty list"

    def test_ansible_ssh_strict_host_key_checking(self):
        defs = load_yaml("defaults/main.yml")
        assert defs.get("ansible_ssh_strict_host_key_checking") is True, \
            "ansible_ssh_strict_host_key_checking should default to true"

    def test_vault_password_file_exists(self):
        defs = load_yaml("defaults/main.yml")
        assert "vault_password_file" in defs, \
            "vault_password_file should be defined"


# ─── 3. generate-secrets: Ansible Vault ─────────────────

class TestGenerateSecrets:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read("roles/generate-secrets/tasks/main.yml")

    def test_header_mentions_vault(self):
        assert "Ansible Vault" in self.content, \
            "generate-secrets role should mention Ansible Vault in header"

    def test_has_vault_encrypted_load(self):
        assert "vault_encrypt_secrets" in self.content, \
            "generate-secrets should use vault_encrypt_secrets variable"

    def test_has_plaintext_fallback(self):
        assert "plaintext fallback" in self.content, \
            "generate-secrets should have plaintext fallback for migration"

    def test_save_task_has_vault_block(self):
        assert "Ansible Vault encrypted" in self.content, \
            "generate-secrets should have a task block for Vault-encrypted saves"

    def test_save_task_has_plaintext_migration(self):
        assert "plaintext migration" in self.content, \
            "generate-secrets should have plaintext migration path"

    def test_vault_encryption_fails_closed(self):
        assert "Plaintext fallback is intentionally disabled" in self.content
        assert "could not encrypt" not in self.content

    def test_vault_encryption_selects_default_vault_id(self):
        assert "--encrypt-vault-id" in self.content
        assert "- default" in self.content

    def test_no_log_on_secrets(self):
        assert "no_log: true" in self.content, \
            "generate-secrets should use no_log on secret operations"

    def test_every_secret_bearing_fact_is_censored(self):
        tasks = yaml.safe_load(self.content)
        protected = {
            "Ensure saved_secrets is defined for first run",
            "Generate object storage credentials",
            "Generate platform credentials",
            "Resolve alert notification credentials (Telegram + email)",
            "Set unified secret variables for all roles",
        }
        by_name = {task.get("name"): task for task in tasks}
        assert protected <= by_name.keys()
        assert all(by_name[name].get("no_log") is True for name in protected)


# ─── 4. k8s-secrets: Vault TLS ──────────────────────────

class TestVaultTLS:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read("roles/k8s-secrets/tasks/main.yml")

    def test_vault_uses_one_chart_managed_maintenance_pdb(self):
        """Every tier must be drainable without overlapping Vault PDBs."""
        assert re.search(
            r"(?ms)^\s{8}ha:\n\s{10}disruptionBudget:\n"
            r"\s{12}enabled: true\n\s{12}maxUnavailable: 1$",
            self.content,
        )
        assert "Remove legacy duplicate Vault PodDisruptionBudget" in self.content
        assert "Reconcile the chart-created Vault PodDisruptionBudget for maintenance" in self.content
        assert "name: vault-pdb" in self.content
        assert "state: absent" in self.content

    def test_vault_tls_retries_transient_admission_webhook_recovery(self):
        tls = read("roles/k8s-secrets/tasks/vault_tls.yml")
        assert tls.count("retries: 6") >= 4
        assert tls.count("delay: 10") >= 4
        assert "until: _vault_selfsigned_issuer is not failed" in tls
        assert "until: vault_internal_certificate is not failed" in tls

    def test_vault_prepares_new_raft_pvcs_without_root(self):
        assert "name: prepare-raft-data" in self.content
        assert "mkdir -p /vault/data/raft" in self.content
        assert "runAsNonRoot: true" in self.content
        assert "readOnlyRootFilesystem: true" in self.content

    def test_vault_reconciles_ondelete_revision_peers_before_primary(self):
        assert "includeConfigAnnotation: true" in self.content
        assert "include_tasks: reconcile_vault_pod.yml" in self.content
        assert "(range(1, vault_replicas | int) | list) + [0]" in self.content
        reconcile = read("roles/k8s-secrets/tasks/reconcile_vault_pod.yml")
        assert "controller-revision-hash" in reconcile
        assert "status.updateRevision" in reconcile
        assert "Wait for reconciled Vault server container" in reconcile

    def test_vault_raft_retry_join_uses_tls_covered_fqdn(self):
        assert (
            "vault-{{ peer }}.vault-internal.{{ vault_ns }}.svc.cluster.local:8200"
            in self.content
        )
        assert "vault-{{ peer }}.vault-internal:8200" not in self.content

    def test_tls_disable_is_configurable(self):
        assert "vault_tls_disabled" in self.content, \
            "Vault tlsDisable should use vault_tls_disabled variable"

    def test_tls_disable_not_hardcoded_true(self):
        lines = self.content.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped == "tlsDisable: true":
                pytest.fail("tlsDisable should not be hardcoded to true")

    def test_vault_initialization_probe_waits_for_the_standalone_pod(self):
        assert "Wait for the Vault server container before checking initialization" in self.content
        assert "name: vault-0" in self.content
        assert "vault_status_raw.rc in [0, 2]" in self.content
        assert "until: vault_status_raw is not failed" not in self.content
        assert "Prove Vault initialization material can be encrypted before initialization" in self.content
        assert "Refuse to discard unencrypted Vault recovery material" in self.content
        assert "vault_init_encrypt is succeeded" in self.content
        assert "--encrypt-vault-id" in self.content
        assert "when: vault_init_data is defined" in self.content
        assert "https://vault-0.vault-internal.{{ vault_ns }}.svc.cluster.local:8200" in self.content

    def test_vault_internal_certificate_secures_local_cli_operations(self):
        tls = read("roles/k8s-secrets/tasks/vault_tls.yml")
        assert "ipAddresses:" in tls
        assert "- 127.0.0.1" in tls
        assert "Wait for the current Vault internal certificate revision" in tls
        assert "selectattr('observedGeneration', 'ge'" in tls
        assert "containerStatuses[0].state.running is defined" in self.content

    def test_vault_init_material_is_project_scoped_and_gitignored(self):
        defaults = read("defaults/main.yml")
        gitignore = read(".gitignore")
        assert ".vault-init-{{ project_name | default('k8s') }}.json" in defaults
        assert ".vault-init-*.json" in gitignore

    def test_integrated_raft_is_used_in_every_resource_tier(self):
        assert "vault_ha: true" in self.content
        assert 'path = "/vault/data/raft"' in self.content
        assert "Integrated Raft is used even for a one-replica" in self.content

    def test_vault_dns_override_is_helm_managed_without_forced_restart(self):
        renderer = read("roles/k8s-secrets/files/vault-post-renderer.sh")
        assert "post_renderer: '{{ vault_helm_post_renderer }}'" in self.content
        assert "type: postrenderer/v1" in self.content
        assert "name: vault-dns" in self.content
        assert "${HELM_PLUGIN_DIR}/vault-post-renderer.sh" in self.content
        assert "vault_helm_major | int >= 4" in self.content
        assert "dnsPolicy) = \"None\"" in renderer
        assert "10.233.0.3" in renderer
        assert "Delete Vault pods to apply DNS patch" not in self.content

    def test_vault_affinity_uses_the_chart_templated_string_contract(self):
        assert "affinity: |" in self.content
        assert "preferredDuringSchedulingIgnoredDuringExecution:" in self.content
        assert "Vault chart consumes affinity as a templated YAML string" in self.content


class TestDragonflyPodSecurity:
    def test_operator_instance_satisfies_restricted_pod_security(self):
        content = read("roles/dragonfly/tasks/main.yml")
        assert "podSecurityContext:" in content
        assert "runAsNonRoot: true" in content
        assert "runAsUser: 1000" in content
        assert "runAsGroup: 1000" in content
        assert "type: RuntimeDefault" in content
        assert "containerSecurityContext:" in content
        assert "allowPrivilegeEscalation: false" in content
        assert "drop: [ALL]" in content

    def test_default_deny_consumers_receive_paired_egress_policy(self):
        content = read("roles/dragonfly/tasks/main.yml")
        assert "Allow consumers to egress to Dragonfly" in content
        assert "allow-egress-to-dragonfly" in content
        assert "kubernetes.io/metadata.name: \"{{ df_ns }}\"" in content
        assert "port: 6379" in content


# ─── 5. k8s-gitops: ArgoCD ──────────────────────────────

class TestArgoCD:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read("roles/k8s-gitops/tasks/main.yml")

    def test_no_hardcoded_insecure_flag(self):
        lines = self.content.split("\n")
        in_args = False
        for line in lines:
            stripped = line.strip()
            if "extraArgs:" in stripped:
                in_args = True
            if in_args and stripped == "- --insecure":
                pytest.fail("--insecure should not be hardcoded; use argocd_insecure_mode")
            if in_args and stripped.startswith("resources:"):
                break

    def test_server_insecure_is_configurable(self):
        assert "argocd_insecure_mode" in self.content, \
            "server.insecure should use argocd_insecure_mode variable"

    def test_appproject_has_allowlists(self):
        assert "argocd_allowed_source_repos" in self.content, \
            "AppProject should use argocd_allowed_source_repos variable"

    def test_appproject_source_repos_not_wildcard(self):
        assert "argocd_allowed_source_repos" in self.content, \
            "AppProject sourceRepos should be templated from argocd_allowed_source_repos"

    def test_appproject_namespaces_not_wildcard(self):
        assert "argocd_allowed_namespaces" in self.content, \
            "AppProject destinations should be templated from argocd_allowed_namespaces"

    def test_appproject_cluster_resources_not_wildcard(self):
        assert "argocd_allowed_cluster_resources" in self.content, \
            "AppProject clusterResourceWhitelist should use allowlist variable"

    def test_appproject_namespace_resources_not_wildcard(self):
        assert "argocd_allowed_namespace_resources" in self.content, \
            "AppProject namespaceResourceWhitelist should use allowlist variable"


# ─── 6. No cluster-admin bindings ───────────────────────

class TestNoClusterAdmin:
    def test_no_cluster_admin_in_any_role(self):
        """Verify no ClusterRoleBinding with cluster-admin in any role."""
        roles_dir = os.path.join(ROOT, "roles")
        for root_dir, _, files in os.walk(roles_dir):
            for f in files:
                if f.endswith((".yml", ".yaml")):
                    content = read(os.path.relpath(os.path.join(root_dir, f), ROOT))
                    if "cluster-admin" in content and "ClusterRoleBinding" in content:
                        pytest.fail(
                            f"Found cluster-admin ClusterRoleBinding in {os.path.relpath(os.path.join(root_dir, f), ROOT)}"
                        )


def test_default_deny_uses_standard_networkpolicy_and_removes_invalid_cilium_legacy():
    for path in (
        "roles/k8s-secrets/tasks/main.yml",
        "roles/k8s-autoscaling/tasks/main.yml",
        "roles/gitlab-selfhosted/tasks/main.yml",
        "roles/k8s-gitops/tasks/main.yml",
        "roles/k8s-databases/tasks/main.yml",
        "roles/k8s-observability/tasks/main.yml",
        "roles/temporal/tasks/main.yml",
        "roles/object-storage/tasks/main.yml",
    ):
        content = read(path)
        assert "Remove legacy invalid Cilium default-deny" in content, path
        assert "apiVersion: networking.k8s.io/v1" in content, path
        assert "kind: NetworkPolicy" in content, path
        assert "policyTypes:" in content, path
        for task in content.split("- name:")[1:]:
            if "kind: CiliumNetworkPolicy" in task and "name: default-deny" in task:
                assert "state: absent" in task, path


def test_health_gate_rejects_invalid_cilium_policies():
    health = read("scripts/health-gates.sh")
    assert "Invalid CiliumNetworkPolicies:" in health
    assert 'select(.type == "Valid" and .status == "True")' in health
    assert '[[ "$invalid_cilium_policies" -ne 0 ]]' in health


def test_health_gate_rejects_unavailable_aggregated_apis():
    health = read("scripts/health-gates.sh")
    assert "Health gate: Aggregated API services" in health
    assert "apiservices.apiregistration.k8s.io" in health
    assert "Unavailable API services:" in health
    assert "_hg_check_aggregated_apis" in health


def test_database_ingress_allows_every_external_postgres_consumer():
    cluster = read("roles/k8s-cluster-management/tasks/main.yml")
    policy = cluster.split(
        "- name: Create allow-databases-from-apps NetworkPolicy", 1
    )[1].split("- name:", 1)[0]
    for namespace in ("gitlab", "production", "monitoring", "temporal", "glitchtip"):
        assert f"kubernetes.io/metadata.name: {namespace}" in policy


def test_recycled_private_ip_host_keys_are_reset_only_for_new_servers():
    infra = read("roles/hetzner-infra/tasks/main.yml")
    network = read("roles/network-security/tasks/main.yml")

    assert "Record private IPs allocated to servers created in this run" in infra
    assert "newly_created_node_ips" in infra
    assert "Remove stale host keys only for nodes provisioned in this run" in network
    assert "newly_created_node_ips | default([])" in network
    assert "ssh-keygen" in network


def test_storage_egress_includes_the_selected_seaweedfs_s3_port():
    cluster = read("roles/k8s-cluster-management/tasks/main.yml")
    policy = cluster.split(
        "name: Create allow-egress-to-storage CiliumNetworkPolicy", 1
    )[1].split("- name:", 1)[0]
    assert 'port: "8333"' in policy
    assert "    - vault" in policy
    ingress = cluster.split(
        "name: Create allow-storage-from-apps NetworkPolicy", 1
    )[1].split("- name:", 1)[0]
    assert "kubernetes.io/metadata.name: vault" in ingress


def test_apiserver_ingress_allows_aggregated_api_target_port():
    cluster = read("roles/k8s-cluster-management/tasks/main.yml")
    policy = cluster.split(
        "name: Create allow-apiserver-webhook-ingress CiliumNetworkPolicy", 1
    )[1].split("- name:", 1)[0]
    assert 'port: "6443"' in policy
    assert "- kube-apiserver" in policy


def test_seaweedfs_gateway_routes_have_matching_cilium_ingress():
    content = read("roles/object-storage/tasks/main.yml")
    assert "Allow Gateway API traffic to SeaweedFS filer endpoints" in content
    assert "name: allow-object-storage-gateway-ingress" in content


def test_seaweedfs_node_health_probes_are_allowed_before_helm_waits():
    content = read("roles/object-storage/tasks/main.yml")
    policy = content.index("name: allow-seaweedfs-node-health-probes")
    install = content.index("name: Install SeaweedFS via official Helm chart")
    assert policy < install
    assert "- remote-node" in content[policy:install]
    assert "- { port: '9333', protocol: TCP }" in content[policy:install]


def test_seaweedfs_uses_and_verifies_hashicorp_raft():
    content = read("roles/object-storage/tasks/main.yml")
    assert "raftHashicorp: true" in content
    assert "raftBootstrap: false" in content
    assert "Detect a legacy SeaweedFS Raft backend" in content
    assert "Restart SeaweedFS masters once when migrating the Raft backend" in content
    assert "Verify SeaweedFS HashiCorp Raft membership" in content
    assert "cluster.raft.ps" in content
    assert "app.kubernetes.io/component: filer" in content
    assert 'port: "8333"' in content
    assert 'port: "8888"' in content
    assert "fromEntities:" in content and "- cluster" in content


def test_seaweedfs_replicates_multi_server_data_and_refreshes_filer_topology():
    defaults = read("roles/object-storage/defaults/main.yml")
    content = read("roles/object-storage/tasks/main.yml")
    normalize = read("playbooks/tasks/normalize_profile.yml")
    assert "object_storage_replication_placement" in defaults
    assert "enableReplication:" in content
    assert "replicationPlacement:" in content
    assert "defaultReplication:" in content
    assert "Migrate existing SeaweedFS volumes to the selected replica placement" in content
    assert "volume.configure.replication" in content
    assert "volume.fix.replication" in content
    assert "Verify every SeaweedFS volume satisfies the selected replica placement" in content
    assert "Refresh SeaweedFS filer topology after a chart or Raft change" in content
    assert "object_storage_replication_placement" in normalize


def test_pre_observability_roles_do_not_create_monitoring_crs():
    """Monitoring CRDs do not exist until k8s-observability installs them."""
    for path in (
        "roles/k8s-secrets/tasks/main.yml",
        "roles/object-storage/tasks/main.yml",
        "roles/elasticsearch/tasks/main.yml",
    ):
        content = read(path)
        assert "kind: ServiceMonitor" not in content, path
        assert "kind: VMServiceScrape" not in content, path


def test_observability_registers_enabled_predeployed_services():
    template = read("roles/k8s-observability/templates/vmservicescrapes.yml")
    tasks = read("roles/k8s-observability/tasks/main.yml")

    for component in ("object_storage", "secrets", "elasticsearch"):
        assert f"platform_{component}_enabled" in template
        assert f"platform_{component}_enabled" in tasks
    for service in ("seaweedfs", "vault", "elasticsearch"):
        assert f"name: {service}" in template
    assert "name: temporal" not in template
    assert "lookup('ansible.builtin.template'" in tasks
    assert "| from_yaml_all | list" in tasks


def test_platform_monitoring_uses_native_victoriametrics_resources():
    for path in (Path(ROOT) / "roles").rglob("*.yml"):
        if "kubespray" in path.parts:
            continue
        content = path.read_text(encoding="utf-8")
        assert "kind: ServiceMonitor" not in content, path
        for block in content.split("kind: VMRule")[:-1]:
            assert "apiVersion: operator.victoriametrics.com/" in block[-200:], path


def test_promtail_is_isolated_in_a_privileged_agent_namespace():
    observability = read("roles/k8s-observability/tasks/main.yml")
    assert "logging_agent_namespace: logging-agents" in observability
    assert "pod-security.kubernetes.io/enforce: privileged" in observability
    promtail = observability.split("name: Install Promtail for log collection", 1)[1]
    assert "release_namespace: '{{ logging_agent_namespace }}'" in promtail
    assert "name: default-deny" in promtail
    assert "name: allow-logging-egress" in promtail


def test_filebeat_is_isolated_in_the_privileged_agent_namespace():
    observability = read("roles/k8s-observability/tasks/main.yml")
    elasticsearch = read("roles/elasticsearch/tasks/main.yml")
    health = read("roles/k8s-observability/tasks/health_checks.yml")

    filebeat = observability.split("name: Install Filebeat for log collection (ELK)", 1)[1]
    assert "release_namespace: '{{ logging_agent_namespace }}'" in filebeat
    assert "name: Remove legacy Filebeat release from Elasticsearch namespace" in observability
    assert "name: Replicate the minimum Elasticsearch credentials into the agent namespace" in observability
    assert "pod-security.kubernetes.io/enforce: baseline" in elasticsearch
    assert "k8s:io.kubernetes.pod.namespace: logging-agents" in elasticsearch
    assert "name: Check Filebeat node coverage" in health


def test_private_api_tunnel_is_health_supervised():
    cluster = read("roles/k8s-cluster-management/tasks/main.yml")
    supervisor = read("scripts/kube-api-tunnel-supervisor.sh")

    assert "kube-api-tunnel-supervisor.sh" in cluster
    assert "--local-port \"$local_api_port\"" in cluster
    assert "--kubeconfig \"$kubeconfig\"" in cluster
    assert "KUBECONFIG=\"$KUBECONFIG_FILE\" kubectl" in supervisor
    assert "--server=\"https://127.0.0.1:${LOCAL_PORT}\"" in supervisor
    assert "failures >= 2" in supervisor
    assert "ServerAliveInterval=15" in supervisor
    assert "sleep 2" not in supervisor
    assert "seq 1 120" in cluster
    assert "did not stop within 30 seconds" in cluster
    assert "IPQoS=none" in supervisor
    assert 'TARGETS+=("$2")' in supervisor
    assert "target_index=$(((target_index + 1) % ${#TARGETS[@]}))" in supervisor
    assert "for target in (master_ips" in cluster


def test_vault_unauthenticated_metrics_access_is_listener_scoped():
    vault = read("roles/k8s-secrets/tasks/main.yml")
    assert vault.count("unauthenticated_metrics_access = true") == 2
    assert vault.count('listener "tcp" {') == 2
    assert vault.count("telemetry {") == 4
    assert "find /vault/data/raft -type f -exec chmod 0600" in vault
    assert "fsGroupChangePolicy: OnRootMismatch" in vault
    assert 'service_registration "kubernetes" {}' in vault


def test_database_retry_gates_tolerate_transient_api_failover():
    databases = read("roles/k8s-databases/tasks/main.yml")
    assert databases.count("resources | default([])") >= 7


def test_pmm_admin_secret_is_not_interpolated_into_process_arguments():
    observability = read("roles/k8s-observability/tasks/main.yml")
    task = observability.split("- name: Issue a validated PMM 3 service-account token", 1)[1].split(
        "- name: Set PMM service-account token fact", 1
    )[0]
    assert '"$PMM_ADMIN_PASSWORD"' in task
    assert 'os.environ["PMM_ADMIN_PASSWORD"]' in task
    assert "{{ grafana_admin_password }}" not in task
    assert "percona-db-key-$(date +%s%N)" in task
    assert "retries: 12" in task
    assert "until: >-" in task


def test_victoriametrics_alerting_uses_non_reserved_config_and_tier_datasource():
    alerting = read("roles/k8s-observability/tasks/alerting.yml")
    assert "name: platform-alertmanager-config" in alerting
    assert "configSecret: platform-alertmanager-config" in alerting
    assert "vmalertmanager-platform-config" not in alerting
    assert "vmsingle-vmsingle." in alerting
    assert "vmselect-vmcluster." in alerting


def test_hcloud_exporter_is_pinned_and_hardened():
    observability = read("roles/k8s-observability/tasks/main.yml")
    defaults = read("defaults/main.yml")
    assert 'hcloud_exporter_version: "3.21.0"' in defaults
    assert "promhippie/hcloud-exporter:{{ hcloud_exporter_version }}" in observability
    assert "HCLOUD_EXPORTER_TOKEN" in observability
    assert "HCLOUD_EXPORTER_COLLECTOR_SERVER_METRICS" in observability
    assert "containerPort: 9501" in observability
    assert "automountServiceAccountToken: false" in observability
    assert "runAsUser: 1337" in observability
    assert "runAsGroup: 1337" in observability
    assert observability.count("path: /healthz") >= 2
    assert "interval: 5m" in observability


def test_observability_health_checks_are_strict_and_cover_all_agents():
    checks = read("roles/k8s-observability/tasks/health_checks.yml")
    assert "ignore_errors: true" not in checks
    assert checks.count("status.updateStatus") >= 3
    for workload in ("promtail", "pmm-server", "hetzner-cloud-exporter"):
        assert f"name: {workload}" in checks


def test_postgresql_operator_config_uses_current_secure_schema():
    databases = read("roles/k8s-databases/tasks/main.yml")
    assert "pool_mode: transaction" in databases
    assert "max_client_conn: '1000'" in databases
    assert "default_pool_size: '25'" in databases
    assert "poolMode:" not in databases
    assert "maxClientConn:" not in databases
    assert "defaultPoolSize:" not in databases
    assert "customLibraries:" not in databases
    assert "SUPERUSER" not in databases
    assert " all md5" not in databases
    assert "percona/percona-distribution-postgresql:18.4-1" in databases
    assert "percona/percona-pgbouncer:1.25.2-1" in databases
    assert "percona/percona-pgbackrest:2.58.0-2" in databases
    assert "percona/pmm-client:3.8.1" in databases
    assert "PMM_SERVER_TOKEN" in databases


def test_os_hardening_waits_for_package_manager_and_does_not_hide_node_failures():
    tasks = read("roles/network-security/tasks/main.yml")
    assert "apt-get update" not in tasks
    assert "apt-get install" not in tasks
    bastion = tasks.split(
        "- name: Install auditd and unattended-upgrades on bastion", 1
    )[1].split("- name: 'Configure auditd rules on bastion", 1)[0]
    nodes = tasks.split(
        "- name: Install auditd and unattended-upgrades on K8s nodes", 1
    )[1].split("- name: Configure auditd rules on K8s nodes", 1)[0]

    for package_task in (bastion, nodes):
        assert "DPkg::Lock::Timeout=300" in package_task
        assert "/var/lib/dpkg/lock-frontend" in package_task
        assert "DEBIAN_FRONTEND=noninteractive" in package_task
        assert "apt-daily.timer" in package_task
    assert "REMOTECMD\n    done" in nodes
    assert "REMOTECMD\n    done || true" not in nodes
    assert "until: node_audit_packages is success" in nodes


def test_dragonfly_storage_growth_preserves_snapshot_claims():
    tasks = read("roles/dragonfly/tasks/main.yml")
    assert "reconcile_statefulset_storage.yml" in tasks
    assert "storage_reconcile_statefulset: dragonfly" in tasks
    assert "storage_reconcile_claim: df" in tasks
    assert "storage_reconcile_orphan: false" in tasks
    assert "storage_reconcile_wait: false" in tasks
