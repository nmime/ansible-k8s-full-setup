# Public repository boundary

This repository contains reusable platform contracts only: Ansible roles,
playbooks, generic defaults, example configuration, static validation, and
provider-neutral operational guidance.

Deployment repositories must remain private and own all live-cluster state,
including:

- real domains, public addresses, node names, topology, and service inventory;
- Argo CD applications and environment overlays;
- access maps, repository inventories, incident reports, and current health;
- credentials, encrypted recovery material, secret references tied to a live
  deployment, and provider resource identifiers.

Public examples use reserved domains and address ranges. The boundary test in
`tests/test_public_repository_boundary.py` rejects known deployment identifiers,
real service domains, live public addresses, and private-key material.

Changes flow from this base into a private cluster-truth repository. Live
overrides never flow back unless they are first reduced to a generic contract.
