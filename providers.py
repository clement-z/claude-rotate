"""Upstreams besides the rotated Claude accounts, each under its own path prefix.

static-bearer   a base URL and a token file: the device key is replaced by
                Authorization: Bearer <token>.
openai-siwc     one "Sign in with ChatGPT" credential (plan usage) forwarded to
                api.openai.com. One refresh at a time; new tokens are written to
                disk before they are used; a refused refresh takes the provider
                down until a new sign-in is installed.

Protocol: https://developers.openai.com/siwc/token-sharing-open-source (read 2026-11-17).
Tokens never leave this module except in the upstream Authorization header: not in
logs, audit records, status or error messages.
"""
import asyncio
import codecs
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ANTHROPIC = "anthropic"  # the rotated Claude accounts on the bare root
# First path segments the Claude passthrough, the admin endpoints and FastAPI's own
# pages answer; a provider there would shadow them or be shadowed.
RESERVED = {"v1", "api", "rotate", "docs", "redoc", ANTHROPIC}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
URL_RE = re.compile(r"^https?://[^/?#\s@]+(/[^?#\s]*)?$")  # no userinfo, query or fragment
KEYS = {"static-bearer": {"kind", "prefix", "upstream", "token_file"},
        "openai-siwc": {"kind", "prefix"}}
TIMEOUT = httpx.Timeout(600.0, connect=15.0)
# Pinned: a refresh sends the rotating refresh token, so no config may redirect it.
SIWC_API = "https://api.openai.com"
SIWC_AUTH = "https://auth.openai.com"
SIWC_RESOURCE = "https://api.openai.com/v1"  # the token's audience, fixed by the flow
# Refresh errors that only a new sign-in repairs.
FATAL = {"invalid_grant", "invalid_client", "refresh_token_reused",
         "refresh_token_expired", "refresh_token_invalidated"}
REFRESH_AHEAD_S = 600  # the background loop refreshes this long before expiry
REFRESH_INLINE_S = 60  # below this a request refreshes first
RETRY_S, RETRY_MAX_S = 30, 300  # backoff after a failed refresh, shared by every caller
SSE_LINE_MAX = 8 * 1024 * 1024  # a longer SSE line is skipped for usage, still relayed
# A cookie one device's call receives must not ride along on another device's call.
NO_COOKIES = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


class ProviderDown(Exception):
    """The provider cannot serve now. The message is safe to show the caller."""


class ClientGone(Exception):
    """The caller disconnected before the request was sent or replayed."""


def load_providers(cfg: dict, config_dir: Path, state_dir: Path) -> dict:
    """Validate cfg["providers"] and build one provider object per entry. A broken
    entry stops startup, like the rest of the config; a missing credential only
    marks that provider down."""
    specs = cfg.get("providers", {})
    if not isinstance(specs, dict):
        raise SystemExit("providers must map names to provider objects")
    table, seen = {}, set()
    for name, spec in specs.items():
        if not isinstance(name, str) or not NAME_RE.match(name) or name == ANTHROPIC:
            raise SystemExit(f"provider name {name!r}: use 1-32 lowercase letters, digits, "
                             "'_' or '-'; 'anthropic' names the Claude accounts")
        if not isinstance(spec, dict) or spec.get("kind") not in KEYS:
            raise SystemExit(f"provider '{name}': kind must be static-bearer or openai-siwc")
        unknown = set(spec) - KEYS[spec["kind"]]
        if unknown:
            raise SystemExit(f"provider '{name}': unknown key(s) {', '.join(sorted(unknown))} for "
                             f"{spec['kind']}" + (" (it talks to OpenAI only)" if spec["kind"] == "openai-siwc" else ""))
        prefix = spec.get("prefix", "/" + name)
        segment = prefix[1:] if isinstance(prefix, str) and prefix.startswith("/") else ""
        if not NAME_RE.match(segment) or segment in RESERVED or segment in seen:
            raise SystemExit(f"provider '{name}': prefix must be one unique path segment such as "
                             f"/{name}, and not one of /{', /'.join(sorted(RESERVED))}")
        seen.add(segment)
        spec = dict(spec, prefix="/" + segment)
        if spec["kind"] == "static-bearer":
            if not (isinstance(spec.get("upstream"), str) and URL_RE.match(spec["upstream"])):
                raise SystemExit(f"provider '{name}': upstream must be an http(s) URL without "
                                 "user info, query or fragment")
            if not isinstance(spec.get("token_file"), str) or not spec["token_file"].strip():
                raise SystemExit(f"provider '{name}': static-bearer needs a token_file")
            table[name] = StaticBearer(name, spec, config_dir)
        else:
            table[name] = OpenAISiwc(name, spec, state_dir)
    return table


async def maintain_loop(table: dict) -> None:
    """Refresh credentials ahead of expiry, so requests rarely wait for one."""
    while True:
        for provider in table.values():
            try:
                await provider.maintain()
            except Exception as exc:  # the loop must outlive any one failure
                print(f"warning: provider {provider.name}: maintenance failed "
                      f"({type(exc).__name__})", file=sys.stderr)
        await asyncio.sleep(60)


async def _check(alive) -> None:
    if alive is not None and not await alive():
        raise ClientGone()


class StaticBearer:
    kind = "static-bearer"

    def __init__(self, name: str, spec: dict, config_dir: Path):
        self.name, self.prefix, self.upstream = name, spec["prefix"], spec["upstream"]
        self.client = httpx.AsyncClient(timeout=TIMEOUT, cookies=NO_COOKIES)
        try:
            self._token = (config_dir / spec["token_file"]).read_text().strip()
        except OSError:
            self._token = ""
        self.down = None if self._token else f"token file {spec['token_file']} is missing or empty"

    async def send(self, method: str, url: httpx.URL, headers: dict, body: bytes, alive=None):
        """(streamed upstream response, retries)."""
        if self.down:
            raise ProviderDown(self.down)
        await _check(alive)
        request = self.client.build_request(
            method, url, headers=headers | {"authorization": f"Bearer {self._token}"}, content=body)
        return await self.client.send(request, stream=True), 0

    async def maintain(self) -> None:
        pass

    def status(self) -> dict:
        return {"kind": self.kind, "prefix": self.prefix, "upstream": self.upstream,
                "state": "down" if self.down else "ok", "reason": self.down}


class OpenAISiwc:
    kind = "openai-siwc"
    upstream = SIWC_API

    def __init__(self, name: str, spec: dict, state_dir: Path):
        self.name, self.prefix = name, spec["prefix"]
        # Named after the provider, in a directory of its own: two providers can never
        # hold the same rotating credential, and no other file shares the names.
        self.path = state_dir / "credentials" / f"{name}.json"
        self.marker = self.path.with_suffix(".down")
        self.client = httpx.AsyncClient(timeout=TIMEOUT, cookies=NO_COOKIES)
        self.auth = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0), cookies=NO_COOKIES)
        self.lock = asyncio.Lock()
        self.down = self.unsaved = self.last_error = None
        self.lost = False  # a refresh answer went missing: OpenAI may have rotated the token
        self.refreshes, self.failures, self.retry_at = 0, 0, 0.0
        self.rec = self._load()

    def _load(self) -> dict:
        try:
            rec = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.down = (f"no credential credentials/{self.path.name} in the state directory: "
                         "sign in with siwc_login.py and install it with claude-rotate-admin")
            return {}
        except (OSError, ValueError) as exc:
            self.down = f"credential credentials/{self.path.name} is unreadable ({type(exc).__name__})"
            return {}
        if not usable_record(rec):
            self.down = f"credential credentials/{self.path.name} lacks a token, the client_id or its expiry"
            return {}
        # A refusal is remembered against this credential, so a restart does not serve
        # with it again; installing a new credential clears it.
        try:
            marker = json.loads(self.marker.read_text())
        except (OSError, ValueError):
            marker = None
        if isinstance(marker, dict) and marker.get("generation") == generation(rec):
            self.down = str(marker.get("reason") or "refused earlier")
        elif marker is not None:
            self.marker.unlink(missing_ok=True)
        return rec

    def _remaining(self) -> float:
        return self.rec["expires_at"] - time.time() if self.rec else 0.0

    async def token(self) -> str:
        """A usable access token, refreshing first when it is about to expire."""
        if not self.down and self._remaining() < REFRESH_INLINE_S:
            await self.refresh(self.rec.get("access_token"))
        if self.down:
            raise ProviderDown(self.down)
        if self._remaining() <= 0:
            raise ProviderDown(self._unavailable())
        return self.rec["access_token"]

    def _unavailable(self) -> str:
        wait = earliest_refresh(self.rec) - time.time()
        return ("the access token expired and " + (
            f"OpenAI allows the next refresh in {round(wait)} s" if wait > 0 else
            f"the refresh is failing ({self.last_error or 'no detail'}); next try in "
            f"{max(0, round(self.retry_at - time.time()))} s"))

    async def send(self, method: str, url: httpx.URL, headers: dict, body: bytes, alive=None):
        """(streamed upstream response, retries). An upstream 401 gets one refresh; the
        request is replayed only with a new token, and not for a caller that left."""
        token = await self.token()
        await _check(alive)
        upstream = await self.client.send(self.client.build_request(
            method, url, headers=headers | {"authorization": f"Bearer {token}"}, content=body), stream=True)
        if upstream.status_code != 401:
            return upstream, 0
        await self.refresh(token, force=True)
        if self.down:
            await upstream.aclose()
            raise ProviderDown(self.down)
        if self.rec.get("access_token") == token:
            return upstream, 0  # no replacement: the caller gets OpenAI's 401
        await upstream.aclose()
        await _check(alive)
        token = await self.token()
        return await self.client.send(self.client.build_request(
            method, url, headers=headers | {"authorization": f"Bearer {token}"}, content=body), stream=True), 1

    async def refresh(self, seen: str | None, force: bool = False) -> None:
        """Serialized. A caller that waited on the lock finds the token it saw already
        replaced and returns, so a rotating refresh token is never sent twice. Failed
        attempts set one retry time that every caller honours; routine refreshes also
        wait for OpenAI's earliest_refresh_at, a refresh after a 401 does not."""
        async with self.lock:
            if self.down or not self.rec or self.rec.get("access_token") != seen:
                return
            now = time.time()
            if now < self.retry_at or (not force and now < earliest_refresh(self.rec)):
                return
            if self.unsaved:
                self._save(self.rec)
            try:
                r = await self.auth.post(SIWC_AUTH + "/api/accounts/oauth/token",
                                         headers={"accept": "application/json"}, data={
                    "grant_type": "refresh_token", "client_id": self.rec["client_id"],
                    "refresh_token": self.rec["refresh_token"], "resource": SIWC_RESOURCE})
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                return self._failed(f"token endpoint unreachable ({type(exc).__name__})")
            except httpx.HTTPError as exc:
                # The request may have arrived: OpenAI may have rotated the token.
                self.lost = True
                return self._failed(f"the refresh answer was lost ({type(exc).__name__}); "
                                    "OpenAI may have rotated the token, the next try will tell")
            try:
                data = r.json()
            except ValueError:
                data = None
            if r.status_code != 200:
                code = error_code(data)
                if r.status_code in (400, 401, 403) and code in FATAL:
                    return self._refused(code)
                return self._failed(f"token endpoint answered {r.status_code}" + (f" ({code})" if code else ""))
            rec = record_from(data, self.rec) if isinstance(data, dict) else None
            if rec is None or not usable_record(rec):
                self.lost = True  # a 200 rotates the token even when its body is unusable
                return self._failed("the refresh answer was malformed; OpenAI may have rotated the token")
            # The old refresh token is dead from here on. A crash before the file is
            # renamed in place loses the new one too and needs a new sign-in; that
            # window is a single write and rename. If the write fails, the new tokens
            # still serve from memory, because refusing would not bring the old token
            # back; status says "unsaved" and the save, not the refresh, is retried.
            self._save(rec)
            self.rec, self.last_error, self.lost = rec, None, False
            self.refreshes, self.failures, self.retry_at = self.refreshes + 1, 0, 0.0

    def _failed(self, message: str) -> None:
        self.last_error = message
        self.failures += 1
        self.retry_at = time.time() + min(RETRY_MAX_S, RETRY_S * 2 ** (self.failures - 1))

    def _refused(self, code: str) -> None:
        self.down = (f"the refresh was refused ({code})"
                     + (" after a lost refresh answer, so OpenAI had rotated it" if self.lost else "")
                     + ": sign in again with siwc_login.py and install the new credential")
        try:
            write_private(self.marker, json.dumps({"generation": generation(self.rec), "reason": self.down}))
        except OSError as exc:
            print(f"warning: provider {self.name}: could not record the refusal ({type(exc).__name__})",
                  file=sys.stderr)

    def _save(self, rec: dict) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, exist_ok=True)
            write_private(self.path, json.dumps(rec, indent=2) + "\n")
            self.unsaved = None
        except OSError as exc:
            self.unsaved = (f"the refreshed credential could not be saved ({type(exc).__name__}); "
                            "it serves from memory, and a restart now would need a new sign-in")

    async def maintain(self) -> None:
        if self.down or not self.rec:
            return
        if self.unsaved:
            self._save(self.rec)
        if self._remaining() < REFRESH_AHEAD_S:
            await self.refresh(self.rec["access_token"])

    def status(self) -> dict:
        now = time.time()
        saved = self.rec.get("refresh_saved_at")
        state = ("down" if self.down else "unavailable" if self._remaining() <= 0
                 else "unsaved" if self.unsaved else "ok")
        return {"kind": self.kind, "prefix": self.prefix, "upstream": self.upstream, "state": state,
                "reason": self.down or (self._unavailable() if state == "unavailable" else self.unsaved),
                "last_error": self.last_error,
                "access_token_min": round(self._remaining() / 60) if self.rec else None,
                # Refresh tokens live 30 days from their last rotation.
                "refresh_token_days": round(30 - (now - saved) / 86400, 1)
                if type(saved) in (int, float) else None,
                "refreshes": self.refreshes, "client_id": self.rec.get("client_id"),
                "host_id": self.rec.get("ext_agent_host_id"), "scopes": self.rec.get("scopes")}


def usable_record(rec) -> bool:
    return (isinstance(rec, dict) and type(rec.get("expires_at")) in (int, float)
            and all(isinstance(rec.get(k), str) and rec[k].strip()
                    for k in ("access_token", "refresh_token", "client_id")))


def generation(rec: dict) -> str:
    """Identifies one credential without storing anything that could be replayed."""
    return hashlib.sha256(str(rec.get("refresh_token")).encode()).hexdigest()[:16]


def error_code(data) -> str | None:
    """The OAuth error code of a token-endpoint answer, only if it looks like one."""
    err = data.get("error") if isinstance(data, dict) else None
    code = err.get("code") if isinstance(err, dict) else err
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else None


def record_from(resp: dict, previous: dict) -> dict | None:
    """Credential record from a token response, fields a refresh omits kept; None when
    the expiry is unusable. Same shape as siwc_login.py writes."""
    expires_in = resp.get("expires_in", 3600)
    if type(expires_in) not in (int, float) or expires_in <= 0:
        return None
    now = int(time.time())
    rec = dict(previous)
    for key in ("access_token", "refresh_token", "id_token", "token_type", "expires_in", "earliest_refresh_at"):
        if resp.get(key) is not None:
            rec[key] = resp[key]
    if isinstance(resp.get("scope"), str):
        rec["scopes"] = sorted(resp["scope"].split())
    rec["expires_at"] = now + int(expires_in)
    rec["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["refresh_saved_at"] = now
    return rec


def earliest_refresh(rec: dict) -> int:
    """earliest_refresh_at as Unix seconds, or 0 when absent or unreadable."""
    value = rec.get("earliest_refresh_at")
    if type(value) in (int, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except ValueError:
                return 0
    return 0


def write_private(path: Path, text: str) -> None:
    """Atomic owner-only write. The file it replaces is kept as <name>.prev."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)  # left by a crash of an earlier process with this pid
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            prev = path.with_name(path.name + ".prev")
            prev.unlink(missing_ok=True)
            try:
                os.link(path, prev)  # the live name never goes missing
            except OSError:
                shutil.copyfile(path, prev)
                os.chmod(prev, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def usage_of(event) -> dict | None:
    """Token counts of a Responses, Chat Completions or Anthropic answer or stream
    event, in the audit log's fields (prompt tokens without the cached part)."""
    if not isinstance(event, dict):
        return None
    usage = event.get("usage")
    for holder in ("response", "message"):  # response.completed; Anthropic message_start
        if not isinstance(usage, dict) and isinstance(event.get(holder), dict):
            usage = event[holder].get("usage")
    if not isinstance(usage, dict):
        return None

    def n(value):
        return value if type(value) is int and value >= 0 else 0

    if "prompt_tokens" not in usage and "input_tokens_details" not in usage and (
            "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
            or str(event.get("type", "")).startswith("message_")):
        return {k: n(usage.get(k)) for k in ("input_tokens", "output_tokens",
                                             "cache_read_input_tokens", "cache_creation_input_tokens")}
    prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
    output = usage.get("output_tokens", usage.get("completion_tokens"))
    if type(prompt) is not int and type(output) is not int:
        return None
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
    cached = n(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return {"input_tokens": max(0, n(prompt) - cached), "output_tokens": n(output),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}


class SseUsage:
    """Usage from an SSE stream fed chunk by chunk; lines may span chunks. Counters
    are cumulative, and Anthropic's arrive split (input in message_start, output in
    message_delta), so each keeps the largest value seen."""

    def __init__(self):
        self.decode = codecs.getincrementaldecoder("utf-8")(errors="replace").decode
        self.buf, self.skip, self.usage = "", False, None

    def feed(self, chunk: bytes) -> None:
        lines = (self.buf + self.decode(chunk)).split("\n")
        self.buf = lines.pop()
        for line in lines:
            if self.skip:  # the tail of an oversized line
                self.skip = False
                continue
            if line.startswith("data:") and '"usage"' in line:
                try:
                    usage = usage_of(json.loads(line[5:]))
                except (ValueError, RecursionError):
                    continue
                if usage:
                    self.usage = {k: max(v, (self.usage or {}).get(k, 0)) for k, v in usage.items()}
        if len(self.buf) > SSE_LINE_MAX:
            self.buf, self.skip = "", True
