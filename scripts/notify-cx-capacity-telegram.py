#!/usr/bin/env python3
"""Notify Telegram when the complete medium-optimized CX mapping returns in the EU."""

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
from datetime import datetime, timedelta, timezone
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
ROLE_ORDER = ("bastion", "control_plane", "worker")


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
        "schema_version": 2,
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


def availability_signature(snapshot: dict[str, Any]) -> dict[str, dict[str, bool]]:
    return {
        name: {
            role: bool(location["availability"].get(role, False))
            for role in ROLE_ORDER
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
            status = "🟢 COMPLETE — deployable"
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
    first_location = complete_locations[0] if complete_locations else changed_locations[0]
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
            "Provision only when the location is COMPLETE. Then set "
            "<code>infrastructure.region</code> to that location and use "
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


def deployment_settings() -> dict[str, Any]:
    project = os.environ.get(
        "CX_CAPACITY_DEPLOY_PROJECT", "n0xeid-medium-optimized-cx"
    )
    run_root = Path(
        os.environ.get(
            "CX_CAPACITY_DEPLOY_RUN_ROOT",
            str(ROOT / ".campaign-state" / project / "controller"),
        )
    ).expanduser()
    if not run_root.is_absolute():
        raise RuntimeError("CX_CAPACITY_DEPLOY_RUN_ROOT must be absolute")
    try:
        retry_seconds = int(
            os.environ.get("CX_CAPACITY_DEPLOY_RETRY_SECONDS", "300")
        )
        stale_seconds = int(
            os.environ.get("CX_CAPACITY_DEPLOY_STALE_SECONDS", "900")
        )
    except ValueError as exc:
        raise RuntimeError(
            "CX capacity deployment retry/stale intervals must be integers"
        ) from exc
    return {
        "project": project,
        "domain": os.environ.get(
            "CX_CAPACITY_DEPLOY_DOMAIN", "medium-optimized.n0xeid.xyz"
        ),
        "dns_zone": os.environ.get("CX_CAPACITY_DNS_ZONE", "n0xeid.xyz"),
        "certificate_issuer": os.environ.get(
            "CX_CAPACITY_CERTIFICATE_ISSUER", "letsencrypt-prod"
        ),
        "manage_dns": env_enabled("CX_CAPACITY_MANAGE_DNS", True),
        "run_root": run_root,
        "retry_seconds": retry_seconds,
        "stale_seconds": stale_seconds,
    }


def build_deploy_command(location: str, settings: dict[str, Any]) -> list[str]:
    run_root = settings["run_root"]
    command = [
        str(ROOT / "run_tier.sh"),
        "medium-optimized",
        "--campaign-id",
        "cx-auto",
        "--project",
        settings["project"],
        "--domain",
        settings["domain"],
        "--location",
        location,
        "--run-root",
        str(run_root),
        "--config",
        str(run_root / "platform.yaml"),
        "--log-file",
        str(run_root / "logs" / "deploy.log"),
        "--capacity-family",
        "cx",
        "--dns-zone",
        settings["dns_zone"],
        "--certificate-issuer",
        settings["certificate_issuer"],
    ]
    if settings["manage_dns"]:
        command.append("--manage-dns")
    return command


def execute_deploy(command: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(log_path.parent, 0o700)
    with log_path.open("a", encoding="utf-8") as stream:
        os.chmod(log_path, 0o600)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    return completed.returncode


def deployment_message(
    status: str,
    location: str,
    settings: dict[str, Any],
    *,
    exit_code: int | None = None,
    log_path: Path | None = None,
) -> str:
    icons = {"running": "🚀", "succeeded": "✅", "failed": "❌"}
    lines = [
        f"{icons[status]} <b>CX medium-optimized deployment {html.escape(status)}</b>",
        f"Location: <code>{html.escape(location)}</code>",
        f"Project: <code>{html.escape(settings['project'])}</code>",
        f"Domain: <code>{html.escape(settings['domain'])}</code>",
    ]
    if exit_code is not None:
        lines.append(f"Exit code: <code>{exit_code}</code>")
    if log_path is not None:
        lines.append(f"Log: <code>{html.escape(str(log_path))}</code>")
    return "\n".join(lines)


def parse_state_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def maybe_auto_deploy(
    snapshot: dict[str, Any],
    state_file: Path,
    token: str,
    chat_id: str,
    *,
    notify: Callable[[str, str, str], None] | None = None,
    executor: Callable[[list[str], Path], int] = execute_deploy,
    recheck: Callable[[], dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    notify = notify or send_telegram
    if not env_enabled("CX_CAPACITY_AUTO_DEPLOY", False):
        return {"enabled": False, "triggered": False}
    settings = deployment_settings()
    if settings["retry_seconds"] < 60:
        raise RuntimeError("CX_CAPACITY_DEPLOY_RETRY_SECONDS must be at least 60")
    if settings["stale_seconds"] < 60:
        raise RuntimeError("CX_CAPACITY_DEPLOY_STALE_SECONDS must be at least 60")
    complete_locations = list(snapshot["complete_locations"])
    if not complete_locations:
        return {"enabled": True, "triggered": False, "reason": "no-complete-location"}

    current_time = now or datetime.now(timezone.utc)
    state = read_state(state_file)
    deployment = state.get("deployment", {})
    if deployment.get("status") == "succeeded":
        return {
            "enabled": True,
            "triggered": False,
            "reason": "already-succeeded",
            "location": deployment.get("location"),
        }
    running_updated = parse_state_time(deployment.get("updated_at"))
    if (
        deployment.get("status") == "running"
        and running_updated is not None
        and current_time
        < running_updated + timedelta(seconds=settings["stale_seconds"])
    ):
        return {
            "enabled": True,
            "triggered": False,
            "reason": "deployment-running",
            "location": deployment.get("location"),
        }
    next_retry = parse_state_time(deployment.get("next_retry_at"))
    if (
        deployment.get("status") == "failed"
        and next_retry is not None
        and current_time < next_retry
    ):
        return {
            "enabled": True,
            "triggered": False,
            "reason": "retry-backoff",
            "location": deployment.get("location"),
        }

    previous_location = str(deployment.get("location", ""))
    location = (
        previous_location
        if previous_location in complete_locations
        else sorted(complete_locations)[0]
    )
    if recheck is not None:
        refreshed = recheck()
        if location not in refreshed["complete_locations"]:
            return {
                "enabled": True,
                "triggered": False,
                "reason": "capacity-disappeared-on-recheck",
                "location": location,
            }

    run_root = settings["run_root"]
    console_log = run_root / "logs" / "auto-deploy-console.log"
    command = build_deploy_command(location, settings)
    attempts = int(deployment.get("attempts", 0)) + 1
    started_at = current_time.replace(microsecond=0).isoformat()
    state["deployment"] = {
        "status": "running",
        "location": location,
        "project": settings["project"],
        "domain": settings["domain"],
        "attempts": attempts,
        "started_at": started_at,
        "updated_at": started_at,
        "command": command,
        "console_log": str(console_log),
    }
    atomic_write_state(state_file, state)
    notify(
        token,
        chat_id,
        deployment_message("running", location, settings, log_path=console_log),
    )
    execution_error = None
    try:
        exit_code = executor(command, console_log)
    except OSError as exc:
        exit_code = 127
        execution_error = f"{type(exc).__name__}: {exc}"

    finished_at = datetime.now(timezone.utc).replace(microsecond=0)
    state = read_state(state_file)
    final_status = "succeeded" if exit_code == 0 else "failed"
    deployment = {
        **state.get("deployment", {}),
        "status": final_status,
        "exit_code": exit_code,
        "finished_at": finished_at.isoformat(),
        "updated_at": finished_at.isoformat(),
    }
    if exit_code != 0:
        deployment["next_retry_at"] = (
            finished_at + timedelta(seconds=settings["retry_seconds"])
        ).isoformat()
    if execution_error is not None:
        deployment["execution_error"] = execution_error
    state["deployment"] = deployment
    atomic_write_state(state_file, state)
    notify(
        token,
        chat_id,
        deployment_message(
            final_status,
            location,
            settings,
            exit_code=exit_code,
            log_path=console_log,
        ),
    )
    return {
        "enabled": True,
        "triggered": True,
        "status": final_status,
        "location": location,
        "exit_code": exit_code,
    }


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
            name for name, availability in signature.items() if any(availability.values())
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
    if isinstance(previous.get("deployment"), dict):
        state["deployment"] = previous["deployment"]
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
            if not args.dry_run:
                result["deployment"] = maybe_auto_deploy(
                    snapshot,
                    args.state_file,
                    token,
                    chat_id,
                    recheck=lambda: collect_snapshot(
                        hcloud_token, args.endpoint, args.profiles_dir
                    ),
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
