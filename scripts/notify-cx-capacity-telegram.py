#!/usr/bin/env python3
"""Acquire one CX33 and two CX43 servers when available in Helsinki."""

from __future__ import annotations

import argparse
import fcntl
import html
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_FILE = (
    ROOT / "platform-orchestrator" / ".state" / "cx-capacity-monitor.json"
)
TARGET_LOCATION = "hel1"
ROLE_ORDER = ("control_plane", "worker")
ORDER_SET = "k8s-cx33-cx43-pair"
ORDER_SPECS = (
    {
        "order_key": "cx33-1",
        "availability_role": "control_plane",
        "server_type": "cx33",
        "name_suffix": "master-1",
        "server_role": "reserve-master",
        "display_role": "Kubernetes control plane",
    },
    {
        "order_key": "cx43-1",
        "availability_role": "worker",
        "server_type": "cx43",
        "name_suffix": "worker-1",
        "server_role": "reserve-worker",
        "display_role": "Kubernetes worker",
    },
    {
        "order_key": "cx43-2",
        "availability_role": "worker",
        "server_type": "cx43",
        "name_suffix": "worker-2",
        "server_role": "reserve-worker",
        "display_role": "Kubernetes worker",
    },
)
LEGACY_ORDER_KEYS = {"cx33": "cx33-1", "cx43": "cx43-1"}


def load_capacity_report() -> Any:
    module_path = ROOT / "scripts" / "hetzner-capacity-report.py"
    spec = importlib.util.spec_from_file_location(
        "hetzner_capacity_report", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Hetzner capacity report")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def helsinki_location(document: dict[str, Any]) -> dict[str, str]:
    for location in document.get("locations", []):
        name = str(location.get("name", ""))
        if name == TARGET_LOCATION:
            return {
                "name": name,
                "city": str(location.get("city", "")),
                "country": str(location.get("country", "")).upper(),
                "network_zone": str(location.get("network_zone", "")),
            }
    raise RuntimeError(
        f"Hetzner API returned no location named {TARGET_LOCATION} (Helsinki)"
    )


def collect_snapshot(
    token: str,
    endpoint: str,
    profiles_dir: Path,
    api_get: Callable[[str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report = load_capacity_report()
    getter = api_get or report.api_get
    location_document = getter("locations?per_page=50", token, endpoint)
    server_document = getter("server_types?per_page=50", token, endpoint)
    pricing_document = getter("pricing", token, endpoint)
    location = helsinki_location(location_document)

    pricing = pricing_document.get("pricing", {})
    catalog = report.normalize_catalog(
        server_document.get("server_types", []), TARGET_LOCATION
    )
    plans = report.build_plans(catalog, pricing, profiles_dir, TARGET_LOCATION)
    plan = next(
        (
            item
            for item in plans
            if item["profile"] == "medium-optimized" and item["family"] == "cx"
        ),
        None,
    )
    if plan is None:
        raise RuntimeError("medium-optimized CX mapping is missing from capacity plans")
    requested_types = {role: plan["types"][role] for role in ROLE_ORDER}
    requested_availability = {
        role: bool(plan["availability"].get(role, False)) for role in ROLE_ORDER
    }
    location_result = {
        **location,
        "available": all(requested_availability.values()),
        "types": requested_types,
        "availability": requested_availability,
    }
    return {
        "schema_version": 5,
        "checked_at": utc_now(),
        "profile": "k8s-cx-pair",
        "family": "cx",
        "complete_locations": (
            [TARGET_LOCATION] if location_result["available"] else []
        ),
        "locations": {TARGET_LOCATION: location_result},
    }


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read capacity state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"capacity state {path} is not a JSON object")
    return value


def atomic_write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def availability_signature(snapshot: dict[str, Any]) -> dict[str, dict[str, bool]]:
    return {
        name: {
            role: bool(location["availability"].get(role, False)) for role in ROLE_ORDER
        }
        for name, location in sorted(snapshot["locations"].items())
    }


def build_message(
    snapshot: dict[str, Any],
    changed_locations: list[str],
    previous_signature: dict[str, dict[str, bool]] | None = None,
) -> str:
    escaped_locations = []
    previous_signature = previous_signature or {}
    for name in changed_locations:
        location = snapshot["locations"][name]
        types = location["types"]
        available_types = [
            types[role] for role in ROLE_ORDER if location["availability"][role]
        ]
        missing_types = [
            types[role] for role in ROLE_ORDER if not location["availability"][role]
        ]
        if not missing_types:
            status = "🟢 COMPLETE — available"
        elif available_types:
            status = f"🟡 PARTIAL — {len(available_types)}/{len(ROLE_ORDER)} shapes"
        else:
            status = "⚫ UNAVAILABLE — 0/2 shapes"
        previous = previous_signature.get(name, {})
        transitions = [
            f"<code>{html.escape(types[role])}</code>: "
            f"{'available' if previous.get(role, False) else 'unavailable'} → "
            f"{'available' if location['availability'][role] else 'unavailable'}"
            for role in ROLE_ORDER
            if role in previous
            and bool(previous[role]) != bool(location["availability"][role])
        ]
        escaped_locations.append(
            "\n".join(
                [
                    f"📍 <b>{html.escape(name)}</b> — "
                    f"{html.escape(location['city'])}, {html.escape(location['country'])}",
                    f"• status: <b>{status}</b>",
                    "• available: "
                    + (
                        ", ".join(
                            f"<code>{html.escape(name)}</code>"
                            for name in available_types
                        )
                        if available_types
                        else "none"
                    ),
                    "• missing: "
                    + (
                        ", ".join(
                            f"<code>{html.escape(name)}</code>"
                            for name in missing_types
                        )
                        if missing_types
                        else "none"
                    ),
                    *([f"• changed: {'; '.join(transitions)}"] if transitions else []),
                    f"• target: 1 × "
                    f"<code>{html.escape(types['control_plane'])}</code>, "
                    f"2 × "
                    f"<code>{html.escape(types['worker'])}</code>",
                ]
            )
        )
    locations_text = "\n\n".join(escaped_locations)
    complete_locations = [
        name for name in changed_locations if snapshot["locations"][name]["available"]
    ]
    first_location = (
        complete_locations[0] if complete_locations else changed_locations[0]
    )
    return "\n".join(
        [
            "📊 <b>Hetzner Helsinki CX pair availability changed</b>",
            "",
            "Requested Kubernetes reserve: <code>1 × cx33 + 2 × cx43</code>.",
            locations_text,
            "",
            f"Checked: <code>{html.escape(snapshot['checked_at'])}</code>",
            "",
            "Inspect the current location report:",
            f"<code>./scripts/hetzner-capacity-report.sh --location "
            f"{html.escape(first_location)}</code>",
            "Available types are acquired independently. The hard cap is one "
            "<code>cx33</code> and two <code>cx43</code>; no other server type "
            "is created.",
        ]
    )


def telegram_credentials() -> tuple[str, str]:
    token = os.environ.get(
        "CX_CAPACITY_TELEGRAM_BOT_TOKEN",
        os.environ.get("ALERT_TELEGRAM_BOT_TOKEN", ""),
    )
    chat_id = os.environ.get(
        "CX_CAPACITY_TELEGRAM_CHAT_ID",
        os.environ.get("ALERT_TELEGRAM_CHAT_ID", ""),
    )
    return token, chat_id


def send_telegram(
    token: str,
    chat_id: str,
    message: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    if not token or not chat_id:
        raise RuntimeError(
            "Telegram is not configured; set CX_CAPACITY_TELEGRAM_BOT_TOKEN and "
            "CX_CAPACITY_TELEGRAM_CHAT_ID (or the ALERT_TELEGRAM_* fallbacks)"
        )
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ansible-k8s-full-setup/cx-capacity-monitor",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram API failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram API failed: {exc.reason}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        description = (
            str(result.get("description", "invalid response"))
            if isinstance(result, dict)
            else "invalid response"
        )
        raise RuntimeError(f"Telegram API rejected the message: {description}")


def env_enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def order_settings() -> dict[str, Any]:
    reservation_project = os.environ.get(
        "CX_CAPACITY_ORDER_PROJECT", "n0xeid-medium-optimized-cx-reserve"
    ).strip()
    if not reservation_project:
        raise RuntimeError("CX_CAPACITY_ORDER_PROJECT must not be empty")
    return {
        "enabled": env_enabled("CX_CAPACITY_ORDER_ENABLED", False),
        "hcloud_bin": str(
            Path(
                os.environ.get("CX_CAPACITY_HCLOUD_BIN", "/opt/homebrew/bin/hcloud")
            ).expanduser()
        ),
        "reservation_project": reservation_project,
        "cluster": os.environ.get(
            "CX_CAPACITY_ORDER_CLUSTER", "n0xeid-medium-optimized-cx"
        ).strip(),
        "network": os.environ.get(
            "CX_CAPACITY_ORDER_NETWORK",
            "n0xeid-medium-optimized-cx-network",
        ).strip(),
        "firewall": os.environ.get(
            "CX_CAPACITY_ORDER_FIREWALL",
            "n0xeid-medium-optimized-cx-fw-nodes",
        ).strip(),
        "ssh_key": os.environ.get("CX_CAPACITY_ORDER_SSH_KEY", "splox key 1").strip(),
        "placement_group": os.environ.get(
            "CX_CAPACITY_ORDER_PLACEMENT_GROUP",
            "n0xeid-medium-optimized-cx-spread",
        ).strip(),
        "image": os.environ.get("CX_CAPACITY_ORDER_IMAGE", "ubuntu-24.04").strip(),
    }


def run_hcloud(arguments: list[str], binary: str) -> subprocess.CompletedProcess[str]:
    path = Path(binary)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"hcloud binary is not executable: {binary}")
    try:
        return subprocess.run(
            [binary, *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"hcloud command failed to execute: {exc}") from exc


def hcloud_json(result: subprocess.CompletedProcess[str], context: str) -> Any:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"{context} failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{context} returned invalid JSON") from exc


def server_type_name(server: dict[str, Any]) -> str:
    value = server.get("server_type", "")
    if isinstance(value, dict):
        return str(value.get("name", ""))
    return str(value)


def expected_server_name(
    settings: dict[str, Any], specification: dict[str, str]
) -> str:
    return f"{settings['reservation_project']}-{specification['name_suffix']}"


def expected_server_labels(
    settings: dict[str, Any], specification: dict[str, str]
) -> dict[str, str]:
    return {
        "project": settings["reservation_project"],
        "cluster": settings["cluster"],
        "role": specification["server_role"],
        "managed-by": "cx-capacity-monitor",
        "order-set": ORDER_SET,
    }


def validate_managed_server(
    server: dict[str, Any],
    settings: dict[str, Any],
    specification: dict[str, str],
) -> None:
    name = expected_server_name(settings, specification)
    if str(server.get("name", "")) != name:
        raise RuntimeError(f"unexpected managed server name: {server.get('name')}")
    actual_type = server_type_name(server)
    if actual_type != specification["server_type"]:
        raise RuntimeError(
            f"managed server {name} has type {actual_type}, expected "
            f"{specification['server_type']}"
        )
    labels = server.get("labels", {})
    expected_labels = expected_server_labels(settings, specification)
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        raise RuntimeError(f"managed server {name} has unexpected labels")


def order_notification(server: dict[str, Any], specification: dict[str, str]) -> str:
    return "\n".join(
        [
            "🛒 <b>Hetzner Kubernetes reserve acquired</b>",
            f"Type: <code>{html.escape(specification['server_type'])}</code>",
            f"Role: {html.escape(specification['display_role'])}",
            f"Server: <code>{html.escape(str(server['name']))}</code>",
            f"Location: <code>{TARGET_LOCATION}</code> (Helsinki)",
            "Networking: private Kubernetes network, node firewall, no public IP",
            "Order cap: 1 × <code>cx33</code> + 2 × <code>cx43</code>.",
        ]
    )


def persist_orders(state_file: Path, orders: dict[str, Any]) -> None:
    state = read_state(state_file)
    state["orders"] = orders
    atomic_write_state(state_file, state)


def normalize_order_receipts(value: Any) -> dict[str, Any]:
    orders = dict(value) if isinstance(value, dict) else {}
    for legacy_key, order_key in LEGACY_ORDER_KEYS.items():
        if order_key not in orders and legacy_key in orders:
            orders[order_key] = orders[legacy_key]
        orders.pop(legacy_key, None)
    return orders


def maybe_order_servers(
    snapshot: dict[str, Any],
    state_file: Path,
    token: str,
    chat_id: str,
    *,
    notify: Callable[[str, str, str], None] = send_telegram,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    settings = order_settings()
    if not settings["enabled"]:
        return {"enabled": False, "limit": len(ORDER_SPECS), "statuses": {}}

    execute = runner or (
        lambda arguments: run_hcloud(arguments, settings["hcloud_bin"])
    )
    inventory_result = execute(
        [
            "server",
            "list",
            "--selector",
            f"order-set={ORDER_SET}",
            "-o",
            "json",
        ]
    )
    inventory = hcloud_json(inventory_result, "managed server inventory")
    if not isinstance(inventory, list):
        raise RuntimeError("managed server inventory is not a JSON list")
    if len(inventory) > len(ORDER_SPECS):
        raise RuntimeError(
            f"managed server cap exceeded: found {len(inventory)}, "
            f"maximum is {len(ORDER_SPECS)}"
        )

    expected_by_name = {
        expected_server_name(settings, specification): specification
        for specification in ORDER_SPECS
    }
    unexpected = sorted(
        str(server.get("name", ""))
        for server in inventory
        if str(server.get("name", "")) not in expected_by_name
    )
    if unexpected:
        raise RuntimeError(
            f"unexpected servers use order-set={ORDER_SET}: {', '.join(unexpected)}"
        )
    for server in inventory:
        validate_managed_server(server, settings, expected_by_name[str(server["name"])])

    state = read_state(state_file)
    previous_orders = state.get("orders", {})
    orders = normalize_order_receipts(previous_orders)
    if orders != previous_orders:
        persist_orders(state_file, orders)
    statuses: dict[str, str] = {}
    by_name = {str(server["name"]): server for server in inventory}
    availability = snapshot["locations"][TARGET_LOCATION]["availability"]

    for specification in ORDER_SPECS:
        order_key = specification["order_key"]
        server_type = specification["server_type"]
        name = expected_server_name(settings, specification)
        existing = by_name.get(name)
        previous = orders.get(order_key, {})
        previous = previous if isinstance(previous, dict) else {}

        if existing is None and previous.get("status") == "acquired":
            statuses[order_key] = "previously-acquired-missing"
            continue

        if existing is None and not bool(
            availability.get(specification["availability_role"], False)
        ):
            statuses[order_key] = "waiting-for-capacity"
            continue

        if existing is None:
            if len(by_name) >= len(ORDER_SPECS):
                raise RuntimeError("managed server cap reached before order")
            name_check = execute(["server", "describe", name, "-o", "json"])
            if name_check.returncode == 0:
                existing = hcloud_json(name_check, f"server {name} lookup")
                if not isinstance(existing, dict):
                    raise RuntimeError(f"server {name} lookup returned invalid JSON")
                validate_managed_server(existing, settings, specification)
            else:
                lookup_error = (
                    name_check.stderr.strip() or name_check.stdout.strip()
                ).lower()
                if "not found" not in lookup_error:
                    raise RuntimeError(
                        f"server {name} lookup failed: "
                        f"{name_check.stderr.strip() or name_check.stdout.strip()}"
                    )
                labels = expected_server_labels(settings, specification)
                create_arguments = [
                    "server",
                    "create",
                    "--name",
                    name,
                    "--type",
                    server_type,
                    "--image",
                    settings["image"],
                    "--location",
                    TARGET_LOCATION,
                    "--ssh-key",
                    settings["ssh_key"],
                    "--network",
                    settings["network"],
                    "--firewall",
                    settings["firewall"],
                    "--placement-group",
                    settings["placement_group"],
                    "--without-ipv4",
                    "--without-ipv6",
                ]
                for key, value in labels.items():
                    create_arguments.extend(["--label", f"{key}={value}"])
                create_arguments.extend(["-o", "json"])
                created = execute(create_arguments)
                if created.returncode != 0:
                    detail = created.stderr.strip() or created.stdout.strip()
                    normalized = detail.lower()
                    if (
                        "resource_unavailable" in normalized
                        or "not available" in normalized
                        or "unavailable" in normalized
                    ):
                        statuses[order_key] = "capacity-lost-before-order"
                        continue
                    raise RuntimeError(f"server {name} creation failed: {detail}")
                create_document = hcloud_json(created, f"server {name} creation")
                existing = (
                    create_document.get("server", create_document)
                    if isinstance(create_document, dict)
                    else create_document
                )
                if not isinstance(existing, dict):
                    raise RuntimeError(f"server {name} creation returned invalid JSON")
                validate_managed_server(existing, settings, specification)
                by_name[name] = existing

        record = {
            **previous,
            "status": "acquired",
            "server_id": existing.get("id"),
            "server_name": name,
            "server_type": server_type,
            "role": specification["server_role"],
            "location": TARGET_LOCATION,
            "ordered_at": previous.get("ordered_at")
            or existing.get("created")
            or utc_now(),
        }
        orders[order_key] = record
        persist_orders(state_file, orders)
        if not record.get("notified_at"):
            try:
                notify(
                    token,
                    chat_id,
                    order_notification(existing, specification),
                )
            except RuntimeError as exc:
                record["notification_error"] = str(exc)
                orders[order_key] = record
                persist_orders(state_file, orders)
                raise
            record["notified_at"] = utc_now()
            record.pop("notification_error", None)
            orders[order_key] = record
            persist_orders(state_file, orders)
        statuses[order_key] = "acquired"

    return {
        "enabled": True,
        "limit": len(ORDER_SPECS),
        "statuses": statuses,
        "managed_count": len(by_name),
    }


def reconcile(
    snapshot: dict[str, Any],
    state_file: Path,
    token: str,
    chat_id: str,
    notify: Callable[[str, str, str], None] = send_telegram,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    previous = read_state(state_file)
    current = set(snapshot["complete_locations"])
    signature = availability_signature(snapshot)
    previous_signature = previous.get("notified_availability")
    if isinstance(previous_signature, dict):
        changed_locations = sorted(
            name
            for name, availability in signature.items()
            if availability != previous_signature.get(name)
        )
    else:
        # Schema-v1 states only tracked complete placement. On migration, send
        # one current snapshot for locations with any required CX shape.
        changed_locations = sorted(
            name
            for name, availability in signature.items()
            if any(availability.values())
        )
    notification_sent = False
    message = (
        build_message(
            snapshot,
            changed_locations,
            previous_signature if isinstance(previous_signature, dict) else None,
        )
        if changed_locations
        else ""
    )

    if changed_locations and not dry_run:
        notify(token, chat_id, message)
        notification_sent = True

    state = {
        **snapshot,
        "notified_complete_locations": sorted(current),
        "notified_availability": signature,
        "last_notification_at": (
            snapshot["checked_at"]
            if notification_sent
            else previous.get("last_notification_at")
        ),
    }
    if isinstance(previous.get("orders"), dict):
        state["orders"] = previous["orders"]
    if not dry_run:
        atomic_write_state(state_file, state)
    return {
        "checked_at": snapshot["checked_at"],
        "complete_locations": sorted(current),
        "changed_locations": changed_locations,
        "notification_sent": notification_sent,
        "dry_run": dry_run,
        "message": message,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire one CX33 and two CX43 servers when available in Helsinki."
        )
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=ROOT / "platform-orchestrator" / "profiles",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("HCLOUD_ENDPOINT", "https://api.hetzner.cloud/v1"),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="query and render a pending notification without sending or changing state",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="send a Telegram connectivity test without querying Hetzner",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token, chat_id = telegram_credentials()
    if args.test_telegram:
        try:
            send_telegram(
                token,
                chat_id,
                "✅ <b>Hetzner CX capacity monitor is connected</b>\n"
                "Telegram delivery works. No resources were provisioned.",
            )
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print("Telegram connectivity test sent.")
        return 0

    hcloud_token = os.environ.get("HCLOUD_TOKEN", "")
    if not hcloud_token:
        print(
            "ERROR: HCLOUD_TOKEN is required; load the gitignored project .env",
            file=sys.stderr,
        )
        return 2

    lock_path = Path(f"{args.state_file}.lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    try:
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            snapshot = collect_snapshot(hcloud_token, args.endpoint, args.profiles_dir)
            order_result = (
                None
                if args.dry_run
                else maybe_order_servers(
                    snapshot,
                    args.state_file,
                    token,
                    chat_id,
                )
            )
            result = reconcile(
                snapshot,
                args.state_file,
                token,
                chat_id,
                dry_run=args.dry_run,
            )
            if order_result is not None:
                result["orders"] = order_result
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.dry_run and result["message"]:
        print(result["message"])
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "message"},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
