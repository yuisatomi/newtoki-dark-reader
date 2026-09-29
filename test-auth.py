"""Offline migration, device approval, revocation and login checks."""
import importlib
import json
import os
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

_support = importlib.import_module('test-app')
import reader_auth
import reader_sync

PASSWORD = 'reading-test-password-2026'


class AccountTest(unittest.TestCase):
    setUp = _support.ReaderAppTest.setUp
    tearDown = _support.ReaderAppTest.tearDown
    request = _support.ReaderAppTest.request
    login = _support.ReaderAppTest.login

    def setup_owner(self):
        self.login()
        status, payload, headers = self.request('/app/api/account', {'username': 'reader', 'password': PASSWORD})
        self.assertEqual((status, payload), (200, {'ok': True}))
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]

    def start_pair(self, name='Phone'):
        status, payload, _ = self.request('/v1/auth/device/start', {'name': name}, cookie=False)
        self.assertEqual(status, 200, payload)
        return payload

    def approve(self, pair):
        status, payload, _ = self.request('/app/api/devices/approve', {'user_code': pair['user_code'], 'decision': 'approve'})
        self.assertEqual(status, 200, payload)

    def claim(self, pair):
        return self.request('/v1/auth/device/poll', {'device_code': pair['device_code']}, cookie=False)

    def bearer(self, token, body=None):
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        req = Request(self.base + '/v1/progress', headers=headers, method='PUT' if body else 'GET',
                      data=json.dumps(body).encode() if body else None)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_account_migration_and_login(self):
        self.assertEqual(self.request('/app/api/account', {'username': 'reader', 'password': PASSWORD})[0], 401)
        self.assertEqual(self.request('/v1/auth/device/start', {'name': 'Phone'})[0], 409)
        self.setup_owner()
        reader_sync.init_db()
        self.assertEqual(self.request('/app/api/me')[1]['username'], 'reader')
        self.assertEqual(self.bearer(reader_sync.SYNC_TOKEN)[1]['progress'][0]['episode_id'], '91')
        self.assertEqual(self.request('/app/api/login', {'token': reader_sync.SYNC_TOKEN})[0], 401)
        self.assertEqual(self.request('/app/api/login', {'username': 'reader', 'password': 'wrong'})[0], 401)
        self.assertEqual(self.request('/app/api/login', {'username': 'someone', 'password': PASSWORD})[0], 401)
        status, _, headers = self.request('/app/api/login', {'username': 'reader', 'password': PASSWORD})
        self.assertEqual(status, 200)
        for attribute in ('HttpOnly', 'Secure', 'SameSite=Strict'):
            self.assertIn(attribute, headers['Set-Cookie'])
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        raw_cookie = self.cookie.split('=', 1)[1]
        self.assertEqual(self.bearer(raw_cookie)[0], 401, 'Web sessions must not act as device credentials')
        self.assertEqual(self.request('/app/api/logout', {})[0], 200)
        self.assertFalse(self.request('/app/api/me')[1]['authenticated'], 'Logout must invalidate a copied cookie')
        with reader_sync.connect() as db:
            row = dict(db.execute('SELECT * FROM reader_account').fetchone())
            self.assertNotIn(PASSWORD, json.dumps(row))
            self.assertEqual(row['rounds'], 600_000)

    def test_pair_approve_claim_revoke_and_scope(self):
        self.setup_owner()
        pair = self.start_pair('<script>phone</script>')
        self.assertEqual(self.claim(pair)[1], {'status': 'pending'})
        self.assertEqual(self.claim(pair)[0], 429)
        self.assertEqual(self.request('/app/api/devices/approve', {'user_code': pair['user_code'], 'decision': 'approve'}, cookie=False)[0], 401)
        self.assertEqual(self.request('/app/api/devices/approve', {'user_code': pair['user_code'], 'decision': 'approve'}, app_header=False)[0], 403)
        self.approve(pair)
        with reader_sync.connect() as db:
            db.execute('UPDATE reader_pairings SET last_poll=0')
        status, granted, _ = self.claim(pair)
        self.assertEqual(status, 200)
        secret = granted['token']
        self.assertEqual(self.claim(pair)[0], 410, 'A device credential is delivered once')
        self.assertEqual(self.bearer(secret)[0], 200)
        self.assertEqual(self.bearer(secret, {'kind': 'novel', 'work_id': '999', 'episode_id': '92', 'position': .7, 'expected_revision': 1})[0], 200)
        self.assertEqual(self.request('/app/api/work/999')[1]['progress']['episode_id'], '92')
        owner_cookie = self.cookie
        self.cookie = 'reader_session=' + secret
        self.assertEqual(self.request('/app/api/devices')[0], 401)
        self.cookie = owner_cookie
        devices = self.request('/app/api/devices')[1]['devices']
        self.assertNotIn(secret, json.dumps(devices))
        device = next(row for row in devices if row['kind'] == 'device')
        self.assertEqual(self.request('/app/api/devices/revoke', {'id': device['id']})[0], 200)
        self.assertEqual(self.bearer(secret)[0], 401)
        self.assertTrue(self.request('/app/api/me')[1]['authenticated'])
        self.assertEqual(self.bearer(reader_sync.SYNC_TOKEN)[0], 200, 'Keep legacy migration path')
        with reader_sync.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM reader_credentials')]
            self.assertNotIn(secret, json.dumps(rows))
            self.assertNotIn(pair['device_code'], json.dumps([dict(r) for r in db.execute('SELECT * FROM reader_pairings')]))

    def test_expiry_denial_and_single_use(self):
        self.setup_owner()
        expired = self.start_pair()
        with reader_sync.connect() as db:
            db.execute('UPDATE reader_pairings SET expires_at=0')
        self.assertEqual(self.claim(expired)[0], 410)
        denied = self.start_pair()
        self.request('/app/api/devices/approve', {'user_code': denied['user_code'], 'decision': 'deny'})
        self.assertEqual(self.claim(denied)[0], 410)
        pair = self.start_pair()
        self.approve(pair)
        self.assertEqual(self.request('/app/api/devices/approve', {'user_code': pair['user_code'], 'decision': 'approve'})[0], 409)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.claim(pair)[0], range(2)))
        self.assertEqual(sorted(results), [200, 410])

    def test_password_change_revokes_all_new_credentials(self):
        self.setup_owner()
        pair = self.start_pair()
        self.approve(pair)
        token = self.claim(pair)[1]['token']
        old_cookie = self.cookie
        wrong = {'username': 'reader', 'password': PASSWORD + 'new', 'current_password': 'wrong'}
        self.assertEqual(self.request('/app/api/account', wrong)[0], 403)
        self.assertEqual(self.bearer(token)[0], 200)
        wrong['current_password'] = PASSWORD
        status, _, headers = self.request('/app/api/account', wrong)
        self.assertEqual(status, 200)
        self.assertEqual(self.bearer(token)[0], 401)
        self.assertFalse(self.request('/app/api/me')[1]['authenticated'])
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.assertNotEqual(self.cookie, old_cookie)
        self.assertTrue(self.request('/app/api/me')[1]['authenticated'])
        self.assertEqual(self.bearer(reader_sync.SYNC_TOKEN)[1]['progress'][0]['episode_id'], '91')

    def test_attempt_limits_and_console_reset(self):
        self.setup_owner()
        for _ in range(12):
            status = self.request('/app/api/login', {'username': 'reader', 'password': 'wrong'})[0]
        self.assertEqual(status, 429)
        reader_auth.set_password(reader_sync.connect, 'reader', PASSWORD + 'reset', reset=True)
        self.assertFalse(self.request('/app/api/me')[1]['authenticated'])
        with self.assertRaises(ValueError):
            reader_auth.set_password(reader_sync.connect, 'reader', 'short', reset=True)
        self.assertFalse(reader_auth.valid_password('a' * 14))
        self.assertTrue(reader_auth.valid_password('가' * 15))
        self.assertFalse(reader_auth.valid_password('a' * 257))

    def test_browser_password_and_device_approval(self):
        try:
            from playwright.sync_api import sync_playwright, expect
        except ImportError:
            self.skipTest('Playwright is not installed')
        executable = os.getenv('READER_TEST_BROWSER', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        if not Path(executable).exists():
            self.skipTest('Chromium browser is not available')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                page = browser.new_page(viewport={'width': 390, 'height': 800})
                page.goto(self.base + '/app')
                page.locator('#token').fill(reader_sync.SYNC_TOKEN)
                page.locator('#login-form button').click()
                page.locator('#account-open').click()
                page.locator('#account-username').fill('reader')
                page.locator('#new-password').fill(PASSWORD)
                page.locator('#confirm-password').fill(PASSWORD)
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.locator('#account-form button').click()
                expect(page.locator('#account-message')).to_contain_text('계정을 저장했습니다')
                page.locator('#account-back').click()
                page.locator('#logout').click()
                page.locator('#login').wait_for(state='visible')
                pair = self.start_pair('Mobile Firefox')
                page.goto(self.base + '/app#connect=' + pair['user_code'])
                page.locator('#username').fill('reader')
                page.locator('#password').fill(PASSWORD)
                page.locator('#login-form button').click()
                expect(page.locator('#pair-name')).to_contain_text(pair['user_code'])
                self.assertEqual(self.claim(pair)[1]['status'], 'pending', 'Opening the URL must not approve')
                page.locator('#pair-approve').click()
                expect(page.locator('#pair-message')).to_contain_text('승인했습니다')
                with reader_sync.connect() as db:
                    db.execute('UPDATE reader_pairings SET last_poll=0')
                secret = self.claim(pair)[1]['token']
                page.locator('#devices-refresh').click()
                device = page.locator('#device-list .item').filter(has_text='Mobile Firefox')
                expect(device).to_be_visible()
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.on('dialog', lambda dialog: dialog.accept())
                device.get_by_role('button', name='연결 해제').click()
                expect(device).to_have_count(0)
                self.assertEqual(self.bearer(secret)[0], 401)
                page.reload()
                page.locator('#home').wait_for(state='visible')
                self.assertFalse(page.locator('#login').is_visible())
                # Run the actual userscript, emulating only Tampermonkey storage/transport.
                script = browser.new_page(viewport={'width': 390, 'height': 800})
                def gm_request(options):
                    target = urlparse(options['url'])
                    self.assertEqual(target.hostname, 'reader-sync.flolim.com')
                    self.assertIn(target.path, ('/v1/progress', '/v1/auth/device/start', '/v1/auth/device/poll'))
                    request = Request(self.base + target.path + ('?' + target.query if target.query else ''),
                                      data=(options.get('data') or '').encode() or None,
                                      headers=options.get('headers', {}), method=options['method'])
                    try:
                        response = urlopen(request, timeout=5)
                    except HTTPError as exc:
                        response = exc
                    with response:
                        return {'status': response.status, 'responseText': response.read().decode()}
                script.expose_function('__request', gm_request)
                script.add_init_script("""
                  window.__values={}; window.__menus={};
                  window.GM_getValue=(key,fallback)=>Object.hasOwn(__values,key)?__values[key]:fallback;
                  window.GM_setValue=(key,value)=>{__values[key]=value};
                  window.GM_registerMenuCommand=(name,fn)=>{__menus[name]=fn};
                  window.GM_xmlhttpRequest=options=>__request({method:options.method,url:options.url,
                    headers:options.headers,data:options.data}).then(options.onload,options.onerror);
                """)
                script.route('https://newtoki1.org/**', lambda route: route.fulfill(content_type='text/html', body='<title>Reader test</title><body>Test</body>'))
                script.on('dialog', lambda dialog: dialog.accept('Script phone'))
                script.goto('https://newtoki1.org/')
                script.add_script_tag(content=Path('newtoki-dark-reader.user.js').read_text(encoding='utf-8'))
                script.evaluate("Object.entries(__menus).find(([name])=>name.includes('계정 연결'))[1]()")
                link = script.locator('#nt-account-connect a')
                expect(link).to_be_visible()
                url = link.get_attribute('href')
                self.assertNotIn('device_code', url)
                page.goto(self.base + '/app' + '#' + urlparse(url).fragment)
                page.reload()  # The userscript link opens a fresh tab, not same-document hash navigation.
                expect(page.locator('#pair-name')).to_contain_text('Script phone')
                self.assertFalse(script.evaluate("!!__values.ntReaderDeviceToken"))
                page.locator('#pair-approve').click()
                expect(script.locator('#nt-account-connect')).to_contain_text('연결되었습니다', timeout=15000)
                device_token = script.evaluate('__values.ntReaderDeviceToken')
                self.assertTrue(device_token.startswith('rd_'))
                self.assertFalse(script.evaluate("!!__values.ntReaderSyncToken"))
                self.assertEqual(self.bearer(device_token)[0], 200)
                page.locator('#devices-refresh').click()
                row = page.locator('#device-list .item').filter(has_text='Script phone')
                row.get_by_role('button', name='연결 해제').click()
                expect(row).to_have_count(0)
                self.assertEqual(self.bearer(device_token)[0], 401)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
