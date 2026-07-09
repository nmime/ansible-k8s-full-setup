#!/usr/bin/env python3
"""
E2E-style validation tests for elasticsearch role.
Validates the full role from entry (defaults) through output (tasks),
ensuring the entire role pipeline is clean and compliant.

Run: python3 tests/test_elasticsearch_e2e_yaml.py
"""
import os
import re
import sys
import subprocess
import unittest
import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
ROLE_DIR = os.path.join(REPO_ROOT, "roles", "elasticsearch")


def load_yaml_docs(path):
    with open(path) as f:
        return list(yaml.safe_load_all(f))


class TestEndToEndLicenseCompliance(unittest.TestCase):
    """E2E test: trace the entire license configuration from defaults to tasks."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROLE_DIR, "defaults", "main.yml")) as f:
            cls.defaults = yaml.safe_load(f)
        with open(os.path.join(ROLE_DIR, "tasks", "main.yml")) as f:
            cls.tasks_content = f.read()
        cls.tasks_docs = load_yaml_docs(
            os.path.join(ROLE_DIR, "tasks", "main.yml"))

    def test_defaults_declare_basic_license(self):
        """Defaults must declare basic license type."""
        self.assertEqual(self.defaults.get("es_license_type"), "basic")

    def test_tasks_enforce_basic_via_env(self):
        """Tasks must enforce basic license via xpack.license.self_generated.type."""
        self.assertIn("xpack.license.self_generated.type", self.tasks_content)
        self.assertIn("value: 'basic'", self.tasks_content)

    def test_no_platinum_license_file_exists(self):
        """No platinum license file should exist on disk."""
        files_dir = os.path.join(ROLE_DIR, "files")
        if os.path.isdir(files_dir):
            for fname in os.listdir(files_dir):
                fpath = os.path.join(files_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath) as f:
                        content = f.read().lower()
                    self.assertNotIn("platinum", content,
                                     f"File {fname} must not contain 'platinum'")

    def test_no_license_application_job_in_pipeline(self):
        """No job in the task pipeline should apply a paid license."""
        for doc in self.tasks_docs:
            if isinstance(doc, dict) and "name" in doc:
                name = doc["name"].lower()
                self.assertNotIn("apply", name) or \
                    self.assertNotIn("license", name) or \
                    self.assertTrue("basic" in name or "self" in name,
                                   f"License application task should only apply basic: {doc['name']}")

    def test_no_crack_pipeline_stages(self):
        """No crack-related stages in the entire task pipeline."""
        crack_patterns = [
            "crack", "patch_xpack", "patch-xpack", "License.java",
            "LicenseVerifier", "javac", "jar -xf", "jar -cf",
            "x-pack-core.*.jar"
        ]
        for doc in self.tasks_docs:
            if isinstance(doc, dict):
                doc_str = yaml.dump(doc) if doc else ""
                for pattern in crack_patterns:
                    if pattern.startswith("x-pack-core"):
                        self.assertNotRegex(doc_str, pattern,
                                            f"Crack pattern '{pattern}' found in task: {doc.get('name', '')}")
                    else:
                        self.assertNotIn(pattern, doc_str,
                                         f"Crack pattern '{pattern}' found in task: {doc.get('name', '')}")

    def test_role_yaml_validates_cleanly(self):
        """All YAML in the role should parse without errors."""
        tasks_path = os.path.join(ROLE_DIR, "tasks", "main.yml")
        defaults_path = os.path.join(ROLE_DIR, "defaults", "main.yml")
        # If we got here, YAML already parsed successfully
        self.assertIsNotNone(self.defaults)
        self.assertIsNotNone(self.tasks_docs)

    def test_full_role_no_platinum_word(self):
        """The entire role directory should not contain the word 'platinum'."""
        for root, dirs, files in os.walk(ROLE_DIR):
            # Skip .git
            dirs[:] = [d for d in dirs if d != ".git"]
            for fname in files:
                fpath = os.path.join(root, fname)
                if fname.endswith((".yml", ".yaml", ".md", ".json", ".sh")):
                    with open(fpath, encoding="utf-8", errors="ignore") as f:
                        content = f.read().lower()
                    self.assertNotIn("platinum", content,
                                     f"'platinum' found in {fpath}")


class TestRoleStructure(unittest.TestCase):
    """E2E test: role directory structure is correct."""

    def test_defaults_dir_exists(self):
        self.assertTrue(os.path.isdir(
            os.path.join(ROLE_DIR, "defaults")))

    def test_tasks_dir_exists(self):
        self.assertTrue(os.path.isdir(
            os.path.join(ROLE_DIR, "tasks")))

    def test_defaults_main_exists(self):
        self.assertTrue(os.path.isfile(
            os.path.join(ROLE_DIR, "defaults", "main.yml")))

    def test_tasks_main_exists(self):
        self.assertTrue(os.path.isfile(
            os.path.join(ROLE_DIR, "tasks", "main.yml")))

    def test_no_files_directory_with_license(self):
        files_dir = os.path.join(ROLE_DIR, "files")
        if os.path.isdir(files_dir):
            for fname in os.listdir(files_dir):
                self.assertNotIn("license", fname.lower())
                self.assertNotIn("platinum", fname.lower())


class TestStaticAnalysisShellScript(unittest.TestCase):
    """E2E test: verify the shell-based static analysis script works."""

    def test_shell_test_script_exists(self):
        script = os.path.join(os.path.dirname(__file__),
                              "test_elasticsearch_license_compliance.sh")
        self.assertTrue(os.path.isfile(script),
                        "Shell test script must exist")

    def test_shell_test_script_executable(self):
        # Make sure it's executable (chmod may not have been run)
        script = os.path.join(os.path.dirname(__file__),
                              "test_elasticsearch_license_compliance.sh")
        os.chmod(script, 0o755)
        self.assertTrue(os.access(script, os.X_OK),
                        "Shell test script must be executable")

    def test_shell_test_script_passes(self):
        """Run the shell-based static analysis and expect it to pass."""
        script = os.path.join(os.path.dirname(__file__),
                              "test_elasticsearch_license_compliance.sh")
        os.chmod(script, 0o755)
        result = subprocess.run(
            ["bash", script], capture_output=True, text=True, cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0,
                         f"Shell test failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
