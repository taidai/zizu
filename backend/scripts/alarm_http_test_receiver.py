"""Ephemeral, in-memory HTTP receiver used only by ZiZu alarm E2E tests."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Any


class _ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        response_status: int,
        delay_seconds: float,
    ) -> None:
        self.response_status = response_status
        self.delay_seconds = delay_seconds
        self._records: list[dict[str, Any]] = []
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        super().__init__(address, _ReceiverHandler)

    def record_once(
        self,
        idempotency_key: str,
        path: str,
        body: bytes,
        content_type: str | None,
    ) -> None:
        with self._lock:
            if idempotency_key in self._seen:
                return
            self._seen.add(idempotency_key)
            try:
                parsed_body: Any = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed_body = body.decode("utf-8", errors="replace")
            self._records.append(
                {
                    "idempotency_key": idempotency_key,
                    "path": path,
                    "content_type": content_type,
                    "body": parsed_body,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._records]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._seen.clear()


class _ReceiverHandler(BaseHTTPRequestHandler):
    server: _ReceiverServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1024 * 1024:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        key = (self.headers.get("Idempotency-Key") or "").strip()
        if not key:
            self.send_error(400)
            return
        self.server.record_once(
            key,
            self.path,
            body,
            self.headers.get("Content-Type"),
        )
        if self.server.delay_seconds:
            time.sleep(self.server.delay_seconds)
        self.send_response(self.server.response_status)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/records":
            self.send_error(404)
            return
        body = json.dumps(
            {"items": self.server.records()},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        if self.path != "/records":
            self.send_error(404)
            return
        self.server.clear()
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_arguments: object) -> None:
        return


class AlarmHttpTestReceiver:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        response_status: int = 204,
        delay_seconds: float = 0,
    ) -> None:
        if not 200 <= response_status <= 599:
            raise ValueError("response status must be between 200 and 599")
        if delay_seconds < 0 or delay_seconds > 60:
            raise ValueError("delay seconds must be between 0 and 60")
        self._server = _ReceiverServer(
            (host, port),
            response_status=response_status,
            delay_seconds=delay_seconds,
        )
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="alarm-http-test-receiver",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        self._thread = None

    def records(self) -> list[dict[str, Any]]:
        return self._server.records()

    def serve_forever(self) -> None:
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--response-status", type=int, default=204)
    parser.add_argument("--delay-seconds", type=float, default=0)
    arguments = parser.parse_args()
    receiver = AlarmHttpTestReceiver(
        arguments.host,
        arguments.port,
        response_status=arguments.response_status,
        delay_seconds=arguments.delay_seconds,
    )
    print(
        json.dumps(
            {"status": "listening", "host": arguments.host, "port": receiver.port},
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        receiver.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
