from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APM = (ROOT / "roles/apm-server/tasks/main.yml").read_text(encoding="utf-8")
ES = (ROOT / "roles/elasticsearch/tasks/main.yml").read_text(encoding="utf-8")
CLUSTER = (ROOT / "roles/k8s-cluster-management/tasks/main.yml").read_text(
    encoding="utf-8"
)


def test_apm_image_entrypoint_is_not_duplicated_in_args():
    deploy = APM.split("- name: Deploy APM Server", 1)[1].split("\n- name:", 1)[0]
    args = deploy.split("args:", 1)[1].split("ports:", 1)[0]
    assert "- apm-server" not in args
    assert "- -e" in args


def test_apm_wait_gate_requires_ready_containers():
    wait = APM.split("- name: Wait for APM Server pods ready", 1)[1].split(
        "\n- name:", 1
    )[0]
    assert "containerStatuses" in wait
    assert "selectattr('ready', 'equalto', true)" in wait


def test_apm_pods_use_restricted_security_contexts():
    for contract in (
        "runAsNonRoot: true",
        "seccompProfile:",
        "allowPrivilegeEscalation: false",
        'drop: ["ALL"]',
    ):
        assert contract in APM


def test_elasticsearch_sysctl_is_persisted_by_kubespray_not_privileged_pods():
    assert "vm.max_map_count" in CLUSTER
    assert "additional_sysctl:" in CLUSTER
    assert "privileged: true" not in ES
    assert "name: sysctl" not in ES
