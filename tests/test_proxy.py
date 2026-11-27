"""End-to-end tests for the claude-rotate HTTP layer against a mocked
Anthropic upstream (httpx.MockTransport) via the real ASGI app:

auth, header rewriting, SSE relay, quota-429 rotate+retry, burst-429 pacing,
429 passthrough when hold is off, hold-until-reset, and the admin endpoints.
"""
import asyncio
import gzip
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

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


# --- harness -----------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())
R.AUDIT_PATH = tmp / "audit.jsonl"
R.save_state = lambda: None
R.ACCOUNTS = {"a": {"name": "a", "token": "tok-a"}, "b": {"name": "b", "token": "tok-b"}}
R.DEVICE_BY_KEY = {"devkey-1": "laptop"}
R.CFG["devices"] = {"laptop": "devkey-1"}
R.THRESHOLD, R.THRESHOLD_7D = 0.8, 0.98
R.STRATEGY, R.COOLDOWN_S, R.SWITCH_MARGIN, R.CF_MARGIN_S = "consume-first", 300, 0.05, 3600
R.HOLD_MAX_S = 0


def fresh_state(active="a", accounts=None):
    R.STATE = {"active_account": active, "accounts": accounts or {}, "events": [],
               "last_switch_ts": 0}
    R.AUDIT_PATH.write_text("")


def unified(u5="0.5", u7="0.2", status="allowed", **extra):
    now = time.time()
    h = {"anthropic-ratelimit-unified-5h-utilization": u5,
         "anthropic-ratelimit-unified-7d-utilization": u7,
         "anthropic-ratelimit-unified-5h-reset": str(now + 3600),
         "anthropic-ratelimit-unified-7d-reset": str(now + 86400),
         "anthropic-ratelimit-unified-status": status}
    h.update(extra)
    return {k: v for k, v in h.items() if v is not None}


def use_upstream(handler):
    R.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url=R.UPSTREAM, timeout=30.0)


def call(path="/v1/messages", key="devkey-1", body=None, headers=None, method="POST"):
    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app),
                                     base_url="http://proxy", timeout=30.0) as ac:
            hdrs = {"authorization": f"Bearer {key}"} if key else {}
            hdrs.update(headers or {})
            content = json.dumps(body or {"model": "claude-opus-5", "messages": []})
            return await ac.request(method, path, content=content, headers=hdrs)
    return asyncio.run(_go())


def audit_recs():
    return [json.loads(l) for l in R.AUDIT_PATH.read_text().splitlines() if l.strip()]


# --- 1. auth -----------------------------------------------------------------
fresh_state()
use_upstream(lambda req: httpx.Response(200))
r = call(key="wrong-key")
check("unknown device key → 401", r.status_code == 401
      and r.json()["error"]["type"] == "authentication_error")
check("401 without any credentials", call(key=None).status_code == 401)

# --- 2. transparent forwarding + header rewriting ----------------------------
fresh_state()
seen = {}


def ok_handler(req):
    seen["auth"] = req.headers.get("authorization")
    seen["accept-encoding"] = req.headers.get("accept-encoding")
    seen["x-api-key"] = req.headers.get("x-api-key")
    seen["beta"] = req.headers.get("anthropic-beta")
    # Genuinely gzipped body so the proxy-side httpx auto-decompresses and the
    # stale content-encoding header must be dropped (load-bearing fact #2).
    body = gzip.compress(json.dumps({"usage": {"input_tokens": 7, "output_tokens": 3}}).encode())
    return httpx.Response(200, content=body,
                          headers={**unified(), "content-encoding": "gzip",
                                   "content-type": "application/json"})


use_upstream(ok_handler)
r = call(headers={"x-api-key": "should-be-dropped", "anthropic-beta": "oauth-2025-04-20"})
check("success passes through", r.status_code == 200)
check("device key swapped for account token", seen["auth"] == "Bearer tok-a")
check("accept-encoding forced to identity", seen["accept-encoding"] == "identity")
check("x-api-key stripped", seen["x-api-key"] is None)
check("client anthropic-beta preserved", seen["beta"] == "oauth-2025-04-20")
check("content-encoding dropped from response", "content-encoding" not in r.headers)
check("state updated from unified headers",
      R.STATE["accounts"]["a"]["util_5h"] == 0.5 and R.STATE["accounts"]["a"]["util_7d"] == 0.2)
recs = audit_recs()
check("audit written with usage + device + account",
      recs and recs[-1]["device"] == "laptop" and recs[-1]["account"] == "a"
      and recs[-1]["usage"]["input_tokens"] == 7 and recs[-1]["model"] == "claude-opus-5")
check("ordinary answer with 5h utilization has no rate-limit snapshot", "ratelimit" not in recs[-1])

# --- 3. SSE relay ------------------------------------------------------------
fresh_state()
SSE = ('event: message_start\n'
       'data: {"message": {"usage": {"input_tokens": 11, "output_tokens": 1}}}\n\n'
       'event: message_delta\n'
       'data: {"usage": {"output_tokens": 33}}\n\n')
use_upstream(lambda req: httpx.Response(
    200, content=SSE.encode(),
    headers={**unified(), "content-type": "text/event-stream"}))
r = call()
check("SSE body relayed untouched", r.text == SSE)
recs = audit_recs()
check("SSE usage captured to audit",
      recs and recs[-1]["usage"] == {"input_tokens": 11, "output_tokens": 33,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0})

# --- 4. quota 429 → rotate → transparent retry -------------------------------
fresh_state("a", {"b": {"util_5h": 0.1, "util_7d": 0.1}})
calls = []


def quota_handler(req):
    calls.append(req.headers["authorization"])
    if req.headers["authorization"] == "Bearer tok-a":
        return httpx.Response(429, json={"error": "limit"},
                              headers=unified(u5="1.0", status="rejected"))
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.1"))


use_upstream(quota_handler)
r = call()
check("quota 429 hidden from client, retry succeeds", r.status_code == 200)
check("retry rode the next account", calls == ["Bearer tok-a", "Bearer tok-b"])
check("active account rotated", R.STATE["active_account"] == "b")
recs = audit_recs()
check("audit records final account + retry count",
      recs[-1]["account"] == "b" and recs[-1]["retries"] == 1 and recs[-1]["status"] == 200)
check("audit records the switched 429 under the original account",
      len(recs[-1]["rate_limits"]) == 1 and recs[-1]["rate_limits"][0]["account"] == "a"
      and recs[-1]["rate_limits"][0]["verdict"] == "switched"
      and recs[-1]["rate_limits"][0]["headers"]["unified-status"] == "rejected")

# --- 5. burst 429 → pace same account, no rotation ---------------------------
fresh_state("a", {"a": {"util_5h": 0.3, "util_7d": 0.1}, "b": {"util_5h": 0.0}})
calls = []
burst_error = {"type": "rate_limit_error", "message": "burst on this account"}


def burst_handler(req):
    calls.append(req.headers["authorization"])
    if len(calls) == 1:
        return httpx.Response(429, json={"error": burst_error, "private": "discarded-body-marker"},
                              headers={**unified(u5="0.3"), "retry-after": "1"})
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.3"))


use_upstream(burst_handler)
t0 = time.time()
r = call()
elapsed = time.time() - t0
check("burst 429 hidden from client", r.status_code == 200)
check("burst retried on the SAME account", calls == ["Bearer tok-a", "Bearer tok-a"])
check("no rotation on burst", R.STATE["active_account"] == "a")
check("pace honored (~retry-after)", 0.9 <= elapsed < 5)
limits = audit_recs()[-1]["rate_limits"]
check("audit records a retried burst, its headers, Retry-After and structured error",
      len(limits) == 1 and limits[0]["verdict"] == "burst" and limits[0]["account"] == "a"
      and limits[0]["retry_after"] == "1" and limits[0]["headers"]["unified-5h-utilization"] == "0.3"
      and limits[0]["error"] == burst_error and "discarded-body-marker" not in R.AUDIT_PATH.read_text())

# --- 6. every account spent, hold off → 429 passthrough ----------------------
now = time.time()
spent = {"a": {"util_5h": 1.0, "reset_5h": now + 100},
         "b": {"util_5h": 1.0, "reset_5h": now + 2000}}
fresh_state("a", dict(spent))
R.HOLD_MAX_S = 0
# Pin the active account's reset as the soonest so pick_account's fallback
# stays on it → "exhausted" (not a bounce to the other spent account).
returned_error = {"type": "rate_limit_error", "message": "spent"}
use_upstream(lambda req: httpx.Response(429, json={"error": returned_error},
                                        headers=unified(
                                            u5="1.0", status="rejected",
                                            **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)})))
r = call()
check("all spent + hold off → 429 passthrough", r.status_code == 429 and r.json() == {"error": returned_error})
check("audit records the 429", audit_recs()[-1]["status"] == 429)
entry = audit_recs()[-1]["rate_limits"][0]
check("audit records the returned exhausted attempt and error",
      entry["account"] == "a" and entry["verdict"] == "exhausted" and entry["error"] == returned_error)

# Buffered and streaming audit records use the same narrow header snapshot.
fresh_state()
extra = {"Anthropic-RateLimit-Fable": "x" * 250,
         **{f"anthropic-ratelimit-test-{i}": str(i) for i in range(50)},
         "authorization": "response-secret-marker", "x-ignored": "ignored-header-marker"}
use_upstream(lambda req: httpx.Response(200, headers=unified(**extra), json={"usage": {}}))
r = call(body={"model": "Claude-FaBlE-5", "messages": []},
         headers={"anthropic-ratelimit-request-only": "request-header-marker"})
snapshot = audit_recs()[-1]["ratelimit"]
check("Fable captures only response rate-limit headers, truncated and capped at forty",
      r.status_code == 200 and len(snapshot) == 40 and snapshot["fable"] == "x" * 200
      and all(k == "fable" or k.startswith(("unified-", "test-")) for k in snapshot)
      and not any(s in R.AUDIT_PATH.read_text() for s in (
          "response-secret-marker", "ignored-header-marker", "request-header-marker", "tok-a", "devkey-1")))

use_upstream(lambda req: httpx.Response(200, content=SSE.encode(), headers={
    **unified(u5=None), "content-type": "text/event-stream"}))
r = call()
rec = audit_recs()[-1]
check("a non-Fable stream without 5h utilization records the other headers",
      r.text == SSE and rec["util_5h"] is None and rec["ratelimit"]["unified-7d-utilization"] == "0.2"
      and "unified-5h-utilization" not in rec["ratelimit"])

fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
long_error = {"type": "rate_limit_error", "message": "m" * 350, "extra": "ignored-error-marker"}
use_upstream(lambda req: httpx.Response(429, content=json.dumps({"error": long_error}).encode(), headers={
    **unified(u5="1.0", status="rejected", **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)}),
    "content-type": "text/event-stream", "retry-after": "1" * 250}))
r = call()
entry = audit_recs()[-1]["rate_limits"][0]
check("a streamed 429 is relayed and its audit bounds Retry-After and the error message",
      r.status_code == 429 and r.json() == {"error": long_error} and entry["retry_after"] == "1" * 200
      and entry["error"] == {"type": "rate_limit_error", "message": "m" * 300})

chat = "/v1/chat/completions"
chat_body = {"model": "claude-fable-5", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
fresh_state()
calls = []


def chat_upstream(req):
    calls.append(req)
    if len(calls) == 1:
        return httpx.Response(429, json={"error": burst_error}, headers=unified(**{"retry-after": "0"}))
    return httpx.Response(200, content=SSE.encode(), headers={**unified(), "content-type": "text/event-stream"})


use_upstream(chat_upstream)
r = call(chat, body=chat_body)
rec = audit_recs()[-1]
check("OpenAI chat stream audits a retried burst and the Fable answer",
      r.status_code == 200 and "[DONE]" in r.text and rec["retries"] == 1
      and rec["rate_limits"][0]["error"] == burst_error and rec["ratelimit"]["unified-status"] == "allowed")

fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
use_upstream(lambda req: httpx.Response(429, json={"error": returned_error},
                                        headers=unified(u5=None, status="rejected",
                                                        **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)})))
r = call(chat, body=chat_body | {"model": "claude-opus-5", "stream": False})
rec = audit_recs()[-1]
check("OpenAI chat still maps exhaustion to 503 and audits the upstream 429 and missing 5h",
      r.status_code == 503 and r.json()["error"]["type"] == "overloaded_error"
      and rec["status"] == 503 and rec["rate_limits"][0]["error"] == returned_error
      and rec["rate_limits"][0]["verdict"] == "exhausted" and rec["ratelimit"]["unified-status"] == "rejected"
      and "unified-5h-utilization" not in rec["ratelimit"])

fresh_state()
use_upstream(lambda req: httpx.Response(429, json={"error": burst_error},
                                        headers=unified(**{"retry-after": "0"})))
r = call(chat, body=chat_body | {"stream": False})
rec = audit_recs()[-1]
check("OpenAI chat still translates a returned burst 429 after retries and records all six attempts",
      r.status_code == 429 and r.json() == {"error": {"type": "api_error", "message": "upstream: burst on this account"}}
      and rec["retries"] == 5 and len(rec["rate_limits"]) == 6
      and all(e["verdict"] == "burst" and e["error"] == burst_error for e in rec["rate_limits"]))

# --- 7. every account spent, hold on → wait for reset, then succeed ----------
now = time.time()
fresh_state("a", {"a": {"util_5h": 1.0, "reset_5h": now + 100},
                  "b": {"util_5h": 1.0, "reset_5h": now + 2000}})
R.HOLD_MAX_S = 2  # deadline caps the wait so the test stays fast
calls = []


def hold_handler(req):
    calls.append(1)
    if len(calls) == 1:
        # Keep the active account's reset the soonest (see scenario 6) so the
        # verdict is "exhausted" and the hold path actually engages.
        return httpx.Response(429, json={"error": "limit"},
                              headers=unified(
                                  u5="1.0", status="rejected",
                                  **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)}))
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.0"))


use_upstream(hold_handler)
t0 = time.time()
r = call()
elapsed = time.time() - t0
check("held request eventually succeeds", r.status_code == 200 and len(calls) == 2)
check("hold waited for the deadline window", 1.5 <= elapsed < 10)
check("hold event recorded", any(e["type"] == "hold" for e in R.STATE["events"]))
R.HOLD_MAX_S = 0

# Fast-forward the hold waits so the real hold loop can make twelve retries.
fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
calls = []


def repeated_hold(req):
    calls.append(req)
    if len(calls) <= 12:
        return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": str(len(calls))}},
                              headers=unified(u5="1.0", status="rejected",
                                              **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)}))
    return httpx.Response(200, json={"usage": {}}, headers=unified())


use_upstream(repeated_hold)
with patch.object(R, "HOLD_MAX_S", 60), patch.object(R, "retry_wait"):
    r = call()
rec = audit_recs()[-1]
check("more than ten held 429s retain ten entries and count the omitted attempts",
      r.status_code == 200 and rec["retries"] == 12 and rec["rate_limits_omitted"] == 2
      and [e["error"]["message"] for e in rec["rate_limits"]] == [str(i) for i in range(1, 10)] + ["12"]
      and all(e["verdict"] == "exhausted" for e in rec["rate_limits"]))

# --- 8. admin endpoints ------------------------------------------------------
fresh_state()
check("status needs auth", call(path="/rotate/status", key="nope", method="GET").status_code == 401)
r = call(path="/rotate/status", method="GET")
check("status shape", r.status_code == 200 and r.json()["strategy"] == "consume-first"
      and "accounts" in r.json() and "series" in r.json()["weekly"])
check("status auth via ?key=", call(path="/rotate/status?key=devkey-1", key=None,
                                    method="GET").status_code == 200)
r = call(path="/rotate/stats", method="GET")
check("stats returns rollups", r.status_code == 200 and "by_device" in r.json()
      and set(r.json()["periods"]) == {"since", "7d", "30d", "all", "trend"})
r = call(path="/rotate/panel?key=devkey-1", key=None, method="GET")
check("panel serves html", r.status_code == 200 and "text/html" in r.headers["content-type"])
check("panel refuses to be framed", r.headers["content-security-policy"] == "frame-ancestors 'none'"
      and r.headers["x-frame-options"] == "DENY")

# --- 9. idle-account usage refresh -------------------------------------------
fresh_state(active="a", accounts={"a": {"last_seen": time.time()},
                                  "b": {"last_seen": time.time() - 7200, "util_7d": 0.01}})
R.REFRESH_IDLE_S = 1800
R._REFRESH_TRIED.clear()
probes = []


def probe_handler(req):
    probes.append((req.url.path, req.headers["authorization"], json.loads(req.content)["max_tokens"]))
    return httpx.Response(200, headers=unified(u5="0.14", u7="0.69"))


use_upstream(probe_handler)
asyncio.run(R.refresh_idle_accounts())
check("refresh sends one 1-token request, to the idle account only",
      probes == [("/v1/messages", "Bearer tok-b", 1)])
check("refresh records the idle account's real usage", R.STATE["accounts"]["b"]["util_7d"] == 0.69)
check("refresh never changes the active account", R.STATE["active_account"] == "a")
asyncio.run(R.refresh_idle_accounts())
check("a just-refreshed account is not probed again", len(probes) == 1)

fresh_state(active="a", accounts={"a": {"last_seen": time.time()}, "b": {}})
R._REFRESH_TRIED.clear()
probes.clear()
use_upstream(lambda req: probes.append(1) or httpx.Response(429, json={"type": "error"}))
asyncio.run(R.refresh_idle_accounts())
asyncio.run(R.refresh_idle_accounts())
check("headerless 429 (retired model) neither paces nor gets retried before the interval",
      "pace_until" not in R.STATE["accounts"].get("b", {}) and len(probes) == 1)

# --- 10. panel_tailnet: Tailscale Serve identity opens the panel, not the proxy
ts = {"tailscale-user-login": "alice@example.com"}
R.PANEL_TAILNET = False
check("Tailscale header alone is refused while panel_tailnet is off",
      call(path="/rotate/status", key=None, method="GET", headers=ts).status_code == 401)
R.PANEL_TAILNET = True
for p in ("/rotate/status", "/rotate/stats", "/rotate/panel"):
    check(f"panel_tailnet: keyless {p} with the Tailscale header → 200",
          call(path=p, key=None, method="GET", headers=ts).status_code == 200)
check("panel_tailnet: keyless without the header → 401",
      call(path="/rotate/stats", key=None, method="GET").status_code == 401)
use_upstream(lambda req: httpx.Response(200))
check("panel_tailnet: the header never opens the proxy itself",
      call(key=None, headers=ts).status_code == 401)
R.PANEL_TAILNET = False

# --- 11. panel switch: select an account and hold it, or hand back to the strategy
J = {"content-type": "application/json"}


def sw(body, key="devkey-1", headers=J):
    return call(path="/rotate/switch", key=key, body=body, headers=headers)


fresh_state(active="a", accounts={"a": {"util_5h": 0.3}, "b": {"util_5h": 0.9}})
check("switch needs auth", sw({"account": "b"}, key="nope").status_code == 401)
check("switch refuses an account at its threshold",
      sw({"account": "b"}).status_code == 409 and R.STATE["active_account"] == "a")
check("switch refuses an unknown account", sw({"account": "zz"}).status_code == 404)
R.STATE["accounts"]["b"]["util_5h"] = 0.1
check("switch refuses a form post",
      sw({"account": "b"}, headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 403)
check("switch refuses a cross-site request",
      sw({"account": "b"}, headers=J | {"sec-fetch-site": "cross-site"}).status_code == 403)
r = sw({"account": "b"})
check("switch selects and pins", r.status_code == 200 and R.STATE["active_account"] == "b"
      and R.STATE["pinned"] == "b" and R.STATE["events"][-1]["reason"] == "manual")
check("status reports the pin", call(path="/rotate/status", method="GET").json()["pinned"] == "b")
r = sw({"auto": True})
check("auto releases the pin", r.status_code == 200 and "pinned" not in R.STATE
      and R.STATE["active_account"] == "b")
R.PANEL_TAILNET = True
check("panel_tailnet: a tailnet viewer can switch with same-origin JSON",
      sw({"account": "a"}, key=None, headers=J | ts).status_code == 200 and R.STATE["active_account"] == "a")
check("panel_tailnet: a cross-site post through Serve is refused",
      sw({"account": "b"}, key=None, headers=ts | {"content-type": "text/plain",
                                                   "sec-fetch-site": "cross-site"}).status_code == 403)
R.PANEL_TAILNET = False

print(f"\nall {PASS} checks passed")
