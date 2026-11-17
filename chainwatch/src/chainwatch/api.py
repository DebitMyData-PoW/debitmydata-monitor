"""/push: where the stats page turns a device's notifications on, changes or reads them.

A small HTTP server on this server only (DMDW_PUSH_LISTEN, 127.0.0.1:8471); nginx passes it
stats.debitmydata.io/push, with a limit on requests per visitor.

  GET  /push    {"enabled", "key", "transfer_floor_dmd", "max_pools", "max_addresses"}: the
                monitor's public key, for the browser's subscription
  POST /push    JSON, with "action":
     subscribe    {subscription: {endpoint, keys: {p256dh, auth}}, prefs}   turns them on
     update       {endpoint, prefs}                                          new choices
     get          {endpoint}          {on, prefs, items}: its choices and its recent notices
     unsubscribe  {endpoint}          turns them off (the row goes)
     test         {endpoint}          a test notice, within seconds (one per 10 seconds)
     renew        {old, subscription} the browser replaced the subscription (service worker)

A device is known by its subscription's endpoint (the push service's address for it), which
only that browser has. Only the browsers' push services are taken as endpoints (push.py).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from chainwatch import db, notify, push
from chainwatch.config import Settings
from chainwatch.rpc import Rpc, RpcError, RpcUnavailable

log = logging.getLogger(__name__)

MAX_BODY = 16 * 1024
TEST_EVERY = timedelta(seconds=10)


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class PushApi:
    """The answers, apart from HTTP (the tests call them directly)."""

    def __init__(self, settings: Settings, keys: push.Keys | None, rpc: Rpc | None) -> None:
        self.settings, self.keys, self.rpc = settings, keys, rpc
        self.lock = threading.Lock()
        self.conn: psycopg.Connection[Any] | None = None

    def _db(self) -> psycopg.Connection[Any]:
        if self.conn is None or self.conn.closed or self.conn.broken:
            self.conn = db.connect(self.settings.database_url)
        return self.conn

    def config(self) -> dict[str, Any]:
        return {
            "enabled": self.keys is not None,
            "key": self.keys.public if self.keys else None,
            "transfer_floor_dmd": float(self.settings.large_transfer_dmd),
            "max_pools": notify.MAX_POOLS,
            "max_addresses": notify.MAX_ADDRESSES,
        }

    def valid_address(self, address: str) -> bool:
        """The node says whether it is a DMD address; when it cannot answer, its form is enough."""
        if self.rpc is None:
            return True
        try:
            answer = self.rpc.call("validateaddress", address)
        except (RpcUnavailable, RpcError):
            return True
        return bool(answer.get("isvalid")) if isinstance(answer, dict) else False

    def _prefs(self, raw: Any) -> dict[str, Any]:
        try:
            return notify.clean_prefs(
                raw, float(self.settings.large_transfer_dmd), self.valid_address
            )
        except ValueError as exc:
            raise Refused(400, str(exc)) from exc

    @staticmethod
    def _subscription(raw: Any) -> tuple[str, str, str]:
        if not isinstance(raw, dict):
            raise Refused(400, "The browser's subscription is missing.")
        endpoint = str(raw.get("endpoint") or "")
        keys = raw.get("keys") or {}
        p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
        if len(endpoint) > 1000 or not push.host_allowed(endpoint):
            raise Refused(400, "This browser's push service is not one the monitor sends to.")
        if not 80 <= len(p256dh) <= 100 or not 16 <= len(auth) <= 40:
            raise Refused(400, "The browser's subscription keys are not valid.")
        return endpoint, p256dh, auth

    def _find(self, conn: psycopg.Connection[Any], endpoint: Any) -> dict[str, Any] | None:
        if not isinstance(endpoint, str) or not endpoint:
            raise Refused(400, "The subscription's endpoint is missing.")
        return conn.execute(
            "SELECT * FROM push_devices WHERE endpoint_hash = %s",
            (notify.endpoint_hash(endpoint),),
        ).fetchone()

    def _answer(self, row: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
        if row is None:
            return {"on": False, "prefs": notify.DEFAULT_PREFS, "items": []}
        return {
            "on": True,
            "prefs": {**notify.DEFAULT_PREFS, **row["prefs"]},
            "items": list(reversed(notify.inbox(row["sent"] or [], now))),
        }

    def handle(self, data: Any, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        if self.keys is None:
            raise Refused(503, "Notifications are not set up on this server yet.")
        if not isinstance(data, dict):
            raise Refused(400, "A JSON object is needed.")
        action = data.get("action")
        with self.lock:
            conn = self._db()
            if action == "subscribe":
                endpoint, p256dh, auth = self._subscription(data.get("subscription"))
                prefs = self._prefs(data.get("prefs") or {})
                old = self._find(conn, endpoint)
                if old is None:
                    count = conn.execute("SELECT count(*) AS n FROM push_devices").fetchone()
                    if count and count["n"] >= self.settings.push_max_devices:
                        raise Refused(503, "Too many devices: try again later.")
                    row = conn.execute(
                        "INSERT INTO push_devices (endpoint, endpoint_hash, p256dh, auth, prefs, "
                        "event_cursor, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, "
                        "%s, %s) RETURNING *",
                        (
                            endpoint,
                            notify.endpoint_hash(endpoint),
                            p256dh,
                            auth,
                            Jsonb(prefs),
                            notify.latest_event(conn),
                            now,
                            now,
                        ),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "UPDATE push_devices SET p256dh = %s, auth = %s, prefs = %s, "
                        "event_cursor = %s, failures = 0, updated_at = %s WHERE id = %s "
                        "RETURNING *",
                        (p256dh, auth, Jsonb(prefs), notify.latest_event(conn), now, old["id"]),
                    ).fetchone()
                return self._answer(row, now)
            if action == "update":
                old = self._find(conn, data.get("endpoint"))
                if old is None:
                    raise Refused(404, "Notifications are not on for this device.")
                prefs = self._prefs(data.get("prefs") or {})
                row = conn.execute(
                    "UPDATE push_devices SET prefs = %s, updated_at = %s WHERE id = %s RETURNING *",
                    (Jsonb(prefs), now, old["id"]),
                ).fetchone()
                return self._answer(row, now)
            if action == "get":
                return self._answer(self._find(conn, data.get("endpoint")), now)
            if action == "unsubscribe":
                old = self._find(conn, data.get("endpoint"))
                if old is not None:
                    conn.execute("DELETE FROM push_devices WHERE id = %s", (old["id"],))
                return {"on": False}
            if action == "test":
                old = self._find(conn, data.get("endpoint"))
                if old is None:
                    raise Refused(404, "Notifications are not on for this device.")
                if old["test_at"] is not None and now - old["test_at"] < TEST_EVERY:
                    raise Refused(429, "A test notice is already on its way.")
                conn.execute(
                    "UPDATE push_devices SET test_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, old["id"]),
                )
                return {"ok": True}
            if action == "renew":
                endpoint, p256dh, auth = self._subscription(data.get("subscription"))
                old = self._find(conn, data.get("old"))
                if old is None:
                    raise Refused(404, "Notifications are not on for this device.")
                with conn.transaction():
                    conn.execute(
                        "DELETE FROM push_devices WHERE endpoint_hash = %s AND id <> %s",
                        (notify.endpoint_hash(endpoint), old["id"]),
                    )
                    conn.execute(
                        "UPDATE push_devices SET endpoint = %s, endpoint_hash = %s, p256dh = %s, "
                        "auth = %s, failures = 0, updated_at = %s WHERE id = %s",
                        (endpoint, notify.endpoint_hash(endpoint), p256dh, auth, now, old["id"]),
                    )
                return {"ok": True}
        raise Refused(400, "Unknown action.")


class Handler(BaseHTTPRequestHandler):
    api: PushApi
    server_version = "dmd-push"
    sys_version = ""

    def _send(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/push":
            self._send(404, {"error": "Not found."})
            return
        self._send(200, self.api.config())

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/push":
            self._send(404, {"error": "Not found."})
            return
        origin = self.headers.get("Origin")
        site = urlsplit(self.api.settings.site_url)
        if origin and origin != f"{site.scheme}://{site.netloc}":
            self._send(403, {"error": "Only the stats page can do this."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._send(413 if length > MAX_BODY else 400, {"error": "A JSON body is needed."})
            return
        try:
            data = json.loads(self.rfile.read(length))
            self._send(200, self.api.handle(data))
        except ValueError:
            self._send(400, {"error": "A JSON body is needed."})
        except Refused as exc:
            self._send(exc.status, {"error": str(exc)})
        except psycopg.Error as exc:
            log.warning("push api: %s", exc)
            self._send(503, {"error": "The server is busy: try again in a moment."})

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("push api: " + format, *args)


def serve(api: PushApi, listen: str) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    handler = type("PushHandler", (Handler,), {"api": api})
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="push-api", daemon=True)
    thread.start()
    log.info("notifications API on %s", listen)
    return server
