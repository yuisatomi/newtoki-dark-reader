"""Separate, cookie-authenticated novel reader mounted at /app."""

import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import reader_auth

from reader_crawl import (CrawlUnavailable, HumanVerificationRequired, LockedChapter,
                          checked_url, collect_episode_list, extract_episode, source_browser)


HTML_PATH = Path(__file__).with_name("reader_app.html")
WORK_PATH = re.compile(r"^/app/api/work/([0-9]+)$")
SOURCE_PATH = re.compile(r"^/app/api/work/([0-9]+)/source$")
REFRESH_PATH = re.compile(r"^/app/api/work/([0-9]+)/refresh$")
RESUME_PATH = re.compile(r"^/app/api/work/([0-9]+)/resume$")
EPISODE_PATH = re.compile(r"^/app/api/episode/([0-9]+)/([0-9]+)$")
RETRY_PATH = re.compile(r"^/app/api/episode/([0-9]+)/([0-9]+)/retry$")


def init_db(connect):
    reader_auth.init_db(connect)
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS reader_paused_sources (host TEXT PRIMARY KEY, error TEXT NOT NULL)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS reader_works (
                work_id TEXT PRIMARY KEY, host TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS reader_episodes (
                work_id TEXT NOT NULL, episode_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, title TEXT NOT NULL,
                source_url TEXT NOT NULL, paragraphs_json TEXT,
                state TEXT NOT NULL DEFAULT 'missing', error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (work_id, episode_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS reader_jobs (
                work_id TEXT NOT NULL, episode_id TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL,
                PRIMARY KEY (work_id, episode_id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS reader_episode_order ON reader_episodes(work_id, ordinal)")


def respond(handler, status, payload, cookie=None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    if cookie:
        handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if not 0 < length <= 16_384:
        raise ValueError("invalid content length")
    data = json.loads(handler.rfile.read(length))
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


def enqueue(db, work_id, episode_id="", force=False):
    now = int(time.time() * 1000)
    db.execute("""
        INSERT INTO reader_jobs(work_id,episode_id,state,updated_at) VALUES(?,?,'queued',?)
        ON CONFLICT(work_id,episode_id) DO UPDATE SET
          state=CASE WHEN ? AND reader_jobs.state!='running' THEN 'queued' ELSE reader_jobs.state END,
          error=CASE WHEN ? AND reader_jobs.state!='running' THEN '' ELSE reader_jobs.error END,
          updated_at=CASE WHEN ? AND reader_jobs.state!='running' THEN excluded.updated_at ELSE reader_jobs.updated_at END
    """, (work_id, episode_id, now, int(force), int(force), int(force)))


def queue_prefetch(db, work_id, episode_id):
    rows = db.execute("""
        SELECT episode_id,state FROM reader_episodes
        WHERE work_id=? AND ordinal >= (SELECT ordinal FROM reader_episodes WHERE work_id=? AND episode_id=?)
        ORDER BY ordinal LIMIT 3
    """, (work_id, work_id, episode_id)).fetchall()
    for row in rows:
        if row["state"] == "missing":
            enqueue(db, work_id, row["episode_id"])


def verification_message(db, work_id):
    row = db.execute("""SELECT p.error FROM reader_paused_sources p
        JOIN reader_works w ON w.host=p.host WHERE w.work_id=?""", (work_id,)).fetchone()
    return row["error"] if row else ""


def handle_get(handler, connect, token):
    path = urlparse(handler.path).path
    if path in ("/app", "/app/"):
        body = HTML_PATH.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.wfile.write(body)
        return
    if path == "/app/api/me":
        identity = reader_auth.session(handler, connect, token)
        owner = reader_auth.account(connect)
        respond(handler, 200, {"authenticated": bool(identity), "configured": bool(owner),
                              "username": owner['username'] if identity and owner else '',
                              "device_id": identity['id'] if identity else ''})
        return
    if not reader_auth.session(handler, connect, token):
        respond(handler, 401, {"error": "로그인이 필요합니다."})
        return
    with connect() as db:
        if path == '/app/api/devices':
            rows = db.execute('''SELECT id,kind,name,created_at,last_seen,expires_at FROM reader_credentials
                WHERE revoked=0 AND (expires_at=0 OR expires_at>?) ORDER BY created_at DESC''', (int(time.time()),)).fetchall()
            respond(handler, 200, {'devices': [dict(row) for row in rows]})
            return
        if path == '/app/api/devices/request':
            code = parse_qs(urlparse(handler.path).query).get('code', [''])[0].strip().upper()
            row = db.execute("SELECT user_code,name,expires_at,state FROM reader_pairings WHERE user_code=? AND expires_at>? AND state='pending'",
                             (code, int(time.time()))).fetchone()
            respond(handler, 200 if row else 404, {'request': dict(row)} if row else {'error': '만료되었거나 없는 연결 요청입니다.'})
            return
        if path == "/app/api/library":
            rows = db.execute("""
                SELECT p.work_id,p.episode_id,p.position,p.title,p.revision,
                       COALESCE(w.title,'') AS work_title,COALESCE(w.host,'') AS host,
                       COALESCE(e.title,'') AS episode_title,p.updated_at AS last_seen
                FROM progress p LEFT JOIN reader_works w ON w.work_id=p.work_id
                LEFT JOIN reader_episodes e ON e.work_id=p.work_id AND e.episode_id=p.episode_id
                WHERE p.kind='novel' AND p.deleted=0
                UNION ALL
                SELECT w.work_id,'',0,'',0,w.title,w.host,'',w.updated_at
                FROM reader_works w WHERE NOT EXISTS (
                    SELECT 1 FROM progress p WHERE p.kind='novel' AND p.work_id=w.work_id
                ) ORDER BY last_seen DESC
            """).fetchall()
            respond(handler, 200, {"works": [dict(row) for row in rows]})
            return
        match = WORK_PATH.fullmatch(path)
        if match:
            work_id = match.group(1)
            work = db.execute("SELECT * FROM reader_works WHERE work_id=?", (work_id,)).fetchone()
            progress = db.execute("SELECT episode_id,position,title,revision,deleted FROM progress WHERE kind='novel' AND work_id=?", (work_id,)).fetchone()
            episodes = db.execute("SELECT episode_id,ordinal,title,state,error FROM reader_episodes WHERE work_id=? ORDER BY ordinal", (work_id,)).fetchall()
            job = db.execute("SELECT state,error FROM reader_jobs WHERE work_id=? AND episode_id=''", (work_id,)).fetchone()
            respond(handler, 200, {"work": dict(work) if work else None,
                                   "progress": dict(progress) if progress else None,
                                   "episodes": [dict(row) for row in episodes],
                                   "list_job": dict(job) if job else None,
                                   "verification": verification_message(db, work_id)})
            return
        match = EPISODE_PATH.fullmatch(path)
        if match:
            work_id, episode_id = match.groups()
            row = db.execute("SELECT * FROM reader_episodes WHERE work_id=? AND episode_id=?", (work_id, episode_id)).fetchone()
            if not row:
                respond(handler, 404, {"error": "회차 목록을 먼저 불러오세요."})
                return
            queue_prefetch(db, work_id, episode_id)
            job = db.execute("SELECT state,error FROM reader_jobs WHERE work_id=? AND episode_id=?", (work_id, episode_id)).fetchone()
            result = dict(row)
            content = result.pop("paragraphs_json")
            result["paragraphs"] = json.loads(content) if content else None
            respond(handler, 200, {"episode": result, "job": dict(job) if job else None,
                                   "verification": verification_message(db, work_id)})
            return
    respond(handler, 404, {"error": "not found"})


def handle_post(handler, connect, token, store_progress):
    path = urlparse(handler.path).path
    try:
        data = read_json(handler)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        respond(handler, 400, {"error": str(exc)})
        return
    if handler.headers.get("X-Reader-App") != "1":
        respond(handler, 403, {"error": "same-origin request required"})
        return
    if path == "/app/api/login":
        status, payload, cookie = reader_auth.login(handler, connect, token, data)
        respond(handler, status, payload, cookie)
        return
    if not reader_auth.session(handler, connect, token):
        respond(handler, 401, {"error": "로그인이 필요합니다."})
        return
    managed = reader_auth.manage(handler, connect, path, data)
    if managed is not None:
        status, payload, cookie = managed
        respond(handler, status, payload, cookie)
        return
    if path == "/app/api/progress":
        data["kind"] = "novel"
        try:
            status, payload = store_progress(data)
        except (ValueError, TypeError) as exc:
            respond(handler, 400, {"error": str(exc)})
            return
        respond(handler, status, payload)
        return
    if path == "/app/api/works":
        try:
            host, work_id, episode_id = checked_url(str(data.get("url", "")))
        except ValueError as exc:
            respond(handler, 400, {"error": str(exc)})
            return
        with connect() as db:
            now = int(time.time() * 1000)
            db.execute("""UPDATE progress SET deleted=0,revision=revision+1,updated_at=?
                WHERE kind='novel' AND work_id=? AND deleted=1""", (now, work_id))
            db.execute("""
                INSERT INTO reader_works(work_id,host,title,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(work_id) DO UPDATE SET host=excluded.host,updated_at=excluded.updated_at
            """, (work_id, host, f"소설 {work_id}", now))
            enqueue(db, work_id, force=True)
        respond(handler, 200, {"work_id": work_id, "episode_id": episode_id})
        return
    match = SOURCE_PATH.fullmatch(path)
    if match:
        work_id = match.group(1)
        host = data.get("host")
        if host not in ("newtoki1.org", "toki32.com"):
            respond(handler, 400, {"error": "지원하는 출처를 선택하세요."})
            return
        with connect() as db:
            progress = db.execute("SELECT 1 FROM progress WHERE kind='novel' AND work_id=? AND deleted=0", (work_id,)).fetchone()
            if not progress:
                respond(handler, 404, {"error": "저장된 작품이 아닙니다."})
                return
            db.execute("""
                INSERT INTO reader_works(work_id,host,title,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(work_id) DO UPDATE SET host=excluded.host,updated_at=excluded.updated_at
            """, (work_id, host, f"소설 {work_id}", int(time.time() * 1000)))
            enqueue(db, work_id, force=True)
        respond(handler, 200, {"ok": True})
        return
    match = RESUME_PATH.fullmatch(path)
    if match:
        with connect() as db:
            work = db.execute("SELECT host FROM reader_works WHERE work_id=?", (match.group(1),)).fetchone()
            if not work or not work["host"]:
                respond(handler, 404, {"error": "출처를 먼저 선택하세요."})
                return
            db.execute("DELETE FROM reader_paused_sources WHERE host=?", (work["host"],))
            db.execute("""UPDATE reader_jobs SET state='queued',error=''
                WHERE state='verification' AND work_id IN (SELECT work_id FROM reader_works WHERE host=?)""",
                       (work["host"],))
            db.execute("""UPDATE reader_episodes SET state='missing',error=''
                WHERE state='verification' AND work_id IN (SELECT work_id FROM reader_works WHERE host=?)""",
                       (work["host"],))
        respond(handler, 200, {"ok": True})
        return
    match = REFRESH_PATH.fullmatch(path)
    if match:
        with connect() as db:
            work = db.execute("SELECT host FROM reader_works WHERE work_id=?", (match.group(1),)).fetchone()
            if not work or not work["host"]:
                respond(handler, 404, {"error": "출처를 먼저 선택하세요."})
                return
            enqueue(db, match.group(1), force=True)
        respond(handler, 200, {"ok": True})
        return
    match = RETRY_PATH.fullmatch(path)
    if match:
        work_id, episode_id = match.groups()
        with connect() as db:
            row = db.execute("SELECT state FROM reader_episodes WHERE work_id=? AND episode_id=?", (work_id, episode_id)).fetchone()
            if not row or row["state"] != "error":
                respond(handler, 409, {"error": "재시도할 수 없는 회차입니다."})
                return
            db.execute("UPDATE reader_episodes SET state='missing',error='' WHERE work_id=? AND episode_id=?", (work_id, episode_id))
            enqueue(db, work_id, episode_id, force=True)
        respond(handler, 200, {"ok": True})
        return
    respond(handler, 404, {"error": "not found"})


def browse_episode(url, work_id, episode_id):
    with source_browser() as browser:
        return extract_episode(browser, url, work_id, episode_id)


def run_one_job(connect):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT j.work_id,j.episode_id FROM reader_jobs j
            WHERE j.state='queued' AND NOT EXISTS (
                SELECT 1 FROM reader_works w JOIN reader_paused_sources p ON p.host=w.host
                WHERE w.work_id=j.work_id
            ) ORDER BY j.updated_at,j.rowid LIMIT 1""").fetchone()
        if not row:
            return False
        work_id, episode_id = row["work_id"], row["episode_id"]
        db.execute("UPDATE reader_jobs SET state='running',attempts=attempts+1,updated_at=? WHERE work_id=? AND episode_id=?",
                   (int(time.time() * 1000), work_id, episode_id))
        work = db.execute("SELECT host FROM reader_works WHERE work_id=?", (work_id,)).fetchone()
        episode = db.execute("SELECT source_url FROM reader_episodes WHERE work_id=? AND episode_id=?", (work_id, episode_id)).fetchone() if episode_id else None
    try:
        if not work or not work["host"]:
            raise CrawlUnavailable("작품 출처가 없습니다.")
        if not episode_id:
            title, chapters = collect_episode_list(f"https://{work['host']}/novel/{work_id}", work_id)
            with connect() as db:
                now = int(time.time() * 1000)
                db.execute("UPDATE reader_works SET title=?,updated_at=? WHERE work_id=?", (title, now, work_id))
                for ordinal, (chapter_id, chapter_title, url) in enumerate(chapters, 1):
                    db.execute("""
                        INSERT INTO reader_episodes(work_id,episode_id,ordinal,title,source_url,updated_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(work_id,episode_id) DO UPDATE SET
                        ordinal=excluded.ordinal,title=excluded.title,source_url=excluded.source_url
                    """, (work_id, chapter_id, ordinal, chapter_title, url, now))
        else:
            if not episode:
                raise CrawlUnavailable("목록에서 회차를 찾지 못했습니다.")
            paragraphs = browse_episode(episode["source_url"], work_id, episode_id)
            with connect() as db:
                db.execute("""
                    UPDATE reader_episodes SET paragraphs_json=?,state='ready',error='',updated_at=?
                    WHERE work_id=? AND episode_id=?
                """, (json.dumps(paragraphs, ensure_ascii=False), int(time.time() * 1000), work_id, episode_id))
        with connect() as db:
            db.execute("UPDATE reader_jobs SET state='done',error='',updated_at=? WHERE work_id=? AND episode_id=?",
                       (int(time.time() * 1000), work_id, episode_id))
    except HumanVerificationRequired as exc:
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO reader_paused_sources VALUES(?,?)", (work["host"], str(exc)))
            db.execute("UPDATE reader_jobs SET state='verification',error=? WHERE work_id=? AND episode_id=?",
                       (str(exc), work_id, episode_id))
            if episode_id:
                db.execute("UPDATE reader_episodes SET state='verification',error=? WHERE work_id=? AND episode_id=?",
                           (str(exc), work_id, episode_id))
    except LockedChapter as exc:
        with connect() as db:
            db.execute("UPDATE reader_episodes SET state='locked',error=? WHERE work_id=? AND episode_id=?", (str(exc), work_id, episode_id))
            db.execute("UPDATE reader_jobs SET state='done',error=? WHERE work_id=? AND episode_id=?", (str(exc), work_id, episode_id))
    except Exception as exc:
        with connect() as db:
            message = str(exc)[:500]
            if episode_id:
                db.execute("UPDATE reader_episodes SET state='error',error=? WHERE work_id=? AND episode_id=?", (message, work_id, episode_id))
            db.execute("UPDATE reader_jobs SET state='error',error=?,updated_at=? WHERE work_id=? AND episode_id=?",
                       (message, int(time.time() * 1000), work_id, episode_id))
    return True


def start_worker(connect):
    with connect() as db:
        db.execute("UPDATE reader_jobs SET state='queued' WHERE state='running'")

    def loop():
        while True:
            try:
                if not run_one_job(connect):
                    time.sleep(1)
            except Exception:
                logging.exception("reader crawler worker failed")
                time.sleep(2)

    threading.Thread(target=loop, name="reader-crawler", daemon=True).start()
