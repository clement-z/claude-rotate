"""What the Claude passthrough sends upstream: path, query and headers, as Claude Code
and existing devices rely on them, and what it refuses or strips on the way. A
recording stand-in replaces api.anthropic.com on the production client.

  python3 tests/test_passthrough_paths.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


tmp = Path(tempfile.mkdtemp())
R.AUDIT_PATH = tmp / "audit.jsonl"
R.save_state = lambda: None
R.ACCOUNTS = {"a": {"name": "a", "token": "tok-a"}}
R.DEVICE_BY_KEY = {"devkey-1": "laptop", "devkey-2": "phone"}
R.CFG["devices"] = {"laptop": "devkey-1", "phone": "devkey-2"}
R.STATE = {"active_account": "a", "accounts": {}, "events": [], "last_switch_ts": 0}
seen = []


def upstream(req):
    seen.append(req)
    headers = {"content-type": "application/json"}
    if req.url.raw_path.startswith(b"/v1/cookie"):
        headers["set-cookie"] = "session=from-upstream; Path=/"
    if req.url.raw_path.startswith(b"/v1/conn"):
        headers |= {"connection": "X-Up, keep-alive", "x-up": "1", "x-other": "2"}
    return httpx.Response(200, headers=headers, json={"ok": True})


R.client._transport = httpx.MockTransport(upstream)
check("the production client routes to the stand-in",
      all(t is None for t in R.client._mounts.values()))


def call(path, key="devkey-1", method="POST", headers=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app), base_url="http://proxy") as c:
            hdrs = {"authorization": f"Bearer {key}", "content-type": "application/json"} | (headers or {})
            return await c.request(method, "http://proxy" + path, content=json.dumps({"model": "claude-x"}),
                                   headers=hdrs)
    return asyncio.run(go())


def last():
    return seen[-1]


r = call("/v1/messages?beta=true")
check("plain path and query go upstream unchanged",
      r.status_code == 200 and last().url.raw_path == b"/v1/messages?beta=true"
      and last().url.host == "api.anthropic.com")
call("/v1/messages?a=1&key=devkey-1&b=x%20y&key=other")
check("the key parameter is dropped, the rest keeps spelling and order",
      last().url.raw_path == b"/v1/messages?a=1&b=x%20y")
call("/v1/files/abc%20def")
check("a percent-encoded space stays encoded", last().url.raw_path == b"/v1/files/abc%20def")
call("/v1/messages", headers={"x-api-key": "devkey-1", "cookie": "c=1", "x-forwarded-for": "1.2.3.4",
                              "anthropic-beta": "oauth-2025-04-20", "tailscale-user-login": "a@b"})
h = last().headers
check("the account token replaces the device key; private headers are dropped",
      h["authorization"] == "Bearer tok-a" and "x-api-key" not in h and "cookie" not in h
      and "x-forwarded-for" not in h and "tailscale-user-login" not in h
      and h["anthropic-beta"] == "oauth-2025-04-20" and h["accept-encoding"] == "identity")
r = call("/v1/models", method="GET")
check("other methods and the response pass through with the account named",
      r.status_code == 200 and last().method == "GET" and r.headers["x-rotate-account"] == "a")

call("/v1/files/a%3Fb%23c?x=1")
check("encoded ? and # stay encoded", last().url.raw_path == b"/v1/files/a%3Fb%23c?x=1")
for path in ("/v1/%2e%2e/x", "/v1/%2E%2E", "/v1/%2E/x", "/v1/.%2e/x", "/v1/%2e./x", "/v1/a%2Fb", "/v1/a%5Cb"):
    n = len(seen)
    r = call(path)
    check(f"{path} gets 400 without an upstream call", r.status_code == 400 and len(seen) == n)
r = call("//evil.example/v1/x")
check("//evil.example/v1/x stays on api.anthropic.com",
      r.status_code == 200 and (last().url.scheme, last().url.host, last().url.port) == ("https", "api.anthropic.com", None)
      and last().url.raw_path == b"//evil.example/v1/x")
r = call("/v1/conn", headers={"connection": "X-Secret", "x-secret": "1", "x-keep": "1"})
check("a header the request's Connection names is not forwarded",
      "x-secret" not in last().headers and last().headers.get("x-keep") == "1")
check("a header the upstream's Connection names is not returned",
      "x-up" not in r.headers and r.headers.get("x-other") == "2")
call("/v1/cookie", key="devkey-1")
call("/v1/messages", key="devkey-2")
check("a cookie set on one device's call is not sent on another's",
      "cookie" not in last().headers and not list(R.client.cookies.jar))

print(f"\nall {PASS} checks passed")
