from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agents_can_reach_dragonfly_without_platform_managed_blanket_egress():
    tasks = (ROOT / "roles/dragonfly/tasks/main.yml").read_text()
    ingress = tasks.split(
        "name: Create allow-from-platform NetworkPolicies for Dragonfly", 1
    )[1].split("name: Discover existing Dragonfly consumer namespaces", 1)[0]
    managed_egress = tasks.split(
        "name: Resolve existing Dragonfly consumer namespaces", 1
    )[1].split("name: Allow consumers to egress to Dragonfly", 1)[0]
    managed_consumers = managed_egress.split(
        "name: Allow agents workloads to resolve DNS and reach Dragonfly", 1
    )[0]

    assert "- agents" in ingress
    assert "'agents'" not in managed_consumers
    assert "name: allow-agents-dragonfly" in managed_egress
    assert "toEntities: [host]" in managed_egress
    assert "toCIDR: [169.254.25.10/32]" in managed_egress
    assert "serviceName: dragonfly" in managed_egress
    assert "k8s:app.kubernetes.io/name: dragonfly" in managed_egress
