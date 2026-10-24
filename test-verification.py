"""Authorized human input bridge; synthetic checks only, never solve a live CAPTCHA."""
import base64
import importlib
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

support = importlib.import_module('test-app')
reader_app = support.reader_app
reader_sync = support.reader_sync
import reader_verification as control


class VerificationTest(unittest.TestCase):
    setUpBase = support.ReaderAppTest.setUp
    tearDownBase = support.ReaderAppTest.tearDown
    request = support.ReaderAppTest.request
    login = support.ReaderAppTest.login

    def setUp(self):
        self.setUpBase()
        control._active = None
        self.login()
        self.request('/app/api/works', {'url':'https://toki32.com/novel/999'})
        with reader_sync.connect() as db:
            db.execute("UPDATE reader_jobs SET state='done'")
            db.execute("INSERT INTO reader_episodes(work_id,episode_id,ordinal,title,source_url,state,updated_at) VALUES('999','91',1,'테스트 1화','https://toki32.com/novel/999/91','verification',1)")
            db.execute("INSERT INTO reader_jobs(work_id,episode_id,state,updated_at,run_token) VALUES('999','91','verification',1,'test-run')")
            db.execute("INSERT INTO reader_paused_sources VALUES('toki32.com','checking')")
        self.path = '/app/api/work/999/verification'
        self.page = Mock(url='https://toki32.com/novel/999/91')
        self.ready = False
        self.page.evaluate.side_effect = lambda script: self.ready if script == control.BROWSER_CONTENT_READY else {'width':1050,'height':790}
        self.page.screenshot.return_value = b'jpeg-test'
        self.channel = Mock()
        self.channel.send.return_value = {'targetInfo':{'targetId':'fixed-tab'}}
        self.context = Mock(pages=[self.page])
        self.context.new_cdp_session.return_value = self.channel
        self.browser = Mock(contexts=[self.context])
        self.connectPatch = patch.object(control, 'source_browser')
        self.connectMock = self.connectPatch.start()
        self.connectMock.return_value.__enter__.return_value = self.browser
        self.envPatch = patch.dict(os.environ, {'READER_BROWSER_CDP':'http://127.0.0.1:9222'})
        self.envPatch.start()

    def tearDown(self):
        control._active = None
        self.connectPatch.stop()
        self.envPatch.stop()
        self.tearDownBase()

    def opened(self):
        status, data, headers = self.request(self.path, {'action':'open'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['state'], 'waiting')
        self.assertEqual(base64.b64decode(data['image']), b'jpeg-test')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return data

    def test_authentication_scope_and_no_arbitrary_commands(self):
        for kwargs, expected in (({'cookie':False},401),({'cookie':False,'bearer':True},401),({'app_header':False},403)):
            self.assertEqual(self.request(self.path, {'action':'open'}, **kwargs)[0], expected)
        for body in ({'action':'open','url':'http://127.0.0.1:9222'}, {'action':'Runtime.evaluate'},
                     {'action':'key','session':'x','frame':'x','key':'Control+L'}):
            self.assertEqual(self.request(self.path, body)[0],400)
        self.connectMock.assert_not_called()
        with patch.dict(os.environ, {'READER_BROWSER_CDP':''}):
            self.assertEqual(self.request(self.path, {'action':'open'})[0],503)
        with patch.dict(os.environ, {'READER_BROWSER_CDP':'http://192.168.1.1:9222'}):
            self.assertEqual(self.request(self.path, {'action':'open'})[0],503)
        self.connectMock.assert_not_called()

    def test_only_exact_waiting_tab_is_visible_and_never_navigated(self):
        unrelated = [Mock(url=url) for url in ('https://evil.invalid/novel/999/91',
                      'https://toki32.com/novel/111/91','https://toki32.com/novel/999/92')]
        self.context.pages = unrelated+[self.page]
        data = self.opened()
        self.assertNotIn('fixed-tab',str(data))
        self.assertNotIn('9222',str(data))
        for page in unrelated:
            page.evaluate.assert_not_called()
            page.screenshot.assert_not_called()
        self.page.goto.assert_not_called()
        self.page.reload.assert_not_called()
        self.page.mouse.click.assert_not_called()

    def test_single_controller_bound_to_login_and_expiry(self):
        first = self.opened()
        previous = self.cookie
        self.login()
        self.assertEqual(self.request(self.path, {'action':'open'})[0],409)
        self.assertEqual(self.request(self.path, {'action':'frame','session':first['session']})[0],403)
        self.cookie = previous
        self.assertEqual(self.request(self.path, {'action':'close','session':first['session']})[0],200)
        self.opened()
        control._active['expires'] = 0
        self.assertEqual(self.request(self.path, {'action':'frame','session':first['session']})[0],410)

    def test_explicit_click_single_use_and_invalid_coordinates(self):
        first = self.opened()
        body = {'action':'click','session':first['session'],'frame':first['frame'],'x':90.5,'y':300}
        for x in (-1,1050,True,float('nan'),float('inf')):
            self.assertEqual(self.request(self.path, body|{'x':x})[0],400)
        self.page.mouse.click.assert_not_called()
        self.assertEqual(self.request(self.path, body)[0],200)
        self.page.mouse.click.assert_called_once_with(90.5,300)
        self.assertEqual(self.request(self.path, body)[0],409)
        self.page.mouse.click.assert_called_once()
        control._active['frame']=''
        self.assertEqual(self.request(self.path, body|{'frame':''})[0],409)

    def test_unicode_identifiers_and_huge_numbers_fail_closed(self):
        first=self.opened()
        for value in ('é'*48,'\ud800'*48):
            self.assertEqual(self.request(self.path,{'action':'frame','session':value})[0],403)
            body={'action':'click','session':first['session'],'frame':value,'x':90,'y':300}
            self.assertEqual(self.request(self.path,body)[0],409)
        self.assertEqual(self.request(self.path,body|{'frame':first['frame'],'x':10**1000})[0],400)
        self.page.mouse.click.assert_not_called()

    def test_stale_frame_size_and_target_replacements_reject_input(self):
        first=self.opened()
        body={'action':'click','session':first['session'],'frame':first['frame'],'x':90,'y':300}
        control._active['captured']-=16
        self.assertEqual(self.request(self.path,body)[0],409)
        control._active['captured']=time.monotonic()
        control._active['size']['width']=1000
        self.assertEqual(self.request(self.path,body)[0],409)
        self.channel.send.return_value={'targetInfo':{'targetId':'replacement-tab'}}
        self.assertEqual(self.request(self.path,body)[0],410)
        self.page.mouse.click.assert_not_called()

    def test_source_change_deletion_and_changed_jobs_stop_control(self):
        for change in ("UPDATE reader_works SET host='toki33.com' WHERE work_id='999'",
                       "DELETE FROM reader_works WHERE work_id='999'"):
            first=self.opened()
            with reader_sync.connect() as db: db.execute(change)
            self.assertEqual(self.request(self.path,{'action':'frame','session':first['session']})[0],410)
            with reader_sync.connect() as db:
                db.execute("INSERT OR REPLACE INTO reader_works VALUES('999','toki32.com','테스트',1)")
        first=self.opened()
        with reader_sync.connect() as db: db.execute("UPDATE reader_jobs SET state='queued' WHERE episode_id='91'")
        self.assertEqual(self.request(self.path,{'action':'frame','session':first['session']})[1]['state'],'finished')
        self.page.mouse.click.assert_not_called()

    def test_snapshot_result_is_discarded_after_deletion(self):
        def deleted(**kwargs):
            with reader_sync.connect() as db: db.execute("DELETE FROM reader_works WHERE work_id='999'")
            return b'jpeg-test'
        self.page.screenshot.side_effect=deleted
        status,data,_=self.request(self.path,{'action':'open'})
        self.assertEqual(status,410)
        self.assertNotIn('image',data)
        self.assertIsNone(control._active)

    def test_keyboard_input_and_automatic_resume_preserve_progress(self):
        first=self.opened()
        status,data,_=self.request(self.path,{'action':'key','session':first['session'],'frame':first['frame'],'key':'Tab'})
        self.assertEqual(status,200)
        self.page.keyboard.press.assert_called_once_with('Tab')
        self.ready=True
        with patch.object(reader_app,'ready_verification_pages',return_value={('toki32.com','999','91')}):
            result=self.request(self.path,{'action':'frame','session':first['session']})[1]
        self.assertEqual(result['state'],'ready')
        self.assertNotIn('image',result)
        with reader_sync.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM reader_jobs WHERE episode_id='91'").fetchone()[0],'queued')
            self.assertEqual(db.execute('SELECT count(*) FROM reader_paused_sources').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT position FROM progress WHERE work_id='999'").fetchone()[0],.6)

    def test_browser_unavailable_does_not_lose_waiting_job(self):
        self.connectMock.return_value.__enter__.side_effect=reader_app.CrawlUnavailable('offline')
        self.assertEqual(self.request(self.path,{'action':'open'})[0],503)
        self.assertIsNone(control._active)
        with reader_sync.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM reader_jobs WHERE episode_id='91'").fetchone()[0],'verification')

    def test_ready_page_keeps_explicit_collection_pause(self):
        with reader_sync.connect() as db:
            db.execute("INSERT INTO reader_work_settings(work_id,paused) VALUES('999',1)")
        self.ready=True
        result=self.request(self.path,{'action':'open'})[1]
        self.assertEqual(result['state'],'ready')
        self.page.mouse.click.assert_not_called()
        with reader_sync.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM reader_jobs WHERE episode_id='91'").fetchone()[0],'verification')

    def test_shared_host_uses_waiting_work_not_other_tabs(self):
        self.request('/app/api/works',{'url':'https://toki32.com/novel/111'})
        path='/app/api/work/111/verification'
        status,data,_=self.request(path,{'action':'open'})
        self.assertEqual(status,200)
        self.assertEqual(data['title'],'테스트 1화')
        self.assertEqual(self.request(self.path,{'action':'frame','session':data['session']})[0],403)
        self.assertEqual(self.request(path,{'action':'close','session':data['session']})[0],200)

    def test_maximum_session_age_and_late_ready_page_reject_input(self):
        first=self.opened()
        control._active['created']-=301
        self.assertEqual(self.request(self.path,{'action':'frame','session':first['session']})[0],410)
        first=self.opened()
        observations=0
        def becomes_ready(script):
            nonlocal observations
            if script!=control.BROWSER_CONTENT_READY: return {'width':1050,'height':790}
            observations+=1
            return observations>1
        self.page.evaluate.side_effect=becomes_ready
        with patch.object(reader_app,'ready_verification_pages',return_value={('toki32.com','999','91')}):
            result=self.request(self.path,{'action':'click','session':first['session'],'frame':first['frame'],'x':90,'y':300})[1]
        self.assertEqual(result['state'],'ready')
        self.page.mouse.click.assert_not_called()

    def test_mobile_and_desktop_touch_closed_frame_then_collection_resumes(self):
        from playwright.sync_api import expect,sync_playwright
        executable=os.environ.get('READER_TEST_BROWSER',r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        if not Path(executable).exists(): self.skipTest('Local Chrome unavailable')
        self.connectPatch.stop()
        profile=Path(self.temp.name)/'browser-profile'
        process=subprocess.Popen([executable,'--headless=new','--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=0','--user-data-dir='+str(profile),'--no-first-run','--no-default-browser-check','about:blank'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            port_file=profile/'DevToolsActivePort'
            for _ in range(100):
                if port_file.exists(): break
                time.sleep(.1)
            endpoint='http://127.0.0.1:'+port_file.read_text().splitlines()[0]
            with patch.dict(os.environ,{'READER_BROWSER_CDP':endpoint}),sync_playwright() as playwright:
                browser=playwright.chromium.connect_over_cdp(endpoint)
                source_context=browser.contexts[0]
                def serve(route):
                    if route.request.url.startswith('https://challenges.cloudflare.com/'):
                        route.fulfill(content_type='text/html',body="""<style>body{margin:0}input{position:absolute;left:10px;top:10px;width:24px;height:24px}</style>
                            <input type=checkbox aria-label='Synthetic approval'><script>document.querySelector('input').onchange=()=>parent.postMessage('test-approved','https://toki32.com');</script>""")
                    else:
                        route.fulfill(status=403,headers={'cf-mitigated':'challenge'},content_type='text/html',body="""
                            <title>Just a moment...</title><h1>Synthetic human confirmation</h1><div id=closed></div><script>
                            const root=document.querySelector('#closed').attachShadow({mode:'closed'});
                            root.innerHTML='<iframe src="https://challenges.cloudflare.com/test-only" style="position:absolute;left:100px;top:150px;width:300px;height:90px;border:0"></iframe>';
                            window.addEventListener('message',e=>{if(e.origin!=='https://challenges.cloudflare.com'||e.data!=='test-approved')return;
                            document.title='Test source ready';document.body.innerHTML='<div data-theme-novel-content><p>'+('테스트 본문 '.repeat(30))+'</p></div>';});</script>""")
                source_context.route('https://**/*',serve)
                source=source_context.new_page()
                for viewport,touch in (({'width':390,'height':844},True),({'width':1280,'height':900},False)):
                    control._active=None
                    source.goto('https://toki32.com/novel/999/91',wait_until='networkidle')
                    with reader_sync.connect() as db:
                        db.execute("UPDATE reader_jobs SET state='verification',run_token='test-run' WHERE episode_id='91'")
                        db.execute("UPDATE reader_episodes SET state='verification',paragraphs_json=NULL WHERE episode_id='91'")
                        db.execute("INSERT OR REPLACE INTO reader_paused_sources VALUES('toki32.com','checking')")
                    context=browser.new_context(viewport=viewport,has_touch=touch)
                    context.add_cookies([{'name':self.cookie.split('=',1)[0],'value':self.cookie.split('=',1)[1],'url':self.base}])
                    page=context.new_page()
                    page.goto(self.base+'/app?work=999')
                    page.locator('#work-message').get_by_role('button',name='사이트 확인',exact=True).click()
                    expect(page.locator('#site-verification')).to_be_visible()
                    expect(page.locator('#site-image')).to_be_visible()
                    expect(page.locator('#site-message')).to_contain_text('확인 대기 중')
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),viewport['width'])
                    artifacts=Path(__file__).with_name('build');artifacts.mkdir(exist_ok=True)
                    page.screenshot(path=str(artifacts/('site-check-mobile.png' if touch else 'site-check-desktop.png')))
                    # Coordinates of our synthetic checkbox, not detection/solving of any CAPTCHA.
                    point=page.locator('#site-image').evaluate('(el)=>({x:120*el.clientWidth/el.naturalWidth,y:170*el.clientHeight/el.naturalHeight})')
                    if touch: page.locator('#site-image').tap(position=point)
                    else: page.locator('#site-image').click(position=point)
                    expect(page.locator('#site-message')).to_contain_text('확인',timeout=10000)
                    expect(page.locator('#site-view')).to_be_hidden(timeout=10000)
                    with reader_sync.connect() as db:
                        self.assertEqual(db.execute("SELECT state FROM reader_jobs WHERE episode_id='91'").fetchone()[0],'queued')
                    worker=threading.Thread(target=reader_app.run_one_job,args=(reader_sync.connect,),daemon=True)
                    worker.start();worker.join(timeout=15)
                    self.assertFalse(worker.is_alive())
                    with reader_sync.connect() as db:
                        stored=db.execute("SELECT state,error FROM reader_episodes WHERE episode_id='91'").fetchone()
                        self.assertEqual(stored[0],'ready',dict(stored))
                        self.assertEqual(db.execute("SELECT position FROM progress WHERE work_id='999'").fetchone()[0],.6)
                    page.get_by_role('button',name='닫기',exact=True).click()
                    expect(page.locator('#site-verification')).not_to_be_visible()
                    context.close()
                source_context.unroute('https://**/*')
                browser.close()
        finally:
            if process.poll() is None: process.terminate()
            process.wait(timeout=10)


if __name__=='__main__': unittest.main()
