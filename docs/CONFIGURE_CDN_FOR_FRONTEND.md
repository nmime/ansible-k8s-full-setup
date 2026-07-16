# Configure CDN for a Frontend

This note describes a generic pattern for serving frontend assets through the edge CDN while keeping dynamic API traffic on the application origin.

## Request Flow

```
User → cdn.example.com → GeoDNS → nearest edge cache
                                      │
                                      ├─ HIT: return cached asset
                                      └─ MISS: fetch from origin.example.com
```

The CDN does not own the application content. It proxies the origin and caches responses according to the edge Nginx rules.

## 1. Add a CDN Hostname at the Gateway

Expose the same frontend service through both the direct app hostname and the CDN hostname:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: frontend
spec:
  parentRefs:
    - name: main-gateway
      namespace: gateway
  hostnames:
    - "app.example.com"
    - "cdn.example.com"
  rules:
    - backendRefs:
        - name: frontend
          port: 80
```

Use names that match your chart, service, and environment.

## 2. Deploy the Edge CDN

```bash
export GCORE_API_KEY="your_gcore_api_key"

ansible-playbook playbooks/edge-cdn.yml \
  -e edge_cdn_confirm=true \
  -e "domain=example.com" \
  -e "origin_server_ip=<kubernetes-load-balancer-ip>"
```

This creates regional edge servers, configures GeoDNS, installs Nginx caching, enables TLS, and registers health checks.

## 3. Configure Frontend Asset URLs

Set the build-time public asset URL to the CDN hostname while keeping API calls on the API or app origin.

### Next.js

```javascript
// next.config.js
module.exports = {
  assetPrefix: process.env.CDN_URL || '',
}
```

### Vite or React

```javascript
// vite.config.js
export default defineConfig({
  base: process.env.CDN_URL || '/',
})
```

### Vue CLI

```javascript
// vue.config.js
module.exports = {
  publicPath: process.env.CDN_URL || '/',
}
```

Build with:

```bash
CDN_URL=https://cdn.example.com npm run build
```

Generated HTML should load static assets from `https://cdn.example.com`, while API calls continue to use the normal API hostname.

## 4. Build and Deploy

```bash
docker build --build-arg CDN_URL=https://cdn.example.com \
  -t registry.example.com/frontend:v1.2.0 .

docker push registry.example.com/frontend:v1.2.0
```

Then update your deployment or Helm values with the new image tag and, if your chart supports it, the CDN hostname.

## 5. Test

```bash
curl -I https://app.example.com/
curl -I https://cdn.example.com/
curl -I https://cdn.example.com/assets/main.js
```

Expected results:

- Direct and CDN hostnames return success responses.
- First CDN asset request is typically a cache miss.
- Repeated CDN asset requests should become cache hits.
- API responses should not be cached unless explicitly configured.

## Cache Behavior

| Path type | Cache | Typical TTL |
|-----------|-------|-------------|
| JavaScript, CSS, images, fonts | yes | 30 days |
| HTML pages | yes, if safe | 1 hour or less |
| API routes | no | no cache |
| Everything else | yes | short default TTL |

Prefer content-hashed asset filenames so new deployments naturally bypass old cached assets.

## Common Patterns

### Assets Through CDN, HTML Direct

Best for apps with user-specific HTML. Serve HTML from `app.example.com` and reference static assets on `cdn.example.com`.

### Entire Static Site Through CDN

Best for marketing sites, documentation, blogs, and static exports. Serve both HTML and assets through the CDN with short HTML TTLs and long asset TTLs.

### Separate Static Hostname

Best for large single-page apps. Use `static.example.com` for assets and `app.example.com` for the application shell and dynamic routes.

## Summary

1. Add the CDN hostname to the frontend route.
2. Deploy and verify the edge CDN.
3. Build the frontend with CDN asset URLs.
4. Keep API calls direct unless they are explicitly safe to cache.
5. Use content-hashed filenames for safe long-lived asset caching.
