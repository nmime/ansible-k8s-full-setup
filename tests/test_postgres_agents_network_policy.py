from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_social_agents_gets_identity_aware_postgres_egress():
    tasks = (ROOT / "roles/k8s-databases/tasks/main.yml").read_text()
    policy = tasks.split(
        "name: Allow SocialAgents migration and runtime workloads to reach PostgreSQL",
        1,
    )[1].split("name: Allow database egress (replication + backups)", 1)[0]

    assert "name: allow-social-agents-postgres" in policy
    assert "namespace: agents" in policy
    assert "k8s:app.kubernetes.io/name: social-agents" in policy
    assert "serviceName: '{{ project_name | default(''k8s'') }}-pg-primary'" in policy
    assert "serviceName: '{{ project_name | default(''k8s'') }}-pg-pgbouncer'" in policy
    assert "k8s:app.kubernetes.io/component: pg" in policy
    assert "k8s:postgres-operator.crunchydata.com/role: pgbouncer" in policy
    assert policy.count("port: '5432'") == 4
    assert "'agents' in (" in policy
