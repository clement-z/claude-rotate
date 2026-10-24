"""Real local sockets verify readiness, watchdog notifications and shutdown."""
import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import uvicorn

spec = importlib.util.spec_from_file_location('systemd_runner',
    Path(__file__).resolve().parent.parent / 'deploy/systemd_runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_watchdog_and_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            address = str(Path(tmp) / 'notify')
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(address)
                receiver.setblocking(False)
                with patch.dict(os.environ, {'NOTIFY_SOCKET': address, 'WATCHDOG_USEC': '300000',
                                             'WATCHDOG_PID': str(os.getpid())}):
                    notifier = runner.SystemdNotifier()
                async def app(scope, receive, send):
                    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                    await send({'type': 'http.response.body', 'body': b'ok'})
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
                server = runner.SystemdServer(uvicorn.Config(app, log_level='error', lifespan='off'), notifier)
                # The notifier reads watchdog environment at server startup.
                with patch.dict(os.environ, {'WATCHDOG_USEC': '300000', 'WATCHDOG_PID': str(os.getpid())}):
                    task = asyncio.create_task(server.serve(sockets=[sock]))
                    loop = asyncio.get_running_loop()
                    try:
                        ready = await asyncio.wait_for(loop.sock_recv(receiver, 1024), 3)
                        self.assertEqual(ready, b'READY=1')
                        self.assertTrue(server.started)
                        reader, writer = await asyncio.open_connection('127.0.0.1', port)
                        writer.write(b'GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n')
                        await writer.drain()
                        self.assertIn(b'200 OK', await reader.read())
                        writer.close()
                        await writer.wait_closed()
                        self.assertEqual(await asyncio.wait_for(loop.sock_recv(receiver, 1024), 2), b'WATCHDOG=1')
                    finally:
                        server.should_exit = True
                        await asyncio.wait_for(task, 3)
                    notifications = []
                    while True:
                        try:
                            notifications.append(receiver.recv(1024))
                        except BlockingIOError:
                            break
                    self.assertIn(b'STOPPING=1', notifications)

    async def test_abstract_socket_and_wrong_pid(self):
        address = '\0claude-rotate-test-' + str(os.getpid())
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(address)
            receiver.settimeout(1)
            with patch.dict(os.environ, {'NOTIFY_SOCKET': '@' + address[1:], 'WATCHDOG_USEC': '300000',
                                         'WATCHDOG_PID': str(os.getpid() + 1)}):
                notifier = runner.SystemdNotifier()
                self.assertIsNone(notifier.watchdog_interval())
                notifier.notify('READY=1')
            self.assertEqual(receiver.recv(1024), b'READY=1')


if __name__ == '__main__':
    unittest.main()
