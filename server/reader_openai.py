"""Private SIWC credentials and Responses transport; no API-key fallback."""
import argparse
import base64
from contextlib import contextmanager
from functools import lru_cache
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
import webbrowser

MODEL = 'gpt-6-luna'
AUTH = 'https://auth.openai.com'
API = 'https://api.openai.com/v1'
SCOPES = {'openid', 'profile', 'email', 'offline_access', 'resource.invoke', 'chatgpt.tokens.use.direct'}
_lock = threading.RLock()


class OpenAIError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Bearer credentials must never follow a redirect.


def open_request(url, *, token=None, data=None, form=False):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'auth.openai.com', 'api.openai.com'} or parsed.port or parsed.username or parsed.password:
        raise OpenAIError('auth', 'OpenAI 인증 서버 주소를 확인할 수 없습니다.')
    headers = {'Accept': 'text/event-stream' if url.endswith('/responses') else 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        data = (urlencode(data) if form else json.dumps(data, ensure_ascii=False)).encode()
    try:
        return build_opener(ProxyHandler({}), NoRedirect()).open(Request(url, data=data, headers=headers), timeout=90)
    except HTTPError as error:
        status = error.code
        error.close()
        code, message = ('auth', 'ChatGPT 연결을 다시 확인하세요.') if status == 401 else (
            ('quota', 'ChatGPT 사용 한도에 도달했습니다. 잠시 후 다시 시도하세요.') if status == 429 else
            ('access', 'ChatGPT 계정의 모델·앱 사용 권한을 확인하세요.') if status == 403 else
            ('request', 'OpenAI 요청이 거부되었습니다. 연결 상태를 확인하고 다시 시도하세요.'))
        raise OpenAIError(code, message) from None
    except (URLError, TimeoutError, OSError):
        raise OpenAIError('network', 'OpenAI 연결이 중단되었습니다. 직접 다시 시도하세요.') from None


def request_json(url, **kwargs):
    with open_request(url, **kwargs) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise OpenAIError('response', 'OpenAI 응답이 너무 큽니다.')
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise OpenAIError('response', 'OpenAI 응답을 확인할 수 없습니다.') from None


@lru_cache(maxsize=1)
def discovery():
    data = request_json(AUTH + '/.well-known/openid-configuration')
    if data.get('issuer', '').rstrip('/') != AUTH:
        raise OpenAIError('auth', '인증 발급자가 일치하지 않습니다.')
    for key in ('jwks_uri', 'revocation_endpoint'):
        parsed = urlparse(data.get(key, ''))
        if parsed.scheme != 'https' or parsed.hostname != 'auth.openai.com' or parsed.port or parsed.username or parsed.password:
            raise OpenAIError('auth', '인증 메타데이터를 확인할 수 없습니다.')
    return data


def verify_identity(id_token, client_id, nonce=None, subject=None):
    try:
        import jwt
        metadata = discovery()
        header = jwt.get_unverified_header(id_token)
        if header.get('alg') not in ('RS256', 'ES256'):
            raise ValueError()
        keys = request_json(metadata['jwks_uri']).get('keys', [])
        key = next(k for k in keys if k.get('kid') == header.get('kid') and k.get('use', 'sig') == 'sig')
        decoded = jwt.decode(id_token, jwt.PyJWK.from_dict(key).key, algorithms=[header['alg']],
                             audience=client_id, issuer=metadata['issuer'],
                             options={'require': ['exp', 'iss', 'aud', 'sub']})
        if not isinstance(decoded['sub'], str) or not decoded['sub']:
            raise ValueError()
        if nonce is not None and (not isinstance(decoded.get('nonce'), str) or not secrets.compare_digest(decoded['nonce'], nonce)):
            raise ValueError()
        if subject is not None and decoded['sub'] != subject:
            raise ValueError()
        return decoded
    except ImportError:
        raise OpenAIError('auth', 'OAuth 검증 의존성 PyJWT[crypto]를 설치하세요.') from None
    except OpenAIError:
        raise
    except Exception:
        raise OpenAIError('auth', 'ChatGPT 인증 서명·계정·유효기간을 검증하지 못했습니다. 다시 연결하세요.') from None


def state_dir():
    return Path(os.environ.get('READER_OPENAI_DIR', '/var/lib/reader-sync/openai'))


def atomic_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.credential-', dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(data, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def credential_lock():
    with _lock:
        directory = state_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (directory / 'credentials.lock').open('a+b') as handle:
            os.chmod(directory / 'credentials.lock', 0o600)
            if os.name == 'posix':
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'posix':
                    fcntl.flock(handle, fcntl.LOCK_UN)


def load_credentials():
    try:
        record = json.loads((state_dir() / 'credentials.json').read_text(encoding='utf-8'))
        if not isinstance(record, dict):
            raise ValueError()
        return record
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise OpenAIError('auth', 'ChatGPT 연결 정보를 읽지 못했습니다. PC에서 다시 연결하세요.') from None


def account_status():
    try:
        data = load_credentials()
        connected = bool(data.get('refresh_token') and data.get('access_token') and not data.get('needs_reauth'))
        return {'connected': connected, 'registered': bool(data.get('client_id')), 'email': str(data.get('email', '')),
                'model': MODEL, 'model_available': bool(data.get('model_available') and connected),
                'error': str(data.get('error', ''))}
    except OpenAIError as error:
        return {'connected': False, 'registered': False, 'email': '', 'model': MODEL, 'model_available': False, 'error': str(error)}


def checked_tokens(tokens, client_id, nonce=None, subject=None, previous=None):
    previous = previous or {}
    identity = verify_identity(tokens.get('id_token') or previous.get('id_token', ''), client_id, nonce, subject)
    scopes = tokens.get('scope', ' '.join(previous.get('scopes', [])))
    if not isinstance(scopes, str) or not SCOPES.issubset(set(scopes.split())):
        raise OpenAIError('access', 'ChatGPT 플랜 사용 권한을 승인해야 합니다.')
    access, refresh = tokens.get('access_token'), tokens.get('refresh_token') or previous.get('refresh_token')
    expires = tokens.get('expires_in')
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh or type(expires) not in (int, float) or not math.isfinite(expires) or not 0 < expires <= 31_536_000:
        raise OpenAIError('auth', 'ChatGPT 인증 정보가 완전하지 않습니다.')
    return {'client_id': client_id, 'subject': identity['sub'], 'email': identity.get('email', ''),
            'id_token': tokens.get('id_token') or previous['id_token'], 'access_token': access,
            'refresh_token': refresh, 'scopes': scopes.split(), 'expires_at': time.time() + expires,
            'model_available': False, 'needs_reauth': False, 'error': ''}


def available_model(access_token):
    models = request_json(API + '/models', token=access_token).get('models', [])
    return any(isinstance(model, dict) and model.get('slug') == MODEL and model.get('visibility') == 'list' for model in models)


def import_credentials(data):
    if not isinstance(data, dict) or not isinstance(data.get('client_id'), str) or data['client_id'] == 'dynamic_agent_client':
        raise OpenAIError('auth', '발급된 OAuth 클라이언트 정보가 필요합니다.')
    identity = verify_identity(data.get('id_token', ''), data['client_id'], data.get('nonce'), data.get('subject'))
    scopes = data.get('scopes')
    if (not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes)
            or not SCOPES.issubset(set(scopes)) or not all(isinstance(data.get(k), str) and data[k] for k in ('access_token', 'refresh_token'))):
        raise OpenAIError('access', 'ChatGPT 플랜 사용 권한이 없습니다.')
    if type(data.get('expires_at')) not in (int, float) or not math.isfinite(data['expires_at']) or data['expires_at'] <= time.time():
        raise OpenAIError('auth', '인증 정보가 만료되었습니다. PC에서 다시 연결하세요.')
    record = {key: data[key] for key in ('client_id', 'access_token', 'refresh_token', 'id_token', 'scopes', 'expires_at')}
    record.update(subject=identity['sub'], email=identity.get('email', ''),
                  model_available=available_model(record['access_token']), needs_reauth=False, error='')
    if not record['model_available']:
        raise OpenAIError('model', '이 ChatGPT 계정에서는 GPT-6 Luna를 사용할 수 없습니다.')
    with credential_lock():
        host = state_dir() / 'host.json'
        if not host.exists():
            atomic_json(host, {'ext_agent_host_id': 'urn:uuid:' + str(uuid.uuid4())})
        atomic_json(state_dir() / 'credentials.json', record)


def access_token(force=False, rejected_token=None):
    with credential_lock():
        record = load_credentials()
        if not record.get('refresh_token') or record.get('needs_reauth'):
            raise OpenAIError('auth', 'PC에서 ChatGPT를 연결하거나 다시 인증하세요.')
        if rejected_token is not None and record.get('access_token') != rejected_token:
            return record['access_token']
        if force or record.get('expires_at', 0) <= time.time() + 60:
            try:
                tokens = request_json(AUTH + '/api/accounts/oauth/token', form=True, data={
                    'grant_type': 'refresh_token', 'client_id': record['client_id'],
                    'refresh_token': record['refresh_token'], 'resource': API})
                # A refresh need not return a new ID token; its previous verified identity remains bound.
                if 'id_token' in tokens:
                    verify_identity(tokens['id_token'], record['client_id'], subject=record['subject'])
                scopes = tokens.get('scope', ' '.join(record['scopes']))
                expires = tokens.get('expires_in')
                refresh = tokens.get('refresh_token', record['refresh_token'])
                if not isinstance(scopes, str) or not SCOPES.issubset(set(scopes.split())):
                    raise OpenAIError('access', 'ChatGPT 플랜 사용 권한이 해제되었습니다.')
                if (not isinstance(tokens.get('access_token'), str) or not tokens['access_token'] or not isinstance(refresh, str) or not refresh
                        or type(expires) not in (int, float) or not math.isfinite(expires) or not 0 < expires <= 31_536_000):
                    raise OpenAIError('auth', 'ChatGPT 인증 갱신 결과가 올바르지 않습니다.')
                record.update(access_token=tokens['access_token'], refresh_token=refresh,
                              id_token=tokens.get('id_token') or record['id_token'], scopes=scopes.split(),
                              expires_at=time.time() + expires, error='')
                atomic_json(state_dir() / 'credentials.json', record)
            except OpenAIError as error:
                if error.code in ('auth', 'access', 'request'):
                    record.update(needs_reauth=True, error='ChatGPT 인증이 만료되거나 해제되었습니다. PC에서 다시 연결하세요.')
                    atomic_json(state_dir() / 'credentials.json', record)
                raise
        if not record.get('model_available'):
            raise OpenAIError('model', '이 ChatGPT 계정에서는 GPT-6 Luna를 사용할 수 없습니다.')
        return record['access_token']


def disconnect():
    message = 'ChatGPT 연결을 해제했습니다. 저장된 번역은 계속 읽을 수 있습니다.'
    with credential_lock():
        record = load_credentials()
        if record.get('refresh_token'):
            try:
                with open_request(discovery()['revocation_endpoint'], form=True, data={
                    'token': record['refresh_token'], 'token_type_hint': 'refresh_token', 'client_id': record['client_id']}):
                    pass
            except OpenAIError:
                message = '서버 연결을 해제했습니다. 원격 해제는 확인되지 않았으므로 ChatGPT 설정에서도 앱 연결을 해제하세요.'
        retained = {key: record[key] for key in ('client_id', 'subject', 'email') if key in record}
        atomic_json(state_dir() / 'credentials.json', retained)
    return message


def response_text(payload):
    token = access_token()
    for attempt in range(2):
        try:
            response = open_request(API + '/responses', token=token, data=payload)
            break
        except OpenAIError as error:
            if error.code != 'auth' or attempt:
                raise
            token = access_token(force=True, rejected_token=token)
    text, total, completed = [], 0, False
    event_data = []
    try:
        with response:
            for raw in response:
                total += len(raw)
                if total > 4_000_000 or len(raw) > 2_000_000:
                    raise OpenAIError('response', '번역 응답이 너무 큽니다.')
                line = raw.decode('utf-8').rstrip('\r\n')
                if line.startswith('data:'):
                    event_data.append(line[5:].lstrip())
                elif not line and event_data:
                    event = json.loads('\n'.join(event_data))
                    event_data = []
                    kind = event.get('type')
                    if kind == 'response.output_text.delta':
                        text.append(event.get('delta', ''))
                    elif kind in ('error', 'response.failed', 'response.incomplete'):
                        code = event.get('response', {}).get('error', {}).get('code', '') or event.get('code', '')
                        quota = code in ('subscription_sharing_usage_limit_exceeded', 'subscription_sharing_usage_unavailable', 'rate_limit_exceeded')
                        raise OpenAIError('quota' if quota else 'response',
                                          'ChatGPT 사용 한도 또는 플랜 상태를 확인한 뒤 다시 시도하세요.' if quota else '번역 응답이 완료되지 않았습니다. 직접 다시 시도하세요.')
                    elif kind == 'response.completed':
                        if event.get('response', {}).get('status') != 'completed':
                            raise OpenAIError('response', '번역 응답이 완료되지 않았습니다.')
                        completed = True
                        break
    except OpenAIError:
        raise
    except Exception:
        raise OpenAIError('network', '번역 응답이 중단되었습니다. 직접 다시 시도하세요.') from None
    if not completed or not text:
        raise OpenAIError('response', '완료된 번역 응답을 받지 못했습니다.')
    return ''.join(text)


def authorize(previous=None, local_directory=None):
    previous = previous or {}
    directory = local_directory or Path.home() / '.config' / 'reader-sync-openai'
    host_path = directory / 'host.json'
    if not host_path.exists():
        atomic_json(host_path, {'ext_agent_host_id': 'urn:uuid:' + str(uuid.uuid4())})
    host_id = previous.get('ext_agent_host_id') or json.loads(host_path.read_text(encoding='utf-8'))['ext_agent_host_id']
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Callback query strings contain authorization codes.

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = parsed.path == '/auth/callback' and secrets.compare_digest(query.get('state', [''])[0], state)
            if valid and not result:
                result.update({key: values[0] for key, values in query.items()})
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(('연결을 확인하고 있습니다. 이 창을 닫아도 됩니다.' if valid else '올바르지 않은 인증 응답입니다.').encode())

    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = 1
        redirect = f'http://127.0.0.1:{server.server_port}/auth/callback'
        client_id = previous.get('client_id') or 'dynamic_agent_client'
        query = {'client_id': client_id, 'ext_agent_host_id': host_id, 'response_type': 'code',
                 'redirect_uri': redirect, 'scope': ' '.join(sorted(SCOPES)), 'resource': API,
                 'state': state, 'nonce': nonce, 'code_challenge_method': 'S256',
                 'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
        if client_id == 'dynamic_agent_client':
            query['agent_name_hint'] = 'Reader Sync'
        elif previous.get('id_token'):
            query['id_token_hint'] = previous['id_token']
        if not webbrowser.open(AUTH + '/api/accounts/authorize?' + urlencode(query)):
            raise OpenAIError('auth', 'PC에서 기본 브라우저를 열 수 없습니다.')
        print('브라우저에서 ChatGPT 로그인과 플랜 사용 승인을 완료하세요.', flush=True)
        deadline = time.monotonic() + 600
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result or result.get('error') or not result.get('code'):
        raise OpenAIError('auth', 'ChatGPT 연결이 취소되었거나 시간이 만료되었습니다.')
    issued = result.get('client_id', client_id)
    if issued == 'dynamic_agent_client' or (client_id != 'dynamic_agent_client' and issued != client_id):
        raise OpenAIError('auth', 'OAuth 클라이언트가 일치하지 않습니다.')
    tokens = request_json(AUTH + '/api/accounts/oauth/token', form=True, data={
        'grant_type': 'authorization_code', 'client_id': issued, 'code': result['code'],
        'code_verifier': verifier, 'redirect_uri': redirect, 'resource': API})
    record = checked_tokens(tokens, issued, nonce, previous.get('subject'))
    record['nonce'] = nonce
    return record


def main():
    parser = argparse.ArgumentParser(description='ChatGPT OAuth 연결 도우미 (API 키를 사용하지 않음)')
    parser.add_argument('action', choices=['connect', 'status', '_import', '_connection-info'])
    parser.add_argument('--ssh', help='LXC SSH 주소, 예: root@192.168.100.118')
    parser.add_argument('--identity-file', help='기존 SSH 개인키 경로')
    parser.add_argument('--new-account', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == '_import':
            raw = sys.stdin.buffer.read(200_001)
            if len(raw) > 200_000:
                raise OpenAIError('auth', '인증 정보가 너무 큽니다.')
            import_credentials(json.loads(raw))
            print('ChatGPT OAuth 연결 완료: ' + MODEL)
        elif args.action == '_connection-info':
            with credential_lock():
                host = state_dir() / 'host.json'
                if not host.exists():
                    atomic_json(host, {'ext_agent_host_id': 'urn:uuid:' + str(uuid.uuid4())})
                try:
                    record = load_credentials()
                except OpenAIError:
                    record = {}
                info = {key: record[key] for key in ('client_id', 'subject', 'id_token') if key in record}
                info['ext_agent_host_id'] = json.loads(host.read_text(encoding='utf-8'))['ext_agent_host_id']
            print(json.dumps(info))
        elif args.action == 'status':
            print(json.dumps(account_status(), ensure_ascii=False))
        else:
            import re
            if not args.ssh or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:-]*', args.ssh):
                parser.error('--ssh에 기존 LXC SSH 주소를 입력하세요.')
            ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '-o', 'KexAlgorithms=curve25519-sha256']
            if args.identity_file:
                ssh += ['-i', args.identity_file]
            ssh.append(args.ssh)
            command = 'runuser -u reader-sync -- /opt/reader-sync/venv/bin/python /opt/reader-sync/reader_openai.py '
            previous = json.loads(subprocess.check_output(ssh + [command + '_connection-info'], timeout=30))
            if args.new_account:
                previous = {'ext_agent_host_id': previous['ext_agent_host_id']}
            record = authorize(previous)
            subprocess.run(ssh + [command + '_import'], input=json.dumps(record).encode(), check=True, timeout=120)
    except (OpenAIError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, OpenAIError) else '연결 도우미 실행에 실패했습니다. SSH·의존성·연결 주소를 확인하세요.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
