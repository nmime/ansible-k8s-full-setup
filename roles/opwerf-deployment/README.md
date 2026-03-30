# OpenWerf Deployment Role

Deploys the OpenWerf AI-powered workflow orchestration platform on Kubernetes.

## Overview

OpenWerf is a comprehensive workflow orchestration platform that combines:
- **Dashboard**: React-based web interface for workflow management
- **API**: NestJS backend for workflow execution and management
- **Worker**: Temporal-based workflow workers
- **Credential Proxy**: Secure credential management service
- **Redis**: Caching and session storage
- **Elasticsearch**: Advanced search and indexing

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Cilium Gateway API                       │
│  https://app.example.com  │  https://api.example.com       │
└──────────────┬───────────────────────┬──────────────────────┘
               │                       │
    ┌──────────▼─────────┐  ┌──────────▼─────────┐
    │   Dashboard        │  │   API              │
    │   (nginx + React)  │  │   (NestJS)         │
    │   2 replicas       │  │   2 replicas       │
    └────────────────────┘  └─────────┬──────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              │                       │                       │
    ┌─────────▼─────────┐  ┌──────────▼─────────┐ ┌─────────▼─────────┐
    │   Worker          │  │ Credential Proxy   │ │   Redis           │
    │   (Temporal)      │  │   (Internal Auth)  │ │   (StatefulSet)   │
    │   2 replicas      │  │   1 replica        │ │   1 replica       │
    └───────────────────┘  └────────────────────┘ └───────────────────┘
              │                       │                       │
              └───────────────────────┼───────────────────────┘
                                      │
         ┌────────────────────────────┼────────────────────────┐
         │                            │                        │
┌────────▼─────────┐   ┌──────────────▼────────┐  ┌──────────▼──────────┐
│  PostgreSQL      │   │  Temporal             │  │  MinIO              │
│  (Percona)       │   │  (temporal ns)        │  │  (storage ns)       │
│  databases ns    │   │                       │  │                     │
└──────────────────┘   └───────────────────────┘  └─────────────────────┘
         │
┌────────▼─────────┐
│  Elasticsearch   │
│  (StatefulSet)   │
│  opwerf ns       │
└──────────────────┘
```

## Prerequisites

### Required Infrastructure (deployed by other roles)
- **Kubernetes cluster** (k8s-cluster-management role)
- **PostgreSQL cluster** (k8s-databases role) - opwerf user auto-created
- **Temporal** (temporal role) - opwerf namespace created
- **MinIO** (minio-storage role) - opwerf-artifacts bucket auto-created
- **Gateway API** (k8s-cluster-management role) - main-gateway in gateway namespace

### Required Configuration
- `domain` - Base domain (e.g., `example.com`)
- `opwerf_e2b_api_key` - E2B API key (optional)
- `opwerf_anthropic_api_key` - Anthropic API key (optional)
- `opwerf_image_registry` - Container registry (default: `registry.{{ domain }}`)
- `opwerf_image_tag` - Image tag (default: `latest`)

## Components Deployed

### Namespace: `opwerf`

| Resource | Type | Replicas | Storage |
|----------|------|----------|----------|
| **opwerf-redis** | StatefulSet | 1 | 5Gi (tier-based) |
| **opwerf-elasticsearch** | StatefulSet | 1 | 10Gi (tier-based) |
| **opwerf-api** | Deployment | 1-2 (tier) | - |
| **opwerf-worker** | Deployment | 1-2 (tier) | - |
| **opwerf-credential-proxy** | Deployment | 1 | - |
| **opwerf-dashboard** | Deployment | 1-2 (tier) | - |

### Secrets
- **opwerf-secrets**: All sensitive credentials (DB, Redis, MinIO, API keys, encryption keys)

### ConfigMaps
- **opwerf-config**: Application configuration (DB host, Temporal address, MinIO endpoint, etc.)
- **opwerf-redis-config**: Redis configuration

### Services
- **opwerf-api** (ClusterIP): API service on port 8080
- **opwerf-dashboard** (ClusterIP): Dashboard service on port 80
- **opwerf-credential-proxy** (ClusterIP): Credential proxy on port 4000
- **opwerf-redis** (Headless): Redis on port 6379
- **opwerf-elasticsearch** (Headless): Elasticsearch on ports 9200/9300

### HTTPRoutes (Gateway API)
- **opwerf-dashboard**: `https://app.{{ domain }}` → opwerf-dashboard:80
- **opwerf-api**: `https://api.{{ domain }}` → opwerf-api:8080

### Autoscaling (HPA)
- **opwerf-api-hpa**: 1-20 replicas (CPU 70%, Memory 80%)
- **opwerf-worker-hpa**: 1-10 replicas (CPU 70%)

## Default Variables (roles/opwerf-deployment/defaults/main.yml)

```yaml
# Application settings
opwerf_namespace: opwerf
opwerf_app_name: opwerf
opwerf_domain: app.{{ domain }}
opwerf_api_domain: api.{{ domain }}

# Tier-based replication
opwerf_api_replicas: "{{ 2 if tier in ['medium', 'production'] else 1 }}"
opwerf_dashboard_replicas: "{{ 2 if tier in ['medium', 'production'] else 1 }}"
opwerf_worker_replicas: "{{ 2 if tier in ['medium', 'production'] else 1 }}"

# Database (Percona PostgreSQL)
opwerf_db_name: opwerf
opwerf_db_user: opwerf
opwerf_pg_host: "{{ project_name }}-pg-pgbouncer.databases.svc.cluster.local"
opwerf_pg_port: 5432

# Redis
opwerf_redis_storage_size: 5Gi

# Elasticsearch
opwerf_elasticsearch_replicas: 1
opwerf_elasticsearch_storage_size: 10Gi

# Temporal
opwerf_temporal_address: temporal-frontend.temporal.svc.cluster.local:7233
opwerf_temporal_namespace: opwerf

# MinIO
opwerf_minio_endpoint: minio.storage.svc.cluster.local
opwerf_minio_port: 9000
opwerf_minio_bucket: opwerf-artifacts
```

## Deployment

### Via Platform Orchestrator

```bash
cd platform-orchestrator

# Edit platform.yaml
vim platform.yaml
# Set:
#   applications.opwerf.enabled: true
#   applications.opwerf.e2b_api_key: "your-key"
#   applications.opwerf.anthropic_api_key: "your-key"

# Deploy
./platform.sh deploy opwerf
```

### Via Ansible Directly

```bash
ansible-playbook playbooks/deploy_platform.yml \
  -e tier=small \
  -e domain=example.com \
  -e email=admin@example.com \
  -e deploy_opwerf=true \
  -e opwerf_e2b_api_key=your-key \
  -e opwerf_anthropic_api_key=your-key \
  --tags opwerf
```

### Deploy Specific Component Only

```bash
# Deploy only OpenWerf (assumes infrastructure already exists)
ansible-playbook playbooks/deploy_platform.yml \
  -i inventory.yml \
  --tags opwerf
```

## Tier Scaling

| Tier | API | Worker | Dashboard | Redis | Elasticsearch | Total Cost Addition |
|------|-----|--------|-----------|-------|---------------|---------------------|
| **minimal** | 1 | 1 | 1 | 1 (5Gi) | 1 (10Gi) | ~€2-3/mo |
| **small** | 1 | 1 | 1 | 1 (5Gi) | 1 (10Gi) | ~€2-3/mo |
| **medium** | 2 | 2 | 2 | 1 (5Gi) | 1 (10Gi) | ~€4-5/mo |
| **production** | 2 | 2 | 2 | 1 (5Gi) | 1 (10Gi) | ~€4-5/mo |

*Note: Uses shared PostgreSQL, Temporal, and MinIO infrastructure*

## Secret Management

All secrets are auto-generated by the `generate-secrets` role and persisted to `.platform-secrets.yml`:

- `opwerf_db_password` - PostgreSQL password (also created by Percona operator)
- `opwerf_redis_password` - Redis password
- `opwerf_credential_proxy_token` - Internal auth token
- `opwerf_encryption_key` - Data encryption key (32 hex chars)
- `opwerf_encryption_salt` - Encryption salt (16 hex chars)
- `opwerf_session_signing_key` - JWT signing key (64 hex chars)

## Accessing OpenWerf

```bash
# Get credentials
./platform-orchestrator/platform.sh credentials

# Access dashboard
open https://app.example.com

# Access API
curl https://api.example.com/health

# Check deployment status
kubectl get pods -n opwerf
kubectl get httproute -n opwerf
```

## Building and Pushing Images

```bash
# From opwerf repository root
cd /path/to/opwerf

# Build images
docker build -f docker/Dockerfile.api -t registry.example.com/opwerf/api:latest .
docker build -f docker/Dockerfile.worker -t registry.example.com/opwerf/worker:latest .
docker build -f docker/Dockerfile.credential-proxy -t registry.example.com/opwerf/credential-proxy:latest .
docker build -f docker/Dockerfile.dashboard -t registry.example.com/opwerf/dashboard:latest .

# Push to GitLab registry (deployed by gitlab-selfhosted role)
docker login registry.example.com
docker push registry.example.com/opwerf/api:latest
docker push registry.example.com/opwerf/worker:latest
docker push registry.example.com/opwerf/credential-proxy:latest
docker push registry.example.com/opwerf/dashboard:latest

# Or use GitLab CI/CD (recommended)
# Push to GitLab, let runners build and push automatically
```

## Troubleshooting

### Pods not starting
```bash
# Check pod status
kubectl get pods -n opwerf
kubectl describe pod opwerf-api-xxx -n opwerf
kubectl logs opwerf-api-xxx -n opwerf

# Common issues:
# 1. Image pull errors - check registry credentials
# 2. Database connection errors - verify PostgreSQL is ready
# 3. Redis connection errors - verify Redis pod is running
```

### HTTPRoute not working
```bash
# Check HTTPRoute status
kubectl get httproute -n opwerf
kubectl describe httproute opwerf-dashboard -n opwerf

# Check Gateway
kubectl get gateway -n gateway
kubectl describe gateway main-gateway -n gateway

# Check Cilium Gateway status
kubectl get svc -n gateway
```

### Database connection errors
```bash
# Verify PostgreSQL cluster
kubectl get perconapgcluster -n databases

# Check if opwerf user exists
kubectl get secret -n databases | grep opwerf

# Check opwerf database
kubectl exec -n databases <postgres-pod> -- psql -U postgres -c "\l" | grep opwerf
```

### Elasticsearch not starting
```bash
# Check for vm.max_map_count issue
kubectl logs opwerf-elasticsearch-0 -n opwerf

# If error about max_map_count:
# The initContainer should handle this, but may need privileged PSP
```

## Integration with Existing Infrastructure

### PostgreSQL
- Uses existing Percona PG cluster in `databases` namespace
- User `opwerf` auto-created via PerconaPGCluster CRD
- Database `opwerf` auto-created
- Password retrieved from Percona-generated secret: `k8s-pg-pguser-opwerf`

### Temporal
- Connects to existing Temporal cluster in `temporal` namespace
- Uses namespace `opwerf` (auto-created by Temporal server)
- Frontend address: `temporal-frontend.temporal.svc.cluster.local:7233`

### MinIO
- Uses existing MinIO cluster in `storage` namespace
- Bucket `opwerf-artifacts` (created by opwerf-deployment role)
- Credentials: same as platform MinIO root user/password

### Gateway API
- Uses existing `main-gateway` in `gateway` namespace
- HTTPRoutes created in `opwerf` namespace
- TLS termination handled by Gateway (wildcard cert from cert-manager)

## Monitoring

### Grafana Dashboards
OpenWerf metrics are automatically scraped by VictoriaMetrics (if observability stack is enabled).

```bash
# Access Grafana
open https://grafana.example.com

# Import OpenWerf dashboards (if available)
# - OpenWerf API metrics
# - OpenWerf Worker metrics
# - Redis metrics
# - Elasticsearch metrics
```

### Logs
```bash
# View logs via Loki/Grafana (if observability stack enabled)
# Or direct kubectl logs:
kubectl logs -f deployment/opwerf-api -n opwerf
kubectl logs -f deployment/opwerf-worker -n opwerf
kubectl logs -f deployment/opwerf-dashboard -n opwerf
```

## Updates and Upgrades

```bash
# Update image tag
vim platform.yaml
# Set applications.opwerf.image_tag: "v1.2.3"

# Redeploy
./platform-orchestrator/platform.sh deploy opwerf

# Or via Ansible
ansible-playbook playbooks/deploy_platform.yml \
  -e deploy_opwerf=true \
  -e opwerf_image_tag=v1.2.3 \
  --tags opwerf

# Rolling restart (if config changed)
kubectl rollout restart deployment/opwerf-api -n opwerf
kubectl rollout restart deployment/opwerf-worker -n opwerf
kubectl rollout restart deployment/opwerf-dashboard -n opwerf
```

## Removal

```bash
# Remove OpenWerf (keeps database and data)
kubectl delete namespace opwerf

# Clean up database (optional, irreversible)
kubectl exec -n databases <postgres-pod> -- psql -U postgres -c "DROP DATABASE opwerf;"
kubectl exec -n databases <postgres-pod> -- psql -U postgres -c "DROP USER opwerf;"

# Clean up MinIO bucket (optional, irreversible)
kubectl exec -n storage <minio-pod> -- mc rb local/opwerf-artifacts --force
```

## Security Considerations

- All secrets auto-generated and stored in Kubernetes secrets
- Database credentials managed by Percona operator
- Internal credential proxy uses separate auth token
- TLS termination at Gateway level (wildcard cert)
- Pod Security Standards: baseline enforced
- Network policies: recommended (deploy via Cilium)
- Redis password-protected
- Elasticsearch without auth (internal only)

## License

OpenWerf deployment role is part of the ansible-k8s-full-setup project.
