import os, pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OBS = os.path.join(REPO, "roles", "k8s-observability")

class TestFileStructure:
    def test_tracing_exists(self):
        assert os.path.isfile(os.path.join(OBS, "tasks", "tracing.yml"))
    def test_health_checks_exists(self):
        assert os.path.isfile(os.path.join(OBS, "tasks", "health_checks.yml"))
    def test_vmservicescrapes_exists(self):
        assert os.path.isfile(os.path.join(OBS, "templates", "vmservicescrapes.yml"))
    def test_observeability_md_exists(self):
        assert os.path.isfile(os.path.join(REPO, "docs", "OBSERVABILITY.md"))

class TestMainOrchestration:
    @pytest.fixture
    def content(self):
        with open(os.path.join(OBS, "tasks", "main.yml")) as fh:
            return fh.read()
    def test_includes_tracing(self, content):
        assert "import_tasks: tracing.yml" in content
    def test_includes_health_checks(self, content):
        assert "import_tasks: health_checks.yml" in content
    def test_references_vmservicescrapes(self, content):
        assert "vmservicescrapes.yml" in content
    def test_no_servicemonitors_ref(self, content):
        assert "servicemonitors.yml" not in content
    def test_no_prometheus_rule_crd(self, content):
        assert "kind: PrometheusRule" not in content
    def test_filebeat_uses_supported_container_filestream(self, content):
        assert "type: filestream" in content
        assert "parsers:\n                  - container: ~" in content
        assert "type: container" not in content
    def test_loki_persistent_claims_are_never_auto_deleted(self, content):
        assert content.count("enableStatefulSetAutoDeletePVC: false") >= 3
        assert content.count("whenDeleted: Retain") >= 3
        assert content.count("whenScaled: Retain") >= 3

    def test_loki_workloads_use_runtime_default_seccomp(self, content):
        assert "lokiCanary:" in content
        assert content.count("type: RuntimeDefault") >= 3

    def test_loki_retention_is_backed_by_compactor_deletion(self, content):
        assert "retention_period: '{{ loki_retention }}'" in content
        assert "retention_enabled: true" in content
        assert "delete_request_store: s3" in content

    def test_pmm_rwo_volume_uses_recreate_strategy(self, content):
        pmm = content.split("- name: Deploy PMM Server", 1)[1].split(
            "- name: Create PMM Server Service", 1
        )[0]
        assert "strategy:\n          type: Recreate" in pmm
        assert "runAsNonRoot: true" in pmm
        assert "fsGroupChangePolicy: OnRootMismatch" in pmm
        assert "fix-perms" not in pmm
        assert "PMM_ENABLE_UPDATES" in pmm
        assert "PMM_METRICS_RESOLUTION" in pmm
        assert "PMM_ENABLE_TELEMETRY" in pmm
        assert "DISABLE_UPDATES" not in pmm
        assert "Remove the legacy privileged PMM permission init container" in content
        assert "/spec/template/spec/initContainers" in content

    def test_pmm_readiness_and_service_token_are_fail_closed(self, content):
        wait = content.split("- name: Wait for PMM Server to be ready", 1)[1].split(
            "- name: Get PMM Server pod name", 1
        )[0]
        assert "failed_when: false" not in wait
        assert "Require a non-empty PMM service-account token" in content
        assert "Persist the PMM service-account token" in content
        assert "Issue a validated PMM 3 service-account token" in content
        assert "glsa_" in content
        assert "_legacy_pmm_server_token_result" not in content
        assert "supervisorctl stop pmm-managed" not in content

class TestTracingContent:
    @pytest.fixture
    def content(self):
        with open(os.path.join(OBS, "tasks", "tracing.yml")) as fh:
            return fh.read()
    def test_has_tempo_chart(self, content):
        assert "grafana/tempo" in content
    def test_has_otel_chart(self, content):
        assert "opentelemetry-collector" in content
    def test_has_explicit_versions(self, content):
        assert "tempo_chart_version" in content
        assert "tempo_image_tag" in content
        assert "otel_collector_image_tag" in content
    def test_gated_by_tracing_enabled(self, content):
        assert "tracing_enabled | bool" in content
    def test_s3_storage(self, content):
        assert "backend: s3" in content
    def test_otel_exports_to_tempo(self, content):
        assert "tempo.{{ tempo_namespace }}.svc.cluster.local:4317" in content
        assert "repository: otel/opentelemetry-collector-k8s" in content
        assert "ports:\n        metrics:" in content
    def test_tempo_chart_uses_current_monolithic_values_contract(self, content):
        assert "tempo:\n        tag:" in content
        assert "config:\n        auth_enabled:" not in content
        assert "config.expand-env: 'true'" in content
    def test_vmservicescrape_for_tempo(self, content):
        assert "VMServiceScrape" in content
    def test_tempo_storage_growth_preserves_existing_claims(self, content):
        assert "reconcile_statefulset_storage.yml" in content
        assert "storage_reconcile_statefulset: tempo" in content
        helper = open(os.path.join(REPO, "playbooks", "tasks", "reconcile_statefulset_storage.yml")).read()
        assert "Reject {{ storage_reconcile_statefulset }} storage shrink attempts" in helper
        assert "propagationPolicy: Orphan" in helper
        assert "kubernetes.core.k8s_json_patch" in helper
        assert "/spec/resources/requests/storage" in helper

class TestHealthChecksContent:
    @pytest.fixture
    def content(self):
        with open(os.path.join(OBS, "tasks", "health_checks.yml")) as fh:
            return fh.read()
    def test_checks_all_components(self, content):
        for c in ["victoria-metrics-operator", "vmsingle", "VMCluster", "loki", "grafana", "tempo", "opentelemetry-collector"]:
            assert c in content, f"Missing check for {c}"
    def test_health_checks_are_fail_closed(self, content):
        assert "ignore_errors: true" not in content
        assert "failed_when: false" not in content

class TestKedaFix:
    @pytest.fixture
    def content(self):
        with open(os.path.join(REPO, "roles", "k8s-autoscaling", "tasks", "main.yml")) as fh:
            return fh.read()
    def test_has_vm_service_scrape(self, content):
        assert "VMServiceScrape" in content
    def test_has_native_prometheus_metrics_configuration(self, content):
        assert "prometheus:" in content
        assert "metricServer:" in content
        assert "operator:" in content
        assert content.count("enabled: true") >= 2

class TestDefaults:
    @pytest.fixture
    def content(self):
        with open(os.path.join(REPO, "defaults", "main.yml")) as fh:
            return fh.read()
    def test_tracing_vars(self, content):
        for v in ["tracing_enabled", "tempo_retention", "tempo_storage_size", "otel_collector_enabled"]:
            assert v in content, f"Missing {v}"


class TestCorootStructure:
    def test_cluster_agent_has_wal_replay_headroom(self):
        with open(os.path.join(REPO, "defaults", "main.yml")) as fh:
            defaults = fh.read()
        assert "coroot_cluster_agent_memory_limit: 2Gi" in defaults

    def test_health_gate_matches_operator_statefulset(self):
        with open(os.path.join(OBS, "tasks", "coroot.yml")) as fh:
            content = fh.read()
        assert "kind: StatefulSet" in content
        assert "name: coroot-coroot" in content
        assert "status.readyReplicas" in content

    def test_storage_growth_uses_claim_preserving_reconciliation(self):
        with open(os.path.join(OBS, "tasks", "coroot.yml")) as fh:
            content = fh.read()
        assert content.count("reconcile_statefulset_storage.yml") == 5
        assert "storage_reconcile_statefulset: coroot-coroot" in content
        assert "storage_reconcile_statefulset: coroot-clickhouse-shard-0" in content
        assert "force_conflicts: true" in content
        assert content.count("storage_reconcile_orphan: false") == 3
        assert content.count("storage_reconcile_wait: false") == 2


def test_promtail_is_retired_from_the_agent_namespace_when_loki_is_disabled():
    with open(os.path.join(OBS, "tasks", "main.yml")) as fh:
        content = fh.read()
    task = content.split(
        "- name: Remove Promtail node agents when Loki is not the selected log backend",
        1,
    )[1].split("- name:", 1)[0]
    assert "release_namespace: '{{ logging_agent_namespace }}'" in task
    assert 'when: log_stack != "loki"' in task
