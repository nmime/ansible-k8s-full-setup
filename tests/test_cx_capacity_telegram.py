"""Contracts for the stateful Helsinki CX pair monitor and order cap."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT / "scripts" / "notify-cx-capacity-telegram.py"

spec = importlib.util.spec_from_file_location("cx_capacity_telegram", MONITOR_PATH)
assert spec and spec.loader
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def snapshot(
    *available_roles: str,
    checked_at: str = "2026-07-25T10:00:00+00:00",
) -> dict:
    assert set(available_roles) <= set(monitor.ROLE_ORDER)
    types = {"control_plane": "cx33", "worker": "cx43"}
    availability = {role: role in available_roles for role in monitor.ROLE_ORDER}
    complete = all(availability.values())
    return {
        "schema_version": 5,
        "checked_at": checked_at,
        "profile": "k8s-cx-pair",
        "family": "cx",
        "complete_locations": [monitor.TARGET_LOCATION] if complete else [],
        "locations": {
            monitor.TARGET_LOCATION: {
                "name": monitor.TARGET_LOCATION,
                "city": "Helsinki",
                "country": "FI",
                "network_zone": "eu-central",
                "available": complete,
                "types": types,
                "availability": availability,
            }
        },
    }


def completed(
    arguments: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        arguments,
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


class FakeHcloud:
    def __init__(self, servers: list[dict] | None = None) -> None:
        self.servers = {str(server["name"]): server for server in (servers or [])}
        self.calls: list[list[str]] = []
        self.created_types: list[str] = []

    def __call__(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if arguments[:2] == ["server", "list"]:
            return completed(arguments, stdout=json.dumps(list(self.servers.values())))
        if arguments[:2] == ["server", "describe"]:
            server = self.servers.get(arguments[2])
            if server is None:
                return completed(arguments, returncode=1, stderr="Server not found")
            return completed(arguments, stdout=json.dumps(server))
        if arguments[:2] == ["server", "create"]:
            name = arguments[arguments.index("--name") + 1]
            server_type = arguments[arguments.index("--type") + 1]
            labels: dict[str, str] = {}
            for index, value in enumerate(arguments):
                if value == "--label":
                    key, label_value = arguments[index + 1].split("=", 1)
                    labels[key] = label_value
            server = {
                "id": 1000 + len(self.servers),
                "name": name,
                "server_type": {"name": server_type},
                "labels": labels,
                "status": "running",
                "created": "2026-07-25T10:00:01+00:00",
            }
            self.servers[name] = server
            self.created_types.append(server_type)
            return completed(arguments, stdout=json.dumps(server))
        raise AssertionError(f"unexpected hcloud call: {arguments}")


def managed_server(
    specification: dict[str, str],
    *,
    name: str | None = None,
    server_id: int = 1000,
) -> dict:
    settings = monitor.order_settings()
    return {
        "id": server_id,
        "name": name or monitor.expected_server_name(settings, specification),
        "server_type": {"name": specification["server_type"]},
        "labels": monitor.expected_server_labels(settings, specification),
        "status": "running",
        "created": "2026-07-25T10:00:01+00:00",
    }


def test_location_filter_selects_only_hetzner_helsinki():
    document = {
        "locations": [
            {
                "name": "nbg1",
                "city": "Nuremberg",
                "country": "DE",
                "network_zone": "eu-central",
            },
            {
                "name": "hel1",
                "city": "Helsinki",
                "country": "fi",
                "network_zone": "eu-central",
            },
        ]
    }

    assert monitor.helsinki_location(document) == {
        "name": "hel1",
        "city": "Helsinki",
        "country": "FI",
        "network_zone": "eu-central",
    }


def test_location_filter_fails_closed_without_helsinki():
    with pytest.raises(RuntimeError, match=r"hel1 \(Helsinki\)"):
        monitor.helsinki_location(
            {"locations": [{"name": "fsn1", "city": "Falkenstein"}]}
        )


def test_notifies_on_first_availability_and_return_but_not_unchanged(tmp_path):
    state_file = tmp_path / "nested" / "state.json"
    messages: list[str] = []
    notify = lambda _token, _chat_id, message: messages.append(message)

    first = monitor.reconcile(
        snapshot("control_plane", "worker"),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    unchanged = monitor.reconcile(
        snapshot(
            "control_plane",
            "worker",
            checked_at="2026-07-25T10:15:00+00:00",
        ),
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
        snapshot(
            "control_plane",
            "worker",
            checked_at="2026-07-25T10:45:00+00:00",
        ),
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
    assert "1 × <code>cx33</code>" in messages[0]
    assert "2 × <code>cx43</code>" in messages[0]
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
            snapshot("control_plane"),
            state_file,
            "token",
            "chat",
            notify=fail,
        )
    assert not state_file.exists()

    delivered: list[str] = []
    result = monitor.reconcile(
        snapshot("control_plane"),
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
        snapshot("worker"),
        state_file,
        "",
        "",
        notify=lambda _token, _chat_id, message: calls.append(message),
        dry_run=True,
    )

    assert result["changed_locations"] == ["hel1"]
    assert result["notification_sent"] is False
    assert calls == []
    assert not state_file.exists()


def test_state_never_persists_credentials(tmp_path):
    state_file = tmp_path / "state.json"
    monitor.reconcile(
        snapshot("control_plane"),
        state_file,
        "super-secret-token",
        "-123456",
        notify=lambda *_args: None,
    )
    raw = state_file.read_text(encoding="utf-8")

    assert "super-secret-token" not in raw
    assert "-123456" not in raw


def test_partial_availability_and_each_transition_are_reported_once(tmp_path):
    state_file = tmp_path / "state.json"
    messages: list[str] = []
    notify = lambda _token, _chat_id, message: messages.append(message)

    first = monitor.reconcile(
        snapshot("control_plane"),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    unchanged = monitor.reconcile(
        snapshot(
            "control_plane",
            checked_at="2026-07-25T10:01:00+00:00",
        ),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    expanded = monitor.reconcile(
        snapshot(
            "control_plane",
            "worker",
            checked_at="2026-07-25T10:02:00+00:00",
        ),
        state_file,
        "token",
        "chat",
        notify=notify,
    )
    lost = monitor.reconcile(
        snapshot(checked_at="2026-07-25T10:03:00+00:00"),
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
    assert "PARTIAL — 1/2 shapes" in messages[0]
    assert "available: <code>cx33</code>" in messages[0]
    assert "missing: <code>cx43</code>" in messages[0]
    assert "<code>cx43</code>: unavailable → available" in messages[1]
    assert "UNAVAILABLE — 0/2 shapes" in messages[2]


def test_reconcile_preserves_lifetime_order_receipts(tmp_path):
    state_file = tmp_path / "state.json"
    monitor.atomic_write_state(
        state_file,
        {"orders": {"cx33": {"status": "acquired", "server_id": 42}}},
    )

    monitor.reconcile(
        snapshot(),
        state_file,
        "token",
        "chat",
        notify=lambda *_args: None,
    )

    assert monitor.read_state(state_file)["orders"]["cx33"]["server_id"] == 42


def test_orders_each_available_type_independently_and_never_duplicates(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CX_CAPACITY_ORDER_ENABLED", "true")
    state_file = tmp_path / "state.json"
    runner = FakeHcloud()
    messages: list[str] = []
    notify = lambda _token, _chat_id, message: messages.append(message)

    first = monitor.maybe_order_servers(
        snapshot("control_plane"),
        state_file,
        "token",
        "chat",
        notify=notify,
        runner=runner,
    )
    second = monitor.maybe_order_servers(
        snapshot("control_plane", "worker"),
        state_file,
        "token",
        "chat",
        notify=notify,
        runner=runner,
    )
    third = monitor.maybe_order_servers(
        snapshot("control_plane", "worker"),
        state_file,
        "token",
        "chat",
        notify=notify,
        runner=runner,
    )

    assert first["statuses"] == {
        "cx33-1": "acquired",
        "cx43-1": "waiting-for-capacity",
        "cx43-2": "waiting-for-capacity",
    }
    assert second["statuses"] == {
        "cx33-1": "acquired",
        "cx43-1": "acquired",
        "cx43-2": "acquired",
    }
    assert third["managed_count"] == 3
    assert runner.created_types == ["cx33", "cx43", "cx43"]
    assert len(runner.servers) == 3
    assert len(messages) == 3
    create_calls = [call for call in runner.calls if call[:2] == ["server", "create"]]
    assert all("--without-ipv4" in call for call in create_calls)
    assert all("--without-ipv6" in call for call in create_calls)
    assert all(call[call.index("--location") + 1] == "hel1" for call in create_calls)
    orders = monitor.read_state(state_file)["orders"]
    assert set(orders) == {"cx33-1", "cx43-1", "cx43-2"}
    assert all(order["status"] == "acquired" for order in orders.values())


def test_lifetime_receipt_prevents_reordering_a_deleted_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CX_CAPACITY_ORDER_ENABLED", "true")
    state_file = tmp_path / "state.json"
    monitor.atomic_write_state(
        state_file,
        {
            "orders": {
                "cx33": {
                    "status": "acquired",
                    "server_id": 42,
                    "server_name": "deleted-reservation",
                }
            }
        },
    )
    runner = FakeHcloud()

    result = monitor.maybe_order_servers(
        snapshot("control_plane"),
        state_file,
        "token",
        "chat",
        notify=lambda *_args: None,
        runner=runner,
    )

    assert result["statuses"]["cx33-1"] == "previously-acquired-missing"
    assert runner.created_types == []
    assert set(monitor.read_state(state_file)["orders"]) == {"cx33-1"}


def test_legacy_type_receipts_migrate_to_unique_order_slots():
    migrated = monitor.normalize_order_receipts(
        {
            "cx33": {"server_id": 1},
            "cx43": {"server_id": 2},
        }
    )

    assert migrated == {
        "cx33-1": {"server_id": 1},
        "cx43-1": {"server_id": 2},
    }


def test_managed_inventory_fails_closed_above_three_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("CX_CAPACITY_ORDER_ENABLED", "true")
    servers = [
        managed_server(monitor.ORDER_SPECS[0], server_id=1),
        managed_server(monitor.ORDER_SPECS[1], server_id=2),
        managed_server(monitor.ORDER_SPECS[2], server_id=3),
        managed_server(
            monitor.ORDER_SPECS[2],
            name="unexpected-fourth-server",
            server_id=4,
        ),
    ]

    with pytest.raises(RuntimeError, match="cap exceeded"):
        monitor.maybe_order_servers(
            snapshot("control_plane", "worker"),
            tmp_path / "state.json",
            "token",
            "chat",
            notify=lambda *_args: None,
            runner=FakeHcloud(servers),
        )


def test_ordering_is_explicitly_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("CX_CAPACITY_ORDER_ENABLED", raising=False)
    result = monitor.maybe_order_servers(
        snapshot("control_plane", "worker"),
        tmp_path / "state.json",
        "token",
        "chat",
        runner=lambda _arguments: pytest.fail("hcloud must not run"),
    )
    assert result == {"enabled": False, "limit": 3, "statuses": {}}


def test_wrapper_uses_protected_env_and_has_no_cluster_deployment_path():
    wrapper = (ROOT / "scripts" / "notify-cx-capacity-telegram.sh").read_text(
        encoding="utf-8"
    )
    source = MONITOR_PATH.read_text(encoding="utf-8")

    assert 'source "${SCRIPT_DIR}/load-project-env.sh"' in wrapper
    assert "ALERT_TELEGRAM_BOT_TOKEN" in source
    assert "ALERT_TELEGRAM_CHAT_ID" in source
    assert "CX_CAPACITY_ORDER_ENABLED" in source
    assert "ORDER_SPECS" in source
    assert source.index("order_result =") < source.index("result = reconcile(")
    assert "run_tier.sh" not in source
    assert "CX_CAPACITY_AUTO_DEPLOY" not in source
    assert "print(token" not in source
    assert "print(chat_id" not in source
