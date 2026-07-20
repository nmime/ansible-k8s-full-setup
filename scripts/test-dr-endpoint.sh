#!/usr/bin/env bash
# Provision or remove a campaign-scoped, TLS-protected MinIO DR endpoint.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/load-project-env.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/load-project-env.sh"

ACTION="${1:-}"
CAMPAIGN="${2:-}"
DNS_ZONE="${TEST_DR_DNS_ZONE:-n0xeid.xyz}"
LOCATION="${TEST_DR_LOCATION:-hel1}"
SERVER_TYPE="${TEST_DR_SERVER_TYPE:-cx23}"
STATE_ROOT="${TEST_DR_STATE_ROOT:-${ROOT_DIR}/.campaign-state}"
MINIO_IMAGE="minio/minio:RELEASE.2025-04-22T22-12-26Z"
MC_IMAGE="minio/mc:RELEASE.2025-04-16T18-13-26Z"
CADDY_IMAGE="caddy:2.10.2-alpine"
CURL_IMAGE="curlimages/curl:8.17.0"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[test-dr] %s\n' "$*"; }
usage() {
  cat <<'EOF'
Usage: test-dr-endpoint.sh up|down CAMPAIGN

Creates one disposable Hetzner VPS running pinned MinIO and Caddy images.
Credentials come from the repository-local .env and are never printed. `up`
prints only the non-secret exports needed by the five-tier runner.
EOF
}

[[ "$ACTION" == up || "$ACTION" == down ]] || { usage >&2; exit 2; }
[[ "$CAMPAIGN" =~ ^[a-z0-9][a-z0-9-]{2,30}$ ]] || fail "invalid campaign name"
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN is required}"
command -v hcloud >/dev/null || fail "hcloud is required"
command -v jq >/dev/null || fail "jq is required"
command -v ssh-keygen >/dev/null || fail "ssh-keygen is required"

PROJECT="${CAMPAIGN}-dr"
DOMAIN="${PROJECT}.${DNS_ZONE}"
STATE_DIR="${STATE_ROOT}/${CAMPAIGN}"
STATE_FILE="${STATE_DIR}/dr.env"
KNOWN_HOSTS_FILE="${STATE_DIR}/known_hosts"
SSH_KEY_FILE="${TEST_DR_SSH_PUBLIC_KEY:-${HOME}/.ssh/id_ed25519.pub}"

remove_endpoint() {
  local failures=0
  hcloud server delete "$PROJECT" >/dev/null 2>&1 || true
  hcloud firewall delete "${PROJECT}-fw" >/dev/null 2>&1 || true
  hcloud ssh-key delete "${PROJECT}-key" >/dev/null 2>&1 || true
  hcloud zone rrset delete "$DNS_ZONE" "$PROJECT" A >/dev/null 2>&1 || true
  hcloud server describe "$PROJECT" >/dev/null 2>&1 && failures=$((failures + 1))
  hcloud firewall describe "${PROJECT}-fw" >/dev/null 2>&1 && failures=$((failures + 1))
  hcloud ssh-key describe "${PROJECT}-key" >/dev/null 2>&1 && failures=$((failures + 1))
  rm -f "$STATE_FILE" "$KNOWN_HOSTS_FILE"
  rmdir "$STATE_DIR" 2>/dev/null || true
  [[ "$failures" -eq 0 ]] || fail "DR cleanup verification failed"
  log "verified removed ${PROJECT}; unrelated resources and DNS records were preserved"
}

if [[ "$ACTION" == down ]]; then
  remove_endpoint
  exit 0
fi

: "${BACKUP_DR_ACCESS_KEY:?BACKUP_DR_ACCESS_KEY is required}"
: "${BACKUP_DR_SECRET_KEY:?BACKUP_DR_SECRET_KEY is required}"
[[ ${#BACKUP_DR_ACCESS_KEY} -ge 8 ]] || fail "BACKUP_DR_ACCESS_KEY must contain at least 8 characters"
[[ ${#BACKUP_DR_SECRET_KEY} -ge 16 ]] || fail "BACKUP_DR_SECRET_KEY must contain at least 16 characters"
[[ -f "$SSH_KEY_FILE" ]] || fail "SSH public key not found: $SSH_KEY_FILE"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
touch "$KNOWN_HOSTS_FILE"
chmod 600 "$KNOWN_HOSTS_FILE"

hcloud server describe "$PROJECT" >/dev/null 2>&1 \
  && fail "${PROJECT} already exists; run down first or choose another campaign"

cleanup_failed_up() {
  local rc=$?
  if [[ "$rc" -ne 0 ]]; then
    log "provisioning failed; removing partial DR resources"
    remove_endpoint || true
  fi
  exit "$rc"
}
trap cleanup_failed_up EXIT

CLIENT_IP=$(curl -4fsS --max-time 10 https://api.ipify.org)
[[ "$CLIENT_IP" =~ ^[0-9.]+$ ]] || fail "could not determine controller IPv4"

SSH_FINGERPRINT=$(ssh-keygen -lf "$SSH_KEY_FILE" -E md5 | awk '{sub(/^MD5:/, "", $2); print $2}')
SSH_KEY_NAME=$(hcloud ssh-key list -o json | jq -r --arg fingerprint "$SSH_FINGERPRINT" \
  '.[] | select(.fingerprint == $fingerprint) | .name' | head -n 1)
if [[ -z "$SSH_KEY_NAME" ]]; then
  SSH_KEY_NAME="${PROJECT}-key"
  hcloud ssh-key create --name "$SSH_KEY_NAME" --public-key-from-file "$SSH_KEY_FILE" >/dev/null
fi
hcloud firewall create --name "${PROJECT}-fw" >/dev/null
hcloud firewall add-rule "${PROJECT}-fw" --direction in --protocol tcp --port 22 --source-ips "${CLIENT_IP}/32" >/dev/null
hcloud firewall add-rule "${PROJECT}-fw" --direction in --protocol tcp --port 80 --source-ips 0.0.0.0/0 --source-ips ::/0 >/dev/null
hcloud firewall add-rule "${PROJECT}-fw" --direction in --protocol tcp --port 443 --source-ips 0.0.0.0/0 --source-ips ::/0 >/dev/null
hcloud server create \
  --name "$PROJECT" --type "$SERVER_TYPE" --image ubuntu-24.04 \
  --location "$LOCATION" --ssh-key "$SSH_KEY_NAME" --firewall "${PROJECT}-fw" >/dev/null

SERVER_IP=$(hcloud server describe "$PROJECT" -o json | jq -r '.public_net.ipv4.ip')
[[ "$SERVER_IP" =~ ^[0-9.]+$ ]] || fail "server IPv4 was not assigned"
hcloud zone rrset set-records --record "$SERVER_IP" "$DNS_ZONE" "$PROJECT" A >/dev/null

for _ in $(seq 1 60); do
  if ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN_HOSTS_FILE" -o ConnectTimeout=5 "root@${SERVER_IP}" true 2>/dev/null; then break; fi
  sleep 5
done
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN_HOSTS_FILE" -o ConnectTimeout=10 "root@${SERVER_IP}" true \
  || fail "SSH did not become ready"

BUCKET="${TEST_DR_BUCKET:-${BACKUP_DR_BUCKET:-${CAMPAIGN}-backups}}"
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail "invalid S3 bucket name"

# Secrets travel only over the authenticated SSH channel and are stored in a
# root-only environment file on this disposable server.
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN_HOSTS_FILE" "root@${SERVER_IP}" \
  "MINIO_ROOT_USER=$(printf '%q' "$BACKUP_DR_ACCESS_KEY") MINIO_ROOT_PASSWORD=$(printf '%q' "$BACKUP_DR_SECRET_KEY") DOMAIN=$(printf '%q' "$DOMAIN") BUCKET=$(printf '%q' "$BUCKET") MINIO_IMAGE=$(printf '%q' "$MINIO_IMAGE") MC_IMAGE=$(printf '%q' "$MC_IMAGE") CADDY_IMAGE=$(printf '%q' "$CADDY_IMAGE") CURL_IMAGE=$(printf '%q' "$CURL_IMAGE") bash -s" <<'REMOTE'
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io
systemctl enable --now docker
install -d -m 0700 /opt/test-dr /var/lib/test-dr-minio
install -d -m 0700 /opt/test-dr/mc
printf 'MINIO_ROOT_USER=%s\nMINIO_ROOT_PASSWORD=%s\n' "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" > /opt/test-dr/minio.env
chmod 0600 /opt/test-dr/minio.env
printf '%s {\n  reverse_proxy minio:9000\n}\n' "$DOMAIN" > /opt/test-dr/Caddyfile
docker network create test-dr >/dev/null 2>&1 || true
docker run -d --name minio --restart unless-stopped --network test-dr \
  --env-file /opt/test-dr/minio.env -v /var/lib/test-dr-minio:/data \
  "$MINIO_IMAGE" server /data --console-address :9001 >/dev/null
docker run -d --name caddy --restart unless-stopped --network test-dr \
  -p 80:80 -p 443:443 -v /opt/test-dr/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v test-dr-caddy-data:/data -v test-dr-caddy-config:/config "$CADDY_IMAGE" >/dev/null
for _ in $(seq 1 60); do
  docker run --rm --network test-dr "$CURL_IMAGE" -fsS http://minio:9000/minio/health/live >/dev/null 2>&1 && break
  sleep 2
done
docker run --rm --network test-dr -v /opt/test-dr/mc:/config "$MC_IMAGE" \
  --config-dir /config alias set dr http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
docker run --rm --network test-dr -v /opt/test-dr/mc:/config "$MC_IMAGE" \
  --config-dir /config mb --ignore-existing "dr/${BUCKET}" >/dev/null
REMOTE

for _ in $(seq 1 90); do
  if curl -fsS --max-time 10 "https://${DOMAIN}/minio/health/live" >/dev/null 2>&1; then break; fi
  sleep 5
done
curl -fsS --max-time 15 "https://${DOMAIN}/minio/health/live" >/dev/null \
  || fail "TLS MinIO health endpoint did not become ready"

cat > "$STATE_FILE" <<EOF
BACKUP_DR_ENDPOINT=https://${DOMAIN}
BACKUP_DR_BUCKET=${BUCKET}
TEST_DR_PROJECT=${PROJECT}
TEST_DR_DOMAIN=${DOMAIN}
EOF
chmod 600 "$STATE_FILE"
trap - EXIT
log "ready: https://${DOMAIN} (bucket ${BUCKET})"
printf 'export BACKUP_DR_ENDPOINT=%q\n' "https://${DOMAIN}"
printf 'export BACKUP_DR_BUCKET=%q\n' "$BUCKET"
printf 'export TEST_DR_STATE_FILE=%q\n' "$STATE_FILE"
