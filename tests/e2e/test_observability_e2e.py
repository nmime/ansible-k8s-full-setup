import os, pytest, subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OBS = os.path.join(REPO, "roles", "k8s-observability")

class TestTaskFileInventory:
    def test_minimum_four_task_files(self):
        tasks = os.listdir(os.path.join(OBS, "tasks"))
        yml = [f for f in tasks if f.endswith(".yml")]
        assert len(yml) >= 4, f"Expected 4+ task files, found {yml}"
    def test_all_present(self):
        for name in ["main.yml", "alerting.yml", "tracing.yml", "health_checks.yml"]:
            assert os.path.isfile(os.path.join(OBS, "tasks", name)), f"Missing {name}"

class TestCrossRoleConsistency:
    def test_defaults_align_with_tracing(self):
        with open(os.path.join(REPO, "defaults", "main.yml")) as fh:
            defs = fh.read()
        with open(os.path.join(OBS, "tasks", "tracing.yml")) as fh:
            tracing = fh.read()
        for v in ["tracing_enabled", "tempo_retention", "tempo_storage_size"]:
            assert v in defs, f"{v} must be in defaults"
            assert v in tracing or v == "tracing_enabled", f"{v} used in tracing"

    def test_keda_uses_vm_service_scrape(self):
        with open(os.path.join(REPO, "roles", "k8s-autoscaling", "tasks", "main.yml")) as fh:
            c = fh.read()
        assert "VMServiceScrape" in c

class TestAnsibleLint:
    @pytest.mark.skipif(
        subprocess.call(["which", "ansible-lint"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0,
        reason="ansible-lint not installed"
    )
    def test_lint_no_syntax_errors(self):
        for tf in ["tasks/main.yml", "tasks/tracing.yml", "tasks/health_checks.yml"]:
            result = subprocess.run(
                ["ansible-lint", tf],
                capture_output=True, text=True, timeout=60
            )
            assert "Syntax Error" not in result.stderr
