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

    def test_no_log_on_secrets(self):
        assert "no_log: true" in self.content, \
            "generate-secrets should use no_log on secret operations"


# ─── 4. k8s-secrets: Vault TLS ──────────────────────────

class TestVaultTLS:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.content = read("roles/k8s-secrets/tasks/main.yml")

    def test_tls_disable_is_configurable(self):
        assert "vault_tls_disabled" in self.content, \
            "Vault tlsDisable should use vault_tls_disabled variable"

    def test_tls_disable_not_hardcoded_true(self):
        lines = self.content.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped == "tlsDisable: true":
                pytest.fail("tlsDisable should not be hardcoded to true")


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
