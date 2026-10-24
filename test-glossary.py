"""Offline per-work terminology and non-destructive translation checks."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

support = importlib.import_module('test-translation')
reader_sync = support.reader_sync
reader_translate = support.reader_translate
reader_openai = support.reader_openai
reader_app = support.support.reader_app


class GlossaryTest(unittest.TestCase):
    setUp = support.TranslationTest.setUp
    tearDown = support.TranslationTest.tearDown
    request = support.TranslationTest.request
    login = support.TranslationTest.login
    source = support.TranslationTest.source
    fake_response = staticmethod(support.TranslationTest.fake_response)

    def save(self, terms, work_id='69shuba-999', **options):
        return self.request('/app/api/manage/glossary', {'work_id': work_id, 'glossary': terms}, **options)

    def glossary(self, work_id='69shuba-999'):
        with reader_sync.connect() as db:
            return reader_translate.glossary(db, work_id)

    def test_additive_migration_preserves_old_rows_and_cache_keys(self):
        self.source()
        with reader_sync.connect() as db:
            db.execute('ALTER TABLE reader_work_settings DROP COLUMN glossary_json')
            before = tuple(db.execute("SELECT rowid,* FROM reader_work_settings").fetchone())
            episode = db.execute("SELECT * FROM reader_episodes WHERE work_id='69shuba-999' LIMIT 1").fetchone()
            key = reader_translate.episode_key(episode)
        reader_sync.init_db()
        reader_sync.init_db()
        with reader_sync.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT rowid,work_id,paused,prefetch,cache_limit_mb,translation_prefetch,translation_anchor FROM reader_work_settings').fetchone()), before)
            self.assertEqual(reader_translate.glossary(db, '69shuba-999'), {})
            self.assertEqual(reader_translate.episode_key(episode), key)
        self.assertEqual(reader_translate.PROMPT_VERSION, 1)

    def test_validation_authentication_and_work_isolation(self):
        self.source()
        for bad in (None, [], {'顾安': 'Gu An'}, {'顾安': '<script>'}, {'ignore rules': '고안'}, {'顾安': ''}, {'顾安': '고'*65}):
            self.assertEqual(self.save(bad)[0], 400, bad)
        self.assertEqual(self.save({'顾安': '고안'}, cookie=False, bearer=True)[0], 401)
        self.assertEqual(self.save({'顾安': '고안'}, app_header=False)[0], 403)
        self.assertEqual(self.save({'顾安': '고안'}, work_id='999')[0], 400)
        self.assertEqual(self.save({'顾安': '고안'}, work_id='69shuba-123')[0], 404)
        self.assertEqual(self.save({'顾安': '고안', '太玄门': '태현문'})[0], 200)
        self.assertEqual(self.glossary(), {'顾安': '고안', '太玄门': '태현문'})
        self.assertEqual(self.glossary('69shuba-123'), {})
        with self.assertRaises(ValueError):
            reader_translate.checked_glossary({'名'+str(n): '표기' for n in range(201)})
        self.request('/app/api/manage/settings', {'work_id': '69shuba-999', 'settings': {'paused': 1}})
        self.assertEqual(self.glossary()['顾安'], '고안')

    def test_known_terms_reach_each_request_without_changing_cached_translation(self):
        base = self.source(['顾安来到太玄门。'])
        self.save({'顾安': '고안', '太玄门': '태현문', '李涯': '이애'})
        before = self.request('/app/api/work/999')[1]['progress']
        def translated(payload, **kwargs):
            body = json.loads(payload['input'][0]['content'])
            self.assertEqual(body['glossary'], {'顾安': '고안', '太玄门': '태현문'})
            self.assertIn('한국 한자 독음', payload['instructions'])
            self.assertIn('중국어 발음', payload['instructions'])
            self.assertIn('호칭', payload['instructions'])
            self.assertIn('terms', payload['text']['format']['schema']['required'])
            return json.dumps({'items': [{'id': item['id'], 'text': '고안이 태현문에 왔다.'} for item in body['items']],
                               'terms': [{'source': '顾安', 'target': '구안'}]})
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=translated) as call:
            reader_translate.run_one(reader_sync.connect)
            ready = self.request(base)[1]
            self.save({'顾安': '고안', '太玄门': '태현문', '李涯': '이애', '新名': '신명'})
            self.request(base+'/translate', {'retry': True})
            self.assertFalse(reader_translate.run_one(reader_sync.connect))
            self.assertEqual(call.call_count, 1)
        self.assertEqual(self.request(base)[1]['episode']['paragraphs'], ready['episode']['paragraphs'])
        self.assertEqual(self.glossary()['顾安'], '고안')
        self.assertEqual(self.request('/app/api/work/999')[1]['progress'], before)

    def test_new_terms_are_shared_between_chunks_and_chapters_without_extra_calls(self):
        base = self.source(['姜琼。'+'中'*6100, '姜琼又来了。'])
        calls = []
        def translated(payload, **kwargs):
            body = json.loads(payload['input'][0]['content'])
            calls.append(body)
            if len(calls) > 1 and any('姜琼' in item['text'] for item in body['items']):
                self.assertEqual(body['glossary']['姜琼'], '강경')
            return json.dumps({'items': [{'id': item['id'], 'text': '강경의 이야기'} for item in body['items']],
                               'terms': [{'source': '姜琼', 'target': '강경'}]})
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=translated) as call:
            reader_translate.run_one(reader_sync.connect)
            self.request('/app/api/episode/69shuba-999/2/translate', {})
            reader_translate.run_one(reader_sync.connect)
        self.assertEqual(self.glossary(), {'姜琼': '강경'})
        self.assertEqual(call.call_count, len(reader_translate.chunks('第1章 试验', ['姜琼。'+'中'*6100, '姜琼又来了。']))*2)

    def test_bad_suggestions_cannot_poison_the_glossary_or_drop_valid_content(self):
        base = self.source(['顾安来了。'])
        self.save({'顾安': '고안'})
        def translated(payload, **kwargs):
            value = json.loads(self.fake_response(payload))
            value['terms'] = [{'source': '顾安', 'target': '구안'}, {'source': '不存在', 'target': '신명'},
                              {'source': '顾安', 'target': 'Gu An'}, {'source': '来了', 'target': '오다'},
                              {'source': '<script>', 'target': '이름'}, None]
            return json.dumps(value)
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=translated):
            reader_translate.run_one(reader_sync.connect)
        self.assertEqual(self.glossary(), {'顾安': '고안'})
        self.assertEqual(self.request(base)[1]['translation']['state'], 'ready')

    def test_late_result_cannot_recreate_deleted_work_or_override_manual_terms(self):
        base = self.source(['顾安来了。'])
        self.request(base+'/translate', {})
        def translated(payload, **kwargs):
            self.save({'顾安': '고안'})
            value = json.loads(self.fake_response(payload))
            value['terms'] = [{'source': '顾安', 'target': '구안'}]
            value['items'][0]['text'] = '구안'
            return json.dumps(value)
        with patch.object(reader_openai, 'response_text', side_effect=translated):
            reader_translate.run_one(reader_sync.connect)
        self.assertEqual(self.glossary(), {'顾安': '고안'})
        self.request('/app/api/episode/69shuba-999/2/translate', {})
        def deleting(payload, **kwargs):
            self.request('/app/api/manage/delete', {'work_id': '69shuba-999', 'confirm': True})
            return translated(payload)
        with patch.object(reader_openai, 'response_text', side_effect=deleting):
            reader_translate.run_one(reader_sync.connect)
        self.assertEqual(self.glossary(), {})
        with reader_sync.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reader_work_settings WHERE work_id='69shuba-999'").fetchone()[0], 0)

    def test_backup_restore_clear_and_delete_cover_glossary_lifecycle(self):
        base = self.source()
        self.save({'顾安': '고안'})
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
            reader_translate.run_one(reader_sync.connect)
        backup = self.request('/app/api/manage/backup?work_id=69shuba-999')[1]
        self.assertEqual(backup['version'], 2)
        self.assertEqual(backup['glossary'], {'顾安': '고안'})
        self.assertNotIn('glossary', self.request('/app/api/manage/backup?work_id=999')[1])
        self.request('/app/api/manage/cache-clear', {'work_id': '69shuba-999', 'confirm': True})
        self.assertEqual(self.glossary(), {'顾安': '고안'})
        self.save({'李涯': '이애'})
        self.assertEqual(self.request('/app/api/manage/restore', {'backup': backup, 'confirm': True})[0], 200)
        self.assertEqual(self.glossary(), {'顾安': '고안'})
        self.assertEqual(self.request(base)[1]['translation']['state'], 'ready')
        del backup['glossary']
        self.assertEqual(self.request('/app/api/manage/restore', {'backup': backup, 'confirm': True})[0], 200)
        self.assertEqual(self.glossary(), {})
        self.request('/app/api/manage/delete', {'work_id': '69shuba-999', 'confirm': True})
        self.assertEqual(self.glossary(), {})

    def test_browser_work_glossary_editing_is_mobile_safe_and_keeps_cached_content(self):
        from playwright.sync_api import expect, sync_playwright
        executable = os.environ.get('READER_TEST_BROWSER', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        if not Path(executable).exists():
            self.skipTest('Local Chromium is unavailable')
        base = self.source()
        self.save({'顾安': '고안'})
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.fake_response):
            reader_translate.run_one(reader_sync.connect)
        cached = self.request(base)[1]['episode']['paragraphs']
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                for viewport in ({'width': 390, 'height': 844}, {'width': 1280, 'height': 900}):
                    context = browser.new_context(viewport=viewport)
                    context.add_cookies([{'name': self.cookie.split('=',1)[0], 'value': self.cookie.split('=',1)[1], 'url': self.base}])
                    page = context.new_page()
                    page.goto(self.base+'/app/manage')
                    card = page.locator('#manage-list [data-work-id="69shuba-999"]')
                    editor = card.get_by_label('작품별 용어집')
                    expect(editor).to_have_value('顾安=고안')
                    expect(page.locator('#manage-list [data-work-id="999"]').get_by_label('작품별 용어집')).to_have_count(0)
                    editor.fill('顾安=고안\n李涯=이애')
                    card.get_by_role('button', name='용어집 저장', exact=True).click()
                    expect(page.locator('#manage-message')).to_contain_text('기존 번역과 읽은 위치는 유지')
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])
                    self.assertEqual(self.request(base)[1]['episode']['paragraphs'], cached)
                    self.save({'顾安': '고안'})
                    context.close()
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
