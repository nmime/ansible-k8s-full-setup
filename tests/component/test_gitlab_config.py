"""Component tests: GitLab Helm values structure vs chart 10.x."""
import os, re, pytest, yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GITLAB_TASKS_PATH = os.path.join(REPO_ROOT, "roles", "gitlab-selfhosted", "tasks", "main.yml")
DEFAULTS_PATH = os.path.join(REPO_ROOT, "defaults", "main.yml")
GENERATE_SECRETS_PATH = os.path.join(
    REPO_ROOT, "roles", "generate-secrets", "tasks", "main.yml"
)

def read(path):
    with open(path) as f:
        return f.read()

class TestChart10ValuesStructure:
    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(GITLAB_TASKS_PATH)

    @pytest.mark.component
    def test_global_edition_present(self):
        assert "edition:" in self.content

    @pytest.mark.component
    def test_global_hosts_present(self):
        assert "hosts:" in self.content and "domain:" in self.content

    @pytest.mark.component
    def test_global_email_present(self):
        assert "email:" in self.content

    @pytest.mark.component
    def test_chart_gateway_and_issuer_are_disabled_for_platform_gateway(self):
        assert "gatewayApi:" in self.content
        assert "installEnvoy: false" in self.content
        assert self.content.count("configureCertmanager: false") >= 2

    @pytest.mark.component
    def test_webservice_configured(self):
        assert "webservice:" in self.content and "replicaCount:" in self.content

    @pytest.mark.component
    def test_sidekiq_configured(self):
        assert "sidekiq:" in self.content

    def test_production_replica_floors_and_caps_are_wired_to_chart_values(self):
        for variable in (
            "gitlab_webservice_max_replicas",
            "gitlab_sidekiq_max_replicas",
            "gitlab_registry_max_replicas",
        ):
            assert variable in self.content
        assert "minReplicas: '{{ gitlab_sidekiq_replicas | int }}'" in self.content
        assert "maxReplicas: '{{ gitlab_webservice_max_replicas | int }}'" in self.content
        assert "maxReplicas: '{{ gitlab_sidekiq_max_replicas | int }}'" in self.content
        assert "maxReplicas: '{{ gitlab_registry_max_replicas | int }}'" in self.content

    def test_heavy_rails_workloads_require_safe_cross_node_spread(self):
        tasks = yaml.safe_load(self.content)
        install = next(task for task in tasks if task.get("name") == "Install GitLab with Helm")
        rails = install["kubernetes.core.helm"]["values"]["gitlab"]
        for component in ("webservice", "sidekiq"):
            strategy = rails[component]["deployment"]["strategy"]
            assert strategy == {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            }
            spread = rails[component]["topologySpreadConstraints"]
            assert len(spread) == 1
            assert spread[0]["minDomains"] == 2
            assert spread[0]["topologyKey"] == "kubernetes.io/hostname"
            assert spread[0]["whenUnsatisfiable"] == "DoNotSchedule"
            assert spread[0]["nodeAffinityPolicy"] == "Honor"
            assert spread[0]["nodeTaintsPolicy"] == "Honor"
            expressions = spread[0]["labelSelector"]["matchExpressions"]
            assert expressions[0]["values"] == ["gitlab"]
            assert expressions[1]["values"] == [component]

            # With three production workers, hard maxSkew=1 keeps the normal
            # two-replica floor on separate nodes while still admitting the
            # configured four-replica cap as a 2/1/1 distribution.
            assert max([1, 1, 0]) - min([1, 1, 0]) <= spread[0]["maxSkew"]
            assert max([2, 1, 1]) - min([2, 1, 1]) <= spread[0]["maxSkew"]

        affinity = next(
            task
            for task in tasks
            if task.get("name")
            == "Add preferred cross-component anti-affinity to GitLab Rails workloads"
        )
        assert affinity["loop"] == [
            {"deployment": "gitlab-webservice-default", "component": "webservice"},
            {"deployment": "gitlab-sidekiq-all-in-1-v2", "component": "sidekiq"},
        ]
        definition = affinity["kubernetes.core.k8s"]["definition"]
        assert definition["metadata"]["name"] == "{{ item.deployment }}"
        anti_affinity = definition["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]
        assert "requiredDuringSchedulingIgnoredDuringExecution" not in anti_affinity
        preferred = anti_affinity["preferredDuringSchedulingIgnoredDuringExecution"]
        assert preferred[0]["weight"] == 100
        assert preferred[0]["podAffinityTerm"]["labelSelector"]["matchExpressions"][1][
            "values"
        ] == ["webservice", "sidekiq"]
        assert "Rebalance GitLab Rails workloads when a rolling update co-locates them" in self.content

    def test_webservice_memory_limit_is_profile_tunable(self):
        tasks = yaml.safe_load(self.content)
        facts = next(
            task
            for task in tasks
            if task.get("name") == "Set GitLab tier-specific variables"
        )
        install = next(
            task for task in tasks if task.get("name") == "Install GitLab with Helm"
        )
        webservice = install["kubernetes.core.helm"]["values"]["gitlab"]["webservice"]
        assert (
            "gitlab.webservice_memory_limit"
            in facts["set_fact"]["gitlab_webservice_memory_limit"]
        )
        assert webservice["resources"]["requests"]["memory"] == (
            "{{ gitlab_webservice_memory_request }}"
        )
        assert webservice["resources"]["limits"]["memory"] == (
            "{{ gitlab_webservice_memory_limit }}"
        )

    def test_public_webservice_route_is_explicitly_opt_in(self):
        tasks = yaml.safe_load(self.content)
        facts = next(
            task
            for task in tasks
            if task.get("name") == "Set GitLab tier-specific variables"
        )
        assert (
            "default(false)"
            in facts["set_fact"]["gitlab_public_webservice_enabled"]
        )

        private_route = next(
            task
            for task in tasks
            if task.get("name") == "Create private GitLab Gateway API HTTPRoute"
        )
        public_route = next(
            task
            for task in tasks
            if task.get("name")
            == "Publish the canonical GitLab hostname through the main Gateway"
        )
        disable_signup = next(
            task
            for task in tasks
            if task.get("name")
            == "Disable public GitLab self-registration before publishing the webservice"
        )
        assert disable_signup["when"] == "gitlab_public_webservice_enabled | bool"
        assert "signup_enabled: false" in (
            disable_signup["ansible.builtin.command"]["argv"][-1]
        )
        assert tasks.index(disable_signup) < tasks.index(public_route)
        private_spec = private_route["kubernetes.core.k8s"]["definition"]["spec"]
        assert {parent["name"] for parent in private_spec["parentRefs"]} == {
            "admin-gateway"
        }
        assert private_spec["hostnames"] == (
            "{{ [gitlab_domain] + gitlab_domain_aliases }}"
        )
        public_spec = public_route["kubernetes.core.k8s"]["definition"]["spec"]
        assert public_route["when"] == "gitlab_public_webservice_enabled | bool"
        assert public_spec["parentRefs"] == [
            {
                "name": "main-gateway",
                "namespace": "cilium-system",
                "sectionName": "https",
            }
        ]
        assert public_spec["hostnames"] == ["{{ gitlab_domain }}"]

        install = next(
            task for task in tasks if task.get("name") == "Install GitLab with Helm"
        )
        app_config = install["kubernetes.core.helm"]["values"]["global"]["appConfig"]
        assert app_config["initialDefaults"]["signupEnabled"] is False

    def test_runner_uses_internal_service_behind_vpn_only_gateway(self):
        tasks = yaml.safe_load(self.content)
        install = next(
            task
            for task in tasks
            if task.get("name") == "Install GitLab Runner with Helm"
        )
        values = install["kubernetes.core.helm"]["values"]
        internal = (
            "http://gitlab-webservice-default.{{ gitlab_namespace }}.svc:8181"
        )
        assert values["gitlabUrl"] == internal
        assert f'clone_url = "{internal}"' in values["runners"]["config"]
        assert "https://{{ gitlab_domain }}" not in values["runners"]["config"]
        assert values["metrics"] == {"enabled": True}
        assert values["serviceAccount"] == {"create": True}
        assert values["nodeSelector"] == {
            "node-role.kubernetes.io/worker": "true"
        }
        assert values["runners"]["tags"] == "kubernetes,k8s"
        assert (
            "request_concurrency = {{ gitlab_runner_concurrent | int }}"
            in values["runners"]["config"]
        )
        assert (
            'node_selector = { "node-role.kubernetes.io/worker" = "true" }'
            in values["runners"]["config"]
        )
        assert 'environment = ["HOME=/tmp"]' in values["runners"]["config"]
        assert values["podSecurityContext"]["seccompProfile"] == {
            "type": "RuntimeDefault"
        }
        assert (
            "[runners.kubernetes.pod_security_context.seccomp_profile]"
            in values["runners"]["config"]
        )
        assert 'type = "RuntimeDefault"' in values["runners"]["config"]

        scrape = next(
            task
            for task in tasks
            if task.get("name") == "Create GitLab Runner VMPodScrape"
        )
        scrape_definition = scrape["kubernetes.core.k8s"]["definition"]
        assert scrape_definition["kind"] == "VMPodScrape"
        assert scrape_definition["spec"]["selector"]["matchLabels"] == {
            "app": "gitlab-runner"
        }
        assert scrape_definition["spec"]["podMetricsEndpoints"] == [
            {"port": "metrics", "interval": "30s", "path": "/metrics"}
        ]

        policy = next(
            task
            for task in tasks
            if task.get("name")
            == "Allow VictoriaMetrics to scrape GitLab Runner metrics"
        )
        policy_spec = policy["kubernetes.core.k8s"]["definition"]["spec"]
        assert policy_spec["endpointSelector"]["matchLabels"] == {
            "app": "gitlab-runner"
        }
        assert policy_spec["ingress"][0]["toPorts"][0]["ports"] == [
            {"port": "9252", "protocol": "TCP"}
        ]

    def test_custom_production_capacity_defaults_to_full_mode_memory(self):
        tasks = yaml.safe_load(self.content)
        facts = next(
            task
            for task in tasks
            if task.get("name") == "Set GitLab tier-specific variables"
        )["set_fact"]
        from jinja2 import Environment

        context = {"gitlab": {}, "resource_tier": "medium", "tier": "custom"}
        environment = Environment()
        assert environment.from_string(facts["gitlab_mode"]).render(**context) == "full"
        assert environment.from_string(
            facts["gitlab_webservice_memory_request"]
        ).render(**context) == "2.5Gi"
        assert environment.from_string(
            facts["gitlab_webservice_memory_limit"]
        ).render(**context) == "5Gi"

    def test_readiness_fails_closed_on_rails_placement_and_gitaly_pdb_drift(self):
        gate = self.content.split("- name: Enforce GitLab post-reconcile readiness", 1)[
            1
        ].split("- name: Add preferred cross-component anti-affinity", 1)[0]
        assert "Require hard per-component topology spread" in gate
        assert "whenUnsatisfiable == 'DoNotSchedule'" in gate
        assert "minDomains | default(0) | int) == 2" in gate
        assert "nodeAffinityPolicy == 'Honor'" in gate
        assert "nodeTaintsPolicy == 'Honor'" in gate
        assert "name: gitlab-gitaly" in gate
        assert "spec.maxUnavailable | int) == 0" in gate
        assert "spec.minAvailable is not defined" in gate

    def test_enabled_runner_requires_modern_authentication_token(self):
        gate = self.content.split(
            "- name: Require a GitLab Runner authentication token when Runner is selected",
            1,
        )[1].split("- name: Create GitLab namespace", 1)[0]
        install = self.content.split("- name: Install GitLab Runner with Helm", 1)[1].split(
            "- name: Create S3 cache secret for GitLab Runner", 1
        )[0]
        assert "^glrt-[A-Za-z0-9_-]+(\\.[A-Za-z0-9_-]+)*$" in gate
        assert "vault_encrypt_secrets | default(true) | bool" in gate
        assert "gitlab_runner_concurrent | int > 0" in gate
        assert "gitlab_runner_replicas | int > 0" in gate
        assert "when: gitlab_runner_enabled | bool" in gate
        assert "quiet: true" in gate
        assert "runnerToken: '{{ _gitlab_runner_auth_token }}'" in install
        assert "replicas: '{{ gitlab_runner_replicas | int }}'" in install
        assert "concurrent: '{{ gitlab_runner_concurrent | int }}'" in install
        assert "allow_privilege_escalation = false" in install
        assert 'cap_drop = ["ALL"]' in install
        assert "automount_service_account_token = false" in install
        assert "gitlab_runner_token is defined" not in install
        assert "gitlab_runner_token != ''" not in install
        assert "no_log: true" in install

        bootstrap = self.content.split(
            "- name: Read the persisted GitLab Runner authentication token", 1
        )[1].split("- name: Add GitLab Runner Helm repository", 1)[0]
        assert "platform-gitlab-runner-auth" in bootstrap
        assert "/api/v4/runners/verify" in bootstrap
        assert "s_ansible_k8s_reconcile" in bootstrap
        assert "/api/v4/runners" in bootstrap
        assert "gitlab-gitlab-runner-secret" in bootstrap
        assert "'runner-registration-token'] | b64decode" in bootstrap
        assert "GITLAB_RUNNER_REGISTRATION_TOKEN" in bootstrap
        assert "deployment/gitlab-toolbox -c toolbox" in bootstrap
        assert "runner-token: '{{ _gitlab_runner_auth_token }}'" in bootstrap
        assert bootstrap.count("no_log: true") >= 8

        secrets = read(GENERATE_SECRETS_PATH)
        resolution = secrets.split("- name: Resolve GitLab Runner authentication token", 1)[
            1
        ].split("- name: Require encrypted persistence", 1)[0]
        assert "lookup('env', 'GITLAB_RUNNER_TOKEN')" in resolution
        assert "saved_secrets.gitlab_runner_token" in resolution
        assert "no_log: true" in resolution
        assert secrets.count('gitlab_runner_token: "{{ gitlab_runner_token }}"') == 2
        assert "requires vault_encrypt_secrets=true" in secrets
        assert "Require a modern authentication token when GitLab Runner is selected" in secrets
        assert "the Runner will not be silently omitted" in secrets

    def test_enabled_runner_install_waits_for_deployment_convergence(self):
        tasks = yaml.safe_load(self.content)
        install = next(
            task
            for task in tasks
            if task.get("name") == "Install GitLab Runner with Helm"
        )
        converge = next(
            task
            for task in tasks
            if task.get("name")
            == "Wait for the enabled GitLab Runner deployment to converge"
        )
        assert install["kubernetes.core.helm"]["wait"] is True
        assert install["kubernetes.core.helm"]["force_conflicts"] is True
        assert install["kubernetes.core.helm"]["values"]["concurrent"] == (
            "{{ gitlab_runner_concurrent | int }}"
        )
        assert install["kubernetes.core.helm"]["values"]["replicas"] == (
            "{{ gitlab_runner_replicas | int }}"
        )
        assert converge["kubernetes.core.k8s_info"]["name"] == "gitlab-runner"
        assert converge["when"] == "gitlab_runner_enabled | bool"
        assert converge["retries"] == 60
        assert "readyReplicas" in converge["until"]
        assert "availableReplicas" in converge["until"]
        assert "updatedReplicas" in converge["until"]

    @pytest.mark.component
    def test_gitlab_shell_configured(self):
        assert "gitlab-shell:" in self.content

    @pytest.mark.component
    def test_kas_enabled(self):
        assert "kas:" in self.content

    @pytest.mark.component
    def test_toolbox_enabled(self):
        assert "toolbox:" in self.content

    @pytest.mark.component
    def test_object_store_configured(self):
        assert "object_store:" in self.content or "objectStorage:" in self.content
        for b in ["gitlab-lfs", "gitlab-artifacts", "gitlab-uploads"]:
            assert b in self.content

    @pytest.mark.component
    def test_registry_storage_secret(self):
        assert "gitlab-registry-storage" in self.content
        assert r"checksum_disabled: true" in self.content
        assert r"redirect:\n  disable: true" in self.content

    @pytest.mark.component
    def test_helm_chart_ref(self):
        assert "chart_ref: gitlab/gitlab" in self.content

    @pytest.mark.component
    def test_helm_timeout(self):
        assert "timeout: 30m0s" in self.content

    def test_helm_reclaims_server_side_apply_fields_before_post_patches(self):
        install = self.content.split("- name: Install GitLab with Helm", 1)[1].split(
            "- name: Add cross-component anti-affinity", 1
        )[0]
        assert "force_conflicts: true" in install

    def test_failed_helm_revision_recovery_deletes_only_exact_failed_history(self):
        recovery = self.content.split(
            "- name: Recover a failed GitLab Helm revision without deleting release workloads",
            1,
        )[1].split("- name: Discover failed GitLab database migration Jobs", 1)[0]
        for contract in (
            "owner=helm",
            "name=gitlab",
            "status == 'failed'",
            "item.type == 'helm.sh/release.v1'",
            "sh.helm.release.v1.gitlab.v",
            "item.metadata.labels.owner == 'helm'",
            "item.metadata.labels.name == 'gitlab'",
            "item.metadata.ownerReferences | default([]) | length",
            "item.data.release | default('') | length",
            "uid: '{{ item.metadata.uid }}'",
            "resourceVersion: '{{ item.metadata.resourceVersion }}'",
            "Delete only failed GitLab Helm revisions newer than the deployed predecessor",
            "Require failed revision cleanup to expose the deployed predecessor",
        ):
            assert contract in recovery

        history_path = recovery.split(
            "- name: Discover GitLab Helm release history Secrets", 1
        )[1].split(
            "- name: Recheck GitLab Helm status after failed revision cleanup", 1
        )[0]
        assert "kind: Secret" in history_path
        assert "kind: StatefulSet" not in history_path
        assert "kind: PersistentVolumeClaim" not in history_path
        assert "release_state: absent" not in history_path

    def test_failed_first_install_uninstall_requires_absent_or_data_free_gitaly(self):
        recovery = self.content.split(
            "- name: Recover a failed GitLab Helm revision without deleting release workloads",
            1,
        )[1].split("- name: Discover failed GitLab database migration Jobs", 1)[0]
        first_install = recovery.split(
            "- name: Discover first-install Gitaly StatefulSets", 1
        )[1]
        for contract in (
            "_gitlab_deployed_release_secrets | length == 0",
            "status.readyReplicas",
            "status.phase",
            "spec.volumeName",
            "default(0) | int) == 0",
            "default('')) ==\n             'Pending'",
            "default('') | length) == 0",
            "metadata.name ==\n             'gitlab-gitaly'",
            "metadata.name ==\n             'repo-data-gitlab-gitaly-0'",
            "metadata.labels.heritage",
            "'app.kubernetes.io/managed-by'] == 'Helm'",
            "'meta.helm.sh/release-namespace'] | default('')) == gitlab_namespace",
            "Remove only a proven data-free failed first GitLab release",
            "No workload or PVC was deleted",
        ):
            assert contract in first_install
        assert "release_state: absent" in first_install
        assert "Remove failed GitLab Helm release before reinstall" not in self.content

    def test_post_reconcile_gate_requires_data_bearing_gitlab_pvcs_bound(self):
        gate = self.content.split("- name: Enforce GitLab post-reconcile readiness", 1)[1].split(
            "- name: Add cross-component anti-affinity", 1
        )[0]
        assert "kind: PersistentVolumeClaim" in gate
        assert "release=gitlab" in gate
        assert "app=gitaly" in gate
        assert "Require data-bearing GitLab persistent volume claims to exist" in gate
        assert "--selector=release=gitlab,app=gitaly" in gate
        assert "toolbox backup scratch claim intentionally uses WaitForFirstConsumer" in gate
        assert "--for=jsonpath={.status.phase}=Bound" in gate
        assert "--timeout={{ gitlab_readiness_timeout }}" in gate

    def test_post_reconcile_gate_selects_critical_controllers_by_labels(self):
        gate = self.content.split("- name: Enforce GitLab post-reconcile readiness", 1)[1].split(
            "- name: Add cross-component anti-affinity", 1
        )[0]
        for kind, app in (
            ("StatefulSet", "gitaly"),
            ("Deployment", "webservice"),
            ("Deployment", "sidekiq"),
            ("Deployment", "toolbox"),
        ):
            assert f"kind: {kind}\n            app: {app}" in gate
        assert "Require every critical GitLab controller to exist" in gate
        assert "kubectl rollout status" in gate
        assert "release=gitlab,app=" in gate

    def test_readiness_failure_is_sanitized_and_fail_closed(self):
        gate = self.content.split("- name: Enforce GitLab post-reconcile readiness", 1)[1].split(
            "- name: Add cross-component anti-affinity", 1
        )[0]
        assert "Collect sanitized GitLab PVC readiness metadata" in gate
        assert "Collect sanitized GitLab controller readiness metadata" in gate
        assert "Collect sanitized GitLab warning event metadata" in gate
        assert "Fail closed when GitLab is not fully ready" in gate
        assert "OBJECT_NAME:.involvedObject.name" in gate
        assert ".message" not in gate
        assert "kubectl describe" not in gate

    def test_readiness_timeout_is_configurable_with_bounded_default(self):
        assert "gitlab_readiness_timeout:" in self.content
        assert "gitlab.readiness_timeout | default(''30m'')" in self.content

    def test_chart_10_gitaly_persistence_contract_uses_rendered_values_path(self):
        gitaly = self.content.split("        gitaly:", 1)[1].split("        kas:", 1)[0]
        assert "          maxUnavailable: 0" in gitaly
        assert "minAvailable:" not in gitaly
        assert "          persistence:" in gitaly
        assert "            enabled: true" in gitaly
        assert "            size: '{{ gitlab_gitaly_storage_size }}'" in gitaly
        assert "            storageClass: '{{ storage_class" in gitaly
        assert "persistentVolumeClaim:" not in gitaly

    def test_singleton_gitaly_pdb_blocks_voluntary_total_outage(self):
        tasks = yaml.safe_load(self.content)
        install = next(task for task in tasks if task.get("name") == "Install GitLab with Helm")
        gitaly = install["kubernetes.core.helm"]["values"]["gitlab"]["gitaly"]
        assert gitaly["maxUnavailable"] == 0
        assert "minAvailable" not in gitaly

        readme = read(os.path.join(REPO_ROOT, "README.md"))
        assert "equivalent to `minAvailable: 1`" in readme
        assert "This does\nnot provide node-failure HA" in readme
        assert "temporarily override the PDB" in readme

    def test_unbound_gitaly_size_drift_has_conservative_recovery(self):
        recovery = self.content.split("- name: Inspect existing Gitaly storage", 1)[1].split(
            "- name: Install GitLab with Helm", 1
        )[0]
        assert "rejectattr('spec.resources.requests.storage'" in recovery
        assert "(item.status.phase | default('')) == 'Pending'" in recovery
        assert "(item.spec.volumeName | default('') | length) == 0" in recovery
        assert "(_gitlab_existing_gitaly_ready_replicas | int) == 0" in recovery
        assert "Remove never-ready Gitaly StatefulSets" in recovery
        assert "Remove unbound Gitaly claims" in recovery
        assert "Bound, previously ready, or otherwise potentially data-bearing" in recovery

    def test_gitaly_recovery_requires_one_exact_helm_owned_storage_pair(self):
        recovery = self.content.split("- name: Inspect existing Gitaly storage", 1)[1].split(
            "- name: Install GitLab with Helm", 1
        )[0]
        assert "Require an unambiguous existing Gitaly storage inventory" in recovery
        assert "does not have the exact GitLab Helm" in recovery
        for contract in (
            "metadata.name == 'gitlab-gitaly'",
            "metadata.labels.release == 'gitlab'",
            "metadata.labels.app == 'gitaly'",
            "metadata.labels.heritage == 'Helm'",
            "metadata.labels['app.kubernetes.io/managed-by'] == 'Helm'",
            "metadata.annotations['meta.helm.sh/release-name'] == 'gitlab'",
            "metadata.annotations['meta.helm.sh/release-namespace'] == gitlab_namespace",
            "metadata.ownerReferences | default([]) | length",
            "persistentVolumeClaimRetentionPolicy.whenDeleted == 'Retain'",
            "persistentVolumeClaimRetentionPolicy.whenScaled == 'Retain'",
            "metadata.name == 'repo-data-gitlab-gitaly-0'",
        ):
            assert contract in recovery

    def test_bound_gitaly_template_drift_has_fail_closed_sts_only_recovery(self):
        recovery = self.content.split("- name: Inspect existing Gitaly storage", 1)[1].split(
            "- name: Install GitLab with Helm", 1
        )[0]
        guard = recovery.split(
            "- name: Prove bound Gitaly PVC and controller are safe for StatefulSet-only recovery",
            1,
        )[1].split(
            "- name: Orphan only the drifted Gitaly StatefulSet", 1
        )[0]
        for contract in (
            "status.phase | default('')) == 'Bound'",
            "spec.volumeName | default('') | length) > 0",
            "spec.resources.requests.storage == gitlab_gitaly_storage_size",
            "status.capacity.storage == gitlab_gitaly_storage_size",
            "spec.storageClassName == _gitlab_desired_gitaly_storage_class",
            "status.observedGeneration",
            "status.readyReplicas",
            "status.currentReplicas",
            "status.updatedReplicas",
            "status.availableReplicas",
            "status.currentRevision ==",
            "status.updateRevision",
            "spec.replicas | default(1) | int) == 0",
        ):
            assert contract in guard
        assert "neither fully healthy nor safely scaled to zero" in guard

        orphan = recovery.split(
            "- name: Orphan only the drifted Gitaly StatefulSet", 1
        )[1].split(
            "- name: Re-read retained Gitaly PVC", 1
        )[0]
        assert "kind: StatefulSet" in orphan
        assert "propagationPolicy: Orphan" in orphan
        assert "uid: '{{ _gitlab_existing_gitaly_statefulset.metadata.uid }}'" in orphan
        assert "resourceVersion: '{{ _gitlab_existing_gitaly_statefulset.metadata.resourceVersion }}'" in orphan
        assert "kind: PersistentVolumeClaim" not in orphan

    def test_bound_gitaly_recovery_proves_same_pvc_uid_and_volume_after_orphaning(self):
        recovery = self.content.split("- name: Inspect existing Gitaly storage", 1)[1].split(
            "- name: Install GitLab with Helm", 1
        )[0]
        proof = recovery.split(
            "- name: Prove StatefulSet-only recovery preserved the exact Bound Gitaly PVC",
            1,
        )[1]
        assert "metadata.uid == _gitlab_existing_gitaly_pvc.metadata.uid" in proof
        assert "status.phase == 'Bound'" in proof
        assert "spec.volumeName == _gitlab_existing_gitaly_pvc.spec.volumeName" in proof
        assert "spec.resources.requests.storage == gitlab_gitaly_storage_size" in proof
        assert "status.capacity.storage == gitlab_gitaly_storage_size" in proof
        assert "spec.storageClassName == _gitlab_desired_gitaly_storage_class" in proof
        assert "state: absent" not in proof

class TestDefaultsTasksConsistency:
    @pytest.fixture(autouse=True)
    def _read_all(self):
        self.defaults_raw = read(DEFAULTS_PATH)
        self.tasks_raw = read(GITLAB_TASKS_PATH)

    @pytest.mark.component
    def test_chart_version_in_sync(self):
        d = re.search(r'gitlab_chart_version:\s*["\']?([^"\'\n#]+)', self.defaults_raw)
        t = re.search(r'gitlab_chart_version:\s*([^\n]+)', self.tasks_raw)
        assert d and t
        assert d.group(1).strip("'\"") == t.group(1).strip()

    @pytest.mark.component
    def test_storage_class_used(self):
        assert "storage_class" in self.tasks_raw or "storageClass" in self.tasks_raw

    @pytest.mark.component
    def test_tier_logic_preserved(self):
        assert "gitlab_mode" in self.tasks_raw

    @pytest.mark.component
    def test_toolbox_skips_database_covered_by_native_percona_backup(self):
        assert "--skip db" in self.tasks_raw
        assert "--s3tool awscli" in self.tasks_raw

    @pytest.mark.component
    def test_toolbox_awscli_receives_minio_credentials(self):
        for token in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_DEFAULT_REGION",
            "AWS_REQUEST_CHECKSUM_CALCULATION",
            "accesskey",
            "secretkey",
        ):
            assert token in self.tasks_raw

    @pytest.mark.component
    def test_toolbox_backup_scratch_persistence_is_configurable(self):
        assert "gitlab_backup_persistence_enabled:" in self.tasks_raw
        assert "gitlab.backup_persistence_enabled | default(true)" in self.tasks_raw
        assert "enabled: '{{ gitlab_backup_persistence_enabled | bool }}'" in self.tasks_raw
        assert "name: gitlab-toolbox-backup-tmp" in self.tasks_raw
        assert "platform.n0xeid.xyz/backup-scratch: 'true'" in self.tasks_raw
        assert "state: patched" in self.tasks_raw

    @pytest.mark.component
    def test_every_toolbox_backup_bucket_is_bootstrapped(self):
        buckets = read(os.path.join(REPO_ROOT, "roles", "object-storage", "defaults", "main.yml"))
        for bucket in (
            "gitlab-artifacts",
            "gitlab-registry",
            "gitlab-lfs",
            "gitlab-uploads",
            "gitlab-packages",
            "gitlab-mr-diffs",
            "gitlab-terraform-state",
            "gitlab-pages",
            "gitlab-ci-secure-files",
            "gitlab-agent-plan-content",
            "gitlab-backups",
            "gitlab-tmp",
        ):
            assert f"- {bucket}" in buckets

    @pytest.mark.component
    def test_kas_gateway_ingress_is_explicitly_allowed(self):
        assert "Allow GitLab KAS ingress from gateway" in self.tasks_raw
        assert "name: allow-kas-ingress" in self.tasks_raw
        assert "app: kas" in self.tasks_raw
        assert "port: '8150'" in self.tasks_raw

class TestBackupCompatibility:
    @pytest.fixture(autouse=True)
    def _content(self):
        path = os.path.join(REPO_ROOT, "roles", "backup-restore", "tasks", "gitlab.yml")
        self.content = read(path) if os.path.isfile(path) else ""

    @pytest.mark.component
    def test_backup_task_exists(self):
        assert os.path.isfile(os.path.join(REPO_ROOT, "roles", "backup-restore", "tasks", "gitlab.yml"))

    @pytest.mark.component
    def test_backup_cronjob_present(self):
        if self.content:
            assert "CronJob" in self.content

    @pytest.mark.component
    def test_backup_credentials_secret(self):
        if self.content:
            assert "gitlab-rails-backup-credentials" in self.content

    @pytest.mark.component
    def test_official_toolbox_backup_is_required(self):
        if self.content:
            assert "gitlab-toolbox-backup" in self.content

    @pytest.mark.component
    def test_external_database_backup_contract_is_documented(self):
        if self.content:
            assert "external Percona" in self.content
            assert "version-matched backup" in self.content

class TestNoDeprecatedKeys:
    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(GITLAB_TASKS_PATH)

    @pytest.mark.component
    def test_external_postgresql_uses_global_psql(self):
        assert re.search(r'^\s+psql:\s*$', self.content, re.MULTILINE)
        assert "-pg-pgbouncer.databases.svc.cluster.local" in self.content

    @pytest.mark.component
    def test_external_postgresql_verifies_the_internal_tls_identity(self):
        assert "databases.postgresql.service_alias" in self.content
        assert "PGSSLMODE: verify-full" in self.content
        assert "PGSSLROOTCERT: /etc/gitlab/postgresql/ca.crt" in self.content
        assert self.content.count("secretName: gitlab-postgresql-password") >= 5
        assert self.content.count("mountPath: /etc/gitlab/postgresql") >= 5

    @pytest.mark.component
    def test_no_obsolete_database_external_key(self):
        assert not re.search(r'^\s+database:\s*\n\s+external:', self.content, re.MULTILINE)

    @pytest.mark.component
    def test_no_postgresql_install(self):
        assert not re.search(r'\bpostgresql:\s*\n\s+install:', self.content)

    @pytest.mark.component
    def test_no_redis_install_key(self):
        in_redis = False
        indent_level = None
        for line in self.content.splitlines():
            if re.match(r'^\s+redis:\s*$', line):
                in_redis = True
                indent_level = len(line) - len(line.lstrip())
                continue
            if in_redis:
                ci = len(line) - len(line.lstrip()) if line.strip() else indent_level + 1
                if line.strip() and ci <= indent_level and line.strip() != "redis:":
                    break
                s = line.strip()
                if not s.startswith("#") and re.match(r'install:', s):
                    pytest.fail(f"Deprecated redis.install: {s}")
