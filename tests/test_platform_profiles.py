"""Contracts for named platform profiles and resource-tier propagation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = REPO_ROOT / "platform-orchestrator" / "profiles"

EXPECTED_PROFILE_TIERS = {
    "minimal": ("minimal", "minimal"),
    "small": ("small", "small"),
    "medium": ("medium", "medium"),
    "medium-optimized": ("medium", "small"),
    "production": ("production", "production"),
}

COMPONENT_PATHS = (
    "storage.enabled",
    "secrets.enabled",
    "secrets.eso.enabled",
    "databases.enabled",
    "databases.postgresql.enabled",
    "databases.mongodb.enabled",
    "gitlab.enabled",
    "gitlab.runner.enabled",
    "gitops.enabled",
    "observability.enabled",
    "coroot.enabled",
    "elasticsearch.enabled",
    "autoscaling.enabled",
    "dragonfly.enabled",
    "temporal.enabled",
    "postal.enabled",
    "tracing.enabled",
    "backup.enabled",
    "glitchtip.enabled",
    "apm.enabled",
    "blackbox.enabled",
    "applications.daytona.enabled",
    "compliance.hipaa.enabled",
)

MEDIUM_SERVICE_PATHS = tuple(
    path
    for path in COMPONENT_PATHS
    if path not in {"applications.daytona.enabled", "compliance.hipaa.enabled"}
)
ALERT_CHANNEL_PATHS = (
    "alerting.telegram.enabled",
    "alerting.email.enabled",
)


def load_profile(name: str) -> dict:
    with (PROFILES_DIR / f"{name}.yaml").open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def get_path(data: dict, dotted_path: str):
    value = data
    for key in dotted_path.split("."):
        assert key in value, f"{dotted_path} must be explicit in every named profile"
        value = value[key]
    return value


class TestNamedProfileContract:
    def test_exact_supported_profile_set(self):
        actual = {path.stem for path in PROFILES_DIR.glob("*.yaml")}
        assert actual == set(EXPECTED_PROFILE_TIERS)

    def test_example_inventory_remains_valid_yaml(self):
        with (REPO_ROOT / "inventory.example").open(encoding="utf-8") as stream:
            inventory = yaml.safe_load(stream)
        assert inventory["all"]["vars"]["platform_profile"] == "medium"
        assert inventory["all"]["vars"]["resource_tier"] == "medium"

    def test_custom_example_is_a_complete_valid_selector(self):
        example_path = (
            REPO_ROOT / "platform-orchestrator" / "platform.example.yaml"
        )
        with example_path.open(encoding="utf-8") as stream:
            example = yaml.safe_load(stream)
        for path in COMPONENT_PATHS + ALERT_CHANNEL_PATHS:
            assert isinstance(get_path(example, path), bool)
        result = subprocess.run(
            [
                "ansible-playbook",
                str(REPO_ROOT / "playbooks" / "validate_profile.yml"),
                "-e",
                f"@{example_path}",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_targeted_deploy_applies_profile_normalization_tasks(self):
        playbook = (REPO_ROOT / "playbooks" / "deploy_platform.yml").read_text(encoding="utf-8")
        assert "apply:\n          tags: [always]" in playbook

    def test_legacy_direct_hipaa_variable_remains_compatible(self, tmp_path):
        example_path = (
            REPO_ROOT / "platform-orchestrator" / "platform.example.yaml"
        )
        with example_path.open(encoding="utf-8") as stream:
            legacy = yaml.safe_load(stream)
        legacy.pop("compliance")
        legacy.pop("coroot")
        legacy_path = tmp_path / "legacy-direct-vars.yaml"
        legacy_path.write_text(yaml.safe_dump(legacy), encoding="utf-8")
        result = subprocess.run(
            [
                "ansible-playbook",
                str(REPO_ROOT / "playbooks" / "validate_profile.yml"),
                "-e",
                f"@{legacy_path}",
                "-e",
                "hipaa_compliance=true",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        ("profile_name", "expected_tier", "expected_resource_tier"),
        [
            (name, tiers[0], tiers[1])
            for name, tiers in EXPECTED_PROFILE_TIERS.items()
        ],
    )
    def test_profile_identity_and_tier_mapping(
        self,
        profile_name,
        expected_tier,
        expected_resource_tier,
    ):
        profile = load_profile(profile_name)
        assert profile["platform_profile"] == profile_name
        assert profile["tier"] == expected_tier
        assert profile["resource_tier"] == expected_resource_tier

    @pytest.mark.parametrize("profile_name", EXPECTED_PROFILE_TIERS)
    def test_component_enablement_is_explicit(self, profile_name):
        profile = load_profile(profile_name)
        for path in COMPONENT_PATHS + ALERT_CHANNEL_PATHS:
            assert isinstance(get_path(profile, path), bool)

    @pytest.mark.parametrize("profile_name", EXPECTED_PROFILE_TIERS)
    def test_gitlab_dependencies_are_enabled_together(self, profile_name):
        profile = load_profile(profile_name)
        if profile["gitlab"]["enabled"]:
            assert profile["storage"]["enabled"]
            assert profile["databases"]["postgresql"]["enabled"]
            assert profile["dragonfly"]["enabled"]

    def test_small_workers_meet_the_declared_eight_gib_floor(self):
        assert load_profile("small")["infrastructure"]["workers"]["type"] == "cx33"

    def test_minimal_does_not_enable_gitlab_without_redis(self):
        profile = load_profile("minimal")
        assert profile["gitlab"]["enabled"] is False
        assert profile["dragonfly"]["enabled"] is False

    @pytest.mark.parametrize("profile_name", EXPECTED_PROFILE_TIERS)
    def test_ansible_normalizes_each_named_profile(self, profile_name):
        result = subprocess.run(
            [
                "ansible-playbook",
                str(REPO_ROOT / "playbooks" / "validate_profile.yml"),
                "-e",
                f"@{PROFILES_DIR / f'{profile_name}.yaml'}",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestMediumOptimizedContract:
    @pytest.fixture(autouse=True)
    def _profile(self):
        self.profile = load_profile("medium-optimized")

    def test_keeps_the_complete_medium_service_set(self):
        for path in MEDIUM_SERVICE_PATHS:
            assert get_path(self.profile, path) is True, f"{path} must remain enabled"
        assert get_path(self.profile, "applications.daytona.enabled") is False
        assert get_path(self.profile, "compliance.hipaa.enabled") is False

    def test_retains_critical_quorum_topologies(self):
        assert self.profile["infrastructure"]["control_plane"]["count"] == 3
        assert self.profile["secrets"]["vault"]["replicas"] == 3
        assert self.profile["databases"]["postgresql"]["replicas"] == 3
        assert self.profile["databases"]["mongodb"]["replicas"] == 3
        assert self.profile["storage"]["master_replicas"] == 3
        assert self.profile["storage"]["volume_replicas"] == 3
        assert self.profile["elasticsearch"]["master"]["replicas"] == 3

    def test_uses_compact_stateless_baselines(self):
        replica_paths = (
            "gitlab.webservice_replicas",
            "gitlab.sidekiq_replicas",
            "gitops.server_replicas",
            "gitops.repo_replicas",
            "gitops.controller_replicas",
            "observability.metrics.replicas",
            "observability.grafana.replicas",
            "autoscaling.replicas",
            "dragonfly.replicas",
            "temporal.frontend_replicas",
            "temporal.history_replicas",
            "temporal.matching_replicas",
            "temporal.worker_replicas",
            "temporal.web_replicas",
            "postal.web_replicas",
            "postal.smtp_replicas",
            "postal.worker_replicas",
            "apm.replicas",
            "blackbox.replicas",
        )
        assert all(get_path(self.profile, path) == 1 for path in replica_paths)
        assert self.profile["autoscaling"]["defaults"] == {
            "min_replicas": 1,
            "max_replicas": 4,
        }

    def test_bounds_storage_and_retention_for_the_small_envelope(self):
        assert self.profile["storage"]["size_per_replica"] == "40Gi"
        assert self.profile["databases"]["postgresql"]["storage_size"] == "30Gi"
        assert self.profile["databases"]["mongodb"]["storage_size"] == "20Gi"
        assert self.profile["observability"]["metrics"]["storage_size"] == "40Gi"
        assert self.profile["observability"]["metrics"]["retention"] == "14d"
        assert self.profile["observability"]["logging"]["retention"] == "7d"
        assert self.profile["tracing"]["retention"] == "24h"
        assert self.profile["coroot"]["storage_size"] == "10Gi"
        assert self.profile["coroot"]["clickhouse"]["storage_size"] == "20Gi"
        assert self.profile["coroot"]["clickhouse"]["resources"] == {
            "cpu_request": "250m",
            "cpu_limit": "1",
            "memory_request": "512Mi",
            "memory_limit": "2Gi",
        }
        assert self.profile["coroot"]["resources"] == {
            "cpu_request": "100m",
            "cpu_limit": "500m",
            "memory_request": "512Mi",
            "memory_limit": "1Gi",
        }

    def test_profile_init_preserves_the_contract(self, tmp_path):
        orchestrator = tmp_path / "platform-orchestrator"
        shutil.copytree(REPO_ROOT / "platform-orchestrator", orchestrator)
        result = subprocess.run(
            ["bash", "platform.sh", "init", "medium-optimized"],
            cwd=orchestrator,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        with (orchestrator / "platform.yaml").open(encoding="utf-8") as stream:
            generated = yaml.safe_load(stream)
        assert generated["platform_profile"] == "medium-optimized"
        assert generated["tier"] == "medium"
        assert generated["resource_tier"] == "small"


class TestResourceTierConsumers:
    @pytest.mark.parametrize(
        "relative_path",
        (
            "roles/apm-server/defaults/main.yml",
            "roles/dragonfly/defaults/main.yml",
            "roles/elasticsearch/defaults/main.yml",
            "roles/glitchtip/defaults/main.yml",
            "roles/gitlab-selfhosted/tasks/main.yml",
            "roles/k8s-autoscaling/tasks/main.yml",
            "roles/k8s-cluster-management/tasks/main.yml",
            "roles/k8s-databases/tasks/main.yml",
            "roles/k8s-gitops/tasks/main.yml",
            "roles/k8s-observability/tasks/main.yml",
            "roles/k8s-observability/tasks/tracing.yml",
            "roles/k8s-observability/tasks/coroot.yml",
            "roles/k8s-secrets/tasks/main.yml",
            "roles/object-storage/defaults/main.yml",
            "roles/postal/defaults/main.yml",
            "roles/postal/tasks/main.yml",
            "roles/temporal/tasks/main.yml",
        ),
    )
    def test_resource_sized_roles_consume_resource_tier(self, relative_path):
        content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "resource_tier" in content

    def test_orchestrator_rejects_a_malformed_medium_optimized_contract(self):
        content = (
            REPO_ROOT / "platform-orchestrator" / "platform.sh"
        ).read_text(encoding="utf-8")
        assert "medium-optimized)" in content
        assert 'TIER" != "medium"' in content
        assert 'RESOURCE_TIER" != "small"' in content
        assert '-e "platform_profile=${PROFILE}"' in content
        assert '-e "resource_tier=${RESOURCE_TIER}"' in content

    def test_ansible_normalization_enforces_profile_contract(self):
        content = (
            REPO_ROOT / "playbooks" / "tasks" / "normalize_profile.yml"
        ).read_text(encoding="utf-8")
        assert "Enforce the selected named profile mapping" in content
        assert "medium-optimized: {tier: medium, resource_tier: small}" in content
        assert "production: {tier: production, resource_tier: production}" in content

    def test_explicit_server_types_are_capacity_checked(self):
        content = (
            REPO_ROOT / "roles" / "hetzner-infra" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        assert "Reject undersized explicit or auto-selected server types" in content
        assert "selected_cp_spec.cores" in content
        assert "selected_worker_spec.memory" in content

    def test_dns_capability_is_checked_before_provisioning(self):
        content = (
            REPO_ROOT / "playbooks" / "deploy_platform.yml"
        ).read_text(encoding="utf-8")
        assert "hcloud zone --help" in content
        assert "hetzner_manage_dns=false" in content

    def test_bastion_hcloud_install_enforces_the_pinned_version(self):
        content = (
            REPO_ROOT / "roles" / "network-security" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        assert 'hcloud version 2>/dev/null' in content
        assert '!= "${HCLOUD_VER}"' in content

    def test_only_canonical_hyphenated_profile_name_is_used(self):
        profile_files = list(PROFILES_DIR.glob("*.yaml"))
        content = "\n".join(path.read_text(encoding="utf-8") for path in profile_files)
        assert "medium_optimized" not in content

    def test_tracing_has_one_canonical_helm_implementation(self):
        main = (
            REPO_ROOT / "roles" / "k8s-observability" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        tracing = (
            REPO_ROOT / "roles" / "k8s-observability" / "tasks" / "tracing.yml"
        ).read_text(encoding="utf-8")
        assert "grafana/tempo" not in main
        assert "opentelemetry-collector" not in main
        assert tracing.count("chart_ref: grafana/tempo") == 1
        assert tracing.count("chart_ref: open-telemetry/opentelemetry-collector") == 1
        assert "create-tempo-bucket" in tracing
        assert "grafana-tempo-datasource" in tracing


class TestComponentLifecycle:
    def test_targeted_runs_always_normalize_the_profile(self):
        content = (REPO_ROOT / "playbooks" / "deploy_platform.yml").read_text(
            encoding="utf-8"
        )
        normalization = content.split(
            "- name: Normalize the selected platform profile", maxsplit=1
        )[1].split("- name:", maxsplit=1)[0]
        assert "tags: [always]" in normalization
        assert "tags: [databases, postgresql, mongodb]" in content

    def test_eso_and_runner_flags_are_consumed_by_their_roles(self):
        secrets = (
            REPO_ROOT / "roles" / "k8s-secrets" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        gitlab = (
            REPO_ROOT / "roles" / "gitlab-selfhosted" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        assert secrets.count("when: eso_enabled | bool") >= 6
        assert "gitlab_runner_enabled | bool" in gitlab

    def test_blackbox_only_probes_enabled_services(self):
        blackbox = (
            REPO_ROOT / "roles" / "blackbox-exporter" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        for normalized_flag in (
            "platform_postgresql_enabled",
            "platform_elasticsearch_enabled",
            "platform_dragonfly_enabled",
            "platform_object_storage_enabled",
            "platform_gitlab_enabled",
            "platform_gitops_enabled",
            "platform_postal_enabled",
            "platform_temporal_enabled",
        ):
            assert normalized_flag in blackbox
        assert 'targets: "{{ bb_internal_targets }}"' in blackbox

    def test_alert_delivery_routes_obey_channel_flags(self):
        alerting_path = (
            REPO_ROOT / "roles" / "k8s-observability" / "tasks" / "alerting.yml"
        )
        alerting = alerting_path.read_text(encoding="utf-8")
        assert "alert_telegram_configured" in alerting
        assert "alert_email_configured" in alerting
        assert "{% if alert_telegram_configured | bool %}" in alerting
        assert "{% if alert_email_configured | bool %}" in alerting

        tasks = yaml.safe_load(alerting)
        config_task = next(
            task
            for task in tasks
            if task.get("name") == "Create VMAlertmanager config secret"
        )
        template = config_task["kubernetes.core.k8s"]["definition"]["stringData"][
            "alertmanager.yaml"
        ]
        environment = Environment(trim_blocks=True)
        environment.filters["bool"] = bool
        for telegram_enabled, email_enabled in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            rendered = environment.from_string(template).render(
                alert_telegram_configured=telegram_enabled,
                alert_email_configured=email_enabled,
                alert_telegram_chat_id=-100123456,
                alert_email_to="ops@example.com",
                domain="example.com",
            )
            parsed = yaml.safe_load(rendered)
            assert isinstance(parsed["route"]["routes"], list)
            receiver_names = {
                receiver["name"] for receiver in parsed["receivers"]
            }
            assert ("telegram-critical" in receiver_names) is telegram_enabled
            assert ("email-warning" in receiver_names) is email_enabled

    def test_orchestrator_exposes_every_selectable_component(self, tmp_path):
        orchestrator = tmp_path / "platform-orchestrator"
        shutil.copytree(REPO_ROOT / "platform-orchestrator", orchestrator)
        shutil.copytree(REPO_ROOT / "playbooks", tmp_path / "playbooks")
        shutil.copytree(REPO_ROOT / "defaults", tmp_path / "defaults")
        shutil.copy(
            orchestrator / "platform.example.yaml", orchestrator / "platform.yaml"
        )
        result = subprocess.run(
            ["bash", "platform.sh", "components"],
            cwd=orchestrator,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        for component in (
            "object-storage",
            "eso",
            "postgresql",
            "mongodb",
            "elasticsearch",
            "dragonfly",
            "gitlab-runner",
            "tracing",
            "temporal",
            "postal",
            "backup",
            "glitchtip",
            "apm",
            "blackbox",
            "daytona",
            "coroot",
            "hipaa",
        ):
            assert component in result.stdout

    @pytest.mark.parametrize(
        ("component", "enabled_paths"),
        (
            ("coroot", ("coroot.enabled", "observability.enabled")),
            (
                "hipaa",
                (
                    "compliance.hipaa.enabled",
                    "secrets.enabled",
                    "observability.enabled",
                ),
            ),
        ),
    )
    def test_orchestrator_can_enable_late_added_components(
        self,
        tmp_path,
        component,
        enabled_paths,
    ):
        orchestrator = tmp_path / "platform-orchestrator"
        shutil.copytree(REPO_ROOT / "platform-orchestrator", orchestrator)
        shutil.copytree(REPO_ROOT / "playbooks", tmp_path / "playbooks")
        shutil.copytree(REPO_ROOT / "defaults", tmp_path / "defaults")
        shutil.copy(
            orchestrator / "platform.example.yaml", orchestrator / "platform.yaml"
        )
        result = subprocess.run(
            ["bash", "platform.sh", "enable", component],
            cwd=orchestrator,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        with (orchestrator / "platform.yaml").open(encoding="utf-8") as stream:
            profile = yaml.safe_load(stream)
        assert all(get_path(profile, path) is True for path in enabled_paths)

    def test_observability_cannot_be_disabled_while_coroot_is_selected(
        self,
        tmp_path,
    ):
        orchestrator = tmp_path / "platform-orchestrator"
        shutil.copytree(REPO_ROOT / "platform-orchestrator", orchestrator)
        profile = load_profile("medium")
        with (orchestrator / "platform.yaml").open("w", encoding="utf-8") as stream:
            yaml.safe_dump(profile, stream)
        result = subprocess.run(
            ["bash", "platform.sh", "disable", "observability"],
            cwd=orchestrator,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0
        assert "coroot" in result.stdout + result.stderr

    @pytest.mark.parametrize(
        ("enabled_path", "disabled_path", "expected_message"),
        (
            ("gitlab.enabled", "dragonfly.enabled", "GitLab chart 10 requires"),
            ("glitchtip.enabled", "dragonfly.enabled", "GlitchTip requires"),
            ("apm.enabled", "elasticsearch.enabled", "APM Server requires"),
            ("temporal.enabled", "elasticsearch.enabled", "Temporal requires"),
            ("postal.enabled", "dragonfly.enabled", "Postal requires"),
            ("tracing.enabled", "storage.enabled", "Tempo tracing requires"),
            ("backup.enabled", "storage.enabled", "Backup automation requires"),
            ("alerting.email.enabled", "postal.enabled", "Email alerting requires"),
            ("coroot.enabled", "observability.enabled", "Coroot require observability"),
            ("compliance.hipaa.enabled", "secrets.enabled", "HIPAA-oriented controls require"),
        ),
    )
    def test_invalid_dependency_combinations_fail_offline_validation(
        self,
        tmp_path,
        enabled_path,
        disabled_path,
        expected_message,
    ):
        profile = load_profile("medium")
        # Isolate the dependency under test so the expected validation rule is
        # the first and only failing consumer.
        profile["gitlab"]["enabled"] = False
        profile["gitlab"]["runner"]["enabled"] = False
        profile["glitchtip"]["enabled"] = False
        profile["apm"]["enabled"] = False
        profile["temporal"]["enabled"] = False
        profile["postal"]["enabled"] = False
        profile["tracing"]["enabled"] = False
        profile["backup"]["enabled"] = False
        profile["coroot"]["enabled"] = False
        profile["blackbox"]["enabled"] = False
        profile["compliance"]["hipaa"]["enabled"] = False
        profile["secrets"]["eso"]["enabled"] = False
        profile["alerting"]["email"]["enabled"] = False
        enabled_parent, enabled_key = enabled_path.rsplit(".", maxsplit=1)
        disabled_parent, disabled_key = disabled_path.rsplit(".", maxsplit=1)

        def resolve_parent(data, dotted_path):
            for key in dotted_path.split("."):
                data = data[key]
            return data

        resolve_parent(profile, enabled_parent)[enabled_key] = True
        resolve_parent(profile, disabled_parent)[disabled_key] = False
        invalid_profile = tmp_path / "invalid-profile.yaml"
        invalid_profile.write_text(yaml.safe_dump(profile), encoding="utf-8")
        result = subprocess.run(
            [
                "ansible-playbook",
                str(REPO_ROOT / "playbooks" / "validate_profile.yml"),
                "-e",
                f"@{invalid_profile}",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert expected_message in result.stdout + result.stderr

    def test_removal_workflow_is_confirmation_and_data_guarded(self):
        playbook = (REPO_ROOT / "playbooks" / "remove_component.yml").read_text(
            encoding="utf-8"
        )
        assert "confirm_component_removal | default('') == target_component" in playbook
        assert "Require explicit permission for data-bearing components" in playbook
        assert "Refuse implicit PVC deletion" in playbook
        assert "Refuse removal while the component is selected" in playbook
        assert "delete_component_data | bool" in playbook
        assert "Remote\n          object-storage backup and tracing buckets are intentionally retained" in playbook

    def test_coroot_uses_pinned_official_operator_and_external_metrics(self):
        defaults = (REPO_ROOT / "defaults" / "main.yml").read_text(encoding="utf-8")
        coroot = (
            REPO_ROOT / "roles" / "k8s-observability" / "tasks" / "coroot.yml"
        ).read_text(encoding="utf-8")
        assert "coroot_operator_chart_version: \"0.9.7\"" in defaults
        assert "coroot_chart_version: \"0.3.3\"" in defaults
        assert "coroot_image_tag: \"1.23.3\"" in defaults
        assert "repo_url: https://coroot.github.io/helm-charts" in coroot
        assert "chart_ref: coroot/coroot-operator" in coroot
        assert "chart_ref: coroot/coroot-ce" in coroot
        assert "externalPrometheus:" in coroot
        assert "pod-security.kubernetes.io/enforce: privileged" in coroot
        assert "denyGlobalSecrets: true" in coroot
        assert "Create VPN-only Coroot HTTPRoute" in coroot
        assert "name: admin-gateway" in coroot
        assert "latest" not in coroot
        assert "Wait for the Coroot node agent DaemonSet rollout" in coroot

    def test_hipaa_redaction_is_wired_into_every_log_collector(self):
        observability = (
            REPO_ROOT / "roles" / "k8s-observability" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        hipaa = (
            REPO_ROOT / "roles" / "hipaa-hardening" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        assert "regulated-data-redaction" in observability
        assert "pipelineStages" in observability
        assert "record_transformer" in observability
        assert observability.count("[REDACTED_EMAIL]") >= 3
        assert "promtail-redaction" not in hipaa
        assert "Cilium transparent pod-network encryption required" in hipaa

    def test_catalog_and_deployment_guide_cover_every_lifecycle_component(self):
        catalog = (
            REPO_ROOT / "docs" / "TECHNOLOGY_CATALOG.md"
        ).read_text(encoding="utf-8")
        deployment = (REPO_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
        components = (
            "object-storage",
            "secrets",
            "eso",
            "databases",
            "postgresql",
            "mongodb",
            "elasticsearch",
            "dragonfly",
            "gitlab",
            "gitlab-runner",
            "gitops",
            "observability",
            "coroot",
            "tracing",
            "autoscaling",
            "temporal",
            "postal",
            "backup",
            "glitchtip",
            "apm",
            "blackbox",
            "daytona",
            "hipaa",
        )
        for component in components:
            assert component in catalog
            assert f"./platform.sh deploy {component}" in deployment
        for foundation in ("infra", "network", "dns", "cluster", "tls"):
            assert f"./platform.sh deploy {foundation}" in deployment

    def test_vault_upgrade_plan_does_not_claim_kubernetes_auto_unseal(self):
        plan = (
            REPO_ROOT / "docs" / "VAULT_UPGRADE_PLAN.md"
        ).read_text(encoding="utf-8")
        assert "auto-unseal mechanism (Kubernetes secrets)" not in plan
        assert "verify keys in K8s secrets" not in plan
        assert "wait for re-election and auto-unseal" not in plan.lower()
