#!/usr/bin/env python3
"""Remove Docker-only host mounts from the pinned Filebeat Helm chart.

The Elastic Filebeat 8.5.1 chart unconditionally renders Docker's container
directory and control socket.  This platform uses containerd and reads CRI log
symlinks from /var/log/containers, so neither Docker path is required.  The
post-renderer deliberately fails closed if the expected Filebeat DaemonSet or
the required /var/log mount is missing.
"""

from __future__ import annotations

import sys
from typing import Any

import yaml


DOCKER_ONLY_PATHS = {"/var/lib/docker/containers", "/var/run/docker.sock"}
REQUIRED_LOG_PATH = "/var/log"


def _is_filebeat_daemonset(document: Any) -> bool:
    if not isinstance(document, dict):
        return False
    if document.get("apiVersion") != "apps/v1" or document.get("kind") != "DaemonSet":
        return False
    labels = document.get("metadata", {}).get("labels", {})
    return labels.get("release") == "filebeat" and str(labels.get("chart", "")).startswith(
        "filebeat-"
    )


def _remove_docker_mounts(document: dict[str, Any]) -> None:
    try:
        pod_spec = document["spec"]["template"]["spec"]
        volumes = pod_spec["volumes"]
        containers = pod_spec["containers"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Filebeat DaemonSet has an unexpected pod specification") from exc

    if not isinstance(volumes, list) or not isinstance(containers, list):
        raise ValueError("Filebeat DaemonSet volumes and containers must be lists")

    docker_volume_names = {
        volume.get("name")
        for volume in volumes
        if isinstance(volume, dict)
        and volume.get("hostPath", {}).get("path") in DOCKER_ONLY_PATHS
    }
    pod_spec["volumes"] = [
        volume
        for volume in volumes
        if not (
            isinstance(volume, dict)
            and volume.get("hostPath", {}).get("path") in DOCKER_ONLY_PATHS
        )
    ]

    for container in containers + pod_spec.get("initContainers", []):
        if not isinstance(container, dict):
            continue
        mounts = container.get("volumeMounts", [])
        if not isinstance(mounts, list):
            raise ValueError("Filebeat container volumeMounts must be a list")
        container["volumeMounts"] = [
            mount
            for mount in mounts
            if not (
                isinstance(mount, dict)
                and (
                    mount.get("name") in docker_volume_names
                    or mount.get("mountPath") in DOCKER_ONLY_PATHS
                )
            )
        ]

    remaining_host_paths = {
        volume.get("hostPath", {}).get("path")
        for volume in pod_spec["volumes"]
        if isinstance(volume, dict) and isinstance(volume.get("hostPath"), dict)
    }
    if remaining_host_paths & DOCKER_ONLY_PATHS:
        raise ValueError("Docker-only Filebeat host paths remain after post-rendering")
    if REQUIRED_LOG_PATH not in remaining_host_paths:
        raise ValueError("Filebeat must retain its /var/log hostPath for containerd CRI logs")

    filebeat_containers = [
        container
        for container in containers
        if isinstance(container, dict) and container.get("name") == "filebeat"
    ]
    if len(filebeat_containers) != 1:
        raise ValueError("expected exactly one Filebeat container")
    security_context = filebeat_containers[0].get("securityContext", {})
    if security_context.get("privileged") is not False:
        raise ValueError("Filebeat container must remain non-privileged")
    if security_context.get("allowPrivilegeEscalation") is not False:
        raise ValueError("Filebeat container must disable privilege escalation")
    dropped_capabilities = security_context.get("capabilities", {}).get("drop", [])
    if "ALL" not in dropped_capabilities:
        raise ValueError("Filebeat container must drop all Linux capabilities")
    log_mounts = [
        mount
        for mount in filebeat_containers[0].get("volumeMounts", [])
        if isinstance(mount, dict) and mount.get("mountPath") == REQUIRED_LOG_PATH
    ]
    if len(log_mounts) != 1 or log_mounts[0].get("readOnly") is not True:
        raise ValueError("Filebeat must retain a read-only /var/log mount")


def main() -> int:
    try:
        documents = [
            document for document in yaml.safe_load_all(sys.stdin) if document is not None
        ]
        filebeat_daemonsets = [
            document for document in documents if _is_filebeat_daemonset(document)
        ]
        if len(filebeat_daemonsets) != 1:
            raise ValueError(
                f"expected exactly one Filebeat DaemonSet, found {len(filebeat_daemonsets)}"
            )
        _remove_docker_mounts(filebeat_daemonsets[0])
        yaml.safe_dump_all(documents, sys.stdout, explicit_start=True, sort_keys=False)
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"filebeat post-render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
