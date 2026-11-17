"""What the Claude passthrough sends upstream: path, query and headers, as Claude Code
and existing devices rely on them. A recording stand-in replaces api.anthropic.com.

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
    return httpx.Response(200, headers=headers, json={"ok": True})


R.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url=R.UPSTREAM,
                             cookies=getattr(R, "NO_COOKIES", None))


def call(path, key="devkey-1", method="POST", headers=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app), base_url="http://proxy") as c:
            hdrs = {"authorization": f"Bearer {key}", "content-type": "application/json"} | (headers or {})
            return await c.request(method, path, content=json.dumps({"model": "claude-x"}), headers=hdrs)
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

print(f"\nall {PASS} checks passed")
