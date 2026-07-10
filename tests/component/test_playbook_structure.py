"""Component tests: validate playbooks, roles, and their interconnections."""

import os
import re
import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS_DIR = os.path.join(REPO_ROOT, "playbooks")
ROLES_DIR = os.path.join(REPO_ROOT, "roles")
DEFAULTS_PATH = os.path.join(REPO_ROOT, "defaults", "main.yml")
REQUIREMENTS_YML = os.path.join(REPO_ROOT, "requirements.yml")


def read(path):
    with open(path) as f:
        return f.read()


def get_role_names():
    """Get list of role directory names."""
    if not os.path.isdir(ROLES_DIR):
        return []
    return [d for d in os.listdir(ROLES_DIR) if os.path.isdir(os.path.join(ROLES_DIR, d))]


# ── Playbook structure ────────────────────────────────────────────

class TestPlaybookStructure:
    """Component: playbooks are valid YAML and have required structure."""

    @pytest.fixture(autouse=True)
    def _load_playbooks(self):
        self.playbooks = {}
        for fname in os.listdir(PLAYBOOKS_DIR):
            if fname.endswith(".yml") or fname.endswith(".yaml"):
                fpath = os.path.join(PLAYBOOKS_DIR, fname)
                with open(fpath) as f:
                    self.playbooks[fname] = yaml.safe_load(f)

    @pytest.mark.component
    def test_deploy_platform_is_list(self):
        content = self.playbooks.get("deploy_platform.yml")
        assert isinstance(content, list), "Playbook should be a YAML list"

    @pytest.mark.component
    def test_deploy_platform_has_name(self):
        play = self.playbooks.get("deploy_platform.yml", [{}])[0]
        assert "name" in play, "Playbook should have a name"

    @pytest.mark.component
    def test_deploy_platform_hosts_localhost(self):
        play = self.playbooks.get("deploy_platform.yml", [{}])[0]
        assert play.get("hosts") == "localhost"

    @pytest.mark.component
    def test_deploy_platform_loads_defaults(self):
        play = self.playbooks.get("deploy_platform.yml", [{}])[0]
        vars_files = play.get("vars_files", [])
        assert any("defaults/main.yml" in str(v) for v in vars_files)


class TestRoleStructure:
    """Component: each role has required files."""

    @pytest.fixture(autouse=True)
    def _roles(self):
        self.roles = get_role_names()

    @pytest.mark.component
    def test_all_roles_have_tasks_main(self):
        for role in self.roles:
            tasks_path = os.path.join(ROLES_DIR, role, "tasks", "main.yml")
            assert os.path.isfile(tasks_path), \
                f"Role '{role}' missing tasks/main.yml"

    @pytest.mark.component
    def test_roles_with_defaults_have_main(self):
        for role in self.roles:
            defaults_dir = os.path.join(ROLES_DIR, role, "defaults")
            if os.path.isdir(defaults_dir):
                defaults_path = os.path.join(defaults_dir, "main.yml")
                assert os.path.isfile(defaults_path), \
                    f"Role '{role}' has defaults/ but no defaults/main.yml"

    @pytest.mark.component
    def test_role_names_are_snake_case(self):
        snake_case = re.compile(r"^[a-z][a-z0-9_-]*$")
        for role in self.roles:
            assert snake_case.match(role), f"Role name '{role}' is not snake_case"


class TestPlaybookRoleReferences:
    """Component: every role imported in playbook actually exists."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        with open(os.path.join(PLAYBOOKS_DIR, "deploy_platform.yml")) as f:
            self.playbook_content = f.read()
        self.role_names = set(get_role_names())

    @pytest.mark.component
    def test_all_imported_roles_exist(self):
        """Every import_role in deploy_platform.yml points to an existing role dir."""
        # Only match lines within import_role blocks: "  name: role-name"
        role_refs = []
        lines = self.playbook_content.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if "import_role:" in stripped:
                # Check next line for "name: role-name"
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    m = re.match(r"name:\s+(\S+)", next_line)
                    if m:
                        ref = m.group(1)
                        if ref not in ("localhost", "true", "false"):
                            role_refs.append(ref)
        for ref in role_refs:
            assert ref in self.role_names, \
                f"Playbook references role '{ref}' which does not exist in roles/"

    @pytest.mark.component
    def test_generate_secrets_role_exists(self):
        assert "generate-secrets" in self.role_names

    @pytest.mark.component
    def test_hetzner_infra_role_exists(self):
        assert "hetzner-infra" in self.role_names

    @pytest.mark.component
    def test_k8s_cluster_management_role_exists(self):
        assert "k8s-cluster-management" in self.role_names


class TestRequirementsYml:
    """Component: requirements.yml lists expected collections."""

    @pytest.fixture(autouse=True)
    def _load(self):
        with open(REQUIREMENTS_YML) as f:
            self.data = yaml.safe_load(f)

    @pytest.mark.component
    def test_has_collections_key(self):
        assert "collections" in self.data

    @pytest.mark.component
    def test_community_general_collection(self):
        names = [c["name"] for c in self.data.get("collections", [])]
        assert "community.general" in names

    @pytest.mark.component
    def test_kubernetes_core_collection(self):
        names = [c["name"] for c in self.data.get("collections", [])]
        assert "kubernetes.core" in names

    @pytest.mark.component
    def test_all_collections_have_version(self):
        for coll in self.data.get("collections", []):
            assert "version" in coll, f"Collection '{coll['name']}' has no version constraint"


class TestAnsibleCfg:
    """Component: ansible.cfg has expected settings."""

    @pytest.mark.component
    def test_ansible_cfg_exists(self):
        assert os.path.isfile(os.path.join(REPO_ROOT, "ansible.cfg"))

    @pytest.mark.component
    def test_ansible_cfg_has_defaults_section(self):
        cfg = read(os.path.join(REPO_ROOT, "ansible.cfg"))
        assert "[defaults]" in cfg

    @pytest.mark.component
    def test_ansible_cfg_has_ssh_connection(self):
        cfg = read(os.path.join(REPO_ROOT, "ansible.cfg"))
        assert "[ssh_connection]" in cfg

    @pytest.mark.component
    def test_ansible_cfg_has_pipelining(self):
        cfg = read(os.path.join(REPO_ROOT, "ansible.cfg"))
        assert "pipelining = true" in cfg
