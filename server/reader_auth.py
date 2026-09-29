"""Single-owner login and explicit device approval; progress remains shared.

Legacy bearer tokens remain valid during migration. New device tokens only
authorize progress APIs, never account administration or pairing approval.
"""
import hashlib
import hmac
import re
import secrets
import time
from http.cookies import CookieError, SimpleCookie

SESSION_SECONDS = 30 * 86400
PAIR_SECONDS = 600
PASSWORD_ROUNDS = 600_000


def init_db(connect):
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS reader_account (
            id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL,
            salt TEXT NOT NULL, password_hash TEXT NOT NULL, rounds INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS reader_credentials (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
            name TEXT NOT NULL, created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS reader_pairings (
            digest TEXT PRIMARY KEY, user_code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            expires_at INTEGER NOT NULL, state TEXT NOT NULL, last_poll INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS reader_auth_limits (
            bucket TEXT PRIMARY KEY, started_at INTEGER NOT NULL, count INTEGER NOT NULL)""")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def account(connect):
    with connect() as db:
        row = db.execute("SELECT * FROM reader_account WHERE id=1").fetchone()
        return dict(row) if row else None


def limited(connect, bucket, maximum):
    # ponytail: global per-action limit for this single-owner server, not per-IP;
    # avoids trusting spoofable forwarding headers. Use edge limits if shared at scale.
    now = int(time.time())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""INSERT INTO reader_auth_limits VALUES(?,?,1)
            ON CONFLICT(bucket) DO UPDATE SET
              count=CASE WHEN started_at<=?-60 THEN 1 ELSE count+1 END,
              started_at=CASE WHEN started_at<=?-60 THEN ? ELSE started_at END""",
                   (bucket, now, now, now, now))
        return db.execute("SELECT count FROM reader_auth_limits WHERE bucket=?", (bucket,)).fetchone()[0] > maximum


def valid_password(password):
    return isinstance(password, str) and 15 <= len(password) <= 256


def password_matches(row, password):
    if not isinstance(password, str) or len(password) > 256:
        return False
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(row['salt']), row['rounds']).hex()
    return hmac.compare_digest(hashed, row['password_hash'])


def set_password(connect, username, password, previous=None, reset=False):
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', username):
        raise ValueError('아이디는 영문·숫자·밑줄·점·하이픈 3~64자로 입력하세요.')
    if not valid_password(password):
        raise ValueError('비밀번호는 15~256자로 입력하세요.')
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PASSWORD_ROUNDS).hex()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT password_hash FROM reader_account WHERE id=1').fetchone()
        if not reset and (current['password_hash'] if current else None) != previous:
            raise ValueError('계정 정보가 변경되었습니다. 다시 로그인하세요.')
        db.execute('INSERT OR REPLACE INTO reader_account VALUES(1,?,?,?,?)', (username, salt, hashed, PASSWORD_ROUNDS))
        db.execute('UPDATE reader_credentials SET revoked=1')
        db.execute('DELETE FROM reader_pairings')


def issue(db, kind, name):
    raw = ('rs_' if kind == 'browser' else 'rd_') + secrets.token_urlsafe(32)
    now = int(time.time())
    device_id = secrets.token_hex(12)
    db.execute('INSERT INTO reader_credentials VALUES(?,?,?,?,?,?,?,0)',
               (device_id, digest(raw), kind, str(name)[:80] or '이름 없는 기기', now, now,
                now + SESSION_SECONDS if kind == 'browser' else 0))
    return raw


def credential(connect, raw, kind):
    if not isinstance(raw, str) or not 20 <= len(raw) <= 128:
        return None
    now = int(time.time())
    with connect() as db:
        row = db.execute('''SELECT id,kind,name,created_at,last_seen,expires_at FROM reader_credentials
            WHERE digest=? AND kind=? AND revoked=0 AND (expires_at=0 OR expires_at>?)''',
                         (digest(raw), kind, now)).fetchone()
        if row and row['last_seen'] < now - 60:
            db.execute('UPDATE reader_credentials SET last_seen=? WHERE id=?', (now, row['id']))
        return dict(row) if row else None


def session_value(handler):
    try:
        cookies = SimpleCookie()
        cookies.load(handler.headers.get('Cookie', ''))
        return cookies['reader_session'].value
    except (CookieError, KeyError):
        return ''


def session(handler, connect, legacy_token):
    raw = session_value(handler)
    result = credential(connect, raw, 'browser')
    if result:
        return result
    # Pre-migration signed cookies work only until the owner account is created.
    if not legacy_token or account(connect):
        return None
    try:
        expiry, nonce, signature = raw.split('.')
        expected = hmac.new(legacy_token.encode(), (expiry + '.' + nonce).encode(), hashlib.sha256).hexdigest()
        if int(expiry) > time.time() and len(nonce) >= 20 and hmac.compare_digest(signature.encode(), expected.encode()):
            return {'id': '', 'kind': 'browser', 'name': '이전 로그인'}
    except ValueError:
        pass
    return None


def cookie(raw='', clear=False):
    return f'reader_session={raw}; Path=/app; Max-Age={0 if clear else SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict'


def login(handler, connect, legacy_token, data):
    if limited(connect, 'login', 12):
        return 429, {'error': '로그인 시도가 많습니다. 1분 후 다시 시도하세요.'}, None
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM reader_account WHERE id=1').fetchone()
        if row:
            matches = password_matches(row, data.get('password'))
            valid = matches and data.get('username') == row['username']
        else:
            supplied = data.get('token', '')
            valid = isinstance(supplied, str) and bool(legacy_token) and hmac.compare_digest(supplied.encode(), legacy_token.encode())
        if not valid:
            return 401, {'error': '로그인 정보가 올바르지 않습니다.'}, None
        raw = issue(db, 'browser', data.get('device_name') or '웹 브라우저')
    return 200, {'ok': True}, cookie(raw)


def device_request(connect, path, data):
    """Unauthenticated start/poll. Short user codes are never credentials."""
    if path == '/v1/auth/device/start':
        if limited(connect, 'pair-start', 12):
            return 429, {'error': '연결 요청이 많습니다. 1분 후 다시 시도하세요.'}
        if not account(connect):
            return 409, {'error': '서버에서 소유자 계정을 먼저 설정하세요.'}
        name = data.get('name', '')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            return 400, {'error': '기기 이름을 1~80자로 입력하세요.'}
        raw = secrets.token_urlsafe(32)
        code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
        code = code[:4] + '-' + code[4:]
        now = int(time.time())
        with connect() as db:
            db.execute('DELETE FROM reader_pairings WHERE expires_at<=?', (now,))
            db.execute('INSERT INTO reader_pairings VALUES(?,?,?,?,?,0)', (digest(raw), code, name.strip(), now + PAIR_SECONDS, 'pending'))
        return 200, {'device_code': raw, 'user_code': code, 'expires_in': PAIR_SECONDS, 'interval': 5}
    if path != '/v1/auth/device/poll':
        return 404, {'error': 'not found'}
    raw = data.get('device_code')
    if not isinstance(raw, str) or not 30 <= len(raw) <= 128:
        return 400, {'error': '잘못된 연결 요청입니다.'}
    now = int(time.time())
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM reader_pairings WHERE digest=?', (digest(raw),)).fetchone()
        if not row or row['expires_at'] <= now or row['state'] in ('used', 'denied'):
            return 410, {'error': '만료·취소되었거나 이미 연결된 요청입니다. 다시 연결하세요.'}
        if row['last_poll'] > now - 4:
            return 429, {'error': 'slow_down'}
        db.execute('UPDATE reader_pairings SET last_poll=? WHERE digest=?', (now, digest(raw)))
        if row['state'] == 'pending':
            return 200, {'status': 'pending'}
        token = issue(db, 'device', row['name'])
        db.execute("UPDATE reader_pairings SET state='used' WHERE digest=?", (digest(raw),))
        return 200, {'status': 'approved', 'token': token}


def manage(handler, connect, path, data):
    """Caller must check browser session and same-origin request header."""
    if path == '/app/api/account':
        if limited(connect, 'account-change', 6):
            return 429, {'error': '잠시 후 다시 시도하세요.'}, None
        current = account(connect)
        if current and not password_matches(current, data.get('current_password')):
            return 403, {'error': '현재 비밀번호가 올바르지 않습니다.'}, None
        try:
            set_password(connect, data.get('username'), data.get('password'), current['password_hash'] if current else None)
        except ValueError as exc:
            return 400, {'error': str(exc)}, None
        with connect() as db:
            raw = issue(db, 'browser', '웹 브라우저')
        return 200, {'ok': True}, cookie(raw)
    if path == '/app/api/devices/revoke':
        with connect() as db:
            db.execute('UPDATE reader_credentials SET revoked=1 WHERE id=?', (str(data.get('id', '')),))
        return 200, {'ok': True}, None
    if path == '/app/api/devices/approve':
        if not account(connect):
            return 409, {'error': '먼저 소유자 계정을 설정하세요.'}, None
        if limited(connect, 'pair-approve', 12):
            return 429, {'error': '잠시 후 다시 시도하세요.'}, None
        code = str(data.get('user_code', '')).strip().upper()
        decision = data.get('decision')
        if decision not in ('approve', 'deny'):
            return 400, {'error': '승인 또는 거절을 선택하세요.'}, None
        with connect() as db:
            result = db.execute("UPDATE reader_pairings SET state=? WHERE user_code=? AND state='pending' AND expires_at>?",
                                ('approved' if decision == 'approve' else 'denied', code, int(time.time())))
        return (200, {'ok': True}, None) if result.rowcount else (409, {'error': '유효한 대기 요청이 아닙니다.'}, None)
    if path == '/app/api/logout':
        with connect() as db:
            db.execute('UPDATE reader_credentials SET revoked=1 WHERE digest=?', (digest(session_value(handler)),))
        return 200, {'ok': True}, cookie(clear=True)
    return None
