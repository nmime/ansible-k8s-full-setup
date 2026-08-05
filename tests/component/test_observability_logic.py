import os, re, pytest, yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OBS = os.path.join(REPO, "roles", "k8s-observability")

def read(path):
    with open(os.path.join(OBS, path)) as fh:
        return fh.read()

class TestIncludeOrder:
    def test_order(self):
        c = read("tasks/main.yml")
        assert c.index("alerting.yml") < c.index("tracing.yml") < c.index("health_checks.yml")

class TestTemplateJinja2:
    def test_balanced_blocks(self):
        c = read("templates/vmservicescrapes.yml")
        opens = len(re.findall(r'\{%\s*if', c))
        closes = len(re.findall(r'\{%\s*endif', c))
        assert opens == closes, f"Unbalanced: {opens} opens, {closes} closes"

class TestTracingLogic:
    def test_repos_before_deploy(self):
        c = read("tasks/tracing.yml")
        assert c.index("helm_repository") < c.index("kubernetes.core.helm:")
    def test_all_deploys_gated(self):
        c = read("tasks/tracing.yml")
        assert "tracing_enabled | bool" in c

class TestAlertingIntact:
    def test_vmalertmanager(self):
        assert "VMAlertmanager" in read("tasks/alerting.yml")
    def test_vmalert(self):
        assert "VMAlert" in read("tasks/alerting.yml")
    def test_vmrules(self):
        assert "VMRule" in read("tasks/alerting.yml")

    def test_slo_rules_cover_http_workloads_ci_storage_and_delivery(self):
        content = read("tasks/alerting.yml")
        for alert in (
            "ServiceHttp5xxRatioHigh",
            "ServiceHttp5xxRatioCritical",
            "PodUnexpectedRestart",
            "PodRestartStorm",
            "PodCrashLoopBackOffPersistent",
            "PodOOMKilledProduction",
            "ProductionDeploymentUnavailable",
            "KubernetesJobFailed",
            "GitLabRunnerMetricsDown",
            "GitLabRunnerSystemErrors",
            "GitLabRunnerQueueSlow",
            "ObjectStorageS35xx",
            "ObjectStorageNoMasterLeader",
            "AlertDeliveryFailed",
            "AlertmanagerConfigReloadFailed",
            "VMAlertRuleEvaluationErrors",
        ):
            assert f"alert: {alert}" in content
        assert 'increase(kube_job_status_failed[10m]) > 0' in content
        assert 'gitlab_runner_errors_total{level=~"error|fatal|panic"}' in content
        assert 'app_id!=""' in content
        assert "absent(SeaweedFS_master_is_leader)" in content

    def test_telegram_sends_warning_critical_and_resolved_notifications(self):
        content = read("tasks/alerting.yml")
        assert "receiver: telegram-critical" in content
        assert "receiver: telegram-warning" in content
        assert content.count("send_resolved: true") >= 3
        assert content.count(
            "bot_token_file: /etc/vm/secrets/alert-secrets/telegram-bot-token"
        ) == 2
        assert "/etc/alertmanager/secrets" not in content

    def test_gitlab_runner_alerts_identify_the_failing_manager_and_avoid_rollout_flaps(self):
        content = read("tasks/alerting.yml")
        assert "kube_deployment_spec_replicas" in content
        assert "kube_deployment_status_replicas_available" in content
        assert "Intentional scale-to-zero is excluded" in content
        assert "sum by (namespace, pod, job, level)" in content
        assert "sum by (namespace, pod, job, status)" in content
        assert "sum by (namespace, runner, runner_name, le)" in content
        assert "humanizeDuration $value" in content
        assert "Labels.runner_name" in content
        assert "Labels.system_id" in content
        assert content.count("runbook_url: 'https://git.n0xeid.xyz/fun/argocd/") >= 4

    def test_replica_and_storage_controls_are_profile_driven(self):
        tasks = yaml.safe_load(read("tasks/alerting.yml"))
        alertmanager = next(task for task in tasks if task["name"] == "Deploy VMAlertmanager CR")
        vmalert = next(task for task in tasks if task["name"] == "Deploy VMAlert CR")
        alertmanager_spec = alertmanager["kubernetes.core.k8s"]["definition"]["spec"]
        alertmanager_metadata = alertmanager["kubernetes.core.k8s"]["definition"][
            "metadata"
        ]
        vmalert_spec = vmalert["kubernetes.core.k8s"]["definition"]["spec"]
        assert "alerting.replicas" in alertmanager_spec["replicaCount"]
        assert "resource_tier in ['medium', 'production']" in alertmanager_spec["replicaCount"]
        assert "alerting.storage_size" in alertmanager_spec["storage"][
            "volumeClaimTemplate"
        ]["spec"]["resources"]["requests"]["storage"]
        assert "alerting.vmalert_replicas" in vmalert_spec["replicaCount"]
        assert "tier == 'production'" in vmalert_spec["replicaCount"]
        assert (
            "_alertmanager_config_secret_revision.resources[0].metadata.resourceVersion"
            in alertmanager_metadata[
            "annotations"
            ]["n0xeid.xyz/config-secret-resource-version"]
        )

    def test_blackbox_private_gateway_matches_the_vpn_boundary(self):
        path = os.path.join(REPO, "roles", "blackbox-exporter", "tasks", "main.yml")
        with open(path) as fh:
            content = fh.read()
        assert "http_private_gateway" in content
        assert "valid_status_codes: [404]" in content
        assert "http_private_gateway_redirect" in content
        assert "valid_status_codes: [307]" in content
        assert "blackbox-private-gateway" in content
        assert "blackbox-private-gateway-redirect" in content
        assert "https://git.'  + domain" in content
        assert "gitlab.public_webservice_enabled" in content
        assert "https://gitlab.'  + domain" not in content

    def test_blackbox_verifies_dragonfly_tls_with_the_platform_ca(self):
        path = os.path.join(REPO, "roles", "blackbox-exporter", "tasks", "main.yml")
        with open(path) as fh:
            content = fh.read()
        assert "tcp_tls_verified" in content
        assert "ca_file: /etc/blackbox/datastore-ca/ca.crt" in content
        assert "server_name: dragonfly-tls.dragonfly.svc.cluster.local" in content
        assert "insecure_skip_verify: false" in content
        assert "name: blackbox-internal-tls" in content
        assert "dragonfly.dragonfly.svc.cluster.local:6379" not in content

class TestElasticsearchUntouched:
    def test_exists(self):
        es = os.path.join(REPO, "roles", "elasticsearch", "tasks", "main.yml")
        assert os.path.isfile(es), "Elasticsearch role must exist untouched"
