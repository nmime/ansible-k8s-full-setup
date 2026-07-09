# Deploy and Test the Ansible Workflow

## Prerequisites

### Install Ansible
```bash
pip install ansible-core>=2.16.0
ansible-galaxy collection install -r requirements.yml
```

### Required Tools
```bash
# Verify tools
ansible --version      # >= 2.16.0
kubectl version        # >= 1.30
helm version           # >= 3.14
yq --version           # >= 4.0 (for platform orchestrator)
hcloud version         # Latest
```

### Setup Environment
```bash
export HCLOUD_TOKEN="your-hetzner-api-token"
```

## Deployment Options

### Option A: Platform Orchestrator (Recommended)
```bash
cd platform-orchestrator
./platform.sh init                # Creates platform.yaml from platform.example.yaml
vim platform.yaml                 # Set domain, project, tier
./platform.sh deploy all          # Full deployment
./platform.sh credentials        # Show all passwords
```

### Option B: Ansible Directly
```bash
cp inventory.example inventory.yml
vim inventory.yml                 # Customize settings
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml
```

### Option C: Component-by-Component
```bash
# Deploy only specific components using tags
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags infrastructure
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags network
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags cluster
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags secrets
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags storage
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags databases
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags gitlab
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags gitops
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags observability
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags autoscaling
```

## Verification

### Check Infrastructure
```bash
hcloud server list
hcloud network list
hcloud load-balancer list
```

### Check Kubernetes Cluster
```bash
kubectl get nodes
kubectl get pods -A
kubectl get svc -A
kubectl get pvc -A
```

### Check Application (nest-react-boilerplate)
```bash
# Application pods
kubectl get pods -n production
# Expected: nest-react-boilerplate pods Running

# ArgoCD application status
kubectl get application nest-react-boilerplate -n argocd
# Expected: Synced, Healthy

### Check Services
```bash
# GitLab
kubectl get pods -n gitlab
kubectl get secret gitlab-gitlab-initial-root-password -n gitlab -o jsonpath='{.data.password}' | base64 -d

# ArgoCD
kubectl get pods -n argocd
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d

# Grafana
kubectl get pods -n monitoring
kubectl get secret grafana -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d

# SeaweedFS
kubectl get pods -n storage

# Vault
kubectl get pods -n vault
kubectl exec -n vault vault-0 -- vault status

# KEDA
kubectl get pods -n keda
kubectl get scaledobjects -A
```

### Health Check (via Platform Orchestrator)
```bash
./platform-orchestrator/platform.sh health
./platform-orchestrator/platform.sh status
./platform-orchestrator/platform.sh credentials
```

## Troubleshooting

### Connection Timeouts
```bash
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --timeout=300
```

### Pod Issues
```bash
# Check unhealthy pods
kubectl get pods -A | grep -vE 'Running|Completed'

# Describe failing pod
kubectl describe pod <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace>
```

### Helm Release Issues
```bash
# List all releases
helm list -A

# Check release status
helm status gitlab -n gitlab
helm status argocd -n argocd

# Rollback if needed
helm rollback gitlab 1 -n gitlab
```

### Auto-Healing
```bash
./platform-orchestrator/platform.sh heal
```

## Scaling

### Change Tier
```bash
# Update tier in platform.yaml or inventory
# Then redeploy
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml -e tier=production
```

### Tier Upgrade Path
- medium-optimized full tool set (~€55.92/mo server compute, ~€61.92/mo with lb11)
- minimal (~€16.47/mo server compute)
- small (~€21.96/mo server compute, ~€27.96/mo with lb11)
- medium (~€85.44/mo server compute, ~€91.44/mo with lb11)
- production (~€101.43/mo server compute, ~€107.43/mo with lb11)

Current observed K8s footprint: `4 x cx23 + 4 x cx33 = €55.92/mo` server compute
(`€0.092/h`, `€671.04/year`). Medium and production use larger 16Gi-class Hetzner
nodes (`cx43`) for both control planes and workers, plus larger persistent volumes
for object storage, PostgreSQL, Vault, and observability. Treat the figures above
as server-compute subtotals only: storage volumes, backups/snapshots, traffic overage,
IPv4/floating IPs, edge/CDN services, and tax/VAT can change the final bill.
Use `platform-orchestrator/profiles/medium-optimized.yaml` to keep the
medium/production tool set while matching the current CX23/CX33 server budget.
Run `./platform.sh init medium-optimized`, set `global.domain` and `global.email`,
then run `./platform.sh deploy all`.

## Backup and Restore

### Automated Backups
- GitLab: Daily at 2 AM (via toolbox CronJob)
- PostgreSQL: Weekly full + daily incremental (pgbackrest to SeaweedFS)
- MongoDB: Weekly backups to SeaweedFS (if enabled)

### Manual Backup
```bash
# GitLab backup
kubectl exec -n gitlab -it $(kubectl get pods -n gitlab -l app=toolbox -o name) -- backup-utility

# PostgreSQL backup (via Percona pgbackrest)
kubectl exec -n databases -it $(kubectl get pods -n databases -l postgres-operator.crunchydata.com/data=postgres -o name | head -1) -- pgbackrest backup --stanza=db --type=full
```

## Clean Up

### Remove Platform (preserves DNS)
```bash
./platform-orchestrator/platform.sh destroy
```

### Remove via Ansible
```bash
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml -e state=absent
```

## Best Practices

1. Always test in staging tier before production
2. Use VPN for all admin access (GitLab, ArgoCD, Grafana, Vault)
3. Enable and verify backups before going to production
4. Monitor Grafana dashboards and configure alerting
5. Keep all configuration in version control
6. Use External Secrets Operator for application secrets
7. Use ArgoCD ApplicationSets for multi-environment deployments
8. Ensure Node.js 24 is available in the build environment for nest-react-boilerplate
9. Dragonfly provides Redis v6-compatible caching for the application
