from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TASKS = (ROOT / "roles/temporal/tasks/main.yml").read_text(encoding="utf-8")


def test_temporal_uses_current_chart_persistence_contract():
    assert "temporal_chart_ver: 1.2.0" in TASKS
    assert "datastores:" in TASKS
    assert "pluginName: postgres12" in TASKS
    assert "databaseName: temporal" in TASKS
    assert "databaseName: temporal_visibility" in TASKS
    assert "existingSecret: temporal-db-credentials" in TASKS
    assert "secretKey: password" in TASKS
    assert "manageSchema: true" in TASKS
    assert "createDatabase: true" in TASKS


def test_temporal_does_not_pass_removed_chart_values():
    tasks = yaml.safe_load(TASKS)
    install = next(task for task in tasks if task["name"] == "Install Temporal server via Helm")
    values = install["kubernetes.core.helm"]["values"]
    assert not ({"cassandra", "elasticsearch", "prometheus", "grafana", "mysql"} & values.keys())
    persistence = values["server"]["config"]["persistence"]
    assert "additionalStores" not in persistence
    assert "default" not in persistence
    assert "visibility" not in persistence
    assert set(persistence["datastores"]) == {"default", "visibility"}
    assert not ({"createDatabase", "setup", "update"} & values["schema"].keys())


def test_temporal_uses_chart_compatible_images_and_shims():
    assert "temporal_server_ver: 1.31.0" in TASKS
    assert "temporal_ui_ver: 2.49.1" in TASKS
    assert "temporal_admin_tools_ver: 1.31.0" in TASKS
    assert "dockerize: false" in TASKS
    assert "elasticsearchTool: false" in TASKS


def test_temporal_uses_one_namespace_fact_consistently():
    assert "{{ temporal_namespace" not in TASKS
    assert "namespace: '{{ temporal_ns }}'" in TASKS


def test_temporal_ingress_policy_covers_grpc_and_web_ui():
    ingress = TASKS.split("- name: Allow Temporal ingress from cluster", 1)[1].split(
        "- name:", 1
    )[0]
    for port in ("7233", "7234", "7235", "8080"):
        assert f"port: '{port}'" in ingress


def test_temporal_sql_visibility_does_not_require_elasticsearch():
    normalize = (ROOT / "playbooks/tasks/normalize_profile.yml").read_text(
        encoding="utf-8"
    )
    dependency = normalize.split(
        "- name: Validate Temporal external service dependencies", 1
    )[1].split("- name:", 1)[0]
    assert "platform_postgresql_enabled" in dependency
    assert "platform_elasticsearch_enabled" not in dependency

    orchestrator = (ROOT / "platform-orchestrator/platform.sh").read_text(
        encoding="utf-8"
    )
    temporal_selection = next(
        line
        for line in orchestrator.splitlines()
        if "temporal) echo" in line and ".databases.enabled" in line
    )
    assert ".databases.postgresql.enabled" in temporal_selection
    assert ".elasticsearch.enabled" not in temporal_selection


def test_temporal_small_connection_pools_fit_postgresql_budget():
    assert "temporal_sql_max_conns" in TASKS
    assert "default(5 if resource_tier in [''minimal'', ''small''] else 10)" in TASKS
    assert "temporal_sql_max_idle_conns" in TASKS
    assert "default(2 if resource_tier in [''minimal'', ''small''] else 5)" in TASKS
    assert TASKS.count("maxConns: '{{ temporal_sql_max_conns | int }}'") == 2
    assert TASKS.count("maxIdleConns: '{{ temporal_sql_max_idle_conns | int }}'") == 2


def test_temporal_replica_controls_are_independent_and_consumed():
    tasks = yaml.safe_load(TASKS)
    facts = next(task for task in tasks if task["name"] == "Set Temporal tier-specific variables")[
        "set_fact"
    ]
    assert "temporal_replicas" not in facts

    install = next(task for task in tasks if task["name"] == "Install Temporal server via Helm")
    server = install["kubernetes.core.helm"]["values"]["server"]
    for component in ("frontend", "history", "matching", "worker"):
        assert server[component]["replicaCount"] == (
            "{{ temporal_" + component + "_replicas | int }}"
        )
