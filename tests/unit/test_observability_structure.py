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
        assert os.path.isfile(os.path.join(REPO, "OBSERVABILITY.md"))

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
        assert "tempo-distributor" in content
    def test_vmservicescrape_for_tempo(self, content):
        assert "VMServiceScrape" in content

class TestHealthChecksContent:
    @pytest.fixture
    def content(self):
        with open(os.path.join(OBS, "tasks", "health_checks.yml")) as fh:
            return fh.read()
    def test_checks_all_components(self, content):
        for c in ["victoria-metrics-operator", "vmsingle", "VMCluster", "loki", "grafana", "tempo", "otel-collector"]:
            assert c in content, f"Missing check for {c}"
    def test_ignore_errors(self, content):
        assert content.count("ignore_errors: true") >= 5

class TestKedaFix:
    @pytest.fixture
    def content(self):
        with open(os.path.join(REPO, "roles", "k8s-autoscaling", "tasks", "main.yml")) as fh:
            return fh.read()
    def test_has_vm_service_scrape(self, content):
        assert "VMServiceScrape" in content
    def test_has_prometheus_enabled_flag(self, content):
        assert "prometheus_enabled" in content

class TestDefaults:
    @pytest.fixture
    def content(self):
        with open(os.path.join(REPO, "defaults", "main.yml")) as fh:
            return fh.read()
    def test_tracing_vars(self, content):
        for v in ["tracing_enabled", "tempo_retention", "tempo_storage_size", "otel_collector_enabled"]:
            assert v in content, f"Missing {v}"
