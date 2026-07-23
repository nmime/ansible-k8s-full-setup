#!/usr/bin/env python3
"""Report the live Hetzner server catalog and five-profile tariff totals."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
TARIFF_CATALOG_PATH = ROOT / "platform-orchestrator" / "capacity-tariffs.yaml"
with TARIFF_CATALOG_PATH.open(encoding="utf-8") as tariff_stream:
    TARIFF_CATALOG = yaml.safe_load(tariff_stream)
PROFILE_ORDER = tuple(TARIFF_CATALOG["profiles"])
FAMILY_ORDER = tuple(TARIFF_CATALOG["families"])
FAMILY_LABELS = {
    name: values["label"] for name, values in TARIFF_CATALOG["families"].items()
}
FAMILY_ARCHITECTURE = {
    name: values["architecture"] for name, values in TARIFF_CATALOG["families"].items()
}
TARIFF_TYPES: dict[str, dict[str, dict[str, str]]] = TARIFF_CATALOG["profiles"]


def api_get(path: str, token: str, endpoint: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/{path.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "ansible-k8s-full-setup/capacity-report",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Hetzner API {path} failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Hetzner API {path} failed: {exc.reason}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Hetzner API {path} returned an invalid document")
    return result


def family_for(name: str) -> str:
    match = re.match(r"^([a-z]+)", name)
    return match.group(1) if match else "other"


def type_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    family = family_for(str(item["name"]))
    suffix = re.search(r"(\d+)$", str(item["name"]))
    return (
        FAMILY_ORDER.index(family) if family in FAMILY_ORDER else len(FAMILY_ORDER),
        int(suffix.group(1)) if suffix else 0,
        str(item["name"]),
    )


def location_price(prices: list[dict[str, Any]], location: str) -> dict[str, Any]:
    return next((price for price in prices if price.get("location") == location), {})


def location_available(server_type: dict[str, Any], location: str) -> bool:
    return any(
        entry.get("name") == location and entry.get("available") is True
        for entry in server_type.get("locations", [])
    )


def normalize_catalog(server_types: list[dict[str, Any]], location: str) -> list[dict[str, Any]]:
    catalog = []
    for server_type in sorted(server_types, key=type_sort_key):
        price = location_price(server_type.get("prices", []), location)
        catalog.append(
            {
                "name": server_type["name"],
                "family": family_for(str(server_type["name"])),
                "architecture": server_type.get("architecture"),
                "cpu_type": server_type.get("cpu_type"),
                "vcpus": int(server_type.get("cores", 0)),
                "memory_gib": str(Decimal(str(server_type.get("memory", 0)))),
                "local_disk_gib": int(server_type.get("disk", 0)),
                "available": location_available(server_type, location),
                "hourly_net": str(price.get("price_hourly", {}).get("net", "")),
                "monthly_net": str(price.get("price_monthly", {}).get("net", "")),
                "included_traffic_tib": str(
                    Decimal(str(price.get("included_traffic", 0))) / Decimal(2**40)
                ),
                "excess_traffic_per_tb_net": str(
                    price.get("price_per_tb_traffic", {}).get("net", "")
                ),
            }
        )
    return catalog


def find_location_amount(
    entries: list[dict[str, Any]], name: str, location: str, field: str
) -> Decimal:
    entry = next(item for item in entries if item.get("name") == name or item.get("type") == name)
    price = location_price(entry.get("prices", []), location)
    return Decimal(str(price[field]["net"]))


def storage_estimate(profile: dict[str, Any]) -> int:
    # Keep one source of truth by importing the migration estimator.
    import importlib.util

    estimator_path = ROOT / "scripts" / "profile-storage-capacity.py"
    spec = importlib.util.spec_from_file_location("profile_storage_capacity", estimator_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load profile storage estimator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.estimate(profile)
    return int(result["persistent_total_gib"]) + int(result["backup_scratch_gib"])


def build_plans(
    catalog: list[dict[str, Any]],
    pricing: dict[str, Any],
    profiles_dir: Path,
    location: str,
) -> list[dict[str, Any]]:
    by_name = {item["name"]: item for item in catalog}
    volume_price = Decimal(str(pricing["volume"]["price_per_gb_month"]["net"]))
    ipv4_price = find_location_amount(pricing["primary_ips"], "ipv4", location, "price_monthly")
    plans = []
    for profile_name in PROFILE_ORDER:
        with (profiles_dir / f"{profile_name}.yaml").open(encoding="utf-8") as stream:
            profile = yaml.safe_load(stream)
        cp_count = int(profile["infrastructure"]["control_plane"]["count"])
        worker_count = int(profile["infrastructure"]["workers"]["count"])
        bastion_enabled = bool(profile["network"]["bastion"]["enabled"])
        lb_enabled = bool(profile["network"]["load_balancer"]["enabled"])
        lb_type = str(profile["network"]["load_balancer"].get("type", "lb11"))
        lb_price = (
            find_location_amount(
                pricing["load_balancer_types"], lb_type, location, "price_monthly"
            )
            if lb_enabled
            else Decimal("0")
        )
        volume_gib = storage_estimate(profile)
        volume_total = volume_price * volume_gib
        for family in FAMILY_ORDER:
            selected = TARIFF_TYPES[profile_name][family]
            missing = [name for name in selected.values() if name not in by_name]
            if missing:
                raise RuntimeError(f"catalog is missing tariff types: {', '.join(missing)}")
            compute = (
                Decimal(by_name[selected["control_plane"]]["monthly_net"]) * cp_count
                + Decimal(by_name[selected["worker"]]["monthly_net"]) * worker_count
            )
            if bastion_enabled:
                compute += Decimal(by_name[selected["bastion"]]["monthly_net"]) + ipv4_price
            infrastructure = compute + lb_price
            selected_availability = {
                role: bool(by_name[name]["available"]) for role, name in selected.items()
            }
            plans.append(
                {
                    "profile": profile_name,
                    "family": family,
                    "family_label": FAMILY_LABELS[family],
                    "architecture": FAMILY_ARCHITECTURE[family],
                    "types": selected,
                    "control_plane_count": cp_count,
                    "worker_count": worker_count,
                    "availability": selected_availability,
                    "all_types_available": all(selected_availability.values()),
                    "deployment_status": (
                        "planning-only-arm64-unattested"
                        if family == "cax"
                        else "deployable"
                        if all(selected_availability.values())
                        else "temporarily-unavailable"
                    ),
                    "infrastructure_monthly_net": f"{infrastructure:.2f}",
                    "volume_gib": volume_gib,
                    "volume_monthly_net": f"{volume_total:.2f}",
                    "total_monthly_net": f"{infrastructure + volume_total:.2f}",
                }
            )
    return plans


def money(value: str, places: int = 2) -> str:
    return f"€{Decimal(value):.{places}f}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Live Hetzner capacity and tariff report",
        "",
        f"Captured: `{report['captured_at']}`; location: `{report['location']}`; "
        f"currency: `{report['currency']}`; account VAT: `{report['vat_rate']}%`.",
        "",
        "## Server catalog",
        "",
        "| Type | Family | Arch | CPU | vCPU | RAM | Local SSD | Traffic | Available | Hourly net | Monthly net |",
        "|---|---|---|---|---:|---:|---:|---:|:---:|---:|---:|",
    ]
    for item in report["server_types"]:
        lines.append(
            f"| `{item['name']}` | {item['family'].upper()} | {item['architecture']} | "
            f"{item['cpu_type']} | {item['vcpus']} | {item['memory_gib']} GiB | "
            f"{item['local_disk_gib']} GiB | {item['included_traffic_tib']} TiB | "
            f"{'yes' if item['available'] else 'no'} | "
            f"{money(item['hourly_net'], 4)} | {money(item['monthly_net'])} |"
        )
    lines.extend(
        [
            "",
            "## Five-profile tariff totals",
            "",
            "Totals include the selected servers, bastion IPv4, configured load balancer, "
            "and profile PVC capacity. Local server SSD is not deducted from CSI volumes.",
            "",
            "| Profile | Family | Types (bastion / control plane / worker) | Status | Infrastructure | Volumes | Total net/month |",
            "|---|---|---|---|---:|---:|---:|",
        ]
    )
    for plan in report["plans"]:
        selected = plan["types"]
        lines.append(
            f"| `{plan['profile']}` | {plan['family'].upper()} | "
            f"`{selected['bastion']}` / `{selected['control_plane']}` / `{selected['worker']}` | "
            f"{plan['deployment_status']} | {money(plan['infrastructure_monthly_net'])} | "
            f"{money(plan['volume_monthly_net'])} ({plan['volume_gib']} GiB) | "
            f"**{money(plan['total_monthly_net'])}** |"
        )
    lines.extend(
        [
            "",
            "CAX remains planning-only until every selected container image and operational "
            "path has an ARM64 production attestation. CX and CAX availability is capacity-"
            "dependent. Re-run this report immediately before provisioning.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--location", default="hel1")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--profiles-dir", type=Path, default=ROOT / "platform-orchestrator" / "profiles"
    )
    parser.add_argument(
        "--endpoint", default=os.environ.get("HCLOUD_ENDPOINT", "https://api.hetzner.cloud/v1")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("HCLOUD_TOKEN", "")
    if not token:
        print("ERROR: HCLOUD_TOKEN is required; load the gitignored project .env", file=sys.stderr)
        return 2
    server_document = api_get("server_types?per_page=50", token, args.endpoint)
    pricing_document = api_get("pricing", token, args.endpoint)
    catalog = normalize_catalog(server_document.get("server_types", []), args.location)
    pricing = pricing_document.get("pricing", {})
    report = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "location": args.location,
        "currency": pricing.get("currency"),
        "vat_rate": str(Decimal(str(pricing.get("vat_rate", "0")))),
        "server_types": catalog,
        "plans": build_plans(catalog, pricing, args.profiles_dir, args.location),
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
