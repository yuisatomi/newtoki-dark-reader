"""Offline source, OAuth, translation, compatibility and reader checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

support = importlib.import_module('test-app')
import reader_crawl
import reader_openai
import reader_sync
import reader_translate


class TranslationTest(unittest.TestCase):
    request = support.ReaderAppTest.request
    login = support.ReaderAppTest.login

    def setUp(self):
        support.ReaderAppTest.setUp(self)
        self.environment = patch.dict(os.environ, {'READER_OPENAI_DIR': str(Path(self.temp.name) / 'openai')})
        self.environment.start()
        self.login()

    def tearDown(self):
        self.environment.stop()
        support.ReaderAppTest.tearDown(self)

    def source(self, paragraphs=None):
        self.request('/app/api/works', {'url': 'https://www.69shuba.com/book/999.htm'})
        with reader_sync.connect() as db:
            db.execute("UPDATE reader_works SET title='中国小说' WHERE work_id='69shuba-999'")
            for index in range(1, 4):
                db.execute('''INSERT INTO reader_episodes
                    (work_id,episode_id,ordinal,title,source_url,paragraphs_json,state,updated_at)
                    VALUES('69shuba-999',?,?,?,?,?,'ready',1)''',
                    (str(index), index, f'第{index}章 试验', f'https://www.69shuba.com/txt/999/{index}',
                     json.dumps(paragraphs or ['第一段。', '第二段。'], ensure_ascii=False)))
        return '/app/api/episode/69shuba-999/1'

    def descending_source(self, paragraphs=None):
        base = self.source(paragraphs)
        with reader_sync.connect() as db:
            for episode_id, chapter in (('1', 110), ('2', 109), ('3', 108)):
                db.execute('UPDATE reader_episodes SET title=? WHERE work_id=? AND episode_id=?',
                           (f'第{chapter}章 试验', '69shuba-999', episode_id))
        return base

    @staticmethod
    def fake_response(payload):
        assert payload['model'] == 'gpt-6-luna'
        assert payload['reasoning']['effort'] == 'none'
        assert payload['store'] is False and payload['stream'] is True
        items = json.loads(payload['input'][0]['content'])['items']
        return json.dumps({'items': [{'id': item['id'], 'text': '한국어 번역 ' + item['id']} for item in items]})

    def test_shuba_urls_and_order(self):
        for url in ('https://www.69shuba.com/book/58953.htm', 'https://www.69shuba.com/book/58953/', 'https://69shuba.com/book/58953'):
            self.assertEqual(reader_crawl.checked_url(url)[1:], ('69shuba-58953', None))
        self.assertEqual(reader_crawl.checked_url('https://www.69shuba.com/txt/58953/38359848')[1:], ('69shuba-58953', '38359848'))
        for url in ('http://www.69shuba.com/book/1.htm', 'https://www.69shuba.com:443/book/1.htm',
                    'https://www.69shuba.com.evil.invalid/book/1.htm', 'https://user@www.69shuba.com/book/1.htm'):
            with self.assertRaises(ValueError): reader_crawl.checked_url(url)
        with self.assertRaises(ValueError): reader_crawl.checked_url('https://www.69shuba.com/book/999/', '999')
        with self.assertRaises(ValueError): reader_crawl.work_url('newtoki1.org', '69shuba-999')
        parser = reader_crawl.ShubaLinks('69shuba-999', 'https://www.69shuba.com/book/999/')
        parser.feed('''<title>作品最新章节</title><a href="/txt/999/20">第1章 一</a>
            <a href="/txt/999/2"><span>第2章 二</span></a><a href="/txt/999/20">重复</a>
            <a href="https://evil.invalid/txt/999/3">坏链接</a><a href="/txt/998/4">别的书</a>''')
        self.assertEqual(list(parser.links), ['20', '2'])
        self.assertEqual(parser.links['2'][1], '第2章 二')

    def test_shuba_chronological_order_preserves_special_neighbors(self):
        chapters = [(str(i), title, f'https://www.69shuba.com/txt/999/{i}') for i, title in enumerate(
            ['完结感言', '第110章 后续', '第109章 试验', '月末总结', '第108章 起点', '序章'], 1)]
        expected = list(reversed(chapters))
        self.assertEqual(reader_crawl.chronological_shuba_chapters(chapters), expected)
        self.assertEqual(reader_crawl.chronological_shuba_chapters(expected), expected)
        for uncertain in (chapters[:1], [('1', '序章', ''), ('2', '无章号', '')],
                          [('1', '第2章', ''), ('2', '第1章', ''), ('3', '第3章', '')]):
            self.assertEqual(reader_crawl.chronological_shuba_chapters(uncertain), uncertain)

    def test_shuba_order_migration_preserves_everything_except_ordinals(self):
        self.descending_source()
        base = '/app/api/episode/69shuba-999/2'
        self.request(base + '/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
            reader_translate.run_one(reader_sync.connect)
        self.request('/app/api/progress', {'work_id': '69shuba-999', 'episode_id': '2', 'position': .42, 'expected_revision': 0})
        with reader_sync.connect() as db:
            db.execute("INSERT INTO reader_episodes SELECT '999',episode_id,ordinal,title,'https://newtoki1.org/novel/999/'||episode_id,paragraphs_json,state,error,updated_at FROM reader_episodes WHERE work_id='69shuba-999'")
        def preserved():
            with reader_sync.connect() as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                return {table: [dict(row) if table != 'reader_episodes' else {key: row[key] for key in row.keys() if key != 'ordinal'}
                                for row in db.execute('SELECT * FROM "'+table+'" ORDER BY rowid')] for table in tables}
        before = preserved()
        reader_sync.init_db()
        reader_sync.init_db()
        self.assertEqual(preserved(), before)
        ordered = self.request('/app/api/work/69shuba-999')[1]['episodes']
        self.assertEqual([(row['episode_id'], row['ordinal']) for row in ordered], [('3', 1), ('2', 2), ('1', 3)])
        self.assertEqual([row['episode_id'] for row in self.request('/app/api/work/999')[1]['episodes']], ['1', '2', '3'])
        self.assertEqual(self.request(base)[1]['translation']['state'], 'ready')
        # The same canonical ordinals must drive prefetch, not numeric episode IDs.
        with reader_sync.connect() as db:
            db.execute("DELETE FROM reader_jobs WHERE work_id='69shuba-999' AND episode_id!=''")
            db.execute("UPDATE reader_episodes SET state='missing' WHERE work_id='69shuba-999' AND episode_id IN ('1','3')")
            support.reader_app.queue_prefetch(db, '69shuba-999', '2')
            self.assertEqual([row[0] for row in db.execute("SELECT episode_id FROM reader_jobs WHERE work_id='69shuba-999' AND episode_id!=''")], ['1'])

    def test_shuba_descending_backup_is_restored_in_reading_order(self):
        self.descending_source()
        self.request('/app/api/episode/69shuba-999/2/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
            reader_translate.run_one(reader_sync.connect)
        self.request('/app/api/progress', {'work_id': '69shuba-999', 'episode_id': '2', 'position': .42, 'expected_revision': 0})
        backup = self.request('/app/api/manage/backup?work_id=69shuba-999')[1]
        self.assertEqual(backup['version'], 2)
        self.assertEqual(self.request('/app/api/manage/restore', {'backup': backup, 'confirm': True})[0], 200)
        restored = self.request('/app/api/manage/backup?work_id=69shuba-999')[1]
        self.assertEqual([row['episode_id'] for row in restored['episodes']], ['3', '2', '1'])
        self.assertEqual(restored['progress'], backup['progress'])
        self.assertEqual(restored['translations'], backup['translations'])
        for item in restored['episodes']:
            original = next(row for row in backup['episodes'] if row['episode_id'] == item['episode_id'])
            self.assertEqual(item['paragraphs'], original['paragraphs'])

    def test_shuba_order_migration_rolls_back_partial_failure(self):
        self.descending_source()
        with reader_sync.connect() as db:
            db.execute('''CREATE TRIGGER fail_order BEFORE UPDATE OF ordinal ON reader_episodes
                WHEN OLD.work_id='69shuba-999' AND OLD.episode_id='1'
                BEGIN SELECT RAISE(ABORT,'simulated migration interruption'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            reader_sync.init_db()
        with reader_sync.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute(
                "SELECT episode_id,ordinal FROM reader_episodes WHERE work_id='69shuba-999' ORDER BY ordinal")], [('1', 1), ('2', 2), ('3', 3)])
            db.execute('DROP TRIGGER fail_order')
        reader_sync.init_db()
        self.assertEqual([row['episode_id'] for row in self.request('/app/api/work/69shuba-999')[1]['episodes']], ['3', '2', '1'])

    def test_browser_shuba_next_and_latest_use_repaired_order(self):
        from playwright.sync_api import expect, sync_playwright
        executable = os.environ.get('READER_TEST_BROWSER', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        if not Path(executable).exists(): self.skipTest('Local Chromium is unavailable')
        self.descending_source(['原文段落。'*8]*100)
        for episode in ('1', '2', '3'):
            self.request('/app/api/episode/69shuba-999/'+episode+'/translate', {})
        def translate(payload):
            result = json.loads(self.fake_response(payload))
            items = json.loads(payload['input'][0]['content'])['items']
            for source, target in zip(items, result['items']):
                if source['id'] == 'title': target['text'] = '제'+re.search(r'\d+', source['text'])[0]+'장 시험'
            return json.dumps(result)
        with patch.object(reader_openai, 'response_text', side_effect=translate):
            for _ in range(3): reader_translate.run_one(reader_sync.connect)
        reader_sync.init_db()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                for viewport in ({'width': 390, 'height': 844}, {'width': 1280, 'height': 900}):
                    with reader_sync.connect() as db:
                        old = db.execute("SELECT revision FROM progress WHERE work_id='69shuba-999'").fetchone()
                    reader_sync.store_progress({'kind': 'novel', 'work_id': '69shuba-999', 'episode_id': '2', 'position': .42,
                                                'expected_revision': old[0] if old else 0, 'allow_rewind': True})
                    context = browser.new_context(viewport=viewport)
                    context.add_cookies([{'name': self.cookie.split('=', 1)[0], 'value': self.cookie.split('=', 1)[1], 'url': self.base}])
                    page = context.new_page()
                    page.goto(self.base+'/app?work=69shuba-999&ep=2')
                    expect(page.locator('#current-episode')).to_have_text('현재 109화')
                    expect(page.locator('#manual-save')).to_be_enabled()
                    page.wait_for_timeout(100)
                    self.assertAlmostEqual(page.evaluate('scrollY/(document.documentElement.scrollHeight-innerHeight)'), .42, delta=.01)
                    page.evaluate('scrollBy(0,-30)')
                    page.locator('#next').click()
                    expect(page.locator('#current-episode')).to_have_text('현재 110화')
                    expect(page.locator('#next')).to_be_disabled()
                    page.wait_for_timeout(900)
                    self.assertEqual(self.request('/app/api/work/69shuba-999')[1]['progress']['episode_id'], '1')
                    page.locator('#previous').click()
                    expect(page.locator('#current-episode')).to_have_text('현재 109화')
                    expect(page.locator('#progress-notice')).to_be_visible()
                    expect(page.locator('#progress-notice-text')).to_contain_text('第110章')
                    page.wait_for_timeout(900)
                    self.assertEqual(self.request('/app/api/work/69shuba-999')[1]['progress']['episode_id'], '1')
                    page.locator('#previous').click()
                    expect(page.locator('#current-episode')).to_have_text('현재 108화')
                    page.locator('#resume-latest').click()
                    expect(page.locator('#current-episode')).to_have_text('현재 110화')
                    expect(page.locator('#progress-notice')).to_be_hidden()
                    context.close()
            finally:
                browser.close()

    def test_original_prefetch_never_translates(self):
        base = self.source()
        self.request('/app/api/manage')
        self.request('/app/api/work/69shuba-999')
        self.assertIsNone(self.request(base)[1]['episode']['paragraphs'])
        self.assertEqual(self.request(base)[1]['translation']['state'], 'missing')
        self.assertEqual(self.request(base + '?language=zh')[1]['episode']['paragraphs'], ['第一段。', '第二段。'])
        self.assertEqual(self.request(base + '?language=invalid')[0], 400)
        with reader_sync.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reader_translations').fetchone()[0], 0)
        self.assertFalse(reader_translate.run_one(reader_sync.connect))

    def test_deduplicated_translation_and_cache(self):
        base = self.source()
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda _: self.request(base + '/translate', {}), range(3)))
        self.assertTrue(all(result[0] == 200 for result in results))
        with reader_sync.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reader_translations').fetchone()[0], 1)
        with patch.object(reader_openai, 'response_text', side_effect=self.fake_response) as call:
            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(sorted(pool.map(lambda _: reader_translate.run_one(reader_sync.connect), range(2))), [False, True])
            ready = self.request(base)[1]
            self.assertEqual(ready['translation']['state'], 'ready')
            self.assertEqual(ready['episode']['paragraphs'], ['한국어 번역 p0:0', '한국어 번역 p1:0'])
            self.assertEqual(self.request(base + '?language=zh')[1]['episode']['paragraphs'], ['第一段。', '第二段。'])
            self.request(base + '/translate', {})
            self.assertFalse(reader_translate.run_one(reader_sync.connect))
            self.assertEqual(call.call_count, 1)
        self.assertEqual(self.request('/app/api/manage')[1]['works'][0]['translated'], 1)

    def test_translation_status_reports_last_stage_time(self):
        base = self.source()
        self.request(base + '/translate', {})
        with reader_sync.connect() as db:
            db.execute('UPDATE reader_translations SET updated_at=1')
        queued = self.request(base)[1]['translation']
        self.assertEqual((queued['state'], queued['updated_at']), ('queued', 1))
        def translated(payload):
            running = self.request(base)[1]['translation']
            self.assertEqual(running['state'], 'running')
            self.assertGreater(running['updated_at'], queued['updated_at'])
            return self.fake_response(payload)
        with patch.object(reader_openai, 'response_text', side_effect=translated):
            reader_translate.run_one(reader_sync.connect)
        self.assertEqual(self.request(base)[1]['translation']['state'], 'ready')

    def test_browser_translation_polling_keeps_screen_and_explains_status(self):
        from playwright.sync_api import expect, sync_playwright
        executable = os.environ.get('READER_TEST_BROWSER', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        if not Path(executable).exists(): self.skipTest('Local Chromium is unavailable')
        base = self.source()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                for viewport in ({'width': 390, 'height': 844}, {'width': 1280, 'height': 900}):
                    with reader_sync.connect() as db:
                        db.execute('DELETE FROM reader_translations')
                    context = browser.new_context(viewport=viewport)
                    context.add_cookies([{'name': self.cookie.split('=', 1)[0], 'value': self.cookie.split('=', 1)[1], 'url': self.base}])
                    page = context.new_page()
                    requests = []
                    page.on('request', lambda request: requests.append((request.method, urlparse(request.url).path)))
                    page.goto(self.base + '/app?work=69shuba-999&ep=1')
                    expect(page.locator('#reader-message')).to_contain_text('번역 대기')
                    page.evaluate('''() => {
                        window.readerMessages=[];window.readerScrolls=[];
                        new MutationObserver(() => readerMessages.push(document.getElementById('reader-message').textContent))
                            .observe(document.getElementById('reader-message'),{childList:true,subtree:true});
                        window.scrollTo=(...args)=>readerScrolls.push(args);
                    }''')
                    page.wait_for_timeout(2300)
                    self.assertFalse(any('본문 불러오는 중' in message for message in page.evaluate('readerMessages')))
                    self.assertEqual(page.evaluate('readerScrolls'), [])
                    expect(page.locator('#translation-progress')).to_contain_text('전체 1묶음 중 0묶음 완료')
                    with reader_sync.connect() as db:
                        db.execute("UPDATE reader_translations SET state='running',updated_at=?", (int(time.time()*1000)-65000,))
                    expect(page.locator('#reader-message')).to_contain_text('번역 중', timeout=5000)
                    expect(page.locator('#translation-progress')).to_contain_text('현재 단계 1분')
                    expect(page.locator('#translation-progress')).to_contain_text('1번째 묶음 응답 대기')
                    expect(page.locator('#manual-save')).to_be_disabled()
                    page.route('**/app/api/episode/69shuba-999/1?language=ko', lambda route: route.abort())
                    expect(page.locator('#reader-message')).to_contain_text('상태 확인 실패', timeout=5000)
                    expect(page.locator('#reader-message')).to_contain_text('번역 실패로 확정된 것은 아닙니다')
                    page.unroute('**/app/api/episode/69shuba-999/1?language=ko')
                    expect(page.locator('#reader-message')).to_contain_text('번역 중', timeout=5000)
                    with reader_sync.connect() as db:
                        db.execute("UPDATE reader_translations SET state='error',code='quota',error='ChatGPT 사용 한도'")
                    expect(page.locator('#reader-message')).to_contain_text('번역 실패', timeout=5000)
                    expect(page.locator('#reader-message')).to_contain_text('ChatGPT 사용 한도')
                    expect(page.locator('#translation-progress')).to_contain_text('자동 재시도하지 않습니다')
                    expect(page.locator('#retry-translation')).to_be_visible()
                    self.assertEqual(sum(method == 'POST' and path == base + '/translate' for method, path in requests), 1)
                    with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
                        page.locator('#retry-translation').click()
                        page.wait_for_timeout(100)
                        reader_translate.run_one(reader_sync.connect)
                    expect(page.locator('#content p')).to_have_count(2, timeout=5000)
                    expect(page.locator('#translation-progress')).to_be_hidden()
                    expect(page.locator('#content p').first).to_contain_text('한국어 번역')
                    expect(page.locator('#manual-save')).to_be_enabled()
                    # A late Korean status response must not overwrite original preview.
                    translated = self.request(base)[1]
                    held = []
                    page.route('**/app/api/episode/69shuba-999/1?language=ko', lambda route: held.append(route))
                    page.reload(wait_until='domcontentloaded')
                    page.wait_for_timeout(300)
                    self.assertEqual(len(held), 1)
                    page.locator('#reader-language').click()
                    expect(page.locator('#reader-message')).to_contain_text('원문 미리보기')
                    held[0].fulfill(status=200, json=translated)
                    page.wait_for_timeout(200)
                    expect(page.locator('#content p').first).to_contain_text('第一段')
                    expect(page.locator('#manual-save')).to_be_disabled()
                    context.close()
            finally:
                browser.close()

    def test_legacy_progress_and_source_family(self):
        self.source()
        self.assertEqual(self.request('/app/api/work/69shuba-999/source', {'host': 'newtoki1.org'})[0], 400)
        self.assertEqual(self.request('/app/api/work/999/source', {'host': 'www.69shuba.com'})[0], 400)
        saved = self.request('/app/api/progress', {'work_id': '69shuba-999', 'episode_id': '1', 'position': .4, 'expected_revision': 0})
        self.assertEqual(saved[0], 200)
        remote = self.request('/v1/progress', bearer=True)[1]['progress']
        self.assertEqual([row['work_id'] for row in remote], ['999'])
        self.assertEqual(self.request('/app/api/work/999')[1]['progress']['position'], .6)
        library = self.request('/app/api/library')[1]['works']
        self.assertEqual({row['work_id'] for row in library}, {'999', '69shuba-999'})

    def test_chunking_partial_retry_and_no_auto_retry(self):
        original = '中' * 12500
        batches = reader_translate.chunks('标题', [original, '结尾。'])
        self.assertTrue(all(sum(len(item['text']) for item in batch) <= 6000 for batch in batches))
        self.assertEqual(''.join(item['text'] for batch in batches for item in batch if item['id'].startswith('p0:')), original)
        base = self.source([original])
        self.request(base + '/translate', {})
        count = 0
        def fail_once(payload):
            nonlocal count
            count += 1
            if count == 2: raise reader_openai.OpenAIError('quota', '사용 한도')
            return self.fake_response(payload)
        with patch.object(reader_openai, 'response_text', side_effect=fail_once):
            reader_translate.run_one(reader_sync.connect)
            failed = self.request(base)[1]
            self.assertEqual(failed['translation']['state'], 'error')
            self.assertEqual(failed['translation']['completed'], 1)
            self.assertIsNone(failed['episode']['paragraphs'])
            self.request(base + '/translate', {})
            self.assertFalse(reader_translate.run_one(reader_sync.connect))
            self.assertEqual(count, 2)
            self.request(base + '/translate', {'retry': True})
            reader_translate.run_one(reader_sync.connect)
        self.assertEqual(count, len(batches) + 1)
        self.assertEqual(self.request(base)[1]['translation']['state'], 'ready')

    def test_bad_output_never_finishes(self):
        base = self.source()
        self.request(base + '/translate', {})
        with patch.object(reader_openai, 'response_text', return_value='{"items":[]}'):
            reader_translate.run_one(reader_sync.connect)
        result = self.request(base)[1]
        self.assertEqual(result['translation']['state'], 'error')
        self.assertIsNone(result['episode']['paragraphs'])

    def test_deletion_invalidates_late_translation(self):
        base = self.source()
        self.request(base + '/translate', {})
        def deleting(payload):
            self.assertEqual(self.request('/app/api/manage/delete', {'work_id': '69shuba-999', 'confirm': True})[0], 200)
            return self.fake_response(payload)
        with patch.object(reader_openai, 'response_text', side_effect=deleting):
            reader_translate.run_one(reader_sync.connect)
        with reader_sync.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reader_translations').fetchone()[0], 0)
        self.assertEqual(self.request('/app/api/work/999')[1]['progress']['position'], .6)

    def test_backup_restore_and_cache_clear(self):
        base = self.source()
        self.request(base + '/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
            reader_translate.run_one(reader_sync.connect)
        backup = self.request('/app/api/manage/backup?work_id=69shuba-999')[1]
        self.assertEqual(backup['version'], 2)
        self.assertEqual(len(backup['translations']), 1)
        self.assertNotIn('access_token', json.dumps(backup))
        original = self.request('/app/api/manage/backup?work_id=999')[1]
        self.assertEqual(original['version'], 1)
        self.assertEqual(self.request('/app/api/manage/cache-clear', {'work_id': '69shuba-999', 'confirm': True})[0], 200)
        self.assertIsNone(self.request(base + '?language=zh')[1]['episode']['paragraphs'])
        self.assertEqual(self.request('/app/api/manage/restore', {'backup': backup, 'confirm': True})[0], 200)
        self.assertEqual(self.request(base)[1]['translation']['state'], 'ready')
        self.assertEqual(self.request(base)[1]['episode']['paragraphs'], ['한국어 번역 p0:0', '한국어 번역 p1:0'])
        corrupt = json.loads(json.dumps(backup))
        corrupt['translations'][0]['source_hash'] = 'wrong'
        self.assertEqual(self.request('/app/api/manage/restore', {'backup': corrupt, 'confirm': True})[0], 400)

    def test_source_change_invalidates_cache_and_capacity_counts_translation(self):
        base = self.source()
        self.request(base + '/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
            reader_translate.run_one(reader_sync.connect)
        with reader_sync.connect() as db:
            raw = db.execute('SELECT sum(length(CAST(paragraphs_json AS BLOB))) FROM reader_episodes').fetchone()[0]
            self.assertGreater(support.reader_app.cache_bytes(db, '69shuba-999'), raw)
            db.execute("UPDATE reader_episodes SET paragraphs_json='[\"changed\"]' WHERE work_id='69shuba-999' AND episode_id='1'")
        self.assertEqual(self.request(base)[1]['translation']['state'], 'missing')
        self.assertIsNone(self.request(base)[1]['episode']['paragraphs'])

    def test_account_api_is_private_and_tokens_hidden(self):
        self.assertEqual(self.request('/app/api/translation/account', cookie=False, bearer=True)[0], 401)
        self.assertEqual(self.request('/app/api/translation/disconnect', {}, app_header=False)[0], 403)
        record = {'client_id': 'client', 'email': 'reader@example.invalid', 'access_token': 'secret-access',
                  'refresh_token': 'secret-refresh', 'id_token': 'secret-identity', 'model_available': True}
        reader_openai.atomic_json(reader_openai.state_dir() / 'credentials.json', record)
        status = self.request('/app/api/translation/account')[1]
        self.assertTrue(status['connected'])
        self.assertNotIn('secret', json.dumps(status))
        with patch.object(reader_openai, 'discovery', return_value={'revocation_endpoint': reader_openai.AUTH + '/revoke'}), patch.object(reader_openai, 'open_request', side_effect=reader_openai.OpenAIError('network', 'failed')):
            result = self.request('/app/api/translation/disconnect', {})
        self.assertEqual(result[0], 200)
        self.assertIn('원격', result[1]['message'])
        self.assertFalse(self.request('/app/api/translation/account')[1]['connected'])
        self.assertNotIn('access_token', reader_openai.load_credentials())

    def test_capacity_stops_translation_before_external_request(self):
        base = self.source(['中' * 120000])
        with reader_sync.connect() as db:
            db.execute("INSERT INTO reader_work_settings VALUES('69shuba-999',0,2,1)")
        self.request(base + '/translate', {})
        with patch.object(reader_openai, 'response_text') as request:
            reader_translate.run_one(reader_sync.connect)
            request.assert_not_called()
        self.assertEqual(self.request(base)[1]['translation']['code'], 'capacity')

    def test_browser_shuba_source_and_reader(self):
        from playwright.sync_api import expect, sync_playwright
        executable = os.environ.get('READER_TEST_BROWSER', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        if not Path(executable).exists(): self.skipTest('Local Chromium is unavailable')
        base = self.source(['原文段落。' * 8] * 120)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                for viewport in ({'width': 390, 'height': 844}, {'width': 1280, 'height': 900}):
                    with reader_sync.connect() as db:
                        db.execute('DELETE FROM reader_translations')
                        db.execute("DELETE FROM progress WHERE work_id='69shuba-999'")
                    context = browser.new_context(viewport=viewport)
                    context.add_cookies([{'name': self.cookie.split('=', 1)[0], 'value': self.cookie.split('=', 1)[1], 'url': self.base}])
                    page = context.new_page()
                    page.goto(self.base + '/app?work=69shuba-999&ep=1')
                    expect(page.locator('#reader-message')).to_contain_text('번역')
                    expect(page.locator('#content p')).to_have_count(0)
                    expect(page.locator('#manual-save')).to_be_disabled()
                    with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
                        reader_translate.run_one(reader_sync.connect)
                    expect(page.locator('#content p')).to_have_count(120, timeout=6000)
                    expect(page.locator('#manual-save')).to_be_enabled()
                    page.evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*.4)')
                    page.wait_for_timeout(900)
                    page.evaluate('scrollBy(0,-30)')
                    page.locator('#reader-language').click()
                    expect(page.locator('#reader-message')).to_contain_text('원문 미리보기')
                    expect(page.locator('#content p').first).to_contain_text('原文')
                    expect(page.locator('#manual-save')).to_be_disabled()
                    before = self.request('/app/api/work/69shuba-999')[1]['progress']
                    page.evaluate('scrollTo(0,document.documentElement.scrollHeight)')
                    page.wait_for_timeout(900)
                    page.reload()
                    expect(page.locator('#reader-message')).to_contain_text('원문 미리보기')
                    self.assertEqual(self.request('/app/api/work/69shuba-999')[1]['progress'], before)
                    page.locator('#reader-language').click()
                    expect(page.locator('#content p').first).to_contain_text('한국어 번역')
                    expect(page.locator('#manual-save')).to_be_enabled()
                    page.wait_for_timeout(100)
                    self.assertAlmostEqual(page.evaluate('scrollY/(document.documentElement.scrollHeight-innerHeight)'), before['position'], delta=.01)
                    with reader_sync.connect() as db:
                        db.execute('DELETE FROM reader_translations')
                    page.reload()
                    expect(page.locator('#reader-message')).to_contain_text('번역')
                    with patch.object(reader_openai, 'response_text', side_effect=reader_openai.OpenAIError('quota', 'ChatGPT 사용 한도')):
                        reader_translate.run_one(reader_sync.connect)
                    expect(page.locator('#retry-translation')).to_be_visible(timeout=6000)
                    expect(page.locator('#manual-save')).to_be_disabled()
                    self.assertEqual(self.request('/app/api/work/69shuba-999')[1]['progress'], before)
                    with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
                        page.locator('#retry-translation').click()
                        page.wait_for_timeout(100)
                        reader_translate.run_one(reader_sync.connect)
                    expect(page.locator('#content p')).to_have_count(120, timeout=6000)
                    context.close()
                # Browser-based source parsing uses the public DOM, not private APIs.
                context = browser.new_context()
                page = context.new_page()
                fixture = '<title>书最新章节</title><h1>作品</h1><a href="/txt/999/2">第2章 后续</a><a href="/txt/999/1">第1章 起点</a>'
                chapter = '<div class="txtnav"><h1>标题</h1><div class="txtinfo">广告信息</div><script>bad()</script><button>按钮</button>' + ('原文段落。' * 20) + '<br>最后一段。<br>(本章完)</div>'
                @contextmanager
                def source_page(*_): yield page
                with patch.object(reader_crawl, 'source_page', source_page):
                    for automatic_check in (False, True):
                        documents = []
                        def serve(route):
                            if route.request.resource_type == 'document': documents.append(route.request.url)
                            if automatic_check and 'approved=1' not in route.request.url:
                                route.fulfill(status=403, content_type='text/html; charset=utf-8',
                                    headers={'cf-mitigated': 'challenge'}, body='<title>Just a moment...</title>'
                                    '<script>setTimeout(()=>location.replace(location.pathname+"?approved=1"),200)</script>')
                            else:
                                route.fulfill(status=200, content_type='text/html; charset=utf-8',
                                    body=chapter if '/txt/' in route.request.url else fixture)
                        page.route('**/*', serve)
                        title, links = reader_crawl.collect_shuba_list('https://www.69shuba.com/book/999.htm', '69shuba-999', browser)
                        self.assertEqual((title, [item[0] for item in links]), ('作品', ['1', '2']))
                        paragraphs = reader_crawl.extract_episode(browser, 'https://www.69shuba.com/txt/999/1', '69shuba-999', '1')
                        self.assertEqual(paragraphs, ['原文段落。' * 20, '最后一段。'])
                        before = len(documents)
                        self.assertEqual(reader_crawl.extract_episode(browser, 'https://www.69shuba.com/txt/999/1', '69shuba-999', '1'), paragraphs)
                        self.assertEqual(len(documents), before, 'A loaded chapter must not navigate back into verification')
                        page.unroute('**/*', serve)
                context.close()
            finally:
                browser.close()


class OAuthTest(unittest.TestCase):
    def setUp(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.jwt = jwt
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'READER_OPENAI_DIR': self.temp.name})
        self.environment.start()
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key())) | {'kid': 'test', 'use': 'sig'}
        self.metadata = {'issuer': reader_openai.AUTH, 'jwks_uri': reader_openai.AUTH + '/jwks', 'revocation_endpoint': reader_openai.AUTH + '/revoke'}
        reader_openai.discovery.cache_clear()
        self.requests = patch.object(reader_openai, 'request_json', side_effect=self.response)
        self.requests.start()

    def tearDown(self):
        self.requests.stop()
        reader_openai.discovery.cache_clear()
        self.environment.stop()
        self.temp.cleanup()

    def identity(self, **updates):
        claims = {'iss': reader_openai.AUTH, 'aud': 'client', 'sub': 'owner', 'exp': int(time.time()) + 3600,
                  'nonce': 'nonce', 'email': 'reader@example.invalid'}
        claims.update(updates)
        return self.jwt.encode(claims, self.private, algorithm='RS256', headers={'kid': 'test'})

    def tokens(self):
        return {'access_token': 'access', 'refresh_token': 'refresh', 'id_token': self.identity(),
                'expires_in': 3600, 'scope': ' '.join(reader_openai.SCOPES)}

    def record(self):
        return reader_openai.checked_tokens(self.tokens(), 'client', 'nonce', 'owner')

    def response(self, url, **kwargs):
        if url.endswith('openid-configuration'): return self.metadata
        if url.endswith('/jwks'): return {'keys': [self.jwk]}
        if url.endswith('/models'): return {'models': [{'slug': reader_openai.MODEL, 'visibility': 'list'}]}
        raise AssertionError('Unexpected network call')

    def test_identity_and_permissions_validation(self):
        self.assertEqual(self.record()['subject'], 'owner')
        for update in ({'iss': 'https://evil.invalid'}, {'aud': 'wrong'}, {'nonce': 'wrong'}, {'sub': 'other'}, {'exp': 1}):
            with self.subTest(update=update), self.assertRaises(reader_openai.OpenAIError):
                reader_openai.verify_identity(self.identity(**update), 'client', 'nonce', 'owner')
        bad = self.tokens() | {'scope': 'openid profile email'}
        with self.assertRaises(reader_openai.OpenAIError): reader_openai.checked_tokens(bad, 'client', 'nonce')

    def test_import_preserves_old_connection_on_bad_model(self):
        reader_openai.import_credentials(self.record() | {'nonce': 'nonce'})
        before = reader_openai.load_credentials()
        with patch.object(reader_openai, 'available_model', return_value=False), self.assertRaises(reader_openai.OpenAIError):
            reader_openai.import_credentials(self.record() | {'nonce': 'nonce'})
        self.assertEqual(reader_openai.load_credentials(), before)
        self.assertTrue(reader_openai.account_status()['model_available'])
        if os.name == 'posix': self.assertEqual((reader_openai.state_dir() / 'credentials.json').stat().st_mode & 0o777, 0o600)

    def test_refresh_serialized_and_replacement_saved(self):
        record = self.record() | {'expires_at': 1, 'model_available': True}
        reader_openai.atomic_json(reader_openai.state_dir() / 'credentials.json', record)
        count = 0
        def refresh(url, **kwargs):
            nonlocal count
            self.assertEqual(kwargs['data']['client_id'], 'client')
            self.assertNotIn('scope', kwargs['data'])
            count += 1
            time.sleep(.05)
            return {'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600}
        with patch.object(reader_openai, 'request_json', side_effect=refresh):
            with ThreadPoolExecutor(3) as pool:
                self.assertEqual(list(pool.map(lambda _: reader_openai.access_token(), range(3))), ['new-access'] * 3)
        self.assertEqual(count, 1)
        self.assertEqual(reader_openai.load_credentials()['refresh_token'], 'new-refresh')

    def test_stream_completion_and_single_auth_retry(self):
        stream = b'data: {"type":"response.output_text.delta","delta":"done"}\n\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        with patch.object(reader_openai, 'access_token', side_effect=['old', 'new']) as tokens, patch.object(reader_openai, 'open_request', side_effect=[reader_openai.OpenAIError('auth', 'expired'), io.BytesIO(stream)]) as requests:
            self.assertEqual(reader_openai.response_text({}), 'done')
            self.assertEqual(requests.call_count, 2)
            tokens.assert_any_call(force=True, rejected_token='old')
        for raw in (b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
                    b'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}}\n\n',
                    b'data: {"type":"response.incomplete"}\n\n',
                    b'data: {"type":"response.output_text.delta","delta":"partial"}\n\ndata: {"type":"response.completed"}\n\n'):
            with patch.object(reader_openai, 'access_token', return_value='token'), patch.object(reader_openai, 'open_request', return_value=io.BytesIO(raw)), self.assertRaises(reader_openai.OpenAIError):
                reader_openai.response_text({})

    def test_refresh_failure_requires_reconnection_without_repeating(self):
        reader_openai.atomic_json(reader_openai.state_dir() / 'credentials.json', self.record() | {'expires_at': 1, 'model_available': True})
        with patch.object(reader_openai, 'request_json', side_effect=reader_openai.OpenAIError('request', 'rejected')) as request:
            for _ in range(2):
                with self.assertRaises(reader_openai.OpenAIError): reader_openai.access_token()
            self.assertEqual(request.call_count, 1)
        self.assertFalse(reader_openai.account_status()['connected'])
        record = self.record() | {'expires_at': 1, 'model_available': True}
        reader_openai.atomic_json(reader_openai.state_dir() / 'credentials.json', record)
        with patch.object(reader_openai, 'request_json', return_value={'access_token': 'new', 'refresh_token': [], 'expires_in': 3600}), self.assertRaises(reader_openai.OpenAIError):
            reader_openai.access_token()
        self.assertEqual(reader_openai.load_credentials()['refresh_token'], record['refresh_token'])

    def test_loopback_pkce_flow(self):
        opened = {}
        callback_thread = None
        original = self.response
        def response(url, **kwargs):
            if url.endswith('/oauth/token'):
                self.assertEqual(kwargs['data']['client_id'], 'client')
                verifier = kwargs['data']['code_verifier']
                import base64, hashlib
                self.assertEqual(opened['code_challenge'][0], base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='))
                self.assertEqual(kwargs['data']['redirect_uri'], opened['redirect_uri'][0])
                return self.tokens() | {'id_token': self.identity(nonce=opened['nonce'][0])}
            return original(url, **kwargs)
        def browser(url):
            nonlocal callback_thread
            opened.update(parse_qs(urlparse(url).query))
            callback = opened['redirect_uri'][0] + '?' + urlencode({'state': opened['state'][0], 'code': 'code', 'client_id': 'client'})
            callback_thread = threading.Thread(target=lambda: urlopen(callback, timeout=5).read())
            callback_thread.start()
            return True
        with patch.object(reader_openai.webbrowser, 'open', side_effect=browser), patch.object(reader_openai, 'request_json', side_effect=response):
            record = reader_openai.authorize(local_directory=Path(self.temp.name) / 'pc')
        callback_thread.join(timeout=5)
        self.assertEqual(record['subject'], 'owner')
        self.assertEqual(opened['client_id'], ['dynamic_agent_client'])
        self.assertEqual(urlparse(opened['redirect_uri'][0]).hostname, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
