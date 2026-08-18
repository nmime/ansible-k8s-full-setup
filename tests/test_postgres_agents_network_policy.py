from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_configured_agents_app_gets_identity_aware_postgres_egress():
    tasks = (ROOT / "roles/k8s-databases/tasks/main.yml").read_text()
    policy = tasks.split(
        "name: Allow configured agents workloads to reach PostgreSQL",
        1,
    )[1].split("name: Allow database egress (replication + backups)", 1)[0]

    assert "name: allow-agents-postgres" in policy
    assert "namespace: agents" in policy
    assert "databases.postgresql.agents_app_name" in policy
    assert "default('application-runtime')" in policy
    assert "serviceName: '{{ project_name | default(''k8s'') }}-pg-primary'" in policy
    assert "serviceName: '{{ project_name | default(''k8s'') }}-pg-pgbouncer'" in policy
    assert "k8s:app.kubernetes.io/component: pg" in policy
    assert "k8s:postgres-operator.crunchydata.com/role: pgbouncer" in policy
    assert policy.count("port: '5432'") == 4
    assert "'agents' in (" in policy
