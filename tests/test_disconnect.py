"""Real loopback TCP disconnect through Uvicorn; upstream is entirely mocked."""
import asyncio
import json
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as r


async def main():
    with tempfile.TemporaryDirectory() as scratch:
        r.STATE_PATH = Path(scratch) / "state.json"
        r.AUDIT_PATH = Path(scratch) / "audit.jsonl"
        r.ACCOUNTS = {"a": {"token": "fake-upstream"}}
        r.DEVICE_BY_KEY = {"fake-key": "device"}
        r.STATE = {"active_account": "a", "accounts": {}, "events": []}
        r.HOLD_MAX_S = 3
        calls = []
        mode = "hold"

        def upstream(request):
            calls.append(time.monotonic())
            if mode == "pace":
                return httpx.Response(429, json={"error": "burst"}, headers={"retry-after": "3"})
            return httpx.Response(429, json={"error": "spent"}, headers={
                "anthropic-ratelimit-unified-status": "rejected",
                "anthropic-ratelimit-unified-5h-utilization": "1",
                "anthropic-ratelimit-unified-5h-reset": str(time.time() + 30),
            })

        await r.client.aclose()
        r.client = httpx.AsyncClient(base_url=r.UPSTREAM, transport=httpx.MockTransport(upstream))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(r.app, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(500):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            assert server.started
            for mode in ("hold", "pace"):
                for path in ("/v1/messages", "/v1/chat/completions"):
                    calls.clear()
                    r.STATE = {"active_account": "a", "accounts": {}, "events": []}
                    body = json.dumps({"model": "test", "messages": [{"role": "user", "content": "hi"}]}).encode()
                    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.write((f"POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer fake-key\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
                    await writer.drain()
                    for _ in range(100):
                        if calls:
                            break
                        await asyncio.sleep(0.01)
                    assert len(calls) == 1
                    writer.close()
                    await writer.wait_closed()
                    # Release well before the three-second wait, not just on retry.
                    for _ in range(150):
                        if not server.server_state.tasks:
                            break
                        await asyncio.sleep(0.01)
                    assert len(calls) == 1, "disconnected request retried upstream"
                    assert not server.server_state.tasks, "disconnected wait retained its task"
                    print(f"real TCP disconnected {mode} cancelled: {path}")
        finally:
            server.should_exit = True
            await task
            sock.close()
            await r.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
