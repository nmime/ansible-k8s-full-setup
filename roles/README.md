# Roles Directory

This directory contains Ansible roles for each component:

- `hetzner-infra/` - Hetzner Cloud infrastructure provisioning
- `k8s-cluster-management/` - Kubernetes cluster installation via Kubespray
- `network-security/` - VPN and firewall configuration
- `k8s-secrets/` - Vault and External Secrets Operator setup
- `k8s-databases/` - PostgreSQL and MongoDB deployments
- `minio-storage/` - MinIO S3 storage deployment
- `gitlab-selfhosted/` - GitLab CE deployment
- `k8s-gitops/` - ArgoCD GitOps deployment
- `k8s-observability/` - VictoriaMetrics, Loki, and Grafana
- `k8s-autoscaling/` - KEDA event-driven autoscaling

## Role Structure

Each role follows Ansible best practices:
- `tasks/` - Task files (main.yml, handlers.yml)
- `defaults/` - Default variables
- `templates/` - Jinja2 templates
- `files/` - Static files
- `vars/` - Role-specific variables
- `handlers/` - Handlers for restarting services
- `meta/` - Role metadata

## Usage

To use a role in a playbook:
```yaml
- hosts: your_hosts
  roles:
    - role: role_name
      tier: medium
      state: present
```