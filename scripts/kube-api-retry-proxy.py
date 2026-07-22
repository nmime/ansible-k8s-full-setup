#!/usr/bin/env python3
"""Stable local TCP boundary for a reconnecting SSH Kubernetes API tunnel.

The proxy retries only while establishing the upstream TLS stream. It never
manufactures a Kubernetes response and stops retrying as soon as the backend
returns any bytes, so established requests retain normal fail-fast semantics.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import sys
import threading
import time
from pathlib import Path


COPY_CHUNK = 64 * 1024


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


class RetryProxy:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        backend_unix: str,
        connect_timeout: float,
        retry_interval: float,
        max_connections: int,
        max_replay_bytes: int,
        ready_file: str | None,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.backend_unix = backend_unix
        self.connect_timeout = connect_timeout
        self.retry_interval = retry_interval
        self.max_replay_bytes = max_replay_bytes
        self.ready_file = Path(ready_file) if ready_file else None
        self.stop_event = threading.Event()
        self.capacity = threading.BoundedSemaphore(max_connections)
        self.listener: socket.socket | None = None

    def stop(self, _signum: int | None = None, _frame: object | None = None) -> None:
        self.stop_event.set()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass

    def _mark_ready(self) -> None:
        if self.ready_file is None:
            return
        descriptor = os.open(
            self.ready_file,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()}\n")

    def _clear_ready(self) -> None:
        if self.ready_file is None:
            return
        try:
            self.ready_file.unlink()
        except FileNotFoundError:
            pass

    def _connect_backend(self, deadline: float) -> socket.socket | None:
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            backend = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            backend.settimeout(min(1.0, max(0.05, deadline - time.monotonic())))
            try:
                backend.connect(self.backend_unix)
                backend.settimeout(None)
                return backend
            except OSError:
                backend.close()
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self.stop_event.wait(min(self.retry_interval, remaining))
        return None

    @staticmethod
    def _relay(client: socket.socket, backend: socket.socket) -> None:
        open_sockets = {client: backend, backend: client}
        while open_sockets:
            readable, _, _ = select.select(list(open_sockets), [], [], 60.0)
            if not readable:
                continue
            for source in readable:
                destination = open_sockets.get(source)
                if destination is None:
                    continue
                try:
                    payload = source.recv(COPY_CHUNK)
                except OSError:
                    payload = b""
                if payload:
                    try:
                        destination.sendall(payload)
                    except OSError:
                        return
                    continue
                open_sockets.pop(source, None)
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

    def _handle_client(self, client: socket.socket) -> None:
        try:
            with client:
                deadline = time.monotonic() + self.connect_timeout
                replay = bytearray()
                client.settimeout(min(1.0, self.connect_timeout))
                while not replay and time.monotonic() < deadline:
                    try:
                        payload = client.recv(COPY_CHUNK)
                    except socket.timeout:
                        continue
                    if not payload:
                        return
                    replay.extend(payload)
                client.settimeout(None)
                if not replay or len(replay) > self.max_replay_bytes:
                    return

                while not self.stop_event.is_set() and time.monotonic() < deadline:
                    backend = self._connect_backend(deadline)
                    if backend is None:
                        return
                    with backend:
                        try:
                            backend.sendall(replay)
                        except OSError:
                            self.stop_event.wait(self.retry_interval)
                            continue

                        retry_backend = False
                        while time.monotonic() < deadline:
                            timeout = min(1.0, max(0.05, deadline - time.monotonic()))
                            try:
                                readable, _, _ = select.select(
                                    [client, backend], [], [], timeout
                                )
                            except OSError:
                                return
                            if not readable:
                                continue
                            if client in readable:
                                try:
                                    payload = client.recv(COPY_CHUNK)
                                except OSError:
                                    return
                                if not payload:
                                    return
                                replay.extend(payload)
                                if len(replay) > self.max_replay_bytes:
                                    return
                                try:
                                    backend.sendall(payload)
                                except OSError:
                                    retry_backend = True
                                    break
                            if backend in readable:
                                try:
                                    response = backend.recv(COPY_CHUNK)
                                except OSError:
                                    response = b""
                                if not response:
                                    retry_backend = True
                                    break
                                try:
                                    client.sendall(response)
                                except OSError:
                                    return
                                self._relay(client, backend)
                                return
                        if not retry_backend:
                            return
                    self.stop_event.wait(self.retry_interval)
        finally:
            self.capacity.release()

    def serve(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener = listener
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.listen_host, self.listen_port))
        listener.listen(128)
        listener.settimeout(1.0)
        self._mark_ready()
        try:
            while not self.stop_event.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                if not self.capacity.acquire(blocking=False):
                    client.close()
                    continue
                threading.Thread(
                    target=self._handle_client,
                    args=(client,),
                    daemon=True,
                ).start()
        finally:
            self._clear_ready()
            try:
                listener.close()
            except OSError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=_positive_int, required=True)
    parser.add_argument("--backend-unix", required=True)
    parser.add_argument("--connect-timeout", type=_positive_float, default=60.0)
    parser.add_argument("--retry-interval", type=_positive_float, default=0.2)
    parser.add_argument("--max-connections", type=_positive_int, default=64)
    parser.add_argument("--max-replay-bytes", type=_positive_int, default=1024 * 1024)
    parser.add_argument("--ready-file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    proxy = RetryProxy(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        backend_unix=args.backend_unix,
        connect_timeout=args.connect_timeout,
        retry_interval=args.retry_interval,
        max_connections=args.max_connections,
        max_replay_bytes=args.max_replay_bytes,
        ready_file=args.ready_file,
    )
    signal.signal(signal.SIGTERM, proxy.stop)
    signal.signal(signal.SIGINT, proxy.stop)
    try:
        proxy.serve()
    except OSError as error:
        print(f"kube-api retry proxy failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
