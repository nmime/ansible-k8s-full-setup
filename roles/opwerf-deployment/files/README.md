# OpenWerf

AI-powered workflow orchestration platform.

## Structure

```
├── src/                    # Application source code
├── docker/                 # Dockerfiles
│   ├── Dockerfile.api
│   ├── Dockerfile.worker
│   ├── Dockerfile.dashboard
│   ├── Dockerfile.credential-proxy
│   └── nginx.conf
├── helm/                   # Helm chart (ArgoCD watches this)
└── .gitlab-ci.yml         # CI/CD pipeline
```

## Branch Workflow

| Branch | Trigger | Image Tag | Environment |
|--------|---------|-----------|-------------|
| `st` | Merge request | `st-latest` | Staging |
| `pp` | Merge request | `pp-latest` | Pre-production |
| `main` | Push | `latest` | Production |

### Promotion Flow

1. Feature branch → MR to `st` → test → merge → builds `st-latest`
2. MR `st` → `pp` → test → merge → promotes images to `pp-latest`
3. Push `pp` → `main` → builds `latest` for production
