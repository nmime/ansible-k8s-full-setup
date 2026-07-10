#!/usr/bin/env python3
"""verify-version-matrix.py - Validates version consistency across the project."""
import sys, re, yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = REPO_ROOT / "defaults" / "main.yml"

EXPECTED = {
    "k8s_version": "v1.35.6", "cilium_version": "v1.19.5",
    "gateway_api_version": "v1.6.0", "cert_manager_version": "v1.21.0",
    "metallb_version": "v0.16.1", "hetzner_ccm_version": "v1.33.0",
    "hetzner_csi_version": "v2.22.0", "kubespray_version": "v2.31.0",
    "keda_chart_version": "2.20.1", "es_version": "9.4.3",
    "kibana_version": "9.4.3", "apm_server_version": "9.4.3",
    "postal_version": "3.3.7", "blackbox_chart_version": "11.15.1",
    "dragonfly_operator_version": "v1.6.1", "dragonfly_image_version": "v1.39.0",
    "vm_operator_version": "0.66.2", "eso_chart_version": "2.7.0",
    "hcloud_cli_version": "v1.42.0", "yq_version": "v4.44.6",
}

STALE = {
    "v1.35.4": "k8s", "v1.19.4": "cilium", "v1.5.1": "gateway-api",
    "v1.20.2": "cert-manager", "v0.15.3": "metallb", "v1.31.0": "ccm",
    "v2.21.0": "csi", "2.19.0": "keda", "9.4.1": "es/kibana/apm",
    "3.3.6": "postal", "11.10.0": "blackbox", "v1.5.0": "dragonfly-op",
    "v1.38.1": "dragonfly-img", "0.59.3": "vm-op", "2.5.0": "eso",
}

def check_central():
    errs = []
    data = yaml.safe_load(DEFAULTS_FILE.read_text())
    for var, exp in EXPECTED.items():
        actual = data.get(var)
        if actual is None:
            errs.append(f"missing '{var}'")
        elif str(actual) != exp:
            errs.append(f"'{var}' = '{actual}', expected '{exp}'")
    return errs

def check_latest_tags():
    errs = []
    for yml in (REPO_ROOT / "roles").rglob("*.yml"):
        content = yml.read_text()
        for i, line in enumerate(content.splitlines(), 1):
            s = line.strip()
            if "pod-security.kubernetes.io" in s:
                continue
            if re.search(r'["\s:]latest\s*$|["\s:]latest["\s}]', s):
                errs.append(f"{yml.relative_to(REPO_ROOT)}:{i}: `:latest` tag")
                break
        clean = re.sub(r'pod-security\.kubernetes\.io[^\n]*', '', content)
        if "releases/latest/" in clean:
            errs.append(f"{yml.relative_to(REPO_ROOT)}: `/latest/` URL")
    return errs

def check_stale():
    errs = []
    for yml in (REPO_ROOT / "roles").rglob("*.yml"):
        content = yml.read_text()
        for old, comp in STALE.items():
            if old in content:
                errs.append(f"{yml.relative_to(REPO_ROOT)}: stale '{old}' ({comp})")
    return errs

def check_profiles():
    errs = []
    for yml in (REPO_ROOT / "platform-orchestrator" / "profiles").glob("*.yaml"):
        if "version: v1.35.4" in yml.read_text():
            errs.append(f"{yml.relative_to(REPO_ROOT)}: old k8s version")
    return errs

def check_inventory():
    inv = REPO_ROOT / "inventory.example"
    if inv.exists() and "k8s_version: v1.35.4" in inv.read_text():
        return ["inventory.example: old k8s version"]
    return []

def check_kubectl_var():
    ns = REPO_ROOT / "roles" / "network-security" / "tasks" / "main.yml"
    if not ns.exists():
        return []
    c = ns.read_text()
    errs = []
    if "dl.k8s.io/release/v1.35" in c:
        errs.append("kubectl uses hardcoded K8s version")
    if "${K8S_VER}" not in c:
        errs.append("kubectl should use ${K8S_VER}")
    return errs

def check_cli_pinned():
    ns = REPO_ROOT / "roles" / "network-security" / "tasks" / "main.yml"
    if not ns.exists():
        return []
    c = ns.read_text()
    errs = []
    clean = re.sub(r'pod-security\.kubernetes\.io[^\n]*', '', c)
    if "releases/latest/" in clean:
        errs.append("unpinned /latest/ URL")
    if "${HCLOUD_VER}" not in c:
        errs.append("hcloud should use ${HCLOUD_VER}")
    if "${YQ_VER}" not in c:
        errs.append("yq should use ${YQ_VER}")
    return errs

def main():
    checks = [
        ("defaults/main.yml", check_central),
        (":latest tags", check_latest_tags),
        ("stale versions in roles", check_stale),
        ("profiles consistency", check_profiles),
        ("inventory.example", check_inventory),
        ("kubectl uses k8s_version", check_kubectl_var),
        ("CLI tools pinned", check_cli_pinned),
    ]
    all_errs = []
    for name, fn in checks:
        errs = fn()
        ok = " OK" if not errs else "FAIL"
        status = "OK" if not errs else f"FAIL ({len(errs)})"
        print(f"[{ok:>5}] {name}: {status}")
        all_errs.extend(errs)
    print()
    if all_errs:
        for e in all_errs:
            print(f"  -> {e}")
        print(f"RESULT: FAIL - {len(all_errs)} error(s)")
        sys.exit(1)
    print("RESULT: PASS - all version checks passed")
    sys.exit(0)

if __name__ == "__main__":
    main()
