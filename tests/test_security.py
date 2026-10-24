"""Security boundary regressions with dummy credentials and a mocked upstream."""
import asyncio
import json
import sys
import tempfile
from collections import deque
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as r


async def main():
    with tempfile.TemporaryDirectory() as scratch:
        r.AUDIT_PATH = Path(scratch) / "audit.jsonl"
        r.STATE_PATH = Path(scratch) / "state.json"
        r._AUDIT_CACHE = {"offset": 0, "records": deque(), "sig": b""}
        r.ACCOUNTS = {"a": {"token": "fake-upstream-a"}, "b": {"token": "fake-upstream-b"}}
        r.CFG["devices"] = {"device": "fake-device-key"}
        r.DEVICE_BY_KEY = {"fake-device-key": "device"}
        r.STATE = {"active_account": "a", "accounts": {}, "events": []}
        sent = []

        def upstream(request):
            sent.append(request)
            return httpx.Response(400, json={"error": {"message": "mock rejection"}})

        await r.client.aclose()
        r.client = httpx.AsyncClient(base_url=r.UPSTREAM, transport=httpx.MockTransport(upstream))
        transport = httpx.ASGITransport(app=r.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(base_url="http://proxy", transport=transport) as client:
            auth = {"Authorization": "Bearer fake-device-key"}
            assert (await client.post("/v1/messages", json={})).status_code == 401
            assert not sent

            response = await client.get("/v1/models?key=fake-device-key&x=a%2Bb&x=2&%6bey=second", headers=auth)
            assert response.status_code == 400
            assert str(sent[-1].url) == "https://api.anthropic.com/v1/models?x=a%2Bb&x=2"
            assert sent[-1].headers["authorization"] == "Bearer fake-upstream-a"
            assert (await client.get("/v1/models?key=fake-device-key")).status_code == 400
            assert str(sent[-1].url) == "https://api.anthropic.com/v1/models"
            private = {"Cookie": "session=private", "Referer": "http://proxy/?key=private",
                       "Tailscale-User-Login": "test@example.invalid", "X-Forwarded-For": "100.100.1.1",
                       "Forwarded": "for=private", "Origin": "http://proxy"}
            await client.get("/v1/models", headers=auth | private | {"anthropic-version": "2023-06-01"})
            assert all(name not in sent[-1].headers for name in private)
            assert sent[-1].headers["anthropic-version"] == "2023-06-01"

            for model in ({"invalid": 1}, ["invalid"], 123, True, None):
                assert (await client.post("/v1/messages", headers=auth, json={"model": model})).status_code == 400
                assert (await client.get("/rotate/stats", headers=auth)).status_code == 200
                r.audit({"device": "device", "account": "a", "model": model, "status": 400})
                r._AUDIT_CACHE = {"offset": 0, "records": deque(), "sig": b""}
                assert (await client.get("/rotate/stats", headers=auth)).status_code == 200

            for body in (b'[]', b'null', b'{"messages":[null]}', b'\xff',
                         b'[' * 2000 + b'0' + b']' * 2000):
                before = len(sent)
                response = await client.post("/v1/chat/completions", headers=auth, content=body)
                assert response.status_code == 400, (body, response.status_code)
                assert len(sent) == before

            r.DEVICE_BY_KEY[""] = "must-never-authenticate"
            before = len(sent)
            assert (await client.get("/v1/models")).status_code == 401
            assert len(sent) == before

            old_limit = r.MAX_REQUEST_BYTES
            r.MAX_REQUEST_BYTES = 8

            async def chunks():
                yield b"12345"
                yield b"67890"

            for path in ("/v1/messages", "/v1/chat/completions"):
                before = len(sent)
                assert (await client.post(path, headers=auth, content=chunks())).status_code == 413
                assert len(sent) == before
            r.MAX_REQUEST_BYTES = old_limit

            r.ACCOUNTS["a"]["disabled"] = True
            await client.post("/v1/messages", headers=auth, json={"model": "regular"})
            assert sent[-1].headers["authorization"] == "Bearer fake-upstream-b"
            r.ACCOUNTS["b"]["disabled"] = True
            before = len(sent)
            for path in ("/v1/messages", "/v1/chat/completions"):
                response = await client.post(path, headers=auth, json={"model": "regular", "messages": [{"role": "user", "content": "hi"}]})
                assert response.status_code == 503
                assert len(sent) == before
            assert r.pick_account() is None
            r.ACCOUNTS["b"]["disabled"] = False
            r.STATE["active_account"] = "removed-account"
            await client.get("/v1/models", headers=auth)
            assert sent[-1].headers["authorization"] == "Bearer fake-upstream-b"

            for path in ("/rotate/panel", "/rotate/status", "/rotate/stats"):
                response = await client.get(path, headers=auth)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["referrer-policy"] == "no-referrer"
            print("proxy security checks passed")
        await r.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
