# CDN Routing Modes

## The Problem You Asked About

**"If I enter `app.example.com` (without `cdn`), how does it use CDN?"**

Answer: **It doesn't**. By default, `app.example.com` goes through **Hetzner DNS → K8s directly** (no CDN).

**Only `cdn.example.com` goes through Gcore GeoDNS → edges → CDN.**

---

## Two Modes

### Mode 1: Subdomain Only (Current Default)

```yaml
edge_mode: "subdomain"  # default
edge_subdomain: "cdn"
```

**DNS Setup:**
```
Hetzner DNS:
  app.example.com  →  116.203.x.x (K8s LB, direct)
  dash.example.com →  116.203.x.x (K8s LB, direct)
  api.example.com  →  116.203.x.x (K8s LB, direct)

Gcore DNS:
  cdn.example.com  →  GeoDNS → nearest edge → cached
```

**User Experience:**
- Visit `app.example.com` → direct to K8s (no CDN, ~300ms from Asia)
- Visit `cdn.example.com` → via edge (~30ms from Asia, cached)

**Use case:** Testing CDN or serving only static assets through CDN.

---

### Mode 2: Full CDN (Recommended for Production)

```yaml
edge_mode: "full"
edge_cdn_subdomains: ["app", "dash"]  # or [] for all
```

**DNS Setup:**
```
Hetzner DNS:
  vpn.example.com     →  49.12.x.x (bastion, direct - no CDN for VPN!)
  gitlab.example.com  →  116.203.x.x (LB, direct - admin via VPN)
  api.example.com     →  116.203.x.x (LB, direct - optional)

Gcore DNS:
  app.example.com   →  GeoDNS → edge → cached
  dash.example.com  →  GeoDNS → edge → cached
```

**User Experience:**
- Visit `app.example.com` → via edge (~30ms from Asia) ✓
- Visit `dash.example.com` → via edge (~30ms from Asia) ✓
- Visit `api.example.com` → direct to K8s (fresh data)

**Use case:** Production. Users don't need to know about CDN—they use normal URLs.

---

## How to Switch to Full CDN Mode

### Step 1: Configure Variables

```yaml
# group_vars/all.yml or playbook vars
edge_mode: "full"

# Option A: Route specific subdomains
edge_cdn_subdomains:
  - "app"     # app.example.com via CDN
  - "dash"    # dash.example.com via CDN
  - "www"     # www.example.com via CDN

# Option B: Route ALL subdomains (wildcard)
edge_cdn_subdomains: []
```

### Step 2: Add Subdomains to HTTPRoutes

Your HTTPRoutes already accept the direct domain. No changes needed:

```yaml
# This works for both direct and CDN routing:
hostnames:
  - "app.example.com"
```

K8s Gateway doesn't care if the request came from an edge or directly.

### Step 3: Update Nginx Edge Config (Optional)

The edge Nginx already accepts `*.{{ edge_domain }}`. No changes needed.

### Step 4: Deploy Edge CDN with Full Mode

```bash
ansible-playbook -i inventory/hosts.yml playbooks/edge-cdn.yml \
  -e "edge_mode=full" \
  -e "edge_cdn_subdomains=['app','dash']" \
  -e "domain=example.com" \
  -e "origin_server_ip=116.203.12.34"
```

**What this does:**

Creates GeoDNS records in Gcore for:
- `app.example.com` → GeoDNS → edges
- `dash.example.com` → GeoDNS → edges

**And leaves in Hetzner DNS:**
- `vpn.example.com` → bastion (direct)
- `gitlab.example.com` → LB (direct)
- `*.example.com` → LB (wildcard catchall for everything else)

### Step 5: Test

```bash
# Check DNS resolution
dig +short app.example.com
# Should return edge IP (not LB IP anymore)

# Check which edge you get
curl -I https://app.example.com/
# X-Edge-Region: EU / US / APAC
# X-Cache-Status: MISS (first) → HIT (second)

# Check direct domain still works
curl -I https://vpn.example.com/
# Should resolve to bastion IP (not edge)
```

---

## DNS Priority Rules

When a domain is in **both Hetzner and Gcore**, which wins?

**Answer:** It depends on **NS records** at your domain registrar.

### Option A: Split DNS (current)

**At registrar (e.g., Namecheap, GoDaddy):**
```
example.com        NS  ns1.hetzner.com  (Hetzner handles most records)
cdn.example.com    NS  ns1.gcorelabs.net  (Gcore handles cdn subdomain only)
```

This is **NS delegation** — Hetzner handles the zone, but Gcore handles a specific subdomain.

**Result:**
- `app.example.com` → resolved by Hetzner
- `cdn.example.com` → resolved by Gcore

### Option B: Full Gcore DNS (for full CDN mode)

**At registrar:**
```
example.com  NS  ns1.gcorelabs.net  (Gcore handles ALL records)
```

**Gcore DNS zone has ALL records:**
```
app.example.com   A  GeoDNS → edges
dash.example.com  A  GeoDNS → edges
vpn.example.com   A  49.12.x.x (bastion, direct)
origin.example.com A  116.203.x.x (K8s LB)
```

**Result:** Gcore resolves everything. You can use GeoDNS for any subdomain.

### Option C: Keep Hetzner, CNAME to Gcore (simple hybrid)

**Hetzner DNS:**
```
app.example.com   CNAME  app.cdn.example.com  (redirect to CDN)
cdn.example.com   A      GeoDNS (Gcore)
vpn.example.com   A      49.12.x.x (direct)
```

**User visits `app.example.com`:**
1. Hetzner DNS: `app.example.com` CNAME → `app.cdn.example.com`
2. Gcore DNS: `app.cdn.example.com` → nearest edge IP
3. User connects to edge

**Problem:** CNAME adds extra DNS lookup (slower). GeoDNS A record is better.

---

## Recommendation

### For Testing:
```yaml
edge_mode: "subdomain"  # Keep default
```

Users must use `cdn.example.com` explicitly. Safe, no changes to existing DNS.

### For Production:
```yaml
edge_mode: "full"
edge_cdn_subdomains: ["app", "www", "dash"]  # Public-facing apps
```

Users use normal URLs (`app.example.com`). CDN is transparent. Admin services stay direct.

---

## Summary

**Your question: "I enter `app.example.com`, not `cdn.example.com`. How does it use CDN?"**

**Answer:**

**Current setup** (mode=subdomain): It **doesn't** use CDN. You must use `cdn.example.com`.

**Full CDN mode**: Change `edge_mode: "full"` and create GeoDNS records for `app.example.com` in Gcore. Then it **does** use CDN when users visit `app.example.com`.

The subdomain mode is for testing. Full mode is for production.
