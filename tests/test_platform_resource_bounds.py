from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_tasks(relative_path: str) -> list[dict]:
    return yaml.safe_load((ROOT / relative_path).read_text())


def task_named(tasks: list[dict], name: str) -> dict:
    return next(task for task in tasks if task.get("name") == name)


def assert_complete_resources(resources: dict) -> None:
    assert set(resources) == {"requests", "limits"}
    assert set(resources["requests"]) == {"cpu", "memory"}
    assert set(resources["limits"]) == {"cpu", "memory"}
    assert all(str(value) for section in resources.values() for value in section.values())


def workload_resources(task: dict, workload_key: str = "kubernetes.core.k8s") -> list[dict]:
    pod_spec = task[workload_key]["definition"]["spec"]["template"]["spec"]
    return pod_spec["containers"]


def test_cilium_chart_and_reconciliation_bound_operator_and_envoy() -> None:
    tasks = load_tasks("roles/k8s-cluster-management/tasks/main.yml")
    group_vars = task_named(tasks, "Generate Kubespray group_vars")["copy"]["content"]
    assert "cilium_extra_values:" in group_vars
    assert "operator:" in group_vars
    assert "envoy:" in group_vars
    assert group_vars.count("resource_tier in ['minimal', 'small']") >= 8

    for name in (
        "Reconcile Cilium operator resource bounds after Kubespray",
        "Reconcile Cilium Envoy resource bounds after Kubespray",
    ):
        containers = workload_resources(task_named(tasks, name))
        assert len(containers) == 1
        assert_complete_resources(containers[0]["resources"])


def test_nodelocal_ccm_and_every_csi_container_have_complete_bounds() -> None:
    tasks = load_tasks("roles/k8s-cluster-management/tasks/main.yml")

    nodelocal = workload_resources(
        task_named(tasks, "Reconcile NodeLocal DNS complete resource bounds")
    )
    assert [container["name"] for container in nodelocal] == ["node-cache"]
    assert_complete_resources(nodelocal[0]["resources"])

    ccm_patch = task_named(
        tasks, "Enforce the bounded CCM controller policy and rollout deadline"
    )["kubernetes.core.k8s_json_patch"]["patch"]
    ccm_resources = next(
        operation["value"]
        for operation in ccm_patch
        if operation["path"] == "/spec/template/spec/containers/0/resources"
    )
    assert_complete_resources(ccm_resources)

    expected = {
        "Reconcile Hetzner CSI controller resource bounds": {
            "hcloud-csi-driver",
            "csi-attacher",
            "csi-resizer",
            "csi-provisioner",
            "liveness-probe",
        },
        "Reconcile Hetzner CSI node resource bounds": {
            "csi-node-driver-registrar",
            "liveness-probe",
            "hcloud-csi-driver",
        },
    }
    for name, container_names in expected.items():
        containers = workload_resources(task_named(tasks, name))
        assert {container["name"] for container in containers} == container_names
        for container in containers:
            assert_complete_resources(container["resources"])


def test_metallb_chart_bounds_all_long_running_and_frr_init_containers() -> None:
    tasks = load_tasks("roles/k8s-cluster-management/tasks/main.yml")
    values = task_named(tasks, "Install MetalLB")["kubernetes.core.helm"]["values"]

    assert_complete_resources(values["controller"]["resources"])
    assert_complete_resources(values["speaker"]["resources"])
    frr = values["frr-k8s"]["frrk8s"]
    for component in ("resources", "frr", "reloader", "frrMetrics", "frrStatus"):
        resources = frr[component] if component == "resources" else frr[component]["resources"]
        assert_complete_resources(resources)

    init_task = task_named(tasks, "Reconcile MetalLB FRR-K8s init-container resource bounds")
    assert set(init_task["loop"]) == {
        "cp-frr-files",
        "cp-reloader",
        "cp-metrics",
        "cp-frr-status",
    }
    init_containers = init_task["kubernetes.core.k8s"]["definition"]["spec"]["template"]["spec"]["initContainers"]
    assert len(init_containers) == 1
    assert_complete_resources(init_containers[0]["resources"])


def test_monitoring_sidecars_and_init_containers_have_complete_bounds() -> None:
    tasks = load_tasks("roles/k8s-observability/tasks/main.yml")
    vmagent = task_named(tasks, "Deploy VMAgent for scraping")["kubernetes.core.k8s"]["definition"]["spec"]
    assert_complete_resources(vmagent["configReloaderResources"])

    grafana = task_named(tasks, "Install Grafana with Helm")["kubernetes.core.helm"]["values"]
    assert_complete_resources(grafana["initChownData"]["resources"])
    assert_complete_resources(grafana["downloadDashboards"]["resources"])
    assert_complete_resources(grafana["sidecar"]["resources"])

    alerting = load_tasks("roles/k8s-observability/tasks/alerting.yml")
    for name in ("Deploy VMAlertmanager CR", "Deploy VMAlert CR"):
        spec = task_named(alerting, name)["kubernetes.core.k8s"]["definition"]["spec"]
        assert_complete_resources(spec["configReloaderResources"])


def test_gitlab_vendor_init_sidecars_jobs_and_runner_have_limits() -> None:
    tasks = load_tasks("roles/gitlab-selfhosted/tasks/main.yml")
    install = task_named(tasks, "Install GitLab with Helm")
    values = install["kubernetes.core.helm"]["values"]
    gitlab = values["gitlab"]

    for component in (
        "webservice",
        "sidekiq",
        "gitlab-shell",
        "gitaly",
        "kas",
        "toolbox",
        "gitlab-exporter",
        "migrations",
    ):
        assert_complete_resources(gitlab[component]["init"]["resources"])

    assert_complete_resources(gitlab["webservice"]["workhorse"]["resources"])
    assert_complete_resources(gitlab["gitlab-exporter"]["resources"])
    assert_complete_resources(gitlab["migrations"]["resources"])
    assert_complete_resources(gitlab["toolbox"]["backups"]["cron"]["resources"])
    assert_complete_resources(values["registry"]["init"]["resources"])
    assert_complete_resources(values["upgradeCheck"]["resources"])
    assert_complete_resources(values["shared-secrets"]["resources"])

    runner = task_named(tasks, "Install GitLab Runner with Helm")
    runner_config = runner["kubernetes.core.helm"]["values"]["runners"]["config"]
    assert runner_config.count("cpu_limit") == 3
    assert runner_config.count("memory_limit") == 3
