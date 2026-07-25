#!/usr/bin/env python3
"""Notify Telegram when the complete medium-optimized CX mapping returns in the EU."""

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
DEFAULT_STATE_FILE = ROOT / "platform-orchestrator" / ".state" / "cx-capacity-monitor.json"
EU_COUNTRY_CODES = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)


def load_capacity_report() -> Any:
    module_path = ROOT / "scripts" / "hetzner-capacity-report.py"
    spec = importlib.util.spec_from_file_location("hetzner_capacity_report", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Hetzner capacity report")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def eu_locations(document: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for location in document.get("locations", []):
        country = str(location.get("country", "")).upper()
        name = str(location.get("name", ""))
        if country in EU_COUNTRY_CODES and name:
            result.append(
                {
                    "name": name,
                    "city": str(location.get("city", "")),
                    "country": country,
                    "network_zone": str(location.get("network_zone", "")),
                }
            )
    return sorted(result, key=lambda item: item["name"])


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
    locations = eu_locations(location_document)
    if not locations:
        raise RuntimeError("Hetzner API returned no EU locations")

    pricing = pricing_document.get("pricing", {})
    location_results: dict[str, dict[str, Any]] = {}
    for location in locations:
        name = location["name"]
        catalog = report.normalize_catalog(server_document.get("server_types", []), name)
        plans = report.build_plans(catalog, pricing, profiles_dir, name)
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
        location_results[name] = {
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

    complete = sorted(
        name for name, location in location_results.items() if location["available"]
    )
    return {
        "schema_version": 1,
        "checked_at": utc_now(),
        "profile": "medium-optimized",
        "family": "cx",
        "complete_locations": complete,
        "locations": location_results,
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


def build_message(snapshot: dict[str, Any], new_locations: list[str]) -> str:
    escaped_locations = []
    for name in new_locations:
        location = snapshot["locations"][name]
        types = location["types"]
        escaped_locations.append(
            "\n".join(
                [
                    f"📍 <b>{html.escape(name)}</b> — "
                    f"{html.escape(location['city'])}, {html.escape(location['country'])}",
                    f"• bastion: 1 × <code>{html.escape(types['bastion'])}</code>",
                    f"• control plane: {location['control_plane_count']} × "
                    f"<code>{html.escape(types['control_plane'])}</code>",
                    f"• workers: {location['worker_count']} × "
                    f"<code>{html.escape(types['worker'])}</code>",
                    f"• infrastructure: €{html.escape(location['infrastructure_monthly_net'])}/month net",
                    f"• CSI volumes: {location['volume_gib']} GiB, "
                    f"€{html.escape(location['volume_monthly_net'])}/month net",
                    f"• active local claims: {location['local_reserved_gib']} GiB",
                    f"• <b>total: €{html.escape(location['total_monthly_net'])}/month net</b>",
                ]
            )
        )
    locations_text = "\n\n".join(escaped_locations)
    first_location = new_locations[0]
    return "\n".join(
        [
            "🚨 <b>Hetzner CX capacity is deployable</b>",
            "",
            "The complete <code>medium-optimized</code> mapping is available:",
            locations_text,
            "",
            f"Checked: <code>{html.escape(snapshot['checked_at'])}</code>",
            "",
            "Reconfirm before provisioning:",
            f"<code>./scripts/hetzner-capacity-report.sh --location "
            f"{html.escape(first_location)}</code>",
            "Then set <code>infrastructure.region</code> to that location and deploy with "
            "<code>--capacity-family cx</code>. This monitor never provisions resources.",
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
    acknowledged = set(previous.get("notified_complete_locations", [])) & current
    new_locations = sorted(current - acknowledged)
    notification_sent = False
    message = build_message(snapshot, new_locations) if new_locations else ""

    if new_locations and not dry_run:
        notify(token, chat_id, message)
        acknowledged = set(current)
        notification_sent = True

    state = {
        **snapshot,
        "notified_complete_locations": sorted(acknowledged),
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
        "new_locations": new_locations,
        "notification_sent": notification_sent,
        "dry_run": dry_run,
        "message": message,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Notify Telegram when medium-optimized CX capacity returns in the EU."
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
            snapshot = collect_snapshot(
                hcloud_token, args.endpoint, args.profiles_dir
            )
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
