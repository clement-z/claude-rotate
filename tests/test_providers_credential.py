"""Credential lifecycle of the openai-siwc provider (providers.OpenAISiwc): background,
inline and post-401 refreshes, earliest_refresh_at, the shared backoff, refusals and
lost answers saved in the record, malformed refresh answers, failed saves and the
rotations they hold back, broken credential files, and that no token reaches status,
errors or the saved notes. An in-process
stand-in replaces auth.openai.com and api.openai.com: every refresh rotates both
tokens, a used refresh token is refused as reused. Nothing leaves the process.

  python3 tests/test_providers_credential.py
"""
import asyncio
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import providers as P

PASS = 0
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
URL = httpx.URL("https://api.openai.com/v1/responses")
CLIENT = "app_fake_client"
TOKENS = set()  # every fake token handed out, for the leak checks
SHOWN = {"json.dumps(status())": [], "ProviderDown messages": [], "last_error": [], "the saved notes": []}
TMP = Path(tempfile.mkdtemp())


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


async def no_network(self, request):
    raise AssertionError(f"real network request to {request.url.host}")


httpx.AsyncHTTPTransport.handle_async_request = no_network


class Stand:
    """auth.openai.com and api.openai.com with one current access and refresh token."""

    def __init__(self):
        self.n, self.used, self.refresh = 0, set(), None
        self.rotate()
        self.calls, self.forms, self.disk_at_post = 0, [], []
        self.bearers, self.urls = [], []
        self.script = []  # actions for the next token-endpoint calls
        self.expires_in, self.earliest_in = 3600, None
        self.gate, self.arrived = None, asyncio.Event()  # holds token answers back
        self.hold, self.held, self.release = 0, 0, asyncio.Event()  # holds the first inference calls
        self.watch = None  # credential file read when a refresh arrives

    def mint(self, kind):
        self.n += 1
        token = f"fake-{kind}-{self.n}-{os.urandom(6).hex()}"
        TOKENS.add(token)
        return token

    def rotate(self):
        if self.refresh:
            self.used.add(self.refresh)
        self.access, self.refresh = self.mint("acc"), self.mint("ref")

    def revoke_access(self):
        self.access = self.mint("acc")

    def revoke_refresh(self):
        self.refresh = self.mint("ref")

    def record(self, expires_in=3600, **extra):
        now = int(time.time())
        return {"access_token": self.access, "refresh_token": self.refresh, "client_id": CLIENT,
                "ext_agent_host_id": "urn:uuid:00000000-0000-4000-8000-000000000000",
                "scopes": ["chatgpt.tokens.use.direct", "offline_access"],
                "expires_at": now + expires_in, "refresh_saved_at": now} | extra

    async def token_endpoint(self, req):
        self.calls += 1
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        self.forms.append((req.method, str(req.url), req.headers.get("content-type"), form))
        if self.watch:
            self.disk_at_post.append(json.loads(self.watch.read_text()).get("refresh_token"))
        action = self.script.pop(0) if self.script else None
        if self.gate:
            self.arrived.set()
            await self.gate.wait()
        if action == "connect":
            raise httpx.ConnectError("fake: connection refused", request=req)
        if action == "pool":
            raise httpx.PoolTimeout("fake: no free connection")
        if action == "timeout-before":
            raise httpx.ReadTimeout("fake: timed out", request=req)
        if action == "500":
            return httpx.Response(500, json={"error": "server_error"})
        if isinstance(action, tuple) and action[0] == "error":
            return httpx.Response(action[1], json={"error": action[2]})
        if form.get("grant_type") != "refresh_token" or form.get("client_id") != CLIENT:
            return httpx.Response(400, json={"error": "invalid_request"})
        if form.get("refresh_token") in self.used:
            return httpx.Response(400, json={"error": "refresh_token_reused"})
        if form.get("refresh_token") != self.refresh:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.rotate()
        if action == "timeout-after":
            raise httpx.ReadTimeout("fake: timed out", request=req)
        if isinstance(action, tuple) and action[0] == "raw":
            return httpx.Response(200, content=action[1](self))
        if isinstance(action, tuple) and action[0] == "json":
            return httpx.Response(200, json=action[1](self))
        body = {"access_token": self.access, "refresh_token": self.refresh, "token_type": "Bearer",
                "expires_in": self.expires_in, "scope": "chatgpt.tokens.use.direct offline_access"}
        if self.earliest_in is not None:
            body["earliest_refresh_at"] = int(time.time()) + self.earliest_in
        return httpx.Response(200, json=body)

    async def inference(self, req):
        self.urls.append(str(req.url))
        self.bearers.append(req.headers.get("authorization"))
        if self.held < self.hold:
            self.held += 1
            if self.held == self.hold:
                self.release.set()
            await self.release.wait()
        if req.headers.get("authorization") == f"Bearer {self.access}":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"error": {"code": "invalid_token"}})


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def install(state, content, name="oai"):
    """As claude-rotate-admin install-credential leaves it: credentials/ 700, the file 600."""
    creds = state / "credentials"
    creds.mkdir(mode=0o700, exist_ok=True)
    path = creds / f"{name}.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    os.chmod(path, 0o600)


def start(stand, state, name="oai"):
    prov = P.OpenAISiwc(name, {"prefix": "/openai"}, state)
    prov.auth._transport, prov.auth._mounts = httpx.MockTransport(stand.token_endpoint), {}
    prov.client._transport, prov.client._mounts = httpx.MockTransport(stand.inference), {}
    return prov


def fresh(expires_in=3600, **extra):
    stand, state = Stand(), Path(tempfile.mkdtemp(dir=TMP))
    install(state, stand.record(expires_in, **extra))
    return stand, state, start(stand, state)


def disk(prov):
    return json.loads(prov.path.read_text())


def observe(prov):
    SHOWN["json.dumps(status())"].append(json.dumps(prov.status()))
    SHOWN["last_error"].append(str(prov.last_error))
    if P.NOTES in prov.rec:
        SHOWN["the saved notes"].append(json.dumps(prov.rec[P.NOTES]))


async def down_msg(awaitable):
    """The ProviderDown message, or None when the call succeeded."""
    try:
        result = await awaitable
    except P.ProviderDown as exc:
        SHOWN["ProviderDown messages"].append(str(exc))
        return str(exc)
    if isinstance(result, tuple):
        await result[0].aclose()
    return None


async def send(prov):
    r, retries = await prov.send("POST", URL, {"content-type": "application/json"}, b'{"model": "m"}')
    await r.aread()
    await r.aclose()
    return r.status_code, retries


async def background_refresh():
    stand, state, prov = fresh(expires_in=300)
    old, creds = dict(prov.rec), state / "credentials"
    await prov.maintain()
    check("maintain() with under 600 s left refreshes once",
          stand.calls == 1 and prov.rec["access_token"] == stand.access
          and prov.rec["refresh_token"] == stand.refresh)
    check("the refresh posts the refresh token, client_id and resource as a form to auth.openai.com",
          stand.forms[0] == ("POST", TOKEN_URL, "application/x-www-form-urlencoded", {
              "grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": old["refresh_token"],
              "resource": "https://api.openai.com/v1"}))
    new = disk(prov)
    check("the credential file holds the new generation and keeps the fields a refresh omits",
          new == prov.rec and new["refresh_token"] == stand.refresh and new["expires_at"] >= time.time() + 3590
          and new["ext_agent_host_id"] == old["ext_agent_host_id"] and new["client_id"] == CLIENT)
    prev = creds / "oai.json.prev"
    check("oai.json.prev holds the previous generation", json.loads(prev.read_text()) == old)
    check("the credential and .prev are mode 600, credentials/ 700, no temp file left",
          mode(prov.path) == 0o600 and mode(prev) == 0o600 and mode(creds) == 0o700
          and sorted(p.name for p in creds.iterdir()) == ["oai.json", "oai.json.prev"])
    s = prov.status()
    check("status counts the refresh and reports ok with an hour left",
          s["state"] == "ok" and s["reason"] is None and s["last_error"] is None and s["refreshes"] == 1
          and s["access_token_min"] == 60 and s["refresh_token_days"] == 30.0 and s["client_id"] == CLIENT)
    await prov.maintain()
    check("maintain() with an hour left does not refresh", stand.calls == 1)
    again = start(stand, state)
    check("a restart serves the refreshed credential", again.down is None and await again.token() == stand.access)
    shutil.rmtree(creds)
    prov.rec["expires_at"] = time.time() + 300
    await prov.maintain()
    check("a save recreates a missing credentials/ with mode 700 and the file with mode 600",
          stand.calls == 2 and prov.unsaved is None and mode(creds) == 0o700 and mode(prov.path) == 0o600
          and disk(prov)["refresh_token"] == stand.refresh)
    observe(prov)


async def earliest_refresh():
    stand, state, prov = fresh(expires_in=300, earliest_refresh_at=int(time.time()) + 120)
    await prov.maintain()
    check("maintain() waits for a future earliest_refresh_at", stand.calls == 0)
    prov.rec["expires_at"] = time.time() + 30
    check("an inline token() under 60 s also waits and returns the current token",
          await prov.token() == stand.access and stand.calls == 0)
    prov.rec["expires_at"] = time.time() - 1
    msg = await down_msg(prov.token())
    check("with the token expired, token() raises ProviderDown naming the wait",
          msg is not None and re.search(r"OpenAI allows the next refresh in 1[12]\d s", msg) is not None
          and stand.calls == 0)
    s = prov.status()
    check("status says unavailable with the wait",
          s["state"] == "unavailable" and "OpenAI allows the next refresh in" in s["reason"])
    observe(prov)
    iso = datetime.fromtimestamp(time.time() + 120, timezone.utc).isoformat().replace("+00:00", "Z")
    prov.rec.update(expires_at=time.time() + 300, earliest_refresh_at=iso)
    await prov.maintain()
    check("an ISO 8601 earliest_refresh_at is honoured too", stand.calls == 0)
    prov.rec["expires_at"] = time.time() + 3000
    stand.revoke_access()
    stand.earliest_in = 200
    check("a refresh after a 401 ignores earliest_refresh_at and the request is replayed",
          await send(prov) == (200, 1) and stand.calls == 1)
    prov.rec["expires_at"] = time.time() + 300
    await prov.maintain()
    check("the earliest_refresh_at of a refresh answer is stored and honoured",
          stand.calls == 1 and disk(prov)["earliest_refresh_at"] > time.time() + 190)
    prov.rec["earliest_refresh_at"] = int(time.time()) - 1
    await prov.maintain()
    check("maintain() refreshes once earliest_refresh_at has passed", stand.calls == 2)
    observe(prov)


async def inline_refresh():
    stand, state, prov = fresh(expires_in=30)
    old = stand.access
    tok = await prov.token()
    check("token() with under 60 s left refreshes before returning",
          tok == stand.access != old and stand.calls == 1 and disk(prov)["access_token"] == tok)
    prov.rec["expires_at"] = time.time() + 30
    check("send() with under 60 s left uses the refreshed token on its only call",
          await send(prov) == (200, 0) and stand.calls == 2 and stand.bearers == [f"Bearer {stand.access}"])
    observe(prov)


async def after_401():
    stand, state, prov = fresh()
    old = stand.access
    stand.revoke_access()
    result = await send(prov)
    check("after a remote rotation send() refreshes once and replays with the new token",
          result == (200, 1) and stand.calls == 1 and stand.urls == [str(URL)] * 2
          and stand.bearers == [f"Bearer {old}", f"Bearer {stand.access}"])
    check("the replacement is saved", disk(prov)["access_token"] == stand.access)
    observe(prov)

    stand, state, prov = fresh()
    old = stand.access
    stand.revoke_access()
    stand.hold = 2
    results = await asyncio.gather(send(prov), send(prov))
    check("two concurrent sends that both get 401 cause exactly one refresh",
          stand.calls == 1 and prov.refreshes == 1)
    check("both replay with the new token",
          results == [(200, 1), (200, 1)]
          and stand.bearers == [f"Bearer {old}"] * 2 + [f"Bearer {stand.access}"] * 2)

    # The replacement is short-lived, so the replay waits on a second refresh, during
    # which the caller leaves: no replay may go out.
    stand, state, prov = fresh()
    stand.revoke_access()
    stand.expires_in = 30
    gone = False
    real_endpoint = stand.token_endpoint

    async def endpoint(req):
        nonlocal gone
        if stand.calls == 1:
            gone = True
        return await real_endpoint(req)

    async def alive():
        return not gone

    prov.auth._transport = httpx.MockTransport(endpoint)
    try:
        await prov.send("POST", URL, {}, b"{}", alive)
        raised = False
    except P.ClientGone:
        raised = True
    check("a caller who leaves while the replay waits for its token gets no replay",
          raised and stand.calls == 2 and len(stand.bearers) == 1)

    stand, state, prov = fresh(expires_in=30)
    stand.gate = asyncio.Event()
    tasks = [asyncio.create_task(prov.token()) for _ in range(3)] + [asyncio.create_task(prov.maintain())]
    await stand.arrived.wait()
    for _ in range(10):
        await asyncio.sleep(0)
    in_flight = stand.calls == 1 and prov.lock.locked()
    stand.gate.set()
    tokens = await asyncio.gather(*tasks)
    check("callers queued behind a slow refresh get its token without a second refresh",
          in_flight and stand.calls == 1 and tokens[:3] == [stand.access] * 3)
    observe(prov)


async def failed_refresh():
    stand, state, prov = fresh()
    old = stand.access
    stand.revoke_access()
    stand.script = ["500"]
    t0 = time.time()
    result = await send(prov)
    check("a failed refresh after a 401 hands the caller the 401 without a replay",
          result == (401, 0) and stand.bearers == [f"Bearer {old}"] and stand.calls == 1)
    check("the failure keeps the token, records the error and sets retry_at 30 s ahead",
          prov.rec["access_token"] == old and prov.last_error == "token endpoint answered 500 (server_error)"
          and round(prov.retry_at - t0) == 30 and prov.down is None)
    prov.rec["expires_at"] = time.time() + 30
    tok = await prov.token()
    prov.rec["expires_at"] = time.time() + 300
    await prov.maintain()
    result = await send(prov)
    check("before retry_at, token(), maintain() and another 401 leave the token endpoint alone",
          tok == old and result == (401, 0) and stand.calls == 1)
    prov.rec["expires_at"] = time.time() - 1
    msg = await down_msg(prov.token())
    check("an expired token during the backoff raises ProviderDown with the error and the wait",
          msg is not None and "the refresh is failing (token endpoint answered 500" in msg
          and re.search(r"next try in (29|30) s", msg) is not None and stand.calls == 1)
    s = prov.status()
    check("status says unavailable with the failing refresh", s["state"] == "unavailable" and s["reason"] == msg)
    observe(prov)
    prov.rec["expires_at"] = time.time() + 300
    waits = [30]
    for _ in range(5):
        prov.retry_at, stand.script = 0.0, ["500"]
        t0 = time.time()
        await prov.maintain()
        waits.append(round(prov.retry_at - t0))
    check("the backoff doubles from 30 s and stops at 300 s",
          waits == [30, 60, 120, 240, 300, 300] and stand.calls == 6)
    prov.retry_at = 0.0
    await prov.maintain()
    check("a successful refresh clears the error, the failure count and retry_at",
          stand.calls == 7 and prov.last_error is None and prov.failures == 0 and prov.retry_at == 0.0
          and await send(prov) == (200, 0))
    observe(prov)


async def refusal():
    for code, provoke in (("invalid_grant", Stand.revoke_refresh), ("refresh_token_reused", Stand.rotate)):
        stand, state, prov = fresh(expires_in=300)
        refused = dict(prov.rec)
        provoke(stand)
        await prov.maintain()
        check(f"{code}: the provider goes down",
              prov.down is not None and f"refused ({code})" in prov.down and stand.calls == 1)
        msg = await down_msg(prov.send("POST", URL, {}, b"{}"))
        check(f"{code}: send() raises ProviderDown with the reason and sends nothing",
              msg == prov.down and stand.bearers == [])
        s = prov.status()
        check(f"{code}: status is down with the reason", s["state"] == "down" and s["reason"] == prov.down)
        on_disk = disk(prov)
        check(f"{code}: the refusal is saved in the refused record itself, mode 600",
              on_disk[P.NOTES] == {"refused": prov.down} and on_disk["refresh_token"] == refused["refresh_token"]
              and mode(prov.path) == 0o600 and prov.unsaved is None
              and sorted(x.name for x in (state / "credentials").iterdir()) == ["oai.json", "oai.json.prev"])
        observe(prov)
        await prov.maintain()
        check(f"{code}: a down provider leaves the token endpoint alone", stand.calls == 1)
        again = start(stand, state)
        check(f"{code}: a restart on the same state stays down",
              again.down == prov.down and again.status()["state"] == "down")
        stand.rotate()
        install(state, stand.record())
        again = start(stand, state)
        check(f"{code}: a new credential and a restart give ok, the refusal gone with the old record",
              again.down is None and again.status()["state"] == "ok" and P.NOTES not in again.rec
              and await send(again) == (200, 0))

    stand, state, prov = fresh(expires_in=300)
    stand.script = [("error", 401, {"code": "refresh_token_expired"})]
    await prov.maintain()
    check("a fatal code nested as error.code in a 401 also takes the provider down",
          prov.down is not None and "refused (refresh_token_expired)" in prov.down and P.NOTES in disk(prov))
    observe(prov)

    stand, state, prov = fresh(expires_in=300)
    stand.script = [("error", 400, "slow_down")]
    t0 = time.time()
    await prov.maintain()
    check("a non-fatal 400 (slow_down) is a failure with backoff, not down",
          prov.down is None and prov.last_error == "token endpoint answered 400 (slow_down)"
          and round(prov.retry_at - t0) == 30 and P.NOTES not in disk(prov) and prov.status()["state"] == "ok")
    observe(prov)


async def lost_answer():
    stand, state, prov = fresh(expires_in=300)
    rec, raw = dict(prov.rec), prov.path.read_bytes()
    stand.script = ["timeout-after"]
    t0 = time.time()
    await prov.maintain()
    check("a refresh answer lost after the remote rotation sets lost and says so",
          prov.lost is True and stand.refresh != rec["refresh_token"]
          and prov.last_error.startswith("the refresh answer was lost (ReadTimeout)"))
    tokens = lambda r: (r["access_token"], r["refresh_token"])
    check("the lost refresh keeps the tokens in memory and on disk and sets retry_at",
          tokens(prov.rec) == tokens(rec) == tokens(disk(prov)) and round(prov.retry_at - t0) == 30
          and prov.down is None)
    check("the lost answer and its backoff are saved in the record",
          disk(prov)[P.NOTES] == {"lost": True, "retry_at": prov.retry_at, "failures": 1} and prov.unsaved is None)
    observe(prov)
    await prov.maintain()
    check("before retry_at the lost refresh is not retried", stand.calls == 1)
    again = start(stand, state)
    await again.maintain()
    check("a restart keeps the lost answer and the backoff, and does not try at once",
          again.lost is True and again.retry_at == prov.retry_at and again.failures == 1 and again.down is None
          and "before the last restart" in again.last_error and stand.calls == 1)
    observe(again)
    prov.retry_at = 0.0
    await prov.maintain()
    check("after retry_at the old refresh token is sent, refused as reused, and the reason names the lost answer",
          stand.calls == 2 and stand.forms[-1][3]["refresh_token"] == rec["refresh_token"]
          and prov.down is not None and "refused (refresh_token_reused)" in prov.down
          and "lost refresh answer" in prov.down)
    observe(prov)

    stand, state, prov = fresh(expires_in=300)
    stand.script = ["timeout-before"]
    await prov.maintain()
    lost = prov.lost
    prov.retry_at = 0.0
    await prov.maintain()
    check("an answer lost before any rotation is followed by a normal refresh that clears lost",
          lost is True and prov.lost is False and prov.down is None and stand.calls == 2
          and prov.rec["access_token"] == stand.access and P.NOTES not in disk(prov))

    stand, state, prov = fresh(expires_in=300)
    rec = dict(prov.rec)
    stand.script = ["connect"]
    t0 = time.time()
    await prov.maintain()
    check("a ConnectError is a failure with lost staying False",
          prov.lost is False and prov.last_error == "token endpoint unreachable (ConnectError)"
          and prov.rec == rec and round(prov.retry_at - t0) == 30 and prov.down is None and stand.n == 2
          and P.NOTES not in disk(prov))
    observe(prov)

    stand, state, prov = fresh(expires_in=300)
    stand.script = ["pool"]
    await prov.maintain()
    check("a PoolTimeout (the request never left) is a failure with lost staying False",
          prov.lost is False and prov.last_error == "token endpoint unreachable (PoolTimeout)"
          and P.NOTES not in disk(prov))


async def malformed(variants):
    for label, action in variants:
        stand, state, prov = fresh(expires_in=300)
        rec = dict(prov.rec)
        stand.script = [action]
        try:
            await prov.maintain()
            error = None
        except Exception as exc:
            print(f"     {type(exc).__name__}: {exc}")
            error = exc
        check(f"a 200 with {label}: down, nothing published, the spent record marked refused on disk",
              error is None and stand.calls == 1 and prov.down is not None
              and "OpenAI accepted the refresh" in prov.down and prov.refreshes == 0
              and {k: v for k, v in prov.rec.items() if k != P.NOTES} == rec
              and disk(prov) == prov.rec and disk(prov)[P.NOTES] == {"refused": prov.down})
        observe(prov)
    await prov.maintain()
    again = start(stand, state)
    check("after a malformed answer nothing more is sent and a restart stays down",
          stand.calls == 1 and again.down == prov.down)
    observe(prov)


async def odd_earliest():
    stand, state, prov = fresh(expires_in=300)
    stand.script = [("raw", lambda s: json.dumps(tokens_with()(s)).replace(
        '"expires_in": 3600', '"expires_in": 3600, "earliest_refresh_at": NaN').encode())]
    try:
        await prov.maintain()
        state_now = prov.status()["state"]
        prov.rec["expires_at"] = time.time() + 300
        await prov.maintain()
        error = None
    except Exception as exc:
        print(f"     {type(exc).__name__}: {exc}")
        error = exc
    check("a NaN earliest_refresh_at is read as absent: no exception, the next refresh is not blocked",
          error is None and state_now == "ok" and stand.calls == 2 and prov.refreshes == 2)

    stand, state, prov = fresh(earliest_refresh_at=int(time.time()) + 3000)
    stand.revoke_access()
    check("an answer without earliest_refresh_at drops the one of the token it replaced",
          await send(prov) == (200, 1) and "earliest_refresh_at" not in prov.rec
          and "earliest_refresh_at" not in disk(prov))
    observe(prov)


def tokens_with(**fields):
    return lambda s: {"access_token": s.access, "refresh_token": s.refresh, "expires_in": 3600} | fields


async def failed_save():
    stand, state, prov = fresh(expires_in=300)
    rec, raw, creds = dict(prov.rec), prov.path.read_bytes(), state / "credentials"

    def no_space(fd):
        raise OSError(28, "No space left on device")

    real_fsync = os.fsync
    P.os.fsync = no_space
    try:
        await prov.maintain()
    finally:
        P.os.fsync = real_fsync
    check("a failing fsync during the save still serves the new tokens from memory",
          stand.calls == 1 and prov.rec["access_token"] == stand.access and await prov.token() == stand.access
          and await send(prov) == (200, 0) and stand.bearers == [f"Bearer {stand.access}"])
    s = prov.status()
    check("status keeps availability and durability apart: ok, with unsaved giving the reason",
          s["state"] == "ok" and s["reason"] is None and s["unsaved"] == prov.unsaved
          and "could not be written to disk (OSError)" in s["unsaved"])
    check("no temp file is left and the live file is still the old valid record",
          sorted(p.name for p in creds.iterdir()) == ["oai.json"] and prov.path.read_bytes() == raw
          and P.usable_record(json.loads(raw)))
    observe(prov)
    await prov.maintain()
    check("maintain() retries the save without a second refresh and clears unsaved",
          stand.calls == 1 and prov.unsaved is None and disk(prov) == prov.rec and prov.status()["unsaved"] is None
          and json.loads((creds / "oai.json.prev").read_text()) == rec)

    stand, state, prov = fresh(expires_in=300)
    real_write = P.write_private

    def broken(path, text):
        raise OSError(5, "Input/output error")

    P.write_private = broken
    try:
        await prov.maintain()
        in_memory = prov.rec["refresh_token"]
        check("write_private raising OSError leaves the provider unsaved on the new tokens",
              prov.unsaved is not None and in_memory == stand.refresh and disk(prov)["refresh_token"] != in_memory)
        observe(prov)
        prov.rec["expires_at"] = time.time() + 30
        tok = await prov.token()
        await prov.maintain()
        check("while the disk keeps failing, neither token() nor maintain() rotates again",
              tok == stand.access and stand.calls == 1 and prov.unsaved is not None)
        stand.revoke_access()
        check("nor does a 401: the caller gets OpenAI's 401", await send(prov) == (401, 0) and stand.calls == 1)
        stand.access = tok
        prov.rec["expires_at"] = time.time() - 1
        msg = await down_msg(prov.token())
        s = prov.status()
        check("once the token in memory expires, requests get 503 naming the unsaved credential",
              msg is not None and "no refresh is made until the last one is on disk" in msg
              and s["state"] == "unavailable" and s["unsaved"] == prov.unsaved and stand.calls == 1)
        observe(prov)
    finally:
        P.write_private = real_write
    stand.watch = prov.path
    prov.rec["expires_at"] = time.time() + 30
    tok = await prov.token()
    check("once the disk works, the refresh first saves the credential in memory, then posts it",
          stand.calls == 2 and stand.disk_at_post == [in_memory] and stand.forms[-1][3]["refresh_token"] == in_memory
          and tok == stand.access and disk(prov)["access_token"] == tok and prov.unsaved is None)
    observe(prov)

    # A refusal while nothing can be written: down at once, saved once the disk works.
    stand, state, prov = fresh(expires_in=300)
    stand.revoke_refresh()
    P.write_private = broken
    try:
        await prov.maintain()
        s = prov.status()
        check("a refusal with a failing disk is down and unsaved at once",
              prov.down is not None and s["state"] == "down" and s["unsaved"] is not None and stand.calls == 1)
        again = start(stand, state)
        check("a restart before the refusal reaches the disk does not know it",
              again.down is None and P.NOTES not in disk(again))
        await again.maintain()
        check("its next refresh is refused again and it goes down", again.down is not None and stand.calls == 2)
        await prov.maintain()
        check("maintain() keeps retrying the save while down", prov.unsaved is not None and stand.calls == 2)
    finally:
        P.write_private = real_write
    await prov.maintain()
    again = start(stand, state)
    check("once the disk works the refusal is saved and a restart stays down",
          prov.unsaved is None and disk(prov)[P.NOTES]["refused"] == prov.down and again.down == prov.down
          and stand.calls == 2)

    # The directory sync fails after the rename: the file is in place, its entry maybe not durable.
    stand, state, prov = fresh(expires_in=300)

    def dir_sync_fails(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        return real_fsync(fd)

    P.os.fsync = dir_sync_fails
    try:
        await prov.maintain()
        live = disk(prov)
        prov.rec["expires_at"] = time.time() + 30
        await prov.token()
        check("a directory sync failing after the rename: the new file is in place, status says unsaved, "
              "no further rotation", live["refresh_token"] == stand.refresh == prov.rec["refresh_token"]
              and prov.unsaved is not None and stand.calls == 1)
    finally:
        P.os.fsync = real_fsync
    stand.watch = prov.path
    await prov.maintain()
    check("maintain() then saves with a working sync and only after that runs the refresh held back",
          prov.unsaved is None and stand.calls == 2 and stand.disk_at_post == [live["refresh_token"]]
          and disk(prov)["refresh_token"] == stand.refresh)


async def broken_files(cases):
    for label, content, expected in cases:
        stand, state = Stand(), Path(tempfile.mkdtemp(dir=TMP))
        (state / "credentials").mkdir(mode=0o700)
        if content is not None:
            install(state, content(stand))
        try:
            prov = start(stand, state)
            msg = await down_msg(prov.token())
            await prov.maintain()
            s = prov.status()
            observe(prov)
            ok = (prov.down is not None and expected in prov.down and msg == prov.down
                  and s["state"] == "down" and s["reason"] == prov.down)
        except Exception as exc:
            print(f"     {type(exc).__name__}: {exc}")
            ok = False
        check(f"{label}: down with a message, no exception", ok and stand.calls == 0)


async def main():
    await background_refresh()
    await earliest_refresh()
    await inline_refresh()
    await after_401()
    await failed_refresh()
    await refusal()
    await lost_answer()
    await malformed([
        ("a non-JSON body", ("raw", lambda s: b"<html>upstream error</html>")),
        ("empty-string tokens", ("json", tokens_with(access_token="", refresh_token=""))),
        ("expires_in 0", ("json", tokens_with(expires_in=0))),
        ("a negative expires_in", ("json", tokens_with(expires_in=-60))),
        ("expires_in as a string", ("json", tokens_with(expires_in="3600"))),
        ("JSON without access_token", ("json", lambda s: {"refresh_token": s.refresh, "expires_in": 3600})),
        ("a null access_token", ("json", tokens_with(access_token=None))),
        ("JSON without refresh_token", ("json", lambda s: {"access_token": s.access, "expires_in": 3600})),
        ("a null refresh_token", ("json", tokens_with(refresh_token=None))),
        ("a blank refresh_token", ("json", tokens_with(refresh_token="  "))),
        ("a sub-second expires_in", ("json", tokens_with(expires_in=0.5))),
        ("a 400-digit expires_in", ("json", tokens_with(expires_in=10 ** 400))),
        ("an expires_in beyond 30 days", ("json", tokens_with(expires_in=31 * 86400))),
        ("expires_in NaN", ("raw", lambda s: json.dumps(tokens_with()(s)).replace(
            '"expires_in": 3600', '"expires_in": NaN').encode())),
        ("expires_in Infinity", ("raw", lambda s: json.dumps(tokens_with()(s)).replace(
            '"expires_in": 3600', '"expires_in": Infinity').encode())),
    ])
    await odd_earliest()
    await failed_save()
    lacks = "lacks a token, the client_id or its expiry"
    await broken_files([
        ("a missing file", None, "no credential credentials/oai.json in the state directory"),
        ("unreadable JSON", lambda s: "{not json", "credentials/oai.json is unreadable (JSONDecodeError)"),
        ("a record lacking client_id", lambda s: {k: v for k, v in s.record().items() if k != "client_id"}, lacks),
        ("empty-string tokens", lambda s: s.record(access_token="", refresh_token=""), lacks),
        ("a non-numeric expires_at", lambda s: s.record(expires_at="tomorrow"), lacks),
        ("a 400-digit expires_at", lambda s: s.record(expires_at=10 ** 400), lacks),
        ("a NaN expires_at", lambda s: json.dumps(s.record(expires_at=0)).replace(
            '"expires_at": 0', '"expires_at": NaN'), lacks),
    ])
    for where, texts in SHOWN.items():
        check(f"no fake token appears in {where} ({len(texts)} samples)",
              texts and not any(t in text for text in texts for t in TOKENS))


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {PASS} checks passed")
