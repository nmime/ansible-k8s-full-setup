#!/usr/bin/env python3
"""Preflight compatibility checker for platform upgrades.

Usage:
    python3 preflight_check.py [--dry-run true|false]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    level: str = "info"


@dataclass
class PreflightReport:
    checks: List[CheckResult] = field(default_factory=list)
    summary: str = ""

    @property
    def failures(self) -> int:
        return sum(1 for c in self.checks if c.level == "error" and not c.passed)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.level == "warn")

    def passed(self) -> bool:
        return self.failures == 0


def run(cmd, check=False):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=check)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "command not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "timeout")


def check_tools(report, dry_run):
    for tool in ["kubectl", "helm", "yq", "ansible-playbook"]:
        if dry_run:
            report.checks.append(CheckResult(f"tool:{tool}", True, f"[DRY] would check {tool}"))
            continue
        r = run(["which", tool])
        if r.returncode == 0:
            report.checks.append(CheckResult(f"tool:{tool}", True, f"OK {tool} at {r.stdout.strip()}"))
        else:
            report.checks.append(CheckResult(f"tool:{tool}", False, f"missing: {tool}", "error"))


def check_cluster_connectivity(report, dry_run):
    if dry_run:
        report.checks.append(CheckResult("cluster:connectivity", True, "[DRY] would check kubectl cluster-info"))
        return
    r = run(["kubectl", "cluster-info"])
    if r.returncode == 0:
        report.checks.append(CheckResult("cluster:connectivity", True, "OK Cluster reachable"))
    else:
        report.checks.append(CheckResult("cluster:connectivity", False, "Cannot connect to cluster", "error"))


def check_k8s_version(report, dry_run):
    if dry_run:
        report.checks.append(CheckResult("cluster:version", True, "[DRY] would check kubectl version"))
        return
    r = run(["kubectl", "version", "--short"])
    if r.returncode != 0:
        r = run(["kubectl", "version"])
    if r.returncode != 0:
        report.checks.append(CheckResult("cluster:version", False, "Cannot read Kubernetes server version", "error"))
        return
    ver = "unknown"
    for line in r.stdout.splitlines():
        if "Server Version" in line:
            ver = line.split(":")[-1].strip().lstrip("v")
            break
    if ver == "unknown":
        report.checks.append(CheckResult("cluster:version", False, "Server version missing from kubectl output", "error"))
    else:
        report.checks.append(CheckResult("cluster:version", True, f"OK Server version: {ver}"))


def check_helm_releases(report, dry_run):
    if dry_run:
        report.checks.append(CheckResult("helm:health", True, "[DRY] would check helm list --failed"))
        return
    r = run(["helm", "list", "--all-namespaces", "--failed"])
    lines = [l for l in r.stdout.strip().splitlines() if l and not l.startswith("NAME")]
    if not lines:
        report.checks.append(CheckResult("helm:health", True, "OK No failing Helm releases"))
    else:
        report.checks.append(CheckResult("helm:health", False, f"{len(lines)} failing release(s)", "error"))


def check_nodes(report, dry_run):
    if dry_run:
        report.checks.append(CheckResult("nodes:ready", True, "[DRY] would check kubectl get nodes"))
        return
    r = run(["kubectl", "get", "nodes", "--no-headers"])
    if r.returncode != 0:
        report.checks.append(CheckResult("nodes:ready", False, "Cannot list nodes", "error"))
        return
    total = len([l for l in r.stdout.strip().splitlines() if l])
    ready = sum(1 for l in r.stdout.strip().splitlines() if l and "Ready" in l)
    if total > 0 and ready == total:
        report.checks.append(CheckResult("nodes:ready", True, f"OK All {total} nodes Ready"))
    elif total > 0 and ready == 0:
        report.checks.append(CheckResult("nodes:ready", False, "No nodes Ready", "error"))
    else:
        report.checks.append(CheckResult("nodes:ready", False, f"Only {ready}/{total} nodes Ready", "error"))


def check_disk_space(report, dry_run, project_root):
    if dry_run:
        report.checks.append(CheckResult("disk:space", True, "[DRY] would check df"))
        return
    r = run(["df", "-h", project_root])
    pct = 0
    if r.returncode == 0:
        parts = r.stdout.strip().splitlines()
        if len(parts) >= 2:
            pct = int(parts[-1].split()[4].rstrip("%"))
    if pct < 80:
        report.checks.append(CheckResult("disk:space", True, f"OK Disk at {pct}%"))
    else:
        report.checks.append(CheckResult("disk:space", True, f"Disk at {pct}%", "warn"))


def check_snapshot(report, dry_run, snapshot_dir):
    if dry_run:
        report.checks.append(CheckResult("snapshot:exists", True, "[DRY] would check snapshots"))
        return
    sd = Path(snapshot_dir)
    if sd.exists() and any(sd.iterdir()):
        report.checks.append(CheckResult("snapshot:exists", True, f"OK Snapshots in {snapshot_dir}"))
    else:
        report.checks.append(CheckResult("snapshot:exists", True, "No snapshots found", "warn"))


def check_git_clean(report, dry_run, project_root):
    if dry_run:
        report.checks.append(CheckResult("git:clean", True, "[DRY] would check git status"))
        return
    r = run(["git", "-C", project_root, "status", "--porcelain"])
    changes = len([l for l in r.stdout.strip().splitlines() if l])
    if changes == 0:
        report.checks.append(CheckResult("git:clean", True, "OK Working tree clean"))
    else:
        report.checks.append(CheckResult("git:clean", True, f"{changes} uncommitted change(s)", "warn"))


def check_config(report, dry_run, config_file):
    if dry_run:
        report.checks.append(CheckResult("config:valid", True, "[DRY] would validate platform.yaml"))
        return
    cf = Path(config_file)
    if not cf.exists():
        report.checks.append(CheckResult("config:valid", False, f"{config_file} not found", "error"))
        return
    r = run(["yq", "-r", ".global.domain", str(cf)])
    domain = r.stdout.strip().strip('"')
    if not domain or domain.lower() == "null":
        report.checks.append(CheckResult("config:domain", False, "global.domain not set", "error"))
    else:
        report.checks.append(CheckResult("config:domain", True, f"OK Domain: {domain}"))
    r = run(["yq", "-r", ".global.email", str(cf)])
    email = r.stdout.strip().strip('"')
    if not email or email.lower() == "null":
        report.checks.append(CheckResult("config:email", False, "global.email not set", "error"))
    else:
        report.checks.append(CheckResult("config:email", True, f"OK Email: {email}"))


def run_all(project_root, dry_run):
    report = PreflightReport()
    config_file = os.path.join(project_root, "platform-orchestrator", "platform.yaml")
    snapshot_dir = os.path.join(project_root, "snapshot")
    check_tools(report, dry_run)
    check_cluster_connectivity(report, dry_run)
    check_k8s_version(report, dry_run)
    check_helm_releases(report, dry_run)
    check_nodes(report, dry_run)
    check_disk_space(report, dry_run, project_root)
    check_snapshot(report, dry_run, snapshot_dir)
    check_git_clean(report, dry_run, project_root)
    check_config(report, dry_run, config_file)
    return report


def main():
    parser = argparse.ArgumentParser(description="Preflight compatibility checks")
    parser.add_argument("--dry-run", default="false", help="Simulate checks")
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Repository root (defaults to the parent of scripts/)",
    )
    args = parser.parse_args()
    dry = args.dry_run.lower() in ("true", "1", "yes")
    report = run_all(args.project_root, dry)
    print(f"{'='*50}")
    print("  PREFLIGHT REPORT")
    print(f"{'='*50}")
    for c in report.checks:
        sym = "OK" if c.passed else ("WARN" if c.level == "warn" else "FAIL")
        print(f"  [{sym}] {c.name}: {c.message}")
    print(f"{'='*50}")
    print(f"  {len(report.checks)} checks | {report.failures} failures | {report.warnings} warnings")
    print(f"{'='*50}")
    if not report.passed():
        print("\nPreflight FAILED - do not proceed with upgrade.")
        sys.exit(1)
    print("\nPreflight PASSED - safe to proceed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
