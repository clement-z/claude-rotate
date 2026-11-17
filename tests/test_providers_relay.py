"""How provider_proxy relays to the other providers (providers.py): usage read from
split SSE streams and buffered answers, upstream errors, size limits, HEAD and OPTIONS,
client disconnects, request admission, audit totals per provider, and provider 429s
that must leave the Claude accounts alone. Every upstream is an httpx.MockTransport;
the config, tokens and credential are fake files in a temporary directory.

  python3 tests/test_providers_relay.py
"""
import asyncio
import copy
import json
import os
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import httpx

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


tmp = Path(tempfile.mkdtemp())
DESK, LEGACY, LANONLY = "dev-desk-fake-key", "dev-legacy-fake-key", "dev-lanonly-fake-key"
(tmp / "tokens").mkdir()
for _name in ("a", "lan"):
    (tmp / "tokens" / f"{_name}.token").write_text(f"fake-claude-{_name}\n")
(tmp / "lan.token").write_text("fake-lan-token\n")
(tmp / "credentials").mkdir(mode=0o700)
(tmp / "credentials" / "openai.json").write_text(json.dumps({
    "access_token": "fake-oa-access", "refresh_token": "fake-oa-refresh",
    "client_id": "fake-client-id", "expires_at": time.time() + 30 * 86400}))
(tmp / "config.json").write_text(json.dumps({
    "accounts": [{"name": "a", "token_file": "tokens/a.token"},
                 {"name": "lan", "token_file": "tokens/lan.token"}],
    "devices": {"desk": {"key": DESK, "providers": ["anthropic", "lan", "openai"]},
                "legacy": LEGACY,
                "lanonly": {"key": LANONLY, "providers": ["lan"]}},
    "providers": {"lan": {"kind": "static-bearer", "upstream": "http://lan.test/base",
                          "token_file": "lan.token"},
                  "openai": {"kind": "openai-siwc"}},
    "max_concurrent_requests": 2, "refresh_idle_s": 0, "hold_max_s": 0}))
for _var in list(os.environ):
    if _var.startswith("CLAUDE_ROTATE_") or _var.lower().endswith("_proxy"):
        del os.environ[_var]
os.environ.update(CLAUDE_ROTATE_CONFIG=str(tmp / "config.json"),
                  CLAUDE_ROTATE_STATE=str(tmp / "state.json"),
                  CLAUDE_ROTATE_AUDIT=str(tmp / "audit.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R  # noqa: E402


class Upstream:
    """Records each request and answers with self.answer(request), sync or async."""

    def __init__(self, answer):
        self.calls, self.answer = [], answer

    async def __call__(self, request):
        self.calls.append(request)
        response = self.answer(request)
        return await response if asyncio.iscoroutine(response) else response


class Body(httpx.AsyncByteStream):
    """An upstream body in these chunks; an exception among them is raised there and an
    asyncio.Event is waited on."""

    def __init__(self, *items):
        self.items, self.closed = items, False

    async def __aiter__(self):
        for item in self.items:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, asyncio.Event):
                await item.wait()
            else:
                yield item

    async def aclose(self):
        self.closed = True


CLAUDE_ANSWER = {"type": "message", "usage": {"input_tokens": 7, "output_tokens": 3,
                                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
claude = Upstream(lambda request: httpx.Response(200, json=CLAUDE_ANSWER))
lan = Upstream(lambda request: httpx.Response(200, json={"ok": True}))
openai = Upstream(lambda request: httpx.Response(200, json={"ok": True}))
auth = Upstream(lambda request: httpx.Response(500, json={"error": "unexpected refresh"}))
for _client, _upstream in ((R.client, claude), (R.PROVIDERS["lan"].client, lan),
                           (R.PROVIDERS["openai"].client, openai), (R.PROVIDERS["openai"].auth, auth)):
    _client._mounts = {}
    _client._transport = httpx.MockTransport(_upstream)

C = None  # client into the app, made in main()


def js(obj):
    return json.dumps(obj).encode()


def sse(stream, status=200):
    return httpx.Response(status, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=stream)


def split(data, cuts):
    return [data[i:j] for i, j in zip([0] + cuts, cuts + [len(data)])]


def rows():
    try:
        return [json.loads(line) for line in R.AUDIT_PATH.read_text().splitlines()]
    except FileNotFoundError:
        return []


def one(new):
    return new[0] if len(new) == 1 else {}


def gate():
    if R.app.middleware_stack is None:
        R.app.middleware_stack = R.app.build_middleware_stack()
    node = R.app.middleware_stack
    while not isinstance(node, R.RequestAdmission):
        node = node.app
    return node


async def call(method, path, key=DESK, content=None):
    """(response, audit records the request added)."""
    before = len(rows())
    r = await asyncio.wait_for(C.request(method, path, content=content,
                                         headers={"authorization": f"Bearer {key}"}), 10)
    return r, rows()[before:]


async def direct(method, path, body=b"", key=DESK, gone=None, cut_after_body=False):
    """Run the app with a hand-made receive and send, as Uvicorn would (ASGI spec 2.3).
    receive hands over the body, then blocks until `gone` is set and reports a
    disconnect; cut_after_body sets `gone` at the first nonempty response chunk.
    Returns (response start message, response body)."""
    gone = gone or asyncio.Event()
    sent, delivered = [], False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if cut_after_body and message["type"] == "http.response.body" and message.get("body"):
            gone.set()

    headers = [(b"host", b"proxy"), (b"authorization", f"Bearer {key}".encode())]
    if body:
        headers += [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 40000), "server": ("proxy", 80)}
    await asyncio.wait_for(R.app(scope, receive, send), 10)
    start = next((m for m in sent if m["type"] == "http.response.start"), {})
    return start, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


async def until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.005)
    return True


def counts(bucket):
    return bucket and (bucket["requests"], bucket["input_tokens"], bucket["output_tokens"],
                       bucket["cache_read"], bucket["cache_write"])


async def main():
    global C
    now = time.time()
    R.STATE = {"active_account": "a", "last_switch_ts": now - 60,
               "accounts": {"a": {"util_5h": 0.42, "util_7d": 0.3, "reset_5h": now + 7200, "status": "allowed",
                                  "last_seen": now - 5, "pace_until": now - 30},
                            "lan": {"util_5h": 0.1, "util_7d": 0.2, "last_seen": now - 5, "pace_until": now - 30}},
               "events": [{"ts": "2026-11-17T10:00:00+0100", "type": "switch", "from": "lan", "to": "a",
                           "reason": "manual", "util": 0.1}]}
    start_state = copy.deepcopy(R.STATE)
    check("the admission middleware runs with the configured limit of 2", gate().limit == 2 and gate().active == 0)
    C = httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app), base_url="http://proxy")
    usage = lambda i, o, cr, cc: {"input_tokens": i, "output_tokens": o,  # noqa: E731
                                  "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}

    # 1. Usage from split streams and buffered answers
    euro = "€".encode()
    created = b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
    delta = 'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"5 €"}\n\n'.encode()
    done = ('event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1",'
            '"output":[{"type":"message","content":[{"type":"output_text","text":"5 € café"}]}],'
            '"usage":{"input_tokens":1200,"input_tokens_details":{"cached_tokens":1000},'
            '"output_tokens":50,"output_tokens_details":{"reasoning_tokens":10},"total_tokens":1250}}}\n\n').encode()
    chunks = ([created] + split(delta, [delta.index(euro) + 2])
              + split(done, [done.index(euro) + 1, done.index(b'"usage"') + 4,
                             done.index(b"cached_tokens") + 6, len(done) - 1]))
    stream = Body(*chunks)
    openai.answer = lambda request: sse(stream)
    r, new = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex", "stream": True}))
    sent = openai.calls[-1]
    check("a Responses stream reaches the client byte for byte, marked with the provider",
          r.status_code == 200 and r.content == b"".join(chunks) and r.headers.get("x-rotate-provider") == "openai")
    check("the openai request goes to api.openai.com/v1/responses with the credential's bearer, not the device key",
          str(sent.url) == "https://api.openai.com/v1/responses"
          and sent.headers["authorization"] == "Bearer fake-oa-access"
          and not any(DESK.encode() in v for _, v in sent.headers.raw))
    check("a response.completed line split over chunks, with a split UTF-8 character, audits input minus cached, "
          "output and cached as cache read",
          one(new).get("usage") == usage(200, 50, 1000, 0) and one(new).get("provider") == "openai"
          and one(new).get("status") == 200 and one(new).get("model") == "gpt-5.1-codex"
          and "interrupted" not in one(new) and "account" not in one(new))
    check("the finished upstream stream is closed", stream.closed)
    check("admission slot released after a completed stream", gate().active == 0)

    start = ('event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message",'
             '"role":"assistant","model":"claude-x","content":[],"usage":{"input_tokens":30,'
             '"cache_creation_input_tokens":0,"cache_read_input_tokens":500,"output_tokens":1}}}\n\n').encode()
    text = ('event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"héllo"}}\n\n').encode()
    final = ('event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
             '"stop_sequence":null},"usage":{"output_tokens":75}}\n\n').encode()
    stop = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    k1, k2, k3 = start.index(b"cache_read") + 5, text.index("é".encode()) + 1, final.index(b"output_tokens")
    chunks = [start[:k1], start[k1:] + text[:k2], text[k2:] + final[:k3], final[k3:] + stop]
    stream = Body(*chunks)
    lan.answer = lambda request: sse(stream)
    r, new = await call("POST", "/lan/v1/messages", content=js({"model": "claude-x", "stream": True}))
    sent = lan.calls[-1]
    check("an Anthropic stream under the static prefix reaches the client byte for byte",
          r.status_code == 200 and r.content == b"".join(chunks) and r.headers.get("x-rotate-provider") == "lan")
    check("the static provider gets its own bearer under its base path, the device key dropped",
          str(sent.url) == "http://lan.test/base/v1/messages"
          and sent.headers["authorization"] == "Bearer fake-lan-token"
          and not any(DESK.encode() in v for _, v in sent.headers.raw))
    check("an Anthropic stream audits message_start's input and cache read merged with message_delta's output",
          one(new).get("usage") == usage(30, 75, 500, 0) and one(new).get("provider") == "lan")

    answer = js({"id": "chatcmpl-1", "object": "chat.completion",
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                           "prompt_tokens_details": {"cached_tokens": 40}}})
    openai.answer = lambda request: httpx.Response(200, headers={"content-type": "application/json"}, content=answer)
    r, new = await call("POST", "/openai/v1/chat/completions", content=js({"model": "gpt-5.1"}))
    check("a buffered JSON answer reaches the client unchanged and its usage is audited",
          r.status_code == 200 and r.content == answer and one(new).get("usage") == usage(60, 20, 40, 0)
          and one(new).get("model") == "gpt-5.1" and one(new).get("path") == "/openai/v1/chat/completions")
    check("admission slot released after a buffered success", gate().active == 0)

    # 2. Errors
    lan.answer = lambda request: httpx.Response(500, json={"error": {"message": "upstream exploded"}})
    r, new = await call("POST", "/lan/v1/messages", content=js({"model": "m"}))
    check("a buffered upstream 500 passes through with its status and body and is audited",
          r.status_code == 500 and r.json() == {"error": {"message": "upstream exploded"}}
          and one(new).get("status") == 500 and one(new).get("provider") == "lan")

    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)
    lan.answer = refused
    r, new = await call("POST", "/lan/v1/messages", content=js({"model": "m"}))
    check("a transport error before headers gives 502 naming the provider, and an audit record",
          r.status_code == 502 and "provider 'lan' is unreachable (ConnectError)" in r.text
          and one(new).get("status") == 502 and one(new).get("provider") == "lan")

    stream = Body(b'{"partial": ', httpx.ReadError("connection reset"))
    lan.answer = lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
    r, new = await call("POST", "/lan/v1/messages", content=js({"model": "m"}))
    check("a buffered body that breaks off gives 502 'broke off', audited with the upstream status",
          r.status_code == 502 and "provider 'lan' broke off its answer (ReadError)" in r.text
          and one(new).get("status") == 502 and one(new).get("upstream_status") == 200 and stream.closed)

    stream = Body(created, delta, httpx.ReadError("connection reset"))
    openai.answer = lambda request: sse(stream)
    r, new = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex", "stream": True}))
    check("a stream that breaks off keeps what was relayed and is audited as interrupted by the upstream error",
          r.content == created + delta and one(new).get("interrupted") == "upstream ReadError"
          and one(new).get("status") == 200 and stream.closed)

    oai = R.PROVIDERS["openai"]
    n = len(openai.calls)
    oai.down = ("the refresh was refused (invalid_grant): sign in again with siwc_login.py "
                "and install the new credential")
    try:
        r, new = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
    finally:
        oai.down = None
    check("a provider that is down answers 503 naming it, with no upstream call and no token in the body",
          r.status_code == 503 and "provider 'openai' is unavailable: the refresh was refused" in r.text
          and r.json()["error"]["type"] == "provider_unavailable" and len(openai.calls) == n
          and not any(t in r.text for t in ("fake-oa-access", "fake-oa-refresh", DESK)))
    check("the provider-down 503 is audited", one(new).get("status") == 503 and one(new).get("provider") == "openai")
    check("admission slot released after a 503", gate().active == 0)

    # 3. Sizes
    saved = R.MAX_REQUEST_BYTES, R.MAX_RESPONSE_BYTES
    try:
        R.MAX_REQUEST_BYTES = 100
        n = len(lan.calls)

        async def chunked():
            for _ in range(3):
                yield b"x" * 60
        r, new = await call("POST", "/lan/v1/messages", content=chunked())
        check("a request body over max_request_bytes, sent without a length, gets 413 and no upstream call",
              r.status_code == 413 and len(lan.calls) == n)
        check("admission slot released after a 413", gate().active == 0)
        R.MAX_REQUEST_BYTES = saved[0]
        R.MAX_RESPONSE_BYTES = 1024
        stream = Body(*[b"y" * 512] * 4)
        lan.answer = lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
        r, new = await call("POST", "/lan/v1/messages", content=js({"model": "m"}))
        check("a buffered answer over max_response_bytes gets 502, audited with the upstream status, upstream closed",
              r.status_code == 502 and "upstream response too large" in r.text and one(new).get("status") == 502
              and one(new).get("upstream_status") == 200 and stream.closed)
    finally:
        R.MAX_REQUEST_BYTES, R.MAX_RESPONSE_BYTES = saved

    # 4. HEAD and OPTIONS
    lan.answer = lambda request: httpx.Response(
        200, headers={"content-type": "application/json", "content-length": "1234", "etag": '"v1"'})
    head, body = await direct("HEAD", "/lan/v1/models")
    hdrs = {k.decode(): v.decode() for k, v in head.get("headers", [])}
    check("HEAD is forwarded as HEAD and keeps the upstream content-length with an empty body",
          lan.calls[-1].method == "HEAD" and head.get("status") == 200 and hdrs.get("content-length") == "1234"
          and hdrs.get("etag") == '"v1"' and body == b"")
    lan.answer = lambda request: httpx.Response(204, headers={"allow": "GET, POST, OPTIONS"})
    r, new = await call("OPTIONS", "/lan/v1/messages")
    check("OPTIONS is forwarded as OPTIONS and its answer returned",
          lan.calls[-1].method == "OPTIONS" and str(lan.calls[-1].url) == "http://lan.test/base/v1/messages"
          and r.status_code == 204 and r.headers.get("allow") == "GET, POST, OPTIONS"
          and one(new).get("method") == "OPTIONS" and one(new).get("status") == 204)

    n = len(lan.calls)
    r, new = await call("POST", "/lan/v1/messages", key=LEGACY, content=js({"model": "m"}))
    check("a string-key device is refused a provider with 403 and no upstream call",
          r.status_code == 403 and len(lan.calls) == n)
    check("admission slot released after a 403", gate().active == 0)

    # 5. Disconnects
    hold = asyncio.Event()
    stream = Body(created, delta, hold, done)
    openai.answer = lambda request: sse(stream)
    before = len(rows())
    head, body = await direct("POST", "/openai/v1/responses", js({"model": "gpt-5.1-codex", "stream": True}),
                              cut_after_body=True)
    new = rows()[before:]
    check("a client that leaves mid-stream is audited as interrupted by the client, the upstream stream closed",
          head.get("status") == 200 and body.startswith(created) and b"response.completed" not in body
          and one(new).get("interrupted") == "client" and one(new).get("provider") == "openai" and stream.closed)
    check("admission slot released after a stream cut by the client", gate().active == 0)

    entered, release, gone = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def token_endpoint(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"access_token": "fake-oa-access-2", "refresh_token": "fake-oa-refresh-2",
                                         "expires_in": 86400})
    auth.answer = token_endpoint
    oai.rec["expires_at"] = time.time() + 30
    n, before = len(openai.calls), len(rows())
    task = asyncio.create_task(direct("POST", "/openai/v1/responses", js({"model": "gpt-5.1-codex"}), gone=gone))
    await asyncio.wait_for(entered.wait(), 5)
    gone.set()
    release.set()
    head, _ = await task
    new = rows()[before:]
    check("a client that leaves during an inline token refresh gets no inference call and is audited 499",
          len(auth.calls) == 1 and len(openai.calls) == n and head.get("status") == 499
          and one(new).get("status") == 499 and one(new).get("provider") == "openai")
    check("the refresh finished for that client is kept for the next request",
          oai.rec.get("access_token") == "fake-oa-access-2" and oai.rec["expires_at"] > time.time() + 3600)
    check("admission slot released after a 499", gate().active == 0)

    # 6. Admission at capacity
    release, entered = asyncio.Event(), []

    async def slow(request):
        entered.append(request)
        await release.wait()
        return httpx.Response(200, json={"ok": True})
    lan.answer = slow
    held = [asyncio.create_task(call("POST", "/lan/v1/messages", content=js({"model": "m"}))) for _ in range(2)]
    filled = await until(lambda: len(entered) == 2)
    n = len(openai.calls)
    r, new = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
    check("with the limit reached, a provider request gets 503 at capacity and no upstream call",
          filled and gate().active == 2 and r.status_code == 503 and "at capacity" in r.text
          and r.headers.get("retry-after") == "5" and len(openai.calls) == n and new == [])
    release.set()
    finished = await asyncio.gather(*held)
    check("the held requests then complete and admission is back to 0",
          all(resp.status_code == 200 for resp, _ in finished) and gate().active == 0)

    check("no provider request touched the Claude state", R.STATE == start_state)
    check("no provider request called the Anthropic client", claude.calls == [])

    # 8. Provider 429s leave Claude alone
    snap = copy.deepcopy(R.STATE)
    limited = {"retry-after": "30", "anthropic-ratelimit-unified-status": "rejected",
               "anthropic-ratelimit-unified-5h-utilization": "1.0",
               "anthropic-ratelimit-unified-7d-utilization": "1.0",
               "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 3600)}
    for name, upstream, path in (("lan", lan, "/lan/v1/messages"), ("openai", openai, "/openai/v1/responses")):
        upstream.answer = lambda request: httpx.Response(429, headers=limited,
                                                         json={"error": {"type": "rate_limit_error"}})
        n = len(upstream.calls)
        r, new = await call("POST", path, content=js({"model": "m"}))
        check(f"a 429 from {name} reaches the client as 429 with its retry-after, sent upstream once, "
              "audited without an account",
              r.status_code == 429 and len(upstream.calls) == n + 1 and r.headers.get("retry-after") == "30"
              and one(new).get("status") == 429 and one(new).get("provider") == name and "account" not in one(new))
    check("provider 429s leave the active account, events, pacing and hold fields unchanged", R.STATE == snap)
    check("provider 429s record no switch or hold event", R.STATE["events"] == snap["events"])
    check("provider 429s never call the Anthropic client", claude.calls == [])

    r, new = await call("POST", "/v1/messages", key=LEGACY, content=js({"model": "claude-x"}))
    check("a string-key device's Claude request still goes through on the active account after them",
          r.status_code == 200 and len(claude.calls) == 1
          and claude.calls[0].headers["authorization"] == "Bearer fake-claude-a"
          and r.headers.get("x-rotate-account") == "a" and one(new).get("account") == "a"
          and "provider" not in one(new))

    # 7. Audit totals
    R.AUDIT_PATH = tmp / "audit-totals.jsonl"
    R._AUDIT_CACHE = {"offset": 0, "records": deque(), "sig": b""}
    lan.answer = lambda request: httpx.Response(200, json={"type": "message", "usage": usage(11, 5, 2, 1)})
    openai.answer = lambda request: httpx.Response(200, json={"object": "response", "usage": {
        "input_tokens": 300, "input_tokens_details": {"cached_tokens": 100}, "output_tokens": 40}})
    await call("POST", "/lan/v1/messages", content=js({"model": "lan-model"}))
    await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
    R.STATE["active_account"] = "lan"
    r, new = await call("POST", "/v1/messages", content=js({"model": "claude-x"}))
    check("the Claude request rides the account named like the provider",
          r.status_code == 200 and one(new).get("account") == "lan" and "provider" not in one(new))
    r = await C.get("/rotate/stats", headers={"authorization": f"Bearer {DESK}"})
    stats = r.json()
    check("/rotate/stats puts each provider's usage in by_provider",
          r.status_code == 200 and set(stats["by_provider"]) == {"lan", "openai"}
          and counts(stats["by_provider"]["lan"]) == (1, 11, 5, 2, 1)
          and counts(stats["by_provider"]["openai"]) == (1, 200, 40, 100, 0))
    check("by_account holds only the Claude request, though an account and a provider are both named lan",
          set(stats["by_account"]) == {"lan"} and counts(stats["by_account"]["lan"]) == (1, 7, 3, 0, 0))
    check("by_device counts the device's provider and Claude requests together",
          counts(stats["by_device"].get("desk")) == (3, 218, 48, 102, 1))
    agg = R.aggregate_audit()
    check("aggregate_audit() agrees with /rotate/stats",
          (agg["by_provider"], agg["by_account"], agg["by_device"])
          == (stats["by_provider"], stats["by_account"], stats["by_device"]))
    await C.aclose()


asyncio.run(main())
print(f"\nall {PASS} checks passed")
