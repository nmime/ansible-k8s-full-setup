#!/bin/bash
set -euo pipefail

KUBESPRAY_DIR=/root/ansible-k8s-full-setup-fix/playbooks/kubespray
INVENTORY=$KUBESPRAY_DIR/inventory/k8s/hosts.yml
LOG_DIR=/tmp/cycle-tests
TOTAL_CYCLES=3

mkdir -p $LOG_DIR

source $KUBESPRAY_DIR/.venv/bin/activate

verify_cluster() {
    local attempt=0
    local max_attempts=30
    while [ $attempt -lt $max_attempts ]; do
        if ssh -o StrictHostKeyChecking=no -o ProxyCommand='ssh -o StrictHostKeyChecking=no -W %h:%p root@95.216.147.95' root@10.0.2.2 'kubectl get nodes' 2>/dev/null | grep -q 'Ready'; then
            echo "Cluster nodes ready"
            ssh -o StrictHostKeyChecking=no -o ProxyCommand='ssh -o StrictHostKeyChecking=no -W %h:%p root@95.216.147.95' root@10.0.2.2 'kubectl get nodes -o wide; kubectl get pods -A' 2>/dev/null
            local not_running
            not_running=$(ssh -o StrictHostKeyChecking=no -o ProxyCommand='ssh -o StrictHostKeyChecking=no -W %h:%p root@95.216.147.95' root@10.0.2.2 'kubectl get pods -A --no-headers 2>/dev/null | grep -v Running | grep -v Completed | wc -l' 2>/dev/null)
            if [ "$not_running" -eq 0 ] 2>/dev/null; then
                echo "All pods running - cluster healthy"
                return 0
            fi
            echo "Waiting for pods... ($not_running not ready)"
        fi
        attempt=$((attempt + 1))
        sleep 10
    done
    echo "VERIFICATION FAILED after $max_attempts attempts"
    return 1
}

for cycle in $(seq 1 $TOTAL_CYCLES); do
    echo "================================================================"
    echo "CYCLE $cycle of $TOTAL_CYCLES - $(date)"
    echo "================================================================"

    if [ $cycle -gt 1 ]; then
        echo "--- TEARDOWN (reset) ---"
        cd $KUBESPRAY_DIR
        ansible-playbook -i $INVENTORY reset.yml -b --become-user=root -e reset_confirmation=yes 2>&1 | tee $LOG_DIR/cycle${cycle}_reset.log
        RESET_RC=${PIPESTATUS[0]}
        echo "Reset exit code: $RESET_RC"
        if [ $RESET_RC -ne 0 ]; then
            echo "CYCLE $cycle RESET FAILED"
            exit 1
        fi
        rm -rf /tmp/ansible_facts/* /tmp/kubespray-cp/* 2>/dev/null
    fi

    echo "--- DEPLOY (cluster.yml) ---"
    cd $KUBESPRAY_DIR
    ansible-playbook -i $INVENTORY cluster.yml -b --become-user=root 2>&1 | tee $LOG_DIR/cycle${cycle}_deploy.log
    DEPLOY_RC=${PIPESTATUS[0]}
    echo "Deploy exit code: $DEPLOY_RC"
    if [ $DEPLOY_RC -ne 0 ]; then
        echo "CYCLE $cycle DEPLOY FAILED"
        exit 1
    fi

    echo "--- VERIFY ---"
    if verify_cluster; then
        echo "CYCLE $cycle PASSED - $(date)"
    else
        echo "CYCLE $cycle VERIFICATION FAILED"
        exit 1
    fi

    echo ""
done

echo "================================================================"
echo "ALL $TOTAL_CYCLES CYCLES PASSED SUCCESSFULLY - $(date)"
echo "================================================================"
