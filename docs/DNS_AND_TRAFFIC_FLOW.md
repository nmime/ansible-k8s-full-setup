# DNS & Traffic Flow Architecture

This document explains **how DNS works** and **what each component is responsible for** in this platform. There are **two DNS providers** with different purposes.

---

## Two DNS Providers - Why and How

| Provider | API | Purpose | Records Created |
|----------|-----|---------|----------------|
| **Hetzner DNS** | hcloud CLI | Main domain DNS | `*.domain`, `domain`, `vpn.domain` |
| **Gcore DNS** | REST API | GeoDNS for edge CDN | `cdn.domain` with geographic routing |

### Why Two Providers?

1. **Hetzner DNS** - Simple, free, same API token as infrastructure
   - Best for: static records that don't need geographic routing
   - Cost: Free (included with Hetzner Cloud)
   - Features: Standard A/AAAA/CNAME/MX/TXT records

2. **Gcore DNS** - Advanced GeoDNS with health checks
   - Best for: CDN edge routing based on user location
   - Cost: Paid service (has free tier)
   - Features: GeoDNS filters, health checks, automatic failover

---

## The Complete DNS Architecture

```
                           DOMAIN: example.com
                                    │
            ┌───────────────────────┼───────────────────────┐
            │                       │                       │
     Hetzner DNS Zone          Gcore DNS Zone       NS Records
     (main domain)             (edge CDN only)      (at registrar)
            │                       │                       │
┌───────────┴─────────────┐  ┌─────┴──────────┐    ┌───────┴───────┐
│ *.example.com           │  │ cdn.example.com│    │ NS points to: │
│   → Hetzner LB IP       │  │   → GeoDNS:    │    │ Hetzner DNS   │
│                         │  │     EU→Edge EU │    │ servers       │
│ example.com             │  │     US→Edge US │    │               │
│   → Hetzner LB IP       │  │     APAC→Edge  │    │ Gcore handles │
│                         │  │                │    │ cdn.* subdomain│
│ vpn.example.com         │  └────────────────┘    │ via delegation │
│   → Bastion public IP   │                        └───────────────┘
└─────────────────────────┘
```

---

## Component Responsibilities

### 1. Hetzner DNS (roles/hetzner-infra)

**Manages**: Main domain DNS records

**Records created:**

| Record | Type | Value | Purpose |
|--------|------|-------|--------|
| `*` | A | Load Balancer IP | Wildcard for all app subdomains |
| `@` (root) | A | Load Balancer IP | Root domain access |
| `vpn` | A | Bastion public IP | VPN server direct access |
| `origin` | A | Load Balancer IP | CDN origin (edge proxies connect here) |

**How it works:**
```yaml
# roles/hetzner-infra/tasks/main.yml

# 1. Create DNS zone
hcloud zone create --name example.com --mode primary

# 2. Create wildcard A record (*.example.com → LB)
hcloud zone rrset create example.com --name '*' --type A --record {{ lb_public_ip }}

# 3. Create root A record (example.com → LB)
hcloud zone rrset create example.com --name '@' --type A --record {{ lb_public_ip }}

# 4. Create VPN A record (vpn.example.com → bastion)
hcloud zone rrset create example.com --name 'vpn' --type A --record {{ bastion_public_ip }}
```

**API used:** `hcloud` CLI (same HCLOUD_TOKEN as servers)

The managed zone may be a parent of the platform domain:

```yaml
global:
  domain: small.lab.example.com
hetzner_dns_zone: example.com
```

In this case the role verifies the suffix relationship and writes the relative
records `small.lab`, `*.small.lab`, and `vpn.small.lab` into `example.com`.
It fails before record mutation if the domain does not belong to the selected
zone. Omit `hetzner_dns_zone` when the platform domain itself is the zone.

---

### 2. Gcore DNS (roles/edge-cdn)

**Manages**: Edge CDN GeoDNS with health checks

**Records created:**

| Record | Type | Value | Purpose |
|--------|------|-------|--------|
| `cdn.example.com` | A (GeoDNS) | EU→edge-eu, US→edge-us, APAC→edge-apac | CDN with geographic routing |
| `example.com` | A (GeoDNS) | Same as above | Optional: route main domain through CDN |

**How GeoDNS works:**

```
User in Germany requests cdn.example.com:
  1. DNS query → Gcore DNS servers
  2. Gcore checks user's location: Europe
  3. Gcore returns IP of EU edge proxy: 49.12.x.x
  4. User connects to EU edge (lowest latency)

User in Japan requests cdn.example.com:
  1. DNS query → Gcore DNS servers
  2. Gcore checks user's location: Asia
  3. Gcore returns IP of APAC edge proxy: 138.201.x.x
  4. User connects to Singapore edge
```

**GeoDNS Filter Chain:**
```json
"filters": [
  {"type": "geodns"},       // Route by location
  {"type": "is_healthy"},   // Only return healthy servers
  {"type": "default"}       // Fallback if no geo match
]
```

**Health checks:**
- Frequency: 30 seconds
- Endpoint: `/health` on each edge
- If edge fails: automatically removed from DNS responses
- Recovery: automatically re-added when healthy

**API used:** Gcore REST API (`https://api.gcore.com/dns/v2/`)

---

### 3. cert-manager (roles/k8s-cluster-management)

**Manages**: TLS certificates inside Kubernetes

**Certificates issued:**

| Certificate | Issuer | Domains | Used by |
|-------------|--------|---------|--------|
| Wildcard cert | Let's Encrypt (DNS01) | `*.example.com`, `example.com` | Cilium Gateway |
| Internal CA certs | Self-signed CA | Service-specific | Vault internal TLS |

**How DNS01 validation works:**
```
1. cert-manager requests cert for *.example.com
2. Let's Encrypt says: prove you own example.com
3. cert-manager creates TXT record via Hetzner DNS API:
   _acme-challenge.example.com TXT "abc123..."
4. Let's Encrypt verifies the TXT record
5. Certificate issued
6. cert-manager deletes TXT record
```

**ClusterIssuer configuration:**
```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-account-key
    solvers:
      - dns01:
          hetzner:  # Uses HCLOUD_TOKEN
            apiTokenSecretRef:
              name: hetzner-dns-token
              key: token
```

---

### 4. Certbot on Edge Proxies (roles/edge-cdn)

**Manages**: TLS certificates for edge proxy servers

**Why separate from cert-manager?**
- Edge proxies are **standalone Hetzner VMs**, not in Kubernetes
- cert-manager only works inside K8s cluster
- Each edge needs its own TLS cert

**How it works:**
```bash
# On each edge server (EU, US, APAC)
certbot certonly --nginx \
  -d cdn.example.com \
  -d example.com \
  --email admin@example.com \
  --agree-tos

# Auto-renewal cron (weekly)
0 3 */7 * * certbot renew --quiet --deploy-hook 'systemctl reload nginx'
```

---

## Complete Traffic Flow

### Scenario 1: User visits app.example.com (no CDN)

```
1. User browser: DNS lookup app.example.com
   └─→ Hetzner DNS returns: 116.203.x.x (Hetzner LB)

2. User connects to 116.203.x.x:443 (HTTPS)
   └─→ Hetzner Load Balancer receives request

3. LB forwards to the live Cilium Gateway HTTPS NodePort on worker nodes
   └─→ The cluster role discovers the controller-owned port, updates the LB,
       and requires every TCP health check to become healthy
   └─→ Request enters K8s cluster

4. Cilium Gateway (envoy) terminates TLS, reads Host header
   └─→ Certificate: *.example.com wildcard from cert-manager

5. HTTPRoute matches Host: app.example.com
   └─→ Routes to the frontend Service declared by your application repository

6. Service load balances to pod
   └─→ Pod serves response

7. Response flows back: Pod → Service → Gateway → LB → User
```

### Scenario 2: User visits cdn.example.com (with CDN)

```
1. User browser: DNS lookup cdn.example.com
   └─→ Gcore GeoDNS checks user location: Europe
   └─→ Returns: 49.12.x.x (EU edge proxy)

2. User connects to 49.12.x.x:443 (HTTPS)
   └─→ EU edge Nginx receives request
   └─→ Certificate: Let's Encrypt via certbot

3. Nginx checks cache for cdn.example.com/path
   ├─→ CACHE HIT: Serve from /var/cache/nginx (fast!)
   │   └─→ Add header: X-Cache-Status: HIT
   │
   └─→ CACHE MISS: Proxy to origin
       └─→ proxy_pass https://origin.example.com

4. If MISS: Request goes to origin.example.com
   └─→ Hetzner DNS returns: 116.203.x.x (K8s LB)
   └─→ Same flow as Scenario 1

5. Origin response cached at edge for next requests
   └─→ Cache TTL: static=30d, html=1h, default=10m

6. Response to user with headers:
   └─→ X-Cache-Status: MISS (or HIT)
   └─→ X-Edge-Region: EU
```

### Scenario 3: Admin accesses GitLab (VPN required)

```
1. Admin connects to VPN:
   └─→ tailscale up --login-server=https://vpn.example.com
   └─→ Gets VPN IP: 100.64.x.x

2. Split DNS resolves the private admin endpoint to the bastion tailnet IP
   └─→ Example production address: 100.64.0.1

3. The operator discovers the admin Gateway Service HTTPS NodePort
   └─→ Cilium owns this allocation; do not hard-code or patch it

4. HAProxy listens only on the tailnet IP and TCP-passes TLS to that NodePort
   └─→ Its public-IP listeners remain dedicated to Headscale

5. Cilium admin-gateway terminates TLS
   └─→ HTTPRoute matches Host: gitlab.example.com

6. NetworkPolicy CHECK:
   └─→ CiliumNetworkPolicy: only allow from 100.64.0.0/10 or 10.0.0.0/16
   └─→ Admin's VPN IP (100.64.x.x) is allowed ✓

7. Request reaches GitLab webservice pod
```

#### Headscale policy, enrollment, and private DNS

The managed policy is deny-by-default:

- `admin` may reach the tagged subnet router over SSH and the private network
  over the configured TCP management ports (22, 80, 443, and 6443 by default)
  plus ICMP when enabled.
- `dev` has no private-network access by default. A profile may explicitly
  enable HTTPS-only access to the services subnet.
- The bastion advertises `10.0.0.0/16` with the
  `tag:subnet-router` tag. `autoApprovers` accepts only that tagged route.
- Pre-authentication keys are one-use and expire after one hour. The playbook
  creates and consumes the router key entirely on the bastion and deletes the
  temporary key file on every exit path.

For a laptop, create a separate one-use key for the intended Headscale user on
the bastion, use it immediately, and never store it in a shell profile, Git,
Ansible variables, or CI:

```bash
# Run on the bastion. Resolve the user ID first; do not use a reusable key.
ADMIN_ID="$(docker exec headscale headscale users list -o json |
  jq -er '.[] | select(.name == "admin") | .id')"
docker exec headscale headscale preauthkeys create \
  --user "$ADMIN_ID" --expiration 1h

# Run on the laptop with the newly created one-use key.
tailscale up \
  --login-server=https://vpn.example.com \
  --auth-key='<one-use-key>' \
  --accept-routes \
  --accept-dns
```

Confirm the client is registered under the expected user, the private route is
accepted, and an allowed HTTPS endpoint resolves and connects. Also verify that
a disallowed port is rejected. MagicDNS extra records use
`network.vpn.internal_dns.zones` when present, with
`network.internal_dns.zones` as a compatibility fallback. This permits VPN
clients and cluster pods to use different reachable addresses for the same
private hostname. Headscale does not replace the laptop's global resolver
(`override_local_dns: false`); it adds only managed tailnet/private records.
The `dns.nameservers.split` forwarder map may remain empty: it is needed only
when a private suffix must be delegated to a tailnet-reachable DNS server.
Explicit `extra_records_path` entries do not require a split-zone resolver.

For the load-balancer-free `minimal` tier, root and wildcard DNS point to the
bastion. HAProxy listens on public ports 80/443, routes `vpn.<domain>` to the
loopback-only Caddy/Headscale listener, and forwards every other HTTP host or
TLS SNI to the live Cilium Gateway NodePorts on the first cluster node. The
Hetzner firewall permits TCP/80 for ACME and HTTP ingress as well as TCP/443.

---

## DNS Record Summary

### Hetzner DNS Zone (example.com)

```
; Created by roles/hetzner-infra
*.example.com.      A     116.203.12.34    ; Hetzner LB → K8s apps
example.com.        A     116.203.12.34    ; Hetzner LB
vpn.example.com.    A     49.12.245.85     ; Bastion (Headscale VPN)
origin.example.com. A     116.203.12.34    ; CDN origin (edge upstreams here)

; Created by cert-manager (temporary, for DNS01 challenge)
_acme-challenge.example.com. TXT "<random>"  ; Deleted after cert issued
```

### Gcore DNS Zone (example.com - CDN subdomain only)

```
; Created by roles/edge-cdn
; GeoDNS record - returns different IPs based on user location

cdn.example.com.  A  GeoIP:
  EU users        → 49.12.100.1    (edge-eu)
  NA/SA users     → 142.132.50.2   (edge-us)
  AS/OC users     → 138.201.30.3   (edge-apac)
  Default         → 49.12.100.1    (edge-eu)

Health checks: /health every 30s
  └─→ Unhealthy edge removed from responses
  └─→ Re-added when healthy again
```

---

## Choosing Direct vs CDN Access

### When to use direct (*.example.com → Hetzner DNS → K8s):

- Admin services (GitLab, Grafana, ArgoCD, Vault) - VPN access
- WebSocket connections (real-time apps)
- API calls that can't be cached
- Internal services

### When to use CDN (cdn.example.com → Gcore GeoDNS → Edge):

- Static assets (images, CSS, JS)
- Public-facing websites
- Content with high cache hit potential
- Global audience (GeoDNS reduces latency)

### Hybrid approach (recommended):

```
app.example.com     → Direct to K8s (dynamic content, APIs)
static.example.com  → Through CDN (static assets)

# Or use CDN for everything with smart cache rules:
cdn.example.com/api/*     → proxy_cache off (pass through)
cdn.example.com/assets/*  → proxy_cache 30d (cache)
```

---

## Configuration Variables

### Hetzner DNS (main platform)

```yaml
# group_vars/all.yml
domain: "example.com"          # Main domain
hcloud_token: "{{ env.HCLOUD_TOKEN }}"  # Same token for DNS + infra
```

### Gcore DNS (edge CDN)

```yaml
# roles/edge-cdn/defaults/main.yml
gcore_api_key: "{{ env.GCORE_API_KEY }}"  # Separate API key
edge_domain: "{{ domain }}"               # Same domain
edge_dns_provider: "gcore"  # gcore | hetzner | hetzner_cname                     # cdn.example.com
```

### cert-manager (K8s TLS)

```yaml
# Uses hcloud_token via K8s secret
# Certificate names derived from domain variable
```

---

## Troubleshooting

### Check which DNS provider resolves a record

```bash
# Check Hetzner DNS
hcloud zone rrset list example.com

# Check Gcore DNS (via dig)
dig cdn.example.com @ns1.gcorelabs.net

# See which IP you get from your location
dig +short cdn.example.com
curl -I https://cdn.example.com  # Check X-Edge-Region header
```

### Check cert-manager certificates

```bash
kubectl get certificates -A
kubectl get certificaterequests -A
kubectl describe certificate wildcard-cert -n gateway
```

### Check edge proxy cache

```bash
# On edge server
ls -la /var/cache/nginx/
du -sh /var/cache/nginx/

# Check cache status via header
curl -I https://cdn.example.com/some-asset.js
# Look for: X-Cache-Status: HIT or MISS
```

### Check Gcore health status

```bash
# Via Gcore API
curl -H "Authorization: APIKey $GCORE_API_KEY" \
  https://api.gcore.com/dns/v2/zones/example.com/example.com/A/healthchecks
```

---

## Summary Table

| What | Who Manages | DNS Provider | TLS Provider |
|------|-------------|--------------|-------------|
| `*.example.com` | hetzner-infra | Hetzner DNS | cert-manager (K8s) |
| `example.com` | hetzner-infra | Hetzner DNS | cert-manager (K8s) |
| `vpn.example.com` | hetzner-infra | Hetzner DNS | Caddy / Let's Encrypt |
| `origin.example.com` | hetzner-infra | Hetzner DNS | cert-manager (K8s) |
| `cdn.example.com` | edge-cdn | Gcore DNS (GeoDNS) | certbot (on edge VMs) |
| `_acme-challenge.*` | cert-manager | Hetzner DNS (temp) | N/A (DNS validation) |

---

**The key insight**: Hetzner DNS is for the K8s platform (static IPs), Gcore DNS is for intelligent geographic routing to edge proxies. They serve different purposes and don't conflict.
