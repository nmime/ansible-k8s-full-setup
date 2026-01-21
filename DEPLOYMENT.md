---
# Deploy and Test the Ansible Workflow

## Prerequisites

### Install Ansible
```bash
# Install Ansible 2.15+
pip install ansible-core==2.16.0

# Install required collections
ansible-galaxy collection install -r requirements.yml
```

### Setup Environment Variables
```bash
# Copy and customize inventory
cp inventory.example inventory

# Source your environment variables
source env_vars.sh
```

## Deployment

### 1. Verify Prerequisites
```bash
# Check Ansible version
ansible --version

# Verify required tools
python3 --version
ansible-galaxy --version
```

### 2. Customize Inventory
```bash
# Edit inventory file
vim inventory
```

Set your domain, tier, and required parameters:
```ini
[global]
domain = your-domain.com
email = admin@your-domain.com
environment = production
tier = medium
```

### 3. Run the Deployment
```bash
# Deploy the complete platform
ansible-playbook playbooks/deploy_platform.yml -i inventory

# Deploy specific tier
ansible-playbook playbooks/deploy_platform.yml -i inventory -e tier=medium
```

### 4. Monitor Deployment Progress
```bash
# View verbose output
ansible-playbook playbooks/deploy_platform.yml -i inventory -v

# Resume failed tasks
ansible-playbook playbooks/deploy_platform.yml -i inventory --skip-tags irrelevant
```

## Verification

### Check Infrastructure
```bash
# Check Hetzner servers
hcloud server list

# Check networks
hcloud network list
```

### Check Kubernetes Cluster
```bash
# Copy kubeconfig from bastion
scp bastion:/root/.kube/config $HOME/.kube/config
chmod 600 $HOME/.kube/config

# Check nodes
kubectl get nodes

# Check all pods
kubectl get pods -A

# Check services
kubectl get svc -A
```

### Check Services

#### GitLab
```bash
curl -k https://gitlab.your-domain.com
```

#### ArgoCD
```bash
curl -k https://argocd.your-domain.com
```

#### Grafana
```bash
curl -k https://grafana.your-domain.com
```

#### MinIO
```bash
# API
curl -k https://s3.your-domain.com/minio/health/live

# Console
curl -k https://minio.your-domain.com
```

## Troubleshooting

### Common Issues

#### 1. Connection Timeouts
```bash
# Increase Ansible timeout
ansible-playbook playbooks/deploy_platform.yml -i inventory --timeout=300
```

#### 2. Authentication Failures
```bash
# Verify API token
hcloud server list

# Verify SSH access
ssh root@your-server-ip
```

#### 3. Kubernetes Cluster Issues
```bash
# Check node status
kubectl describe node

# Check pod events
kubectl describe pod <pod-name> -n <namespace>
```

## Scaling

### Scale Worker Nodes
```bash
# Add worker nodes via Ansible
ansible-playbook playbooks/scale_nodes.yml -i inventory -e "action=add_nodes worker_count=5"
```

### Update Kubernetes Version
```bash
# Upgrade Kubernetes
ansible-playbook playbooks/upgrade_k8s.yml -i inventory -e "target_version=v1.35.0"
```

## Backup and Restore

### Create Backup
```bash
ansible-playbook playbooks/backup.yml -i inventory
```

### Restore from Backup
```bash
ansible-playbook playbooks/restore.yml -i inventory -e "backup_date=2024-01-20"
```

## Clean Up

### Remove All Resources
```bash
# WARNING: This will delete everything!
ansible-playbook playbooks/cleanup.yml -i inventory
```

## Best Practices

1. **Always test in staging first**
2. **Use different tiers for dev/staging/prod**
3. **Enable backups before production**
4. **Monitor observability dashboards**
5. **Keep Ansible playbooks in version control**

## Next Steps

After deployment:

1. **Configure DNS**: Add DNS records for your services
2. **Set up VPN**: Connect to VPN and access private services
3. **Configure ArgoCD**: Set up ApplicationSets for your apps
4. **Configure Monitoring**: Set up Grafana dashboards
5. **Configure Alerts**: Set up alert rules
6. **Set up Backup**: Configure automated backups

For detailed documentation, see:
- README.md - Overview and quick start
- docs/deployment.md - Detailed deployment guide
- docs/troubleshooting.md - Troubleshooting guide
- docs/operations.md - Day-to-day operations
