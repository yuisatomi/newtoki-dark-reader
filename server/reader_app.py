"""Separate, cookie-authenticated novel reader mounted at /app."""

import json
import logging
import re
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import reader_auth
import reader_openai
import reader_translate
import reader_usage
import reader_verification

from reader_crawl import (CrawlUnavailable, HumanVerificationRequired, LockedChapter,
                          HOSTS, SHUBA_HOSTS, WORK_ID_PATTERN, checked_host, supported_host, checked_url,
                          collect_episode_list, extract_episode, source_browser, shuba_work, valid_work_id, work_url,
                          ready_verification_pages, chronological_shuba_chapters)


HTML_PATH = Path(__file__).with_name("reader_app.html")
WORK_PATH = re.compile(rf"^/app/api/work/({WORK_ID_PATTERN})$")
SOURCE_PATH = re.compile(rf"^/app/api/work/({WORK_ID_PATTERN})/source$")
REFRESH_PATH = re.compile(rf"^/app/api/work/({WORK_ID_PATTERN})/refresh$")
RESUME_PATH = re.compile(rf"^/app/api/work/({WORK_ID_PATTERN})/resume$")
VERIFICATION_PATH = re.compile(rf"^/app/api/work/({WORK_ID_PATTERN})/verification$")
EPISODE_PATH = re.compile(rf"^/app/api/episode/({WORK_ID_PATTERN})/([0-9]{{1,128}})$")
RETRY_PATH = re.compile(rf"^/app/api/episode/({WORK_ID_PATTERN})/([0-9]{{1,128}})/retry$")
TRANSLATE_PATH = re.compile(rf"^/app/api/episode/({WORK_ID_PATTERN})/([0-9]{{1,128}})/translate$")
BACKUP_LIMIT = 32 * 1024 * 1024


def memory_usage():
    info = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            info[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    def counter(name):
        try:
            value = int((Path('/sys/fs/cgroup') / name).read_text().strip())
            return value if value >= 0 else None
        except (OSError, ValueError):
            return None  # "max" is not a numeric limit.
    total, available = info.get('MemTotal'), info.get('MemAvailable')
    current, limit = counter('memory.current'), counter('memory.max')
    used = max(0, total - available) if total and available is not None else None
    source = 'meminfo'
    if current is not None and limit and (not total or limit <= total):
        total, used, available, source = limit, current, max(0, limit-current), 'cgroup'
    swap_total, swap_free = info.get('SwapTotal'), info.get('SwapFree')
    swap_used = max(0, swap_total-swap_free) if swap_total is not None and swap_free is not None else None
    swap_current, swap_limit = counter('memory.swap.current'), counter('memory.swap.max')
    if swap_current is not None and swap_limit is not None and (swap_total is None or swap_limit <= swap_total):
        swap_total, swap_used = swap_limit, swap_current
    return {'total': total, 'used': used, 'available': available, 'source': source,
            'cache_inclusive': current, 'swap_total': swap_total, 'swap_used': swap_used}


def resource_snapshot(db):
    result = {'sampled_at': int(time.time()*1000), 'memory': memory_usage(), 'disk': None, 'database': None}
    # Use the actual connected DB location, not a second environment/default path.
    database = next((row[2] for row in db.execute('PRAGMA database_list') if row[1]=='main'), '')
    if not database:
        return result
    path = Path(database)
    try:
        usage = shutil.disk_usage(path.parent)
        result['disk'] = {'total': usage.total, 'used': usage.used, 'free': usage.free}
    except OSError:
        pass
    def file_size(file, optional=False):
        try:
            return file.stat().st_size
        except FileNotFoundError:
            return 0 if optional else None
        except OSError:
            return None
    sizes = {'main': file_size(path)}
    sizes.update({suffix: file_size(Path(str(path)+'-'+suffix), True) for suffix in ('wal','shm','journal')})
    sizes['total'] = sum(sizes.values()) if all(v is not None for v in sizes.values()) else None
    try:
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        sizes['reusable'] = db.execute('PRAGMA freelist_count').fetchone()[0] * page_size
    except sqlite3.Error:
        sizes['reusable'] = None
    result['database'] = sizes
    return result


def init_db(connect):
    reader_auth.init_db(connect)
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS reader_paused_sources (host TEXT PRIMARY KEY, error TEXT NOT NULL)")
        db.execute('CREATE TABLE IF NOT EXISTS reader_sources (host TEXT PRIMARY KEY)')
        db.execute("""CREATE TABLE IF NOT EXISTS reader_work_settings (
            work_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
            prefetch INTEGER NOT NULL DEFAULT 2, cache_limit_mb INTEGER NOT NULL DEFAULT 0,
            translation_prefetch INTEGER NOT NULL DEFAULT 1,
            translation_anchor TEXT NOT NULL DEFAULT '', glossary_json TEXT NOT NULL DEFAULT '{}'
        )""")
        settings_columns = {row[1] for row in db.execute('PRAGMA table_info(reader_work_settings)')}
        for column, declaration in (('translation_prefetch', 'INTEGER NOT NULL DEFAULT 1'),
                                    ('translation_anchor', "TEXT NOT NULL DEFAULT ''"),
                                    ('glossary_json', "TEXT NOT NULL DEFAULT '{}'")):
            if column not in settings_columns:
                db.execute(f'ALTER TABLE reader_work_settings ADD COLUMN {column} {declaration}')
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
        if 'run_token' not in {row[1] for row in db.execute('PRAGMA table_info(reader_jobs)')}:
            db.execute("ALTER TABLE reader_jobs ADD COLUMN run_token TEXT NOT NULL DEFAULT ''")
        repair_shuba_order(db)
    reader_translate.init_db(connect)


def repair_shuba_order(db, work_id=None):
    works = [work_id] if work_id else [row[0] for row in db.execute(
        "SELECT DISTINCT work_id FROM reader_episodes WHERE work_id LIKE '69shuba-%'")]
    changed = 0
    for key in works:
        if not shuba_work(key):
            continue
        chapters = [tuple(row) for row in db.execute(
            'SELECT episode_id,title,source_url FROM reader_episodes WHERE work_id=? ORDER BY ordinal', (key,))]
        ordered = chronological_shuba_chapters(chapters)
        if ordered != chapters:
            changed += db.executemany('UPDATE reader_episodes SET ordinal=? WHERE work_id=? AND episode_id=? AND ordinal!=?',
                                      [(ordinal, key, chapter[0], ordinal) for ordinal, chapter in enumerate(ordered, 1)]).rowcount
    return changed


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


def read_json(handler, limit=16_384):
    length = int(handler.headers.get("Content-Length", "0"))
    if not 0 < length <= limit:
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
          attempts=CASE WHEN ? AND reader_jobs.state!='running' THEN 0 ELSE reader_jobs.attempts END,
          updated_at=CASE WHEN ? AND reader_jobs.state!='running' THEN excluded.updated_at ELSE reader_jobs.updated_at END
    """, (work_id, episode_id, now, int(force), int(force), int(force), int(force)))


def queue_prefetch(db, work_id, episode_id):
    settings = work_settings(db, work_id)
    if settings['paused']:
        return
    rows = db.execute("""
        SELECT episode_id,state FROM reader_episodes
        WHERE work_id=? AND ordinal >= (SELECT ordinal FROM reader_episodes WHERE work_id=? AND episode_id=?)
        ORDER BY ordinal LIMIT ?
    """, (work_id, work_id, episode_id, settings['prefetch'] + 1)).fetchall()
    for row in rows:
        if row["state"] == "missing":
            enqueue(db, work_id, row["episode_id"])


def queue_translation_prefetch(db):
    """Caller holds a write transaction; only explicit reading moves the window."""
    desired = set()
    windows = db.execute("""SELECT s.work_id,s.translation_anchor,s.translation_prefetch
        FROM reader_work_settings s JOIN reader_works w ON w.work_id=s.work_id
        WHERE s.paused=0 AND s.translation_prefetch>0 AND s.translation_anchor!=''
        AND EXISTS (SELECT 1 FROM reader_translations t
            WHERE t.work_id=s.work_id AND t.episode_id=s.translation_anchor
            AND t.model=? AND t.language=? AND t.prompt_version=? AND t.state IN ('queued','running','ready'))""",
                         (reader_translate.MODEL, reader_translate.LANGUAGE, reader_translate.PROMPT_VERSION)).fetchall()
    for window in windows:
        work_id = window['work_id']
        if not shuba_work(work_id):
            continue
        episodes = db.execute("""SELECT * FROM reader_episodes WHERE work_id=? AND ordinal>(
            SELECT ordinal FROM reader_episodes WHERE work_id=? AND episode_id=?)
            ORDER BY ordinal LIMIT ?""", (work_id, work_id, window['translation_anchor'], window['translation_prefetch'])).fetchall()
        for episode in episodes:
            desired.add((work_id, episode['episode_id']))
            if episode['state'] == 'missing':
                enqueue(db, work_id, episode['episode_id'])
            elif episode['state'] == 'ready' and episode['paragraphs_json']:
                # Existing failures are not automatically retried; cached results remain reusable.
                if not db.execute('SELECT 1 FROM reader_translations WHERE ' + reader_translate.KEY,
                                  reader_translate.episode_key(episode)).fetchone():
                    reader_translate.enqueue(db, episode, prefetch=True)
    for job in db.execute("SELECT work_id,episode_id FROM reader_translations WHERE prefetch=1 AND state IN ('queued','running')").fetchall():
        if (job['work_id'], job['episode_id']) not in desired:
            db.execute("DELETE FROM reader_translations WHERE work_id=? AND episode_id=? AND prefetch=1 AND state IN ('queued','running')",
                       (job['work_id'], job['episode_id']))


def set_work_source(db, work_id, host):
    """Caller holds a write transaction; discard results from the previous source."""
    work_url(host, work_id)
    previous = db.execute('SELECT host FROM reader_works WHERE work_id=?', (work_id,)).fetchone()
    db.execute('INSERT OR IGNORE INTO reader_sources(host) VALUES(?)', (host,))
    changed = previous is None or previous['host'] != host
    now = int(time.time() * 1000)
    db.execute('''INSERT INTO reader_works(work_id,host,title,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(work_id) DO UPDATE SET host=excluded.host,updated_at=excluded.updated_at''',
               (work_id, host, f'소설 {work_id}', now))
    if changed:
        pending = db.execute("""SELECT j.episode_id FROM reader_jobs j JOIN reader_episodes e
            ON e.work_id=j.work_id AND e.episode_id=j.episode_id
            WHERE j.work_id=? AND e.state!='ready'""", (work_id,)).fetchall()
        db.execute('DELETE FROM reader_jobs WHERE work_id=?', (work_id,))
        db.execute("""UPDATE reader_episodes SET source_url=?||episode_id,
            state=CASE WHEN state='ready' THEN state ELSE 'missing' END,error=''
                    WHERE work_id=?""", (work_url(host, work_id, '0')[:-1], work_id))
        enqueue(db, work_id, force=True)
        for row in pending:
            enqueue(db, work_id, row['episode_id'])


def source_hosts(db, work_id=None):
    rows = db.execute("SELECT host FROM reader_sources UNION SELECT host FROM reader_works WHERE host!=''")
    hosts = HOSTS | {row[0] for row in rows if supported_host(row[0])}
    return sorted(host for host in hosts if work_id is None or (host in SHUBA_HOSTS) == shuba_work(work_id))


def work_settings(db, work_id):
    row = db.execute('SELECT paused,prefetch,cache_limit_mb,translation_prefetch FROM reader_work_settings WHERE work_id=?', (work_id,)).fetchone()
    return dict(row) if row else {'paused': 0, 'prefetch': 2, 'cache_limit_mb': 0, 'translation_prefetch': 1}


def checked_settings(data):
    if not isinstance(data, dict):
        raise ValueError('수집 설정이 올바르지 않습니다.')
    values = {}
    for key, default, maximum in (('paused', 0, 1), ('prefetch', 2, 200), ('cache_limit_mb', 0, 1024), ('translation_prefetch', 1, 200)):
        value = data.get(key, default)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError('수집 설정 범위를 확인하세요.')
        values[key] = value
    return values


def save_settings(db, work_id, settings):
    db.execute('''INSERT INTO reader_work_settings(work_id,paused,prefetch,cache_limit_mb,translation_prefetch) VALUES(?,?,?,?,?)
        ON CONFLICT(work_id) DO UPDATE SET paused=excluded.paused,prefetch=excluded.prefetch,
        cache_limit_mb=excluded.cache_limit_mb,translation_prefetch=excluded.translation_prefetch''',
               (work_id, settings['paused'], settings['prefetch'], settings['cache_limit_mb'],
                settings.get('translation_prefetch', work_settings(db, work_id)['translation_prefetch'])))


def cache_bytes(db, work_id):
    return db.execute('SELECT COALESCE(sum(length(CAST(paragraphs_json AS BLOB))),0) FROM reader_episodes WHERE work_id=?',
                      (work_id,)).fetchone()[0] + reader_translate.stored_bytes(db, work_id)


def export_work(db, work_id):
    if cache_bytes(db, work_id) > BACKUP_LIMIT - 1024:
        raise ValueError('앱 백업은 작품당 32 MB까지 지원합니다. 큰 작품은 서버 DB 백업을 이용하세요.')
    work = db.execute('SELECT work_id,host,title FROM reader_works WHERE work_id=?', (work_id,)).fetchone()
    progress = db.execute("SELECT episode_id,position,title FROM progress WHERE kind='novel' AND work_id=? AND deleted=0 AND episode_id!=''", (work_id,)).fetchone()
    if not work and not progress:
        raise ValueError('백업할 작품이 없습니다.')
    episodes = []
    for row in db.execute('SELECT episode_id,ordinal,title,source_url,paragraphs_json FROM reader_episodes WHERE work_id=? ORDER BY ordinal', (work_id,)):
        item = dict(row)
        content = item.pop('paragraphs_json')
        item['paragraphs'] = json.loads(content) if content else None
        episodes.append(item)
    result = {'format': 'reader-work', 'version': 1,
              'work': dict(work) if work else {'work_id': work_id, 'host': '', 'title': progress['title']},
              'settings': work_settings(db, work_id), 'progress': dict(progress) if progress else None, 'episodes': episodes}
    if shuba_work(work_id):
        result.update(version=2, translations=reader_translate.export_ready(db, work_id),
                      glossary=reader_translate.glossary(db, work_id))
    if len(json.dumps({'backup': result, 'confirm': True}, ensure_ascii=False).encode()) > BACKUP_LIMIT - 1024:
        raise ValueError('앱 백업은 작품당 32 MB까지 지원합니다. 큰 작품은 서버 DB 백업을 이용하세요.')
    return result


def checked_backup(data):
    if not isinstance(data, dict) or data.get('format') != 'reader-work' or type(data.get('version')) is not int or data['version'] not in (1, 2):
        raise ValueError('지원하지 않는 백업 파일입니다.')
    work = data.get('work')
    if not isinstance(work, dict) or not valid_work_id(work.get('work_id')):
        raise ValueError('작품 ID가 올바르지 않습니다.')
    work_id = work['work_id']
    host = work.get('host')
    if host != '' and not supported_host(host):
        raise ValueError('지원하지 않는 작품 출처입니다.')
    if (data['version'] == 2) != shuba_work(work_id) or (shuba_work(work_id) and not host):
        raise ValueError('백업 버전과 작품 출처가 일치하지 않습니다.')
    if host:
        work_url(host, work_id)
    def title(value):
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError('제목이 올바르지 않습니다.')
        return value
    result = {'work': {'work_id': work_id, 'host': host, 'title': title(work.get('title'))},
              'settings': checked_settings(data.get('settings', {})), 'episodes': [], 'progress': None}
    episodes = data.get('episodes')
    if not isinstance(episodes, list) or len(episodes) > 30000:
        raise ValueError('회차 목록이 올바르지 않습니다.')
    seen, ordinals = set(), set()
    for item in episodes:
        if not isinstance(item, dict):
            raise ValueError('회차 데이터가 올바르지 않습니다.')
        eid, ordinal, url, paragraphs = (item.get(k) for k in ('episode_id', 'ordinal', 'source_url', 'paragraphs'))
        if not isinstance(eid, str) or not re.fullmatch(r'[0-9]{1,128}', eid) or eid in seen:
            raise ValueError('중복되거나 잘못된 회차 ID입니다.')
        if type(ordinal) is not int or not 1 <= ordinal <= 1000000 or ordinal in ordinals:
            raise ValueError('회차 순서가 올바르지 않습니다.')
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError('회차 주소가 올바르지 않습니다.')
        _, url_work, url_episode = checked_url(url)
        if (url_work, url_episode) != (work_id, eid):
            raise ValueError('회차 주소와 작품 ID가 다릅니다.')
        if paragraphs is not None and (not isinstance(paragraphs, list) or not paragraphs
                or not all(isinstance(p, str) for p in paragraphs)
                or (data['version'] == 2 and not all(p.strip() for p in paragraphs))):
            raise ValueError('본문은 문자열 문단 목록이어야 합니다.')
        result['episodes'].append(dict(episode_id=eid, ordinal=ordinal, title=title(item.get('title')), source_url=url, paragraphs=paragraphs))
        seen.add(eid)
        ordinals.add(ordinal)
    progress = data.get('progress')
    if progress is not None:
        if not isinstance(progress, dict):
            raise ValueError('읽기 기록이 올바르지 않습니다.')
        eid, position = progress.get('episode_id'), progress.get('position')
        if not isinstance(eid, str) or not re.fullmatch(r'[0-9]{1,128}', eid) or type(position) not in (int, float) or not 0 <= position <= 1:
            raise ValueError('읽기 위치가 올바르지 않습니다.')
        result['progress'] = dict(episode_id=eid, position=position, title=title(progress.get('title')))
    result['translations'] = reader_translate.checked_backup(data.get('translations', []), result['episodes']) if data['version'] == 2 else []
    result['glossary'] = reader_translate.checked_glossary(data.get('glossary', {})) if data['version'] == 2 else {}
    return result


def verification_message(db, work_id):
    row = db.execute("""SELECT p.error FROM reader_paused_sources p
        JOIN reader_works w ON w.host=p.host WHERE w.work_id=?""", (work_id,)).fetchone()
    return row["error"] if row else ""


def resume_source(db, host):
    db.execute('DELETE FROM reader_paused_sources WHERE host=?', (host,))
    db.execute("""UPDATE reader_jobs SET state='queued',error='',attempts=0,updated_at=?
        WHERE state='verification' AND work_id IN (SELECT work_id FROM reader_works WHERE host=?)""",
               (int(time.time() * 1000), host))
    db.execute("""UPDATE reader_episodes SET state='missing',error=''
        WHERE state='verification' AND work_id IN (SELECT work_id FROM reader_works WHERE host=?)""", (host,))


def resume_completed_verifications(connect):
    with connect() as db:
        waiting = db.execute("""SELECT w.host,j.work_id,j.episode_id,j.run_token FROM reader_jobs j
            JOIN reader_works w ON w.work_id=j.work_id JOIN reader_paused_sources p ON p.host=w.host
            WHERE j.state='verification' AND NOT EXISTS (
                SELECT 1 FROM reader_work_settings s WHERE s.work_id=j.work_id AND s.paused=1)""").fetchall()
    if not waiting:
        return
    ready = ready_verification_pages({row['host'] for row in waiting})
    if not ready:
        return
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in waiting:
            host, work_id, episode_id, token = row['host'], row['work_id'], row['episode_id'], row['run_token']
            if (host, work_id, episode_id) not in ready:
                continue
            current = db.execute("""SELECT 1 FROM reader_jobs j JOIN reader_works w ON w.work_id=j.work_id
                WHERE j.work_id=? AND j.episode_id=? AND j.run_token=? AND j.state='verification' AND w.host=?
                AND NOT EXISTS (SELECT 1 FROM reader_work_settings s WHERE s.work_id=j.work_id AND s.paused=1)""",
                                 (work_id, episode_id, token, host)).fetchone()
            if current:
                resume_source(db, host)
                # Save the loaded page before older prefetch jobs navigate its tab away.
                db.execute("UPDATE reader_jobs SET updated_at=0 WHERE work_id=? AND episode_id=?", (work_id, episode_id))


def handle_get(handler, connect, token):
    path = urlparse(handler.path).path
    if path in ("/app", "/app/", "/app/manage"):
        body = HTML_PATH.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
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
    if path == '/app/api/translation/account':
        respond(handler, 200, reader_openai.account_status())
        return
    with connect() as db:
        if path == '/app/api/manage/resources':
            db.execute('BEGIN')
            resources = resource_snapshot(db)
            resources['translation_usage'] = reader_usage.snapshot(db)
            respond(handler, 200, resources)
            return
        if path == '/app/api/manage/backup':
            work_id = parse_qs(urlparse(handler.path).query).get('work_id', [''])[0]
            try:
                # One transaction keeps the position, list and body snapshot consistent.
                db.execute('BEGIN')
                backup = export_work(db, work_id)
            except ValueError as exc:
                respond(handler, 400, {'error': str(exc)})
                return
            respond(handler, 200, backup)
            return
        if path == '/app/api/manage':
            rows = db.execute("""WITH works AS (
                SELECT work_id FROM reader_works UNION SELECT work_id FROM reader_episodes
                UNION SELECT work_id FROM reader_jobs
                UNION SELECT work_id FROM progress WHERE kind='novel' AND deleted=0
            ), cache AS (
                SELECT work_id, count(*) AS total, sum(state='ready') AS ready,
                  sum(state='error') AS failed, sum(state='locked') AS locked,
                  sum(length(CAST(paragraphs_json AS BLOB))) AS bytes
                FROM reader_episodes GROUP BY work_id
            ), jobs AS (
                SELECT work_id,sum(state IN ('queued','running')) AS pending,
                  sum(state='verification') AS verification, sum(state='error') AS errors,
                  sum(state='capacity') AS capacity
                FROM reader_jobs GROUP BY work_id
            ) SELECT k.work_id,COALESCE(NULLIF(NULLIF(w.title,''),'소설 '||k.work_id),NULLIF(p.title,''),'소설 '||k.work_id) AS title,
                COALESCE(w.host,'') AS host,COALESCE(p.episode_id,'') AS episode_id,
                COALESCE(e.title,'') AS episode_title,COALESCE(p.position,0) AS position,
                COALESCE(c.total,0) AS total,COALESCE(c.ready,0) AS ready,COALESCE(c.failed,0) AS failed,
                COALESCE(c.locked,0) AS locked,COALESCE(c.bytes,0) AS bytes,
                COALESCE(j.pending,0) AS pending,COALESCE(j.verification,0) AS verification,
                COALESCE(j.errors,0) AS errors,COALESCE(j.capacity,0) AS capacity,
                COALESCE(s.paused,0) AS paused,COALESCE(s.prefetch,2) AS prefetch,
                COALESCE(s.translation_prefetch,1) AS translation_prefetch,
                COALESCE(s.cache_limit_mb,0) AS cache_limit_mb
                FROM works k LEFT JOIN reader_works w ON w.work_id=k.work_id
                LEFT JOIN progress p ON p.kind='novel' AND p.work_id=k.work_id AND p.deleted=0
                LEFT JOIN reader_episodes e ON e.work_id=k.work_id AND e.episode_id=p.episode_id
                LEFT JOIN cache c ON c.work_id=k.work_id LEFT JOIN jobs j ON j.work_id=k.work_id
                LEFT JOIN reader_work_settings s ON s.work_id=k.work_id
                ORDER BY title,k.work_id""").fetchall()
            works = [dict(row) for row in rows]
            for item in works:
                item['sources'] = source_hosts(db, item['work_id'])
                item['translated'] = db.execute("SELECT count(DISTINCT episode_id) FROM reader_translations WHERE work_id=? AND state='ready'", (item['work_id'],)).fetchone()[0]
                item['translation_bytes'] = reader_translate.stored_bytes(db, item['work_id'])
                item['bytes'] += item['translation_bytes']
                if shuba_work(item['work_id']):
                    item['glossary'] = reader_translate.glossary(db, item['work_id'])
            respond(handler, 200, {'works': works, 'sources': source_hosts(db)})
            return
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
                                   "sources": source_hosts(db, work_id),
                                   "progress": dict(progress) if progress else None,
                                   "episodes": [dict(row) for row in episodes],
                                   "list_job": dict(job) if job else None,
                                   "paused": bool(work_settings(db, work_id)['paused']),
                                   "verification": verification_message(db, work_id)})
            return
        match = EPISODE_PATH.fullmatch(path)
        if match:
            work_id, episode_id = match.groups()
            language = parse_qs(urlparse(handler.path).query).get('language', ['ko'])[0]
            if language not in ('ko', 'zh'):
                respond(handler, 400, {'error': '지원하지 않는 읽기 언어입니다.'})
                return
            row = db.execute("SELECT * FROM reader_episodes WHERE work_id=? AND episode_id=?", (work_id, episode_id)).fetchone()
            if not row:
                respond(handler, 404, {"error": "회차 목록을 먼저 불러오세요."})
                return
            queue_prefetch(db, work_id, episode_id)
            job = db.execute("SELECT state,error FROM reader_jobs WHERE work_id=? AND episode_id=?", (work_id, episode_id)).fetchone()
            result = dict(row)
            content = result.pop("paragraphs_json")
            result["paragraphs"] = json.loads(content) if content else None
            translation = reader_translate.status(db, row) if shuba_work(work_id) else None
            if translation is not None and language == 'ko':
                result['paragraphs'] = translation.pop('paragraphs', None)
            elif translation is not None:
                translation.pop('paragraphs', None)
            respond(handler, 200, {"episode": result, "job": dict(job) if job else None,
                                   "translation": translation, "language": language,
                                   "paused": bool(work_settings(db, work_id)['paused']),
                                   "verification": verification_message(db, work_id)})
            return
    respond(handler, 404, {"error": "not found"})


def handle_post(handler, connect, token, store_progress, delete_progress):
    path = urlparse(handler.path).path
    # Authenticate before accepting a larger backup upload.
    if path == '/app/api/manage/restore':
        if handler.headers.get('X-Reader-App') != '1':
            respond(handler, 403, {'error': 'same-origin request required'})
            return
        if not reader_auth.session(handler, connect, token):
            respond(handler, 401, {'error': '로그인이 필요합니다.'})
            return
    try:
        data = read_json(handler, BACKUP_LIMIT if path == '/app/api/manage/restore' else
                         65_536 if path == '/app/api/manage/glossary' else 16_384)
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
    match = VERIFICATION_PATH.fullmatch(path)
    if match:
        try:
            result = reader_verification.operate(connect, match.group(1), reader_auth.digest(reader_auth.session_value(handler)), data)
            if result['state'] == 'ready':
                resume_completed_verifications(connect)
        except reader_verification.VerificationError as error:
            respond(handler, error.status, {'error': str(error)})
            return
        respond(handler, 200, result)
        return
    if path == '/app/api/translation/disconnect':
        try:
            message = reader_openai.disconnect()
        except reader_openai.OpenAIError as error:
            respond(handler, 400, {'error': str(error)})
            return
        with connect() as db:
            db.execute("UPDATE reader_translations SET state='error',error='ChatGPT 연결이 해제되었습니다.',code='auth',run_token='' WHERE state IN ('queued','running')")
            db.execute("UPDATE reader_work_settings SET translation_anchor=''")
        respond(handler, 200, {'ok': True, 'message': message})
        return
    match = TRANSLATE_PATH.fullmatch(path)
    if match:
        work_id, episode_id = match.groups()
        if not shuba_work(work_id) or type(data.get('retry', False)) is not bool:
            respond(handler, 400, {'error': '지원하지 않는 번역 요청입니다.'})
            return
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            episode = db.execute('SELECT * FROM reader_episodes WHERE work_id=? AND episode_id=?', (work_id, episode_id)).fetchone()
            if not episode:
                respond(handler, 404, {'error': '회차 목록을 먼저 불러오세요.'})
                return
            try:
                result = reader_translate.enqueue(db, episode, data.get('retry', False))
            except ValueError as error:
                respond(handler, 409, {'error': str(error)})
                return
            db.execute('''INSERT INTO reader_work_settings(work_id,translation_anchor) VALUES(?,?)
                ON CONFLICT(work_id) DO UPDATE SET translation_anchor=excluded.translation_anchor''', (work_id, episode_id))
            queue_translation_prefetch(db)
            result.pop('paragraphs', None)
            respond(handler, 200, {'translation': result})
        return
    if path == '/app/api/manage/sources':
        try:
            host = checked_host(data.get('host'))
        except ValueError as exc:
            respond(handler, 400, {'error': str(exc)})
            return
        with connect() as db:
            db.execute('INSERT OR IGNORE INTO reader_sources(host) VALUES(?)', (host,))
        respond(handler, 200, {'ok': True, 'host': host})
        return
    if path == '/app/api/manage/glossary':
        work_id = data.get('work_id')
        try:
            if not isinstance(work_id, str) or not valid_work_id(work_id) or not shuba_work(work_id):
                raise ValueError('중국 소설의 작품 ID가 필요합니다.')
            terms = reader_translate.checked_glossary(data.get('glossary'))
        except ValueError as exc:
            respond(handler, 400, {'error': str(exc)})
            return
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM reader_works WHERE work_id=?', (work_id,)).fetchone():
                respond(handler, 404, {'error': '등록된 작품이 아닙니다.'})
                return
            reader_translate.save_glossary(db, work_id, terms)
        respond(handler, 200, {'ok': True})
        return
    if path == '/app/api/manage/restore':
        try:
            if data.get('confirm') is not True:
                raise ValueError('작품 데이터를 교체하려면 복원 확인이 필요합니다.')
            backup = checked_backup(data.get('backup'))
        except (ValueError, TypeError) as exc:
            respond(handler, 400, {'error': str(exc)})
            return
        work = backup['work']
        work_id, now = work['work_id'], int(time.time() * 1000)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('reader_translations', 'reader_jobs', 'reader_episodes'):
                db.execute(f'DELETE FROM {table} WHERE work_id=?', (work_id,))
            db.execute('''INSERT INTO reader_works(work_id,host,title,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(work_id) DO UPDATE SET host=excluded.host,title=excluded.title,updated_at=excluded.updated_at''',
                       (work_id, work['host'], work['title'], now))
            backup['settings']['paused'] = 1  # Review restored data before starting new collection.
            save_settings(db, work_id, backup['settings'])
            db.execute("UPDATE reader_work_settings SET translation_anchor='' WHERE work_id=?", (work_id,))
            for episode in backup['episodes']:
                paragraphs = episode['paragraphs']
                db.execute('''INSERT INTO reader_episodes(work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (work_id, episode['episode_id'], episode['ordinal'], episode['title'],
                    episode['source_url'], json.dumps(paragraphs, ensure_ascii=False) if paragraphs else None,
                    'ready' if paragraphs else 'missing', now))
            repair_shuba_order(db, work_id)
            reader_translate.restore(db, work_id, backup['translations'])
            if shuba_work(work_id):
                reader_translate.save_glossary(db, work_id, backup['glossary'])
            # Bump the current server revision, never reuse the backup's old revision.
            delete_progress(db, 'novel', work_id, clear=True)
            if backup['progress']:
                p = backup['progress']
                db.execute("""UPDATE progress SET episode_id=?,position=?,title=?,device_id='app-restore',deleted=0
                    WHERE kind='novel' AND work_id=?""", (p['episode_id'], p['position'], p['title'][:300], work_id))
            else:
                db.execute("UPDATE progress SET title=?,deleted=0 WHERE kind='novel' AND work_id=?", (work['title'][:300], work_id))
        respond(handler, 200, {'ok': True, 'work_id': work_id})
        return
    if path in ('/app/api/manage/cache-clear', '/app/api/manage/retry', '/app/api/manage/settings'):
        work_id = data.get('work_id')
        if not valid_work_id(work_id):
            respond(handler, 400, {'error': '작품 ID가 필요합니다.'})
            return
        try:
            settings = checked_settings(data.get('settings')) if path.endswith('/settings') else None
            if path.endswith('/cache-clear') and data.get('confirm') is not True:
                raise ValueError('본문 삭제 확인이 필요합니다.')
        except ValueError as exc:
            respond(handler, 400, {'error': str(exc)})
            return
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM reader_works WHERE work_id=?', (work_id,)).fetchone():
                respond(handler, 404, {'error': '출처와 회차 목록을 먼저 등록하세요.'})
                return
            if settings is not None:
                if 'translation_prefetch' not in data['settings']:
                    settings['translation_prefetch'] = work_settings(db, work_id)['translation_prefetch']
                save_settings(db, work_id, settings)
                queue_translation_prefetch(db)
                db.execute("UPDATE reader_jobs SET state='queued',attempts=0,error='',updated_at=? WHERE work_id=? AND state='capacity'",
                           (int(time.time() * 1000), work_id))
            elif path.endswith('/cache-clear'):
                # Invalidate running body jobs too; their late results must not refill the cache.
                db.execute("DELETE FROM reader_jobs WHERE work_id=? AND episode_id!=''", (work_id,))
                db.execute('DELETE FROM reader_translations WHERE work_id=?', (work_id,))
                db.execute("UPDATE reader_episodes SET paragraphs_json=NULL,state=CASE WHEN state='ready' THEN 'missing' ELSE state END WHERE work_id=?", (work_id,))
                settings = work_settings(db, work_id)
                settings['paused'] = 1
                save_settings(db, work_id, settings)
                db.execute("UPDATE reader_work_settings SET translation_anchor='' WHERE work_id=?", (work_id,))
            else:
                rows = db.execute("""SELECT episode_id FROM reader_jobs WHERE work_id=? AND state='error'
                    UNION SELECT episode_id FROM reader_episodes WHERE work_id=? AND state='error'""", (work_id, work_id)).fetchall()
                for row in rows:
                    enqueue(db, work_id, row['episode_id'], force=True)
                db.execute("UPDATE reader_episodes SET state='missing',error='' WHERE work_id=? AND state='error'", (work_id,))
        respond(handler, 200, {'ok': True})
        return
    if path == '/app/api/manage/delete':
        work_id = data.get('work_id')
        if not isinstance(work_id, str) or not re.fullmatch(r'[\w-]{1,128}', work_id) or data.get('confirm') is not True:
            respond(handler, 400, {'error': '삭제할 작품과 전체 삭제 확인이 필요합니다.'})
            return
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            delete_progress(db, 'novel', work_id, clear=True)
            reader_usage.unlink_work(db, work_id)
            for table in ('reader_translations', 'reader_jobs', 'reader_episodes', 'reader_works', 'reader_work_settings'):
                db.execute(f'DELETE FROM {table} WHERE work_id=?', (work_id,))
        respond(handler, 200, {'ok': True})
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
            db.execute('BEGIN IMMEDIATE')
            now = int(time.time() * 1000)
            db.execute("""UPDATE progress SET deleted=0,revision=revision+1,updated_at=?
                WHERE kind='novel' AND work_id=? AND deleted=1""", (now, work_id))
            set_work_source(db, work_id, host)
            enqueue(db, work_id, force=True)
        respond(handler, 200, {"work_id": work_id, "episode_id": episode_id})
        return
    match = SOURCE_PATH.fullmatch(path)
    if match:
        work_id = match.group(1)
        try:
            host = checked_host(data.get('host'))
            work_url(host, work_id)
        except ValueError as exc:
            respond(handler, 400, {"error": str(exc)})
            return
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            work = db.execute('SELECT 1 FROM reader_works WHERE work_id=?', (work_id,)).fetchone()
            progress = db.execute("SELECT 1 FROM progress WHERE kind='novel' AND work_id=? AND deleted=0", (work_id,)).fetchone()
            if not work and not progress:
                respond(handler, 404, {"error": "저장된 작품이 아닙니다."})
                return
            set_work_source(db, work_id, host)
        respond(handler, 200, {"ok": True})
        return
    match = RESUME_PATH.fullmatch(path)
    if match:
        with connect() as db:
            work = db.execute("SELECT host FROM reader_works WHERE work_id=?", (match.group(1),)).fetchone()
            if not work or not work["host"]:
                respond(handler, 404, {"error": "출처를 먼저 선택하세요."})
                return
            resume_source(db, work["host"])
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


def current_job(db, work_id, episode_id, run_token):
    # Serialize completion with whole-work deletion; a re-added work has a new token.
    db.execute('BEGIN IMMEDIATE')
    return db.execute("SELECT 1 FROM reader_jobs WHERE work_id=? AND episode_id=? AND run_token=? AND state='running'",
                      (work_id, episode_id, run_token)).fetchone() is not None


def run_one_job(connect):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # For queued retries, updated_at is the earliest eligible start time.
        row = db.execute("""SELECT j.work_id,j.episode_id,j.attempts FROM reader_jobs j
            WHERE j.state='queued' AND j.updated_at<=? AND NOT EXISTS (
                SELECT 1 FROM reader_work_settings s WHERE s.work_id=j.work_id AND s.paused=1
            ) AND NOT EXISTS (
                SELECT 1 FROM reader_works w JOIN reader_paused_sources p ON p.host=w.host
                WHERE w.work_id=j.work_id
            ) ORDER BY j.updated_at,j.rowid LIMIT 1""", (int(time.time() * 1000),)).fetchone()
        if not row:
            return False
        work_id, episode_id = row["work_id"], row["episode_id"]
        limit = work_settings(db, work_id)['cache_limit_mb'] * 1024 * 1024
        if episode_id and limit and cache_bytes(db, work_id) >= limit:
            db.execute("UPDATE reader_jobs SET state='capacity',error='본문 저장 한도에 도달했습니다. 작품 관리에서 한도를 늘리거나 본문을 삭제하세요.' WHERE work_id=? AND episode_id=?",
                       (work_id, episode_id))
            return True
        attempt = row["attempts"] + 1
        run_token = secrets.token_hex(16)
        db.execute("UPDATE reader_jobs SET state='running',attempts=attempts+1,updated_at=?,run_token=? WHERE work_id=? AND episode_id=?",
                   (int(time.time() * 1000), run_token, work_id, episode_id))
        work = db.execute("SELECT host FROM reader_works WHERE work_id=?", (work_id,)).fetchone()
        episode = db.execute("SELECT source_url FROM reader_episodes WHERE work_id=? AND episode_id=?", (work_id, episode_id)).fetchone() if episode_id else None
    try:
        if not work or not work["host"]:
            raise CrawlUnavailable("작품 출처가 없습니다.")
        if not episode_id:
            title, chapters = collect_episode_list(work_url(work['host'], work_id), work_id)
            with connect() as db:
                if not current_job(db, work_id, episode_id, run_token):
                    return True
                now = int(time.time() * 1000)
                db.execute("UPDATE reader_works SET title=?,updated_at=? WHERE work_id=?", (title, now, work_id))
                for ordinal, (chapter_id, chapter_title, url) in enumerate(chapters, 1):
                    db.execute('''DELETE FROM reader_translations WHERE work_id=? AND episode_id=? AND EXISTS (
                        SELECT 1 FROM reader_episodes WHERE work_id=? AND episode_id=? AND title!=?)''',
                        (work_id, chapter_id, work_id, chapter_id, chapter_title))
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
                if not current_job(db, work_id, episode_id, run_token):
                    return True
                content = json.dumps(paragraphs, ensure_ascii=False)
                limit = work_settings(db, work_id)['cache_limit_mb'] * 1024 * 1024
                old = db.execute('SELECT COALESCE(length(CAST(paragraphs_json AS BLOB)),0) FROM reader_episodes WHERE work_id=? AND episode_id=?', (work_id, episode_id)).fetchone()[0]
                if limit and cache_bytes(db, work_id) - old + len(content.encode()) > limit:
                    db.execute("UPDATE reader_jobs SET state='capacity',error='이 회차를 저장하면 본문 저장 한도를 초과합니다. 작품 관리에서 한도를 늘리세요.' WHERE work_id=? AND episode_id=?",
                               (work_id, episode_id))
                    return True
                current_title = db.execute('SELECT title FROM reader_episodes WHERE work_id=? AND episode_id=?', (work_id, episode_id)).fetchone()[0]
                db.execute('DELETE FROM reader_translations WHERE work_id=? AND episode_id=? AND source_hash!=?',
                           (work_id, episode_id, reader_translate.fingerprint(current_title, paragraphs)))
                db.execute("""
                    UPDATE reader_episodes SET paragraphs_json=?,state='ready',error='',updated_at=?
                    WHERE work_id=? AND episode_id=?
                """, (content, int(time.time() * 1000), work_id, episode_id))
        with connect() as db:
            db.execute("UPDATE reader_jobs SET state='done',error='',updated_at=? WHERE work_id=? AND episode_id=? AND run_token=?",
                       (int(time.time() * 1000), work_id, episode_id, run_token))
    except HumanVerificationRequired as exc:
        with connect() as db:
            if not current_job(db, work_id, episode_id, run_token):
                return True
            db.execute("INSERT OR REPLACE INTO reader_paused_sources VALUES(?,?)", (work["host"], str(exc)))
            db.execute("UPDATE reader_jobs SET state='verification',error=? WHERE work_id=? AND episode_id=?",
                       (str(exc), work_id, episode_id))
            if episode_id:
                db.execute("UPDATE reader_episodes SET state='verification',error=? WHERE work_id=? AND episode_id=?",
                           (str(exc), work_id, episode_id))
    except LockedChapter as exc:
        with connect() as db:
            if not current_job(db, work_id, episode_id, run_token):
                return True
            db.execute("UPDATE reader_episodes SET state='locked',error=? WHERE work_id=? AND episode_id=?", (str(exc), work_id, episode_id))
            db.execute("UPDATE reader_jobs SET state='done',error=? WHERE work_id=? AND episode_id=?", (str(exc), work_id, episode_id))
    except Exception as exc:
        with connect() as db:
            if not current_job(db, work_id, episode_id, run_token):
                return True
            message = str(exc)[:500]
            retry = attempt <= 3  # Initial attempt plus at most three automatic retries.
            delay = 5 * attempt if retry else 0
            if retry:
                message = f'수집 실패 · {delay}초 후 자동 재시도 {attempt}/3: {message}'
            if episode_id:
                db.execute("UPDATE reader_episodes SET state=?,error=? WHERE work_id=? AND episode_id=?",
                           ('missing' if retry else 'error', message, work_id, episode_id))
            db.execute("UPDATE reader_jobs SET state=?,error=?,updated_at=? WHERE work_id=? AND episode_id=?",
                       ('queued' if retry else 'error', message, int((time.time() + delay) * 1000), work_id, episode_id))
    return True


def start_worker(connect):
    with connect() as db:
        db.execute("UPDATE reader_jobs SET state='queued' WHERE state='running'")

    def loop():
        next_verification_check = 0
        while True:
            try:
                if time.monotonic() >= next_verification_check:
                    next_verification_check = time.monotonic() + 10
                    resume_completed_verifications(connect)
                run_one_job(connect)
                time.sleep(1)  # One serial worker: leave a gap even after successful collection.
            except Exception:
                logging.exception("reader crawler worker failed")
                time.sleep(2)

    threading.Thread(target=loop, name="reader-crawler", daemon=True).start()
