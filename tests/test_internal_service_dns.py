import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "roles/k8s-cluster-management/files/reconcile_cluster_dns.py"
)


def load_dns_module():
    spec = importlib.util.spec_from_file_location("reconcile_cluster_dns", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_split_dns_blocks_preserve_external_resolution_and_nodelocal_path():
    module = load_dns_module()
    zones = module.validate_zones(
        [
            {
                "zone": "n0xeid.xyz",
                "records": [
                    {
                        "address": "10.0.10.2",
                        "names": [
                            "git.n0xeid.xyz",
                            "argo.n0xeid.xyz",
                            "metabase.n0xeid.xyz",
                        ],
                    }
                ],
            }
        ]
    )
    coredns = module.coredns_block(zones[0])
    assert "10.0.10.2 git.n0xeid.xyz argo.n0xeid.xyz metabase.n0xeid.xyz" in coredns
    assert "fallthrough" in coredns
    assert "forward . 1.1.1.1 8.8.8.8" in coredns
    nodelocal = module.nodelocal_block(zones[0], "10.233.0.3", "169.254.25.10")
    assert "n0xeid.xyz:53" in nodelocal
    assert "forward . 10.233.0.3" in nodelocal
    assert "bind 169.254.25.10" in nodelocal


def test_split_dns_validation_rejects_names_outside_the_zone():
    module = load_dns_module()
    try:
        module.validate_zones(
            [
                {
                    "zone": "n0xeid.xyz",
                    "records": [
                        {
                            "address": "10.0.10.2",
                            "names": ["metabase.funfiesta.games"],
                        }
                    ],
                }
            ]
        )
    except ValueError as error:
        assert "not a valid child" in str(error)
    else:
        raise AssertionError("out-of-zone internal DNS record was accepted")


def test_gitlab_and_argocd_domains_are_configurable_with_compatibility_aliases():
    gitlab = (
        ROOT / "roles/gitlab-selfhosted/tasks/main.yml"
    ).read_text()
    gitops = (ROOT / "roles/k8s-gitops/tasks/main.yml").read_text()
    assert "gitlab.domain | default('git.' ~ domain, true)" in gitlab
    assert "gitlab.domain_aliases" in gitlab
    assert "hostnames: '{{ [gitlab_domain] + gitlab_domain_aliases }}'" in gitlab
    assert "gitops.domain | default('argocd.' ~ domain, true)" in gitops
    assert "gitops.domain_aliases" in gitops
    assert "sectionName: cluster-https" in gitops
    assert "gitlab.domain | default('git.' ~ domain, true)" in gitops
    assert "hostAliases: '{{ argocd_repo_host_aliases }}'" in gitops
    assert "Discover the in-cluster GitLab Shell service for component-only runs" in gitops
    assert "Record the in-cluster GitLab Shell address for Argo CD" in gitops
    assert "repoServer:\n        replicas:" in gitops
    assert "gitops.insecure_mode" in gitops
    assert "argocd_insecure_mode_effective" in gitops
    assert "gitlab-gitlab-shell-host-keys" in gitops
    assert "extraHosts: '{{ argocd_gitlab_ssh_known_hosts" in gitops


def test_postgresql_extra_users_publish_tls_connection_secrets():
    databases = (
        ROOT / "roles/k8s-databases/tasks/main.yml"
    ).read_text()
    assert "databases.postgresql.extra_users" in databases
    assert "pg-cluster-ca-cert" in databases
    assert "ca.crt:" in databases
    assert "sslmode: verify-full" in databases
    assert "Create stable short PostgreSQL service alias" in databases
    assert "Issue hostname-verified PostgreSQL alias certificate" in databases
    assert "Configure PostgreSQL role search paths" in databases
    assert "ALTER ROLE" in databases
    assert "item.search_path" in databases
    assert "argocd.argoproj.io/compare-options: IgnoreExtraneous" in databases


def test_medium_optimized_declares_scoped_application_database_users():
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator/profiles/medium-optimized.yaml").read_text()
    )
    postgresql = profile["databases"]["postgresql"]
    mongodb = profile["databases"]["mongodb"]
    assert postgresql["service_alias"] == "n0xeid-pg"
    assert mongodb["service_alias"] == "n0xeid-mongo"

    users = {
        item["operator"]["name"]: item
        for item in postgresql["extra_users"]
    }
    assert set(users) == {
        "metabase",
        "dadya-prod",
        "dadya-pp",
        "social-agents-owner",
    }
    assert users["social-agents-owner"]["operator"]["options"] == "BYPASSRLS"
    assert {
        item["target_namespace"] for item in users.values()
    } == {"analytics", "dadya-production", "dadya-preproduction", "agents"}
    for item in users.values():
        assert "target_namespace" not in item["operator"]
        assert "secret_name" not in item["operator"]


def test_cluster_management_consumes_internal_dns_profile():
    tasks = yaml.safe_load(
        (ROOT / "roles/k8s-cluster-management/tasks/main.yml").read_text()
    )
    task = next(
        item
        for item in tasks
        if item.get("name") == "Reconcile upstream and split-horizon cluster DNS"
    )
    assert task["ansible.builtin.script"]["cmd"] == "reconcile_cluster_dns.py"
    assert "network.internal_dns.zones" in task["environment"][
        "PLATFORM_INTERNAL_DNS_ZONES"
    ]
