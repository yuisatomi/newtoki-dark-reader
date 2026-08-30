#!/usr/bin/env python3
import hmac
import ipaddress
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


DB_PATH = os.environ.get("SYNC_DB", "/var/lib/reader-sync/progress.db")
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")
ALLOWED_NETWORK = ipaddress.ip_network(
    os.environ.get("SYNC_ALLOWED_NETWORK", "192.168.100.0/24")
)
PORT = int(os.environ.get("SYNC_PORT", "8787"))


def connect():
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS progress (
                kind TEXT NOT NULL,
                work_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                position REAL NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (kind, work_id)
            )
        """)


def valid_id(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and all(
        ch.isalnum() or ch in "-_" for ch in value
    )


def validate_progress(data):
    if not isinstance(data, dict) or data.get("kind") not in ("novel", "webtoon"):
        raise ValueError("invalid kind")
    if not valid_id(data.get("work_id")) or not valid_id(data.get("episode_id")):
        raise ValueError("invalid work or episode id")
    position = float(data.get("position"))
    if not 0 <= position <= 1:
        raise ValueError("position must be between 0 and 1")
    title = str(data.get("title", ""))[:300]
    device_id = str(data.get("device_id", ""))[:128]
    return data["kind"], data["work_id"], data["episode_id"], position, title, device_id


class Handler(BaseHTTPRequestHandler):
    server_version = "ReaderSync/1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        try:
            return ipaddress.ip_address(self.client_address[0]) in ALLOWED_NETWORK
        except ValueError:
            return False

    def authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + SYNC_TOKEN
        return bool(SYNC_TOKEN) and hmac.compare_digest(supplied, expected)

    def require_access(self):
        if not self.allowed():
            self.send_json(403, {"error": "network not allowed"})
            return False
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return False
        return True

    def do_OPTIONS(self):
        self.send_json(204, {})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(200, {"ok": True})
            return
        if parsed.path != "/v1/progress":
            self.send_json(404, {"error": "not found"})
            return
        if not self.require_access():
            return
        query = parse_qs(parsed.query)
        kind = query.get("kind", [""])[0]
        work_id = query.get("work_id", [""])[0]
        if not kind and not work_id:
            with connect() as db:
                rows = db.execute(
                    "SELECT kind, work_id, episode_id, position, title, device_id, updated_at "
                    "FROM progress ORDER BY updated_at DESC"
                ).fetchall()
            self.send_json(200, {"progress": [dict(row) for row in rows]})
            return
        if kind not in ("novel", "webtoon") or not valid_id(work_id):
            self.send_json(400, {"error": "kind and work_id are required"})
            return
        with connect() as db:
            row = db.execute(
                "SELECT kind, work_id, episode_id, position, title, device_id, updated_at "
                "FROM progress WHERE kind=? AND work_id=?",
                (kind, work_id),
            ).fetchone()
        self.send_json(200, {"progress": dict(row) if row else None})

    def do_PUT(self):
        if urlparse(self.path).path != "/v1/progress":
            self.send_json(404, {"error": "not found"})
            return
        if not self.require_access():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("invalid content length")
            data = json.loads(self.rfile.read(length))
            kind, work_id, episode_id, position, title, device_id = validate_progress(data)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        updated_at = int(time.time() * 1000)
        with connect() as db:
            db.execute("""
                INSERT INTO progress
                    (kind, work_id, episode_id, position, title, device_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, work_id) DO UPDATE SET
                    episode_id=excluded.episode_id,
                    position=excluded.position,
                    title=excluded.title,
                    device_id=excluded.device_id,
                    updated_at=excluded.updated_at
            """, (kind, work_id, episode_id, position, title, device_id, updated_at))
        self.send_json(200, {"ok": True, "updated_at": updated_at})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path != "/v1/progress":
            self.send_json(404, {"error": "not found"})
            return
        if not self.require_access():
            return
        query = parse_qs(parsed.query)
        kind = query.get("kind", [""])[0]
        work_id = query.get("work_id", [""])[0]
        if kind not in ("novel", "webtoon") or not valid_id(work_id):
            self.send_json(400, {"error": "kind and work_id are required"})
            return
        with connect() as db:
            db.execute("DELETE FROM progress WHERE kind=? AND work_id=?", (kind, work_id))
        self.send_json(200, {"ok": True})


def self_test():
    assert validate_progress({
        "kind": "novel", "work_id": "60853", "episode_id": "6919020", "position": 0.5
    })[3] == 0.5
    for invalid in (
        {"kind": "other", "work_id": "1", "episode_id": "2", "position": 0},
        {"kind": "novel", "work_id": "", "episode_id": "2", "position": 0},
        {"kind": "novel", "work_id": "1", "episode_id": "2", "position": 2},
    ):
        try:
            validate_progress(invalid)
            raise AssertionError("invalid progress accepted")
        except ValueError:
            pass
    print("reader sync self-test passed")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        if not SYNC_TOKEN:
            raise SystemExit("SYNC_TOKEN is required")
        init_db()
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
