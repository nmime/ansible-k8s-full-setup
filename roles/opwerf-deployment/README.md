# OpenWerf Deployment Role (ArgoCD + Helm)

Deploys the OpenWerf AI-powered workflow orchestration platform using **ArgoCD GitOps** with a **Helm chart** stored in **GitLab**.

## Overview

This role implements GitOps delivery:
1. **Generates secrets** and pre-populates them in the target namespace
2. **Creates a GitLab repository** (`platform/opwerf`) with the Helm chart
3. **Creates an ArgoCD Application** pointing to the Helm chart
4. **ArgoCD reconciles** all Kubernetes resources automatically

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              GITOPS FLOW                                    │
│                                                                             │
│  ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────────┐│
│  │  Ansible Role     │────▶│  GitLab Repo     │◀────│  ArgoCD              ││
│  │  (opwerf-deploy)  │     │  platform/opwerf │     │  (reconciles)        ││
│  │                   │     │  ├── Chart.yaml  │     │                      ││
│  │  • Secrets        │     │  ├── values.yaml │     │  Application:        ││
│  │  • GitLab project │     │  └── templates/  │     │  • source: gitlab    ││
│  │  • ArgoCD app     │     │                  │     │  • dest: opwerf ns   ││
│  └──────────────────┘     └──────────────────┘     │  • sync: automated   ││
│                                                     └──────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                        KUBERNETES RESOURCES                                 │
│                                                                             │
│  Namespace: opwerf                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ Dashboard   │  │ API         │  │ Worker      │  │ Cred Proxy  │        │
│  │ Deployment  │  │ Deployment  │  │ Deployment  │  │ Deployment  │        │
│  │ 1-2 replicas│  │ 1-2 replicas│  │ 1-2 replicas│  │ 1 replica   │        │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘        │
│                                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ Redis       │  │ Elastic     │  │ ConfigMap   │  │ Secret      │        │
│  │ StatefulSet │  │ StatefulSet │  │ opwerf-     │  │ opwerf-     │        │
│  │ 5Gi storage │  │ 10Gi storage│  │ config      │  │ secrets     │        │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘        │
│                                                                             │
│  ┌─────────────┐  ┌─────────────┐                                          │
│  │ HTTPRoute   │  │ HTTPRoute   │  ── Gateway API routing                  │
│  │ dashboard   │  │ api         │                                          │
│  │ app.domain  │  │ api.domain  │                                          │
│  └─────────────┘  └─────────────┘                                          │
│                                                                             │
│  ┌─────────────┐  ┌─────────────┐                                          │
│  │ HPA API     │  │ HPA Worker  │  ── Autoscaling                          │
│  │ 1-20 pods   │  │ 1-10 pods   │                                          │
│  └─────────────┘  └─────────────┘                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Prerequisites

### Required Infrastructure (deployed by other roles)
- **ArgoCD** (k8s-gitops role) — for GitOps reconciliation
- **GitLab** (gitlab-selfhosted role) — for Helm chart storage
- **PostgreSQL cluster** (k8s-databases role) — opwerf user auto-created
- **Temporal** (temporal role) — workflow engine
- **MinIO** (minio-storage role) — object storage
- **Gateway API** (k8s-cluster-management role) — ingress routing

## Helm Chart Structure

```
roles/opwerf-deployment/files/helm/
├── Chart.yaml          # Helm chart metadata
├── values.yaml         # Default values (overridden by ArgoCD)
└── templates/
    ├── _helpers.tpl    # Template helpers
    ├── configmap.yaml  # Application configuration
    ├── secret.yaml     # Sensitive credentials
    ├── redis.yaml      # Redis StatefulSet + Service
    ├── elasticsearch.yaml  # Elasticsearch StatefulSet + Service
    ├── api.yaml        # API Deployment + Service
    ├── worker.yaml     # Worker Deployment
    ├── credential-proxy.yaml  # Credential Proxy Deployment + Service
    ├── dashboard.yaml  # Dashboard Deployment + Service
    ├── httproute.yaml  # Gateway API HTTPRoutes
    └── hpa.yaml        # HorizontalPodAutoscalers
```

## Deployment Flow

```
1. ansible-playbook runs opwerf-deployment role
   │
2. ├── Generate secrets (passwords, keys)
   │
3. ├── Create namespace 'opwerf' with ArgoCD label
   │
4. ├── Pre-create opwerf-secrets in namespace
   │
5. ├── Create GitLab group 'platform' (if not exists)
   │
6. ├── Create GitLab project 'platform/opwerf' (if not exists)
   │
7. ├── Push Helm chart to GitLab repository
   │
8. ├── Add GitLab repo credentials to ArgoCD
   │
9. ├── Create ArgoCD AppProject 'opwerf'
   │
10.├── Create ArgoCD Application 'opwerf'
   │     └── source: gitlab://platform/opwerf (Helm)
   │     └── destination: opwerf namespace
   │     └── syncPolicy: automated + selfHeal
   │
11.└── ArgoCD syncs and deploys all resources
```

## Usage

### Via Platform Orchestrator

```bash
cd platform-orchestrator

# Edit platform.yaml
vim platform.yaml
# Set:
#   applications.opwerf.enabled: true

./platform.sh deploy opwerf
```

### Via Ansible Directly

```bash
ansible-playbook playbooks/deploy_platform.yml \
  -e tier=small \
  -e domain=example.com \
  -e email=admin@example.com \
  -e deploy_opwerf=true \
  -e opwerf_image_registry=registry.example.com \
  -e opwerf_image_tag=v1.0.0 \
  --tags opwerf
```

### Deploy with API Keys

```bash
ansible-playbook playbooks/deploy_platform.yml \
  -e tier=small \
  -e domain=example.com \
  -e email=admin@example.com \
  -e deploy_opwerf=true \
  -e opwerf_e2b_api_key="your-e2b-key" \
  -e opwerf_anthropic_api_key="your-anthropic-key" \
  --tags opwerf
```

## ArgoCD Integration

### View Application Status

```bash
# CLI
kubectl get application opwerf -n argocd
kubectl describe application opwerf -n argocd

# ArgoCD CLI
argocd app get opwerf
argocd app sync opwerf

# Web UI
open https://argocd.example.com/applications/opwerf
```

### Manual Sync

```bash
argocd app sync opwerf
# or
kubectl patch application opwerf -n argocd --type merge -p '{"operation":{"initiatedBy":{"username":"admin"},"sync":{}}}'
```

### Rollback

```bash
argocd app rollback opwerf <revision>
# or
argocd app history opwerf
argocd app rollback opwerf 3
```

## Updating OpenWerf

### Option 1: Update image tag via Ansible

```bash
ansible-playbook playbooks/deploy_platform.yml \
  -e deploy_opwerf=true \
  -e opwerf_image_tag=v1.2.0 \
  --tags opwerf
```

### Option 2: Update Helm chart in GitLab

```bash
git clone https://gitlab.example.com/platform/opwerf.git
cd opwerf
# Edit values.yaml or templates
git add . && git commit -m "update: ..."
git push origin main
# ArgoCD auto-syncs (selfHeal enabled)
```

### Option 3: Patch ArgoCD Application

```bash
kubectl patch application opwerf -n argocd --type json -p '[
  {"op": "replace", "path": "/spec/source/helm/parameters", "value": [
    {"name": "image.tag", "value": "v1.2.0"}
  ]}
]'
```

## Helm Values Reference

The ArgoCD Application passes these values to the Helm chart:

| Value | Description | Default |
|-------|-------------|----------|
| `global.domain` | Base domain | Required |
| `global.tier` | Deployment tier | `small` |
| `image.registry` | Container registry | `registry.{domain}` |
| `image.tag` | Image tag | `latest` |
| `replicas.api` | API replicas | 1 (2 for medium/prod) |
| `replicas.worker` | Worker replicas | 1 (2 for medium/prod) |
| `replicas.dashboard` | Dashboard replicas | 1 (2 for medium/prod) |
| `database.host` | PostgreSQL host | `{project}-pg-pgbouncer.databases.svc` |
| `database.password` | DB password | Auto-generated |
| `redis.storageSize` | Redis PVC size | `5Gi` |
| `elasticsearch.storageSize` | ES PVC size | `10Gi` |
| `temporal.address` | Temporal frontend | `temporal-frontend.temporal.svc:7233` |
| `ingress.dashboardHost` | Dashboard hostname | `app.{domain}` |
| `ingress.apiHost` | API hostname | `api.{domain}` |

## Monitoring

### ArgoCD Dashboard

```bash
open https://argocd.example.com/applications/opwerf
```

### Pod Status

```bash
kubectl get pods -n opwerf
kubectl logs -f deployment/opwerf-api -n opwerf
kubectl logs -f deployment/opwerf-worker -n opwerf
```

### Application Health

```bash
argocd app get opwerf --show-health
```

## Troubleshooting

### Application stuck in Syncing

```bash
# Check sync status
kubectl get application opwerf -n argocd -o jsonpath='{.status.sync}'

# Check operation state
kubectl get application opwerf -n argocd -o jsonpath='{.status.operationState}'

# Force refresh
argocd app refresh opwerf --hard
```

### GitLab repo not accessible

```bash
# Verify repo secret
kubectl get secret opwerf-helm-repo -n argocd -o yaml

# Test connectivity from ArgoCD
kubectl exec -n argocd deployment/argocd-repo-server -- git ls-remote https://gitlab.example.com/platform/opwerf.git
```

### Helm template errors

```bash
# Test Helm rendering locally
cd roles/opwerf-deployment/files/helm
helm template opwerf . -f values.yaml
```

### Database connection errors

```bash
# Verify PostgreSQL user exists
kubectl get secret -n databases | grep opwerf

# Check PgBouncer
kubectl exec -n databases <pgbouncer-pod> -- psql -h localhost -U opwerf -d opwerf -c 'SELECT 1'
```

## Removal

### Remove via ArgoCD

```bash
argocd app delete opwerf --cascade
# This removes all K8s resources managed by ArgoCD
```

### Remove manually

```bash
# Delete ArgoCD application
kubectl delete application opwerf -n argocd

# Delete namespace (removes all resources)
kubectl delete namespace opwerf

# Delete ArgoCD project
kubectl delete appproject opwerf -n argocd

# Delete GitLab repo (optional)
curl -X DELETE -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  https://gitlab.example.com/api/v4/projects/platform%2Fopwerf
```

## Security

- **Secrets pre-created** by Ansible before ArgoCD syncs
- **Database credentials** managed by Percona operator
- **Repo credentials** stored in ArgoCD secret (not in Application spec)
- **ArgoCD Application** does include inline Helm values with secrets (for simplicity)
  - For production, consider using **External Secrets Operator** + **Vault**
- **TLS** termination at Gateway level (cert-manager wildcard cert)

## License

Part of ansible-k8s-full-setup project.
