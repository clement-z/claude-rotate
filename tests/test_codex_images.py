"""Codex's image requests under an openai-siwc provider with "images": true
(rotator.image_proxy, providers.CodexLogin): which requests go to the ChatGPT backend
and which stay on the Sign in with ChatGPT route, the backend's header allowlist and
the actor header dropped on every route, image answers relayed or replaced, the Codex
login's refresh, refusals and status, request sizes, the large-body budget and the
image line, and the quota headers in /rotate/status and /rotate/stats. Every upstream
is an httpx.MockTransport; the config, tokens and both logins are fake files in a
temporary directory, and the JWTs are made here.

  python3 tests/test_codex_images.py
"""
import asyncio
import base64
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

import httpx

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


async def no_network(self, request):
    raise AssertionError(f"real network request to {request.url.host}")


httpx.AsyncHTTPTransport.handle_async_request = no_network


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def jwt(**claims):
    """JWT-shaped, unsigned: the gateway reads only the exp claim, unverified."""
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(claims)}.fake-signature"


SECRETS = set()  # every fake secret handed out, for the leak checks
CODEX_SECRETS = set()  # the Codex login's tokens, which only the backend may see


def secret(value, codex=False):
    SECRETS.add(value)
    if codex:
        CODEX_SECRETS.add(value)
    return value


NOW = int(time.time())
DESK, LANONLY = secret("dev-desk-fake-key"), secret("dev-lanonly-fake-key")
SIWC = {name: (secret(f"fake-siwc-{name}-access"), secret(f"fake-siwc-{name}-refresh"))
        for name in ("openai", "plain", "draw")}
LAN_TOKEN, CLAUDE_TOKEN = secret("fake-lan-token"), secret("fake-claude-a")
CX_CLIENT, CX_ACCOUNT = "app_fake_codex_client", "fake-chatgpt-account-0001"
BACKEND = "https://chatgpt.com/backend-api/codex/images/"
TOKEN_URL = "https://auth.openai.com/oauth/token"


def codex_record(n, expires_in=3600):
    """The record install-credential --images writes."""
    exp = int(time.time()) + expires_in
    return {"access_token": secret(jwt(exp=exp, sub="fake-user", n=n), True),
            "refresh_token": secret(f"fake-cx-refresh-{n}", True), "client_id": CX_CLIENT,
            "account_id": CX_ACCOUNT, "expires_at": exp, "source": "codex login",
            "saved_at": "2026-11-25T10:00:00+00:00", "refresh_saved_at": int(time.time())}


tmp = Path(tempfile.mkdtemp())
(tmp / "tokens").mkdir()
(tmp / "tokens" / "a.token").write_text(CLAUDE_TOKEN + "\n")
(tmp / "lan.token").write_text(LAN_TOKEN + "\n")
CREDS = tmp / "credentials"
CREDS.mkdir(mode=0o700)
for _name, (_access, _refresh) in SIWC.items():
    (CREDS / f"{_name}.json").write_text(json.dumps({
        "access_token": _access, "refresh_token": _refresh, "client_id": "fake-siwc-client",
        "expires_at": NOW + 30 * 86400}))
CX_PATH = CREDS / "openai.codex.json"  # "draw" has images on and no login
CX_PATH.write_text(json.dumps(codex_record(1)))
(tmp / "config.json").write_text(json.dumps({
    "accounts": [{"name": "a", "token_file": "tokens/a.token"}],
    "devices": {"desk": {"key": DESK, "providers": ["anthropic", "lan", "openai", "plain", "draw"]},
                "lanonly": {"key": LANONLY, "providers": ["lan"]}},
    "providers": {"lan": {"kind": "static-bearer", "upstream": "http://lan.test/base", "token_file": "lan.token"},
                  "openai": {"kind": "openai-siwc", "images": True},
                  "plain": {"kind": "openai-siwc"},
                  "draw": {"kind": "openai-siwc", "images": True}},
    "max_concurrent_requests": 32, "refresh_idle_s": 0, "hold_max_s": 0}))
for _var in list(os.environ):
    if _var.startswith("CLAUDE_ROTATE_") or _var.lower().endswith("_proxy"):
        del os.environ[_var]
os.environ.update(CLAUDE_ROTATE_CONFIG=str(tmp / "config.json"),
                  CLAUDE_ROTATE_STATE=str(tmp / "state.json"),
                  CLAUDE_ROTATE_AUDIT=str(tmp / "audit.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import providers as P  # noqa: E402
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


def js(obj):
    return json.dumps(obj).encode()


IMAGE = js({"created": 1764000000, "data": [{"b64_json": "aGVsbG8gZm94"}], "usage": {"total_tokens": 0}})
CLAUDE_ANSWER = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
                 "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                 "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0}}


def image_ok(request):
    return httpx.Response(200, headers={"content-type": "application/json"}, content=IMAGE)


def ok(request):
    return httpx.Response(200, json={"ok": True})


def unexpected_refresh(request):
    return httpx.Response(500, json={"error": "unexpected refresh"})


def mock(client, upstream):
    client._mounts = {}
    client._transport = httpx.MockTransport(upstream)


claude = Upstream(lambda request: httpx.Response(200, json=CLAUDE_ANSWER))
lan = Upstream(ok)
siwc = {name: Upstream(ok) for name in SIWC}  # api.openai.com, one stand-in per provider
siwc_auth = Upstream(unexpected_refresh)  # auth.openai.com for the providers' own credentials
backend = Upstream(image_ok)  # chatgpt.com/backend-api/codex
cx_auth = Upstream(unexpected_refresh)  # auth.openai.com for the Codex logins
OAI, PLAIN, DRAW = R.PROVIDERS["openai"], R.PROVIDERS["plain"], R.PROVIDERS["draw"]
mock(R.client, claude)
mock(R.PROVIDERS["lan"].client, lan)
for _name in SIWC:
    mock(R.PROVIDERS[_name].client, siwc[_name])
    mock(R.PROVIDERS[_name].auth, siwc_auth)
for _prov in (OAI, DRAW):
    mock(_prov.images.client, backend)
    mock(_prov.images.auth, cx_auth)


def new_login(rec):
    """A restart of OAI's images login on this record file."""
    CX_PATH.write_text(json.dumps(rec))
    login = P.CodexLogin("openai", tmp)
    mock(login.client, backend)
    mock(login.auth, cx_auth)
    OAI.images = login
    return login


C = None  # client into the app, made in main()


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


def idle():
    """Image slots, waiters, large-body bytes and admission slots all back to 0."""
    return (R.IMAGE_LINE.active, R.IMAGE_LINE.waiting, R.LARGE_BODIES.held, gate().active) == (0, 0, 0, 0)


async def call(method, path, content=None, headers=None, key=DESK):
    """(response, audit records the request added)."""
    before = len(rows())
    r = await asyncio.wait_for(C.request(method, path, content=content,
                                         headers={"authorization": f"Bearer {key}"} | (headers or {})), 10)
    return r, rows()[before:]


async def direct(method, path, body=b"", gone=None, cut_after_body=False, sent=None):
    """Run the app with a hand-made receive and send, as Uvicorn would (ASGI spec 2.3).
    receive hands over the body, then blocks until `gone` is set and reports a
    disconnect; cut_after_body sets `gone` at the first nonempty response chunk.
    Returns (response start message, response body); `sent` collects the messages."""
    gone = gone or asyncio.Event()
    sent, delivered = [] if sent is None else sent, False

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

    headers = [(b"host", b"proxy"), (b"authorization", f"Bearer {DESK}".encode())]
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


def leaks(request, values):
    """Whether any of these strings is in the request's headers or body."""
    blob = b"\n".join(v for _, v in request.headers.raw) + b"\n" + request.content
    return any(v.encode() in blob for v in values)


# What Codex sends with an image request, plus what must not pass.
HDRS = {"content-type": "application/json", "originator": "codex_cli_rs",
        "user-agent": "codex_cli_rs/0.172.0 (fake)", "x-codex-image-turn-id": "fake-turn-1",
        "x-openai-actor-authorization": "claude-rotate",
        "x-codex-turn-metadata": '{"repository":"fake-secret-repo"}', "cookie": "session=fake-cookie",
        "x-unlisted": "fake-unlisted", "accept": "application/x-from-client",
        "chatgpt-account-id": "fake-client-account"}
FROM_CLIENT = ("content-type", "originator", "user-agent", "x-codex-image-turn-id")
MAY_REACH = set(FROM_CLIENT) | {"authorization", "chatgpt-account-id", "accept-encoding",  # the gateway's
                                "host", "accept", "connection", "content-length"}  # httpx's own
NEVER = ("x-openai-actor-authorization", "x-codex-turn-metadata", "cookie", "x-unlisted")
# Non-ASCII and odd spacing, so a re-encoded body would show.
PROMPT = '{"model":"gpt-image-2",  "prompt":"a red fox, «renard» — \U0001f98a","size":"1024x1024"}'.encode()


async def main():
    global C
    now = time.time()
    R.STATE = {"active_account": "a", "last_switch_ts": now - 60,
               "accounts": {"a": {"util_5h": 0.1, "util_7d": 0.1, "last_seen": now - 5, "pace_until": now - 30}},
               "events": []}
    C = httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app), base_url="http://proxy")

    check("the images provider holds a Codex login, the other openai-siwc provider none",
          isinstance(OAI.images, P.CodexLogin) and OAI.images.down is None and PLAIN.images is None
          and OAI.images.path == CX_PATH)
    check("default limits: 32 MiB image bodies, 256 MiB in flight, 2 image requests at once, 8 waiting",
          (R.MAX_IMAGE_REQUEST_BYTES, R.MAX_IMAGE_BYTES_IN_FLIGHT, R.MAX_CONCURRENT_IMAGES, R.IMAGE_LINE_MAX)
          == (32 << 20, 256 << 20, 2, 8))

    # 1. Routing
    for endpoint in ("generations", "edits"):
        n = len(backend.calls)
        r, new = await call("POST", f"/openai/v1/images/{endpoint}", content=PROMPT, headers=HDRS)
        sent = backend.calls[-1] if len(backend.calls) == n + 1 else None
        check(f"POST /openai/v1/images/{endpoint} reaches {BACKEND}{endpoint} once, exactly, no query",
              r.status_code == 200 and sent is not None and str(sent.url) == BACKEND + endpoint
              and sent.url.query == b"" and sent.method == "POST")
        check(f"{endpoint}: the Codex login's bearer and account id, not the device key or the client's account id",
              sent.headers["authorization"] == f"Bearer {OAI.images.rec['access_token']}"
              and sent.headers.get_list("chatgpt-account-id") == [CX_ACCOUNT] and not leaks(sent, [DESK]))
        check(f"{endpoint}: the body byte for byte", sent.content == PROMPT)
        check(f"{endpoint}: from the client only content-type, originator, user-agent and x-codex-image-turn-id",
              set(sent.headers.keys()) <= MAY_REACH and all(sent.headers.get(k) == HDRS[k] for k in FROM_CLIENT)
              and sent.headers.get("accept") != HDRS["accept"])
        check(f"{endpoint}: the actor header, turn metadata, cookie and an unlisted header do not reach the backend",
              not set(NEVER) & set(sent.headers.keys())
              and not leaks(sent, ["fake-secret-repo", "fake-cookie", "fake-unlisted", "claude-rotate"]))
        check(f"{endpoint}: the answer reaches the client unchanged, marked with the provider",
              r.content == IMAGE and r.headers.get("x-rotate-provider") == "openai")
        rec = one(new)
        check(f"{endpoint}: audited with route codex-images, provider, status, duration_s and bytes",
              {k: rec.get(k) for k in ("device", "provider", "path", "method", "route", "model", "status", "bytes")}
              == {"device": "desk", "provider": "openai", "path": f"/openai/v1/images/{endpoint}", "method": "POST",
                  "route": "codex-images", "model": "gpt-image-2", "status": 200, "bytes": len(IMAGE)}
              and type(rec.get("duration_s")) in (int, float) and "usage" not in rec)

    n = len(backend.calls)
    r, new = await call("POST", "/openai/v1/images/generations?api-version=fake&x=1", content=PROMPT, headers=HDRS)
    check("a query on an image path never reaches the backend: the URL is the constant one",
          r.status_code == 200 and len(backend.calls) == n + 1 and str(backend.calls[-1].url) == BACKEND + "generations"
          and backend.calls[-1].url.query == b"")

    spellings = [("POST", "/openai/v1/images/generations/", "/v1/images/generations/"),
                 ("POST", "/openai/v1/images//generations", "/v1/images//generations"),
                 ("POST", "/openai/v1/Images/generations", "/v1/Images/generations"),
                 ("POST", "/openai/v1/images/generation%73", "/v1/images/generation%73"),
                 ("POST", "/openai/images/generations", "/images/generations"),
                 ("POST", "/openai/v1/images/variations", "/v1/images/variations"),
                 ("GET", "/openai/v1/images/generations", "/v1/images/generations"),
                 ("PUT", "/openai/v1/images/edits", "/v1/images/edits")]
    for method, path, rest in spellings:
        nb, ns = len(backend.calls), len(siwc["openai"].calls)
        r, new = await call(method, path, content=PROMPT if method != "GET" else None, headers=HDRS)
        sent = siwc["openai"].calls[-1] if len(siwc["openai"].calls) == ns + 1 else None
        check(f"{method} {path} stays on the Sign in with ChatGPT route with that route's token",
              r.status_code == 200 and len(backend.calls) == nb and sent is not None
              and str(sent.url) == "https://api.openai.com" + rest and sent.method == method
              and sent.headers["authorization"] == f"Bearer {SIWC['openai'][0]}"
              and "route" not in one(new) and one(new).get("provider") == "openai")

    for endpoint in ("generations", "edits"):
        nb, ns = len(backend.calls), len(siwc["plain"].calls)
        r, new = await call("POST", f"/plain/v1/images/{endpoint}", content=PROMPT, headers=HDRS)
        sent = siwc["plain"].calls[-1] if len(siwc["plain"].calls) == ns + 1 else None
        check(f"without \"images\": true, POST /plain/v1/images/{endpoint} goes to the SIWC route as before",
              r.status_code == 200 and len(backend.calls) == nb and sent is not None
              and str(sent.url) == f"https://api.openai.com/v1/images/{endpoint}"
              and sent.headers["authorization"] == f"Bearer {SIWC['plain'][0]}" and sent.content == PROMPT
              and "route" not in one(new))

    routes = (("the SIWC route", "/openai/v1/responses", siwc["openai"]),
              ("a static-bearer provider", "/lan/v1/messages", lan),
              ("the Claude passthrough", "/v1/messages", claude),
              ("the /v1/chat/completions endpoint", "/v1/chat/completions", claude))
    body = js({"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]})
    for label, path, upstream in routes:
        n = len(upstream.calls)
        r, _ = await call("POST", path, content=body, headers=HDRS)
        sent = upstream.calls[-1] if len(upstream.calls) == n + 1 else None
        check(f"x-openai-actor-authorization does not reach {label}",
              r.status_code == 200 and sent is not None and "x-openai-actor-authorization" not in sent.headers
              and not leaks(sent, ["claude-rotate", DESK]))
    check("the SIWC route got its own token on the images provider",
          siwc["openai"].calls[-1].headers["authorization"] == f"Bearer {SIWC['openai'][0]}")

    n = len(backend.calls)
    r, _ = await call("POST", "/openai/v1/images/generations", content=PROMPT, headers=HDRS, key=LANONLY)
    r2, _ = await call("POST", "/openai/v1/images/generations", content=PROMPT, headers=HDRS, key="dev-unknown-fake")
    check("a device without the provider's grant gets 403 and an unknown key 401, no backend call",
          r.status_code == 403 and r2.status_code == 401 and len(backend.calls) == n)

    backend.answer = lambda request: httpx.Response(307, headers={"location": "https://elsewhere.test/collect",
                                                                   "set-cookie": "__cf_bm=fake; Path=/"})
    r, new = await call("POST", "/openai/v1/images/generations", content=PROMPT, headers=HDRS)
    check("a redirect from the backend is relayed, not followed, and its cookie is not",
          r.status_code == 307 and r.headers.get("location") == "https://elsewhere.test/collect"
          and len(backend.calls) == n + 1 and "set-cookie" not in r.headers and one(new).get("status") == 307)
    backend.answer = image_ok
    check("image slots, waiters, large bodies and admission are idle after the routing requests", idle())

    # 2. Answers
    held = asyncio.Event()
    stream = Body(IMAGE[:20], held, IMAGE[20:])
    backend.answer = lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
    sent = []
    task = asyncio.create_task(direct("POST", "/openai/v1/images/generations", PROMPT, sent=sent))
    first = await until(lambda: any(m.get("body") for m in sent if m["type"] == "http.response.body"))
    check("an image answer is relayed as it arrives: the first bytes before the upstream ends",
          first and not task.done() and b"".join(m.get("body", b"") for m in sent[1:]) == IMAGE[:20])
    held.set()
    head, got = await task
    check("and then whole, the upstream closed", head.get("status") == 200 and got == IMAGE and stream.closed)

    saved = R.MAX_RESPONSE_BYTES
    try:
        R.MAX_RESPONSE_BYTES = 1024
        large = js({"created": 1, "data": [{"b64_json": "QUJD" * 2000}]})
        answer = lambda request: httpx.Response(  # noqa: E731
            200, headers={"content-type": "application/json", "content-length": str(len(large))},
            stream=Body(*[large[i:i + 1000] for i in range(0, len(large), 1000)]))
        backend.answer = siwc["openai"].answer = answer
        r, new = await call("POST", "/openai/v1/images/generations", content=PROMPT)
        check("a sized JSON answer above max_response_bytes is relayed whole on the image path",
              r.status_code == 200 and r.content == large and one(new).get("bytes") == len(large)
              and one(new).get("status") == 200)
        r, new = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
        check("the same answer on /v1/responses meets the limit (502), so the limit was in force",
              r.status_code == 502 and "upstream response too large" in r.text)
    finally:
        R.MAX_RESPONSE_BYTES = saved
        siwc["openai"].answer = ok

    limited = {"content-type": "application/json", "retry-after": "60", "x-codex-active-limit": "imagegen_premium",
               "x-codex-primary-used-percent": "100", "x-codex-primary-window-minutes": "10080",
               "x-codex-primary-reset-at": str(NOW + 7200), "x-codex-plan-type": "plus"}
    error = js({"error": {"type": "usage_limit_reached", "message": "fake: image limit reached",
                          "resets_in_seconds": 7200}})
    backend.answer = lambda request: httpx.Response(429, headers=limited, content=error)
    r, new = await call("POST", "/openai/v1/images/generations", content=PROMPT)
    check("a 429 is relayed unchanged with its x-codex-* headers and retry-after",
          r.status_code == 429 and r.content == error
          and all(r.headers.get(k) == v for k, v in limited.items()) and one(new).get("status") == 429)
    error = js({"error": {"type": "server_error", "message": "fake: generation failed"}})
    backend.answer = lambda request: httpx.Response(500, headers={"content-type": "application/json"}, content=error)
    n = len(backend.calls)
    r, new = await call("POST", "/openai/v1/images/edits", content=PROMPT)
    check("a JSON 500 is relayed unchanged, sent once (no retry by the gateway)",
          r.status_code == 500 and r.content == error and len(backend.calls) == n + 1
          and one(new).get("status") == 500 and one(new).get("route") == "codex-images")

    page = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>challenge</body></html>"
    stream = Body(page)
    backend.answer = lambda request: httpx.Response(403, headers={"content-type": "text/html; charset=UTF-8"},
                                                    stream=stream)
    r, new = await call("POST", "/openai/v1/images/generations", content=PROMPT)
    check("an HTML 403 becomes a short JSON error with the same status naming the content type",
          r.status_code == 403 and r.headers.get("content-type") == "application/json"
          and r.json() == {"error": {"message": "claude-rotate: image generation failed: the ChatGPT backend "
                                                "answered 403 with text/html; charset=UTF-8", "type": "api_error"}}
          and b"challenge" not in r.content and stream.closed)
    check("audited with the upstream status and content type",
          one(new).get("status") == 403 and one(new).get("upstream_content_type") == "text/html; charset=UTF-8"
          and one(new).get("route") == "codex-images" and "duration_s" in one(new))

    stream = Body(IMAGE[:25], httpx.ReadError("connection reset"))
    backend.answer = lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
    sent, before = [], len(rows())
    try:
        await direct("POST", "/openai/v1/images/generations", PROMPT, sent=sent)
        raised = None
    except Exception as exc:
        raised = exc
    chunks, rec = [m for m in sent if m["type"] == "http.response.body"], one(rows()[before:])
    check("an image answer that breaks off breaks the client connection: the bytes relayed, then no end",
          raised is None and b"".join(m.get("body", b"") for m in chunks) == IMAGE[:25]
          and all(m.get("more_body") for m in chunks))
    check("and it is audited as interrupted by the upstream, with the bytes relayed, the upstream closed",
          rec.get("interrupted") == "upstream ReadError" and rec.get("status") == 200 and rec.get("bytes") == 25
          and rec.get("route") == "codex-images" and stream.closed)

    stream = Body(IMAGE[:25], asyncio.Event(), IMAGE[25:])
    backend.answer = lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
    before = len(rows())
    head, got = await direct("POST", "/openai/v1/images/generations", PROMPT, cut_after_body=True)
    rec = one(rows()[before:])
    check("a client that leaves mid-answer is audited as interrupted by the client, the upstream closed",
          head.get("status") == 200 and got == IMAGE[:25] and rec.get("interrupted") == "client"
          and rec.get("route") == "codex-images" and stream.closed)
    backend.answer = image_ok
    check("idle again after the answers, broken and cut ones included", idle())

    # 3. The Codex login
    login = OAI.images
    old_access, old_refresh = login.rec["access_token"], login.rec["refresh_token"]
    new_access = secret(jwt(exp=NOW + 5000, sub="fake-user", n=2), True)
    new_id = jwt(sub="fake-user", email="fake@example.com")
    disk_at_replay = []

    def by_token(request):
        if request.headers["authorization"] == f"Bearer {old_access}":
            return httpx.Response(401, json={"error": {"type": "invalid_request_error", "code": "token_expired",
                                                       "message": "fake: token expired"}})
        disk_at_replay.append(json.loads(CX_PATH.read_text()).get("access_token"))
        return image_ok(request)
    backend.answer = by_token
    cx_auth.answer = lambda request: httpx.Response(200, json={
        "access_token": new_access, "refresh_token": secret("fake-cx-refresh-2", True), "id_token": new_id,
        "expires_in": 60})
    nb, na = len(backend.calls), len(cx_auth.calls)
    r, new = await call("POST", "/openai/v1/images/generations", content=PROMPT, headers=HDRS)
    posts = cx_auth.calls[na:]
    check("a 401 from the backend gets one refresh", len(posts) == 1)
    post = posts[0] if posts else None
    check("the refresh is a JSON POST to https://auth.openai.com/oauth/token with the record's client id, "
          "the refresh token and refresh_reason unauthorized_recovery, no resource",
          post is not None and post.method == "POST" and str(post.url) == TOKEN_URL
          and post.headers.get("content-type") == "application/json"
          and json.loads(post.content) == {"grant_type": "refresh_token", "client_id": CX_CLIENT,
                                           "refresh_token": old_refresh, "refresh_reason": "unauthorized_recovery"})
    replay = backend.calls[nb:]
    check("and the request is replayed once with the new token, same URL and body, and its answer relayed",
          r.status_code == 200 and r.content == IMAGE and len(replay) == 2
          and replay[1].headers["authorization"] == f"Bearer {new_access}"
          and str(replay[1].url) == str(replay[0].url) == BACKEND + "generations"
          and replay[1].content == replay[0].content == PROMPT)
    check("the new token was on disk before it was used", disk_at_replay == [new_access])
    check("audited with the retry and the refresh after the 401",
          one(new).get("retries") == 1 and one(new).get("refresh_after_401") == "attempted"
          and one(new).get("upstream_error") == "invalid_request_error" and one(new).get("status") == 200)
    disk = json.loads(CX_PATH.read_text())
    check("the record on disk takes expires_at from the new JWT's exp, not expires_in",
          disk.get("expires_at") == NOW + 5000 and disk.get("access_token") == new_access
          and disk.get("refresh_token") == "fake-cx-refresh-2")
    check("it keeps the client and account ids, stores no id token, mode 600, the old one as .prev",
          disk.get("client_id") == CX_CLIENT and disk.get("account_id") == CX_ACCOUNT and "id_token" not in disk
          and new_id not in CX_PATH.read_text() and stat.S_IMODE(CX_PATH.stat().st_mode) == 0o600
          and json.loads((CREDS / "openai.codex.json.prev").read_text()).get("refresh_token") == old_refresh)
    check("the provider's own credential was not refreshed", siwc_auth.calls == [])

    backend.answer = lambda request: httpx.Response(401, json={"error": {"type": "invalid_request_error"}})
    na = len(cx_auth.calls)
    r, new = await call("POST", "/openai/v1/images/generations", content=PROMPT)
    check("a second 401 within 10 min is relayed with no refresh: the floor on forced refreshes",
          r.status_code == 401 and len(cx_auth.calls) == na and "retries" not in one(new)
          and one(new).get("refresh_after_401", "").startswith("held back"))
    backend.answer = image_ok

    def refresh_answer(body):
        cx_auth.answer = lambda request: httpx.Response(200, json=body)

    login.rec["expires_at"] = time.time() + 1200
    na = len(cx_auth.calls)
    await OAI.maintain()
    check("maintain() leaves a login with 20 min left alone", len(cx_auth.calls) == na)
    routine_access = secret(jwt(exp=NOW + 9000, n=3), True)
    refresh_answer({"access_token": routine_access, "refresh_token": secret("fake-cx-refresh-3", True)})
    login.rec["expires_at"] = time.time() + 300
    await OAI.maintain()
    post = cx_auth.calls[-1] if len(cx_auth.calls) == na + 1 else None
    check("maintain() refreshes a login with under 10 min left, refresh_reason proactive_expiry",
          post is not None and str(post.url) == TOKEN_URL
          and json.loads(post.content) == {"grant_type": "refresh_token", "client_id": CX_CLIENT,
                                           "refresh_token": "fake-cx-refresh-2", "refresh_reason": "proactive_expiry"}
          and json.loads(CX_PATH.read_text()).get("expires_at") == NOW + 9000
          and login.rec["access_token"] == routine_access)

    refresh_answer({"access_token": secret("fake-cx-opaque-4", True),
                    "refresh_token": secret("fake-cx-refresh-4", True), "expires_in": 7200})
    login.rec["expires_at"] = time.time() + 300
    await OAI.maintain()
    disk = json.loads(CX_PATH.read_text())
    check("an access token without exp takes expires_at from expires_in",
          disk.get("access_token") == "fake-cx-opaque-4" and abs(disk.get("expires_at", 0) - (time.time() + 7200)) < 5)
    refresh_answer({"access_token": secret("fake-cx-opaque-5", True)})
    login.rec["expires_at"] = time.time() + 300
    await OAI.maintain()
    disk = json.loads(CX_PATH.read_text())
    check("with neither exp nor expires_in it is about 8 days ahead, and a missing refresh token keeps the stored one",
          disk.get("access_token") == "fake-cx-opaque-5" and disk.get("refresh_token") == "fake-cx-refresh-4"
          and abs(disk.get("expires_at", 0) - (time.time() + 8 * 86400)) < 5 and login.last_error is None)
    refresh_answer({"refresh_token": secret("fake-cx-refresh-6", True)})
    login.rec["expires_at"] = time.time() + 300
    await OAI.maintain()
    disk = json.loads(CX_PATH.read_text())
    check("an answer with only a refresh token keeps the access token, saves the new refresh token, "
          "and counts as a failed refresh",
          disk.get("access_token") == "fake-cx-opaque-5" and disk.get("refresh_token") == "fake-cx-refresh-6"
          and login.last_error == "the refresh answer kept the same access token" and login.retry_at > time.time()
          and login.down is None)

    r = await C.get("/rotate/status", headers={"authorization": f"Bearer {DESK}"})
    status = r.json()["providers"] if r.status_code == 200 else {}
    im = status.get("openai", {}).get("images") or {}
    check("the provider's status carries an images object: state, reason, unsaved, last_error, "
          "access_token_min, refreshes, last_answer (the 401 just relayed), usage_error",
          set(im) == {"state", "reason", "unsaved", "last_error", "access_token_min", "refreshes", "last_answer",
                      "usage_error"}
          and im["state"] == "ok" and im["reason"] is None and im["last_error"] == login.last_error
          and abs(im["access_token_min"] - 8 * 1440) <= 1 and im["refreshes"] == 4
          and im["last_answer"].get("status") == 401 and abs(im["last_answer"].get("at", 0) - time.time()) < 60)
    check("a provider without images has no images object, the images provider's own state is unchanged",
          "images" not in status.get("plain", {}) and status.get("openai", {}).get("state") == "ok"
          and status.get("openai", {}).get("access_token_min", 0) > 40000)

    login = new_login(codex_record(10))
    backend.answer = lambda request: httpx.Response(401, json={"error": {"type": "invalid_request_error"}})
    cx_auth.answer = lambda request: httpx.Response(401, json={"detail": "fake: refresh token revoked"})
    na = len(cx_auth.calls)
    r, new = await call("POST", "/openai/v1/images/generations", content=PROMPT)
    check("a refresh answered 401 takes the images login down; the request gets 503 naming codex login",
          len(cx_auth.calls) == na + 1 and r.status_code == 503 and r.json()["error"]["type"] == "provider_unavailable"
          and r.json()["error"]["message"].startswith(
              "claude-rotate: image generation is unavailable: the refresh was refused (401): "
              "make a new login with `codex login`")
          and "--images" in r.text and login.down is not None and one(new).get("status") == 503
          and one(new).get("route") == "codex-images")
    backend.answer = image_ok
    nb = len(backend.calls)
    r, _ = await call("POST", "/openai/v1/images/edits", content=PROMPT)
    r2, _ = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
    check("then the image paths answer 503 without a backend call while /v1/responses still works",
          r.status_code == 503 and len(backend.calls) == nb and r2.status_code == 200 and OAI.down is None)
    check("the refusal is in the record on disk, so a restart keeps the images login down",
          P.CodexLogin("openai", tmp).down == login.down)

    login = new_login(codex_record(11, expires_in=300))
    cx_auth.answer = lambda request: httpx.Response(400, json={"error": "invalid_grant"})
    await OAI.maintain()
    r, _ = await call("POST", "/openai/v1/images/generations", content=PROMPT)
    r2, _ = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
    check("a refresh answered 400 invalid_grant takes the images login down too, and only it",
          login.down is not None and "(invalid_grant)" in login.down and r.status_code == 503
          and "invalid_grant" in r.text and r2.status_code == 200 and OAI.down is None
          and OAI.status()["state"] == "ok")
    login = new_login(codex_record(12, expires_in=300))
    cx_auth.answer = lambda request: httpx.Response(400, json={"error": "invalid_request"})
    await OAI.maintain()
    check("a 400 with another code is a failure with backoff, not a refusal",
          login.down is None and login.last_error == "token endpoint answered 400 (invalid_request)"
          and login.retry_at > time.time())

    nb, nd = len(backend.calls), len(siwc["draw"].calls)
    r, new = await call("POST", "/draw/v1/images/generations", content=PROMPT)
    check("with images on and no login installed, the image paths answer 503 'image generation is unavailable'",
          r.status_code == 503 and r.json()["error"]["message"].startswith(
              "claude-rotate: image generation is unavailable: no credential credentials/draw.codex.json")
          and "install-credential PROVIDER --images" in r.text and len(backend.calls) == nb
          and len(siwc["draw"].calls) == nd and one(new).get("route") == "codex-images")
    r, _ = await call("POST", "/draw/v1/responses", content=js({"model": "gpt-5.1-codex"}))
    check("while /v1/responses on that provider still works",
          r.status_code == 200 and len(siwc["draw"].calls) == nd + 1
          and siwc["draw"].calls[-1].headers["authorization"] == f"Bearer {SIWC['draw'][0]}")
    st = DRAW.status()
    stats = (await C.get("/rotate/stats", headers={"authorization": f"Bearer {DESK}"})).json()
    check("its status says images down with the reason, the provider ok, and /rotate/stats alerts on it",
          st["state"] == "ok" and st["images"]["state"] == "down" and "draw.codex.json" in st["images"]["reason"]
          and st["images"]["access_token_min"] is None
          and any(a["text"].startswith("provider draw: image generation is down: no credential")
                  for a in stats["alerts"]))
    login = new_login(codex_record(13))
    check("idle again after the credential requests", idle())

    # 4. Sizes and admission
    saved = (R.MAX_REQUEST_BYTES, R.MAX_IMAGE_REQUEST_BYTES, R.MAX_IMAGE_BYTES_IN_FLIGHT,
             R.MAX_CONCURRENT_IMAGES, R.IMAGE_LINE_MAX)
    try:
        R.MAX_REQUEST_BYTES, R.MAX_IMAGE_REQUEST_BYTES, R.MAX_IMAGE_BYTES_IN_FLIGHT = 1000, 4000, 5000
        big = js({"model": "gpt-image-2", "prompt": "edit", "images": ["data:image/png;base64," + "A" * 2900]})
        check("the edit body is between max_request_bytes and max_image_request_bytes, two exceed the budget",
              1000 < len(big) <= 4000 and 2 * len(big) > 5000)
        n = len(backend.calls)
        r, _ = await call("POST", "/openai/v1/images/edits", content=big)
        check("under the images provider a large edit body is accepted on the image path",
              r.status_code == 200 and len(backend.calls) == n + 1 and backend.calls[-1].content == big)
        n = len(siwc["openai"].calls)
        r, _ = await call("POST", "/openai/v1/responses", content=big)
        check("and on /v1/responses, which carries generated images back",
              r.status_code == 200 and len(siwc["openai"].calls) == n + 1 and siwc["openai"].calls[-1].content == big)
        for path, upstream in (("/plain/v1/responses", siwc["plain"]), ("/plain/v1/images/edits", siwc["plain"]),
                               ("/lan/v1/messages", lan)):
            n = len(upstream.calls)
            r, _ = await call("POST", path, content=big)
            check(f"the same size gets 413 on {path}, under a provider without images",
                  r.status_code == 413 and len(upstream.calls) == n)
        n = len(backend.calls)
        r, _ = await call("POST", "/openai/v1/images/edits", content=big + b" " * (4001 - len(big)))
        check("a body above max_image_request_bytes gets 413 under the images provider too",
              r.status_code == 413 and len(backend.calls) == n and R.LARGE_BODIES.held == 0)

        release, entered = asyncio.Event(), []

        async def gated(request):
            entered.append(request)
            await release.wait()
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=Body(IMAGE[:9], IMAGE[9:]))
        backend.answer = gated
        first = asyncio.create_task(call("POST", "/openai/v1/images/edits", content=big))
        await until(lambda: len(entered) == 1)
        check("a large body in flight counts against the budget", R.LARGE_BODIES.held == len(big))
        n = len(siwc["openai"].calls)
        r, _ = await call("POST", "/openai/v1/responses", content=big)
        check("a second one beyond max_image_bytes_in_flight gets 503 with Retry-After, no upstream call",
              r.status_code == 503 and r.headers.get("retry-after") == "5" and "too many large requests" in r.text
              and len(siwc["openai"].calls) == n)
        r, _ = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
        check("a body under max_request_bytes does not count and still goes", r.status_code == 200
              and R.LARGE_BODIES.held == len(big))
        release.set()
        r, _ = await first
        check("the held request then completes and the budget is back to 0 after its stream",
              r.status_code == 200 and r.content == IMAGE and R.LARGE_BODIES.held == 0)
        sse = b'event: response.completed\ndata: {"type":"response.completed","response":{}}\n\n'
        siwc["openai"].answer = lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Body(sse[:10], sse[10:]))
        r, _ = await call("POST", "/openai/v1/responses", content=big)
        siwc["openai"].answer = ok
        check("and back to 0 after a large /v1/responses stream", r.status_code == 200 and r.content == sse
              and R.LARGE_BODIES.held == 0)
        backend.answer = lambda request: httpx.Response(200, headers={"content-type": "application/json"},
                                                        stream=Body(IMAGE[:9], asyncio.Event(), IMAGE[9:]))
        await direct("POST", "/openai/v1/images/edits", big, cut_after_body=True)
        check("and after a large image request whose client left mid-answer", idle())

        R.MAX_CONCURRENT_IMAGES, R.IMAGE_LINE_MAX = 1, 2
        release, entered = asyncio.Event(), []
        backend.answer = gated
        small = PROMPT
        a = asyncio.create_task(call("POST", "/openai/v1/images/generations", content=small))
        await until(lambda: len(entered) == 1)
        b = asyncio.create_task(call("POST", "/openai/v1/images/generations", content=small))
        gone = asyncio.Event()
        c = asyncio.create_task(direct("POST", "/openai/v1/images/edits", small, gone=gone))
        lined = await until(lambda: R.IMAGE_LINE.waiting == 2)
        await asyncio.sleep(0.3)
        check("with max_concurrent_images 1, a second and a third image request wait in line, not at the backend",
              lined and R.IMAGE_LINE.active == 1 and R.IMAGE_LINE.waiting == 2 and len(entered) == 1
              and not b.done() and not c.done())
        r, new = await call("POST", "/openai/v1/images/generations", content=small)
        check("a request beyond IMAGE_LINE_MAX waiting gets 429, audited, no backend call",
              r.status_code == 429 and r.json()["error"]["type"] == "rate_limit_error"
              and "2 image requests are already waiting" in r.text and len(entered) == 1
              and one(new).get("status") == 429 and one(new).get("route") == "codex-images")
        r, _ = await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
        check("the line does not hold back /v1/responses", r.status_code == 200)
        before = len(rows())
        gone.set()
        head, _ = await c
        check("a waiter whose client disconnects leaves the line, audited 499, never sent",
              head.get("status") == 499 and R.IMAGE_LINE.waiting == 1 and len(entered) == 1
              and one(rows()[before:]).get("status") == 499 and one(rows()[before:]).get("route") == "codex-images")
        release.set()
        (ra, _), (rb, _) = await asyncio.gather(a, b)
        check("once the first ends the second goes, and both complete",
              ra.status_code == rb.status_code == 200 and ra.content == rb.content == IMAGE and len(entered) == 2)
        check("IMAGE_LINE.active and waiting, the large-body budget and admission are back to 0", idle())
    finally:
        (R.MAX_REQUEST_BYTES, R.MAX_IMAGE_REQUEST_BYTES, R.MAX_IMAGE_BYTES_IN_FLIGHT,
         R.MAX_CONCURRENT_IMAGES, R.IMAGE_LINE_MAX) = saved
        backend.answer = image_ok

    # 5. Quotas
    OAI.quotas.clear()
    resets = int(time.time()) + 7200
    backend.answer = lambda request: httpx.Response(200, headers={
        "content-type": "application/json", "x-codex-active-limit": "imagegen_premium",
        "x-codex-primary-used-percent": "95.0", "x-codex-primary-window-minutes": "10080",
        "x-codex-primary-reset-at": str(resets), "x-codex-plan-type": "plus"}, content=IMAGE)
    siwc["openai"].answer = lambda request: httpx.Response(200, headers={
        "x-codex-primary-used-percent": "40", "x-codex-primary-window-minutes": "300",
        "x-codex-primary-reset-at": str(resets - 3600), "x-codex-secondary-used-percent": "20",
        "x-codex-secondary-window-minutes": "10080", "x-codex-secondary-reset-at": str(resets + 86400)},
        json={"ok": True})
    await call("POST", "/openai/v1/images/generations", content=PROMPT)
    await call("POST", "/openai/v1/responses", content=js({"model": "gpt-5.1-codex"}))
    siwc["openai"].answer, backend.answer = ok, image_ok
    r = await C.get("/rotate/status", headers={"authorization": f"Bearer {DESK}"})
    status_text = r.text
    quotas = r.json()["providers"]["openai"].get("quotas") or {}
    img, cdx = quotas.get("imagegen_premium") or {}, quotas.get("codex") or {}
    check("/rotate/status keeps the provider's quotas keyed imagegen_premium and codex",
          set(quotas) == {"imagegen_premium", "codex"})
    check("imagegen_premium from the image answer: primary window, plan, route images, seen_at",
          img.get("primary") == {"used_percent": 95.0, "window_minutes": 10080, "resets_at": resets}
          and img.get("secondary") is None and img.get("plan") == "plus" and img.get("route") == "images"
          and abs(img.get("seen_at", 0) - time.time()) < 60)
    check("codex from the /v1/responses answer: primary and secondary windows, route inference",
          cdx.get("primary") == {"used_percent": 40.0, "window_minutes": 300, "resets_at": resets - 3600}
          and cdx.get("secondary") == {"used_percent": 20.0, "window_minutes": 10080, "resets_at": resets + 86400}
          and cdx.get("route") == "inference" and abs(cdx.get("seen_at", 0) - time.time()) < 60)
    r = await C.get("/rotate/stats", headers={"authorization": f"Bearer {DESK}"})
    alerts = [a["text"] for a in r.json()["alerts"] if "quota" in a["text"]]
    check("/rotate/stats alerts on the quota at 90 % or more, and only on it",
          len(alerts) == 1 and alerts[0].startswith("provider openai: quota imagegen_premium at 95%, resets in 2.0 h"))

    # 6. The plan's usage windows, read from /wham/usage with the Codex login.
    now = int(time.time())
    wham = {"plan_type": "prolite", "rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_after_seconds": 600,
                           "reset_at": now + 600},
        "secondary_window": {"used_percent": 34, "limit_window_seconds": 604800, "reset_after_seconds": 86400,
                             "reset_at": now + 86400}},
        "additional_rate_limits": [{"limit_name": "Images", "metered_feature": "imagegen_premium", "rate_limit": {
            "primary_window": {"used_percent": 3, "limit_window_seconds": 86400, "reset_after_seconds": 99,
                               "reset_at": now + 99}}}]}
    usage_status = [200]

    def usage_answer(request):
        if request.url.path == "/backend-api/wham/usage":
            return httpx.Response(usage_status[0], json=wham if usage_status[0] == 200 else {"error": "x"})
        return image_ok(request)

    login = OAI.images
    backend.answer, login.usage_at, n = usage_answer, 0.0, len(backend.calls)
    await OAI.maintain()
    polls = [req for req in backend.calls[n:] if req.url.path == "/backend-api/wham/usage"]
    check("maintain() reads the plan's usage: one GET https://chatgpt.com/backend-api/wham/usage with the "
          "Codex login's bearer and account id",
          len(polls) == 1 and polls[0].method == "GET" and str(polls[0].url) == P.CODEX_USAGE_URL
          and polls[0].headers.get("authorization") == f"Bearer {login.rec['access_token']}"
          and polls[0].headers.get("chatgpt-account-id") == login.rec["account_id"]
          and not leaks(polls[0], SECRETS - CODEX_SECRETS))
    q = OAI.status()["quotas"]
    check("its 5-hour and weekly windows become quota codex, route usage, with the plan",
          q.get("codex", {}).get("primary") == {"used_percent": 12.0, "window_minutes": 300, "resets_at": now + 600}
          and q["codex"].get("secondary") == {"used_percent": 34.0, "window_minutes": 10080, "resets_at": now + 86400}
          and q["codex"].get("route") == "usage" and q["codex"].get("plan") == "prolite")
    check("an additional limit lands under its metered feature",
          q.get("imagegen_premium", {}).get("primary") == {"used_percent": 3.0, "window_minutes": 1440,
                                                            "resets_at": now + 99}
          and q["imagegen_premium"].get("route") == "usage" and login.usage_error is None)
    await OAI.maintain()
    check("the usage is not read again within 5 minutes",
          len([req for req in backend.calls[n:] if req.url.path == "/backend-api/wham/usage"]) == 1)
    usage_status[0], login.usage_at = 503, 0.0
    await OAI.maintain()
    r = await C.get("/rotate/status", headers={"authorization": f"Bearer {DESK}"})
    status_text = status_text + r.text
    check("a failed usage query keeps the last figures and says why in the images status",
          OAI.status()["quotas"]["codex"]["primary"]["used_percent"] == 12.0
          and r.json()["providers"]["openai"]["images"]["usage_error"] == "usage query answered 503"
          and OAI.images.down is None)
    n = len(backend.calls)
    DRAW.images.usage_at = 0.0
    await DRAW.maintain()
    check("a provider whose images login is missing reads no usage", len(backend.calls) == n)
    backend.answer = image_ok

    # 7. The figures survive a restart.
    R.save_quotas()
    saved = json.loads(R.QUOTAS_PATH.read_text())
    check("save_quotas() writes the providers' figures next to the state file",
          set(saved) == {"openai"} and saved["openai"]["codex"]["primary"]["used_percent"] == 12.0
          and "imagegen_premium" in saved["openai"])
    before, mtime = dict(OAI.quotas), R.QUOTAS_PATH.stat().st_mtime_ns
    R.save_quotas()
    check("and does not write again while nothing changed", R.QUOTAS_PATH.stat().st_mtime_ns == mtime)
    OAI.quotas = {}
    R.load_quotas()
    check("load_quotas() at startup gives a provider its last figures back", OAI.quotas == before)
    R.QUOTAS_PATH.write_text("{not json")
    OAI.quotas = {}
    R.load_quotas()
    check("an unreadable file is ignored", OAI.quotas == {})
    OAI.quotas = before
    R.save_quotas()

    audit_text = R.AUDIT_PATH.read_text()
    check("no token or device key appears in /rotate/status, /rotate/stats or the audit log",
          not any(s in status_text or s in r.text or s in audit_text for s in SECRETS))
    others = [req for up in (claude, lan, *siwc.values(), siwc_auth) for req in up.calls]
    check("the Codex login's tokens reached only the backend and its token endpoint",
          not any(leaks(req, CODEX_SECRETS) for req in others))
    check("the backend got no other credential",
          not any(leaks(req, SECRETS - CODEX_SECRETS) for req in backend.calls + cx_auth.calls))
    check("no provider's own credential was refreshed", siwc_auth.calls == [])
    check("all idle at the end", idle())
    await C.aclose()


asyncio.run(main())
print(f"\nall {PASS} checks passed")
