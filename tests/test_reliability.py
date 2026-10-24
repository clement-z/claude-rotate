"""Bounded analytics/logging and whole-request admission, using dummy fixtures."""
import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from collections import deque
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R
import httpx


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = R.AUDIT_PATH
        self.old_cache = R._AUDIT_CACHE
        self.old_limits = {k: getattr(R, k, None) for k in (
            'AUDIT_CACHE_RECORDS', 'AUDIT_CACHE_BYTES', 'AUDIT_MAX_BYTES', 'AUDIT_BACKUPS')}
        R.AUDIT_PATH = Path(self.temp.name) / 'audit.jsonl'
        R._AUDIT_CACHE = {'offset': 0, 'records': deque(), 'sig': b''}
        R.AUDIT_CACHE_RECORDS = 3
        R.AUDIT_CACHE_BYTES = 4096
        R.AUDIT_MAX_BYTES = 1024
        R.AUDIT_BACKUPS = 2

    def tearDown(self):
        R.AUDIT_PATH = self.old_path
        R._AUDIT_CACHE = self.old_cache
        for k, v in self.old_limits.items():
            if v is not None:
                setattr(R, k, v)
        self.temp.cleanup()

    def row(self, n, model='test'):
        return {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'device': 'test', 'account': 'test', 'model': model, 'n': n}

    def write(self, rows):
        R.AUDIT_PATH.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_small_history_is_exact(self):
        self.write([self.row(i) for i in range(2)])
        result = R.aggregate_audit()
        self.assertEqual(result['by_device']['test']['requests'], 2)
        self.assertFalse(result.get('history_limited', False))
        self.assertEqual(R.aggregate_audit()['by_device']['test']['requests'], 2)

    def test_cache_caps_history_and_reports_partial_totals(self):
        self.write([self.row(i) for i in range(12)])
        result = R.aggregate_audit()
        self.assertLessEqual(len(R._AUDIT_CACHE['records']), 3)
        self.assertEqual(result['by_device']['test']['requests'], 3)
        self.assertTrue(result['history_limited'])
        self.assertTrue(any('partial' in a['text'].lower() for a in result['alerts']))

    def test_cache_read_is_bounded_even_for_large_existing_log(self):
        self.write([self.row(i, 'x' * 800) for i in range(100)])
        result = R.aggregate_audit()
        self.assertLessEqual(len(R._AUDIT_CACHE['records']), 3)
        self.assertLessEqual(R._AUDIT_CACHE['bytes'], R.AUDIT_CACHE_BYTES)
        self.assertTrue(result['history_limited'])
        self.assertEqual(R._AUDIT_CACHE['offset'], R.AUDIT_PATH.stat().st_size)

    def test_rotating_log_stays_bounded_and_keeps_newest(self):
        for i in range(40):
            R.audit(self.row(i, 'x' * 100))
        files = list(Path(self.temp.name).glob('audit.jsonl*'))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(p.stat().st_size <= 1024 for p in files))
        self.assertEqual(json.loads(R.AUDIT_PATH.read_text().splitlines()[-1])['n'], 39)

    def test_rotation_detected_even_with_identical_first_record(self):
        rows = [self.row(i) for i in range(2)]
        self.write(rows)
        R.aggregate_audit()
        R.AUDIT_PATH.rename(R.AUDIT_PATH.with_suffix('.old'))
        self.write([rows[0], self.row(9)])
        R.aggregate_audit()
        self.assertEqual([r['n'] for r in R._AUDIT_CACHE['records']], [0, 9])

    def test_storage_failure_does_not_break_proxy_accounting(self):
        import errno
        error = OSError(errno.ENOSPC, 'test disk full')
        with patch.object(Path, 'write_text', side_effect=error):
            R.save_state()
        with patch.object(Path, 'open', side_effect=error):
            R.audit(self.row(1))
        self.assertGreaterEqual(R.STORAGE_ERRORS['state_write']['count'], 1)
        self.assertGreaterEqual(R.STORAGE_ERRORS['audit_write']['count'], 1)

    def test_recent_rotation_explicitly_marks_partial_history(self):
        for i in range(40):
            R.audit(self.row(i, 'x' * 100))
        R._AUDIT_CACHE = {'offset': 0, 'records': deque(), 'sig': b''}
        self.assertTrue(R.aggregate_audit()['history_limited'])

    def test_too_small_configured_audit_limit_refused_at_startup(self):
        child = subprocess.run([sys.executable, '-c', 'import rotator'],
            cwd=Path(R.__file__).parent, text=True, capture_output=True,
            env={**os.environ, 'CLAUDE_ROTATE_AUDIT_MAX_BYTES': '1'})
        self.assertNotEqual(child.returncode, 0)
        self.assertIn('audit_max_bytes', child.stderr)

    def test_oversized_audit_record_never_breaks_request(self):
        # Even a valid limit can be exceeded by unusual metadata after truncation.
        R.AUDIT_MAX_BYTES = 4096
        before = R.STORAGE_ERRORS.get('audit_write', {}).get('count', 0)
        R.audit(dict(self.row(1), path='\U0001f600' * 20000))
        self.assertEqual(R.STORAGE_ERRORS['audit_write']['count'], before + 1)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_cap_closes_upstream_on_failure(self):
        class Stream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield b'x' * 8
                yield b'y' * 8
            async def aclose(self):
                self.closed = True
        stream = Stream()
        response = httpx.Response(200, stream=stream)
        with patch.object(R, 'MAX_RESPONSE_BYTES', 10):
            with self.assertRaises(R.HTTPException) as error:
                await R.bounded_response(response)
        self.assertEqual(error.exception.status_code, 502)
        self.assertTrue(stream.closed)
        response = httpx.Response(200, content=b'ok')
        self.assertEqual(await R.bounded_response(response), b'ok')

    async def test_non_message_response_is_logged_with_method(self):
        with tempfile.TemporaryDirectory() as scratch:
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={'data': []})),
                    base_url=R.UPSTREAM) as upstream:
                with patch.object(R, 'client', upstream), \
                     patch.object(R, 'AUDIT_PATH', Path(scratch) / 'audit.jsonl'), \
                     patch.object(R, 'STATE_PATH', Path(scratch) / 'state.json'), \
                     patch.object(R, 'STATE', {'active_account': 'a', 'accounts': {}, 'events': []}), \
                     patch.object(R, 'ACCOUNTS', {'a': {'name': 'a', 'token': 'dummy'}}), \
                     patch.object(R, 'DEVICE_BY_KEY', {'dummy-key': 'test'}):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app),
                                                 base_url='http://test') as client:
                        response = await client.get('/v1/models', headers={'Authorization': 'Bearer dummy-key'})
                    self.assertEqual(response.status_code, 200)
                    row = json.loads(R.AUDIT_PATH.read_text())
                    self.assertEqual((row['method'], row['path'], row['device']), ('GET', '/v1/models', 'test'))

    async def test_limit_covers_stream_and_releases_after_cancellation(self):
        entered = asyncio.Event()
        finish = asyncio.Event()
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope['path'])
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            entered.set()
            await finish.wait()
            await send({'type': 'http.response.body', 'body': b'ok'})

        gate = R.RequestAdmission(inner, limit=1)
        messages = []
        async def send(message):
            messages.append(message)
        async def receive():
            await asyncio.Future()
        scope = {'type': 'http', 'path': '/v1/messages', 'method': 'POST'}
        first = asyncio.create_task(gate(scope, receive, send))
        await entered.wait()
        await gate(scope, receive, send)
        self.assertEqual([m['status'] for m in messages if m['type'] == 'http.response.start'], [200, 503])
        self.assertEqual(len(calls), 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        finish.set()
        await gate(scope, receive, send)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
