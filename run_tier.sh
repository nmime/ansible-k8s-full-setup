#!/bin/bash
TIER=${1:?"Usage: $0 <minimal|medium|production>"}
PROJECT="k8s-${TIER}"
DOMAIN="${TIER}.k8s-test.example.com"
EMAIL="test@k8s-test.example.com"
LOGFILE="/root/run-${TIER}.log"

echo "=== Starting $TIER at $(date) ===" | tee "$LOGFILE"
set -a; . ${HOME}/.env; set +a
export HCLOUD_TOKEN

cd /root/ansible-k8s-full-setup-fix
set +e
ansible-playbook playbooks/deploy_platform.yml \
  -e tier="$TIER" \
  -e domain="$DOMAIN" \
  -e email="$EMAIL" \
  -e project_name="$PROJECT" \
  -v 2>&1 | tee -a "$LOGFILE"
RETCODE=${PIPESTATUS[0]}
set -e

if [ $RETCODE -eq 0 ]; then
  echo "=== $TIER SUCCEEDED at $(date) ===" | tee -a "$LOGFILE"
else
  echo "=== $TIER FAILED (rc=$RETCODE) at $(date) ===" | tee -a "$LOGFILE"
fi

echo "=== Teardown $TIER at $(date) ===" | tee -a "$LOGFILE"
bash /root/ansible-k8s-full-setup-fix/teardown.sh "$PROJECT" 2>&1 | tee -a "$LOGFILE"
echo "=== Done $TIER at $(date) ===" | tee -a "$LOGFILE"
exit $RETCODE
