"""Offline token accounting, stream metadata and non-billing monitoring checks."""
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

support = importlib.import_module('test-translation')
reader_app = support.support.reader_app
reader_sync = support.reader_sync
reader_translate = support.reader_translate
reader_openai = support.reader_openai
import reader_usage


def response(**overrides):
    return {'id': 'resp-usage-test', 'model': 'gpt-6-luna', 'service_tier': 'default', 'status': 'completed',
            'usage': {'input_tokens': 1000, 'output_tokens': 500, 'total_tokens': 1500,
                      'input_tokens_details': {'cached_tokens': 200, 'cache_write_tokens': 100},
                      'output_tokens_details': {'reasoning_tokens': 100}}} | overrides


class UsageTest(unittest.TestCase):
    setUp = support.TranslationTest.setUp
    tearDown = support.TranslationTest.tearDown
    request = support.TranslationTest.request
    login = support.TranslationTest.login
    source = support.TranslationTest.source

    def snapshot(self):
        return self.request('/app/api/manage/resources')[1]['translation_usage']

    def successful(self, payload, on_response=None):
        if on_response:
            on_response(response())
        return support.TranslationTest.fake_response(payload)

    def start(self):
        with reader_sync.connect() as db:
            episode = dict(db.execute("SELECT * FROM reader_episodes WHERE work_id='69shuba-999' AND episode_id='1'").fetchone())
        episode['source_hash'] = reader_translate.episode_key(episode)[2]
        return reader_usage.begin(reader_sync.connect, episode, 'gpt-6-luna', False)

    def test_exact_prices_do_not_double_count_cache_or_reasoning(self):
        data = reader_usage.measurement(response())
        self.assertEqual(data['cost_pico_usd'], 334_500_000)
        self.assertEqual(data['total_tokens'], 1500)
        self.assertEqual(data['price_date'], '2026-10-25')
        self.assertEqual(reader_usage.measurement(response(service_tier='priority'))['cost_pico_usd'], 669_000_000)
        self.assertEqual(reader_usage.measurement(response(service_tier='flex'))['cost_pico_usd'], 167_250_000)
        large = response()
        large['usage'] = {'input_tokens': 300000, 'output_tokens': 200, 'total_tokens': 300200,
                          'input_tokens_details': {'cached_tokens': 30000, 'cache_write_tokens': 10000}}
        self.assertEqual(reader_usage.measurement(large)['cost_pico_usd'], 55_250_000_000)

    def test_missing_or_invalid_usage_never_becomes_a_zero_charge(self):
        for missing in ({}, response(usage=None), response(usage={'input_tokens': True, 'output_tokens': 1, 'total_tokens': 2})):
            data = reader_usage.measurement(missing)
            self.assertIsNone(data['total_tokens'])
            self.assertIsNone(data['cost_pico_usd'])
        incomplete = response()
        del incomplete['usage']['input_tokens_details']['cache_write_tokens']
        data = reader_usage.measurement(incomplete)
        self.assertEqual(data['total_tokens'], 1500)
        self.assertIsNone(data['cost_pico_usd'])
        for other in (response(model='unknown-model'), response(service_tier=None)):
            self.assertEqual(reader_usage.measurement(other)['total_tokens'], 1500)
            self.assertIsNone(reader_usage.measurement(other)['cost_pico_usd'])
        bad = response()
        bad['usage']['input_tokens_details']['cached_tokens'] = 1001
        self.assertIsNone(reader_usage.measurement(bad)['cost_pico_usd'])

    def test_stream_reports_only_whitelisted_metadata_and_keeps_text_contract(self):
        final = response() | {'output': 'private chapter text', 'instructions': 'private prompt'}
        events = [{'type': 'response.output_text.delta', 'delta': 'done'},
                  {'type': 'response.completed', 'response': final}]
        stream = ''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
        observed = []
        with patch.object(reader_openai, 'access_token', return_value='token'), patch.object(reader_openai, 'open_request', return_value=io.BytesIO(stream)):
            self.assertEqual(reader_openai.response_text({}, on_response=observed.append), 'done')
        self.assertEqual(len(observed), 1)
        self.assertEqual(set(observed[0]), {'id', 'model', 'service_tier', 'usage', 'status'})
        final['status'] = 'failed'
        stream = ('data: '+json.dumps({'type': 'response.failed', 'response': final})+'\n\n').encode()
        with patch.object(reader_openai, 'access_token', return_value='token'), patch.object(reader_openai, 'open_request', return_value=io.BytesIO(stream)), self.assertRaises(reader_openai.OpenAIError):
            reader_openai.response_text({}, on_response=observed.append)
        self.assertEqual(len(observed), 2, 'Failed responses may still report chargeable usage')

    def test_requests_are_persistent_and_cached_rereads_are_free(self):
        base = self.source(translation_prefetch=1)
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.successful) as call:
            reader_translate.run_one(reader_sync.connect, reader_app.queue_translation_prefetch)
            reader_translate.run_one(reader_sync.connect, reader_app.queue_translation_prefetch)
            self.request(base+'/translate', {'retry': True})
            self.assertFalse(reader_translate.run_one(reader_sync.connect, reader_app.queue_translation_prefetch))
            self.assertEqual(call.call_count, 2)
        stats = self.snapshot()
        self.assertEqual(stats['total']['requests'], 2)
        self.assertEqual(stats['total']['total_tokens'], 3000)
        self.assertEqual(stats['total']['input_tokens'], 2000)
        self.assertAlmostEqual(stats['total']['cost_usd'], .000669)
        self.assertEqual({item['prefetch'] for item in stats['recent']}, {0, 1})
        self.assertEqual(len(stats['works']), 1)
        reader_sync.init_db()
        self.assertEqual(self.snapshot()['total'], stats['total'])
        with reader_sync.connect() as db:
            request_id = db.execute('SELECT request_id FROM reader_translation_usage LIMIT 1').fetchone()[0]
        reader_usage.finish(reader_sync.connect, request_id, response(usage=None), 'failed')
        self.assertEqual(self.snapshot()['total'], stats['total'], 'Duplicate settlement must not charge twice')

    def test_partial_retry_counts_real_calls_but_not_reused_chunks(self):
        base = self.source(['中'*6100])
        self.request(base+'/translate', {})
        count = 0
        def fail_second(payload, on_response=None):
            nonlocal count
            count += 1
            if count == 2:
                raise reader_openai.OpenAIError('network', 'interrupted')
            return self.successful(payload, on_response)
        with patch.object(reader_openai, 'response_text', side_effect=fail_second):
            reader_translate.run_one(reader_sync.connect)
            self.request(base+'/translate', {'retry': True})
            reader_translate.run_one(reader_sync.connect)
        stats = self.snapshot()['total']
        batches = len(reader_translate.chunks('第1章 试验', ['中'*6100]))
        self.assertEqual((stats['requests'], stats['reported'], stats['failed'], stats['unreported']), (batches+1, batches, 1, 1))
        self.assertEqual(stats['total_tokens'], 1500*batches)
        self.assertEqual(self.request(base)[1]['translation']['state'], 'ready')

    def test_validation_failure_and_late_deletion_still_count_spent_tokens(self):
        base = self.source()
        self.request(base+'/translate', {})
        def invalid(payload, on_response=None):
            on_response(response())
            return '{"items":[]}'
        with patch.object(reader_openai, 'response_text', side_effect=invalid):
            reader_translate.run_one(reader_sync.connect)
        self.assertEqual(self.snapshot()['total']['failed'], 1)
        self.assertEqual(self.snapshot()['total']['total_tokens'], 1500)
        self.request(base+'/translate', {'retry': True})
        def deleting(payload, on_response=None):
            self.request('/app/api/manage/delete', {'work_id': '69shuba-999', 'confirm': True})
            return self.successful(payload, on_response)
        with patch.object(reader_openai, 'response_text', side_effect=deleting):
            reader_translate.run_one(reader_sync.connect)
        stats = self.snapshot()
        self.assertEqual(stats['total']['total_tokens'], 3000)
        self.assertEqual(stats['works'][0]['title'], '삭제된 작품')
        with reader_sync.connect() as db:
            self.assertTrue(all(tuple(row) == ('', '', '') for row in db.execute('SELECT work_id,episode_id,source_hash FROM reader_translation_usage')))
            self.assertEqual(db.execute('SELECT count(*) FROM reader_translations').fetchone()[0], 0)

    def test_restart_unknown_calls_and_old_cache_are_not_backfilled(self):
        base = self.source()
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=support.TranslationTest.fake_response):
            reader_translate.run_one(reader_sync.connect)
        self.assertIsNone(self.snapshot()['total']['cost_usd'])
        request_id = self.start()
        reader_usage.interrupt(reader_sync.connect)
        with reader_sync.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM reader_translation_usage WHERE request_id=?', (request_id,)).fetchone()[0], 'interrupted')
            before = [tuple(row) for row in db.execute('SELECT * FROM reader_translations')]
            db.execute('DROP TABLE reader_translation_usage')
        reader_sync.init_db()
        with reader_sync.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM reader_translations')], before)
        stats = self.snapshot()
        self.assertEqual(stats['total']['requests'], 0)
        self.assertEqual(stats['untracked_cached'], 1)

    def test_request_registration_after_work_deletion_remains_anonymous(self):
        self.source()
        with reader_sync.connect() as db:
            episode = dict(db.execute("SELECT * FROM reader_episodes WHERE work_id='69shuba-999' AND episode_id='1'").fetchone())
        episode['source_hash'] = reader_translate.episode_key(episode)[2]
        self.request('/app/api/manage/delete', {'work_id': '69shuba-999', 'confirm': True})
        request_id = reader_usage.begin(reader_sync.connect, episode, 'gpt-6-luna', False)
        reader_usage.finish(reader_sync.connect, request_id, response(), 'completed')
        stats = self.snapshot()
        self.assertEqual(stats['total']['total_tokens'], 1500)
        self.assertEqual(stats['recent'][0]['work_title'], '삭제된 작품')
        self.assertEqual(stats['recent'][0]['episode_title'], '')
        self.assertEqual(stats['works'][0]['work_id'], '')

    def test_cache_clear_backup_restore_and_privacy_do_not_reset_spending(self):
        base = self.source()
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.successful):
            reader_translate.run_one(reader_sync.connect)
        before = self.snapshot()['total']
        backup = self.request('/app/api/manage/backup?work_id=69shuba-999')[1]
        self.assertNotIn('usage', json.dumps(backup))
        self.request('/app/api/manage/cache-clear', {'work_id': '69shuba-999', 'confirm': True})
        self.request('/app/api/manage/restore', {'backup': backup, 'confirm': True})
        self.assertEqual(self.snapshot()['total'], before)
        self.assertEqual(self.request('/app/api/manage/resources', cookie=False, bearer=True)[0], 401)
        self.assertEqual(self.request('/app/api/work/999')[1]['progress']['position'], .6)

    def test_today_uses_korean_midnight_not_host_timezone(self):
        self.source()
        first, second = self.start(), self.start()
        midnight = int(datetime.now(timezone(timedelta(hours=9))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
        with reader_sync.connect() as db:
            db.execute('UPDATE reader_translation_usage SET started_at=? WHERE request_id=?', (midnight-1, first))
            db.execute('UPDATE reader_translation_usage SET started_at=? WHERE request_id=?', (midnight, second))
        for request_id in (first, second):
            reader_usage.finish(reader_sync.connect, request_id, response(), 'completed')
        stats = self.snapshot()
        self.assertEqual(stats['total']['requests'], 2)
        self.assertEqual(stats['today']['requests'], 1)
        self.assertAlmostEqual(stats['today']['cost_usd'], .0003345)

    def test_browser_shows_cost_and_unknowns_without_overflow_or_resetting_editor(self):
        from playwright.sync_api import expect, sync_playwright
        executable = os.environ.get('READER_TEST_BROWSER', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        if not Path(executable).exists():
            self.skipTest('Local Chromium is unavailable')
        base = self.source()
        self.request(base+'/translate', {})
        with patch.object(reader_openai, 'response_text', side_effect=self.successful):
            reader_translate.run_one(reader_sync.connect)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                for viewport in ({'width': 390, 'height': 844}, {'width': 1280, 'height': 900}):
                    context = browser.new_context(viewport=viewport)
                    context.add_cookies([{'name': self.cookie.split('=',1)[0], 'value': self.cookie.split('=',1)[1], 'url': self.base}])
                    page = context.new_page()
                    page.goto(self.base+'/app/manage')
                    expect(page.locator('#usage-total')).to_contain_text('1,500 토큰')
                    expect(page.locator('#usage-total')).to_contain_text('$0.000334')
                    expect(page.locator('#translation-usage')).to_contain_text('실제 청구액이 아닙니다')
                    editor = page.get_by_label('작품별 용어집')
                    editor.fill('顾安=고안')
                    page.locator('#resources-refresh').click()
                    expect(page.locator('#resources-refresh')).to_be_enabled()
                    expect(editor).to_have_value('顾安=고안')
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])
                    context.close()
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
