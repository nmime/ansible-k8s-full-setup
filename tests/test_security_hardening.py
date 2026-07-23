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
    tasks = read("roles/k8s-secrets/tasks/reconcile.yml")

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
        self.content = read("roles/k8s-secrets/tasks/reconcile.yml")

    def test_vault_uses_one_chart_managed_maintenance_pdb(self):
        """Every tier must be drainable without overlapping Vault PDBs."""
        assert re.search(
            r"(?ms)^\s{8}ha:\n\s{10}disruptionBudget:\n"
            r"\s{12}enabled: true\n\s{12}maxUnavailable: 1$",
            self.content,
        )
        assert "Remove legacy duplicate Vault PodDisruptionBudget" in self.content
        assert "Reconcile the chart-created Vault PodDisruptionBudget for maintenance" not in self.content
        renderer = read("roles/k8s-secrets/files/vault-post-renderer.sh")
        assert '.metadata.name == "vault"' in renderer
        assert ".spec.maxUnavailable) = 1" in renderer
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

    def test_vault_kv_v2_is_reconciled_after_every_initialization(self):
        reconcile = self.content.split(
            "- name: Discover Vault secrets engines before reconciliation", 1
        )[1].split("- name: Enable Vault file audit device", 1)[0]

        assert "vault secrets list -format=json" in reconcile
        assert "Reject an incompatible Vault secret mount" in reconcile
        assert "'secret/' not in vault_secret_mounts" in reconcile
        assert "Enable missing KV-v2 secrets engine" in reconcile
        assert "vault secrets enable -path=secret kv-v2" in reconcile
        assert "Prove the KV-v2 secrets engine is reconciled" in reconcile
        assert "vault_init is changed" not in reconcile
        assert "failed_when: false" not in reconcile
        assert reconcile.count("no_log: true") >= 6

    def test_vault_kubernetes_auth_is_discovered_and_verified(self):
        reconcile = self.content.split(
            "- name: Discover Vault auth methods before reconciliation", 1
        )[1].split("- name: Create Vault snapshot backup policy", 1)[0]

        assert reconcile.count("vault auth list -format=json") == 2
        assert "Reject an incompatible Vault Kubernetes auth mount" in reconcile
        assert "'kubernetes/' not in vault_auth_methods" in reconcile
        assert "Enable missing Kubernetes auth method" in reconcile
        assert "Prove the Kubernetes auth method is reconciled" in reconcile
        assert "Verify the Vault Kubernetes auth configuration" in reconcile
        assert "'https://kubernetes.default.svc:443'" in reconcile
        assert "vault_init is changed" not in reconcile
        assert "failed_when: false" not in reconcile
        assert reconcile.count("no_log: true") >= 8

    def test_vault_eso_policy_and_role_reconcile_fail_closed(self):
        reconcile = self.content.split("- name: Create Vault policy for ESO", 1)[1]
        reconcile = reconcile.split(
            "- name: Create ClusterSecretStore for Vault", 1
        )[0]

        assert "vault policy write external-secrets" in reconcile
        assert "auth/kubernetes/role/external-secrets" in reconcile
        assert "vault policy read external-secrets" in reconcile
        assert "Prove the Vault ESO auth role is reconciled" in reconcile
        assert ".data.token_ttl | int == 3600" in reconcile
        assert reconcile.count("vault_init_data is defined") >= 6
        assert "vault_init is changed" not in reconcile
        assert "failed_when: false" not in reconcile
        assert reconcile.count("no_log: true") >= 6

    def test_example_externalsecret_is_opt_in_and_removed_when_disabled(self):
        defaults = yaml.safe_load(read("defaults/main.yml"))
        assert defaults["eso_example_secret_enabled"] is False
        assert defaults["eso_example_secret_remote_key"] == ""
        assert defaults["eso_example_secret_remote_property"] == "password"

        tasks = yaml.safe_load(self.content)
        by_name = {task.get("name"): task for task in tasks}
        cleanup = by_name["Remove the disabled example ExternalSecret"]
        assert cleanup["kubernetes.core.k8s"] == {
            "state": "absent",
            "api_version": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "name": "example-secret",
            "namespace": "default",
            "wait": True,
            "wait_timeout": 60,
        }
        assert "not (eso_example_secret_enabled | bool)" in cleanup["when"]
        assert (
            "(_eso_example_externalsecret_crd.resources | default([]) | length) == 1"
            in cleanup["when"]
        )
        assert "_eso_example_externalsecret_managed | bool" in cleanup["when"]

        target_cleanup = by_name["Remove the disabled example target Secret"]
        assert target_cleanup["kubernetes.core.k8s"]["state"] == "absent"
        assert target_cleanup["kubernetes.core.k8s"]["kind"] == "Secret"
        assert target_cleanup["no_log"] is True
        assert target_cleanup["when"] == [
            "not (eso_example_secret_enabled | bool)",
            "_eso_example_target_secret_managed | bool",
        ]

        classification = by_name["Classify managed example fixture ownership"]
        ownership = " ".join(classification["ansible.builtin.set_fact"].values())
        assert 'platform.example.com/fixture' in ownership
        assert "secretStoreRef.name" in ownership
        assert "remoteRef.key" in ownership
        assert "metadata.ownerReferences" in ownership
        assert "selectattr('uid', 'equalto'" in ownership
        assert classification["no_log"] is True

        for gate_name in (
            "Refuse to delete an unmanaged example ExternalSecret collision",
            "Refuse to delete an unmanaged example target Secret collision",
        ):
            gate = by_name[gate_name]
            assert gate["no_log"] is True
            assert "left unchanged" in gate["ansible.builtin.assert"]["fail_msg"]

        create = by_name["Create the opt-in example ExternalSecret"]
        assert create["when"] == "eso_example_secret_enabled | bool"
        definition = create["kubernetes.core.k8s"]["definition"]
        assert definition["metadata"]["labels"]["platform.example.com/fixture"] == (
            "example-secret"
        )
        assert definition["spec"]["target"]["template"]["metadata"]["labels"][
            "platform.example.com/fixture"
        ] == "example-secret"
        remote = definition["spec"]["data"][0]["remoteRef"]
        assert remote == {
            "key": "{{ eso_example_secret_remote_key }}",
            "property": "{{ eso_example_secret_remote_property }}",
        }

    def test_example_externalsecret_requires_an_existing_vault_source(self):
        tasks = yaml.safe_load(self.content)
        by_name = {task.get("name"): task for task in tasks}

        contract = by_name["Validate the opt-in example ExternalSecret contract"]
        requirements = contract["ansible.builtin.assert"]["that"]
        assert "eso_enabled | bool" in requirements
        assert "vault_init_data is defined" in requirements
        assert any("eso_example_secret_remote_key" in item for item in requirements)
        assert any("eso_example_secret_remote_property" in item for item in requirements)

        verify = by_name["Verify the opt-in example source key exists in Vault"]
        assert 'vault kv get -format=json "secret/$2"' in verify[
            "kubernetes.core.k8s_exec"
        ]["command"]
        assert verify["no_log"] is True
        assert verify["when"] == "eso_example_secret_enabled | bool"

        property_gate = by_name[
            "Require the opt-in example source property to exist in Vault"
        ]
        assert property_gate["no_log"] is True
        assert "data.data[eso_example_secret_remote_property] is defined" in " ".join(
            property_gate["ansible.builtin.assert"]["that"]
        )

        normalized = read("playbooks/tasks/normalize_profile.yml")
        assert "secrets.eso.example_secret.enabled" in normalized
        assert "not eso_example_secret_enabled | bool or platform_eso_enabled | bool" in normalized

        example = yaml.safe_load(read("platform-orchestrator/platform.example.yaml"))
        assert example["secrets"]["eso"]["example_secret"] == {
            "enabled": False,
            "remote_key": "",
            "remote_property": "password",
        }

        readme = read("README.md")
        assert "vault kv put secret/demo/app" in readme
        assert "Reconciliation fails closed if the source key or property does not exist" in readme

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
        assert "requiredDuringSchedulingIgnoredDuringExecution:" in self.content
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
        "roles/k8s-secrets/tasks/reconcile.yml",
        "roles/k8s-autoscaling/tasks/main.yml",
        "roles/gitlab-selfhosted/tasks/main.yml",
        "roles/k8s-gitops/tasks/main.yml",
        "roles/k8s-databases/tasks/main.yml",
        "roles/k8s-observability/tasks/main.yml",
        "roles/k8s-observability/tasks/coroot.yml",
        "roles/backup-restore/tasks/velero.yml",
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


def test_keda_admission_fails_closed_and_remains_reachable():
    content = read("roles/k8s-autoscaling/tasks/main.yml")
    helm_values = content.split("- name: Install KEDA with Helm", 1)[1].split(
        "- name:", 1
    )[0]
    assert "webhooks:" in helm_values
    assert "failurePolicy: Fail" in helm_values

    ingress = content.split(
        "- name: Allow Kubernetes API and monitoring traffic to KEDA endpoints", 1
    )[1].split("- name:", 1)[0]
    assert "name: allow-keda-ingress" in ingress
    assert "- kube-apiserver" in ingress
    assert "port: '6443'" in ingress
    assert "port: '9443'" in ingress
    assert "- host" in ingress
    assert "- remote-node" in ingress


def test_coroot_network_policy_preserves_only_required_data_paths():
    content = read("roles/k8s-observability/tasks/coroot.yml")
    policy = content.split(
        "- name: Allow only required Coroot application and agent traffic", 1
    )[1].split("- name:", 1)[0]
    assert "name: allow-coroot-required-traffic" in policy
    for namespace in ("coroot", "kube-system"):
        assert f"k8s:io.kubernetes.pod.namespace: {namespace}" in policy
    assert "- ingress" in policy
    assert "k8s:io.kubernetes.pod.namespace: cilium-system" not in policy
    assert "- kube-apiserver" in policy
    assert "serviceName: '{{ \"vmselect-vmcluster\"" in policy
    assert "serviceName: '{{ \"vminsert-vmcluster\"" in policy
    for port in ("'53'", "'8080'"):
        assert f"port: {port}" in policy
    for port in ("8429", "8480", "8481"):
        assert port in policy


def test_external_secrets_network_policy_keeps_webhook_vault_and_api_paths():
    content = read("roles/k8s-secrets/tasks/reconcile.yml")
    policy = content.split(
        "- name: Allow only required External Secrets reconciliation traffic", 1
    )[1].split("- name:", 1)[0]
    assert "name: allow-external-secrets-required-traffic" in policy
    assert "- kube-apiserver" in policy
    assert "port: '10250'" in policy
    assert "serviceName: vault" in policy
    assert "namespace: '{{ vault_ns }}'" in policy
    assert "port: '8200'" in policy
    assert "k8s:io.kubernetes.pod.namespace: monitoring" in policy


def test_velero_network_policy_uses_configured_external_storage_port():
    content = read("roles/backup-restore/tasks/velero.yml")
    assert "urlsplit('scheme')" in content
    assert "urlsplit('hostname')" in content
    assert "urlsplit('port')" in content
    policy = content.split(
        'name: "Backup-DR | Allow only required Velero control and backup traffic"',
        1,
    )[1].split("- name:", 1)[0]
    assert "name: allow-velero-required-traffic" in policy
    assert "- kube-apiserver" in policy
    assert "- host" in policy
    assert 'port: "{{ backup_dr_storage_port | string }}"' in policy
    assert "toFQDNs:" in policy
    assert 'matchName: "{{ backup_dr_storage_hostname }}"' in policy
    egress = policy.split("egress:", 1)[1]
    assert "- world" not in egress
    assert "- remote-node" not in egress
    assert "port: '8085'" in policy


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
        "roles/k8s-secrets/tasks/reconcile.yml",
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
    assert "name: Remove the legacy replicated Elasticsearch superuser secret" in observability
    assert "logging-ingest-credentials" in observability
    assert "platform_logging_ingest" in elasticsearch
    legacy_cleanup = observability.split(
        "- name: Remove the legacy replicated Elasticsearch superuser secret", 1
    )[1].split(
        "- name: Remove replicated logging credentials when Elasticsearch logging is deselected",
        1,
    )[0]
    replicated_cleanup = observability.split(
        "- name: Remove replicated logging credentials when Elasticsearch logging is deselected",
        1,
    )[1].split("- name: Read Elasticsearch secrets", 1)[0]
    assert "no_log: true" in legacy_cleanup
    assert "no_log: true" in replicated_cleanup
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
    vault = read("roles/k8s-secrets/tasks/reconcile.yml")
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


def test_victoriametrics_replication_tracks_storage_replicas_not_resource_envelope():
    observability = read("roles/k8s-observability/tasks/main.yml")
    production = load_yaml("platform-orchestrator/profiles/production.yaml")
    optimized = load_yaml("platform-orchestrator/profiles/medium-optimized.yaml")

    assert production["resource_tier"] == "small"
    assert production["observability"]["metrics"]["replicas"] == 2
    assert production["observability"]["metrics"]["replication_factor"] == 2
    assert optimized["resource_tier"] == "small"
    assert optimized["observability"]["metrics"]["replicas"] == 1
    assert optimized["observability"]["metrics"]["replication_factor"] == 1
    assert "vm_replication_factor:" in observability
    assert "replicationFactor: '{{ vm_replication_factor | int }}'" in observability


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


def test_operator_injected_database_containers_have_tier_aware_resources():
    defaults = yaml.safe_load(read("defaults/main.yml"))
    normalized = read("playbooks/tasks/normalize_profile.yml")
    database_tasks = yaml.safe_load(read("roles/k8s-databases/tasks/main.yml"))

    resource_defaults = (
        "database_container_default_resources",
        "postgresql_replica_cert_copy_resources",
        "postgresql_pgbackrest_resources",
        "postgresql_pgbackrest_config_resources",
        "postgresql_pgbackrest_init_resources",
        "postgresql_pgbackrest_repo_host_resources",
        "postgresql_pgbackrest_job_resources",
        "mongodb_backup_resources",
        "mongodb_pmm_resources",
    )
    for variable in resource_defaults:
        resources = defaults[variable]
        assert set(resources) == {"requests", "limits"}
        assert set(resources["requests"]) == {"cpu", "memory"}
        assert set(resources["limits"]) == {"cpu", "memory"}
        assert "resource_tier" in resources["requests"]["cpu"]
        assert variable in normalized

    limit_range_task = next(
        task
        for task in database_tasks
        if task.get("name") == "Bound operator-created database helper containers at admission"
    )
    limit_range = limit_range_task["kubernetes.core.k8s"]["definition"]
    assert limit_range["kind"] == "LimitRange"
    container_defaults = limit_range["spec"]["limits"][0]
    assert container_defaults["type"] == "Container"
    assert container_defaults["default"] == (
        "{{ database_container_default_resources.limits }}"
    )
    assert container_defaults["defaultRequest"] == (
        "{{ database_container_default_resources.requests }}"
    )

    pg_task = next(
        task
        for task in database_tasks
        if task.get("name") == "Create PostgreSQL cluster (PG Operator 3.x — v2 API)"
    )
    pg_spec = pg_task["kubernetes.core.k8s"]["definition"]["spec"]
    assert pg_spec["instances"][0]["containers"]["replicaCertCopy"]["resources"] == (
        "{{ postgresql_replica_cert_copy_resources }}"
    )
    pgbackrest = pg_spec["backups"]["pgbackrest"]
    assert pgbackrest["containers"]["pgbackrest"]["resources"] == (
        "{{ postgresql_pgbackrest_resources }}"
    )
    assert pgbackrest["containers"]["pgbackrestConfig"]["resources"] == (
        "{{ postgresql_pgbackrest_config_resources }}"
    )
    assert pgbackrest["initContainer"]["resources"] == (
        "{{ postgresql_pgbackrest_init_resources }}"
    )
    assert pgbackrest["initContainer"]["image"] == (
        "docker.io/percona/percona-postgresql-operator:{{ pg_operator_ver }}"
    )
    assert pgbackrest["jobs"]["resources"] == (
        "{{ postgresql_pgbackrest_job_resources }}"
    )
    assert pgbackrest["repoHost"]["resources"] == (
        "{{ postgresql_pgbackrest_repo_host_resources }}"
    )

    mongo_task = next(
        task
        for task in database_tasks
        if task.get("name") == "Create MongoDB cluster"
    )
    mongo_spec = mongo_task["kubernetes.core.k8s"]["definition"]["spec"]
    assert mongo_spec["backup"]["resources"] == "{{ mongodb_backup_resources }}"
    assert mongo_spec["pmm"]["resources"] == "{{ mongodb_pmm_resources }}"


def test_platform_operators_have_bounded_resources_and_restricted_pod_security():
    observability_tasks = yaml.safe_load(read("roles/k8s-observability/tasks/main.yml"))
    vm_install = next(
        task
        for task in observability_tasks
        if task.get("name") == "Install VictoriaMetrics Operator"
    )
    vm_values = vm_install["kubernetes.core.helm"]["values"]
    assert vm_values["resources"]["requests"] == {"cpu": "50m", "memory": "128Mi"}
    assert vm_values["resources"]["limits"] == {"cpu": "200m", "memory": "256Mi"}
    assert vm_values["podSecurityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert vm_values["securityContext"]["allowPrivilegeEscalation"] is False
    assert vm_values["securityContext"]["capabilities"]["drop"] == ["ALL"]

    database_tasks = yaml.safe_load(read("roles/k8s-databases/tasks/main.yml"))
    mongo_install = next(
        task
        for task in database_tasks
        if task.get("name") == "Install Percona MongoDB Operator"
    )
    mongo_values = mongo_install["kubernetes.core.helm"]["values"]
    assert mongo_values["resources"]["requests"] == {
        "cpu": "50m",
        "memory": "128Mi",
    }
    assert mongo_values["resources"]["limits"] == {
        "cpu": "250m",
        "memory": "256Mi",
    }
    assert mongo_values["podSecurityContext"]["runAsNonRoot"] is True
    assert mongo_values["podSecurityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert mongo_values["securityContext"]["allowPrivilegeEscalation"] is False
    assert mongo_values["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert mongo_values["securityContext"]["readOnlyRootFilesystem"] is True

    pg_hardening = next(
        task
        for task in database_tasks
        if task.get("name") == "Harden PG Operator pod security and allow slow image pulls"
    )
    patch = pg_hardening["shell"]
    assert '"progressDeadlineSeconds":1800' in patch
    assert '"seccompProfile":{"type":"RuntimeDefault"}' in patch


def test_configurable_platform_addons_are_not_best_effort():
    cluster_tasks = yaml.safe_load(read("roles/k8s-cluster-management/tasks/main.yml"))
    cert_manager = next(
        task for task in cluster_tasks if task.get("name") == "Install cert-manager"
    )["kubernetes.core.helm"]["values"]
    for component in (cert_manager, cert_manager["webhook"], cert_manager["cainjector"]):
        assert component["resources"]["requests"]["cpu"]
        assert component["resources"]["requests"]["memory"]
        assert component["resources"]["limits"]["cpu"]
        assert component["resources"]["limits"]["memory"]
    assert cert_manager["startupapicheck"]["resources"]["requests"]

    hcloud_webhook = next(
        task
        for task in cluster_tasks
        if task.get("name")
        == "Install official Hetzner Cloud DNS webhook for cert-manager"
    )["kubernetes.core.helm"]["values"]
    assert hcloud_webhook["resources"]["requests"] == {
        "cpu": "10m",
        "memory": "32Mi",
    }
    assert hcloud_webhook["resources"]["limits"] == {
        "cpu": "100m",
        "memory": "128Mi",
    }

    object_storage = yaml.safe_load(read("roles/object-storage/tasks/main.yml"))
    seaweed = next(
        task
        for task in object_storage[0]["block"]
        if task.get("name") == "Install SeaweedFS via official Helm chart"
    )["kubernetes.core.helm"]["values"]
    for component in ("master", "volume", "filer"):
        assert seaweed[component]["resources"]["requests"]["cpu"]
        assert seaweed[component]["resources"]["requests"]["memory"]
        assert seaweed[component]["resources"]["limits"]["cpu"]
        assert seaweed[component]["resources"]["limits"]["memory"]

    coroot_tasks = yaml.safe_load(read("roles/k8s-observability/tasks/coroot.yml"))
    coroot = next(
        task
        for task in coroot_tasks
        if task.get("name") == "Install pinned Coroot CE with external VictoriaMetrics"
    )["kubernetes.core.helm"]["values"]
    keeper = coroot["clickhouse"]["keeper"]["resources"]
    assert keeper["requests"] == {"cpu": "50m", "memory": "128Mi"}
    assert keeper["limits"] == {"cpu": "500m", "memory": "512Mi"}

    secrets_tasks = yaml.safe_load(read("roles/k8s-secrets/tasks/reconcile.yml"))
    vault = next(
        task for task in secrets_tasks if task.get("name") == "Install Vault via Helm"
    )["kubernetes.core.helm"]["values"]["server"]
    assert vault["resources"]["requests"] == {"cpu": "100m", "memory": "256Mi"}
    assert vault["resources"]["limits"] == {"cpu": "1", "memory": "1Gi"}
    init_resources = vault["extraInitContainers"][0]["resources"]
    assert init_resources["requests"] == {"cpu": "10m", "memory": "32Mi"}
    assert init_resources["limits"] == {"cpu": "100m", "memory": "128Mi"}

    eso = next(
        task
        for task in secrets_tasks
        if task.get("name") == "Install External Secrets Operator via Helm"
    )["kubernetes.core.helm"]["values"]
    for component in (eso, eso["webhook"], eso["certController"]):
        assert component["resources"]["requests"]["cpu"]
        assert component["resources"]["requests"]["memory"]
        assert component["resources"]["limits"]["cpu"]
        assert component["resources"]["limits"]["memory"]

    blackbox_tasks = yaml.safe_load(read("roles/blackbox-exporter/tasks/main.yml"))
    blackbox = next(
        task
        for task in blackbox_tasks
        if task.get("name") == "Install blackbox-exporter via Helm"
    )["kubernetes.core.helm"]["values"]
    assert blackbox["podSecurityContext"]["runAsNonRoot"] is True
    assert blackbox["podSecurityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert blackbox["securityContext"]["allowPrivilegeEscalation"] is False
    assert blackbox["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert blackbox["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"


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


def test_secret_bearing_facts_and_secret_reads_are_censored():
    sensitive_tasks = {
        "roles/dragonfly/tasks/main.yml": ["Set Dragonfly variables"],
        "roles/elasticsearch/tasks/main.yml": [
            "Set Elasticsearch variables",
            "Read the current Elasticsearch credentials before rotation",
            "Read the Kibana service-account token secret",
            "Read the Kibana encryption-key secret",
        ],
        "roles/k8s-databases/tasks/main.yml": [
            "Retrieve Percona-generated GitLab PG password",
            "Set GitLab PG password fact",
            "Retrieve Percona-generated app PG password",
            "Set app PG password fact",
        ],
        "roles/k8s-observability/tasks/main.yml": [
            "Set PMM service-account token fact",
        ],
        "roles/object-storage/tasks/main.yml": ["Set object-storage resolved facts"],
        "roles/postal/tasks/main.yml": ["Set Postal variables"],
    }

    for path, names in sensitive_tasks.items():
        content = read(path)
        for name in names:
            block = content.split(f"- name: {name}", 1)[1].split("\n- name:", 1)[0]
            assert "no_log: true" in block, f"{path}: {name} must be censored"


def test_platform_fact_gathering_excludes_controller_environment_secrets():
    playbook = read("playbooks/deploy_platform.yml")

    assert "gather_facts: false" in playbook
    assert "Gather controller facts without persisting the process environment" in playbook
    assert "- '!env'" in playbook


def test_elasticsearch_password_rotation_precedes_secret_update():
    tasks = read("roles/elasticsearch/tasks/main.yml")
    assert tasks.index("Rotate the Elasticsearch elastic user") < tasks.index(
        "Create ES credentials secret"
    )
    assert tasks.count("platform.example.com/elasticsearch-credentials-hash") == 2
    rotation = tasks.split("- name: Rotate the Elasticsearch elastic user", 1)[1].split(
        "\n- name:", 1
    )[0]
    assert "no_log: true" in rotation
    assert "_security/user/elastic/_password" in rotation


def test_dragonfly_auth_secret_change_rolls_statefulset():
    tasks = read("roles/dragonfly/tasks/main.yml")
    assert tasks.index("Create Dragonfly instance") < tasks.index(
        "Restart Dragonfly OnDelete pods one at a time after credential changes"
    )
    rollout = tasks.split(
        "- name: Restart Dragonfly OnDelete pods one at a time after credential changes", 1
    )[1].split("\n- name:", 1)[0]
    assert 'pod="dragonfly-${ordinal}"' in rollout
    assert "kubectl delete pod" in rollout
    assert "--wait=false" in rollout
    assert "old_uid=" in rollout and "new_uid=" in rollout
    assert '"$new_uid" != "$old_uid"' in rollout
    assert "replacement pod $pod did not become Ready within 5 minutes" in rollout
    assert "no_log: true" in rollout
    assert "platform.example.com/dragonfly-applied-credentials-hash" in tasks
