#!/usr/bin/env python3
"""Conservative Hetzner CSI volume estimator for profile migrations."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

QUANTITY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti)$")


def nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    value: Any = data
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def enabled(data: dict[str, Any], path: str, default: bool = False) -> bool:
    return bool(nested(data, path, default))


def requested_gib(value: Any) -> int:
    match = QUANTITY.fullmatch(str(value))
    if not match:
        raise ValueError(f"unsupported Kubernetes storage quantity: {value}")
    number = Decimal(match.group(1))
    multiplier = {"Ki": Decimal(1) / 1048576, "Mi": Decimal(1) / 1024,
                  "Gi": Decimal(1), "Ti": Decimal(1024)}[match.group(2)]
    return math.ceil(number * multiplier)


def billable_gib(value: Any) -> int:
    # Hetzner volumes are provisioned in whole GiB with a 10 GiB minimum.
    return max(10, requested_gib(value))


def estimate(config: dict[str, Any]) -> dict[str, Any]:
    tier = str(config.get("tier", "custom"))
    resource = str(config.get("resource_tier", tier))
    claims: dict[str, dict[str, Any]] = {}
    default_storage_class = str(
        nested(config, "storage.storage_class", config.get("storage_class", "hcloud-volumes"))
    )

    def storage_class(path: str, fallback: str = default_storage_class) -> str:
        return str(nested(config, path, fallback))

    def add(
        key: str,
        replicas: Any,
        size: Any,
        source: str,
        claim_storage_class: str = default_storage_class,
    ) -> None:
        count = int(replicas)
        if count < 1:
            raise ValueError(f"{key} replica count must be positive")
        per_volume = billable_gib(size)
        requested_per_volume = requested_gib(size)
        provider_billable = claim_storage_class == "hcloud-volumes"
        claims[key] = {
            "replicas": count,
            "requested_size": str(size),
            "requested_per_volume_gib": requested_per_volume,
            "requested_total_gib": count * requested_per_volume,
            "billable_per_volume_gib": per_volume,
            "total_gib": count * per_volume,
            "storage_class": claim_storage_class,
            "provider_billable_gib": count * per_volume if provider_billable else 0,
            "local_reserved_gib": count * per_volume if not provider_billable else 0,
            "source": source,
        }

    if enabled(config, "storage.enabled", True):
        default_ha = resource in {"medium", "production"}
        masters = nested(config, "storage.master_replicas",
                         nested(config, "storage.replicas", 3 if default_ha else 1))
        volumes = nested(config, "storage.volume_replicas", masters)
        filers = nested(config, "storage.filer_replicas", 2 if default_ha else 1)
        default_volume = {"minimal": "20Gi", "small": "40Gi", "medium": "100Gi"}.get(resource, "150Gi")
        add(
            "object-storage/master",
            masters,
            nested(config, "storage.master_size", "4Gi"),
            "SeaweedFS master",
            storage_class("storage.master_storage_class"),
        )
        add("object-storage/volume", volumes,
            nested(config, "storage.size_per_replica", nested(config, "storage.size", default_volume)),
            "SeaweedFS volume",
            storage_class("storage.volume_storage_class"))
        if enabled(config, "storage.index_persistent", True):
            add("object-storage/index", volumes,
                nested(config, "storage.index_size", "4Gi"),
                "SeaweedFS volume indexes",
                storage_class(
                    "storage.index_storage_class",
                    storage_class("storage.volume_storage_class"),
                ))
        add(
            "object-storage/filer",
            filers,
            nested(config, "storage.filer_size", "10Gi"),
            "SeaweedFS filer",
            storage_class("storage.filer_storage_class"),
        )

    if enabled(config, "secrets.enabled", True):
        replicas = nested(config, "secrets.vault.replicas", 3 if resource in {"medium", "production"} else 1)
        size = nested(config, "secrets.vault.storage_size", "20Gi")
        add(
            "vault/data",
            replicas,
            size,
            "Vault Raft data",
            storage_class("secrets.vault.data_storage_class"),
        )
        add(
            "vault/audit",
            replicas,
            nested(config, "secrets.vault.audit_storage_size", size),
            "Vault audit",
            storage_class("secrets.vault.audit_storage_class"),
        )

    databases = enabled(config, "databases.enabled", True)
    if databases and enabled(config, "databases.postgresql.enabled", True):
        replicas = nested(config, "databases.postgresql.replicas", 2 if resource in {"medium", "production"} else 1)
        default_pg = {"minimal": "20Gi", "small": "30Gi", "medium": "50Gi"}.get(resource, "100Gi")
        add(
            "postgresql/data",
            replicas,
            nested(config, "databases.postgresql.storage_size", default_pg),
            "Percona PostgreSQL instances",
            storage_class("databases.postgresql.data_storage_class"),
        )
        add(
            "postgresql/repo1",
            1,
            nested(config, "databases.postgresql.repo_storage_size", "10Gi"),
            "pgBackRest local repository",
            storage_class("databases.postgresql.repo_storage_class"),
        )
    if databases and enabled(config, "databases.mongodb.enabled", False):
        replicas = nested(config, "databases.mongodb.replicas", 3 if resource in {"medium", "production"} else 1)
        default_mongo = {"minimal": "20Gi", "small": "30Gi", "medium": "50Gi"}.get(resource, "100Gi")
        add(
            "mongodb/data",
            replicas,
            nested(config, "databases.mongodb.storage_size", default_mongo),
            "Percona MongoDB members",
            storage_class("databases.mongodb.data_storage_class"),
        )

    if enabled(config, "elasticsearch.enabled", tier in {"medium", "production"}):
        master_replicas = nested(config, "elasticsearch.master.replicas", 3 if resource in {"medium", "production"} else 1)
        data_replicas = nested(config, "elasticsearch.data.replicas", 2 if resource == "production" else 1)
        add("elasticsearch/master", master_replicas,
            nested(config, "elasticsearch.master.storage_size", "30Gi" if resource == "production" else "20Gi"),
            "Elasticsearch masters",
            storage_class("elasticsearch.master.storage_class"))
        add("elasticsearch/data", data_replicas,
            nested(config, "elasticsearch.data.storage_size", "150Gi" if resource == "production" else "100Gi" if resource == "medium" else "20Gi"),
            "Elasticsearch data",
            storage_class("elasticsearch.data.storage_class"))

    if enabled(config, "dragonfly.enabled", tier in {"medium", "production"}):
        add("dragonfly/data", nested(config, "dragonfly.replicas", 2 if resource in {"medium", "production"} else 1),
            nested(config, "dragonfly.snapshot_storage", "20Gi" if resource in {"medium", "production"} else "10Gi"),
            "Dragonfly snapshots")

    gitlab_enabled = enabled(config, "gitlab.enabled", tier != "minimal")
    if gitlab_enabled:
        default_gitaly = "10Gi" if resource == "minimal" else "20Gi" if resource == "small" else "50Gi"
        add("gitlab/gitaly", nested(config, "gitlab.gitaly_replicas", 1),
            nested(config, "gitlab.gitaly_storage_size", default_gitaly), "GitLab Gitaly")

    if enabled(config, "observability.enabled", True):
        metrics_size = nested(config, "observability.metrics.storage_size",
                              {"minimal": "20Gi", "small": "40Gi", "medium": "100Gi"}.get(resource, "150Gi"))
        metrics_replicas = nested(config, "observability.metrics.replicas", 2 if resource in {"medium", "production"} else 1)
        if tier in {"minimal", "small"}:
            add("metrics/vmsingle", 1, metrics_size, "VictoriaMetrics VMSingle")
        else:
            add("metrics/vmstorage", metrics_replicas, metrics_size, "VictoriaMetrics VMStorage")
        stack = str(nested(config, "observability.logging.stack", "loki" if tier in {"minimal", "small"} else "elk"))
        if stack == "loki":
            loki_mode = str(
                nested(
                    config,
                    "observability.logging.deployment_mode",
                    "single-binary" if tier in {"minimal", "small"} else "simple-scalable",
                )
            )
            loki_size = "10Gi" if resource in {"minimal", "small"} else "20Gi"
            if loki_mode == "single-binary":
                add("logging/loki", 1, loki_size, "Loki single-binary")
            elif loki_mode == "simple-scalable":
                add(
                    "logging/loki-write",
                    metrics_replicas,
                    loki_size,
                    "Loki simple-scalable write",
                )
                add(
                    "logging/loki-backend",
                    metrics_replicas,
                    loki_size,
                    "Loki simple-scalable backend",
                )
            else:
                raise ValueError(f"unsupported Loki deployment mode: {loki_mode}")
        if enabled(config, "observability.grafana.enabled", True):
            add("observability/grafana", 1, "10Gi", "Grafana SQLite")
        alert_replicas = nested(config, "alerting.replicas", 2 if resource in {"medium", "production"} else 1)
        add("observability/alertmanager", alert_replicas,
            nested(config, "alerting.storage_size", "5Gi"), "VMAlertmanager")
        if enabled(config, "observability.pmm.enabled", tier in {"medium", "production"}):
            add("observability/pmm", 1, nested(config, "observability.pmm.storage_size",
                                               "20Gi" if resource in {"minimal", "small"} else "50Gi"), "PMM")

    if enabled(config, "coroot.enabled", tier in {"medium", "production"}):
        add("coroot/data", 1, nested(config, "coroot.storage_size",
                                     "10Gi" if resource in {"minimal", "small"} else "20Gi" if resource == "medium" else "40Gi"), "Coroot")
        add("coroot/clickhouse", 1, nested(config, "coroot.clickhouse.storage_size",
                                           "20Gi" if resource in {"minimal", "small"} else "50Gi" if resource == "medium" else "100Gi"), "Coroot ClickHouse")
        add("coroot/keeper", 3, "10Gi", "Coroot ClickHouse Keeper")
    tracing_is_enabled = enabled(config, "tracing.enabled", tier in {"medium", "production"})
    tracing_backend = str(nested(config, "tracing.backend", "tempo"))
    tempo_is_enabled = bool(
        nested(
            config,
            "tracing.tempo.enabled",
            tracing_is_enabled and tracing_backend == "tempo",
        )
    )
    if tracing_is_enabled and tempo_is_enabled:
        add("tracing/tempo", 1, nested(config, "tracing.storage_size",
                                       "10Gi" if resource in {"minimal", "small"} else "20Gi" if resource == "medium" else "40Gi"), "Tempo")
    # Postal is an application opt-in in every tier. Missing selectors must
    # fail closed instead of deriving enablement from the capability tier.
    if enabled(config, "postal.enabled", False):
        add("postal/mariadb", 1, nested(config, "postal.mariadb_storage",
                                        "50Gi" if resource in {"medium", "production"} else "20Gi"), "Postal MariaDB")

    scratch = 0
    scratch_storage_class = default_storage_class
    if gitlab_enabled and bool(nested(config, "gitlab.backup_persistence_enabled", True)):
        scratch = billable_gib(nested(config, "gitlab.backup_persistence_size", "50Gi"))
        scratch_storage_class = storage_class("gitlab.backup_storage_class")
    return {
        "profile": str(config.get("platform_profile", tier)),
        "tier": tier,
        "resource_tier": resource,
        "provider_minimum_volume_gib": 10,
        "claims": claims,
        "persistent_total_gib": sum(item["total_gib"] for item in claims.values()),
        "requested_persistent_total_gib": sum(
            item["requested_total_gib"] for item in claims.values()
        ),
        "provider_persistent_gib": sum(
            item["provider_billable_gib"] for item in claims.values()
        ),
        "local_reserved_gib": sum(item["local_reserved_gib"] for item in claims.values()),
        "backup_scratch_gib": scratch,
        "backup_scratch_storage_class": scratch_storage_class,
        "provider_backup_scratch_gib": (
            scratch if scratch_storage_class == "hcloud-volumes" else 0
        ),
        "local_backup_scratch_gib": (
            scratch if scratch_storage_class != "hcloud-volumes" else 0
        ),
    }


def migration(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    source_estimate = estimate(source)
    target_estimate = estimate(target)
    source_claims = source_estimate["claims"]
    target_claims = target_estimate["claims"]
    delta: dict[str, int] = {}
    storage_class_changes: dict[str, dict[str, str]] = {}
    for key, claim in target_claims.items():
        source_claim = source_claims.get(key, {})
        class_changed = (
            bool(source_claim)
            and source_claim.get("storage_class") != claim.get("storage_class")
        )
        if class_changed:
            storage_class_changes[key] = {
                "source": str(source_claim.get("storage_class", "")),
                "target": str(claim.get("storage_class", "")),
            }
        growth = (
            claim["total_gib"]
            if class_changed
            else max(0, claim["total_gib"] - source_claim.get("total_gib", 0))
        )
        if growth:
            delta[key] = growth
    scratch = max(source_estimate["backup_scratch_gib"], target_estimate["backup_scratch_gib"])
    return {
        "schema_version": 1,
        "source": source_estimate,
        "target": target_estimate,
        "target_delta_by_claim_gib": delta,
        "target_delta_gib": sum(delta.values()),
        "storage_class_changes": storage_class_changes,
        "retained_source_gib": sum(
            claim["total_gib"] for key, claim in source_claims.items() if key not in target_claims
        ),
        "migration_scratch_gib": scratch,
        "required_additional_gib": sum(delta.values()) + scratch,
    }


def load(path: str) -> dict[str, Any]:
    result = subprocess.run(
        ["yq", "-o=json", ".", str(Path(path))],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError(f"profile config is not a mapping: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    print(json.dumps(migration(load(args.source), load(args.target)), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
