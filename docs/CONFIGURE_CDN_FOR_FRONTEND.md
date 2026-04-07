# Configure CDN for Frontend (OpenWerf Example)

This guide shows **exactly how to configure your frontend to use the edge CDN**, using OpenWerf as a real example.

---

## Understanding the Setup

### Without CDN (current)

```
User → app.example.com → Hetzner DNS → K8s LB → Gateway → Frontend Pod

All traffic goes directly to K8s cluster in Germany
Global users: 200-500ms latency
```

### With CDN

```
User → cdn.example.com → Gcore GeoDNS → Nearest Edge → Cache?
                                              ↓
                                         ┌────┴────┐
                                         ↓         ↓
                                       HIT       MISS
                                    (instant)  (fetch from origin)
                                                   ↓
                            origin.example.com → K8s LB → Gateway → Pod
```

**Key insight**: `cdn.example.com` and `app.example.com` serve the **exact same content** from the **same K8s pod**. The edge just caches it.

---

## Step-by-Step Configuration

### Step 1: Add CDN Hostname to HTTPRoute

Edit `roles/opwerf-deployment/files/helm/templates/httproute.yaml`:

**Before:**
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: opwerf-dashboard
spec:
  parentRefs:
    - name: main-gateway
      namespace: gateway
  hostnames:
    - "app.example.com"    # Only accepts app.example.com
  rules:
    - backendRefs:
        - name: opwerf-dashboard
          port: 80
```

**After:**
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: opwerf-dashboard
spec:
  parentRefs:
    - name: main-gateway
      namespace: gateway
  hostnames:
    - "app.example.com"    # Direct access
    - "cdn.example.com"    # CDN access (same pod, same content)
  rules:
    - backendRefs:
        - name: opwerf-dashboard
          port: 80
```

**What this does**: Now K8s Gateway accepts requests for BOTH domains and routes them to the same frontend pod.

### Step 2: Deploy Edge CDN Role

```bash
# Set Gcore API key
export GCORE_API_KEY="your_key_here"

# Deploy edge servers + GeoDNS
ansible-playbook -i inventory/hosts.yml playbooks/edge-cdn.yml \
  -e "domain=example.com" \
  -e "origin_server_ip=$(hcloud load-balancer list | grep lb-k8s | awk '{print $4}')"
```

**What this creates:**
- 3 edge servers (EU, US, APAC)
- Gcore DNS: `cdn.example.com` → GeoDNS → nearest edge
- Health checks every 30s
- Nginx cache (10GB per edge)

### Step 3: Update OpenWerf Helm Values

Edit `roles/opwerf-deployment/files/helm/values.yaml`:

```yaml
ingress:
  enabled: true
  gatewayName: main-gateway
  gatewayNamespace: gateway
  dashboardHost: app.example.com      # Direct domain
  cdnHost: cdn.example.com            # CDN domain (add this line)
  apiHost: api.example.com
```

Then update the template to use `cdnHost`:

```yaml
# httproute.yaml
hostnames:
  - {{ .Values.ingress.dashboardHost | quote }}
  {{- if .Values.ingress.cdnHost }}
  - {{ .Values.ingress.cdnHost | quote }}
  {{- end }}
```

### Step 4: Configure Frontend Build

Your frontend needs to know to use CDN URLs for static assets.

#### For Next.js:

```javascript
// next.config.js
module.exports = {
  assetPrefix: process.env.CDN_URL || '',
  // Example: CDN_URL=https://cdn.example.com
}
```

#### For Vite/React:

```javascript
// vite.config.js
export default defineConfig({
  base: process.env.CDN_URL || '/',
  // Example: CDN_URL=https://cdn.example.com/
})
```

#### For Vue CLI:

```javascript
// vue.config.js
module.exports = {
  publicPath: process.env.CDN_URL || '/',
}
```

#### For Create React App:

```javascript
// package.json
{
  "homepage": "https://cdn.example.com"
}
```

**Build your app:**
```bash
CDN_URL=https://cdn.example.com npm run build
```

**What this does**: Generated HTML will have:
```html
<!-- Assets load from CDN -->
<script src="https://cdn.example.com/_next/static/chunks/main.js"></script>
<link href="https://cdn.example.com/_next/static/css/app.css">

<!-- API calls stay direct -->
<script>
  fetch('https://api.example.com/users')  // NOT through CDN
</script>
```

### Step 5: Rebuild and Deploy

```bash
# 1. Build Docker image with CDN URLs
docker build --build-arg CDN_URL=https://cdn.example.com -t registry.example.com/opwerf/dashboard:v1.2.0 .

# 2. Push to GitLab registry
docker push registry.example.com/opwerf/dashboard:v1.2.0

# 3. Update Helm values
helm upgrade opwerf ./opwerf-chart \
  --set image.tag=v1.2.0 \
  --set ingress.cdnHost=cdn.example.com

# Or redeploy via Ansible (ArgoCD will auto-sync)
ansible-playbook -i inventory/hosts.yml playbooks/deploy_platform.yml \
  -e "opwerf_frontend_image_tag=v1.2.0"
```

---

## Testing

### Test 1: Verify Both Domains Work

```bash
# Direct domain
curl -I https://app.example.com/
# Should return 200 OK

# CDN domain
curl -I https://cdn.example.com/
# Should return 200 OK
# X-Cache-Status: MISS (first request)
# X-Edge-Region: EU (or US/APAC based on your location)

# Second request to CDN
curl -I https://cdn.example.com/
# X-Cache-Status: HIT ✓
```

### Test 2: Check Asset URLs

```bash
# Fetch the HTML
curl https://cdn.example.com/ > index.html

# Check for CDN URLs in script/link tags
grep -o 'https://cdn.example.com[^"]*' index.html

# Should output:
# https://cdn.example.com/_next/static/chunks/main-abc123.js
# https://cdn.example.com/_next/static/css/app-def456.css
# https://cdn.example.com/images/logo.png
```

### Test 3: Check Cache Headers

```bash
# Check a JS file
curl -I https://cdn.example.com/_next/static/chunks/main.js

# Look for:
X-Cache-Status: HIT              # Cached!
Cache-Control: public, max-age=2592000  # 30 days
X-Edge-Region: EU                # Which edge
```

### Test 4: Performance

```bash
# Time direct domain
time curl -o /dev/null -s https://app.example.com/_next/static/chunks/main.js
# real: 0m0.500s (from Germany)

# Time CDN (first - MISS)
time curl -o /dev/null -s https://cdn.example.com/_next/static/chunks/main.js
# real: 0m0.520s (fetched from origin)

# Time CDN (second - HIT)
time curl -o /dev/null -s https://cdn.example.com/_next/static/chunks/main.js
# real: 0m0.030s (from edge cache!) ⚡ 16x faster
```

---

## What Gets Cached

The edge Nginx automatically caches based on file extension:

| Path | Cache | TTL |
|------|-------|-----|
| `/_next/static/**/*.js` | ✓ | 30 days |
| `/_next/static/**/*.css` | ✓ | 30 days |
| `/images/*.png` | ✓ | 30 days |
| `/fonts/*.woff2` | ✓ | 30 days |
| `/*.html` | ✓ | 1 hour |
| `/api/*` | ✗ | no cache |
| `/` (HTML page) | ✓ | 10 min |

### How Edge Decides to Cache

```nginx
# In edge-cdn Nginx config

# Static assets: 30 days
location ~* \.(js|css|png|jpg|gif|svg|woff2)$ {
    proxy_cache edge_cache;
    proxy_cache_valid 200 30d;
}

# HTML: 1 hour
location ~* \.(html|htm)$ {
    proxy_cache edge_cache;
    proxy_cache_valid 200 1h;
}

# API: no cache
location /api/ {
    proxy_cache off;
}

# Everything else: 10 min
location / {
    proxy_cache edge_cache;
    proxy_cache_valid 200 10m;
}
```

---

## Traffic Flow Example

### User in Japan Visits Your App

**Initial page load:**

```
1. Browser: GET https://cdn.example.com/
   ↓
2. DNS: Gcore GeoDNS sees Japan → returns 138.201.x.x (Singapore edge)
   ↓
3. Edge: Cache check for / → MISS
   ↓
4. Edge: proxy_pass https://origin.example.com/ (K8s in Germany)
   ↓
5. K8s: Gateway routes to opwerf-dashboard pod
   ↓
6. Pod: Returns HTML:
   <html>
     <script src="https://cdn.example.com/_next/static/chunks/main.js"></script>
     <link href="https://cdn.example.com/_next/static/css/app.css">
   </html>
   ↓
7. Edge: Cache HTML for 10 min, return to user
   ↓
8. User: Receives HTML in ~300ms (Singapore → Japan)
```

**Browser loads assets:**

```
9. Browser: GET https://cdn.example.com/_next/static/chunks/main.js
   ↓
10. DNS: Already resolved → 138.201.x.x
    ↓
11. Edge: Cache check → MISS (first time)
    ↓
12. Edge: Fetch from origin → K8s → Pod → main.js
    ↓
13. Edge: Cache for 30 days, return to user
    ↓
14. User: Receives JS in ~320ms
```

**Second user in Japan (5 minutes later):**

```
1. Browser: GET https://cdn.example.com/
   ↓
2. DNS: → 138.201.x.x
   ↓
3. Edge: Cache check for / → HIT!
   ↓
4. User: Receives HTML in ~30ms ⚡ (10x faster)

5. Browser: GET https://cdn.example.com/_next/static/chunks/main.js
   ↓
6. Edge: Cache HIT!
   ↓
7. User: Receives JS in ~30ms ⚡ (10x faster)
```

**User makes API call:**

```
Browser: fetch('https://api.example.com/users')
   ↓
DNS: Hetzner DNS → LB IP in Germany
   ↓
K8s: Gateway → opwerf-api pod
   ↓
Response: Direct to user (NO CACHE, always fresh data)
```

---

## Common Patterns

### Pattern 1: CDN for All Assets, Direct for HTML

**Best for**: Dynamic pages with user-specific content

```javascript
// Frontend runs on app.example.com
// But loads assets from cdn.example.com

// next.config.js
module.exports = {
  assetPrefix: 'https://cdn.example.com',
}
```

**Result:**
```html
<!-- HTML at https://app.example.com/ (NOT cached, user-specific) -->
<html>
  <!-- Assets from CDN (cached 30 days) -->
  <script src="https://cdn.example.com/_next/static/main.js"></script>
  
  <!-- User data rendered server-side (fresh) -->
  <div>Welcome, {{ user.name }}</div>
</html>
```

### Pattern 2: CDN for Everything

**Best for**: Static marketing sites, blogs

```javascript
// Entire site at cdn.example.com
// HTML cached 1 hour, assets cached 30 days

// next.config.js
module.exports = {
  assetPrefix: 'https://cdn.example.com',
  // No need to change base URL
}
```

**Users access:** `https://cdn.example.com/`
**Direct domain:** `https://app.example.com/` (for admin, auth, etc.)

### Pattern 3: Separate Static Domain

**Best for**: Large SPAs with lots of assets

```yaml
# Deploy static assets to static.example.com (via CDN)
# Deploy app to app.example.com (direct)

ingress:
  dashboardHost: app.example.com
  staticHost: static.example.com  # Separate HTTPRoute
  apiHost: api.example.com
```

```javascript
// vite.config.js
export default {
  base: 'https://static.example.com/',
}
```

---

## Summary

**Q: How does CDN work if we have one CDN domain?**

**A**: The CDN doesn't host content — it **proxies and caches** your existing content.

1. Your K8s cluster serves `app.example.com` (as always)
2. Edge servers proxy `cdn.example.com` → `origin.example.com` (same K8s)
3. Edge caches responses based on file type
4. Users load from nearest edge (fast) instead of origin (slow)

**Q: Do I need to move files to CDN?**

**A**: No! Your files stay in K8s. The CDN automatically fetches and caches them.

**Q: What if I update my app?**

**A**: Build with new filenames (`main.abc123.js` → `main.def456.js`). Old files stay cached, new files are fetched on first request.

**Q: What's the easiest way to try it?**

**A**: 
1. Add `cdn.example.com` to HTTPRoute hostnames
2. Deploy edge-cdn role
3. Visit `https://cdn.example.com/` — it works immediately!
4. Check `X-Cache-Status` header — first MISS, then HIT

No code changes needed to test. For production, update your build config to use CDN URLs for assets.
