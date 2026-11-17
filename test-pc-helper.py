"""Offline checks: no account login, credential upload or real SSH calls."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).parent / 'server'), str(Path(__file__).parent / 'pc_helper')]
import reader_connect
import reader_openai


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Path(self.temp.name) / 'test-key'
        self.key.write_text('fixture only, not a private key')
        self.previous = {'ext_agent_host_id': 'urn:uuid:server', 'client_id': 'client', 'subject': 'owner', 'id_token': 'old-secret'}
        self.record = {'access_token': 'secret-access', 'refresh_token': 'secret-refresh'}
        self.status = {'connected': True, 'model_available': True, 'model': 'gpt-6-luna', 'email': 'reader@example.invalid'}

    def tearDown(self):
        self.temp.cleanup()

    def remote(self, data, returncode=0, stderr=b''):
        return subprocess.CompletedProcess([], returncode, json.dumps(data).encode(), stderr)

    def test_connect_reuses_flow_and_keeps_secrets_off_arguments_and_output(self):
        progress, output = [], io.StringIO()
        with patch.object(reader_openai.subprocess, 'run', side_effect=[self.remote(self.previous), self.remote({}), self.remote(self.status)]) as ssh, \
                patch.object(reader_openai, 'authorize', return_value=self.record) as authorize, \
                patch('sys.stdout', output), patch('sys.stderr', output):
            self.assertEqual(reader_openai.connect_ssh('root@192.168.100.118', self.key, progress=progress.append), self.status)
        self.assertEqual(ssh.call_count, 3)
        self.assertEqual(authorize.call_args.args[0], self.previous)
        imported = ssh.call_args_list[1]
        self.assertEqual(json.loads(imported.kwargs['input']), self.record)
        self.assertNotIn('secret', repr(imported.args))
        for call in ssh.call_args_list:
            args = call.args[0]
            self.assertIn('StrictHostKeyChecking=yes', args)
            self.assertIn('BatchMode=yes', args)
            self.assertTrue(call.kwargs['capture_output'])
        self.assertEqual(output.getvalue(), '')
        self.assertIn('서버에 등록', progress[-1])

    def test_ssh_failure_stops_before_login_and_redacts_diagnostics(self):
        for failure in [self.remote({}, 255, b'private-token-must-not-display'), FileNotFoundError(), subprocess.TimeoutExpired('ssh', 1)]:
            with self.subTest(failure=type(failure).__name__), patch.object(reader_openai.subprocess, 'run', side_effect=[failure]), \
                    patch.object(reader_openai, 'authorize') as authorize, self.assertRaises(reader_openai.OpenAIError) as error:
                reader_openai.connect_ssh('root@192.168.100.118', self.key)
            authorize.assert_not_called()
            self.assertNotIn('private-token', str(error.exception))

    def test_bad_key_destination_and_server_shape(self):
        with patch.object(reader_openai.subprocess, 'run') as ssh:
            for destination, key in [('-oProxyCommand=bad', self.key), ('root@host', self.key.with_suffix('.pub'))]:
                with self.assertRaises(reader_openai.OpenAIError): reader_openai.connect_ssh(destination, key)
            ssh.assert_not_called()
        for data in [[], {}, {'ext_agent_host_id': 1}]:
            with patch.object(reader_openai.subprocess, 'run', return_value=self.remote(data)), self.assertRaises(reader_openai.OpenAIError):
                reader_openai.connect_ssh('root@host', self.key)

    def test_account_switch_preserves_server_host_and_final_status_is_checked(self):
        with patch.object(reader_openai.subprocess, 'run', side_effect=[self.remote(self.previous), self.remote({}), self.remote(self.status)]), \
                patch.object(reader_openai, 'authorize', return_value=self.record) as authorize:
            reader_openai.connect_ssh('root@host', self.key, new_account=True)
        self.assertEqual(authorize.call_args.args[0], {'ext_agent_host_id': 'urn:uuid:server'})
        with patch.object(reader_openai.subprocess, 'run', side_effect=[self.remote(self.previous), self.remote({}), self.remote(self.status | {'connected': False})]), \
                patch.object(reader_openai, 'authorize', return_value=self.record), self.assertRaises(reader_openai.OpenAIError):
            reader_openai.connect_ssh('root@host', self.key)

    def test_cancellation_cannot_upload_after_login_or_progress_callback(self):
        for stage in ('authorize', 'register'):
            cancel = threading.Event()
            def authorize(*_, **kwargs):
                if stage == 'authorize': cancel.set()
                return self.record
            def progress(text):
                if stage == 'register' and text.startswith('서버에 등록하고'): cancel.set()
            with patch.object(reader_openai.subprocess, 'run', return_value=self.remote(self.previous)) as ssh, \
                    patch.object(reader_openai, 'authorize', side_effect=authorize), self.assertRaises(reader_openai.OpenAIError) as error:
                reader_openai.connect_ssh('root@host', self.key, progress=progress, cancel=cancel)
            self.assertEqual(error.exception.code, 'cancelled')
            self.assertEqual(ssh.call_count, 1)

    def test_loopback_cancellation_never_exchanges_tokens(self):
        cancel = threading.Event()
        def browser(_):
            cancel.set()
            return True
        with patch.object(reader_openai.webbrowser, 'open', side_effect=browser), \
                patch.object(reader_openai, 'request_json') as exchange, self.assertRaises(reader_openai.OpenAIError) as error:
            reader_openai.authorize(local_directory=Path(self.temp.name), progress=lambda _: None, cancel=cancel)
        self.assertEqual(error.exception.code, 'cancelled')
        exchange.assert_not_called()


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = reader_connect.ConnectionWindow(self.root, automatic=False)
        self.temp = tempfile.TemporaryDirectory()
        self.key = Path(self.temp.name) / 'fixture-key'
        self.key.write_text('fixture only')
        self.window.key.set(str(self.key))

    def tearDown(self):
        self.window.destroy()
        self.temp.cleanup()

    def finish(self):
        deadline = time.monotonic() + 3
        while self.window.running and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.window.running)

    def test_success_and_failure_are_displayed_without_console(self):
        with patch.object(reader_openai, 'connect_ssh', return_value={'email': 'reader@example.invalid'}):
            self.window.start()
            self.finish()
        self.assertIn('연결 완료', self.window.status.get())
        self.assertIn('reader@example.invalid', self.window.status.get())
        with patch.object(reader_openai, 'connect_ssh', side_effect=RuntimeError('sensitive-token')):
            self.window.start()
            self.finish()
        self.assertIn('실패', self.window.status.get())
        self.assertNotIn('sensitive-token', self.window.status.get())

    def test_missing_key_has_actionable_message(self):
        self.window.key.set(str(self.key.with_suffix('.pub')))
        with patch.object(reader_openai, 'connect_ssh') as connect:
            self.window.start()
        connect.assert_not_called()
        self.assertIn('SSH 키 선택', self.window.status.get())

    def test_close_cannot_cancel_an_inflight_server_registration(self):
        started, done = threading.Event(), threading.Event()
        def connecting(*_, **kwargs):
            kwargs['progress']('서버에 등록하고 Luna 사용 권한을 확인하고 있습니다…')
            started.set()
            done.wait(2)
            return {'email': 'reader@example.invalid'}
        with patch.object(reader_openai, 'connect_ssh', side_effect=connecting):
            self.window.start()
            self.assertTrue(started.wait(2))
            self.window.close()
            self.assertFalse(self.window.cancel.is_set())
            done.set()
            self.finish()


if __name__ == '__main__':
    unittest.main()
