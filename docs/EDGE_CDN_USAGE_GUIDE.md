# Edge CDN Usage Guide

## How to Use the CDN

After deploying the edge CDN, you have **cdn.example.com** with **GeoDNS** routing users to the nearest edge proxy.

---

## Quick Start

### Option 1: Point Your App to CDN (DNS Change Only)

**Before:**
```html
<!-- Your HTML -->
<link rel="stylesheet" href="https://app.example.com/assets/styles.css">
<script src="https://app.example.com/assets/app.js"></script>
<img src="https://app.example.com/images/logo.png">
```

**After:**
```html
<!-- Just change the hostname -->
<link rel="stylesheet" href="https://cdn.example.com/assets/styles.css">
<script src="https://cdn.example.com/assets/app.js"></script>
<img src="https://cdn.example.com/images/logo.png">
```

**Result**: All assets served from nearest edge proxy with caching.

---

### Option 2: Separate Static Domain (Recommended)

**1. Configure edge-cdn role to create subdomain:**

```yaml
# group_vars/all.yml or inventory
edge_dns_provider: "gcore"  # gcore | hetzner | hetzner_cname
```

This creates: `static.example.com` → GeoDNS → Edge proxies

**2. Update your app to use it:**

```html
<!-- Dynamic content -->
https://app.example.com/api/users  (direct to K8s, no cache)

<!-- Static assets -->
https://static.example.com/assets/styles.css  (via CDN, cached 30d)
https://static.example.com/images/logo.png    (via CDN, cached 30d)
```

---

### Option 3: Proxy Everything Through CDN (Advanced)

Use `cdn.example.com` for ALL traffic with smart caching rules:

```nginx
# Edge Nginx config already has:

location /api/ {
    proxy_cache off;           # API: no cache, pass through
}

location ~* \.(js|css|png|jpg)$ {
    proxy_cache edge_cache;    # Static: cache 30 days
}

location / {
    proxy_cache edge_cache;    # HTML: cache 10 min
}
```

**Your app:**
```
https://cdn.example.com/             → cached 10 min
https://cdn.example.com/api/users    → no cache (direct pass-through)
https://cdn.example.com/images/x.png → cached 30 days
```

---

## What Gets Cached (Automatic)

### Cache TTL by File Type

| File Type | Cache Duration | Examples |
|-----------|----------------|----------|
| **Images** | 30 days | `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.avif`, `.ico`, `.svg` |
| **Fonts** | 30 days | `.woff`, `.woff2` |
| **Styles** | 30 days | `.css` |
| **Scripts** | 30 days | `.js` |
| **HTML** | 1 hour | `.html`, `.htm` |
| **Default** | 10 min | Everything else (except /api/) |
| **API paths** | No cache | `/api/*` |

### Cache Behavior

```
First request to cdn.example.com/assets/app.js:
  1. Edge receives request
  2. Cache MISS → proxy to origin.example.com
  3. Origin serves file from K8s
  4. Edge caches response for 30 days
  5. Response sent to user
  Headers: X-Cache-Status: MISS, X-Edge-Region: EU

Second request (same file, within 30 days):
  1. Edge receives request
  2. Cache HIT → serve from local disk (fast!)
  3. NO request to origin
  4. Response sent to user
  Headers: X-Cache-Status: HIT, X-Edge-Region: EU
```

---

## Checking Cache Status

### Via HTTP Headers

```bash
# Check if cached
curl -I https://cdn.example.com/assets/app.js

# Look for:
X-Cache-Status: HIT      # Served from cache
X-Cache-Status: MISS     # Fetched from origin
X-Cache-Status: EXPIRED  # Cache expired, refreshing
X-Cache-Status: BYPASS   # Not cacheable

X-Edge-Region: EU        # Which edge served it
```

### Test from Different Locations

```bash
# From Europe
curl -I https://cdn.example.com/test.js
# X-Edge-Region: EU
# Served from: 49.12.x.x (Falkenstein)

# From US (use VPS or proxy)
curl -I https://cdn.example.com/test.js
# X-Edge-Region: US
# Served from: 142.132.x.x (Ashburn)

# From Asia (use VPS or proxy)
curl -I https://cdn.example.com/test.js
# X-Edge-Region: APAC
# Served from: 138.201.x.x (Singapore)
```

---

## Cache Purging

### Automatic Purge (Weekly)

A Kubernetes CronJob purges all edge caches every Sunday at 4 AM UTC.

```yaml
# Already created by edge-cdn role
apiVersion: batch/v1
kind: CronJob
metadata:
  name: edge-cache-purge
  namespace: monitoring
spec:
  schedule: "0 4 * * 0"  # Sunday 4 AM
```

### Manual Purge (Specific File)

```bash
# From origin server or K8s pod
curl -X PURGE https://cdn.example.com/purge/assets/app.js

# Response: 200 OK
# File removed from all edges within seconds
```

**Restrictions:**
- Only allowed from origin IP (`10.0.0.0/8` by default)
- Blocked from public internet for security

### Manual Purge (All Files)

```bash
# SSH to each edge and clear cache
ssh root@edge-eu-server
rm -rf /var/cache/nginx/*
systemctl reload nginx
```

---

## GeoDNS Routing Details

### How It Works

```
User Location      → Gcore DNS Returns → User Connects To
─────────────────────────────────────────────────────────
Germany (EU)       → 49.12.x.x         → edge-eu (Falkenstein)
France (EU)        → 49.12.x.x         → edge-eu
UK (EU)            → 49.12.x.x         → edge-eu

USA (NA)           → 142.132.x.x       → edge-us (Ashburn)
Canada (NA)        → 142.132.x.x       → edge-us
Brazil (SA)        → 142.132.x.x       → edge-us

Japan (AS)         → 138.201.x.x       → edge-apac (Singapore)
Australia (OC)     → 138.201.x.x       → edge-apac
India (AS)         → 138.201.x.x       → edge-apac

Default/Unknown    → 49.12.x.x         → edge-eu (fallback)
```

### Verify GeoDNS

```bash
# Check your DNS response
dig +short cdn.example.com
# Returns: IP of nearest edge

# Check specific continent routing (using Gcore API)
curl -H "Authorization: APIKey $GCORE_API_KEY" \
  https://api.gcore.com/dns/v2/zones/example.com/cdn.example.com/A

# Response shows continent → IP mapping
```

---

## Health Check & Failover

### How Health Checks Work

```
Gcore DNS monitors: https://cdn.example.com/health every 30s

Healthy edge:
  GET https://49.12.x.x/health
  Response: 200 {"status":"ok","region":"EU"}
  → Edge STAYS in GeoDNS responses

Unhealthy edge:
  GET https://49.12.x.x/health
  Response: timeout / 5xx / connection refused
  → After 3 failures (90s), edge REMOVED from GeoDNS
  → EU users now routed to US edge instead

Recovery:
  Edge comes back online
  Health check passes again
  → Edge AUTO-ADDED back to GeoDNS within 30s
```

### Manual Health Check

```bash
# Test each edge
curl https://49.12.x.x/health -H "Host: cdn.example.com"
# {"status":"ok","region":"EU"}

curl https://142.132.x.x/health -H "Host: cdn.example.com"
# {"status":"ok","region":"US"}

curl https://138.201.x.x/health -H "Host: cdn.example.com"
# {"status":"ok","region":"APAC"}
```

---

## Performance Testing

### Compare Direct vs CDN

```bash
# Direct to origin (no CDN)
time curl -o /dev/null https://app.example.com/assets/large-file.js
# real: 0m2.5s (from K8s cluster)

# Via CDN (first request - MISS)
time curl -o /dev/null https://cdn.example.com/assets/large-file.js
# real: 0m2.6s (proxied through edge)

# Via CDN (second request - HIT)
time curl -o /dev/null https://cdn.example.com/assets/large-file.js
# real: 0m0.1s (served from edge cache!) ⚡
```

### Load Testing

```bash
# Install Apache Bench
apt install apache2-utils

# Test CDN performance
ab -n 1000 -c 10 https://cdn.example.com/assets/app.js

# Results:
Requests per second:    500 [#/sec] (first ~100 are MISS, rest are HIT)
Time per request:       2ms (average)
```

---

## Cache Hit Rate Monitoring

### Prometheus Metrics (Already Configured)

The edge-cdn role creates a PrometheusRule alert:

```yaml
alert: EdgeCacheHitRateLow
expr: |
  (
    rate(nginx_http_requests_total{status="200",cache_status="HIT"}[5m])
    /
    rate(nginx_http_requests_total{status="200"}[5m])
  ) < 0.5
for: 15m
annotations:
  summary: Edge cache hit rate below 50%
```

**What it means:**
- Hit rate < 50% for 15 min → alert fires
- Indicates:
  - Not enough cacheable content
  - Cache TTL too short
  - Cache being purged too often
  - Traffic pattern changed

### Check Hit Rate in Grafana

```promql
# Cache hit rate (percentage)
rate(nginx_http_requests_total{cache_status="HIT"}[5m])
/
rate(nginx_http_requests_total[5m])
* 100

# Ideal: > 70% for static sites, > 50% for dynamic
```

---

## Use Cases & Examples

### 1. Static Website

**Perfect for CDN**

```yaml
# Deploy site to K8s at app.example.com
# Configure DNS:
#   cdn.example.com → GeoDNS → edges
#   app.example.com → Hetzner LB → K8s (origin)

# In your HTML:
<link rel="canonical" href="https://cdn.example.com/">
<link rel="stylesheet" href="https://cdn.example.com/styles.css">
<script src="https://cdn.example.com/app.js"></script>

# Result:
# - 99% cache hit rate
# - Global users get < 50ms latency
# - Origin sees 1% of traffic
```

### 2. Single Page App (SPA)

**Good for CDN with API pass-through**

```javascript
// Frontend app at https://cdn.example.com
// index.html cached 1 hour
// JS/CSS cached 30 days

// API calls go direct (no cache)
fetch('https://cdn.example.com/api/users')
  // Edge Nginx: location /api/ → proxy_cache off
  // Request passes through edge to origin K8s
```

### 3. Image-Heavy Site

**Ideal for CDN**

```html
<!-- Product images -->
<img src="https://cdn.example.com/products/{{ id }}.jpg">

<!-- All images cached 30 days at edge -->
<!-- Users download from nearest region -->
<!-- Origin bandwidth: 95% reduction -->
```

### 4. Real-Time App (WebSockets)

**Don't use CDN for WebSockets**

```javascript
// WebSocket: direct to origin
const ws = new WebSocket('wss://app.example.com/ws');

// Static assets: via CDN
const script = document.createElement('script');
script.src = 'https://cdn.example.com/bundle.js';
```

---

## Cost Optimization

### Bandwidth Savings

```
Without CDN:
  100,000 requests/day × 500 KB average = 50 GB/day
  → All traffic hits K8s origin
  → Hetzner egress: €0.01/GB = €0.50/day = €15/mo

With CDN (80% hit rate):
  80,000 requests cached at edge = 40 GB/day from cache
  20,000 requests to origin = 10 GB/day from K8s
  → Hetzner egress: €0.10/mo
  → Edge servers: €34.47/mo at the audited price (3× cpx12 servers)
  → Net savings: ~€0 (break-even at low volume)

With CDN (at scale):
  1M requests/day × 500 KB = 500 GB/day
  → Without CDN: €150/mo egress
  → With CDN (80% hit): €30/mo egress + €15/mo edges = €45/mo
  → Savings: €105/mo (70% reduction)
```

**Break-even point**: ~100,000 requests/day with 500 KB average response size.

---

## Advanced Configuration

### Custom Cache Rules

Edit `roles/edge-cdn/templates/edge-nginx.conf.j2`:

```nginx
# Add custom location block
location /downloads/ {
    proxy_pass https://origin;
    proxy_cache edge_cache;
    proxy_cache_valid 200 90d;  # Cache downloads for 90 days
    expires 90d;
}

# Cache API responses (specific endpoints)
location /api/public/ {
    proxy_pass https://origin;
    proxy_cache edge_cache;
    proxy_cache_valid 200 5m;   # Cache public API for 5 min
}
```

### Vary Cache by Query String

```nginx
location /images/ {
    proxy_cache_key $scheme$host$request_uri;  # Includes query params
    # /images/logo.png?v=1 and /images/logo.png?v=2 are different cache entries
}
```

### Ignore Query String (Cache Busting)

```nginx
location /assets/ {
    proxy_cache_key $scheme$host$uri;  # Ignore query params
    # /assets/app.js?v=1 and /assets/app.js?v=2 serve same cached file
}
```

---

## Troubleshooting

### Problem: Cache always MISS

**Check:**

```bash
# 1. Verify origin is reachable from edge
ssh root@edge-eu
curl -v https://origin.example.com/test.js

# 2. Check Nginx error log
tail -f /var/log/nginx/error.log

# 3. Verify cache directory writable
ls -la /var/cache/nginx/
chown www-data:www-data /var/cache/nginx/
```

### Problem: Wrong edge region

**Check GeoDNS:**

```bash
# From problematic location, check DNS response
dig +short cdn.example.com

# Verify Gcore GeoDNS config
curl -H "Authorization: APIKey $GCORE_API_KEY" \
  https://api.gcore.com/dns/v2/zones/example.com/cdn.example.com/A
```

### Problem: Health check failing

**Debug:**

```bash
# Check edge health endpoint
curl https://<edge-ip>/health -H "Host: cdn.example.com"

# Check Gcore health status
curl -H "Authorization: APIKey $GCORE_API_KEY" \
  https://api.gcore.com/dns/v2/zones/example.com/cdn.example.com/A/healthchecks

# Check edge Nginx status
ssh root@edge-eu
systemctl status nginx
journalctl -u nginx -f
```

---

## Summary: How to Use CDN

1. **Deploy edge-cdn role** (creates `cdn.example.com` with GeoDNS)
2. **Change your app URLs** from `app.example.com` to `cdn.example.com`
3. **Test**: Check `X-Cache-Status` header (first=MISS, second=HIT)
4. **Monitor**: Cache hit rate in Grafana (target > 70%)
5. **Optimize**: Move more static assets through CDN
6. **Scale**: As traffic grows, bandwidth savings increase

**Simple rule**: If a URL serves the same content to all users and doesn't change often → route through CDN.
