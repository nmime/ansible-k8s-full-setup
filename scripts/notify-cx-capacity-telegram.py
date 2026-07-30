#!/usr/bin/env python3
"""Notify Telegram when medium-optimized CX availability changes in Helsinki."""

from __future__ import annotations

import argparse
import fcntl
import html
import importlib.util
import json
import os
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
ROLE_ORDER = ("bastion", "control_plane", "worker")


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
    location_result = {
        **location,
        "available": bool(plan["all_types_available"]),
        "types": plan["types"],
        "availability": plan["availability"],
        "control_plane_count": plan["control_plane_count"],
        "worker_count": plan["worker_count"],
        "infrastructure_monthly_net": plan["infrastructure_monthly_net"],
        "volume_gib": plan["volume_gib"],
        "volume_monthly_net": plan["volume_monthly_net"],
        "local_reserved_gib": plan["local_reserved_gib"],
        "total_monthly_net": plan["total_monthly_net"],
    }
    return {
        "schema_version": 3,
        "checked_at": utc_now(),
        "profile": "medium-optimized",
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
            status = "⚫ UNAVAILABLE — 0/3 shapes"
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
                    f"• target: 1 × <code>{html.escape(types['bastion'])}</code>, "
                    f"{location['control_plane_count']} × "
                    f"<code>{html.escape(types['control_plane'])}</code>, "
                    f"{location['worker_count']} × "
                    f"<code>{html.escape(types['worker'])}</code>",
                    f"• infrastructure plan: "
                    f"€{html.escape(location['infrastructure_monthly_net'])}/month net",
                    f"• CSI volumes: {location['volume_gib']} GiB, "
                    f"€{html.escape(location['volume_monthly_net'])}/month net",
                    f"• active local claims: {location['local_reserved_gib']} GiB",
                    f"• <b>planned total: "
                    f"€{html.escape(location['total_monthly_net'])}/month net</b>",
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
            "📊 <b>Hetzner CX capacity changed</b>",
            "",
            "Required for <code>medium-optimized</code>: "
            "<code>cx23 + cx33 + cx43</code>.",
            locations_text,
            "",
            f"Checked: <code>{html.escape(snapshot['checked_at'])}</code>",
            "",
            "Inspect the current location report:",
            f"<code>./scripts/hetzner-capacity-report.sh --location "
            f"{html.escape(first_location)}</code>",
            "Notification only: this monitor contains no provisioning path and "
            "never creates, resizes, or deletes resources.",
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
            "Notify Telegram when medium-optimized CX availability changes in Helsinki."
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
            result = reconcile(
                snapshot,
                args.state_file,
                token,
                chat_id,
                dry_run=args.dry_run,
            )
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
