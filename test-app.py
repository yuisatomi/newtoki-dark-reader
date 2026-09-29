"""Offline end-to-end checks for the separate reader app and existing progress API."""

import ipaddress
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, urlopen


sys.path.insert(0, str(Path(__file__).parent / "server"))
import reader_app  # noqa: E402
import reader_crawl  # noqa: E402
import reader_sync  # noqa: E402
from reader_crawl import LockedChapter, NovelLinks, checked_url  # noqa: E402


class ReaderAppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        reader_sync.DB_PATH = str(Path(self.temp.name) / "progress.db")
        reader_sync.SYNC_TOKEN = "test-shared-token"
        reader_sync.ALLOWED_NETWORK = ipaddress.ip_network("127.0.0.0/8")
        db = sqlite3.connect(reader_sync.DB_PATH)
        try:
            db.execute("""CREATE TABLE progress(kind TEXT NOT NULL,work_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,position REAL NOT NULL,title TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL,
                PRIMARY KEY(kind,work_id))""")
            db.execute("INSERT INTO progress VALUES('novel','999','91',0.6,'기존 작품','old-device',1)")
            db.commit()
        finally:
            db.close()
        reader_sync.init_db()
        reader_sync.init_db()  # Additive migration must be safe on every service restart.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), reader_sync.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = ""
        self.list_fetcher = reader_app.collect_episode_list
        self.episode_fetcher = reader_app.browse_episode

    def tearDown(self):
        reader_app.collect_episode_list = self.list_fetcher
        reader_app.browse_episode = self.episode_fetcher
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, body=None, cookie=True, bearer=False, app_header=True):
        headers = {}
        if cookie and self.cookie:
            headers["Cookie"] = self.cookie
        if bearer:
            headers["Authorization"] = "Bearer " + reader_sync.SYNC_TOKEN
        if body is not None:
            headers["Content-Type"] = "application/json"
            if app_header:
                headers["X-Reader-App"] = "1"
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method="POST" if body is not None else "GET")
        try:
            response = urlopen(request, timeout=4)
        except HTTPError as exc:
            response = exc
        with response:
            content = response.read()
            return response.status, (json.loads(content) if content and path != "/app" else content), response.headers

    def login(self):
        status, result, headers = self.request("/app/api/login", {"token": reader_sync.SYNC_TOKEN})
        self.assertEqual((status, result["ok"]), (200, True))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def test_app_and_shared_progress(self):
        with self.assertRaises(ValueError):
            reader_sync.store_progress([])
        self.assertEqual(self.request("/health")[0], 200)
        status, old, _ = self.request("/v1/progress?kind=novel&work_id=999", bearer=True)
        self.assertEqual((status, old["progress"]["episode_id"], old["revision"]), (200, "91", 1))
        self.assertEqual(self.request("/app/api/work/999", cookie=False)[0], 401)
        self.assertEqual(self.request("/app/api/library", cookie=False)[0], 401)
        self.assertEqual(self.request("/app/api/episode/999/91", cookie=False)[0], 401)
        self.assertEqual(self.request("/app/api/login", {"token": "wrong"})[0], 401)
        self.login()
        self.assertEqual(self.request("/app/api/library", {"x": 1}, app_header=False)[0], 403)
        self.assertEqual(self.request("/app/api/works", {"url": "https://127.0.0.1/novel/1"})[0], 400)
        status, added, _ = self.request("/app/api/works", {"url": "https://newtoki1.org/novel/57458/2"})
        self.assertEqual((status, added["episode_id"]), (200, "2"))
        self.assertEqual(len(self.request("/app/api/library")[1]["works"]), 2)

        reader_app.collect_episode_list = lambda *_: ("시험 작품", [
            ("1", "1화", "https://newtoki1.org/novel/57458/1"),
            ("2", "2화", "https://newtoki1.org/novel/57458/2"),
            ("3", "3화", "https://newtoki1.org/novel/57458/3")])
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        status, work, _ = self.request("/app/api/work/57458")
        self.assertEqual((status, len(work["episodes"]), work["work"]["title"]), (200, 3, "시험 작품"))
        status, waiting, _ = self.request("/app/api/episode/57458/2")
        self.assertEqual((status, waiting["episode"]["state"]), (200, "missing"))

        def fake_episode(_, __, episode_id):
            if episode_id == "3":
                raise LockedChapter("잠긴 회차")
            return ["첫 문단", "둘째 문단"]

        reader_app.browse_episode = fake_episode
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertFalse(reader_app.run_one_job(reader_sync.connect))
        status, ready, _ = self.request("/app/api/episode/57458/2")
        self.assertEqual((status, ready["episode"]["paragraphs"]), (200, ["첫 문단", "둘째 문단"]))
        self.assertEqual(self.request("/app/api/episode/57458/3")[1]["episode"]["state"], "locked")

        status, saved, _ = self.request("/app/api/progress", {
            "work_id": "57458", "episode_id": "2", "position": 0.55, "expected_revision": 0})
        self.assertEqual((status, saved["revision"]), (200, 1))
        self.assertEqual(self.request("/app/api/progress", {
            "work_id": "57458", "episode_id": "1", "position": 0.1, "expected_revision": 0})[0], 409)
        status, synced, _ = self.request("/v1/progress?kind=novel&work_id=57458", bearer=True)
        self.assertEqual((status, synced["progress"]["episode_id"], synced["progress"]["position"]),
                         (200, "2", 0.55))
        self.assertEqual(len(self.request("/app/api/library")[1]["works"]), 2)
        self.assertEqual(self.request("/app/api/episode/57458/3/retry", {})[0], 409)

        def fail_episode(*_):
            raise RuntimeError("temporary source failure")

        reader_app.browse_episode = fail_episode
        self.request("/app/api/episode/57458/1")
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request("/app/api/episode/57458/1")[1]["episode"]["state"], "error")
        reader_app.browse_episode = lambda *_: ["복구한 본문"]
        self.assertEqual(self.request("/app/api/episode/57458/1/retry", {})[0], 200)
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request("/app/api/episode/57458/1")[1]["episode"]["paragraphs"], ["복구한 본문"])
        self.assertEqual(self.request("/app/api/episode/57458/1/retry", {})[0], 409)

        delete = Request(self.base + "/v1/progress?kind=novel&work_id=57458",
                         headers={"Authorization": "Bearer " + reader_sync.SYNC_TOKEN}, method="DELETE")
        with urlopen(delete, timeout=4) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.request("/app/api/library")[1]["works"]), 1)
        self.assertEqual(self.request("/app/api/episode/57458/2")[1]["episode"]["paragraphs"], ["첫 문단", "둘째 문단"])
        tombstone = self.request("/app/api/work/57458")[1]["progress"]
        self.assertEqual(tombstone["deleted"], 1)
        self.assertEqual(self.request("/app/api/progress", {
            "work_id": "57458", "episode_id": "2", "position": 0.6,
            "expected_revision": tombstone["revision"], "allow_rewind": True})[0], 200)
        self.assertEqual(len(self.request("/app/api/library")[1]["works"]), 2)
        self.assertEqual(self.request("/app/api/work/999/source", {"host": "newtoki1.org"})[0], 200)
        source = self.request("/app/api/work/999")[1]
        self.assertEqual((source["work"]["host"], source["list_job"]["state"]), ("newtoki1.org", "queued"))
        self.cookie = "reader_session=forged"
        self.assertEqual(self.request("/app/api/library")[0], 401)

    def test_list_parser_and_url_boundary(self):
        html = """<title>작품 - 사이트</title>
          <li class='list-item'><div class='wr-num'>1425</div><div class='wr-subject'>
          <a class='item-subject' href='/novel/57458/3'>번호 없는 제목</a></div></li>
          <a class='item-subject' href='/novel/57458/2'><span>2화</span></a>
          <a href='/novel/57458?epage=2'>2</a>
          <a href='/novel/other/1'>다른 작품</a>"""
        parser = NovelLinks("57458")
        parser.feed(html)
        self.assertEqual(parser.links, [("3", "1425화 · 번호 없는 제목"), ("2", "2화")])
        self.assertEqual(parser.pages, {2})
        toki = NovelLinks("57458")
        toki.feed("""<li class='novel-ep-row' data-ep='0'>
          <a class='novel-ep-link' href='/novel/57458/1'><span class='ne-num'>0화</span><span class='ne-title-wrap'><span class='ne-title'>프롤로그</span></span></a></li>
          <a class='not-item-subject' href='/novel/57458/9'>관련 링크</a>""")
        self.assertEqual(toki.links, [("1", "0화 · 프롤로그")])
        toki.feed("""<li class='novel-ep-row' data-ep='318'><a class='novel-ep-link' href='/novel/57458/318'>
          <span class='ne-num'>318화</span><span class='ne-title-wrap'><span class='ne-title'>318화</span><span>UP</span></span></a></li>""")
        self.assertEqual(toki.links[-1], ("318", "318화"))
        with self.assertRaises(ValueError):
            checked_url("https://127.0.0.1/novel/57458")
        with self.assertRaises(ValueError):
            checked_url("https://toki31.com/novel/57458")
        self.assertEqual(checked_url("https://toki32.com/novel/57458"), ("toki32.com", "57458", None))
        source = Request("https://newtoki1.org/novel/57458")
        redirect = reader_crawl.SameSiteRedirect("57458")
        self.assertEqual(redirect.redirect_request(
            source, None, 302, "Found", {}, "https://toki32.com/novel/57458").full_url,
            "https://toki32.com/novel/57458")
        with self.assertRaises(ValueError):
            redirect.redirect_request(source, None, 302, "Found", {}, "https://example.com/novel/57458")

    def test_readd_deleted_work(self):
        self.login()
        for work_id, saved_episode in (("999", "91"), ("888", "")):
            request = Request(self.base + f"/v1/progress?kind=novel&work_id={work_id}",
                              headers={"Authorization": "Bearer " + reader_sync.SYNC_TOKEN}, method="DELETE")
            with urlopen(request, timeout=4):
                pass
            self.assertFalse(any(row["work_id"] == work_id for row in self.request("/app/api/library")[1]["works"]))
            self.assertEqual(self.request("/app/api/works", {"url": f"https://newtoki1.org/novel/{work_id}"})[0], 200)
            self.assertTrue(any(row["work_id"] == work_id for row in self.request("/app/api/library")[1]["works"]))
            progress = self.request(f"/app/api/work/{work_id}")[1]["progress"]
            self.assertEqual((progress["deleted"], progress["episode_id"]), (0, saved_episode))

    def test_source_proxy_is_local_and_used_by_both_fetchers(self):
        class Probe(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                self.server.connect_target = self.path
                self.send_error(502)

            def log_message(self, *_):
                pass

        probe = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
        thread = threading.Thread(target=probe.serve_forever, daemon=True)
        thread.start()
        try:
            proxy = f"http://127.0.0.1:{probe.server_port}"
            with patch.dict(os.environ, {"READER_SOURCE_PROXY": proxy}):
                opener = reader_crawl.source_opener(reader_crawl.SameSiteRedirect("57458"))
                self.assertEqual(next(h for h in opener.handlers if isinstance(h, ProxyHandler)).proxies,
                                 {"https": proxy})
                with self.assertRaisesRegex(URLError, "502"):
                    reader_crawl.fetch_index_page("https://newtoki1.org/novel/57458", "57458")
                self.assertEqual(probe.connect_target, "newtoki1.org:443")
                browser = Mock()
                browser.new_context.side_effect = RuntimeError("stop before network")
                with self.assertRaisesRegex(RuntimeError, "stop before network"):
                    reader_crawl.extract_episode(browser, "https://newtoki1.org/novel/57458/2", "57458", "2")
                browser.new_context.assert_called_once_with(service_workers="block", proxy={"server": proxy})
        finally:
            probe.shutdown()
            probe.server_close()
            thread.join(timeout=3)
        with patch.dict(os.environ, {"READER_SOURCE_PROXY": "http://192.168.100.15:8081"}):
            with self.assertRaises(reader_crawl.CrawlUnavailable):
                reader_crawl.source_proxy()

    def test_prefetch_window_is_deduplicated(self):
        with reader_sync.connect() as db:
            db.execute("INSERT INTO reader_works VALUES('9000','newtoki1.org','시험',1)")
            for number in range(1, 6):
                db.execute("""INSERT INTO reader_episodes
                    (work_id,episode_id,ordinal,title,source_url,state,updated_at)
                    VALUES('9000',?,?,?,?,?,1)""",
                    (str(number), number, f'{number}화', f'https://newtoki1.org/novel/9000/{number}',
                     'ready' if number == 3 else 'missing'))
            reader_app.queue_prefetch(db, '9000', '2')
            reader_app.queue_prefetch(db, '9000', '2')
            jobs = db.execute("SELECT episode_id FROM reader_jobs WHERE work_id='9000' ORDER BY episode_id").fetchall()
        self.assertEqual([row['episode_id'] for row in jobs], ['2', '4'])

    def test_verification_pauses_source_until_authenticated_resume(self):
        self.login()
        for work_id, host in (("11", "toki32.com"), ("12", "toki32.com"), ("13", "newtoki1.org")):
            self.request("/app/api/works", {"url": f"https://{host}/novel/{work_id}"})
        calls = []
        def fetch(url, work_id):
            calls.append(work_id)
            if work_id == "11":
                raise reader_crawl.HumanVerificationRequired()
            return "시험", [("1", "1화", url + "/1")]
        reader_app.collect_episode_list = fetch
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(calls, ["11", "13"])
        self.assertTrue(self.request("/app/api/work/12")[1]["verification"])
        self.request("/app/api/work/11/refresh", {})
        self.assertFalse(reader_app.run_one_job(reader_sync.connect))
        reader_app.init_db(reader_sync.connect)  # The pause survives service restart.
        self.assertFalse(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request("/app/api/work/11/resume", {}, cookie=False)[0], 401)
        self.assertEqual(self.request("/app/api/work/11/resume", {}, app_header=False)[0], 403)
        reader_app.collect_episode_list = lambda url, _: ("시험", [("1", "1화", url + "/1")])
        self.assertEqual(self.request("/app/api/work/11/resume", {})[0], 200)
        while reader_app.run_one_job(reader_sync.connect):
            pass
        self.request("/app/api/episode/11/1")
        reader_app.browse_episode = Mock(side_effect=reader_crawl.HumanVerificationRequired())
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request("/app/api/episode/11/1")[1]["episode"]["state"], "verification")
        self.assertFalse(reader_app.run_one_job(reader_sync.connect))
        self.request("/app/api/work/12/resume", {})
        reader_app.browse_episode = lambda *_: ["확인 후 본문"]
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request("/app/api/episode/11/1")[1]["episode"]["paragraphs"], ["확인 후 본문"])
        with patch.object(reader_crawl, "source_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://toki32.com/novel/11", 403,
                "Forbidden", {"cf-mitigated": "challenge"}, None)
            with self.assertRaises(reader_crawl.HumanVerificationRequired):
                reader_crawl.fetch_index_page("https://toki32.com/novel/11", "11")
        for endpoint in ("http://192.168.1.1:9222", "http://user@127.0.0.1:9222", "http://127.0.0.1:9222/path"):
            with patch.dict(os.environ, {"READER_BROWSER_CDP": endpoint}):
                with self.assertRaises(reader_crawl.CrawlUnavailable):
                    reader_crawl.browser_endpoint()

    def test_shared_browser_session_and_manual_verification(self):
        try:
            import playwright.sync_api
        except ImportError:
            self.skipTest("Playwright is not installed")
        executable = os.environ.get("READER_TEST_BROWSER") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        if not Path(executable).exists():
            self.skipTest("A local Chromium browser is not available")
        profile = Path(self.temp.name) / "browser-profile"
        process = subprocess.Popen([executable, "--headless=new", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0", "--user-data-dir=" + str(profile), "--no-first-run",
            "--no-default-browser-check", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = profile / "DevToolsActivePort"
            for _ in range(100):
                if port_file.exists():
                    break
                time.sleep(0.1)
            port = port_file.read_text().splitlines()[0]
            with patch.dict(os.environ, {"READER_BROWSER_CDP": "http://127.0.0.1:" + port}):
                with reader_crawl.source_browser() as browser:
                    context = browser.contexts[0]
                    context.route("**/*", lambda route: route.fulfill(status=403,
                        headers={"cf-mitigated": "challenge"}, body="<title>Just a moment</title>"))
                    with self.assertRaises(reader_crawl.HumanVerificationRequired):
                        reader_crawl.fetch_index_page("https://toki32.com/novel/11", "11", browser)
                    self.assertTrue(any(p.url == "https://toki32.com/novel/11" for p in context.pages))
                    context.unroute("**/*")
                    # Synthetic approval, not an attempt to solve a real CAPTCHA.
                    context.add_cookies([{"name": "reader-test-approved", "value": "yes", "domain": "toki32.com", "path": "/", "secure": True}])
                self.assertIsNone(process.poll(), "Disconnect must not close the verification browser")
                with reader_crawl.source_browser() as browser:
                    context = browser.contexts[0]
                    verification_page = next(p for p in context.pages if p.url == "https://toki32.com/novel/11")
                    for page in list(context.pages):
                        if page != verification_page:
                            page.close()
                    seen = []
                    def serve(route):
                        if route.request.url == "https://toki.peertrk.com/test.js":
                            route.fulfill(content_type="text/javascript", body="""
                              document.querySelector('button').onclick = function() {
                                const a = document.createElement('a'); a.className = 'novel-ep-link';
                                a.href = '/novel/11/0'; a.innerHTML = '<span class="ne-title">0화</span>';
                                document.querySelector('a.novel-ep-link').replaceWith(a); this.remove();
                              };""")
                            return
                        if route.request.resource_type != "document":
                            route.abort()
                            return
                        seen.append(route.request.all_headers().get("cookie", ""))
                        body = ("<title>시험 작품</title><a class='novel-ep-link' href='/novel/11/1'><span class='ne-title'>1화</span></a>"
                            "<button>이전 회차 더 보기</button><script src='https://toki.peertrk.com/test.js'></script>"
                            if route.request.url.endswith("/11") else
                            "<div data-theme-novel-content></div><script>document.querySelector('div').attachShadow({mode:'closed'}).innerHTML='<p>" + "시험 본문 " * 25 + "</p>';</script>")
                        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
                    context.route("**/*", serve)
                    parser, _ = reader_crawl.fetch_index_page("https://toki32.com/novel/11", "11", browser)
                    self.assertEqual(parser.links, [("1", "1화"), ("0", "0화")])
                    self.assertFalse(verification_page.is_closed(), "The user's verified tab must stay open")
                    paragraphs = reader_crawl.extract_episode(browser, "https://toki32.com/novel/11/1", "11", "1")
                    self.assertFalse(verification_page.is_closed())
                    self.assertGreater(len(paragraphs[0]), 80)
                    self.assertEqual(len(seen), 2)
                    self.assertTrue(all("reader-test-approved=yes" in cookie for cookie in seen))
                    context.unroute("**/*")
                    browser.close()  # Only the test owner closes its dedicated browser.
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)

    def test_prefetch_jobs_run_in_chapter_order(self):
        with reader_sync.connect() as db:
            db.execute("CREATE INDEX job_order_probe ON reader_jobs(state,updated_at,episode_id)")
            db.execute("INSERT INTO reader_works VALUES('9000','newtoki1.org','시험',1)")
            for ordinal, episode_id in enumerate(('30', '20', '10'), 1):
                db.execute("""INSERT INTO reader_episodes
                    (work_id,episode_id,ordinal,title,source_url,updated_at)
                    VALUES('9000',?,?,?,?,1)""",
                           (episode_id, ordinal, f'{ordinal}화', f'https://newtoki1.org/novel/9000/{episode_id}'))
            with patch.object(reader_app.time, "time", return_value=1000):
                reader_app.queue_prefetch(db, '9000', '30')
        order = []
        reader_app.browse_episode = lambda _, __, episode_id: order.append(episode_id) or ['본문']
        while reader_app.run_one_job(reader_sync.connect):
            pass
        self.assertEqual(order, ['30', '20', '10'])

    def test_browser_reader(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        engine = os.environ.get("READER_TEST_ENGINE", "chromium")
        executable = os.environ.get("READER_TEST_BROWSER") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        if engine != "firefox" and not Path(executable).exists():
            self.skipTest("A local Chromium browser is not available")
        with reader_sync.connect() as db:
            db.execute("INSERT INTO reader_works VALUES('999','newtoki1.org','기존 작품',1)")
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                VALUES('999','91',1,'91화','https://newtoki1.org/novel/999/91',?,'ready',1)""",
                       (json.dumps(["첫 문단", "둘째 문단"], ensure_ascii=False),))
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                VALUES('999','92',2,'92화','https://newtoki1.org/novel/999/92',?,'ready',1)""",
                       (json.dumps(["다음 회차 본문"], ensure_ascii=False),))
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,error,updated_at)
                VALUES('999','93',3,'93화','https://newtoki1.org/novel/999/93',NULL,'error','수집 실패',1)""")
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,error,updated_at)
                VALUES('999','94',4,'94화','https://newtoki1.org/novel/999/94',NULL,'locked','포인트 필요',1)""")
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,updated_at)
                VALUES('999','95',5,'95화','https://newtoki1.org/novel/999/95',1)""")
            db.execute("INSERT INTO reader_works VALUES('1000','newtoki1.org','새 작품',0)")
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                VALUES('1000','1',1,'1화','https://newtoki1.org/novel/1000/1',?,'ready',1)""",
                       (json.dumps(["새 작품 첫 문단"], ensure_ascii=False),))
            db.execute("INSERT INTO progress(kind,work_id,episode_id,position,title,device_id,updated_at) VALUES('novel','2000','1',0.55,'긴 작품','other-device',0)")
            db.execute("INSERT INTO reader_works VALUES('2000','newtoki1.org','긴 작품',0)")
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                VALUES('2000','1',1,'1화','https://newtoki1.org/novel/2000/1',?,'ready',1)""",
                       (json.dumps(["긴 문단 " + str(i) + " " + "내용 " * 30 for i in range(80)], ensure_ascii=False),))
            db.execute("INSERT INTO progress(kind,work_id,episode_id,position,title,device_id,updated_at) VALUES('novel','3000','1',0.8,'이동 시험','other-device',0)")
            db.execute("INSERT INTO reader_works VALUES('3000','newtoki1.org','이동 시험',0)")
            for number in (1, 2):
                db.execute("""INSERT INTO reader_episodes
                    (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                    VALUES('3000',?,?,?,?,?,'ready',1)""",
                           (str(number), number, f'{number}화', f'https://newtoki1.org/novel/3000/{number}',
                            json.dumps([f'{number}화 문단 {i} ' + '내용 ' * 30 for i in range(80)], ensure_ascii=False)))
            db.execute("INSERT INTO progress(kind,work_id,episode_id,position,title,device_id,updated_at) VALUES('novel','4000','1',0.5,'다른 작품','other-device',0)")
            db.execute("INSERT INTO reader_works VALUES('4000','newtoki1.org','다른 작품',0)")
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                VALUES('4000','93',1,'93화','https://newtoki1.org/novel/4000/93',?,'ready',1)""",
                       (json.dumps(["다른 작품의 93화 본문"], ensure_ascii=False),))
            db.execute("INSERT INTO progress(kind,work_id,episode_id,position,title,device_id,updated_at) VALUES('novel','5000','1',0.1,'즉시 이동','other-device',0)")
            db.execute("INSERT INTO reader_works VALUES('5000','newtoki1.org','즉시 이동',0)")
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                VALUES('5000','1',1,'1화','https://newtoki1.org/novel/5000/1',?,'ready',1)""",
                       (json.dumps(["긴 문단 " + str(i) + " " + "내용 " * 30 for i in range(80)], ensure_ascii=False),))
            db.execute("INSERT INTO reader_works VALUES('6000','newtoki1.org','재추가 작품',0)")
            db.execute("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                VALUES('6000','1',1,'1화','https://newtoki1.org/novel/6000/1',?,'ready',1)""",
                       (json.dumps(["재추가 본문"], ensure_ascii=False),))
            db.execute("""INSERT INTO progress(kind,work_id,episode_id,position,title,device_id,updated_at,deleted,revision)
                VALUES('novel','6000','',0,'재추가 작품','other-device',0,0,2)""")
        with sync_playwright() as playwright:
            browser = (playwright.firefox.launch(headless=True) if engine == "firefox" else
                       playwright.chromium.launch(headless=True, executable_path=executable))
            try:
                page = browser.new_page(viewport={"width": int(os.environ.get("READER_TEST_WIDTH", "390")), "height": 800})
                page.goto(self.base + "/app")
                page.locator("#token").fill(reader_sync.SYNC_TOKEN)
                page.locator("#login-form button").click()
                page.locator("#library-list .item button").first.click(timeout=5000)
                expect(page.locator("#work-title")).to_have_text("기존 작품")
                page.locator("#content p").first.wait_for(timeout=5000)
                self.assertEqual(page.locator("#content p").count(), 2)
                self.assertFalse(page.locator("#login").is_visible())
                page.locator("#show-settings").click()
                page.locator("[data-theme=paper]").click()
                self.assertEqual(page.evaluate("getComputedStyle(document.documentElement).colorScheme"), "light")
                page.locator("#warm").evaluate("el => {el.value='0';el.dispatchEvent(new Event('input'))}")
                expect(page.locator("#warm-value")).to_have_text("0%")
                page.locator("#close-settings").click()
                page.locator("#next").click()
                page.locator("#content p").first.wait_for(timeout=5000)
                expect(page.locator("#content p").first).to_have_text("다음 회차 본문", timeout=5000)
                for _ in range(50):
                    if self.request("/v1/progress?kind=novel&work_id=999", bearer=True)[1]["progress"]["episode_id"] == "92":
                        break
                    time.sleep(0.1)
                page.locator("#previous").click()
                expect(page.locator("#content p").first).to_have_text("첫 문단", timeout=5000)
                page.mouse.wheel(0, 1000)
                page.wait_for_timeout(900)
                self.assertEqual(self.request("/v1/progress?kind=novel&work_id=999", bearer=True)[1]["progress"]["episode_id"], "92")
                page.locator("#show-list").click()
                expect(page.locator("#episode-list button").first).to_have_text("91화")
                page.locator("#back-library").click()
                page.locator("#library-list .item").filter(has_text="새 작품").locator("button").click()
                page.locator("#episode-list button").first.click(timeout=5000)
                expect(page.locator("#content p").first).to_have_text("새 작품 첫 문단", timeout=5000)
                for _ in range(50):
                    progress = self.request("/v1/progress?kind=novel&work_id=1000", bearer=True)[1]["progress"]
                    if progress:
                        break
                    time.sleep(0.1)
                self.assertEqual(progress["episode_id"], "1")
                direct = browser.new_page(viewport={"width": 390, "height": 800})
                direct.goto(self.base + "/app?work=999&ep=92")
                direct.locator("#token").fill(reader_sync.SYNC_TOKEN)
                direct.locator("#login-form button").click()
                expect(direct.locator("#content p").first).to_have_text("다음 회차 본문", timeout=5000)
                direct.locator("#next").click()
                expect(direct.locator("#reader-message")).to_have_text("수집 실패", timeout=5000)
                self.assertEqual(direct.locator("#content a").get_attribute("href"),
                                 "https://newtoki1.org/novel/999/93")
                direct.locator("#next").click()
                expect(direct.locator("#reader-message")).to_have_text("포인트 필요", timeout=5000)
                self.assertEqual(direct.locator("#content a").get_attribute("href"),
                                 "https://newtoki1.org/novel/999/94")
                direct.locator("#next").click()
                expect(direct.locator("#reader-message")).to_have_text("본문 수집 중…", timeout=5000)
                reader_app.browse_episode = lambda *_: ["지연 수집 본문"]
                self.assertTrue(reader_app.run_one_job(reader_sync.connect))
                expect(direct.locator("#content p").first).to_have_text("지연 수집 본문", timeout=6000)
                direct.goto(self.base + "/app?work=2000&ep=1")
                direct.locator("#content p").first.wait_for(timeout=5000)
                for _ in range(50):
                    ratio = direct.evaluate("scrollY / Math.max(1, document.documentElement.scrollHeight - innerHeight)")
                    if 0.45 < ratio < 0.65:
                        break
                    time.sleep(0.1)
                self.assertTrue(0.45 < ratio < 0.65, f"restored ratio: {ratio}")
                direct.mouse.wheel(0, 30000)
                for _ in range(50):
                    advanced = self.request("/v1/progress?kind=novel&work_id=2000", bearer=True)[1]["progress"]
                    if advanced["position"] > 0.95:
                        break
                    time.sleep(0.1)
                self.assertGreater(advanced["position"], 0.95)
                second_device = browser.new_page(viewport={"width": 390, "height": 800})
                second_device.goto(self.base + "/app?work=2000&ep=1")
                second_device.locator("#token").fill(reader_sync.SYNC_TOKEN)
                second_device.locator("#login-form button").click()
                second_device.locator("#content p").first.wait_for(timeout=5000)
                for _ in range(50):
                    ratio = second_device.evaluate("scrollY / Math.max(1, document.documentElement.scrollHeight - innerHeight)")
                    if ratio > 0.9:
                        break
                    time.sleep(0.1)
                self.assertGreater(ratio, 0.9)
                direct.goto(self.base + "/app?work=3000&ep=1")
                direct.locator("#content p").first.wait_for(timeout=5000)
                for _ in range(50):
                    ratio = direct.evaluate("scrollY / Math.max(1, document.documentElement.scrollHeight - innerHeight)")
                    if ratio > 0.7:
                        break
                    time.sleep(0.1)
                self.assertGreater(ratio, 0.7)
                direct.evaluate("() => {scrollTo(0,document.documentElement.scrollHeight);document.querySelector('#next').click()}")
                expect(direct.locator("#content p").first).to_contain_text("2화 문단", timeout=5000)
                for _ in range(50):
                    next_progress = self.request("/v1/progress?kind=novel&work_id=3000", bearer=True)[1]["progress"]
                    if next_progress["episode_id"] == "2":
                        break
                    time.sleep(0.1)
                self.assertEqual(next_progress["episode_id"], "2")
                self.assertLess(next_progress["position"], 0.05, "새 회차에 이전 회차 위치가 저장됐습니다.")
                direct.locator("#previous").click()
                expect(direct.locator("#content p").first).to_contain_text("1화 문단", timeout=5000)
                for _ in range(30):
                    ratio = direct.evaluate("scrollY / Math.max(1, document.documentElement.scrollHeight - innerHeight)")
                    if ratio > 0.95:
                        break
                    time.sleep(0.1)
                self.assertGreater(ratio, 0.95, "즉시 회차 이동 후 이전 회차 위치가 복원되지 않았습니다.")
                direct.goto(self.base + "/app?work=999&ep=92")
                direct.locator("#content p").first.wait_for(timeout=5000)
                direct.locator("#next").click()
                expect(direct.locator("#reader-message")).to_have_text("수집 실패", timeout=5000)
                direct.locator("#show-list").click()
                direct.locator("#back-library").click()
                direct.locator("#add-url").fill("https://newtoki1.org/novel/4000/93")
                direct.locator("#add-work").click()
                expect(direct.locator("#content p").first).to_have_text("다른 작품의 93화 본문", timeout=5000)
                direct.wait_for_timeout(800)
                other_progress = self.request("/v1/progress?kind=novel&work_id=4000", bearer=True)[1]["progress"]
                self.assertEqual(other_progress["episode_id"], "1", "다른 작품의 저장 회차가 자동으로 덮였습니다.")
                direct.goto(self.base + "/app?work=5000&ep=1")
                direct.locator("#content p").first.wait_for(timeout=5000)
                direct.wait_for_timeout(150)
                direct.evaluate("() => {scrollTo(0,document.documentElement.scrollHeight);document.querySelector('#show-list').click()}")
                expect(direct.locator("#episode-list button").first).to_be_visible(timeout=5000)
                for _ in range(20):
                    last = self.request("/v1/progress?kind=novel&work_id=5000", bearer=True)[1]["progress"]
                    if last["position"] > 0.95:
                        break
                    time.sleep(0.1)
                self.assertGreater(last["position"], 0.95, "목록으로 즉시 이동할 때 마지막 위치가 저장되지 않았습니다.")
                with reader_sync.connect() as db:
                    db.execute("UPDATE progress SET episode_id='1',position=0.4,revision=revision+1 "
                               "WHERE kind='novel' AND work_id='3000'")
                direct.goto(self.base + "/app?work=3000&ep=2")
                expect(direct.locator("#content p").first).to_contain_text("2화 문단", timeout=5000)
                for _ in range(50):
                    advanced = self.request("/v1/progress?kind=novel&work_id=3000", bearer=True)[1]["progress"]
                    if advanced["episode_id"] == "2":
                        break
                    time.sleep(0.1)
                self.assertEqual(advanced["episode_id"], "2", "뒤 회차를 직접 열었는데 서버 기록이 갱신되지 않았습니다.")
                direct.goto(self.base + "/app?work=6000&ep=1")
                expect(direct.locator("#content p").first).to_have_text("재추가 본문", timeout=5000)
                for _ in range(50):
                    restored = self.request("/v1/progress?kind=novel&work_id=6000", bearer=True)[1]["progress"]
                    if restored["episode_id"] == "1":
                        break
                    time.sleep(0.1)
                self.assertEqual(restored["episode_id"], "1", "빈 저장 회차에서 처음 읽은 회차를 기록하지 못했습니다.")
                with reader_sync.connect() as db:
                    db.execute("INSERT INTO reader_paused_sources VALUES('newtoki1.org','서버 브라우저에서 사람 확인이 필요합니다.')")
                direct.goto(self.base + "/app?work=999")
                expect(direct.locator("#work-message button")).to_have_text("확인 완료 · 재개")
                direct.locator("#episode-list button").first.click()
                expect(direct.locator("#content p").first).to_have_text("첫 문단")
                direct.locator("#next").click()
                direct.locator("#next").click()
                expect(direct.locator("#reader-message button")).to_have_text("확인 완료 · 재개")
                direct.locator("#reader-message button").click()
                expect(direct.locator("#reader-message")).to_have_text("수집 실패")
                with reader_sync.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM reader_paused_sources").fetchone()[0], 0)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
