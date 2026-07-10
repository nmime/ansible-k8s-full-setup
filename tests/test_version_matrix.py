#!/usr/bin/env python3
"""
Version Matrix Validation Script
Standalone validator for defaults/main.yml version compatibility.
Run: python3 tests/test_version_matrix.py
"""

import os
import re
import json
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS_PATH = os.path.join(REPO_ROOT, "defaults", "main.yml")
REQUIREMENTS_TXT = os.path.join(REPO_ROOT, "requirements.txt")


def read(path):
    with open(path) as f:
        return f.read()


def parse_version_vars(content):
    versions = {}
    for line in content.splitlines():
        m = re.match(r"^\s*(\w+version\w*)\s*:\s*['\"]?([^'\"#\n{]+?)['\"]?\s*$", line)
        if m:
            versions[m.group(1)] = m.group(2).strip()
    return versions


def parse_requirements(content):
    packages = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if "==" in line:
            name, ver = line.split("==", 1)
            packages[name.strip().lower()] = ver.strip()
    return packages


def main():
    errors = []
    defaults = read(DEFAULTS_PATH)
    versions = parse_version_vars(defaults)
    reqs = parse_requirements(read(REQUIREMENTS_TXT))

    # 1. Check K8s version format
    k8s = versions.get("k8s_version", "")
    if not re.match(r"^v\d+\.\d+\.\d+$", k8s):
        errors.append(f"k8s_version '{k8s}' not valid semver")

    # 2. Check ES/Kibana version match
    es = versions.get("es_version", "")
    kib = versions.get("kibana_version", "")
    if es and kib and es != kib:
        errors.append(f"ES ({es}) and Kibana ({kib}) versions mismatch")

    # 3. Check all chart versions are semver
    for key, val in versions.items():
        if "chart" in key.lower():
            if not re.match(r"^\d+\.\d+\.\d+$", val):
                errors.append(f"{key} '{val}' is not semver")

    # 4. Check ansible-core is in requirements
    if "ansible-core" not in reqs:
        errors.append("ansible-core missing from requirements.txt")

    # 5. Check required packages
    for pkg in ["ansible-lint", "yamllint", "pytest"]:
        if pkg not in reqs:
            errors.append(f"{pkg} missing from requirements.txt")

    # 6. Validate tier consistency
    tiers = re.findall(r"tier\s+in\s+\[([^\]]+)\]", defaults)
    for ref in tiers:
        tier_names = re.findall(r"'(\w+)'", ref)
        for t in tier_names:
            if t not in ("minimal", "small", "medium", "production"):
                errors.append(f"Unknown tier '{t}'")

    # Report
    print(f"Version matrix validation: {len(versions)} version vars, {len(reqs)} pinned packages")
    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("PASS: All version checks passed")
        sys.exit(0)


if __name__ == "__main__":
    main()
