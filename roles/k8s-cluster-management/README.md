# k8s-cluster-management

Installs and reconciles the Kubernetes cluster via Kubespray with Cilium
CNI/Hubble/encryption, Gateway API, cert-manager, Hetzner CCM/CSI webhook,
and MetalLB.

## Key variables

Kubespray version, Cilium version, Gateway API CRD version, and container
runtime settings are pinned in `defaults/main.yml`.

## Where applied

Core cluster bootstrap role in `playbooks/deploy_platform.yml`, executed after
`hetzner-infra`.
