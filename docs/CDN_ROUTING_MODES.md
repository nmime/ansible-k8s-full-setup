# CDN DNS Provider Options

NS records for `example.com` can only point to **one DNS provider**.
You must pick one of the three modes below.

---

## Option 1: All DNS in Gcore (`edge_dns_provider: gcore`)

**Best choice for production.** Move NS at your registrar to Gcore.
Gcore handles everything — GeoDNS for CDN domains, plain A records for the rest.

**At your registrar (Namecheap/GoDaddy/etc):**
```
example.com  NS  ns1.gcorelabs.net
example.com  NS  ns2.gcorelabs.net
```

**What Ansible creates in Gcore:**
```
app.example.com    GeoDNS A  -> EU edge / US edge / APAC edge  (CDN)
dash.example.com   GeoDNS A  -> EU edge / US edge / APAC edge  (CDN)
www.example.com    GeoDNS A  -> EU edge / US edge / APAC edge  (CDN)
origin.example.com A         -> 116.203.x.x  (K8s LB, edges fetch from here)
vpn.example.com    A         -> 49.12.x.x    (bastion, direct)
api.example.com    A         -> 116.203.x.x  (K8s LB, direct)
gitlab.example.com A         -> 116.203.x.x  (K8s LB, direct)
```

**Configure:**
```yaml
edge_dns_provider: "gcore"

edge_cdn_domains:
  - "app.example.com"
  - "dash.example.com"
  - "www.example.com"

edge_direct_domains:
  - "vpn.example.com"
  - "api.example.com"
  - "gitlab.example.com"
```

---

## Option 2: All DNS in Hetzner + NS delegation (`edge_dns_provider: hetzner`)

Keep Hetzner DNS for everything. At your registrar, add NS records to
delegate specific CDN subdomains to Gcore. Ansible creates the GeoDNS records
in Gcore and direct A records in Hetzner.

**At your registrar — add NS delegation for each CDN subdomain:**
```
app.example.com   NS  ns1.gcorelabs.net
app.example.com   NS  ns2.gcorelabs.net
dash.example.com  NS  ns1.gcorelabs.net
dash.example.com  NS  ns2.gcorelabs.net
```

**What Ansible creates in Gcore (GeoDNS):**
```
app.example.com    GeoDNS A -> EU / US / APAC edges
dash.example.com   GeoDNS A -> EU / US / APAC edges
```

**What Ansible creates in Hetzner (direct):**
```
vpn.example.com    A -> 49.12.x.x
api.example.com    A -> 116.203.x.x
origin.example.com A -> 116.203.x.x
```

**Configure:**
```yaml
edge_dns_provider: "hetzner"

edge_cdn_domains:
  - "app.example.com"
  - "dash.example.com"

edge_direct_domains:
  - "vpn.example.com"
  - "api.example.com"
```

**Note:** Requires `HCLOUD_TOKEN` env var and `hcloud` CLI on the Ansible controller.

---

## Option 3: Hetzner with CNAME to Gcore (`edge_dns_provider: hetzner_cname`)

Keep all DNS in Hetzner. CDN domains in Hetzner are CNAMEs pointing to
GeoDNS records in a Gcore zone (`cdn.example.com`). No NS delegation needed.

**Tradeoff:** One extra DNS lookup per CDN request (~10–30ms slower than options 1/2).

**What Ansible creates in Gcore (GeoDNS zone: `cdn.example.com`):**
```
app.cdn.example.com   GeoDNS A -> EU / US / APAC edges
dash.cdn.example.com  GeoDNS A -> EU / US / APAC edges
```

**What Ansible creates in Hetzner:**
```
app.example.com    CNAME -> app.cdn.example.com   (-> Gcore GeoDNS)
dash.example.com   CNAME -> dash.cdn.example.com  (-> Gcore GeoDNS)
vpn.example.com    A     -> 49.12.x.x
api.example.com    A     -> 116.203.x.x
origin.example.com A     -> 116.203.x.x
```

**User visits `app.example.com`:**
1. Hetzner: `app.example.com` → CNAME → `app.cdn.example.com`
2. Gcore: `app.cdn.example.com` → GeoDNS → nearest edge IP
3. Browser connects to edge IP

**Configure:**
```yaml
edge_dns_provider: "hetzner_cname"

edge_cdn_domains:
  - "app.example.com"
  - "dash.example.com"

edge_direct_domains:
  - "vpn.example.com"
  - "api.example.com"
```

---

## Comparison

| | gcore | hetzner | hetzner_cname |
|---|---|---|---|
| NS at registrar | → Gcore | → Hetzner | → Hetzner |
| CDN domain DNS | Gcore GeoDNS | Gcore GeoDNS | Gcore GeoDNS |
| Direct domain DNS | Gcore plain A | Hetzner A | Hetzner A |
| Setup complexity | Simple | Medium (NS per subdomain) | Simple |
| DNS speed | Fastest | Fastest | Slightly slower (+1 lookup) |
| Recommended | ✓ Production | When you must keep Hetzner | When NS delegation is blocked |

---

## How to run

```bash
# Option 1: Gcore
ansible-playbook playbooks/edge-cdn.yml \
  -e "edge_cdn_confirm=true" \
  -e "edge_dns_provider=gcore" \
  -e "domain=example.com" \
  -e "origin_server_ip=116.203.x.x"

# Option 2: Hetzner + NS delegation
ansible-playbook playbooks/edge-cdn.yml \
  -e "edge_cdn_confirm=true" \
  -e "edge_dns_provider=hetzner" \
  -e "domain=example.com" \
  -e "origin_server_ip=116.203.x.x"

# Option 3: Hetzner CNAME
ansible-playbook playbooks/edge-cdn.yml \
  -e "edge_cdn_confirm=true" \
  -e "edge_dns_provider=hetzner_cname" \
  -e "domain=example.com" \
  -e "origin_server_ip=116.203.x.x"
```
