#!/usr/bin/env bash
set -euo pipefail

# Keep Vault's private-cluster DNS override in Helm's desired state. A
# follow-up imperative patch would otherwise make every Helm reconciliation
# restart sealed pods.
# Chart 0.34 renders maxUnavailable=0 for a one-replica HA release even when
# values request one. Keep the maintenance PDB in Helm's rendered ownership so
# Helm 4 upgrades do not conflict with an imperative patch.
# shellcheck disable=SC2016 # the pod-name substitution must remain literal YAML.
yq eval '
  # The chart advertises the Pod IP as VAULT_API_ADDR. Standby redirects then
  # bypass the certificate DNS SAN and break Raft snapshot clients. Reuse the
  # chart-owned (unique) variable but publish the stable per-pod headless DNS
  # name that is covered by *.vault-internal.
  (select(.kind == "StatefulSet" and .metadata.name == "vault") |
    .spec.template.spec.containers[] |
    select(.name == "vault") |
    .env[] |
    select(.name == "VAULT_API_ADDR") |
    .value) |= sub("\\$\\(POD_IP\\)"; "$(VAULT_K8S_POD_NAME).vault-internal") |
  (select(.kind == "StatefulSet" and .metadata.name == "vault") |
    .spec.template.spec.dnsPolicy) = "None" |
  (select(.kind == "StatefulSet" and .metadata.name == "vault") |
    .spec.template.spec.dnsConfig) = {
      "nameservers": ["10.233.0.3"],
      "searches": [
        "vault.svc.cluster.local",
        "svc.cluster.local",
        "cluster.local"
      ],
      "options": [{"name": "ndots", "value": "5"}]
    } |
  (select(.kind == "PodDisruptionBudget" and .metadata.name == "vault") |
    .spec.maxUnavailable) = 1
' -
