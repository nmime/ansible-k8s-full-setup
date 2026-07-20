from pathlib import Path


TASKS = Path(__file__).parents[2] / "roles" / "postal" / "tasks" / "main.yml"


def test_postal_schema_is_reconciled_before_components_start() -> None:
    content = TASKS.read_text()

    schema = content.index("name: Initialize or update the Postal database schema")
    components = content.index("name: Deploy Postal components")
    assert schema < components
    assert "postal-schema-reconcile" in content
    assert "'update' if (_postal_schema_exists.stdout | int) > 0 else 'initialize'" in content
    assert "wait_condition:" in content


def test_postal_application_containers_are_restricted() -> None:
    content = TASKS.read_text()

    assert content.count("runAsNonRoot: true") >= 2
    assert content.count("allowPrivilegeEscalation: false") >= 2
    assert content.count("drop: [ALL]") >= 2
    assert content.count("add: [NET_BIND_SERVICE]") >= 2
    assert content.count("type: RuntimeDefault") >= 2


def test_postal_smtp_uses_an_unprivileged_container_port() -> None:
    content = TASKS.read_text()

    assert "default_port: 2525" in content
    assert "containerPort: 2525" in content
    assert content.count("targetPort: 2525") == 2


def test_postal_storage_growth_preserves_the_mariadb_claim() -> None:
    content = TASKS.read_text()
    assert "reconcile_statefulset_storage.yml" in content
    assert "storage_reconcile_statefulset: postal-mariadb" in content
