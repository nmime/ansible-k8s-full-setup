"""Contracts for the stateful EU CX Telegram capacity monitor."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT / "scripts" / "notify-cx-capacity-telegram.py"

spec = importlib.util.spec_from_file_location("cx_capacity_telegram", MONITOR_PATH)
assert spec and spec.loader
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def snapshot(*available: str, checked_at: str = "2026-07-25T10:00:00+00:00") -> dict:
    names = {"fsn1", "hel1", "nbg1"}
    locations = {}
    for name in names:
        locations[name] = {
            "name": name,
            "city": {"fsn1": "Falkenstein", "hel1": "Helsinki", "nbg1": "Nuremberg"}[
                name
            ],
            "country": "FI" if name == "hel1" else "DE",
            "network_zone": "eu-central",
            "available": name in available,
            "types": {
                "bastion": "cx23",
                "control_plane": "cx33",
                "worker": "cx43",
            },
            "availability": {
                "bastion": name in available,
                "control_plane": name in available,
                "worker": name in available,
            },
            "control_plane_count": 3,
            "worker_count": 3,
            "infrastructure_monthly_net": "86.92",
            "volume_gib": 240,
            "volume_monthly_net": "13.73",
            "local_reserved_gib": 300,
            "total_monthly_net": "100.65",
        }
    return {
        "schema_version": 1,
        "checked_at": checked_at,
        "profile": "medium-optimized",
        "family": "cx",
        "complete_locations": sorted(available),
        "locations": locations,
    }


def partial_snapshot(
    location: str,
    *roles: str,
    checked_at: str = "2026-07-25T10:00:00+00:00",
) -> dict:
    value = snapshot(checked_at=checked_at)
    value["locations"][location]["availability"] = {
        role: role in roles for role in monitor.ROLE_ORDER
    }
    value["locations"][location]["available"] = len(roles) == len(monitor.ROLE_ORDER)
    if value["locations"][location]["available"]:
        value["complete_locations"] = [location]
    return value


def test_eu_location_filter_uses_country_membership_not_name_assumptions():
    document = {
        "locations": [
            {
                "name": "nbg1",
                "city": "Nuremberg",
                "country": "DE",
                "network_zone": "eu-central",
            },
            {
                "name": "ash",
                "city": "Ashburn",
                "country": "US",
                "network_zone": "us-east",
            },
            {
                "name": "hel1",
                "city": "Helsinki",
                "country": "fi",
                "network_zone": "eu-central",
            },
        ]
    }

    assert [item["name"] for item in monitor.eu_locations(document)] == [
        "hel1",
        "nbg1",
    ]


def test_notifies_on_first_availability_and_return_but_not_unchanged(tmp_path):
    state_file = tmp_path / "nested" / "state.json"
    messages: list[str] = []

    def notify(_token: str, _chat_id: str, message: str) -> None:
        messages.append(message)

    first = monitor.reconcile(
        snapshot("fsn1"), state_file, "token", "chat", notify=notify
    )
    unchanged = monitor.reconcile(
        snapshot("fsn1", checked_at="2026-07-25T10:15:00+00:00"),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    lost = monitor.reconcile(
        snapshot(checked_at="2026-07-25T10:30:00+00:00"),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    returned = monitor.reconcile(
        snapshot("fsn1", checked_at="2026-07-25T10:45:00+00:00"),
        state_file,
        "token",
        "chat",
        notify=notify,
    )

    assert first["notification_sent"] is True
    assert unchanged["notification_sent"] is False
    assert lost["notification_sent"] is True
    assert returned["notification_sent"] is True
    assert len(messages) == 3
    assert "€100.65/month net" in messages[0]
    assert "3 × <code>cx33</code>" in messages[0]
    assert "3 × <code>cx43</code>" in messages[0]
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert state_file.parent.stat().st_mode & 0o777 == 0o700


def test_failed_notification_is_not_acknowledged_and_is_retried(tmp_path):
    state_file = tmp_path / "state.json"
    attempts = 0

    def fail(_token: str, _chat_id: str, _message: str) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("delivery failed")

    with pytest.raises(RuntimeError, match="delivery failed"):
        monitor.reconcile(
            snapshot("hel1"), state_file, "token", "chat", notify=fail
        )
    assert not state_file.exists()

    delivered: list[str] = []
    result = monitor.reconcile(
        snapshot("hel1"),
        state_file,
        "token",
        "chat",
        notify=lambda _token, _chat_id, message: delivered.append(message),
    )
    assert attempts == 1
    assert result["notification_sent"] is True
    assert len(delivered) == 1


def test_dry_run_does_not_send_or_change_state(tmp_path):
    state_file = tmp_path / "state.json"
    calls: list[str] = []
    result = monitor.reconcile(
        snapshot("nbg1"),
        state_file,
        "",
        "",
        notify=lambda _token, _chat_id, message: calls.append(message),
        dry_run=True,
    )

    assert result["changed_locations"] == ["nbg1"]
    assert result["notification_sent"] is False
    assert calls == []
    assert not state_file.exists()


def test_state_never_persists_credentials(tmp_path):
    state_file = tmp_path / "state.json"
    monitor.reconcile(
        snapshot("fsn1"),
        state_file,
        "super-secret-token",
        "-123456",
        notify=lambda *_args: None,
    )
    raw = state_file.read_text(encoding="utf-8")
    value = json.loads(raw)

    assert "super-secret-token" not in raw
    assert "-123456" not in raw
    assert value["notified_complete_locations"] == ["fsn1"]


def test_partial_availability_and_each_transition_are_reported_once(tmp_path):
    state_file = tmp_path / "state.json"
    messages: list[str] = []
    notify = lambda _token, _chat_id, message: messages.append(message)

    first = monitor.reconcile(
        partial_snapshot("hel1", "bastion"),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    unchanged = monitor.reconcile(
        partial_snapshot(
            "hel1", "bastion", checked_at="2026-07-25T10:01:00+00:00"
        ),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    expanded = monitor.reconcile(
        partial_snapshot(
            "hel1",
            "bastion",
            "control_plane",
            checked_at="2026-07-25T10:02:00+00:00",
        ),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    lost = monitor.reconcile(
        partial_snapshot("hel1", checked_at="2026-07-25T10:03:00+00:00"),
        state_file,
        "token",
        "chat",
        notify=notify,
    )

    assert first["changed_locations"] == ["hel1"]
    assert unchanged["changed_locations"] == []
    assert expanded["changed_locations"] == ["hel1"]
    assert lost["changed_locations"] == ["hel1"]
    assert len(messages) == 3
    assert "PARTIAL — 1/3 shapes" in messages[0]
    assert "available: <code>cx23</code>" in messages[0]
    assert "missing: <code>cx33</code>, <code>cx43</code>" in messages[0]
    assert "<code>cx33</code>: unavailable → available" in messages[1]
    assert "UNAVAILABLE — 0/3 shapes" in messages[2]


def test_wrapper_uses_protected_env_and_documented_fallbacks():
    wrapper = (ROOT / "scripts" / "notify-cx-capacity-telegram.sh").read_text(
        encoding="utf-8"
    )
    source = MONITOR_PATH.read_text(encoding="utf-8")

    assert 'source "${SCRIPT_DIR}/load-project-env.sh"' in wrapper
    assert "ALERT_TELEGRAM_BOT_TOKEN" in source
    assert "ALERT_TELEGRAM_CHAT_ID" in source
    assert "print(token" not in source
    assert "print(chat_id" not in source
