"""Contracts for live Hetzner catalog and tariff planning."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "scripts" / "hetzner-capacity-report.py"
PROFILES = ROOT / "platform-orchestrator" / "profiles"

spec = importlib.util.spec_from_file_location("hetzner_capacity_report", REPORT_PATH)
assert spec and spec.loader
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


MONTHLY = {
    "cx23": "5.49",
    "cx33": "8.49",
    "cx43": "15.99",
    "cax11": "5.99",
    "cax21": "10.49",
    "cax31": "20.99",
    "cpx22": "19.49",
    "cpx32": "35.49",
    "cpx42": "69.49",
    "ccx13": "42.99",
    "ccx23": "85.99",
}


def synthetic_catalog() -> list[dict]:
    names = sorted(
        {
            server_type
            for profile in report.TARIFF_TYPES.values()
            for family in profile.values()
            for server_type in family.values()
        }
    )
    return [
        {
            "name": name,
            "family": report.family_for(name),
            "architecture": report.FAMILY_ARCHITECTURE[report.family_for(name)],
            "cpu_type": "dedicated" if name.startswith("ccx") else "shared",
            "vcpus": 1,
            "memory_gib": "1",
            "local_disk_gib": 1,
            "available": name.startswith(("cpx", "ccx")),
            "hourly_net": "0.01",
            "monthly_net": MONTHLY[name],
        }
        for name in names
    ]


def synthetic_pricing() -> dict:
    return {
        "volume": {"price_per_gb_month": {"net": "0.0572"}},
        "primary_ips": [
            {
                "type": "ipv4",
                "prices": [
                    {"location": "hel1", "price_monthly": {"net": "0.50"}}
                ],
            }
        ],
        "load_balancer_types": [
            {
                "name": "lb11",
                "prices": [
                    {"location": "hel1", "price_monthly": {"net": "7.49"}}
                ],
            }
        ],
    }


def test_every_profile_has_all_four_capacity_tariffs():
    assert tuple(report.TARIFF_TYPES) == report.PROFILE_ORDER
    for profile in report.PROFILE_ORDER:
        assert tuple(report.TARIFF_TYPES[profile]) == report.FAMILY_ORDER
        for family, types in report.TARIFF_TYPES[profile].items():
            assert set(types) == {"bastion", "control_plane", "worker"}
            assert all(name.startswith(family) for name in types.values())


def test_current_medium_optimized_totals_and_safety_statuses():
    plans = report.build_plans(
        synthetic_catalog(), synthetic_pricing(), PROFILES, "hel1"
    )
    selected = {
        plan["family"]: plan
        for plan in plans
        if plan["profile"] == "medium-optimized"
    }

    assert selected["cx"]["types"] == {
        "bastion": "cx23",
        "control_plane": "cx33",
        "worker": "cx43",
    }
    assert selected["cx"]["infrastructure_monthly_net"] == "102.91"
    assert selected["cx"]["volume_gib"] == 260
    assert selected["cx"]["local_reserved_gib"] == 410
    assert selected["cx"]["total_claim_capacity_gib"] == 670
    assert selected["cx"]["total_monthly_net"] == "117.78"
    assert selected["cx"]["deployment_status"] == "temporarily-unavailable"
    assert selected["cax"]["total_monthly_net"] == "102.28"
    assert selected["cax"]["deployment_status"] == "planning-only-arm64-unattested"
    assert selected["cpx"]["infrastructure_monthly_net"] == "275.91"
    assert selected["cpx"]["volume_gib"] == 260
    assert selected["cpx"]["total_monthly_net"] == "290.78"
    assert selected["cpx"]["deployment_status"] == "deployable"
    assert selected["ccx"]["total_monthly_net"] == "667.78"


def test_named_profile_defaults_are_the_balanced_cpx_mapping():
    import yaml

    for profile_name in report.PROFILE_ORDER:
        profile = yaml.safe_load((PROFILES / f"{profile_name}.yaml").read_text())
        selected = report.TARIFF_TYPES[profile_name]["cpx"]
        assert profile["network"]["bastion"]["server_type"] == selected["bastion"]
        assert profile["infrastructure"]["control_plane"]["type"] == (
            selected["control_plane"]
        )
        assert profile["infrastructure"]["workers"]["type"] == selected["worker"]


def test_live_report_wrapper_loads_protected_project_environment():
    wrapper = (ROOT / "scripts" / "hetzner-capacity-report.sh").read_text()
    source = REPORT_PATH.read_text()

    assert 'source "${SCRIPT_DIR}/load-project-env.sh"' in wrapper
    assert "HCLOUD_TOKEN" in wrapper
    assert "Authorization" in source
    assert "Bearer {token}" in source
    assert "print(token" not in source
