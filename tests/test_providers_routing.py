"""Providers under a path prefix: which upstream a request reaches and with which
credential, what is stripped on the way in and out, which device may use what, and
the provider and grant config that refuses to start. Recording stand-ins replace
api.anthropic.com, a static-bearer upstream and api.openai.com; nothing leaves the
process. The file builds its own config before importing rotator.

  python3 tests/test_providers_routing.py
"""
import asyncio
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

ACCOUNT_TOKEN, LAN_TOKEN = "fake-account-token", "fake-lan-token"
ACCESS, REFRESH = "fake-access-1", "fake-refresh-1"
SECRETS = [ACCOUNT_TOKEN, LAN_TOKEN, ACCESS, REFRESH]
KEY = {"claude": "fake-key-claude", "lanbox": "fake-key-lanbox", "oaibox": "fake-key-oaibox",
       "allbox": "fake-key-allbox", "tiny": "fk-tiny"}
LAN_SPEC = {"kind": "static-bearer", "upstream": "https://lan.example/api", "token_file": "tokens/lan.token"}
SANDBOX = "default-src 'none'; sandbox; frame-ancestors 'none'"


def fixture(cfg):
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    (d / "tokens" / "acct.token").write_text(ACCOUNT_TOKEN + "\n")
    (d / "tokens" / "lan.token").write_text(LAN_TOKEN + "\n")
    (d / "credentials").mkdir()
    (d / "credentials" / "openai.json").write_text(json.dumps({
        "access_token": ACCESS, "refresh_token": REFRESH, "client_id": "fake-client",
        "expires_at": int(time.time()) + 10 * 365 * 86400}))
    (d / "config.json").write_text(json.dumps(
        {"accounts": [{"name": "acct", "token_file": "tokens/acct.token"}]} | cfg))
    return d


tmp = fixture({
    "devices": {"claude": KEY["claude"],
                "lanbox": {"key": KEY["lanbox"], "providers": ["lan"]},
                "oaibox": {"key": KEY["oaibox"], "providers": ["openai"]},
                "allbox": {"key": KEY["allbox"], "providers": ["anthropic", "lan", "openai"]},
                "tiny": KEY["tiny"]},
    "providers": {"lan": LAN_SPEC, "openai": {"kind": "openai-siwc"}}})
os.environ.update(CLAUDE_ROTATE_CONFIG=str(tmp / "config.json"),
                  CLAUDE_ROTATE_STATE=str(tmp / "state.json"),
                  CLAUDE_ROTATE_AUDIT=str(tmp / "audit.jsonl"))
import providers as P  # noqa: E402
import rotator as R  # noqa: E402

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


seen = {"anthropic": [], "lan": [], "openai": [], "auth": []}
reply = {}  # upstream -> callable(request) answering instead of 200 {"ok": true}


def stand_in(name):
    def handle(req):
        seen[name].append(req)
        if name in reply:
            return reply[name](req)
        return httpx.Response(200, json={"ok": True})
    return handle


LAN, OAI = R.PROVIDERS["lan"], R.PROVIDERS["openai"]
CLIENTS = {"anthropic": R.client, "lan": LAN.client, "openai": OAI.client, "auth": OAI.auth}
for _name, _client in CLIENTS.items():
    _client._transport = httpx.MockTransport(stand_in(_name))
check("every upstream client routes to its stand-in",
      all(isinstance(cl._transport, httpx.MockTransport) and all(t is None for t in cl._mounts.values())
          for cl in CLIENTS.values()))
check("both providers load ready under /lan and /openai",
      set(R.PREFIXES) == {"lan", "openai"} and LAN.down is None and OAI.down is None)


def snap():
    return {k: len(v) for k, v in seen.items()}


def calls(before):
    return {k: len(v) - before[k] for k, v in seen.items() if len(v) != before[k]}


def leaks(req, key):
    """The device key anywhere upstream, or a header that must not leave."""
    texts = [str(req.url)] + [f"{k}: {v}" for k, v in req.headers.multi_items()]
    return any(key in t for t in texts) or "x-api-key" in req.headers or "cookie" in req.headers


async def ask(c, path, device=None, method="GET", headers=None, key=None, body=b""):
    key = key or (KEY[device] if device else None)
    hdrs = {"authorization": f"Bearer {key}"} if key else {}
    return await c.request(method, "http://proxy" + path, headers=hdrs | (headers or {}), content=body)


async def forwarding(c):
    host = {"anthropic": "api.anthropic.com", "lan": "lan.example", "openai": "api.openai.com"}
    token = {"anthropic": ACCOUNT_TOKEN, "lan": LAN_TOKEN, "openai": ACCESS}
    cases = [
        ("claude", "POST", "/v1/messages?beta=true", "bearer", "anthropic", b"/v1/messages?beta=true"),
        ("lanbox", "GET", f"/lan/v1/models?x=1&key={KEY['lanbox']}&y=a%20b", "x-api-key", "lan",
         b"/api/v1/models?x=1&y=a%20b"),
        ("oaibox", "POST", "/openai/v1/responses?stream=false", "bearer", "openai", b"/v1/responses?stream=false"),
        ("allbox", "POST", "/v1/messages", "bearer", "anthropic", b"/v1/messages"),
        ("allbox", "GET", f"/lan/chat?q=1&key={KEY['allbox']}", "query", "lan", b"/api/chat?q=1"),
        ("allbox", "POST", "/openai/v1/chat/completions", "bearer", "openai", b"/v1/chat/completions"),
    ]
    for dev, method, path, via, up, raw in cases:
        key = KEY[dev]
        hdrs = {"cookie": "c=1", "tailscale-user-login": "someone@example.com", "x-forwarded-for": "1.2.3.4"}
        if via == "bearer":
            hdrs |= {"authorization": f"Bearer {key}", "x-api-key": key}
        elif via == "x-api-key":
            hdrs["x-api-key"] = key
        before = snap()
        r = await c.request(method, "http://proxy" + path, headers=hdrs, content=b'{"model": "m"}')
        req = seen[up][-1] if seen[up] else None
        check(f"{dev} {method} {path.split('?')[0]} reaches {host[up]}{raw.decode()} with the {up} "
              "credential, no key, cookie or tailnet header",
              r.status_code == 200 and calls(before) == {up: 1}
              and (req.url.scheme, req.url.host, req.url.port) == ("https", host[up], None)
              and req.url.raw_path == raw and req.url.query == raw.partition(b"?")[2]
              and req.headers["authorization"] == f"Bearer {token[up]}" and not leaks(req, key)
              and "tailscale-user-login" not in req.headers and "x-forwarded-for" not in req.headers)


async def grants(c):
    for dev, path in (("claude", "/lan/x"), ("claude", "/openai/v1/models"),
                      ("lanbox", "/openai/v1/models"), ("oaibox", "/lan/x")):
        before = snap()
        r = await ask(c, path, dev)
        check(f"{dev} on {path}: 403 without an upstream call",
              r.status_code == 403 and r.json()["error"]["type"] == "permission_error" and calls(before) == {})
    for key, label in ((None, "no key"), ("fake-key-nobody", "an unknown key")):
        for method, path in (("GET", "/lan/x"), ("GET", "/openai/v1/models"), ("POST", "/v1/messages"),
                             ("GET", "/"), ("POST", "/v1/chat/completions")):
            before = snap()
            r = await ask(c, path, method=method, key=key, body=b"{}")
            check(f"{label} on {method} {path}: 401 without an upstream call",
                  r.status_code == 401 and calls(before) == {})
    for dev in ("lanbox", "oaibox"):
        for method, path in (("GET", "/"), ("POST", "/v1/messages"), ("POST", "/v1/chat/completions")):
            before = snap()
            body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
            r = await ask(c, path, dev, method=method, body=body)
            check(f"provider-only {dev} on {method} {path}: 403 without an upstream call",
                  r.status_code == 403 and calls(before) == {})
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
    for dev in ("claude", "allbox"):
        before = snap()
        r = await ask(c, "/v1/chat/completions", dev, method="POST", body=body)
        req = seen["anthropic"][-1]
        check(f"{dev} may use /v1/chat/completions on the Claude account",
              r.status_code == 200 and calls(before) == {"anthropic": 1} and req.url.raw_path == b"/v1/messages"
              and req.headers["authorization"] == f"Bearer {ACCOUNT_TOKEN}" and not leaks(req, KEY[dev]))
    before = snap()
    r = await ask(c, "/", "claude")
    check("a string-key device may use the bare root",
          r.status_code == 200 and calls(before) == {"anthropic": 1} and seen["anthropic"][-1].url.raw_path == b"/")
    for path in ("/LAN/x", "/lanx/x", "/Lan/x"):
        before = snap()
        r = await ask(c, path, "lanbox")
        check(f"{path} is not lan: lanbox gets 403, no upstream call", r.status_code == 403 and calls(before) == {})
        before = snap()
        r = await ask(c, path, "allbox")
        check(f"{path} is not lan: it goes to the Claude account as is",
              r.status_code == 200 and calls(before) == {"anthropic": 1}
              and seen["anthropic"][-1].url.raw_path == path.encode())
    before = snap()
    r = await ask(c, "/lan", "lanbox")
    check("/lan alone reaches the upstream's base path",
          r.status_code == 200 and calls(before) == {"lan": 1} and seen["lan"][-1].url.raw_path == b"/api/")


async def paths(c):
    for path, up, host, raw in (("/lan/a%3Fb%23c?q=1", "lan", "lan.example", b"/api/a%3Fb%23c?q=1"),
                                ("/openai/v1/a%3Fb%23c", "openai", "api.openai.com", b"/v1/a%3Fb%23c")):
        before = snap()
        r = await ask(c, path, "allbox")
        req = seen[up][-1]
        check(f"{path}: %3F and %23 stay encoded upstream",
              r.status_code == 200 and calls(before) == {up: 1} and req.url.host == host and req.url.raw_path == raw)
    for path in ("/lan/a%2Fb", "/lan/a%2fb", "/lan/a%5Cb", "/lan/%2e%2e/x", "/lan/%2E%2E", "/lan/%2E/x",
                 "/lan/.%2e/x", "/lan/%2e./x", "/openai/%2e%2e/v1/x", "/openai/v1%2Fx"):
        before = snap()
        r = await ask(c, path, "allbox")
        check(f"{path}: 400 without an upstream call",
              r.status_code == 400 and "dot segment" in r.json().get("detail", "") and calls(before) == {})
    for path, up, host, raw in (("/lan//x", "lan", "lan.example", b"/api//x"),
                                ("/lan//evil.example/x", "lan", "lan.example", b"/api//evil.example/x"),
                                ("/openai//evil.example/v1/x", "openai", "api.openai.com", b"//evil.example/v1/x")):
        before = snap()
        r = await ask(c, path, "allbox")
        req = seen[up][-1]
        check(f"{path} stays on {host}" + (" under /api" if up == "lan" else ""),
              r.status_code == 200 and calls(before) == {up: 1}
              and (req.url.scheme, req.url.host, req.url.port) == ("https", host, None) and req.url.raw_path == raw)
    before = snap()
    r = await ask(c, f"/lan/x?a=1&key={KEY['allbox']}&b=x%20y&key=other&k%65y={KEY['allbox']}&c=%2B+d", "allbox")
    req = seen["lan"][-1]
    check("lan: every key parameter is dropped, the rest keeps spelling and order",
          r.status_code == 200 and calls(before) == {"lan": 1} and req.url.raw_path == b"/api/x?a=1&b=x%20y&c=%2B+d"
          and not leaks(req, KEY["allbox"]))


async def cookies(c):
    for up, a, path in (("lan", "lanbox", "/lan/x"), ("openai", "oaibox", "/openai/v1/models"),
                        ("anthropic", "claude", "/v1/messages")):
        reply[up] = lambda req, up=up: httpx.Response(
            200, headers={"set-cookie": f"sid=from-{up}; Path=/"}, json={"ok": True})
        r1 = await ask(c, path, a)
        del reply[up]
        before = snap()
        r2 = await ask(c, path, "allbox")
        req = seen[up][-1]
        check(f"{up}: a cookie set on {a}'s call does not ride on allbox's",
              r1.status_code == r2.status_code == 200 and calls(before) == {up: 1}
              and "cookie" not in req.headers and not list(CLIENTS[up].cookies.jar))
        if up != "anthropic":
            check(f"{up}: the upstream's set-cookie does not reach the client", "set-cookie" not in r1.headers)


async def connection(c):
    for up, path in (("lan", "/lan/x"), ("openai", "/openai/v1/models"), ("anthropic", "/v1/messages")):
        reply[up] = lambda req: httpx.Response(
            200, headers={"connection": "X-Up, keep-alive", "x-up": "1", "x-other": "2"}, json={"ok": True})
        r = await ask(c, path, "allbox", headers={"connection": "X-Secret", "x-secret": "1", "x-keep": "1"})
        del reply[up]
        req = seen[up][-1]
        check(f"{up}: a header the request's Connection names is not forwarded",
              r.status_code == 200 and "x-secret" not in req.headers and req.headers.get("x-keep") == "1")
        check(f"{up}: a header the upstream's Connection names is not returned",
              "x-up" not in r.headers and r.headers.get("x-other") == "2")


async def csp(c):
    reply["lan"] = lambda req: httpx.Response(
        200, headers={"content-security-policy": "default-src *"}, text="<script>alert(1)</script>")
    r = await ask(c, "/lan/page", "allbox")
    del reply["lan"]
    check("lan: the upstream's CSP is replaced by the sandbox policy",
          r.status_code == 200 and r.headers.get_list("content-security-policy") == [SANDBOX]
          and r.headers["x-rotate-provider"] == "lan")
    reply["openai"] = lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream", "content-security-policy": "default-src *",
                      "set-cookie": "sid=x"},
        content=b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n')
    r = await ask(c, "/openai/v1/responses", "allbox", method="POST", body=b'{"model": "m", "stream": true}')
    del reply["openai"]
    check("openai: a streamed answer carries the sandbox policy, the provider name and no cookie",
          r.status_code == 200 and r.headers.get_list("content-security-policy") == [SANDBOX]
          and r.headers["x-rotate-provider"] == "openai" and "set-cookie" not in r.headers
          and b"response.completed" in r.content)
    r = await ask(c, "/v1/messages", "allbox", method="POST")
    check("the Claude path names the account, not a provider",
          r.headers.get("x-rotate-account") == "acct" and "x-rotate-provider" not in r.headers)


async def panel(c):
    async def hit(path, key=None, login=None, query=False):
        method = "POST" if path == "/rotate/switch" else "GET"
        hdrs = {"content-type": "application/json"}
        if key and not query:
            hdrs["authorization"] = f"Bearer {key}"
        if login:
            hdrs["tailscale-user-login"] = login
        url = "http://proxy" + path + (f"?key={key}" if query else "")
        return (await c.request(method, url, headers=hdrs,
                                content=b'{"auto": true}' if method == "POST" else b"")).status_code

    for tailnet in (False, True):
        R.PANEL_TAILNET = tailnet
        mode = "tailnet on" if tailnet else "tailnet off"
        for path in ("/rotate/panel", "/rotate/status", "/rotate/stats", "/rotate/switch"):
            codes = [await hit(path, KEY[d]) for d in ("claude", "allbox", "lanbox", "oaibox")]
            check(f"{mode} {path}: a key with the anthropic grant opens it, a provider-only key does not",
                  codes == [200, 200, 401, 401])
            code = await hit(path, login="someone@example.com")
            check(f"{mode} {path}: Tailscale-User-Login alone " + ("opens it" if tailnet else "does not"),
                  code == (200 if tailnet else 401))
        codes = [await hit("/rotate/panel", KEY[d], query=True) for d in ("claude", "lanbox")]
        check(f"{mode} /rotate/panel?key=: the claude key opens it, lanbox's does not", codes == [200, 401])
    R.PANEL_TAILNET = False


async def reporting(c):
    r = await ask(c, "/rotate/stats", "claude")
    devs = r.json()["devices"]
    check("stats: no key suffix for a key shorter than 8 characters", devs["tiny"].get("key_suffix") is None)
    check("stats: a longer key shows its last 4 characters and no key appears whole",
          devs["claude"]["key_suffix"] == "…" + KEY["claude"][-4:] and not any(k in r.text for k in KEY.values()))
    check("stats: each device lists its grants",
          devs["claude"]["providers"] == ["anthropic"] and devs["lanbox"]["providers"] == ["lan"]
          and devs["allbox"]["providers"] == ["anthropic", "lan", "openai"])
    check("stats: no token in the answer", not any(s in r.text for s in SECRETS))
    r = await ask(c, "/rotate/status", "claude")
    provs = r.json()["providers"]
    check("status lists both providers, ok",
          set(provs) == {"lan", "openai"} and all(p["state"] == "ok" for p in provs.values())
          and provs["lan"]["upstream"] == "https://lan.example/api" and provs["openai"]["prefix"] == "/openai")
    check("status: no token in the answer", not any(s in r.text for s in SECRETS))
    log = (tmp / "audit.jsonl").read_text()
    check("the audit log names the providers and holds no token or device key",
          '"provider": "lan"' in log and '"provider": "openai"' in log
          and not any(s in log for s in SECRETS + list(KEY.values())))


async def connection_lines(c):
    # RFC 9110 section 5.3: Connection sent on two lines is one list.
    reply["lan"] = lambda req: httpx.Response(
        200, headers=[("connection", "keep-alive"), ("connection", "x-up"), ("x-up", "1")], json={"ok": True})
    r = await ask(c, "/lan/x", "allbox")
    del reply["lan"]
    check("lan: a header named on the upstream's second Connection line is not returned",
          r.status_code == 200 and "x-up" not in r.headers)
    forwarded = []
    for up, path in (("lan", "/lan/x"), ("anthropic", "/v1/messages")):
        r = await c.get("http://proxy" + path, headers=[
            ("authorization", f"Bearer {KEY['allbox']}"), ("connection", "keep-alive"),
            ("connection", "x-secret"), ("x-secret", "1")])
        forwarded.append(r.status_code == 200 and "x-secret" in seen[up][-1].headers)
    check("lan and Claude path: a header named on the request's second Connection line is not forwarded",
          forwarded == [False, False])


async def main():
    jar = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app), base_url="http://proxy",
                                 cookies=jar) as c:
        await forwarding(c)
        await grants(c)
        await paths(c)
        await cookies(c)
        await connection(c)
        await csp(c)
        await panel(c)
        await reporting(c)
        check("no refresh was attempted", seen["auth"] == [])
        await connection_lines(c)


def load_with(devices):
    d = fixture({"devices": devices})
    old = R.CONFIG_PATH
    R.CONFIG_PATH = d / "config.json"
    try:
        return R.load_config()
    finally:
        R.CONFIG_PATH = old


def providers_with(specs):
    d = fixture({"devices": {}})
    return P.load_providers({"providers": specs}, d, d)


def refuses(load, arg, word):
    try:
        load(arg)
    except SystemExit as exc:
        return word in str(exc)
    return False


cfg = load_with({"x": {"key": "fake-key-x", "providers": ["lan"]}, "y": "fake-key-y"})
check("an object device entry boots with its grant",
      cfg["devices"] == {"x": "fake-key-x", "y": "fake-key-y"} and cfg["device_grants"] == {"x": ["lan"]})
for label, entry, word in (
        ("an object without providers", {"key": "fake-key-x"}, "providers"),
        ("an empty providers list", {"key": "fake-key-x", "providers": []}, "providers"),
        ("a non-string provider", {"key": "fake-key-x", "providers": ["lan", 1]}, "providers"),
        ("providers as a string", {"key": "fake-key-x", "providers": "lan"}, "providers"),
        ("a misspelt \"provider\"", {"key": "fake-key-x", "provider": ["lan"]}, "providers"),
        ("an extra key", {"key": "fake-key-x", "providers": ["lan"], "note": "x"}, "providers"),
        ("an object without a key", {"providers": ["lan"]}, "device keys")):
    check(f"device entry refused: {label}", refuses(load_with, {"x": entry}, word))
check("device entry refused: a key shared by an object and a string",
      refuses(load_with, {"x": {"key": "fake-key-x", "providers": ["lan"]}, "y": "fake-key-x"}, "unique"))


def import_with(devices):
    d = fixture({"devices": devices, "providers": {"lan": LAN_SPEC}})
    env = {**os.environ, "CLAUDE_ROTATE_CONFIG": str(d / "config.json"),
           "CLAUDE_ROTATE_STATE": str(d / "state.json"), "CLAUDE_ROTATE_AUDIT": str(d / "audit.jsonl")}
    return subprocess.run([sys.executable, "-c", "import rotator"], cwd=HERE, env=env,
                          text=True, capture_output=True, timeout=120)


child = import_with({"x": {"key": "fake-key-x", "providers": ["anthropic", "lan"]}})
check("a grant naming configured providers starts", child.returncode == 0)
child = import_with({"x": {"key": "fake-key-x", "providers": ["lan", "nope"]}})
check("a grant naming an unknown provider refuses startup",
      child.returncode != 0 and "unknown provider(s) nope" in child.stderr)

table = providers_with({"lan": LAN_SPEC, "openai": {"kind": "openai-siwc"}})
check("a valid provider table loads",
      {n: p.prefix for n, p in table.items()} == {"lan": "/lan", "openai": "/openai"} and table["lan"].down is None)
without_token = {k: v for k, v in LAN_SPEC.items() if k != "token_file"}
cases = [("a provider named anthropic", {"anthropic": {"kind": "openai-siwc"}}, "provider name"),
         ("an unknown kind", {"lan": LAN_SPEC | {"kind": "bearer"}}, "kind"),
         ("a name that is a reserved prefix", {"docs": {"kind": "openai-siwc"}}, "prefix"),
         ("a duplicate prefix", {"lan": LAN_SPEC, "lan2": LAN_SPEC | {"prefix": "/lan"}}, "prefix"),
         ("a prefix of two segments", {"lan": LAN_SPEC | {"prefix": "/lan/x"}}, "prefix"),
         ("static-bearer with an extra key", {"lan": LAN_SPEC | {"headers": {}}}, "unknown key"),
         ("upstream with user info", {"lan": LAN_SPEC | {"upstream": "https://u:p@lan.example/api"}}, "upstream"),
         ("upstream with a query", {"lan": LAN_SPEC | {"upstream": "https://lan.example/api?x=1"}}, "upstream"),
         ("upstream with a fragment", {"lan": LAN_SPEC | {"upstream": "https://lan.example/api#x"}}, "upstream"),
         ("upstream not http(s)", {"lan": LAN_SPEC | {"upstream": "ftp://lan.example/api"}}, "upstream"),
         ("static-bearer without token_file", {"lan": without_token}, "token_file"),
         ("static-bearer with a blank token_file", {"lan": LAN_SPEC | {"token_file": " "}}, "token_file")]
cases += [(f"reserved prefix {p}", {"lan": LAN_SPEC | {"prefix": p}}, "prefix")
          for p in ("/v1", "/api", "/rotate", "/docs", "/redoc", "/anthropic")]
cases += [(f"openai-siwc with {k}", {"openai": {"kind": "openai-siwc", k: "https://evil.example"}}, "unknown key")
          for k in ("upstream", "auth_url", "credential_file")]
for label, specs, word in cases:
    check(f"provider refused: {label}", refuses(providers_with, specs, word))

asyncio.run(main())
print(f"\nall {PASS} checks passed")
