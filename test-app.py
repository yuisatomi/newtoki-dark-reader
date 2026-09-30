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
            return response.status, (json.loads(content) if content and path not in ('/app','/app/manage') else content), response.headers

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
        self.assertEqual(self.request("/app/api/episode/57458/1")[1]["episode"]["state"], "missing")
        for _ in range(3):
            with reader_sync.connect() as db:
                db.execute("UPDATE reader_jobs SET updated_at=0 WHERE work_id='57458' AND episode_id='1'")
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

    def test_resource_memory_limits_and_unavailable(self):
        mib=1024*1024
        files={'meminfo':'MemTotal: 1048576 kB\nMemAvailable: 786432 kB\nSwapTotal: 524288 kB\nSwapFree: 512000 kB\n',
               'memory.current':str(800*mib),'memory.max':'max','memory.swap.current':str(12*mib),'memory.swap.max':'max'}
        def read(path,*args,**kwargs):
            if path.name not in files:raise FileNotFoundError(path.name)
            return files[path.name]
        with patch.object(Path,'read_text',read):
            memory=reader_app.memory_usage()
            self.assertEqual((memory['total'],memory['used'],memory['source']),(1024*mib,256*mib,'meminfo'))
            self.assertEqual((memory['swap_total'],memory['swap_used']),(512*mib,12*mib))
            self.assertEqual(memory['cache_inclusive'],800*mib)
            files.update({'memory.max':str(512*mib),'memory.current':str(400*mib),
                          'memory.swap.max':'0','memory.swap.current':'0'})
            memory=reader_app.memory_usage()
            self.assertEqual((memory['total'],memory['used'],memory['available'],memory['source']),(512*mib,400*mib,112*mib,'cgroup'))
            self.assertEqual((memory['swap_total'],memory['swap_used']),(0,0))
            files['meminfo']='MemTotal: 1048576 kB\n'
            files['memory.max']='max'
            files.pop('memory.swap.current')
            files.pop('memory.swap.max')
            memory=reader_app.memory_usage()
            self.assertIsNone(memory['used'])
            self.assertIsNone(memory['swap_used'])
        with patch.object(Path,'read_text',side_effect=PermissionError):
            memory=reader_app.memory_usage()
            self.assertIsNone(memory['total'])
            self.assertIsNone(memory['used'])

    def test_authenticated_resource_snapshot(self):
        path='/app/api/manage/resources'
        self.assertEqual(self.request(path,cookie=False)[0],401)
        self.login()
        status,info,headers=self.request(path)
        self.assertEqual(status,200)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertGreater(info['sampled_at'],0)
        self.assertGreater(info['disk']['total'],0)
        self.assertGreater(info['database']['main'],0)
        self.assertNotIn(reader_sync.DB_PATH,json.dumps(info),'Do not expose filesystem paths')
        with reader_sync.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE resource_test(data BLOB)')
            db.execute('INSERT INTO resource_test VALUES(zeroblob(40000))')
            db.commit()
            info=reader_app.resource_snapshot(db)
            self.assertGreater(info['database']['wal'],0)
            self.assertGreater(info['database']['shm'],0)
            self.assertEqual(info['database']['total'],sum(info['database'][key] for key in ('main','wal','shm','journal')))
            db.execute('DELETE FROM resource_test')
            db.commit()
            with patch.object(reader_app.shutil,'disk_usage',side_effect=PermissionError):
                info=reader_app.resource_snapshot(db)
                self.assertIsNone(info['disk'])
                self.assertGreater(info['database']['reusable'],0)
            self.assertEqual(db.execute('SELECT episode_id,position FROM progress').fetchone()[:],('91',.6))
        with sqlite3.connect(':memory:') as db:
            info=reader_app.resource_snapshot(db)
            self.assertIsNone(info['disk'])
            self.assertIsNone(info['database'])

    def test_manage_collection_controls_and_capacity(self):
        self.login()
        self.request('/app/api/work/999/source', {'host':'newtoki1.org'})
        reader_app.collect_episode_list=lambda *_: ('시험 작품', [(str(i),f'{i}화',f'https://newtoki1.org/novel/999/{i}') for i in range(91,96)])
        reader_app.run_one_job(reader_sync.connect)
        endpoint='/app/api/manage/settings'
        settings={'paused':0,'prefetch':0,'cache_limit_mb':1}
        for invalid in ({'prefetch':201},{'prefetch':-1},{'prefetch':True},{'prefetch':1.5},{'cache_limit_mb':-1},{'paused':True}):
            self.assertEqual(self.request(endpoint,{'work_id':'999','settings':invalid})[0],400)
        self.assertEqual(self.request(endpoint,{'work_id':'999','settings':settings},cookie=False)[0],401)
        self.assertEqual(self.request(endpoint,{'work_id':'999','settings':settings})[0],200)
        self.request('/app/api/episode/999/91')
        with reader_sync.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reader_jobs WHERE episode_id!=''").fetchone()[0],1)
        settings['paused']=1
        self.request(endpoint,{'work_id':'999','settings':settings})
        self.assertTrue(self.request('/app/api/episode/999/92')[1]['paused'])
        self.assertFalse(reader_app.run_one_job(reader_sync.connect))
        reader_sync.init_db()
        self.assertEqual(self.request('/app/api/manage')[1]['works'][0]['paused'],1)
        settings.update(paused=0,prefetch=2)
        self.request(endpoint,{'work_id':'999','settings':settings})
        self.request('/app/api/episode/999/91')
        with reader_sync.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reader_jobs WHERE episode_id!=''").fetchone()[0],3)
            db.execute("DELETE FROM reader_jobs WHERE episode_id IN ('92','93')")
        reader_app.browse_episode=lambda *_: ['x'*(1024*1024)]
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request('/app/api/episode/999/91')[1]['job']['state'],'capacity')
        with reader_sync.connect() as db:
            self.assertEqual(reader_app.cache_bytes(db,'999'),0)
            db.execute("DELETE FROM reader_jobs WHERE episode_id IN ('92','93')")
        settings.update(cache_limit_mb=2,prefetch=0)
        self.request(endpoint,{'work_id':'999','settings':settings})
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request('/app/api/episode/999/91')[1]['episode']['state'],'ready')
        settings['cache_limit_mb']=1
        self.request(endpoint,{'work_id':'999','settings':settings})
        self.request('/app/api/episode/999/92')
        with patch.object(reader_app,'browse_episode',side_effect=AssertionError('Full cache must not fetch')):
            self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        with reader_sync.connect() as db:
            self.assertGreater(reader_app.cache_bytes(db,'999'),1024*1024,'Lowering limit must not delete stored text')
            db.execute("UPDATE reader_episodes SET state='error' WHERE episode_id='93'")
            db.execute("UPDATE reader_episodes SET state='locked' WHERE episode_id='94'")
            reader_app.enqueue(db,'999','94')
            db.execute("UPDATE reader_jobs SET state='done' WHERE episode_id='94'")
            db.execute("UPDATE reader_jobs SET state='error',attempts=4 WHERE episode_id=''")
        self.assertEqual(self.request('/app/api/manage/retry',{'work_id':'999'})[0],200)
        with reader_sync.connect() as db:
            self.assertEqual(db.execute("SELECT state,attempts FROM reader_jobs WHERE episode_id=''").fetchone()[:],('queued',0))
            self.assertEqual(db.execute("SELECT state FROM reader_episodes WHERE episode_id='93'").fetchone()[0],'missing')
            self.assertEqual(db.execute("SELECT state FROM reader_episodes WHERE episode_id='94'").fetchone()[0],'locked')
            self.assertEqual(db.execute("SELECT state FROM reader_jobs WHERE episode_id='92'").fetchone()[0],'capacity')

    def test_manage_backup_restore_and_cache_clear(self):
        self.login()
        self.request('/app/api/work/999/source', {'host':'newtoki1.org'})
        reader_app.collect_episode_list=lambda *_: ('백업 작품', [('91','91화','https://newtoki1.org/novel/999/91')])
        reader_app.run_one_job(reader_sync.connect)
        self.request('/app/api/episode/999/91')
        reader_app.browse_episode=lambda *_: ['백업 본문'*5000]
        reader_app.run_one_job(reader_sync.connect)
        url='/app/api/manage/backup?work_id=999'
        self.assertEqual(self.request(url,cookie=False)[0],401)
        status, backup, _=self.request(url)
        self.assertEqual(status,200)
        self.assertGreater(len(json.dumps(backup)),16384)
        self.assertEqual(set(backup),{'format','version','work','settings','progress','episodes'})
        self.assertNotIn('device_id',backup['progress'])
        restore='/app/api/manage/restore'
        self.assertEqual(self.request(restore,{'confirm':True},cookie=False)[0],401)
        self.assertEqual(self.request(restore,{'confirm':True},app_header=False)[0],403)
        self.assertEqual(self.request(restore,{'backup':backup})[0],400)
        # Validate the complete file before any row can be changed.
        invalids=[]
        for key,value in [('source_url','http://127.0.0.1/novel/999/91'),('ordinal',True),('paragraphs',['ok',{}])]:
            invalid=json.loads(json.dumps(backup));invalid['episodes'][0][key]=value;invalids.append(invalid)
        invalid=json.loads(json.dumps(backup));invalid['episodes']*=2;invalids.append(invalid)
        invalid=json.loads(json.dumps(backup));invalid['progress']['position']=float('nan');invalids.append(invalid)
        for invalid in invalids:
            self.assertEqual(self.request(restore,{'backup':invalid,'confirm':True})[0],400)
            self.assertEqual(self.request(url)[1],backup)
        before=self.request('/v1/progress?kind=novel&work_id=999',bearer=True)[1]['progress']
        reader_sync.store_progress({'kind':'webtoon','work_id':'999','episode_id':'2','position':.7})
        clear='/app/api/manage/cache-clear'
        self.assertEqual(self.request(clear,{'work_id':'999'})[0],400)
        self.assertEqual(self.request(clear,{'work_id':'999','confirm':True})[0],200)
        self.assertEqual(self.request('/v1/progress?kind=novel&work_id=999',bearer=True)[1]['progress'],before)
        episode=self.request('/app/api/episode/999/91')[1]
        self.assertEqual((episode['episode']['paragraphs'],episode['paused']),(None,True))
        self.assertFalse(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request(restore,{'backup':backup,'confirm':True})[0],200)
        restored=self.request(url)[1]
        self.assertEqual(restored['episodes'],backup['episodes'])
        self.assertEqual(restored['progress'],backup['progress'])
        self.assertEqual(restored['settings']['paused'],1)
        self.assertEqual(self.request('/app/api/me')[1]['authenticated'],True)
        self.assertEqual(self.request('/v1/progress?kind=webtoon&work_id=999',bearer=True)[1]['progress']['position'],.7)
        self.assertEqual(reader_sync.store_progress({'kind':'novel','work_id':'999','episode_id':'91','position':.9,'expected_revision':before['revision']})[0],409)
        # Restoring a work without a saved position must leave it visible and readable.
        backup['progress']=None
        self.assertEqual(self.request(restore,{'backup':backup,'confirm':True})[0],200)
        self.assertIn('999',[x['work_id'] for x in self.request('/app/api/library')[1]['works']])
        self.assertIsNone(self.request(url)[1]['progress'])

    def test_cache_clear_and_restore_discard_inflight_result(self):
        self.login()
        self.request('/app/api/work/999/source',{'host':'newtoki1.org'})
        reader_app.collect_episode_list=lambda *_: ('원본', [('91','91화','https://newtoki1.org/novel/999/91')])
        reader_app.run_one_job(reader_sync.connect)
        backup=self.request('/app/api/manage/backup?work_id=999')[1]
        for action in ('cache-clear','restore'):
            with self.subTest(action=action):
                self.request('/app/api/manage/settings',{'work_id':'999','settings':{'paused':0}})
                self.request('/app/api/episode/999/91')
                def fetched(*_):
                    payload={'work_id':'999','confirm':True} if action=='cache-clear' else {'backup':backup,'confirm':True}
                    self.assertEqual(self.request('/app/api/manage/'+action,payload)[0],200)
                    return ['늦게 도착한 본문']
                reader_app.browse_episode=fetched
                self.assertTrue(reader_app.run_one_job(reader_sync.connect))
                self.assertIsNone(self.request('/app/api/episode/999/91')[1]['episode']['paragraphs'])

    def test_manage_delete_preserves_other_works_and_sync_tombstone(self):
        self.assertEqual(self.request('/app/manage', cookie=False)[0], 200)
        self.assertEqual(self.request('/app/api/manage', cookie=False)[0], 401)
        body={'work_id':'999','confirm':True}
        self.assertEqual(self.request('/app/api/manage/delete',body,cookie=False)[0],401)
        self.login()
        self.assertEqual(self.request('/app/api/manage/delete',body,app_header=False)[0],403)
        self.assertEqual(self.request('/app/api/manage/delete',{'work_id':'999'})[0],400)
        self.assertEqual(self.request('/app/api/manage/delete',{'work_id':'../999','confirm':True})[0],400)
        with reader_sync.connect() as db:
            db.execute("INSERT INTO reader_works VALUES('999','newtoki1.org','관리 시험',1)")
            db.execute("INSERT INTO reader_works VALUES('888','newtoki1.org','보존 작품',1)")
            db.execute("""INSERT INTO reader_episodes(work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                VALUES('999','91',1,'91화','https://newtoki1.org/novel/999/91','[\"cached\"]','ready',1)""")
            reader_app.enqueue(db,'999','92')
            reader_app.enqueue(db,'888')
        reader_sync.store_progress({'kind':'webtoon','work_id':'999','episode_id':'1','position':.3})
        rows=self.request('/app/api/manage')[1]['works']
        row=next(x for x in rows if x['work_id']=='999')
        self.assertEqual((row['episode_title'],row['position'],row['ready'],row['pending']),('91화',.6,1,1))
        self.assertGreater(row['bytes'],0)
        self.assertEqual(self.request('/app/api/manage/delete',body)[0],200)
        self.assertEqual([x['work_id'] for x in self.request('/app/api/manage')[1]['works']],['888'])
        with reader_sync.connect() as db:
            for table in ('reader_works','reader_episodes','reader_jobs'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE work_id=?',('999',)).fetchone()[0],0)
            tomb=db.execute("SELECT * FROM progress WHERE kind='novel' AND work_id='999'").fetchone()
            self.assertEqual((tomb['deleted'],tomb['episode_id'],tomb['title'],tomb['position'],tomb['device_id']),(1,'','',0,''))
            self.assertEqual(db.execute("SELECT deleted FROM progress WHERE kind='webtoon' AND work_id='999'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM reader_jobs WHERE work_id='888'").fetchone()[0],1)
        rows=self.request('/v1/progress?include_deleted=1',bearer=True)[1]['progress']
        self.assertTrue(any(x['kind']=='novel' and x['work_id']=='999' and x['deleted'] for x in rows))
        self.assertEqual(reader_sync.store_progress({'kind':'novel','work_id':'999','episode_id':'91','position':.7,'expected_revision':1})[0],409)
        self.assertEqual(self.request('/app/api/manage/delete',body)[0],200)
        self.assertTrue(self.request('/app/api/me')[1]['authenticated'])

    def test_deleted_work_cannot_be_resurrected_by_inflight_job(self):
        self.login()
        for job_kind in ('list','episode'):
            for outcome in ('success','failure','verification','locked'):
                with self.subTest(job=job_kind,outcome=outcome):
                    with reader_sync.connect() as db:
                        db.execute("INSERT INTO reader_works VALUES('777','newtoki1.org','옛 데이터',1)")
                        if job_kind=='episode':
                            db.execute("INSERT INTO reader_episodes(work_id,episode_id,ordinal,title,source_url,updated_at) VALUES('777','1',1,'1화','https://newtoki1.org/novel/777/1',1)")
                        reader_app.enqueue(db,'777','1' if job_kind=='episode' else '')
                    def finish_after_delete(*args):
                        self.assertEqual(self.request('/app/api/manage/delete',{'work_id':'777','confirm':True})[0],200)
                        # Re-add the exact same work before the stale network response returns.
                        self.assertEqual(self.request('/app/api/works',{'url':'https://newtoki1.org/novel/777'})[0],200)
                        if outcome=='failure': raise TimeoutError('late failure')
                        if outcome=='verification': raise reader_crawl.HumanVerificationRequired()
                        if outcome=='locked': raise LockedChapter('late locked page')
                        return ('옛 데이터',[('1','1화','https://newtoki1.org/novel/777/1')]) if job_kind=='list' else ['옛 본문']
                    reader_app.collect_episode_list=finish_after_delete
                    reader_app.browse_episode=finish_after_delete
                    self.assertTrue(reader_app.run_one_job(reader_sync.connect))
                    with reader_sync.connect() as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM reader_episodes WHERE work_id='777'").fetchone()[0],0)
                        self.assertEqual(tuple(db.execute("SELECT state,attempts,run_token FROM reader_jobs WHERE work_id='777'").fetchone()),('queued',0,''))
                        self.assertEqual(db.execute('SELECT count(*) FROM reader_paused_sources').fetchone()[0],0)
                    self.assertEqual(self.request('/app/api/manage/delete',{'work_id':'777','confirm':True})[0],200)

    def test_existing_jobs_survive_run_token_migration(self):
        with reader_sync.connect() as db:
            db.execute('DROP TABLE reader_jobs')
            db.execute("""CREATE TABLE reader_jobs(work_id TEXT NOT NULL,episode_id TEXT NOT NULL,
                state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,PRIMARY KEY(work_id,episode_id))""")
            db.execute("INSERT INTO reader_jobs VALUES('8','1','queued',2,'retry pending',9999999)")
        reader_sync.init_db()
        reader_sync.init_db()
        with reader_sync.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM reader_jobs').fetchone()),('8','1','queued',2,'retry pending',9999999,''))

    def test_delayed_retries_are_bounded_and_persisted(self):
        self.login()
        with reader_sync.connect() as db:
            db.execute("INSERT INTO reader_works VALUES('8','newtoki1.org','시험',1)")
            db.execute("INSERT INTO reader_episodes(work_id,episode_id,ordinal,title,source_url,updated_at) VALUES('8','1',1,'1화','https://newtoki1.org/novel/8/1',1)")
            db.execute("INSERT INTO reader_jobs(work_id,episode_id,state,updated_at) VALUES('8','1','queued',1)")
        reader_app.browse_episode = Mock(side_effect=TimeoutError('temporary failure'))
        clock = 1000
        for attempt in range(1, 5):
            with patch.object(reader_app.time, 'time', return_value=clock):
                self.assertTrue(reader_app.run_one_job(reader_sync.connect))
                self.assertFalse(reader_app.run_one_job(reader_sync.connect))
                reader_sync.init_db()  # Restart/init must not reset the retry budget or due time.
                self.assertFalse(reader_app.run_one_job(reader_sync.connect))
            with reader_sync.connect() as db:
                row = db.execute("SELECT * FROM reader_jobs WHERE work_id='8'").fetchone()
                self.assertEqual(row['attempts'], attempt)
                self.assertEqual(row['state'], 'queued' if attempt < 4 else 'error')
                self.assertEqual(row['updated_at'], (clock + 5 * attempt if attempt < 4 else clock) * 1000)
                if attempt < 4:
                    reader_app.queue_prefetch(db, '8', '1')
                    self.assertEqual(db.execute("SELECT updated_at FROM reader_jobs WHERE work_id='8'").fetchone()[0], row['updated_at'])
            clock += 5 * attempt
        self.assertEqual(reader_app.browse_episode.call_count, 4)
        self.assertFalse(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request('/app/api/episode/8/1/retry', {})[0], 200)
        reader_app.browse_episode = Mock(return_value=['복구 본문'])
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        with reader_sync.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT state,attempts FROM reader_jobs WHERE work_id='8'").fetchone()), ('done', 1))
            reader_app.enqueue(db, '8', force=True)
        reader_app.collect_episode_list = Mock(side_effect=TimeoutError('list failure'))
        self.assertTrue(reader_app.run_one_job(reader_sync.connect))
        self.assertEqual(self.request('/app/api/work/8')[1]['list_job']['state'], 'queued')
        self.assertFalse(reader_app.run_one_job(reader_sync.connect))

    def test_worker_waits_after_each_job(self):
        class StopLoop(BaseException):
            pass
        for result in (True, False):
            with patch.object(reader_app.threading, 'Thread') as thread, \
                    patch.object(reader_app, 'run_one_job', return_value=result) as run, \
                    patch.object(reader_app.time, 'sleep', side_effect=StopLoop) as sleep:
                reader_app.start_worker(reader_sync.connect)
                with self.assertRaises(StopLoop):
                    thread.call_args.kwargs['target']()
                run.assert_called_once_with(reader_sync.connect)
                sleep.assert_called_once_with(1)

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
            checked_url("https://toki33.com.evil.example/novel/57458")
        self.assertEqual(checked_url("https://toki32.com/novel/57458"), ("toki32.com", "57458", None))
        self.assertEqual(checked_url("https://toki33.com/novel/57458/2"), ("toki33.com", "57458", "2"))
        for url in ('https://toki33.com:0/novel/57458', 'https://toki33.com:bad/novel/57458',
                    'https://user@toki33.com/novel/57458', 'http://toki33.com/novel/57458'):
            with self.assertRaises(ValueError):
                checked_url(url)
        source = Request("https://newtoki1.org/novel/57458")
        redirect = reader_crawl.SameSiteRedirect("57458")
        self.assertEqual(redirect.redirect_request(
            source, None, 302, "Found", {}, "https://toki32.com/novel/57458").full_url,
            "https://toki32.com/novel/57458")
        with self.assertRaises(ValueError):
            redirect.redirect_request(source, None, 302, "Found", {}, "https://example.com/novel/57458")

    def test_register_and_change_source_preserves_data(self):
        register = '/app/api/manage/sources'
        source = '/app/api/work/999/source'
        self.assertEqual(self.request(register, {'host':'toki33.com'}, cookie=False)[0], 401)
        self.login()
        self.assertEqual(self.request(register, {'host':'toki33.com'}, app_header=False)[0], 403)
        for host in (None, [], 'localhost', '127.0.0.1', 'https://toki33.com',
                     'toki33.com:443', 'toki33.com/', 'toki33.com.evil.example', 'example.com'):
            self.assertEqual(self.request(register, {'host':host})[0], 400)
            self.assertEqual(self.request(source, {'host':host})[0], 400)
        for _ in range(2):
            self.assertEqual(self.request(register, {'host':' TOKI33.COM '})[1]['host'], 'toki33.com')
        reader_sync.init_db()
        self.assertIn('toki33.com', self.request('/app/api/manage')[1]['sources'])
        self.assertEqual(self.request(source, {'host':'newtoki1.org'})[0], 200)
        reader_app.collect_episode_list = lambda *_: ('시험 작품', [
            ('91','91화','https://newtoki1.org/novel/999/91'),
            ('92','92화','https://newtoki1.org/novel/999/92')])
        reader_app.run_one_job(reader_sync.connect)
        with reader_sync.connect() as db:
            db.execute("UPDATE reader_episodes SET state='ready',paragraphs_json='[\"cached\"]' WHERE episode_id='91'")
            reader_app.enqueue(db, '999', '92')
            db.execute("UPDATE reader_jobs SET state='verification',error='old check' WHERE episode_id='92'")
            db.execute("INSERT INTO reader_paused_sources VALUES('newtoki1.org','old check')")
            reader_app.save_settings(db, '999', {'paused':1,'prefetch':4,'cache_limit_mb':8})
        before = self.request('/app/api/manage/backup?work_id=999')[1]
        progress = self.request('/app/api/work/999')[1]['progress']
        self.assertEqual(self.request(source, {'host':'toki33.com'})[0], 200)
        after = self.request('/app/api/manage/backup?work_id=999')[1]
        self.assertEqual(after['work']['title'], before['work']['title'])
        self.assertEqual(after['settings'], before['settings'])
        self.assertEqual(after['progress'], before['progress'])
        self.assertEqual(self.request('/app/api/work/999')[1]['progress'], progress)
        for old, new in zip(before['episodes'], after['episodes']):
            self.assertEqual(new['source_url'], old['source_url'].replace('newtoki1.org','toki33.com'))
            self.assertEqual(new['paragraphs'], old['paragraphs'])
        with reader_sync.connect() as db:
            jobs = [tuple(r) for r in db.execute("SELECT * FROM reader_jobs WHERE work_id='999' ORDER BY episode_id")]
            self.assertEqual([r[1:4] for r in jobs], [('', 'queued', 0), ('92','queued',0)])
            self.assertEqual(db.execute('SELECT host FROM reader_paused_sources').fetchone()[0], 'newtoki1.org')
        self.assertFalse(reader_app.run_one_job(reader_sync.connect), 'Keep the work paused')
        self.assertEqual(self.request(source, {'host':'toki33.com'})[0], 200)
        with reader_sync.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM reader_jobs WHERE work_id='999' ORDER BY episode_id")], jobs)
        self.assertEqual(self.request('/app/api/manage/restore', {'backup':after,'confirm':True})[0], 200)
        self.assertEqual(self.request('/app/api/manage/backup?work_id=999')[1]['episodes'], after['episodes'])
        # URL-based re-add uses the same source-change path, including cached episode URLs.
        self.assertEqual(self.request('/app/api/works', {'url':'https://newtoki2.org/novel/999'})[0], 200)
        self.assertEqual(self.request('/app/api/manage/backup?work_id=999')[1]['episodes'][0]['source_url'], 'https://newtoki2.org/novel/999/91')
        # A newly added work does not need a saved reading position to switch sources.
        self.request('/app/api/works', {'url':'https://toki32.com/novel/888'})
        self.assertEqual(self.request('/app/api/work/888/source', {'host':'toki33.com'})[0], 200)
        self.assertIn('toki33.com', self.request('/app/api/work/888')[1]['sources'])
        self.assertEqual(self.request('/app/api/work/777/source', {'host':'toki33.com'})[0], 404)

    def test_source_change_discards_inflight_results(self):
        self.login()
        for body_job in (False, True):
            for failure in (None, RuntimeError, reader_crawl.HumanVerificationRequired, LockedChapter):
                with self.subTest(body=body_job, failure=failure):
                    self.request('/app/api/work/999/source', {'host':'toki32.com'})
                    with reader_sync.connect() as db:
                        db.execute("DELETE FROM reader_jobs WHERE work_id='999'")
                        db.execute("INSERT OR REPLACE INTO reader_episodes(work_id,episode_id,ordinal,title,source_url,updated_at) VALUES('999','91',1,'91화','https://toki32.com/novel/999/91',1)")
                        reader_app.enqueue(db, '999', '91' if body_job else '')
                    def fetched(*_):
                        self.assertEqual(self.request('/app/api/work/999/source', {'host':'toki33.com'})[0], 200)
                        if failure:
                            raise failure('old source result')
                        return ['stale body'] if body_job else ('stale title', [('91','stale','https://toki32.com/novel/999/91')])
                    reader_app.collect_episode_list = fetched
                    reader_app.browse_episode = fetched
                    self.assertTrue(reader_app.run_one_job(reader_sync.connect))
                    with reader_sync.connect() as db:
                        work = db.execute("SELECT * FROM reader_works WHERE work_id='999'").fetchone()
                        self.assertEqual(work['host'], 'toki33.com')
                        self.assertNotEqual(work['title'], 'stale title')
                        episode = db.execute("SELECT * FROM reader_episodes WHERE work_id='999'").fetchone()
                        self.assertEqual(episode['source_url'], 'https://toki33.com/novel/999/91')
                        self.assertIsNone(episode['paragraphs_json'])
                        self.assertEqual(episode['state'], 'missing')
                        self.assertEqual(db.execute('SELECT count(*) FROM reader_paused_sources').fetchone()[0], 0)
                        self.assertTrue(all(r['state']=='queued' and r['attempts']==0 for r in db.execute('SELECT * FROM reader_jobs')))

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

    def test_large_prefetch_limit(self):
        self.login()
        with reader_sync.connect() as db:
            db.execute("INSERT INTO reader_works VALUES('9000','newtoki1.org','시험',1)")
            db.executemany("""INSERT INTO reader_episodes
                (work_id,episode_id,ordinal,title,source_url,updated_at) VALUES('9000',?,?,?,?,1)""",
                [(str(n),n,f'{n}화',f'https://newtoki1.org/novel/9000/{n}') for n in range(1,251)])
            self.assertEqual(reader_app.work_settings(db,'9000')['prefetch'],2)
        settings={'paused':0,'prefetch':200,'cache_limit_mb':0}
        self.assertEqual(self.request('/app/api/manage/settings',{'work_id':'9000','settings':settings})[0],200)
        reader_sync.init_db()
        backup=self.request('/app/api/manage/backup?work_id=9000')[1]
        self.assertEqual(reader_app.checked_backup(backup)['settings']['prefetch'],200)
        with reader_sync.connect() as db:
            self.assertEqual(reader_app.work_settings(db,'9000'),settings)
            reader_app.queue_prefetch(db,'9000','1')
            reader_app.queue_prefetch(db,'9000','1')
            ids={r[0] for r in db.execute("SELECT episode_id FROM reader_jobs WHERE work_id='9000'")}
            self.assertEqual(ids,{str(n) for n in range(1,202)})
            db.execute("DELETE FROM reader_jobs WHERE work_id='9000'")
            reader_app.queue_prefetch(db,'9000','249')
            self.assertEqual({r[0] for r in db.execute("SELECT episode_id FROM reader_jobs WHERE work_id='9000'")},{'249','250'})

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
                    parser, _ = reader_crawl.fetch_index_page("https://toki33.com/novel/11", "11", browser)
                    self.assertEqual(parser.links, [("1", "1화"), ("0", "0화")])
                    self.assertGreater(len(reader_crawl.extract_episode(browser, "https://toki33.com/novel/11/1", "11", "1")[0]), 80)
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

    def test_browser_progress_conflict_reconciliation(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest('Playwright is not installed')
        executable=os.environ.get('READER_TEST_BROWSER') or r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        if not Path(executable).exists():
            self.skipTest('A local Chromium browser is not available')
        self.login()
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,executable_path=executable)
            try:
                for index,case in enumerate(('same','same-farther','ahead','server-ahead','deleted','unknown','repeated','manual-cancel')):
                    with self.subTest(case=case):
                        work_id=str(7000+index)
                        with reader_sync.connect() as db:
                            db.execute('INSERT INTO reader_works VALUES(?,?,?,0)',(work_id,'newtoki1.org','충돌 시험'))
                            for ordinal,eid in enumerate(('900','20','100'),1):
                                db.execute('''INSERT INTO reader_episodes
                                    (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                                    VALUES(?,?,?,?,?,?,'ready',0)''', (work_id,eid,ordinal,f'{ordinal}화',
                                    f'https://newtoki1.org/novel/{work_id}/{eid}',json.dumps(['시험 문단입니다. '*8]*100)))
                        def progress():
                            return self.request(f'/v1/progress?kind=novel&work_id={work_id}&include_deleted=1',bearer=True)[1]['progress']
                        def update(eid,position):
                            current=progress()
                            status,_=reader_sync.store_progress(dict(kind='novel',work_id=work_id,episode_id=eid,
                                position=position,title='충돌 시험',expected_revision=current['revision'] if current else 0,allow_rewind=True))
                            self.assertEqual(status,200)
                        update('20',.2)
                        page=browser.new_page(viewport={'width':390,'height':800})
                        page.goto(self.base+f'/app?work={work_id}&ep=20')
                        page.locator('#token').fill(reader_sync.SYNC_TOKEN)
                        page.locator('#login-form button').click()
                        page.locator('#content p').first.wait_for()
                        page.wait_for_timeout(1000)  # Finish the initial position-restoration autosave.
                        writes=[]
                        page.on('response',lambda response: writes.append(response.status) if response.url.endswith('/app/api/progress') else None)
                        if case=='manual-cancel':
                            update('100',.8)
                            page.once('dialog',lambda dialog: dialog.dismiss())
                            page.locator('#manual-save').click()
                            expect(page.locator('#save-feedback')).to_have_text('저장을 취소했습니다')
                            before=progress()
                            page.evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*.4)')
                            page.wait_for_timeout(900)
                            self.assertEqual(progress(),before,'Declining manual overwrite must also prevent subsequent autosave rewind')
                            self.assertEqual(writes,[])
                            page.close()
                            continue
                        if case=='deleted':
                            with reader_sync.connect() as db:
                                reader_sync.delete_progress(db,'novel',work_id,clear=True)
                        elif case=='repeated':
                            attempts=[]
                            def race(route):
                                attempts.append(1)
                                update('20',.3 if len(attempts)==1 else .6)
                                route.continue_()
                            page.route('**/app/api/progress',race)
                        else:
                            update({'same':'20','same-farther':'20','ahead':'900','server-ahead':'100','unknown':'9999'}[case],.3 if case=='same-farther' else .8)
                        page.evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*.4)')
                        if case in ('same','same-farther','ahead'):
                            for _ in range(60):
                                if len(writes)>=2:break
                                page.wait_for_timeout(50)
                            self.assertEqual(writes,[409,200])
                            current=progress()
                            self.assertEqual(current['episode_id'],'20')
                            self.assertAlmostEqual(current['position'],.8 if case=='same' else .4,delta=.01)
                            expect(page.locator('#reader-message')).to_have_text('')
                        elif case=='repeated':
                            expect(page.locator('#reader-message')).to_contain_text('이번 저장을 보류')
                            self.assertEqual(len(attempts),2,'A save must retry at most once')
                            page.unroute('**/app/api/progress')
                        else:
                            expected={'server-ahead':'더 뒤의 회차','deleted':'서버에서 삭제된','unknown':'회차 순서를 확인할 수 없어'}[case]
                            expect(page.locator('#reader-message')).to_contain_text(expected)
                            self.assertEqual(writes,[409])
                            before=progress()
                            page.evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*.9)')
                            page.wait_for_timeout(900)
                            self.assertEqual(progress(),before,'Unsafe remote progress must be preserved')
                            self.assertEqual(writes,[409])
                            if case=='server-ahead':
                                page.evaluate('scrollBy(0,-30)')
                                expect(page.locator('#reader-nav')).to_be_visible()
                                page.once('dialog',lambda dialog: dialog.accept())
                                page.locator('#manual-save').click()
                                expect(page.locator('#save-feedback')).to_contain_text('서버에 저장했습니다')
                                self.assertEqual(progress()['episode_id'],'20','Explicit manual rewind must still work')
                        if case in ('same','same-farther','ahead','repeated'):
                            page.evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*.9)')
                            for _ in range(60):
                                if progress()['position']>.89:break
                                page.wait_for_timeout(50)
                            self.assertGreater(progress()['position'],.89,'Autosave must continue after reconciliation or bounded retry')
                        page.close()
            finally:
                browser.close()

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
                VALUES('999','93',3,'숫자가 없는 제목','https://newtoki1.org/novel/999/93',NULL,'error','수집 실패',1)""")
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
                expect(page.locator('#current-episode')).to_have_text('현재 91화')
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
                expect(page.locator('#current-episode')).to_have_text('현재 92화')
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
                direct = browser.new_page(viewport={"width": 390, "height": 800}, has_touch=True)
                direct.goto(self.base + "/app?work=999&ep=92")
                direct.locator("#token").fill(reader_sync.SYNC_TOKEN)
                direct.locator("#login-form button").click()
                expect(direct.locator("#content p").first).to_have_text("다음 회차 본문", timeout=5000)
                direct.locator("#next").click()
                expect(direct.locator("#reader-message")).to_have_text("수집 실패", timeout=5000)
                expect(direct.locator('#current-episode')).to_have_text('현재 목록 3번째')
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
                direct.emulate_media(reduced_motion='reduce')
                for width in (390, 1280):
                    direct.set_viewport_size({'width':width, 'height':800})
                    direct.evaluate('scrollTo(0,2000)')
                    direct.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                    start = direct.evaluate('scrollY')
                    step = direct.evaluate("Math.round((innerHeight-document.querySelector('#reader-nav').getBoundingClientRect().height)*.85)")
                    direct.mouse.click(width-8, 300)
                    direct.wait_for_function('y => scrollY > y', arg=start)
                    self.assertAlmostEqual(direct.evaluate('scrollY'), start+step, delta=2)
                    expect(direct.locator('#reader-nav')).to_be_hidden()
                    expect(direct.locator('#show-nav, #hide-nav')).to_have_count(0)
                    direct.mouse.click(8, 300)
                    self.assertAlmostEqual(direct.evaluate('scrollY'), start, delta=2)
                    expect(direct.locator('#reader-nav')).to_be_visible()
                    direct.mouse.click(width/2, 300)
                    self.assertAlmostEqual(direct.evaluate('scrollY'), start, delta=2)
                    direct.mouse.move(8,300)
                    direct.mouse.down()
                    direct.mouse.move(8,380,steps=5)
                    direct.mouse.up()
                    self.assertAlmostEqual(direct.evaluate('scrollY'), start, delta=2, msg='Drag must not page-scroll')
                    direct.locator('#show-settings').click()
                    direct.mouse.click(8,300)
                    self.assertAlmostEqual(direct.evaluate('scrollY'), start, delta=2)
                    direct.locator('#close-settings').click()
                    direct.evaluate('getSelection().removeAllRanges()')
                    self.assertEqual(direct.evaluate('document.documentElement.scrollWidth<=innerWidth'), True)
                direct.set_viewport_size({'width':390, 'height':800})
                direct.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                start=direct.evaluate('scrollY')
                direct.touchscreen.tap(382,300)
                direct.wait_for_function('y => scrollY>y',arg=start)
                expect(direct.locator('#reader-nav')).to_be_hidden()
                direct.evaluate('scrollBy(0,-30)')
                expect(direct.locator('#reader-nav')).to_be_visible()
                direct.wait_for_timeout(850)  # Finish scroll autosaves before the manual-save probe.
                pending_saves=[]
                direct.route('**/app/api/progress', lambda route: pending_saves.append(route))
                direct.locator('#manual-save').click()
                expect(direct.locator('#manual-save')).to_be_disabled()
                expect(direct.locator('#manual-save')).to_have_text('저장 중…')
                expect(direct.locator('#save-feedback')).to_have_text('서버에 저장 중…')
                for _ in range(30):
                    if pending_saves: break
                    direct.wait_for_timeout(50)
                self.assertEqual(len(pending_saves), 1)
                pending_saves.pop().continue_()
                expect(direct.locator('#save-feedback')).to_have_text('✓ 서버에 저장했습니다')
                expect(direct.locator('#manual-save')).to_be_enabled()
                direct.unroute('**/app/api/progress')
                direct.route('**/app/api/progress', lambda route: route.fulfill(status=503,content_type='application/json',body='{"error":"test failure"}'))
                direct.locator('#manual-save').click()
                expect(direct.locator('#save-feedback')).to_have_text('저장 실패 · 다시 눌러주세요')
                expect(direct.locator('#manual-save')).to_be_enabled()
                direct.unroute('**/app/api/progress')
                direct.mouse.wheel(0, 30000)
                expect(direct.locator('#reader-nav')).to_be_visible()
                self.assertIn('work=2000&ep=1',direct.url, 'Bottom scrolling must not change episode')
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
                direct.locator('#show-settings').click()
                direct.locator('#reader-manage').click()
                expect(direct.locator('#manage')).to_be_visible()
                self.assertTrue(direct.url.endswith('/app/manage'))
                expect(direct.locator('#resources-status')).to_contain_text('최근 확인')
                expect(direct.locator('#resource-db')).to_contain_text('본체')
                expect(direct.locator('#resource-disk')).to_contain_text('사용')
                direct.route('**/app/api/manage/resources',lambda route: route.fulfill(status=503,content_type='application/json',body='{"error":"temporary unavailable"}'))
                direct.locator('#resources-refresh').click()
                expect(direct.locator('#resources-status')).to_contain_text('갱신 실패')
                direct.unroute('**/app/api/manage/resources')
                direct.locator('#resources-refresh').click()
                expect(direct.locator('#resources-status')).to_contain_text('최근 확인')
                card=direct.locator('#manage-list [data-work-id="999"]')
                expect(card).to_contain_text('본문 저장 3개')
                expect(card).to_contain_text('읽은 위치:')
                cached=card.locator('.cached-episodes')
                with reader_sync.connect() as db:
                    before_jobs=[tuple(r) for r in db.execute('SELECT * FROM reader_jobs ORDER BY work_id,episode_id')]
                    before_progress=[tuple(r) for r in db.execute('SELECT * FROM progress ORDER BY kind,work_id')]
                cached.locator('summary').click()
                expect(cached.locator('li')).to_have_text(['목록 1번째 · 91화','목록 2번째 · 92화','목록 5번째 · 95화'])
                with reader_sync.connect() as db:
                    self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM reader_jobs ORDER BY work_id,episode_id')],before_jobs)
                    self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM progress ORDER BY kind,work_id')],before_progress)
                direct.route('**/app/api/work/999',lambda route: route.fulfill(status=503,content_type='application/json',body='{"error":"temporary unavailable"}'))
                cached.get_by_role('button').click()
                expect(cached.locator('[role=status]')).to_contain_text('저장 회차 조회 실패')
                expect(cached.locator('li')).to_have_count(0)
                direct.unroute('**/app/api/work/999')
                cached.get_by_role('button').click()
                expect(cached.locator('li')).to_have_count(3)
                card.get_by_label('미리 수집할 다음 회차 수').fill('200')
                card.get_by_label('본문 저장 한도 (MB)').fill('8')
                card.get_by_role('button',name='수집 설정 저장').click()
                expect(direct.locator('#manage-message')).to_have_text('수집 설정을 저장했습니다.')
                expect(card.get_by_label('미리 수집할 다음 회차 수')).to_have_value('200')
                card.get_by_role('button',name='수집 일시정지',exact=True).click()
                expect(card.get_by_role('button',name='수집 재개',exact=True)).to_be_visible()
                card.get_by_role('button',name='실패 작업 재시도').click()
                expect(direct.locator('#manage-message')).to_contain_text('재시도 대열')
                expect(card.get_by_role('button',name='실패 작업 재시도')).to_be_disabled()
                with direct.expect_download() as download_info:
                    card.get_by_role('button',name='작품 백업',exact=True).click()
                backup_bytes=Path(download_info.value.path()).read_bytes()
                backup=json.loads(backup_bytes)
                self.assertEqual(backup['work']['work_id'],'999')
                self.assertNotIn('credentials',backup)
                direct.once('dialog',lambda dialog: dialog.dismiss())
                card.get_by_role('button',name='본문만 삭제').click()
                expect(card).to_contain_text('본문 저장 3개')
                direct.once('dialog',lambda dialog: dialog.accept())
                card.get_by_role('button',name='본문만 삭제').click()
                expect(card).to_contain_text('본문 저장 0개')
                expect(card).to_contain_text('읽은 위치:')
                cached.locator('summary').click()
                expect(cached.locator('[role=status]')).to_have_text('DB에 본문이 저장된 회차가 없습니다.')
                expect(cached.locator('li')).to_have_count(0)
                direct.locator('#manage > details > summary').click()
                direct.locator('#restore-file').set_input_files({'name':'work.json','mimeType':'application/json','buffer':backup_bytes})
                direct.once('dialog',lambda dialog: dialog.accept())
                direct.locator('#restore-form button').click()
                expect(direct.locator('#manage-message')).to_contain_text('복원했습니다.')
                expect(card).to_contain_text('본문 저장 3개')
                expect(card.get_by_role('button',name='수집 재개',exact=True)).to_be_visible()
                cached.locator('summary').click()
                expect(cached.locator('li')).to_have_count(3)
                card.get_by_role('button',name='수집 재개',exact=True).click()
                expect(card.get_by_role('button',name='수집 일시정지',exact=True)).to_be_visible()
                direct.locator('#source-add-host').fill('toki33.com')
                direct.locator('#source-add-form button').click()
                expect(direct.locator('#manage-message')).to_contain_text('toki33.com 등록 완료')
                card.get_by_label('수집 도메인',exact=True).select_option('toki33.com')
                direct.once('dialog',lambda dialog: dialog.dismiss())
                card.get_by_role('button',name='도메인 변경',exact=True).click()
                direct.reload()
                expect(card.get_by_label('수집 도메인',exact=True)).to_have_value('newtoki1.org')
                card.get_by_label('수집 도메인',exact=True).select_option('toki33.com')
                direct.once('dialog',lambda dialog: dialog.accept())
                card.get_by_role('button',name='도메인 변경',exact=True).click()
                expect(direct.locator('#manage-message')).to_contain_text('toki33.com로 변경했습니다.')
                expect(card).to_contain_text('본문 저장 3개')
                expect(card).to_contain_text('읽은 위치:')
                direct.reload()
                expect(card.get_by_label('수집 도메인',exact=True)).to_have_value('toki33.com')
                expect(card.get_by_role('button',name='도메인 변경',exact=True)).to_be_disabled()
                for width in (390,1280):
                    direct.set_viewport_size({'width':width,'height':800})
                    self.assertTrue(direct.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                direct.set_viewport_size({'width':390,'height':800})
                direct.once('dialog',lambda dialog: dialog.dismiss())
                card.get_by_role('button',name='이 작품 전체 삭제').click()
                expect(card).to_have_count(1)
                direct.once('dialog',lambda dialog: dialog.accept())
                card.get_by_role('button',name='이 작품 전체 삭제').click()
                expect(card).to_have_count(0)
                expect(direct.locator('#manage-message')).to_contain_text('삭제 완료')
                expect(direct.locator('#manage-list [data-work-id="2000"]')).to_have_count(1)
                self.assertTrue(direct.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                direct.reload()
                expect(direct.locator('#manage')).to_be_visible()
                expect(direct.locator('#manage-list [data-work-id="999"]')).to_have_count(0)
                direct.locator('#manage-back').click()
                direct.locator('#manage-open').click()
                expect(direct.locator('#manage')).to_be_visible()
                # Exercise the refresh lifecycle without waiting for real 10-second intervals.
                resource_requests=[]
                direct.on('request',lambda req: resource_requests.append(req.url) if req.url.endswith('/app/api/manage/resources') else None)
                direct.clock.install()
                with direct.expect_response('**/app/api/manage/resources'):
                    direct.locator('#resources-refresh').click()
                expect(direct.locator('#resources-refresh')).to_be_enabled()
                count=len(resource_requests)
                with direct.expect_response('**/app/api/manage/resources'):
                    direct.clock.fast_forward(10001)
                expect(direct.locator('#resources-refresh')).to_be_enabled()
                self.assertEqual(len(resource_requests),count+1)
                direct.locator('#manage-back').click()
                count=len(resource_requests)
                direct.clock.fast_forward(30000)
                self.assertEqual(len(resource_requests),count,'Do not poll outside management')
                with direct.expect_response('**/app/api/manage/resources'):
                    direct.locator('#manage-open').click()
                direct.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
                count=len(resource_requests)
                direct.clock.fast_forward(30000)
                self.assertEqual(len(resource_requests),count,'Do not poll background pages')
                with direct.expect_response('**/app/api/manage/resources'):
                    direct.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
