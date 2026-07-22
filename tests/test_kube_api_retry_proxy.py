from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "scripts/kube-api-retry-proxy.py"
SUPERVISOR = ROOT / "scripts/kube-api-tunnel-supervisor.sh"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _start_proxy(tmp_path: Path, *, connect_timeout: float) -> tuple[subprocess.Popen, int, Path]:
    port = _free_port()
    # AF_UNIX paths are capped at roughly 104 bytes on macOS. Pytest's nested
    # temporary directory can exceed that before the socket name is appended.
    backend = Path(f"/tmp/k8s-api-test-{port}-{time.time_ns()}.sock")
    ready = tmp_path / "proxy.ready"
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROXY),
            "--listen-port",
            str(port),
            "--backend-unix",
            str(backend),
            "--connect-timeout",
            str(connect_timeout),
            "--retry-interval",
            "0.02",
            "--ready-file",
            str(ready),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not ready.exists():
        assert process.poll() is None, process.stderr.read()
        time.sleep(0.02)
    assert ready.exists()
    return process, port, backend


def _stop(process: subprocess.Popen, backend: Path) -> None:
    process.terminate()
    process.wait(timeout=3)
    backend.unlink(missing_ok=True)


def test_proxy_holds_and_replays_a_request_until_backend_recovers(tmp_path: Path):
    process, port, backend_path = _start_proxy(tmp_path, connect_timeout=2)

    def delayed_backend() -> None:
        time.sleep(0.2)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(backend_path))
            listener.listen(1)
            connection, _ = listener.accept()
            with connection:
                payload = connection.recv(4096)
                connection.sendall(b"recovered:" + payload)

    server = threading.Thread(target=delayed_backend)
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.settimeout(2)
            client.sendall(b"tls-client-hello")
            assert client.recv(4096) == b"recovered:tls-client-hello"
        server.join(timeout=2)
        assert not server.is_alive()
    finally:
        _stop(process, backend_path)


def test_proxy_replays_when_first_ssh_backend_drops_before_tls_response(tmp_path: Path):
    process, port, backend_path = _start_proxy(tmp_path, connect_timeout=2)
    first_backend_ready = threading.Event()

    def rotating_backend() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first:
            first.bind(str(backend_path))
            first.listen(1)
            first_backend_ready.set()
            connection, _ = first.accept()
            with connection:
                assert connection.recv(4096) == b"tls-client-hello"
        backend_path.unlink()
        time.sleep(0.1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as recovered:
            recovered.bind(str(backend_path))
            recovered.listen(1)
            connection, _ = recovered.accept()
            with connection:
                payload = connection.recv(4096)
                connection.sendall(b"new-control-plane:" + payload)

    server = threading.Thread(target=rotating_backend)
    server.start()
    assert first_backend_ready.wait(timeout=2)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.settimeout(2)
            client.sendall(b"tls-client-hello")
            assert client.recv(4096) == b"new-control-plane:tls-client-hello"
        server.join(timeout=2)
        assert not server.is_alive()
    finally:
        _stop(process, backend_path)


def test_proxy_fails_closed_after_bounded_backend_outage(tmp_path: Path):
    process, port, backend_path = _start_proxy(tmp_path, connect_timeout=0.3)
    started = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.settimeout(2)
            client.sendall(b"request")
            assert client.recv(4096) == b""
        elapsed = time.monotonic() - started
        assert elapsed >= 0.2
        assert elapsed < 1.5
    finally:
        _stop(process, backend_path)


def test_supervisor_uses_stable_proxy_and_private_reconnect_socket():
    supervisor = SUPERVISOR.read_text(encoding="utf-8")

    assert "kube-api-retry-proxy.py" in supervisor
    assert '--listen-port "$LOCAL_PORT"' in supervisor
    assert '--connect-timeout "$BACKEND_CONNECT_TIMEOUT"' in supervisor
    assert '-L "${backend_socket}:${target}:6443"' in supervisor
    assert "StreamLocalBindUnlink=yes" in supervisor
    assert "Kubernetes API retry proxy exited unexpectedly" in supervisor
    assert "trap 'stop_child; exit 0' INT TERM" in supervisor
    assert "trap cleanup_exit EXIT" in supervisor
    assert 'exit "$status"' in supervisor
