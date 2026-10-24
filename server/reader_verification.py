"""Human-operated, session-bound view of an existing verification tab; never solve it."""
import base64
import secrets
import threading
import time

from reader_crawl import BROWSER_CONTENT_READY, CrawlUnavailable, browser_endpoint, checked_url, source_browser

_lock = threading.Lock()
_active = None


class VerificationError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def waiting(connect, work_id):
    with connect() as db:
        row = db.execute('''SELECT w.host,j.work_id,j.episode_id,j.run_token,
            COALESCE(e.title,w.title) AS title FROM reader_jobs j
            JOIN reader_works w ON w.work_id=j.work_id JOIN reader_paused_sources p ON p.host=w.host
            LEFT JOIN reader_episodes e ON e.work_id=j.work_id AND e.episode_id=j.episode_id
            WHERE j.state='verification' AND w.host=(SELECT host FROM reader_works WHERE work_id=?)
            ORDER BY j.work_id=? DESC,j.updated_at LIMIT 1''', (work_id, work_id)).fetchone()
    return dict(row) if row else None


def current(connect, session):
    with connect() as db:
        requested = db.execute('SELECT host FROM reader_works WHERE work_id=?', (session['requested'],)).fetchone()
        row = db.execute('''SELECT j.state,j.run_token,w.host FROM reader_jobs j
            JOIN reader_works w ON w.work_id=j.work_id WHERE j.work_id=? AND j.episode_id=?''',
                         (session['work_id'], session['episode_id'])).fetchone()
    if not requested or requested['host'] != session['host'] or not row or row['host'] != session['host']:
        raise VerificationError(410, '작품이나 출처가 변경되었습니다. 확인 창을 다시 여세요.')
    return row['state'] == 'verification' and row['run_token'] == session['run_token']


def tab(browser, session):
    expected = (session['host'], session['work_id'], session['episode_id'] or None)
    for context in browser.contexts:
        for page in context.pages:
            try:
                if checked_url(page.url) != expected:
                    continue
            except ValueError:
                continue
            channel = context.new_cdp_session(page)
            try:
                target = channel.send('Target.getTargetInfo')['targetInfo']['targetId']
            finally:
                channel.detach()
            if not session.get('target') or target == session['target']:
                session['target'] = target
                return page
    raise VerificationError(410, '대기 중인 원본 탭이 닫히거나 이동했습니다. 확인 창을 다시 여세요.')


def operate(connect, work_id, owner, data):
    global _active
    action = data.get('action')
    allowed = {'open': {'action'}, 'frame': {'action','session'}, 'close': {'action','session'},
               'click': {'action','session','frame','x','y'}, 'key': {'action','session','frame','key'}}
    if not isinstance(action, str) or action not in allowed or set(data) != allowed[action]:
        raise VerificationError(400, '허용되지 않은 확인 화면 요청입니다.')
    if action == 'click' and not all(type(data[k]) in (int, float) and 0 <= data[k] < 2000 for k in ('x','y')):
        raise VerificationError(400, '화면 안의 좌표만 사용할 수 있습니다.')
    if action == 'key' and data['key'] not in ('Tab','Shift+Tab','Enter','Space'):
        raise VerificationError(400, '허용되지 않은 키입니다.')
    with _lock:
        now = time.monotonic()
        if _active and (now >= _active['expires'] or now >= _active['created']+300):
            _active = None
        if action == 'open':
            if _active:
                raise VerificationError(409, '다른 확인 창이 열려 있습니다. 기존 창을 닫으세요. 연결이 끊긴 창은 1분 후 해제됩니다.')
            try:
                endpoint = browser_endpoint()
            except CrawlUnavailable:
                raise VerificationError(503, '공유 원본 브라우저 설정을 확인하세요.') from None
            if not endpoint:
                raise VerificationError(503, '공유 원본 브라우저가 설정되지 않았습니다.')
            target = waiting(connect, work_id)
            if not target:
                return {'state': 'finished', 'message': '확인 대기가 해제되었습니다. 수집 상태를 확인하세요.'}
            _active = target | {'requested': work_id, 'owner': owner, 'session': secrets.token_hex(24),
                                'created': now, 'expires': now+60, 'frame': '', 'captured': 0}
        else:
            if not _active:
                raise VerificationError(410, '확인 창이 만료되었습니다. 다시 연결하세요.')
            if (_active['owner'] != owner or _active['requested'] != work_id
                    or not isinstance(data.get('session'), str)
                    or not data['session'].isascii()
                    or not secrets.compare_digest(data['session'], _active['session'])):
                raise VerificationError(403, '이 로그인에서 연 확인 창이 아닙니다.')
            if action == 'close':
                _active = None
                return {'state': 'closed'}
        session = _active
        try:
            if not current(connect, session):
                _active = None
                return {'state': 'finished', 'message': '대기 상태가 변경되었습니다. 수집 상태를 확인하세요.'}
            with source_browser() as browser:
                page = tab(browser, session)
                if action in ('open','click','key'):
                    page.bring_to_front()
                if page.evaluate(BROWSER_CONTENT_READY):
                    _active = None
                    return {'state': 'ready', 'message': '확인이 완료되었습니다. 수집을 자동 재개합니다. 일시정지한 작품은 그대로 유지합니다.'}
                size = page.evaluate('() => ({width:innerWidth,height:innerHeight})')
                if not 1 <= size['width'] <= 2000 or not 1 <= size['height'] <= 1600:
                    raise VerificationError(503, '원본 화면 크기가 지원 범위를 벗어났습니다.')
                if action in ('click','key'):
                    if (not session['frame'] or not isinstance(data['frame'], str) or not data['frame'].isascii() or not secrets.compare_digest(data['frame'], session['frame'])
                            or time.monotonic()-session['captured'] > 15 or size != session.get('size')):
                        raise VerificationError(409, '화면이 갱신되었습니다. 최신 화면에서 다시 조작하세요.')
                    if action == 'click' and not (0 <= data['x'] < size['width'] and 0 <= data['y'] < size['height']):
                        raise VerificationError(400, '화면 안의 좌표만 사용할 수 있습니다.')
                    # A frame can authorize only one explicit human input, never an automatic retry.
                    session['frame'] = ''
                    if not current(connect, session):
                        _active = None
                        return {'state': 'finished', 'message': '대기 상태가 변경되었습니다.'}
                    if page.evaluate(BROWSER_CONTENT_READY):
                        _active = None
                        return {'state': 'ready', 'message': '확인이 완료되었습니다. 수집을 자동 재개합니다. 일시정지한 작품은 그대로 유지합니다.'}
                    if tab(browser, session) != page:
                        raise VerificationError(410, '원본 탭이 변경되었습니다. 확인 창을 다시 여세요.')
                    if action == 'click':
                        page.mouse.click(data['x'], data['y'])
                    else:
                        page.keyboard.press(data['key'])
                picture = page.screenshot(type='jpeg', quality=65, scale='css', timeout=5000)
                if len(picture) > 1_000_000:
                    raise VerificationError(503, '확인 화면이 너무 큽니다.')
                if not current(connect, session):
                    _active = None
                    return {'state': 'finished', 'message': '대기 상태가 변경되었습니다. 수집 상태를 확인하세요.'}
                if tab(browser, session) != page:
                    raise VerificationError(410, '원본 탭이 변경되었습니다. 확인 창을 다시 여세요.')
                if page.evaluate(BROWSER_CONTENT_READY):
                    _active = None
                    return {'state': 'ready', 'message': '확인이 완료되었습니다. 수집을 자동 재개합니다. 일시정지한 작품은 그대로 유지합니다.'}
                session.update(frame=secrets.token_hex(16), captured=time.monotonic(), size=size, expires=time.monotonic()+60)
                return {'state': 'waiting', 'session': session['session'], 'frame': session['frame'], **size,
                        'host': session['host'], 'title': session['title'], 'image': base64.b64encode(picture).decode('ascii')}
        except VerificationError as error:
            if action == 'open' or error.status == 410:
                _active = None
            raise
        except Exception:
            if action == 'open':
                _active = None
            raise VerificationError(503, '원본 브라우저에 연결하지 못했습니다. 다시 연결하세요.') from None
