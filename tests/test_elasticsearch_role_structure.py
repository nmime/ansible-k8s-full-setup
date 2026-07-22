#!/usr/bin/env python3
"""
Unit tests for elasticsearch role YAML structure and compliance.
Validates that the role defaults and task definitions are correctly
structured and contain no illegal license bypass artifacts.

Run: python3 tests/test_elasticsearch_role_structure.py
"""
import json
import os
import sys
import unittest

import yaml

ROLE_DIR = os.path.join(os.path.dirname(__file__), "..", "roles", "elasticsearch")


class TestDefaults(unittest.TestCase):
    """Unit tests: defaults/main.yml content."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROLE_DIR, "defaults", "main.yml")
        with open(path) as f:
            cls.defaults = yaml.safe_load(f)

    def test_license_type_is_basic(self):
        self.assertEqual(self.defaults["es_license_type"], "basic",
                         "es_license_type must be 'basic'")

    def test_no_platinum_mention(self):
        path = os.path.join(ROLE_DIR, "defaults", "main.yml")
        with open(path) as f:
            content = f.read()
        self.assertNotIn("platinum", content.lower(),
                         "defaults must not mention 'platinum'")

    def test_es_version_present(self):
        self.assertIn("es_version", self.defaults,
                      "es_version must be defined")

    def test_es_namespace_defined(self):
        self.assertEqual(self.defaults["es_namespace"], "elasticsearch")

    def test_no_crack_related_vars(self):
        for key in self.defaults:
            self.assertNotIn("crack", key.lower(),
                             f"Key '{key}' must not contain 'crack'")

    def test_no_license_file_reference(self):
        for key in self.defaults:
            self.assertNotIn("license_file", key.lower(),
                             f"Key '{key}' must not reference license file")


class TestTasks(unittest.TestCase):
    """Unit tests: tasks/main.yml structure and content."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROLE_DIR, "tasks", "main.yml")
        with open(path) as f:
            cls.content = f.read()
            cls.tasks = yaml.safe_load_all(f)
            cls.tasks_list = list(cls.tasks)

    def test_no_crack_script_configmap(self):
        self.assertNotIn("crack-script", self.content,
                         "tasks must not contain 'crack-script'")

    def test_no_crack_ready_mount(self):
        self.assertNotIn("crack-ready", self.content,
                         "tasks must not contain 'crack-ready'")

    def test_no_patch_xpack(self):
        self.assertNotIn("patch_xpack", self.content,
                         "tasks must not contain 'patch_xpack'")

    def test_no_license_java(self):
        self.assertNotIn("License.java", self.content,
                         "tasks must not reference License.java")

    def test_no_license_verifier_java(self):
        self.assertNotIn("LicenseVerifier.java", self.content,
                         "tasks must not reference LicenseVerifier.java")

    def test_no_javac(self):
        self.assertNotIn("javac", self.content,
                         "tasks must not contain javac compilation")

    def test_no_es_platinum_license_secret(self):
        self.assertNotIn("es-platinum-license", self.content,
                         "tasks must not create es-platinum-license secret")

    def test_no_platinum_license_application_job(self):
        self.assertNotIn("Apply Platinum license", self.content,
                         "tasks must not apply Platinum license")

    def test_basic_license_env_var_present(self):
        self.assertIn("xpack.license.self_generated.type", self.content,
                      "tasks must set xpack.license.self_generated.type")

    def test_basic_license_value_is_basic(self):
        self.assertIn("value: 'basic'", self.content,
                      "xpack.license.self_generated.type must be 'basic'")

    def test_no_jar_replacement_command(self):
        self.assertNotIn("x-pack-core", self.content,
                         "tasks must not reference x-pack-core JAR replacement")

    def test_no_platinum_in_summary(self):
        # Deployment summary should mention Basic, not Platinum
        lines = self.content.split("\n")
        summary_lines = [l for l in lines if "Deployed" in l and "Elasticsearch" in l]
        for line in summary_lines:
            self.assertNotIn("Platinum", line,
                             f"Summary line must not mention Platinum: {line}")

    def test_has_namespace_creation_task(self):
        # YAML multi-doc parsing can split on shell heredoc ---; use raw content
        self.assertIn("Create Elasticsearch namespace", self.content,
                      "Should have namespace creation task")

    def test_has_master_statefulset(self):
        self.assertIn("es-master", self.content,
                      "Must deploy es-master StatefulSet")

    def test_has_data_statefulset(self):
        self.assertIn("es-data", self.content,
                      "Must deploy es-data StatefulSet")

    def test_production_stateful_replicas_can_use_reserved_control_plane_capacity(self):
        toleration = 'node-role.kubernetes.io/control-plane'
        self.assertGreaterEqual(self.content.count(toleration), 2)
        self.assertIn('if tier == "production" else []', self.content)

    def test_has_kibana_deployment(self):
        self.assertIn("kibana", self.content.lower(),
                      "Must deploy Kibana")

    def test_has_tls_secret(self):
        self.assertIn("es-tls-certs", self.content,
                      "Must create TLS secret")

    def test_has_credentials_secret(self):
        self.assertIn("es-credentials", self.content,
                      "Must create credentials secret")

    def test_requires_green_cluster_health_without_unassigned_shards(self):
        self.assertIn(
            "Require green Elasticsearch cluster health after data-node reconciliation",
            self.content,
        )
        self.assertIn("wait_for_status=green", self.content)
        self.assertIn("unassigned_shards", self.content)
        self.assertIn("timed_out", self.content)

    def test_workloads_have_compact_node_startup_probes(self):
        """Slow first boots must not be killed by liveness before quorum forms."""
        self.assertEqual(self.content.count("startupProbe:"), 3)
        self.assertEqual(self.content.count("failureThreshold: 40"), 2)
        self.assertEqual(self.content.count("failureThreshold: 80"), 1)

    def test_kibana_uses_a_service_account_token(self):
        self.assertIn("ELASTICSEARCH_SERVICEACCOUNTTOKEN", self.content)
        self.assertIn("kibana-service-token", self.content)
        self.assertNotIn("name: ELASTICSEARCH_USERNAME", self.content)
        self.assertIn("kibana-encryption-keys", self.content)
        self.assertIn("XPACK_SECURITY_SECURECOOKIES", self.content)

    def test_kibana_recreate_transition_removes_stale_rolling_update(self):
        self.assertIn("type: Recreate", self.content)
        self.assertIn("/spec/strategy/rollingUpdate", self.content)

    def test_initial_master_nodes_is_bootstrap_only(self):
        self.assertIn(
            "Detect whether the Elasticsearch cluster has persistent master data",
            self.content,
        )
        self.assertIn(
            "Add the one-time Elasticsearch bootstrap node list for a new cluster",
            self.content,
        )
        self.assertIn(
            "Remove the one-time Elasticsearch bootstrap node list",
            self.content,
        )
        self.assertIn("cluster.initial_master_nodes-", self.content)
        statefulset = self.content.split("- name: Deploy ES master StatefulSet", 1)[1].split(
            "- name: Add the one-time Elasticsearch bootstrap node list", 1
        )[0]
        self.assertNotIn("cluster.initial_master_nodes", statefulset)

    def test_stateful_storage_growth_orphans_controllers_but_preserves_claims(self):
        self.assertIn("Reject Elasticsearch master storage shrink attempts", self.content)
        self.assertIn("Reject Elasticsearch data storage shrink attempts", self.content)
        self.assertEqual(self.content.count("propagationPolicy: Orphan"), 2)
        self.assertIn("state: patched", self.content)
        self.assertIn("data-es-master-{{ item }}", self.content)
        self.assertIn("data-es-data-{{ item }}", self.content)


class TestNoLicenseFiles(unittest.TestCase):
    """Unit tests: ensure no license artifact files exist."""

    def test_no_platinum_license_json(self):
        path = os.path.join(ROLE_DIR, "files", "platinum_license.json")
        self.assertFalse(os.path.exists(path),
                         "platinum_license.json must not exist")

    def test_no_gold_license_json(self):
        path = os.path.join(ROLE_DIR, "files", "gold_license.json")
        self.assertFalse(os.path.exists(path),
                         "gold_license.json must not exist")

    def test_no_trial_license_json(self):
        path = os.path.join(ROLE_DIR, "files", "trial_license.json")
        self.assertFalse(os.path.exists(path),
                         "trial_license.json must not exist")

    def test_files_dir_no_license_artifacts(self):
        files_dir = os.path.join(ROLE_DIR, "files")
        if os.path.isdir(files_dir):
            for fname in os.listdir(files_dir):
                self.assertNotIn("license", fname.lower(),
                                 f"files/ must not contain license artifact: {fname}")
                self.assertNotIn("platinum", fname.lower(),
                                 f"files/ must not contain platinum artifact: {fname}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
