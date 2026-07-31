"""Security and lifecycle contracts for optional Umami analytics."""

import hashlib
import json
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROOT / "roles/umami/defaults/main.yml").read_text())
TASKS = (ROOT / "roles/umami/tasks/main.yml").read_text()
RESOURCES = (ROOT / "roles/umami/templates/resources.yml.j2").read_text()
NORMALIZE = (ROOT / "playbooks/tasks/normalize_profile.yml").read_text()
DEPLOY = (ROOT / "playbooks/deploy_platform.yml").read_text()
REMOVE = (ROOT / "playbooks/remove_component.yml").read_text()
DATABASES = (ROOT / "roles/k8s-databases/tasks/main.yml").read_text()
ORCHESTRATOR = (ROOT / "platform-orchestrator/platform.sh").read_text()


def render_bootstrap_job_name(
    *, password: str = "secret", image: str = "umami@sha256:one", revision: str = "2"
) -> str:
    tasks = yaml.safe_load(TASKS)
    name_task = next(
        task
        for task in tasks
        if task.get("name")
        == "Compute the credential- and template-bound Umami bootstrap job name"
    )
    environment = Environment()
    environment.filters["hash"] = lambda value, algorithm: hashlib.new(
        algorithm, str(value).encode()
    ).hexdigest()
    environment.filters["bool"] = bool
    return environment.from_string(
        name_task["ansible.builtin.set_fact"]["umami_bootstrap_job_name"]
    ).render(
        umami_admin_password=password,
        umami_manage_runtime_secret=True,
        umami_existing_runtime_secret={"resources": []},
        umami_image=image,
        umami_bootstrap_revision=revision,
    ).strip()


def render_postgresql_users(*, umami_enabled: bool) -> list[dict]:
    database_tasks = yaml.safe_load(DATABASES)
    create_cluster = next(
        task
        for task in database_tasks
        if task.get("name") == "Create PostgreSQL cluster (PG Operator 3.x — v2 API)"
    )
    users_template = create_cluster["kubernetes.core.k8s"]["definition"]["spec"][
        "users"
    ]
    environment = Environment()
    environment.filters["bool"] = bool
    rendered = environment.from_string(users_template).render(
        project_name="n0xeid",
        app_name="app",
        platform_umami_enabled=umami_enabled,
        databases={
            "postgresql": {
                "extra_users": [
                    {
                        "operator": {
                            "name": "metabase",
                            "databases": ["metabase"],
                        }
                    }
                ]
            }
        },
    )
    return yaml.safe_load(rendered)


def render_postgresql_role_search_paths(*, umami_enabled: bool) -> list[dict]:
    database_tasks = yaml.safe_load(DATABASES)
    build_settings = next(
        task
        for task in database_tasks
        if task.get("name") == "Build PostgreSQL role search path settings"
    )
    environment = Environment()
    environment.filters["bool"] = bool
    rendered = environment.from_string(
        build_settings["ansible.builtin.set_fact"]["_pg_role_search_paths"]
    ).render(
        platform_umami_enabled=umami_enabled,
        databases={
            "postgresql": {
                "extra_users": [
                    {
                        "operator": {"name": "metabase"},
                        "search_path": "public",
                    },
                    {"operator": {"name": "without-override"}},
                ]
            }
        },
    )
    return yaml.safe_load(rendered)


def render_resources(*, replicas: int, hpa_enabled: bool) -> list[dict]:
    environment = Environment(
        loader=FileSystemLoader(ROOT / "roles/umami/templates"),
        keep_trailing_newline=True,
    )
    environment.filters["bool"] = bool
    environment.filters["to_json"] = json.dumps
    rendered = environment.get_template("resources.yml.j2").render(
        umami_ns="umami",
        umami_replicas=replicas,
        umami_hpa_enabled=hpa_enabled,
        umami_hpa_min_replicas=max(replicas, 1),
        umami_hpa_max_replicas=4,
        umami_hpa_cpu_target=70,
        umami_image="ghcr.io/umami-software/umami:3.2.0@sha256:" + "a" * 64,
        umami_runtime_secret_name="umami-runtime",
        umami_cpu_request="100m",
        umami_cpu_limit="750m",
        umami_memory_request="256Mi",
        umami_memory_limit="768Mi",
        umami_database_namespace="databases",
        umami_database_cluster="n0xeid-pg",
        umami_database_service_alias="n0xeid-pg",
        umami_admin_gateway_name="admin-gateway",
        umami_main_gateway_name="main-gateway",
        umami_gateway_namespace="cilium-system",
        umami_dashboard_domain="umami.example.com",
        umami_ingest_domain="analytics.example.com",
        umami_bootstrap_job_name="umami-bootstrap-123456789abc",
        umami_websites=[
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "name": "UNO",
                "domain": "uno.example.com",
            }
        ],
    )
    return list(yaml.safe_load_all(rendered))


def test_umami_is_explicitly_opt_in_in_every_profile():
    for profile_path in (ROOT / "platform-orchestrator/profiles").glob("*.yaml"):
        profile = yaml.safe_load(profile_path.read_text())
        assert profile["umami"]["enabled"] is False, profile_path

    example = yaml.safe_load(
        (ROOT / "platform-orchestrator/platform.example.yaml").read_text()
    )
    assert example["umami"]["enabled"] is False
    assert "deploy_umami: false" in (ROOT / "defaults/main.yml").read_text()
    assert "deploy_umami: false" in (ROOT / "inventory.example").read_text()


def test_umami_uses_an_immutable_upstream_release():
    image = DEFAULTS["umami_image"]
    assert "umami-software/umami:3.2.0@sha256:" in image
    assert not image.endswith(":latest")


def test_umami_reuses_postgresql_with_a_dedicated_principal():
    assert "platform_umami_enabled" in NORMALIZE
    assert "platform_postgresql_enabled | bool" in NORMALIZE
    assert "'name': 'umami'" in DATABASES
    assert "'grantPublicSchemaAccess': true" in DATABASES
    assert "if platform_umami_enabled | default(false) | bool" in DATABASES
    assert "pguser-{{ umami_database_user }}" in TASKS
    assert "postgresql://" in TASKS
    assert "'?sslmode='" in TASKS
    assert "umami_database_sslmode | urlencode" in TASKS
    assert DEFAULTS["umami_database_sslmode"] == "verify-full"
    assert "'database-client-ca'" in TASKS
    assert "ca.crt" in TASKS
    assert "NODE_EXTRA_CA_CERTS" in RESOURCES


def test_postgresql_users_template_renders_with_and_without_umami():
    enabled_users = render_postgresql_users(umami_enabled=True)
    disabled_users = render_postgresql_users(umami_enabled=False)

    umami_user = {
        "name": "umami",
        "databases": ["umami"],
        "grantPublicSchemaAccess": True,
    }
    assert umami_user in enabled_users
    assert umami_user not in disabled_users
    assert {"name": "metabase", "databases": ["metabase"]} in enabled_users
    assert {"name": "metabase", "databases": ["metabase"]} in disabled_users
    assert len(enabled_users) == len(disabled_users) + 1


def test_umami_postgresql_role_always_uses_the_public_schema():
    enabled_settings = render_postgresql_role_search_paths(umami_enabled=True)
    disabled_settings = render_postgresql_role_search_paths(umami_enabled=False)
    umami_setting = {
        "operator": {"name": "umami"},
        "search_path": "public",
    }
    metabase_setting = {
        "operator": {"name": "metabase"},
        "search_path": "public",
    }

    assert umami_setting in enabled_settings
    assert umami_setting not in disabled_settings
    assert metabase_setting in enabled_settings
    assert metabase_setting in disabled_settings
    assert all(item["operator"]["name"] != "without-override" for item in enabled_settings)
    assert "_pg_role_search_paths | default([])" in DATABASES


def test_umami_secret_rotation_is_idempotent_and_never_uses_default_in_runtime():
    assert "umami_existing_runtime_secret" in TASKS
    assert "lookup('password', '/dev/null" in TASKS
    assert "ADMIN_PASSWORD" in TASKS
    assert "umami-bootstrap-" in TASKS
    assert "session = await login('umami')" in RESOURCES
    assert "body: JSON.stringify({password: desired})" in RESOURCES
    assert "value: umami" not in RESOURCES


def test_umami_bootstrap_job_is_resumable_across_credentials_and_templates():
    baseline = render_bootstrap_job_name()
    assert baseline.startswith("umami-bootstrap-")
    assert baseline != render_bootstrap_job_name(password="rotated")
    assert baseline != render_bootstrap_job_name(image="umami@sha256:two")
    assert baseline != render_bootstrap_job_name(revision="3")
    assert "Discover stale Umami bootstrap jobs" in TASKS
    assert "Remove stale Umami bootstrap jobs after successful replacement" in TASKS
    assert "item.metadata.name != umami_bootstrap_job_name" in TASKS


def test_umami_bootstrap_starts_only_after_the_deployment_is_ready():
    tasks = yaml.safe_load(TASKS)
    task_names = [task["name"] for task in tasks]
    runtime_task = next(
        task
        for task in tasks
        if task["name"] == "Reconcile Umami runtime resources before bootstrap"
    )
    bootstrap_task = next(
        task
        for task in tasks
        if task["name"]
        == "Reconcile secure Umami bootstrap job after deployment readiness"
    )

    assert "rejectattr('kind', 'equalto', 'Job')" in runtime_task["loop"]
    assert "selectattr('kind', 'equalto', 'Job')" in str(
        bootstrap_task["kubernetes.core.k8s"]["definition"]
    )
    assert task_names.index("Wait for the Umami deployment") < task_names.index(
        "Reconcile secure Umami bootstrap job after deployment readiness"
    )


def test_umami_rollout_is_highly_available_and_resource_bounded():
    assert "maxUnavailable: 0" in RESOURCES
    assert "maxSurge: 1" in RESOURCES
    assert "topologySpreadConstraints:" in RESOURCES
    assert "whenUnsatisfiable: DoNotSchedule" in RESOURCES
    assert "kind: PodDisruptionBudget" in RESOURCES
    assert "kind: HorizontalPodAutoscaler" in RESOURCES
    assert RESOURCES.count("path: /api/heartbeat") == 3
    assert "readOnlyRootFilesystem: true" in RESOURCES
    assert "allowPrivilegeEscalation: false" in RESOURCES
    assert "automountServiceAccountToken: false" in RESOURCES
    assert "drop: [ALL]" in RESOURCES


def test_umami_template_renders_valid_ha_and_singleton_resource_sets():
    ha_documents = render_resources(replicas=2, hpa_enabled=True)
    assert [item["kind"] for item in ha_documents] == [
        "Deployment",
        "Service",
        "PodDisruptionBudget",
        "HorizontalPodAutoscaler",
        "NetworkPolicy",
        "NetworkPolicy",
        "NetworkPolicy",
        "CiliumNetworkPolicy",
        "HTTPRoute",
        "HTTPRoute",
        "Job",
    ]
    for workload_kind in ("Deployment", "Job"):
        workload = next(
            item for item in ha_documents if item["kind"] == workload_kind
        )
        pod_security = workload["spec"]["template"]["spec"]["securityContext"]
        assert pod_security["runAsNonRoot"] is True
        assert pod_security["runAsUser"] == 1001
        assert pod_security["runAsGroup"] == 65533
    singleton_documents = render_resources(replicas=1, hpa_enabled=False)
    assert "PodDisruptionBudget" not in {
        item["kind"] for item in singleton_documents
    }
    assert "HorizontalPodAutoscaler" not in {
        item["kind"] for item in singleton_documents
    }


def test_umami_dashboard_is_private_and_public_surface_is_minimal():
    assert "name: umami-dashboard" in RESOURCES
    assert "name: {{ umami_admin_gateway_name }}" in RESOURCES
    assert "name: umami-ingest" in RESOURCES
    assert "name: {{ umami_main_gateway_name }}" in RESOURCES
    assert "value: /script.js" in RESOURCES
    assert "value: /api/send" in RESOURCES
    assert "type: Exact" in RESOURCES
    assert "kind: CiliumNetworkPolicy" in RESOURCES
    assert "fromEntities: [ingress]" in RESOURCES
    assert "postgres-operator.crunchydata.com/role: pgbouncer" in RESOURCES
    assert "name: allow-umami-bootstrap" in RESOURCES
    assert "app.kubernetes.io/component: bootstrap" in RESOURCES
    assert "169.254.25.10/32" in RESOURCES
    assert "toEntities: [host, remote-node]" in RESOURCES
    assert "name: default-deny" in RESOURCES


def test_umami_is_in_deploy_and_safe_removal_lifecycles():
    assert "name: umami" in DEPLOY
    assert "when: deploy_umami | default(false) | bool" in DEPLOY
    assert "umami:" in REMOVE
    assert "namespaces: [umami]" in REMOVE
    assert 'umami: "{{ platform_umami_enabled }}"' in REMOVE
    assert (
        "run_playbook --tags databases,umami" in ORCHESTRATOR
    ), "late opt-in must reconcile the dedicated PostgreSQL principal first"
