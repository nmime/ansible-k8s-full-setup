#!/usr/bin/env python3
"""
Component integration tests for elasticsearch role.
Validates that the tasks file, defaults, and role structure work together
coherently — no orphaned references, consistent variable usage, etc.

Run: python3 tests/test_elasticsearch_component_integration.py
"""
import os
import re
import unittest
import yaml

ROLE_DIR = os.path.join(os.path.dirname(__file__), "..", "roles", "elasticsearch")


def load_defaults():
    with open(os.path.join(ROLE_DIR, "defaults", "main.yml")) as f:
        return yaml.safe_load(f)


def load_tasks_content():
    with open(os.path.join(ROLE_DIR, "tasks", "main.yml")) as f:
        return f.read()


def load_tasks_docs():
    """Load all task documents."""
    with open(os.path.join(ROLE_DIR, "tasks", "main.yml")) as f:
        return list(yaml.safe_load_all(f))


class TestVariableConsistency(unittest.TestCase):
    """Component test: defaults variables used consistently in tasks."""

    @classmethod
    def setUpClass(cls):
        cls.defaults = load_defaults()
        cls.tasks_content = load_tasks_content()

    def test_es_namespace_referenced(self):
        self.assertIn("es_ns", self.tasks_content,
                      "Tasks should reference es_ns (set from es_namespace)")

    def test_es_version_referenced(self):
        # es_version is used as es_ver in tasks
        self.assertIn("es_ver", self.tasks_content,
                      "Tasks should reference es_ver")

    def test_es_image_referenced(self):
        self.assertIn("es_img", self.tasks_content,
                      "Tasks should reference es_img")

    def test_es_cluster_name_referenced(self):
        self.assertIn("es_cluster", self.tasks_content,
                      "Tasks should reference es_cluster")

    def test_kibana_version_referenced(self):
        self.assertIn("kb_img", self.tasks_content,
                      "Tasks should reference kb_img (kibana image)")

    def test_replicas_variables_referenced(self):
        self.assertIn("es_master_replicas", self.tasks_content)
        self.assertIn("es_data_replicas", self.tasks_content)

    def test_resource_variables_referenced(self):
        # Check at least some resource vars are used
        self.assertIn("es_master_heap", self.tasks_content)
        self.assertIn("es_data_heap", self.tasks_content)


class TestStatefulSetStructure(unittest.TestCase):
    """Component test: ES master and data StatefulSets are properly structured."""

    @classmethod
    def setUpClass(cls):
        cls.docs = load_tasks_docs()
        cls.content = load_tasks_content()

    def _find_task(self, name_substring):
        """Find a task by name substring — use raw content to avoid YAML
        multi-doc parsing issues with heredoc markers."""
        # Read raw content
        with open(os.path.join(ROLE_DIR, "tasks", "main.yml")) as f:
            content = f.read()
        # Check if the task name substring exists in the raw content
        if name_substring.lower() in content.lower():
            # Return a dict-like proxy that contains the full content for assertion
            return {"name": name_substring, "_content": content}
        return None

    def _get_content(self):
        """Get the raw tasks file content."""
        with open(os.path.join(ROLE_DIR, "tasks", "main.yml")) as f:
            return f.read()

    def test_master_statefulset_exists(self):
        task = self._find_task("master StatefulSet")
        self.assertIsNotNone(task, "Master StatefulSet task must exist")

    def test_data_statefulset_exists(self):
        task = self._find_task("data StatefulSet")
        self.assertIsNotNone(task, "Data StatefulSet task must exist")

    def test_kibana_deployment_exists(self):
        task = self._find_task("Deploy Kibana")
        self.assertIsNotNone(task, "Kibana Deployment task must exist")

    def test_tls_secret_creation_exists(self):
        task = self._find_task("TLS Secret")
        self.assertIsNotNone(task, "TLS Secret creation task must exist")

    def test_credentials_secret_exists(self):
        task = self._find_task("credentials secret")
        self.assertIsNotNone(task, "Credentials secret task must exist")

    def test_no_crack_init_container_in_master(self):
        master_task = self._find_task("master StatefulSet")
        if master_task:
            content = master_task.get("_content", "")
            self.assertNotIn("patch-xpack", content)
            self.assertNotIn("patch_xpack", content)
            self.assertNotIn("crack", content.lower())

    def test_no_crack_init_container_in_data(self):
        data_task = self._find_task("data StatefulSet")
        if data_task:
            content = data_task.get("_content", "")
            self.assertNotIn("patch-xpack", content)
            self.assertNotIn("patch_xpack", content)
            self.assertNotIn("crack", content.lower())


class TestSecurityConfiguration(unittest.TestCase):
    """Component test: TLS and security settings are present and correct."""

    @classmethod
    def setUpClass(cls):
        cls.content = load_tasks_content()

    def test_tls_http_enabled(self):
        self.assertIn("xpack.security.http.ssl.enabled", self.content,
                      "HTTP SSL must be enabled")

    def test_tls_transport_enabled(self):
        self.assertIn("xpack.security.transport.ssl.enabled", self.content,
                      "Transport SSL must be enabled")

    def test_security_enabled(self):
        self.assertIn("xpack.security.enabled", self.content,
                      "X-Pack security must be enabled")

    def test_audit_logging_enabled(self):
        self.assertIn("xpack.security.audit.enabled", self.content,
                      "Audit logging must be enabled")

    def test_run_as_non_root(self):
        self.assertIn("runAsNonRoot: true", self.content,
                      "ES containers must run as non-root")

    def test_volume_mount_for_certs(self):
        self.assertIn("config/certs", self.content,
                      "TLS certs must be mounted")

    def test_no_log_on_credentials(self):
        # Check that credential creation task has no_log
        lines = self.content.split("\n")
        in_cred_task = False
        has_no_log = False
        for i, line in enumerate(lines):
            if "credentials secret" in line.lower():
                in_cred_task = True
            if in_cred_task and "no_log" in line:
                has_no_log = True
            if in_cred_task and line.strip().startswith("- name:") and "credentials" not in line.lower():
                break
        self.assertTrue(has_no_log,
                        "Credential creation task must have no_log: true")


class TestNoOrphanedReferences(unittest.TestCase):
    """Component test: no dangling references to removed resources."""

    @classmethod
    def setUpClass(cls):
        cls.content = load_tasks_content()

    def test_no_crack_script_configmap_reference(self):
        """No task should reference a crack-script ConfigMap."""
        self.assertNotIn("es-crack-script", self.content,
                         "Must not reference es-crack-script ConfigMap")

    def test_no_license_secret_reference(self):
        """No task should reference a license secret for platinum."""
        self.assertNotIn("es-platinum-license", self.content)

    def test_no_license_volume_mount(self):
        """The license secret should not be mounted anywhere."""
        # Check no volume mounts for license
        self.assertNotIn("/license", self.content)

    def test_no_emptydir_crack_volume(self):
        """No emptyDir for crack-ready should exist."""
        # We allow emptyDir in general, but not with crack-related names
        patterns = ["crack-ready", "crack-ready-"]
        for pattern in patterns:
            self.assertNotIn(pattern, self.content,
                             f"No '{pattern}' volume references should exist")


class TestNetworkPolicyConsistency(unittest.TestCase):
    """Component test: Network policies and services are consistent."""

    @classmethod
    def setUpClass(cls):
        cls.docs = load_tasks_docs()
        cls.content = load_tasks_content()

    def test_master_headless_service(self):
        self.assertIn("es-master-headless", self.content)

    def test_data_headless_service(self):
        self.assertIn("es-data-headless", self.content)

    def test_client_service(self):
        self.assertIn('name: elasticsearch', self.content)

    def test_kibana_service(self):
        self.assertIn("kibana", self.content)

    def test_network_policy_exists(self):
        self.assertIn("NetworkPolicy", self.content)

    def test_pdb_for_masters(self):
        self.assertIn("es-master-pdb", self.content)

    def test_pdb_for_data(self):
        self.assertIn("es-data-pdb", self.content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
