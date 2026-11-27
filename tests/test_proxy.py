"""End-to-end tests for the claude-rotate HTTP layer against a mocked
Anthropic upstream (httpx.MockTransport) via the real ASGI app:

auth, header rewriting, SSE relay, quota-429 rotate+retry, burst-429 pacing,
429 passthrough when hold is off, hold-until-reset, and the admin endpoints.
"""
import asyncio
import gzip
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace
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
    R.LAST_BLOCKED_ACCOUNT.clear()
    R.LATEST_RATELIMITS.clear()
    R.RATELIMIT_HEADERS_DIRTY = False
    R.AUDIT_PATH.write_text("")


def unified(u5="0.5", u7="0.2", status="allowed", oi=None, oi_status=None, oi_reset=None, **extra):
    now = time.time()
    h = {"anthropic-ratelimit-unified-5h-utilization": u5,
         "anthropic-ratelimit-unified-7d-utilization": u7,
         "anthropic-ratelimit-unified-5h-reset": str(now + 3600) if u5 is not None else None,
         "anthropic-ratelimit-unified-7d-reset": str(now + 86400),
         "anthropic-ratelimit-unified-status": status,
         "anthropic-ratelimit-unified-7d_oi-utilization": oi,
         "anthropic-ratelimit-unified-7d_oi-status": oi_status,
         "anthropic-ratelimit-unified-7d_oi-reset": oi_reset}
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
    records = [json.loads(l) for l in R.AUDIT_PATH.read_text().splitlines() if l.strip()]
    assert all(isinstance(rec.get("util"), dict) and "util_5h" not in rec for rec in records)
    return records


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


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
check("first set of claims records a snapshot and numeric utilization",
      recs[-1]["ratelimit"]["unified-5h-utilization"] == "0.5" and recs[-1]["util"] == {"5h": 0.5, "7d": 0.2})
call()
check("stable Opus claim set omits raw headers", "ratelimit" not in audit_recs()[-1])

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

fresh_state()
calls = []


def two_default_bursts(req):
    calls.append(req.headers["authorization"])
    if len(calls) <= 2:
        return httpx.Response(429, json={"error": burst_error}, headers=unified(
            u5=None, u7="0.2", **{"retry-after": "0.02"}))
    return httpx.Response(200, json={"usage": {}}, headers=unified(u5=None))


use_upstream(two_default_bursts)
started = time.monotonic()
r = call()
rec = audit_recs()[-1]
check("two ordinary Opus bursts stay on the warm account under bounded pacing",
      r.status_code == 200 and calls == ["Bearer tok-a"] * 3 and rec["retries"] == 2
      and time.monotonic() - started >= 0.04
      and [limit["verdict"] for limit in rec["rate_limits"]] == ["burst", "burst"]
      and not R.STATE["accounts"]["a"].get("blocks_by_family"))

# --- 6. every account spent, hold off → 429 passthrough ----------------------
now = time.time()
spent = {"a": {"util_5h": 0.1, "reset_5h": now + 100},
         "b": {"util_5h": 1.0, "reset_5h": now + 2000}}
fresh_state("a", dict(spent))
R.HOLD_MAX_S = 0
# The last candidate becomes hard-blocked on its returned 429.
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
call(body={"model": "Claude-FaBlE-5", "messages": []})
check("Fable captures only response rate-limit headers, truncated and capped at forty",
      r.status_code == 200 and len(snapshot) == 40 and snapshot["fable"] == "x" * 200
      and "ratelimit" in audit_recs()[-1]
      and all(k == "fable" or k.startswith(("unified-", "test-")) for k in snapshot)
      and not any(s in R.AUDIT_PATH.read_text() for s in (
          "response-secret-marker", "ignored-header-marker", "request-header-marker", "tok-a", "devkey-1")))

use_upstream(lambda req: httpx.Response(200, content=SSE.encode(), headers={
    **unified(u5=None), "content-type": "text/event-stream"}))
r = call()
rec = audit_recs()[-1]
check("the first weekly-only Opus stream records headers even after Fable was observed",
      r.text == SSE and rec["util"] == {"7d": 0.2} and rec["ratelimit"]["unified-7d-utilization"] == "0.2")
call()
check("stable weekly-only Opus answers omit the raw snapshot",
      "ratelimit" not in audit_recs()[-1] and audit_recs()[-1]["util"] == {"7d": 0.2})
extra = {f"anthropic-ratelimit-unified-added-{i:02}-utilization": "0.30" for i in range(30)}
extra["anthropic-ratelimit-unified-invalid-utilization"] = "NaN"
extra["anthropic-ratelimit-unified-negative-utilization"] = "-1"
use_upstream(lambda req: httpx.Response(200, json={}, headers=unified(u5=None, u7="0.0", **extra)))
call()
extra = dict(reversed(list(extra.items())))
call()
changed, stable = audit_recs()[-2:]
check("claim-set changes log once despite header reordering at the cap, and utilization is valid",
      "ratelimit" in changed and "ratelimit" not in stable and changed["util"] == stable["util"]
      and stable["util"]["7d"] == 0 and len(stable["util"]) == 1 + R.MAX_LEARNED_CLAIMS
      and not ({"invalid", "negative"} & stable["util"].keys()))

fresh_state()
prefix_claims = {f"anthropic-ratelimit-unified-{name}-utilization": "0.3"
                 for name in [*(f"a{i:02}" for i in range(28)), "x", "x-a"]}
use_upstream(lambda req: httpx.Response(200, json={}, headers=unified(u5=None, **prefix_claims)))
call()
call()
first, second = audit_recs()
check("the cap selects by ID even when one claim ID prefixes another",
      "x" in first["util"] and "x-a" not in first["util"] and first["util"] == second["util"]
      and "ratelimit" in first and "ratelimit" not in second)

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

frame = b"event: error\ndata: " + json.dumps({"type": "error", "error": returned_error}).encode() + b"\n\n"
fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
use_upstream(lambda req: httpx.Response(429, stream=Chunks(frame[:16], frame[16:33], frame[33:]), headers={
    **unified(u5="1.0", status="rejected", **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)}),
    "content-type": "text/event-stream"}))
r = call()
check("a split SSE 429 frame keeps its body and records the first data payload's error",
      r.status_code == 429 and r.content == frame and audit_recs()[-1]["rate_limits"][0]["error"] == returned_error)

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
      and rec["rate_limits"][0]["error"] == burst_error and rec["ratelimit"]["unified-status"] == "allowed"
      and rec["util"] == {"5h": 0.5, "7d": 0.2})

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
answer = call("/rotate/ratelimits", method="GET").json()["latest_ratelimits"]["a"]["claude-opus-5"]
check("the latest table records chat 429s at the actual upstream path",
      answer["status"] == 429 and answer["path"] == "/v1/messages" and answer["headers"] == rec["ratelimit"]
      and answer["time"] == rec["rate_limits"][0]["at"] <= time.time())

fresh_state()
seen_models = []


def numeric_model(req):
    seen_models.append(json.loads(req.content)["model"])
    return httpx.Response(400, json={"error": {"message": "invalid model"}})


use_upstream(numeric_model)
r = call(chat, body=chat_body | {"model": 123})
check("a numeric chat model retains upstream 400 and uses the missing-model diagnostic bucket",
      seen_models == [123] and r.status_code == 400 and r.json()["error"]["message"] == "upstream: invalid model"
      and R.LATEST_RATELIMITS["a"][""]["status"] == 400 and audit_recs()[-1]["util"] == {})
before_errors = R.STORAGE_ERRORS.get("ratelimit_diagnostics", {}).get("count", 0)
with patch.object(R, "remember_ratelimits", side_effect=RuntimeError("diagnostic fault")):
    r = call(chat, body=chat_body | {"model": 123})
check("diagnostic failures are reported and preserve the upstream response",
      r.status_code == 400 and R.STORAGE_ERRORS["ratelimit_diagnostics"]["count"] == before_errors + 1)

now = time.time()
fresh_state("a", {"a": {"claims": {"7d_oi": {"utilization": 0.9, "reset": now - 60}}}})
use_upstream(lambda req: httpx.Response(429, json={"error": returned_error},
                                        headers=unified(u5=None, oi_status="rejected")))
with patch.dict(R.CFG, {"account_pins": {"laptop": {"account": "a", "mode": "strict"}}}):
    r = call(body={"model": "claude-fable-5"})
status = call("/rotate/status", method="GET").json()["accounts"]["a"]
claim = status["claims"]["7d_oi"]
check("a Fable rejection after reset still reports its active recheck block",
      r.status_code == 429 and claim["freshness"] == "awaiting_reading" and claim["block_reason"] == "rejected"
      and now < claim["blocked_until"] <= time.time() + R.RECHECK_S and not status["eligible_by_family"]["fable"])

fresh_state()
use_upstream(lambda req: httpx.Response(429, json={"error": burst_error},
                                        headers=unified(**{"retry-after": "0"})))
r = call(chat, body=chat_body | {"stream": False})
rec = audit_recs()[-1]
check("repeated ambiguous 429s on both accounts exhaust the family in four attempts",
      r.status_code == 503 and r.json()["error"]["type"] == "overloaded_error"
      and rec["retries"] == 3 and len(rec["rate_limits"]) == 4
      and [e["verdict"] for e in rec["rate_limits"]] == ["burst", "switched", "burst", "exhausted"]
      and all(e["error"] == burst_error for e in rec["rate_limits"]))

for path in ("/v1/messages", chat):
    fresh_state()
    failed_retry = httpx.ConnectError("mock retry failed")
    calls = []

    def fail_retry(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": burst_error}, headers=unified(**{"retry-after": "0"}))
        raise failed_retry

    use_upstream(fail_retry)
    caught = None
    try:
        call(path, body=chat_body)
    except httpx.ConnectError as exc:
        caught = exc
    recs = audit_recs()
    check(f"{path} preserves a failed retry exception and writes the 429 audit once",
          caught is failed_retry and len(recs) == 1 and recs[0]["status"] == 502 and recs[0]["retries"] == 1
          and recs[0]["rate_limits"][0]["error"] == burst_error)

large_error_body = json.dumps({"error": burst_error, "padding": "x" * (64 * 1024)}).encode()
fresh_state()
use_upstream(lambda req: httpx.Response(429, content=large_error_body, headers=unified(**{"retry-after": "0"}))
             if not R.STATE["accounts"] else httpx.Response(200, json={"usage": {}}, headers=unified()))
with patch.object(R.json, "loads", wraps=json.loads) as loads:
    r = call()
check("audit skips JSON parsing of discarded 429 errors over 64 KiB",
      r.status_code == 200 and "error" not in audit_recs()[-1]["rate_limits"][0]
      and all(args.args[0] != large_error_body for args in loads.call_args_list))

fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
use_upstream(lambda req: httpx.Response(429, content=large_error_body, headers=unified(
    u5="1.0", status="rejected", **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)})))
with patch.object(R.json, "loads", wraps=json.loads) as loads:
    r = call(chat, body=chat_body)
check("audit skips JSON parsing of discarded OpenAI exhaustion errors over 64 KiB",
      r.status_code == 503 and "error" not in audit_recs()[-1]["rate_limits"][0]
      and all(args.args[0] != large_error_body for args in loads.call_args_list))

fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
headers = unified(u5="1.0", status="rejected", **{
    "anthropic-ratelimit-unified-5h-reset": str(now + 100),
    "anthropic-ratelimit-" + "n" * 120: "long-name",
    **{f"anthropic-ratelimit-test-{i}": "x" * 200 for i in range(40)}})
use_upstream(lambda req: httpx.Response(429, json={"error": returned_error}, headers=headers))
r = call(body=chat_body)
rec = audit_recs()[-1]
check("rate-limit header names are capped at 100 characters in both snapshots",
      r.status_code == 429 and rec["ratelimit"]["n" * 100] == "long-name"
      and rec["rate_limits"][0]["headers"]["n" * 100] == "long-name"
      and all(len(name) <= 100 for name in rec["ratelimit"]))
with patch.object(R, "AUDIT_MAX_BYTES", 4096):
    fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
    r = call(body=chat_body)
rec = audit_recs()[-1]
check("a 4 KiB audit file keeps the attempt and error after dropping both header snapshots",
      r.status_code == 429 and r.json() == {"error": returned_error}
      and R.AUDIT_PATH.stat().st_size <= 4096 and rec["ratelimit_truncated"] is True
      and "ratelimit" not in rec and "headers" not in rec["rate_limits"][0]
      and rec["rate_limits"][0]["error"] == returned_error)

# --- 7. blocked pools dispatch in family order and can recover ---------------
now = time.time()
spent = {"a": {"util_5h": 1.0, "reset_5h": now + 100},
         "b": {"util_5h": 1.0, "reset_5h": now + 2000}}
fresh_state("a", {name: reading.copy() for name, reading in spent.items()})
calls = []


def spent_answer():
    return httpx.Response(429, json={"error": returned_error}, headers=unified(
        u5="1.0", status="rejected",
        **{"retry-after": "19", "anthropic-ratelimit-unified-5h-reset": str(now + 100)}))


def stale_block(req):
    account = req.headers["authorization"][-1]
    calls.append((req.url.path, account))
    if req.url.path == "/v1/messages/count_tokens":
        return httpx.Response(200, json={"input_tokens": 8})
    if account == "a":
        return spent_answer()
    return httpx.Response(200, json={"content": []}, headers=unified(u5=None))


use_upstream(stale_block)
first = call(body={"model": "claude-opus-5"})
count = call("/v1/messages/count_tokens", body={"model": "claude-opus-5"})
fable = call(body={"model": "claude-fable-5"})
second = call(body={"model": "claude-opus-5"})
count_again = call("/v1/messages/count_tokens", body={"model": "claude-opus-5"})
third = call(body={"model": "claude-opus-5"})
check("alternating inference and count_tokens still reaches the stale-blocked account for inference",
      [r.status_code for r in (first, fable, second, third)] == [429, 429, 200, 200]
      and count.json() == count_again.json() == {"input_tokens": 8}
      and calls == [("/v1/messages", "a"), ("/v1/messages/count_tokens", "a"), ("/v1/messages", "a"),
                    ("/v1/messages", "b"), ("/v1/messages/count_tokens", "b"), ("/v1/messages", "b")]
      and "5h" not in R.STATE["accounts"]["b"]["claims"])

fresh_state("a", {"a": spent["a"].copy()})
calls = []


def still_spent(req):
    calls.append((req.url.path, req.headers["authorization"][-1]))
    return spent_answer()


use_upstream(still_spent)
with patch.dict(R.ACCOUNTS, {"a": R.ACCOUNTS["a"]}, clear=True):
    start = time.monotonic()
    with patch.object(R, "HOLD_MAX_S", 0.05):
        opaque = call("/v1/messages/count_tokens", body={"model": "claude-opus-5"})
    elapsed = time.monotonic() - start
    exhausted = call(chat, body=chat_body)
check("count_tokens holds once, then relays the lone spent account's upstream 429",
      opaque.status_code == 429 and opaque.json() == {"error": returned_error}
      and opaque.headers["retry-after"] == "19" and elapsed >= 0.05
      and calls == [("/v1/messages/count_tokens", "a"), ("/v1/messages", "a")]
      and [e["type"] for e in R.STATE["events"]] == ["hold"])
check("chat converts the lone blocked account's upstream 429 to 503 with the relief deadline",
      exhausted.status_code == 503 and int(exhausted.headers["retry-after"]) in range(95, 101)
      and [rec["rate_limits"][0]["error"] for rec in audit_recs()] == [returned_error, returned_error])

fresh_state("a", {name: reading.copy() for name, reading in spent.items()})
calls = []


def recovered_but_paced(req):
    calls.append(req.headers["authorization"])
    return httpx.Response(429, json={"error": burst_error}, headers=unified(u5="0.20", **{"retry-after": "30"}))


use_upstream(recovered_but_paced)
r = call(chat, body=chat_body)
rec = audit_recs()[-1]
check("a blocked-pool dispatch that returns a burst relays chat 429 and Retry-After without retrying",
      calls == ["Bearer tok-a"] and r.status_code == 429 and r.headers["retry-after"] == "30"
      and r.json()["error"]["message"] == "upstream: " + burst_error["message"]
      and rec["status"] == 429 and rec["rate_limits"][0]["verdict"] == "burst")


def hold_handler(req):
    calls.append(1)
    if len(calls) == 1:
        return spent_answer()
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5=None))


use_upstream(hold_handler)
# At the deadline a route can have recovered since the saved 429.
fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
calls = []
clock = [now]


async def recover_on_expiry(seconds, request=None):
    clock[0] += seconds
    if seconds:
        R.STATE["accounts"]["a"]["claims"].pop("5h", None)


with patch.object(R, "HOLD_MAX_S", 1), patch.object(R, "retry_wait", recover_on_expiry), patch.object(
        R, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0],
                                  strftime=time.strftime, localtime=time.localtime)):
    r = call()
check("at hold expiry reselecting uses a recovered account instead of returning the saved 429",
      r.status_code == 200 and len(calls) == 2 and clock[0] == now + 1)

# Fast-forward the hold waits so the real hold loop can make twelve retries.
fresh_state("a", {"b": {"util_5h": 1.0, "reset_5h": now + 2000}})
calls = []
clock = [now]


async def advance_hold(seconds, request=None):
    clock[0] += seconds


def repeated_hold(req):
    calls.append(req)
    if len(calls) <= 12:
        return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": str(len(calls))}},
                              headers=unified(u5="1.0", status="rejected",
                                              **{"anthropic-ratelimit-unified-5h-reset": str(clock[0] + 1)}))
    return httpx.Response(200, json={"usage": {}}, headers=unified())


use_upstream(repeated_hold)
with patch.object(R, "HOLD_MAX_S", 60), patch.object(R, "retry_wait", advance_hold), patch.object(
        R, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0],
                                  strftime=time.strftime, localtime=time.localtime)):
    r = call()
rec = audit_recs()[-1]
check("more than ten held 429s retain ten entries and count the omitted attempts",
      r.status_code == 200 and rec["retries"] == 12 and rec["rate_limits_omitted"] == 2
      and [e["error"]["message"] for e in rec["rate_limits"]] == [str(i) for i in range(1, 10)] + ["12"]
      and all(e["verdict"] == "exhausted" for e in rec["rate_limits"]))


async def family_replay(path, ambiguous):
    fresh_state()
    calls = []
    started = asyncio.Event()
    opus_done = asyncio.Event()
    reset = time.time() + 10000
    wait_for_retry = R.retry_wait

    async def retry_wait(seconds, request):
        if seconds:
            await asyncio.wait_for(opus_done.wait(), 2)
        await wait_for_retry(seconds, request)

    async def respond(req):
        model = json.loads(req.content)["model"]
        account = req.headers["authorization"][-1]
        calls.append((model, account, time.monotonic()))
        if model == "claude-fable-5-2" and account == "a":
            started.set()
            return httpx.Response(429, json={"error": returned_error}, headers=unified(
                u5=None, u7="0.69", oi="0.48" if ambiguous else "1",
                oi_status="allowed" if ambiguous else "rejected", oi_reset=str(reset),
                **{"retry-after": "0.05", "anthropic-ratelimit-unified-representative-claim": "seven_day"}))
        if model == "claude-fable-5-2":
            await asyncio.wait_for(opus_done.wait(), 2)
        return httpx.Response(200, json={"content": [], "usage": {}}, headers=unified(
            u5=None, u7="0.69", oi="0.12" if model == "claude-fable-5-2" else None, oi_reset=str(reset)))

    use_upstream(respond)
    with patch.object(R, "retry_wait", retry_wait):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app), base_url="http://proxy") as client:
            async def ask(model):
                return await client.post(path, json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                                         headers={"authorization": "Bearer devkey-1"})

            fable_task = asyncio.create_task(ask("claude-fable-5-2"))
            await asyncio.wait_for(started.wait(), 2)
            opus = await ask("claude-opus-5")
            opus_done.set()
            fable = await asyncio.wait_for(fable_task, 2)
            R.family_route("fable")["last_switch_ts"] = 0
            still_fable = await ask("claude-fable-5-2")

    fable_calls = [(account, seen) for model, account, seen in calls if "fable" in model]
    audit_fable = next(rec for rec in audit_recs() if rec.get("rate_limits"))
    return (all(r.status_code == 200 for r in (opus, fable, still_fable))
            and opus.headers.get("x-rotate-account") == "a"
            and fable.headers.get("x-rotate-account") == still_fable.headers.get("x-rotate-account") == "b"
            and [account for account, _ in fable_calls] == (["a", "a", "b", "b"] if ambiguous else ["a", "b", "b"])
            and (not ambiguous or fable_calls[1][1] - fable_calls[0][1] >= 0.04)
            and audit_fable["retries"] == (2 if ambiguous else 1)
            and [e["verdict"] for e in audit_fable["rate_limits"]] == (["burst", "switched"] if ambiguous else ["switched"])
            and "default" not in R.STATE["accounts"]["a"].get("pace_by_family", {})
            and R.STATE["active_account"] == "a")


for path in ("/v1/messages", "/v1/chat/completions"):
    for ambiguous in (False, True):
        check(f"{path} keeps Opus on a during a {'repeated ambiguous' if ambiguous else 'Fable-only'} 429",
              asyncio.run(family_replay(path, ambiguous)))


async def late_stream():
    fresh_state()
    reading, finish = asyncio.Event(), asyncio.Event()
    reset = time.time() + 10000
    calls = []

    class Paused(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await finish.wait()
            yield SSE.encode()

    def respond(req):
        model = json.loads(req.content)["model"]
        account = req.headers["authorization"][-1]
        calls.append((model, account))
        limits = unified(u5=None, u7="0.69", oi="0.48", oi_reset=str(reset))
        if len(calls) == 1:
            return httpx.Response(200, stream=Paused(), headers=limits | {"content-type": "text/event-stream"})
        if "fable" in model and account == "a":
            return httpx.Response(429, json={"error": returned_error},
                                  headers=unified(u5=None, u7="0.69", oi="1", oi_reset=str(reset)))
        return httpx.Response(200, json={"usage": {}}, headers=limits if "fable" in model else unified(u5=None))

    use_upstream(respond)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app), base_url="http://proxy") as client:
        async def ask(model):
            return await client.post("/v1/messages", json={"model": model, "messages": []},
                                     headers={"authorization": "Bearer devkey-1"})

        slow = asyncio.create_task(ask("claude-fable-5"))
        await asyncio.wait_for(reading.wait(), 2)
        await ask("claude-fable-5")
        await ask("claude-opus-5")
        finish.set()
        first = await asyncio.wait_for(slow, 2)
        await ask("claude-fable-5")
    return (first.text == SSE and R.claim_utilization("a", "7d_oi") == 1
            and [account for _, account in calls] == ["a", "a", "b", "a", "b"])


check("a late Fable stream close cannot restore old claim readings or reverse the switch", asyncio.run(late_stream()))

fresh_state()
pin = {"account": "a", "mode": "strict"}
use_upstream(lambda req: httpx.Response(429, content=b"not UTF-8 \xff", headers={"retry-after": "19"}))
with patch.dict(R.CFG, {"account_pins": {"laptop": pin}}):
    strict = call(chat, body=chat_body)
check("strict chat relays the first 429 and Retry-After for a non-UTF-8 upstream refusal",
      strict.status_code == 429 and strict.headers["retry-after"] == "19"
      and strict.json()["error"]["message"] == "upstream: not UTF-8 \ufffd"
      and len(audit_recs()) == 1 and len(audit_recs()[0]["rate_limits"]) == 1)

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
check("refresh records 5h when the plan reports it", R.STATE["accounts"]["b"]["util_5h"] == 0.14)
check("refresh never changes the active account", R.STATE["active_account"] == "a")
asyncio.run(R.refresh_idle_accounts())
check("a just-refreshed account is not probed again", len(probes) == 1)

expired = time.time() - 60
fresh_state(active="a", accounts={"a": {"last_seen": time.time()}, "b": {
    "util_5h": 0.14, "reset_5h": expired, "claims": {
        "5h": {"utilization": 0.14, "reset": expired}, "ordinary": {"utilization": 0.65},
        "7d_oi": {"utilization": 0.24}},
    "learned_claims": {"default": ["ordinary"]}, "last_success_claims": {"default": ["5h", "ordinary"]}}})
probes.clear()
R._REFRESH_TRIED.clear()


def weekly_probe(req):
    probes.append(json.loads(req.content)["model"])
    return httpx.Response(200, headers=unified(u5=None, u7="0.69"))


use_upstream(weekly_probe)
asyncio.run(R.refresh_idle_accounts())
info = R.STATE["accounts"]["b"]
check("a weekly-only Haiku idle probe retires 5h and keeps family-specific claims",
      probes == ["claude-haiku-4-5"] and info["util_7d"] == 0.69
      and "5h" not in info["claims"] and "util_5h" not in info and "reset_5h" not in info
      and info["claims"]["ordinary"] == {"utilization": 0.65}
      and info["claims"]["7d_oi"] == {"utilization": 0.24}
      and info["learned_claims"] == {"default": ["ordinary"]}
      and info["last_success_claims"] == {"default": ["5h", "ordinary"]})

reset = time.time() + 600
fresh_state("a", {"a": {"claims": {"5h": {"utilization": 1, "reset": reset}}}})
use_upstream(lambda req: httpx.Response(200, json={}, headers=unified(u5=None)))
with patch.dict(R.CFG, {"account_pins": {"laptop": {"account": "a", "mode": "strict"}}}):
    for path, method in (("/v1/messages/count_tokens", "POST"), ("/v1/models", "GET")):
        assert call(path, method=method).status_code == 200
        assert R.claim_reading("a", "5h") == {"utilization": 1, "reset": reset}
    assert call().status_code == 200
check("only inference can retire the 5h block after count_tokens and metadata report weekly usage",
      "5h" not in R.STATE["accounts"]["a"]["claims"])

fresh_state(active="a", accounts={"a": {"last_seen": time.time()}, "b": {}})
R._REFRESH_TRIED.clear()
probes.clear()
use_upstream(lambda req: probes.append(1) or httpx.Response(429, json={"type": "error"}))
asyncio.run(R.refresh_idle_accounts())
asyncio.run(R.refresh_idle_accounts())
check("headerless 429 (retired model) neither paces nor gets retried before the interval",
      "pace_until" not in R.STATE["accounts"].get("b", {}) and len(probes) == 1)
check("even a headerless idle probe replaces the model's latest answer",
      R.LATEST_RATELIMITS["b"][R.REFRESH_MODEL]["status"] == 429
      and R.LATEST_RATELIMITS["b"][R.REFRESH_MODEL]["headers"] == {})

# The table is bounded independently for each account and kept out of status.
fresh_state()
use_upstream(lambda req: httpx.Response(200, json={}, headers=unified(u5=None)))
for i in range(16):
    call(body={"model": f"claude-test-{i}"})
call(body={"model": "claude-test-0"})
long_model = "claude-test-" + "x" * 300
snapshot_path = tmp / "ratelimit_headers.json"
with patch.object(R, "STATE_PATH", tmp / "diagnostic-state.json"), patch.object(R, "RATELIMIT_HEADERS_PATH", snapshot_path), \
     patch.object(R, "save_state", lambda: R.write_state(R.STATE)):
    call("/v1/messages?ignore=query-marker", body={"model": long_model})
    saved = json.loads(R.STATE_PATH.read_text())
    check("answer writes do not save the latest table in either state or the snapshot",
          "latest_ratelimits" not in saved and not snapshot_path.exists() and R.RATELIMIT_HEADERS_DIRTY)
    R.save_ratelimit_headers()
    assert snapshot_path.stat().st_mode & 0o777 == 0o600 and not R.RATELIMIT_HEADERS_DIRTY
    with patch.object(Path, "replace", side_effect=AssertionError("clean snapshot was rewritten")):
        R.save_ratelimit_headers()
models = json.loads(snapshot_path.read_text())["a"]
check("latest answers persist, evict by last use, cap model IDs and omit the query",
      len(models) == 16 and "claude-test-1" not in models and "claude-test-0" in models
      and long_model[:256] in models and models[long_model[:256]]["path"] == "/v1/messages")
snapshot_path.write_text(json.dumps({"a": models, "removed": models}))
with patch.object(R, "RATELIMIT_HEADERS_PATH", snapshot_path), patch.object(R, "LATEST_RATELIMITS", {}), \
     patch.dict(R.CFG, {"configured_accounts": {"a", "b"}}):
    R.load_ratelimit_headers()
    assert R.LATEST_RATELIMITS == {"a": models} and R.RATELIMIT_HEADERS_DIRTY
    with patch.object(Path, "replace", side_effect=OSError("failed replace")):
        R.save_ratelimit_headers()
    assert R.RATELIMIT_HEADERS_DIRTY and "removed" in json.loads(snapshot_path.read_text())

    async def idle_task(*_):
        await asyncio.Event().wait()

    async def shutdown():
        async with R.lifespan(None):
            pass

    with patch.object(R, "save_quota_history"), patch.object(R, "refresh_loop", idle_task), \
         patch.object(R.providers, "maintain_loop", idle_task):
        asyncio.run(shutdown())
    check("snapshot load prunes removed accounts and shutdown retries a failed save",
          json.loads(snapshot_path.read_text()) == {"a": models} and not R.RATELIMIT_HEADERS_DIRTY)
    R.remember_ratelimits("a", "claude-tick", "/v1/messages", 200, {}, time.time())
    sleeps = []

    async def tick_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    with patch.multiple(R, REFRESH_IDLE_S=0, record_history=lambda now: None, save_rollup=lambda: None,
                        save_quotas=lambda: None, save_quota_history=lambda now: None), \
         patch.object(R.asyncio, "sleep", tick_sleep):
        try:
            asyncio.run(R.refresh_loop())
        except asyncio.CancelledError:
            pass
    check("minute maintenance flushes the dirty snapshot",
          sleeps == [60] and "claude-tick" in json.loads(snapshot_path.read_text())["a"])
panel_status = call("/rotate/status", method="GET").json()
check("status advertises the table without embedding its records",
      panel_status["ratelimits_available"] is True and "latest_ratelimits" not in json.dumps(panel_status))

fresh_state()
surrogate = chr(0xD800)
with patch.object(R, "RATELIMIT_HEADERS_PATH", snapshot_path), patch.dict(R.CFG, {"configured_accounts": {"a", "b"}}):
    call(body={"model": "claude-" + surrogate})
    live = call("/rotate/ratelimits", method="GET")
    assert live.status_code == 200 and "claude-?" in live.json()["latest_ratelimits"]["a"]
    assert call("/rotate/stats", method="GET").status_code == 200
    R.save_ratelimit_headers()
    records = json.loads(snapshot_path.read_text())
    records["a"][surrogate] = {"time": time.time(), "status": 200, "path": "/" + surrogate,
                               "headers": {"unified-" + surrogate: surrogate}}
    snapshot_path.write_text(json.dumps(records))
    R.LATEST_RATELIMITS.clear()
    R.load_ratelimit_headers()
    after_restart = call("/rotate/ratelimits", method="GET")
    models = after_restart.json()["latest_ratelimits"]["a"]
    check("snapshot strings encode immediately and after loading old escaped surrogates",
          after_restart.status_code == 200 and "claude-?" in models
          and models["?"]["path"] == "/?" and models["?"]["headers"] == {"unified-?": "?"})

    rejected_snapshot = tmp / "invalid-ratelimit_headers.json"
    R.LATEST_RATELIMITS.clear()
    before_errors = R.STORAGE_ERRORS.get("ratelimit_headers_load", {}).get("count", 0)
    with patch.object(R, "RATELIMIT_HEADERS_PATH", rejected_snapshot):
        with rejected_snapshot.open("wb") as f:
            f.write(b" " * R.RATELIMIT_HEADERS_MAX_BYTES)
            f.write(snapshot_path.read_bytes())
        R.load_ratelimit_headers()
        rejected_snapshot.unlink()
        os.mkfifo(rejected_snapshot)
        R.load_ratelimit_headers()
        rejected_snapshot.unlink()
        rejected_snapshot.symlink_to(snapshot_path)
        R.load_ratelimit_headers()
    check("an oversized, FIFO or symlink snapshot leaves the gateway table empty",
          not R.LATEST_RATELIMITS and R.STORAGE_ERRORS["ratelimit_headers_load"]["count"] == before_errors + 3)

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
