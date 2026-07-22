from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
POST_RENDERER = ROOT / "scripts" / "filebeat-containerd-post-renderer.sh"

FILEBEAT_MANIFEST = """
apiVersion: v1
kind: ServiceAccount
metadata:
  name: filebeat-filebeat
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: filebeat-filebeat
  labels:
    app: filebeat-filebeat
    chart: filebeat-8.5.1
    release: filebeat
spec:
  template:
    spec:
      volumes:
        - name: data
          hostPath:
            path: /var/lib/filebeat-filebeat-logging-agents-data
            type: DirectoryOrCreate
        - name: varlibdockercontainers
          hostPath:
            path: /var/lib/docker/containers
        - name: varlog
          hostPath:
            path: /var/log
        - name: varrundockersock
          hostPath:
            path: /var/run/docker.sock
      containers:
        - name: filebeat
          securityContext:
            privileged: false
            runAsUser: 0
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
          volumeMounts:
            - name: data
              mountPath: /usr/share/filebeat/data
            - name: varlibdockercontainers
              mountPath: /var/lib/docker/containers
              readOnly: true
            - name: varlog
              mountPath: /var/log
              readOnly: true
            - name: varrundockersock
              mountPath: /var/run/docker.sock
              readOnly: true
"""


def render(manifest: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POST_RENDERER)],
        input=manifest,
        text=True,
        capture_output=True,
        check=False,
    )


def test_post_renderer_removes_only_docker_host_access():
    result = render(FILEBEAT_MANIFEST)
    assert result.returncode == 0, result.stderr
    documents = list(yaml.safe_load_all(result.stdout))
    daemonset = next(document for document in documents if document["kind"] == "DaemonSet")
    pod_spec = daemonset["spec"]["template"]["spec"]

    host_paths = {
        volume["hostPath"]["path"]
        for volume in pod_spec["volumes"]
        if "hostPath" in volume
    }
    assert host_paths == {
        "/var/lib/filebeat-filebeat-logging-agents-data",
        "/var/log",
    }
    mounts = pod_spec["containers"][0]["volumeMounts"]
    assert {mount["mountPath"] for mount in mounts} == {
        "/usr/share/filebeat/data",
        "/var/log",
    }
    assert next(mount for mount in mounts if mount["mountPath"] == "/var/log")["readOnly"]
    assert pod_spec["containers"][0]["securityContext"]["privileged"] is False


def test_post_renderer_fails_closed_without_filebeat_daemonset():
    result = render("apiVersion: v1\nkind: Service\nmetadata:\n  name: unrelated\n")
    assert result.returncode == 1
    assert "expected exactly one Filebeat DaemonSet" in result.stderr


def test_post_renderer_fails_closed_without_required_containerd_log_mount():
    result = render(FILEBEAT_MANIFEST.replace("path: /var/log\n", "path: /other/log\n"))
    assert result.returncode == 1
    assert "must retain its /var/log hostPath" in result.stderr


def test_post_renderer_fails_closed_for_a_privileged_filebeat_container():
    result = render(FILEBEAT_MANIFEST.replace("privileged: false", "privileged: true"))
    assert result.returncode == 1
    assert "must remain non-privileged" in result.stderr


def test_role_installs_filebeat_through_the_containerd_post_renderer():
    tasks = (ROOT / "roles" / "k8s-observability" / "tasks" / "main.yml").read_text()
    filebeat_task = tasks.split("- name: Install Filebeat for log collection (ELK)", 1)[1]
    assert (
        "post_renderer: '{{ playbook_dir }}/../scripts/filebeat-containerd-post-renderer.sh'"
        in filebeat_task
    )
    assert "privileged: false" in filebeat_task
    assert "allowPrivilegeEscalation: false" in filebeat_task
    assert "drop: [ALL]" in filebeat_task
    assert "type: RuntimeDefault" in filebeat_task


def test_nonvendored_workload_roles_do_not_mount_docker_runtime_paths():
    offenders: dict[str, list[str]] = {}
    forbidden_paths = ("/var/run/docker.sock", "/var/lib/docker/containers")
    for path in (ROOT / "roles").rglob("*"):
        if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".sh"}:
            content = path.read_text(encoding="utf-8")
            matched = [forbidden for forbidden in forbidden_paths if forbidden in content]
            if matched:
                offenders[path.relative_to(ROOT).as_posix()] = matched
    assert offenders == {}


def test_promtail_overrides_docker_defaults_for_containerd():
    tasks = (ROOT / "roles" / "k8s-observability" / "tasks" / "main.yml").read_text()
    promtail_task = tasks.split("- name: Install Promtail for log collection", 1)[1].split(
        "- name: Remove legacy invalid Cilium default-deny for logging agents", 1
    )[0]
    assert "defaultVolumes:" in promtail_task
    assert "defaultVolumeMounts:" in promtail_task
    assert "path: /var/log/pods" in promtail_task
    assert "mountPath: /var/log/pods" in promtail_task
    assert "allowPrivilegeEscalation: false" in promtail_task
    assert "drop: [ALL]" in promtail_task
    assert "readOnlyRootFilesystem: true" in promtail_task
    assert "type: RuntimeDefault" in promtail_task
    assert "/var/lib/docker" not in promtail_task


def test_fluentd_uses_the_isolated_containerd_host_log_boundary():
    tasks = (ROOT / "roles" / "k8s-observability" / "tasks" / "main.yml").read_text()
    fluentd_task = tasks.split("- name: Install Fluentd for log collection (EFK)", 1)[
        1
    ].split("- name: Create Hetzner Cloud token secret", 1)[0]
    assert "release_namespace: '{{ logging_agent_namespace }}'" in fluentd_task
    assert "mountVarLogDirectory: false" in fluentd_task
    assert "mountDockerContainersDirectory: false" in fluentd_task
    assert "extraVolumes:" not in fluentd_task
    assert "extraVolumeMounts:" not in fluentd_task
    volumes = fluentd_task.split("      volumes:", 1)[1].split(
        "      securityContext:", 1
    )[0]
    assert "name: elasticsearch-ca" in volumes
    assert "secretName: es-tls-certs" in volumes
    assert "mountPath: /fluentd/certs" in volumes
    assert "readOnly: true" in volumes
    assert "path: /var/log" in fluentd_task
    assert "mountPath: /var/log\n        readOnly: true" in fluentd_task
    assert "securityContext:" in fluentd_task
    assert "privileged: false" in fluentd_task
    assert "allowPrivilegeEscalation: false" in fluentd_task
    assert "drop: [ALL]" in fluentd_task
    assert "readOnlyRootFilesystem: true" in fluentd_task
    assert "type: RuntimeDefault" in fluentd_task
    assert "path /fluentd/state/buffers/kubernetes.system.buffer" in fluentd_task
    assert "pos_file /fluentd/state/fluentd-containers.log.pos" in fluentd_task
    assert "@type regexp" in fluentd_task
    assert "(?<stream>stdout|stderr)" in fluentd_task
    assert "@type json" not in fluentd_task
    assert "write_operation create" in fluentd_task
    assert "/var/lib/docker" not in fluentd_task
    assert "/var/run/docker.sock" not in fluentd_task


def test_efk_gets_agent_namespace_secrets_policies_and_health_check():
    tasks = (ROOT / "roles" / "k8s-observability" / "tasks" / "main.yml").read_text()
    health = (
        ROOT / "roles" / "k8s-observability" / "tasks" / "health_checks.yml"
    ).read_text()
    elasticsearch = (ROOT / "roles" / "elasticsearch" / "tasks" / "main.yml").read_text()

    assert tasks.count('when: log_stack in ["loki", "elk", "efk"]') >= 7
    assert tasks.count('when: log_stack in ["elk", "efk"]') >= 2
    assert "Remove legacy Fluentd release from the baseline monitoring namespace" in tasks
    assert "Remove Fluentd node agents when EFK is not the selected log backend" in tasks
    assert "name: Check Fluentd node coverage" in health
    assert "status.desiredNumberScheduled" in health
    assert "status.numberReady" in health
    assert "name: allow-logging-agents-to-es" in elasticsearch
    assert "name: Remove legacy Filebeat-only Elasticsearch ingress policy" in elasticsearch
    legacy_policy_cleanup = elasticsearch.split(
        "- name: Remove legacy Filebeat-only Elasticsearch ingress policy", 1
    )[1].split(
        "- name: Remove logging-agent Elasticsearch ingress when Elasticsearch logging is deselected",
        1,
    )[0]
    assert "when:" not in legacy_policy_cleanup
    selected_policy_cleanup = elasticsearch.split(
        "- name: Remove logging-agent Elasticsearch ingress when Elasticsearch logging is deselected",
        1,
    )[1].split(
        "- name: Create CiliumNetworkPolicy for logging agents to Elasticsearch", 1
    )[0]
    assert "name: allow-logging-agents-to-es" in selected_policy_cleanup
    assert "not in ['elk', 'efk']" in selected_policy_cleanup
    replication = tasks.split(
        "- name: Replicate the minimum Elasticsearch credentials into the agent namespace",
        1,
    )[1].split("- name: Install Filebeat for log collection (ELK)", 1)[0]
    assert "logging-ingest-credentials" in replication
    assert "username:" in replication
    assert "password:" in replication
    assert "es-credentials" not in replication
    assert "Remove the legacy replicated Elasticsearch superuser secret" in tasks
    cleanup = tasks.split(
        "- name: Remove replicated logging credentials when Elasticsearch logging is deselected",
        1,
    )[1].split("- name: Read Elasticsearch secrets", 1)[0]
    assert "es-tls-certs" in cleanup
    assert "logging-ingest-credentials" in cleanup
    assert "no_log: true" in cleanup
    assert 'when: log_stack not in ["elk", "efk"]' in cleanup
    assert "platform_logging_ingest" in elasticsearch
    assert 'names: ["filebeat-*", "fluentd-*"]' in elasticsearch
    assert 'privileges: ["auto_configure", "create_doc", "create_index", "manage", "view_index_metadata"]' in elasticsearch
    assert "manage_ilm" in elasticsearch
    assert "manage_index_templates" in elasticsearch
    assert "Persist dedicated logging ingest credentials" in elasticsearch
    source_cleanup = elasticsearch.split(
        "- name: Remove stored logging ingest credentials when Elasticsearch logging is deselected",
        1,
    )[1].split("- name: Read the Kibana service-account token secret", 1)[0]
    assert "no_log: true" in source_cleanup
    logging_policy = tasks.split(
        "- name: Allow logging agents to reach the selected backend and Kubernetes discovery",
        1,
    )[1].split("- name: Fetch Elasticsearch credentials for Grafana datasource", 1)[0]
    assert "k8s:k8s-app: kube-dns" in logging_policy
    assert "- kube-apiserver" in logging_policy
    assert "serviceName:" in logging_policy
    assert "loki-gateway" in logging_policy
    assert "elasticsearch" in logging_policy
    assert "- cluster" not in logging_policy


def test_logging_ingest_password_is_stable_and_vault_persisted():
    secrets = (ROOT / "roles" / "generate-secrets" / "tasks" / "main.yml").read_text()
    assert "saved_secrets.es_logging_ingest_password" in secrets
    assert "generated_es_logging_ingest_password" in secrets
    assert secrets.count('es_logging_ingest_password: "{{ es_logging_ingest_password }}"') == 2
