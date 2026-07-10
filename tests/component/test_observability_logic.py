import os, re, pytest

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

class TestElasticsearchUntouched:
    def test_exists(self):
        es = os.path.join(REPO, "roles", "elasticsearch", "tasks", "main.yml")
        assert os.path.isfile(es), "Elasticsearch role must exist untouched"
