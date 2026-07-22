# HIPAA-Oriented Technical Hardening

This repository provides an optional set of technical controls that may
support a regulated-workload security program. It does **not** certify HIPAA
compliance. HIPAA applicability, risk analysis, policies, business associate
agreements, workforce procedures, application controls, retention, and audit
evidence remain the operator's responsibility. Use current official
[HHS Security Rule guidance](https://www.hhs.gov/hipaa/for-professionals/security/index.html)
and qualified legal/security review for the real environment.

The option is **off in every named profile**. That is deliberate: a technical
flag cannot determine whether an organization has met its regulatory duties.

## Select and apply it

The canonical selector is:

```yaml
compliance:
  hipaa:
    enabled: true
    log_redaction_enabled: true
```

Use the lifecycle commands to add it during initial deployment or later:

```bash
cd platform-orchestrator
./platform.sh enable hipaa
./platform.sh validate
./platform.sh deploy hipaa
```

`enable hipaa` also selects Vault/secrets and the full observability core,
because the hardening contract requires them. `deploy hipaa` reconciles the
network-security and observability roles before the final hardening assertions.
Direct Ansible users can still pass the backwards-compatible
`hipaa_compliance=true` variable, but the nested selector is preferred.

## What automation actually enforces

| Control | Implementation |
|---|---|
| Host auditing | auditd rules for identity files, sudo/auth activity, command execution, and selected network activity on the bastion and Kubernetes nodes |
| Host maintenance | auditd and unattended upgrades are installed by the network role; SSH hardening, UFW, and fail2ban are part of the base platform |
| Vault transport | deployment fails if Vault internal TLS is disabled or certificate verification is disabled |
| Pod-network transport | the profile contract and hardening role require Cilium transparent encryption |
| Log redaction | active collector pipelines replace SSN, US phone, and email-shaped patterns before shipping through Promtail, Filebeat, or Fluentd |
| Secret handling | generated credentials remain in Ansible-Vault-encrypted local state; Vault unseal keys/root token are not placed into Kubernetes auto-unseal Secrets or CronJobs |
| Selection safety | disabling dependencies is blocked while HIPAA-oriented hardening is selected; generic automated rollback is refused |

The log patterns currently replace:

- `123-45-6789` with `XXX-XX-XXXX`;
- `(555) 123-4567` with `(XXX) XXX-XXXX`;
- email-shaped strings with `[REDACTED_EMAIL]`.

These rules are intentionally visible in
`roles/k8s-observability/tasks/main.yml` so reviewers can verify the exact
collector configuration. They are basic defense in depth, not a general PHI
detector. Applications must avoid logging sensitive values in the first place.

## What it does not enforce

The playbook does not claim or automatically provide:

- a completed HIPAA risk analysis or risk-management plan;
- a BAA with any infrastructure, SaaS, email, DNS, backup, or support provider;
- application authorization, minimum-necessary access, field-level
  encryption, consent, data classification, or data-loss prevention;
- universal service-level mTLS. Vault uses TLS and Cilium provides transparent
  pod-network encryption, but each application's transport requirements must
  still be reviewed;
- six-year retention or any universal retention period;
- proof that every possible identifier is removed from logs;
- centralized SIEM review, workforce access review, breach notification,
  training, penetration tests, or disaster-recovery exercises;
- HSM/KMS auto-unseal. The current Vault workflow uses protected manual
  recovery material unless the operator designs and deploys an approved
  external seal integration;
- compliance for workloads deployed later by Argo CD or another application
  delivery system.

## Verification

Run offline validation before mutation:

```bash
ansible-playbook playbooks/validate_profile.yml \
  -e @platform-orchestrator/platform.yaml
```

After applying the selection, record evidence from the authorized cluster:

```bash
# Vault TLS/status
kubectl exec -n vault vault-0 -- vault status

# Cilium encryption status (command availability depends on the Cilium image)
kubectl -n kube-system exec ds/cilium -- cilium-dbg encrypt status

# Host audit rules
ssh root@BASTION 'auditctl -l'

# Collector values: inspect whichever stack the profile selected
helm get values promtail -n monitoring -o yaml
helm get values filebeat -n elasticsearch -o yaml
helm get values fluentd -n logging-agents -o yaml
```

Use synthetic values only when testing redaction. Confirm that the stored log
contains the replacement text, then delete the test record according to the
environment's data-handling policy. Also test false positives: replacing every
email-shaped string can reduce operational usefulness.

## Change and rollback boundary

```bash
./platform.sh disable hipaa
```

Disabling changes desired state but does not remove existing audit rules or
rewrite stored logs. `./platform.sh remove hipaa ...` is intentionally refused:
security controls span hosts and the cluster and cannot be generically reversed
without knowing the organization's policy. Review each control, preserve audit
evidence, and perform any rollback under an approved change record.

## Production checklist

- Complete and approve the environment-specific risk analysis.
- Verify provider agreements and service eligibility for regulated data.
- Confirm application authorization and sensitive-data logging rules.
- Verify Vault TLS, Cilium encryption, auditd delivery, and collector redaction
  from live evidence.
- Export audit/backup data to an approved external retention target.
- Test restores in isolation and record RPO/RTO evidence.
- Review Kubernetes RBAC, cloud IAM, administrator MFA, break-glass access, and
  periodic access recertification.
- Establish incident response, breach assessment/notification, monitoring,
  workforce training, and recurring control tests.

Treat this playbook option as one reviewed control set inside that larger
program, never as a compliance label.
