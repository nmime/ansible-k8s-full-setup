#!/bin/bash
set -e
PROJECT=${1:-k8s}
echo "=== Tearing down project: $PROJECT ==="
if [ -f "${HOME}/.env" ]; then
  set -a; . "${HOME}/.env"; set +a
else
  echo "  No ${HOME}/.env found; continuing with existing environment"
fi

hcloud load-balancer delete ${PROJECT}-lb 2>/dev/null || echo "  No LB"
for s in $(hcloud server list -o noheader -o columns=name | grep "^${PROJECT}-"); do
  echo "  Deleting server: $s"
  hcloud server delete "$s" 2>/dev/null || true
done
for v in $(hcloud volume list -o noheader -o columns=name | grep "^${PROJECT}-"); do
  hcloud volume detach "$v" 2>/dev/null || true
  sleep 2
  hcloud volume delete "$v" 2>/dev/null || true
done
for f in $(hcloud firewall list -o noheader -o columns=name | grep "^${PROJECT}-"); do
  hcloud firewall delete "$f" 2>/dev/null || true
done
hcloud ssh-key delete ${PROJECT}-key 2>/dev/null || true
for sub in $(hcloud network describe ${PROJECT}-network -o json 2>/dev/null | jq -r '.subnets[].ip_range' 2>/dev/null); do
  hcloud network remove-subnet ${PROJECT}-network --ip-range "$sub" 2>/dev/null || true
done
hcloud network delete ${PROJECT}-network 2>/dev/null || true
rm -f "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/playbooks/${PROJECT}-infra-facts.yml"
rm -rf /root/.kube/config
echo "=== Teardown complete: $PROJECT ==="
