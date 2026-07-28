#!/usr/bin/env python3
"""Reconcile CoreDNS and NodeLocal DNS without bypassing private service routes."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile


BEGIN = "# BEGIN ANSIBLE MANAGED INTERNAL DNS"
END = "# END ANSIBLE MANAGED INTERNAL DNS"


def run(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        check=True,
        capture_output=True,
    )
    return result.stdout


def validate_zones(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise ValueError("network.internal_dns.zones must be a list")
    zones: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each internal DNS zone must be a mapping")
        zone = str(item.get("zone", "")).strip().rstrip(".").lower()
        if not zone or not re.fullmatch(r"[a-z0-9.-]+", zone):
            raise ValueError(f"invalid internal DNS zone: {zone!r}")
        records = item.get("records", [])
        if not isinstance(records, list) or not records:
            raise ValueError(f"internal DNS zone {zone!r} requires records")
        normalized: list[dict[str, object]] = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f"invalid record in internal DNS zone {zone!r}")
            address = str(ipaddress.ip_address(str(record.get("address", ""))))
            names = record.get("names", [])
            if not isinstance(names, list) or not names:
                raise ValueError(f"record {address!r} requires names")
            normalized_names: list[str] = []
            for name_value in names:
                name = str(name_value).strip().rstrip(".").lower()
                if (
                    not name.endswith(f".{zone}")
                    or not re.fullmatch(r"[a-z0-9.-]+", name)
                ):
                    raise ValueError(
                        f"record {name!r} is not a valid child of zone {zone!r}"
                    )
                normalized_names.append(name)
            normalized.append({"address": address, "names": normalized_names})
        zones.append({"zone": zone, "records": normalized})
    return zones


def strip_managed_blocks(corefile: str) -> str:
    pattern = rf"\n?{re.escape(BEGIN)}.*?{re.escape(END)}[^\n]*\n?"
    return re.sub(pattern, "\n", corefile, flags=re.DOTALL).strip() + "\n"


def coredns_block(zone: dict[str, object]) -> str:
    lines = [
        f"{BEGIN} {zone['zone']}",
        f"{zone['zone']}:53 {{",
        "    errors",
        "    hosts {",
    ]
    for record in zone["records"]:  # type: ignore[index]
        lines.append(f"        {record['address']} {' '.join(record['names'])}")
    lines.extend(
        [
            "        fallthrough",
            "    }",
            "    forward . 1.1.1.1 8.8.8.8 {",
            "        prefer_udp",
            "        max_concurrent 1000",
            "    }",
            "    cache 30",
            "    reload",
            "}",
            f"{END} {zone['zone']}",
        ]
    )
    return "\n".join(lines)


def nodelocal_block(zone: dict[str, object], cluster_dns: str, bind: str) -> str:
    return "\n".join(
        [
            f"{BEGIN} {zone['zone']}",
            f"{zone['zone']}:53 {{",
            "    errors",
            "    cache 30",
            "    reload",
            f"    bind {bind}",
            f"    forward . {cluster_dns} {{",
            "        force_tcp",
            "    }",
            "}",
            f"{END} {zone['zone']}",
        ]
    )


def apply_configmap(name: str, corefile: str) -> bool:
    raw = run("kubectl", "-n", "kube-system", "get", "configmap", name, "-o", "json")
    document = json.loads(raw)
    current = document["data"]["Corefile"]
    if current == corefile:
        return False
    document["data"]["Corefile"] = corefile
    document["metadata"].pop("managedFields", None)
    document["metadata"].pop("resourceVersion", None)
    document["metadata"].pop("uid", None)
    document["metadata"].pop("creationTimestamp", None)
    document["metadata"].pop("generation", None)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as stream:
        json.dump(document, stream)
        stream.flush()
        run("kubectl", "apply", "-f", stream.name)
    return True


def main() -> int:
    zones = validate_zones(json.loads(os.environ.get("PLATFORM_INTERNAL_DNS_ZONES", "[]")))
    coredns_document = json.loads(
        run("kubectl", "-n", "kube-system", "get", "configmap", "coredns", "-o", "json")
    )
    nodelocal_document = json.loads(
        run(
            "kubectl",
            "-n",
            "kube-system",
            "get",
            "configmap",
            "nodelocaldns",
            "-o",
            "json",
        )
    )
    coredns = strip_managed_blocks(
        coredns_document["data"]["Corefile"]
        .replace("127.0.0.53", "1.1.1.1 8.8.8.8")
        .replace("/etc/resolv.conf", "1.1.1.1 8.8.8.8")
    )
    nodelocal = strip_managed_blocks(
        nodelocal_document["data"]["Corefile"].replace(
            "/etc/resolv.conf", "1.1.1.1 8.8.8.8"
        )
    )
    cluster_dns_match = re.search(
        r"cluster\.local:53\s*\{.*?forward\s+\.\s+([0-9.]+)",
        nodelocal,
        re.DOTALL,
    )
    bind_match = re.search(r"cluster\.local:53\s*\{.*?bind\s+([0-9.]+)", nodelocal, re.DOTALL)
    if not cluster_dns_match or not bind_match:
        raise RuntimeError("cannot discover NodeLocal DNS upstream or bind address")
    if zones:
        coredns = (
            "\n\n".join([coredns.rstrip(), *(coredns_block(zone) for zone in zones)])
            + "\n"
        )
        nodelocal = (
            "\n\n".join(
                [
                    nodelocal.rstrip(),
                    *(
                        nodelocal_block(
                            zone,
                            cluster_dns_match.group(1),
                            bind_match.group(1),
                        )
                        for zone in zones
                    ),
                ]
            )
            + "\n"
        )
    coredns_changed = apply_configmap("coredns", coredns)
    nodelocal_changed = apply_configmap("nodelocaldns", nodelocal)
    if coredns_changed:
        run("kubectl", "-n", "kube-system", "rollout", "restart", "deployment", "coredns")
    if nodelocal_changed:
        run(
            "kubectl",
            "-n",
            "kube-system",
            "delete",
            "pods",
            "-l",
            "k8s-app=node-local-dns",
            "--wait=false",
        )
    print(f"changed={'true' if coredns_changed or nodelocal_changed else 'false'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"cluster DNS reconciliation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
