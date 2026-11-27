"""Upstreams besides the rotated Claude accounts, each under its own path prefix.

static-bearer   a base URL and a token file: the device key is replaced by
                Authorization: Bearer <token>.
openai-siwc     one "Sign in with ChatGPT" credential (plan usage) forwarded to
                api.openai.com. One refresh at a time; new tokens are written to
                disk before they are used, and no further refresh is made until
                they are; a refused refresh takes the provider down until a new
                sign-in is installed. With "images": true, Codex's two image
                requests go to the ChatGPT backend instead, with a Codex login
                (CodexLogin). Codex's web search uses that login too.

Protocol: https://developers.openai.com/siwc/token-sharing-open-source (read 2026-11-17).
Tokens never leave this module except in the upstream Authorization header: not in
logs, audit records, status or error messages.
"""
import asyncio
import base64
import codecs
import http.cookiejar
import json
import os
import re
import shutil
import socket
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
        "openai-siwc": {"kind", "prefix", "images"}}
TIMEOUT = httpx.Timeout(600.0, connect=15.0)
# Pinned: a refresh sends the rotating refresh token, so no config may redirect it.
SIWC_API = "https://api.openai.com"
SIWC_AUTH = "https://auth.openai.com"
SIWC_RESOURCE = "https://api.openai.com/v1"  # the token's audience, fixed by the flow
# The ChatGPT backend Codex itself uses with a ChatGPT login. The routes are pinned.
CODEX_BACKEND = "https://chatgpt.com/backend-api/codex"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Codex CLI's public OAuth client
IMAGE_PATHS = {"/v1/images/generations": "/images/generations", "/v1/images/edits": "/images/edits"}
SEARCH_ENDPOINT = "/alpha/search"
SEARCH_PATH = "/v1" + SEARCH_ENDPOINT
# The gateway adds the login's bearer and account id.
CODEX_HEADERS = ("content-type", "originator", "user-agent")
IMAGE_HEADERS = CODEX_HEADERS + ("x-codex-image-turn-id",)
SEARCH_HEADERS = CODEX_HEADERS + ("x-codex-turn-metadata", "accept")
# Refresh refusals Codex treats as permanent for its login (and any 401).
CODEX_PERMANENT = {"invalid_grant", "refresh_token_expired", "refresh_token_reused",
                   "refresh_token_invalidated"}
CODEX_REFRESH_FALLBACK_S = 8 * 86400  # Codex's age rule, for a token without a readable exp
# Where Codex reads the plan's usage windows (5 h and weekly) with a ChatGPT login: a read,
# no model call. The Sign in with ChatGPT route carries no quota headers on /responses.
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_POLL_S = 300
HISTORY_DAYS = 14
QUOTA_BUCKET_S = 300
QUOTA_MAX_SERIES = 16
QUOTA_MAX_POINTS = HISTORY_DAYS * 86400 // QUOTA_BUCKET_S + 1
QUOTA_SOURCES = {"usage", "images", "inference"}
QUOTA_ID = re.compile(r"[a-z0-9_]{1,48}")
QUOTA_RESET_MAX = 253402300800  # the end of year 9999
# Refresh errors that only a new sign-in repairs.
FATAL = {"invalid_grant", "invalid_client", "refresh_token_reused",
         "refresh_token_expired", "refresh_token_invalidated"}
REFRESH_AHEAD_S = 600  # the background loop refreshes this long before expiry
REFRESH_INLINE_S = 60  # below this a request refreshes first
RETRY_S, RETRY_MAX_S = 30, 300  # backoff after a failed refresh, shared by every caller
# A 401 on a token believed valid gets a refresh only this long after the last refresh
# of any kind. A revoked or rotated credential is repaired by the first one; a second
# 401 soon after is not about the token, and no run of client requests may rotate it
# more often (each rotation risks a lost answer, and spends a refresh early).
FORCED_REFRESH_MIN_S = 600
PEEK_MAX = 64 * 1024  # how much of a 401 body is read to classify it
# OpenAI's 401 for a valid credential asking for an operation the route does not allow.
REFUSED_OPERATION = {"rejected_by_hardened_oauth_boundary"}
SSE_LINE_MAX = 8 * 1024 * 1024  # a longer SSE line is skipped for usage, still relayed
# Events after which a stream has nothing more to say: a client may hang up once it
# has one (Codex does, after response.completed) without having cut the answer short.
# Responses: completed, failed, incomplete, error; Anthropic: message_stop, error;
# Chat Completions: data: [DONE].
TERMINAL = {"response.completed", "response.failed", "response.incomplete", "message_stop", "error"}
# A cookie one device's call receives must not ride along on another device's call.
NO_COOKIES = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


class ProviderDown(Exception):
    """The provider cannot serve now. The message is safe to show the caller."""

    def __init__(self, message, notes=None):
        super().__init__(message)
        self.notes = notes or {}


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
        if spec["kind"] == "openai-siwc" and "credential_file" in unknown:
            raise SystemExit(f"provider '{name}': credential_file is gone; the credential lives at "
                             f"credentials/{name}.json in the state directory (see OPERATIONS.md, "
                             "\"Moving a credential from an earlier build\")")
        if unknown:
            raise SystemExit(f"provider '{name}': unknown key(s) {', '.join(sorted(unknown))} for "
                             f"{spec['kind']}" + (" (it talks to OpenAI only)" if spec["kind"] == "openai-siwc" else ""))
        prefix = spec.get("prefix", "/" + name)
        segment = prefix[1:] if isinstance(prefix, str) and prefix.startswith("/") else ""
        if not NAME_RE.match(segment) or segment in RESERVED or segment in seen:
            raise SystemExit(f"provider '{name}': prefix must be one unique path segment such as "
                             f"/{name}, and not one of /{', /'.join(sorted(RESERVED))}")
        seen.add(segment)
        if "images" in spec and type(spec["images"]) is not bool:
            raise SystemExit(f"provider '{name}': images must be true or false")
        spec = dict(spec, prefix="/" + segment)
        if spec["kind"] == "static-bearer":
            if not (isinstance(spec.get("upstream"), str) and URL_RE.match(spec["upstream"])
                    and usable_origin(spec["upstream"])):
                raise SystemExit(f"provider '{name}': upstream must be an http(s) URL with a host, "
                                 "an optional port from 1 to 65535, and no user info, query or fragment")
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


def usable_origin(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return False
    return bool(parsed.host) and (parsed.port is None or 0 < parsed.port < 65536)


def connect_failure(exc: Exception) -> str:
    """What a connect error says: a name that did not resolve shows as a gaierror
    under httpx's ConnectError. A timeout covers the lookup and the TCP connect
    together, so it cannot say which of them hung."""
    cause, depth = exc, 0
    while cause is not None and depth < 8:
        if isinstance(cause, socket.gaierror):
            return f"its name did not resolve ({cause.strerror or 'gaierror'})"
        cause, depth = cause.__cause__ or cause.__context__, depth + 1
    if isinstance(exc, httpx.ConnectTimeout):
        return "no connection in time (ConnectTimeout: the name lookup or the TCP connect hung)"
    return type(exc).__name__


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
        """(streamed upstream response, retries, audit notes). A 401 changes nothing here:
        the token is static."""
        if self.down:
            raise ProviderDown(self.down)
        await _check(alive)
        request = self.client.build_request(
            method, url, headers=headers | {"authorization": f"Bearer {self._token}"}, content=body)
        return await self.client.send(request, stream=True), 0, {}

    async def maintain(self) -> None:
        pass

    def status(self) -> dict:
        return {"kind": self.kind, "prefix": self.prefix, "upstream": self.upstream,
                "state": "down" if self.down else "ok", "reason": self.down}


class QuotaHistory:
    def __init__(self):
        self.series: dict = {}  # (limit, minutes) -> UTC bucket -> observation
        self.dirty = False

    def add(self, limit, minutes, point) -> None:
        seen, used, reset, source = point
        if not (isinstance(limit, str) and QUOTA_ID.fullmatch(limit)
                and type(minutes) is int and 0 < minutes < 1e15
                and number(seen) and seen > 0 and number(used) and 0 <= used <= 100
                and (reset is None or number(reset) and 0 < reset < QUOTA_RESET_MAX)
                and isinstance(source, str) and source in QUOTA_SOURCES):
            return
        bucket = int(seen // QUOTA_BUCKET_S)
        points = self.series.setdefault((limit, minutes), {})
        prior = points.get(bucket)
        if prior is None or seen >= prior[0]:
            points[bucket] = point
            if len(points) > QUOTA_MAX_POINTS:
                del points[min(points)]
            self.dirty = True

    def cap_series(self) -> None:
        excess = len(self.series) - QUOTA_MAX_SERIES
        if excess > 0:
            oldest = sorted(self.series, key=lambda key: (
                max(self.series[key].values())[0], len(self.series[key]), key))
            for key in oldest[:excess]:
                del self.series[key]
            self.dirty = True

    def prune(self, now: float) -> None:
        cutoff = now - HISTORY_DAYS * 86400
        for key, points in list(self.series.items()):
            for bucket, point in list(points.items()):
                if not cutoff <= point[0] <= now:
                    del points[bucket]
                    self.dirty = True
            if not points:
                del self.series[key]

    def snapshot(self, now: float) -> list:
        out = []
        cutoff = now - HISTORY_DAYS * 86400
        for (limit, minutes), buckets in sorted(self.series.items()):
            points = [p for p in sorted(buckets.values()) if cutoff <= p[0] <= now]
            if points:
                out.append({"limit": limit, "minutes": minutes, "points": points})
        return out


class OpenAISiwc:
    kind = "openai-siwc"
    upstream = SIWC_API
    suffix = ".json"
    how_to_get = "sign in with siwc_login.py and install it with claude-rotate-admin"
    how_to_renew = "sign in again with siwc_login.py and install the new credential"

    def __init__(self, name: str, spec: dict, state_dir: Path):
        self.name, self.prefix = name, spec["prefix"]
        # Named after the provider, in a directory of its own: two providers can never
        # hold the same rotating credential, and no other file shares the names.
        self.path = state_dir / "credentials" / f"{name}{self.suffix}"
        self.client = httpx.AsyncClient(timeout=TIMEOUT, cookies=NO_COOKIES)
        self.auth = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0), cookies=NO_COOKIES)
        self.lock = asyncio.Lock()
        self.down = self.unsaved = self.last_error = None
        self.lost = False  # a refresh answer went missing: OpenAI may have rotated the token
        self.refreshes, self.failures, self.retry_at = 0, 0, 0.0
        self.refreshed_at = 0.0  # when the token endpoint was last asked, for any reason
        self.quotas: dict = {}  # quota headers last seen on its answers, by limit id
        self.quota_history = QuotaHistory()
        self.rec = self._load()
        self.images = CodexLogin(name, state_dir) if spec.get("images") is True else None

    def _usable(self, rec) -> bool:
        return usable_record(rec)

    def _load(self) -> dict:
        try:
            rec = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.down = f"no credential credentials/{self.path.name} in the state directory: {self.how_to_get}"
            return {}
        except (OSError, ValueError) as exc:
            self.down = f"credential credentials/{self.path.name} is unreadable ({type(exc).__name__})"
            return {}
        if not self._usable(rec):
            self.down = f"credential credentials/{self.path.name} lacks a token, the client_id or its expiry"
            return {}
        # What the gateway learnt about this credential is saved in the record itself, so
        # a restart keeps it and installing a new credential clears it.
        notes = rec.get(NOTES) if isinstance(rec.get(NOTES), dict) else {}
        if isinstance(notes.get("refused"), str):
            self.down = notes["refused"]
        elif notes.get("lost") is True:
            self.lost = True
            self.last_error = ("a refresh answer was lost before the last restart; OpenAI may have "
                               "rotated the token, the next try will tell")
            if number(notes.get("retry_at")):
                self.retry_at = float(notes["retry_at"])
            if type(notes.get("failures")) is int and notes["failures"] > 0:
                self.failures = notes["failures"]
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
        if self.unsaved:
            return ("the access token expired and no refresh is made until the last one is on disk: "
                    + self.unsaved)
        wait = earliest_refresh(self.rec) - time.time()
        return ("the access token expired and " + (
            f"OpenAI allows the next refresh in {round(wait)} s" if wait > 0 else
            f"the refresh is failing ({self.last_error or 'no detail'}); next try in "
            f"{max(0, round(self.retry_at - time.time()))} s"))

    async def send(self, method: str, url: httpx.URL, headers: dict, body: bytes, alive=None):
        """(streamed upstream response, retries, audit notes). An upstream 401 gets one
        refresh, unless it says the operation is not allowed or a refresh ran less than
        FORCED_REFRESH_MIN_S ago; the request is replayed only with a new token, and not
        for a caller that left. The caller gets any 401 byte for byte."""
        token = await self.token()
        await _check(alive)
        upstream = await self.client.send(self.client.build_request(
            method, url, headers=headers | self._auth(token), content=body), stream=True)
        if upstream.status_code != 401:
            return upstream, 0, {}
        upstream, answer = await peek(upstream)
        kind = error_type(answer)
        notes = {"upstream_error": kind} if kind else {}
        if kind in REFUSED_OPERATION:
            return upstream, 0, notes | {"refresh_after_401": "skipped: operation refused"}
        asked = self.refreshed_at
        await self.refresh(token, force=True)  # waits for one in flight, which may be enough
        notes["refresh_after_401"] = (
            "attempted" if self.refreshed_at != asked
            else "held back: a refresh ran less than 10 min ago" if time.time() - asked < FORCED_REFRESH_MIN_S
            else "not attempted: the refresh is backing off or the credential is not on disk")
        if self.down:
            await upstream.aclose()
            raise ProviderDown(self.down, notes | {"upstream_status": 401})
        if self.rec.get("access_token") == token:
            return upstream, 0, notes  # no replacement: the caller gets OpenAI's 401
        await upstream.aclose()
        token = await self.token()
        await _check(alive)  # last, after every wait
        return await self.client.send(self.client.build_request(
            method, url, headers=headers | self._auth(token), content=body), stream=True), 1, notes

    def _auth(self, token: str) -> dict:
        return {"authorization": f"Bearer {token}"}

    async def refresh(self, seen: str | None, force: bool = False) -> None:
        """Serialized. A caller that waited on the lock finds the token it saw already
        replaced and returns, so a rotating refresh token is never sent twice. Failed
        attempts set one retry time that every caller honours; routine refreshes also
        wait for OpenAI's earliest_refresh_at, a refresh after a 401 does not, but it
        waits FORCED_REFRESH_MIN_S after the last refresh of any kind."""
        async with self.lock:
            if self.down or not self.rec or self.rec.get("access_token") != seen:
                return
            if self.unsaved:
                # Another rotation would spend the refresh token in memory while the disk
                # still holds a spent one: retry the save, rotate only once it is on disk.
                self._save(self.rec)
                if self.unsaved:
                    return
            now = time.time()
            if now < self.retry_at or (not force and now < earliest_refresh(self.rec)):
                return
            if force and now - self.refreshed_at < FORCED_REFRESH_MIN_S:
                return  # the floor on refreshes that requests can cause
            self.refreshed_at = now
            try:
                r = await self._token_request(force)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                return self._failed(f"token endpoint unreachable: {connect_failure(exc)}")
            except httpx.HTTPError as exc:
                # The request may have arrived and rotated the token. The next try sends the
                # same refresh token once more: if OpenAI did rotate it, that try is refused
                # as reused and the provider goes down, where a new sign-in is needed anyway.
                return self._failed(f"the refresh answer was lost ({type(exc).__name__}); "
                                    "OpenAI may have rotated the token, the next try will tell", lost=True)
            try:
                data = r.json()
            except ValueError:
                data = None
            if r.status_code != 200:
                code = error_code(data)
                if self._permanent(r.status_code, code):
                    return self._refused(f"the refresh was refused ({code or r.status_code})" + (
                        " after a lost refresh answer, so OpenAI had rotated it" if self.lost else ""))
                return self._failed(f"token endpoint answered {r.status_code}" + (f" ({code})" if code else ""))
            rec = self._record_from(data) if isinstance(data, dict) else None
            if rec is None:
                # A 200 means the old refresh token is spent; its replacement is unusable.
                return self._refused("OpenAI accepted the refresh but its answer lacks a usable access "
                                     "token, refresh token or expiry, so the credential on disk is spent")
            # The old refresh token is dead from here on. A crash before the file is
            # renamed in place loses the new one too and needs a new sign-in; that
            # window is a single write and rename. If the write fails, the new tokens
            # still serve from memory, because refusing would not bring the old token
            # back; status says "unsaved", the save is retried and nothing else rotates.
            same = rec["access_token"] == self.rec.get("access_token")
            self._save(rec)
            self.rec, self.last_error, self.lost = rec, None, False
            if same:  # kept, since the refresh token may have rotated, but not a success
                return self._failed("the refresh answer kept the same access token")
            self.refreshes, self.failures, self.retry_at = self.refreshes + 1, 0, 0.0

    async def _token_request(self, force: bool) -> httpx.Response:
        return await self.auth.post(SIWC_AUTH + "/api/accounts/oauth/token",
                                    headers={"accept": "application/json"}, data={
            "grant_type": "refresh_token", "client_id": self.rec["client_id"],
            "refresh_token": self.rec["refresh_token"], "resource": SIWC_RESOURCE})

    def _permanent(self, status: int, code: str | None) -> bool:
        return status in (400, 401, 403) and code in FATAL

    def _record_from(self, data: dict) -> dict | None:
        return record_from(data, self.rec)

    def _failed(self, message: str, lost: bool = False) -> None:
        self.last_error = message
        self.failures += 1
        self.retry_at = time.time() + min(RETRY_MAX_S, RETRY_S * 2 ** min(self.failures - 1, 10))
        if lost or self.lost:
            # Kept across a restart, with the backoff: a restart neither forgets that the
            # token may be spent nor tries again at once.
            self.lost = True
            self._note(lost=True, retry_at=self.retry_at, failures=self.failures)

    def _refused(self, reason: str) -> None:
        self.down = f"{reason}: {self.how_to_renew}"
        self._note(refused=self.down)

    def _note(self, **notes) -> None:
        self.rec = dict(self.rec, **{NOTES: notes})
        self._save(self.rec)

    def _save(self, rec: dict) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, exist_ok=True)
            write_private(self.path, json.dumps(rec, indent=2) + "\n")
            self.unsaved = None
        except OSError as exc:
            self.unsaved = (f"the credential in memory could not be written to disk ({type(exc).__name__}); "
                            "a restart now would need a new sign-in")

    async def maintain(self) -> None:
        if self.images:
            try:
                await self.images.maintain()
            except Exception as exc:  # the provider's own credential must still be kept
                print(f"warning: provider {self.name}: images login maintenance failed "
                      f"({type(exc).__name__})", file=sys.stderr)
            try:
                usage = await self.images.usage()
            except Exception as exc:
                print(f"warning: provider {self.name}: usage query failed ({type(exc).__name__})", file=sys.stderr)
                usage = None
            if usage:
                self.record_quotas(usage, "usage", time.time())
        if not self.rec:
            return
        if self.unsaved:  # also a refusal or a lost answer that is not on disk yet
            self._save(self.rec)
        if not self.down and self._remaining() < REFRESH_AHEAD_S:
            await self.refresh(self.rec["access_token"])

    def record_quotas(self, quotas: dict, source: str, seen: float) -> None:
        slots = ("primary", "secondary")
        for limit, figures in quotas.items():
            observed = {slot: figures[slot] | {"seen_at": int(seen)} for slot in slots if figures.get(slot)}
            for window in observed.values():
                reset = window["resets_at"]
                if not (number(reset) and 0 < reset < QUOTA_RESET_MAX):
                    reset = None
                self.quota_history.add(limit, window["window_minutes"], [seen, window["used_percent"], reset, source])
            latest = figures | observed | {"route": source, "seen_at": int(seen)}
            prior = self.quotas.get(limit, {})
            if source != "usage" and len(observed) == 1:
                slot, window = next(iter(observed.items()))
                minutes = window["window_minutes"]
                latest[slot] = None
                matching_slot = empty_slot = None
                # A header can report a window in a different slot.
                for which in slots:
                    prev = prior.get(which)
                    if not isinstance(prev, dict):
                        if empty_slot is None or which == slot:
                            empty_slot = which
                        continue
                    latest[which] = {"seen_at": prior.get("seen_at")} | prev
                    if minutes and prev.get("window_minutes") == minutes:
                        matching_slot = which
                latest[matching_slot or empty_slot or slot] = window
                if "plan" not in latest and "plan" in prior:
                    latest["plan"] = prior["plan"]
            self.quotas[limit] = latest
        self.quota_history.cap_series()

    def status(self) -> dict:
        now = time.time()
        saved = self.rec.get("refresh_saved_at")
        state = "down" if self.down else "unavailable" if self._remaining() <= 0 else "ok"
        extra = {"quotas": self.quotas}
        if self.images:
            extra["images"] = self.images.login_status()
        return extra | {"kind": self.kind, "prefix": self.prefix, "upstream": self.upstream, "state": state,
                "reason": self.down or (self._unavailable() if state == "unavailable" else None),
                # Durability, apart from availability: whether disk holds what memory holds.
                "unsaved": self.unsaved, "last_error": self.last_error,
                "access_token_min": round(self._remaining() / 60) if self.rec else None,
                # Refresh tokens live 30 days from their last rotation.
                "refresh_token_days": round(30 - (now - saved) / 86400, 1) if number(saved) else None,
                "refreshes": self.refreshes, "client_id": self.rec.get("client_id"),
                "host_id": self.rec.get("ext_agent_host_id"), "scopes": self.rec.get("scopes")}


class CodexLogin(OpenAISiwc):
    """The Codex ChatGPT login behind an openai-siwc provider's images and search: what
    `codex login` writes, converted by `claude-rotate-admin install-credential --images`
    to the record shape above plus account_id. It refreshes like the provider's own
    credential, with Codex's parameters."""
    kind = "codex-login"
    upstream = CODEX_BACKEND
    suffix = ".codex.json"
    how_to_get = ("make a login with `codex login` in an empty CODEX_HOME and install it with "
                  "claude-rotate-admin install-credential PROVIDER --images")
    how_to_renew = ("make a new login with `codex login` and install it with "
                    "claude-rotate-admin install-credential PROVIDER --images")

    def __init__(self, name: str, state_dir: Path):
        super().__init__(name, {"prefix": ""}, state_dir)
        self.last_answer = None  # status and time of the backend's last answer
        self.usage_at, self.usage_error = 0.0, None  # last poll of the plan's usage

    def _usable(self, rec) -> bool:
        return usable_record(rec) and isinstance(rec.get("account_id"), str) and bool(rec["account_id"].strip())

    def _auth(self, token: str) -> dict:
        return {"authorization": f"Bearer {token}", "chatgpt-account-id": self.rec["account_id"]}

    async def _token_request(self, force: bool) -> httpx.Response:
        return await self.auth.post(SIWC_AUTH + "/oauth/token", headers={"accept": "application/json"}, json={
            "grant_type": "refresh_token", "client_id": self.rec["client_id"],
            "refresh_token": self.rec["refresh_token"],
            "refresh_reason": "unauthorized_recovery" if force else "proactive_expiry"})

    def _permanent(self, status: int, code: str | None) -> bool:
        return status == 401 or (status in (400, 403) and code in CODEX_PERMANENT)

    def _record_from(self, data: dict) -> dict | None:
        """Each token in the answer is optional and replaces the stored one, as in Codex.
        The expiry comes from the access token's exp, else expires_in, else Codex's 8 days."""
        rec = {k: v for k, v in self.rec.items() if k not in (NOTES, "earliest_refresh_at")}
        for key in ("access_token", "refresh_token"):
            if data.get(key) is not None:
                if not isinstance(data[key], str) or not data[key].strip():
                    return None
                rec[key] = data[key]
        now = int(time.time())
        exp, expires_in = jwt_exp(rec["access_token"]), data.get("expires_in")
        rec["expires_at"] = (exp if exp else now + int(expires_in)
                             if number(expires_in) and 1 <= expires_in <= 30 * 86400
                             else now + CODEX_REFRESH_FALLBACK_S)
        if data.get("earliest_refresh_at") is not None:
            rec["earliest_refresh_at"] = data["earliest_refresh_at"]
        rec["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rec["refresh_saved_at"] = now
        return rec if self._usable(rec) else None

    def note_answer(self, status: int) -> None:
        self.last_answer = {"status": status, "at": int(time.time())}

    async def usage(self) -> dict | None:
        """The plan's usage windows by limit id, at most once per USAGE_POLL_S; None
        when not due or not available (the reason is kept in usage_error)."""
        if self.down or not self.rec or self._remaining() <= 0 or time.time() - self.usage_at < USAGE_POLL_S:
            return None
        self.usage_at = time.time()
        try:
            r = await self.client.get(CODEX_USAGE_URL, headers=self._auth(self.rec["access_token"])
                                      | {"accept": "application/json"})
            data = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as exc:
            self.usage_error = f"usage query failed ({type(exc).__name__})"
            return None
        quotas = usage_quotas(data) if isinstance(data, dict) else {}
        self.usage_error = None if quotas else (f"usage query answered {r.status_code}" if r.status_code != 200
                                                else "usage answer without windows")
        return quotas or None

    def login_status(self) -> dict:
        state = "down" if self.down else "unavailable" if self._remaining() <= 0 else "ok"
        return {"state": state, "reason": self.down or (self._unavailable() if state == "unavailable" else None),
                "unsaved": self.unsaved, "last_error": self.last_error,
                "access_token_min": round(self._remaining() / 60) if self.rec else None,
                "refreshes": self.refreshes, "last_answer": self.last_answer, "usage_error": self.usage_error}


def codex_url(endpoint: str) -> httpx.URL:
    """The backend URL for a fixed endpoint, without the client's query."""
    return httpx.URL(CODEX_BACKEND + endpoint)


def codex_headers(client: dict, search: bool) -> dict:
    allowed = SEARCH_HEADERS if search else IMAGE_HEADERS
    return {k: v for k, v in client.items() if k in allowed} | {"accept-encoding": "identity"}


def jwt_exp(token: str) -> int | None:
    """The exp claim of a JWT, read without checking the signature: scheduling only."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, RecursionError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return int(exp) if number(exp) and exp > 0 else None


def usage_quotas(data: dict) -> dict:
    """/wham/usage (Codex's RateLimitStatusPayload) in quota_snapshots' shape: the plan's
    own limit as `codex`, each additional limit under its metered feature or name."""
    def window(w):
        if not isinstance(w, dict) or not number(w.get("used_percent")):
            return None
        seconds, reset = w.get("limit_window_seconds"), w.get("reset_at")
        return {"used_percent": float(w["used_percent"]),
                "window_minutes": int(seconds) // 60 if number(seconds) and seconds > 0 else None,
                "resets_at": int(reset) if number(reset) and reset > 0 else None}

    def figures(limit):
        if not isinstance(limit, dict):
            return None
        out = {"primary": window(limit.get("primary_window")), "secondary": window(limit.get("secondary_window"))}
        return out if out["primary"] or out["secondary"] else None

    plan = data.get("plan_type")
    plan = {"plan": plan[:32]} if isinstance(plan, str) and plan.strip() else {}
    quotas = {}
    if found := figures(data.get("rate_limit")):
        quotas["codex"] = found | plan
    for extra in data.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        name = next((v for v in (extra.get("metered_feature"), extra.get("limit_name")) if isinstance(v, str)), "")
        name = name.strip().lower().replace("-", "_")
        if QUOTA_ID.fullmatch(name) and (found := figures(extra.get("rate_limit"))):
            quotas[name] = found | plan
    return quotas


QUOTA_RE = re.compile(r"x-([a-z0-9-]{1,48})-(?:primary|secondary)-used-percent")


def quota_snapshots(headers) -> dict:
    """Quota figures in Codex's header families (codex-api/src/rate_limits.rs), by limit
    id. x-codex-* describe the limit x-codex-active-limit names, `codex` without it;
    x-<limit>-primary-* and -secondary-* describe that limit."""
    def num(name, kind=float):
        try:
            value = kind(headers.get(name, ""))
        except ValueError:
            return None
        return value if number(value) else None

    def window(prefix, which):
        used = num(f"{prefix}-{which}-used-percent")
        minutes, reset = num(f"{prefix}-{which}-window-minutes", int), num(f"{prefix}-{which}-reset-at", int)
        if used is None or not (used or minutes or reset):
            return None
        return {"used_percent": used, "window_minutes": minutes, "resets_at": reset}

    def limit_id(text):
        text = text.strip().lower().replace("-", "_")
        return text if QUOTA_ID.fullmatch(text) else None

    out = {}
    families = dict.fromkeys(match[1] for name in headers.keys()
                             if (match := QUOTA_RE.fullmatch(name.lower())))
    for family in families:
        limit = (limit_id(headers.get("x-codex-active-limit", "")) or "codex") if family == "codex" else limit_id(family)
        figures = {"primary": window(f"x-{family}", "primary"), "secondary": window(f"x-{family}", "secondary")}
        if limit and (figures["primary"] or figures["secondary"]):
            plan = headers.get("x-codex-plan-type", "").strip()
            out[limit] = figures | ({"plan": plan[:32]} if plan else {})
    return out


NOTES = "gateway"  # the record's field for what the gateway learnt: a refusal or a lost answer


def number(value) -> bool:
    """A JSON number usable as seconds: not a bool, NaN, Infinity or beyond any date."""
    return type(value) in (int, float) and -1e15 < value < 1e15


def usable_record(rec) -> bool:
    return (isinstance(rec, dict) and number(rec.get("expires_at"))
            and all(isinstance(rec.get(k), str) and rec[k].strip()
                    for k in ("access_token", "refresh_token", "client_id")))


class Peeked(httpx.AsyncByteStream):
    """A response body whose first bytes were read ahead, handed on whole."""

    def __init__(self, head: bytes, rest, upstream: httpx.Response):
        self.head, self.rest, self.upstream = head, rest, upstream

    async def __aiter__(self):
        if self.head:
            yield self.head
        async for chunk in self.rest:
            yield chunk

    async def aclose(self):
        await self.upstream.aclose()


async def peek(upstream: httpx.Response) -> tuple[httpx.Response, bytes | None]:
    """(the same answer, to relay unchanged; its body if it is at most PEEK_MAX bytes)."""
    rest, head, whole = upstream.aiter_bytes(), b"", True
    try:
        while len(head) <= PEEK_MAX:
            head += await rest.__anext__()
        whole = False
    except StopAsyncIteration:
        pass
    except BaseException:
        await upstream.aclose()
        raise
    # The bytes are decoded already: the copy must not decode them again.
    headers = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() != "content-encoding"]
    return (httpx.Response(upstream.status_code, headers=headers, request=upstream.request,
                           stream=Peeked(head, rest, upstream)), head if whole else None)


def error_type(body: bytes | None) -> str | None:
    """Recognize an operation refusal in either field, otherwise report error.type."""
    try:
        err = json.loads(body).get("error") if body else None
    except (ValueError, RecursionError, AttributeError):
        return None
    if not isinstance(err, dict):
        return None
    code = err.get("code")
    if isinstance(code, str) and code in REFUSED_OPERATION:
        return code
    kind = err.get("type")
    return kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", kind) else None


def error_code(data) -> str | None:
    """The OAuth error code of a token-endpoint answer, only if it looks like one."""
    err = data.get("error") if isinstance(data, dict) else None
    code = err.get("code") if isinstance(err, dict) else err
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else None


def record_from(resp: dict, previous: dict) -> dict | None:
    """Credential record from a refresh answer, or None when the answer is unusable.
    OpenAI rotates both tokens at every refresh, so the answer must carry both; the
    identity fields a refresh omits (client_id, account, scopes) are kept, the
    gateway's notes and the old earliest_refresh_at are not. Same shape as
    siwc_login.py writes."""
    if not all(isinstance(resp.get(k), str) and resp[k].strip() for k in ("access_token", "refresh_token")):
        return None
    expires_in = resp.get("expires_in", 3600)
    if not number(expires_in) or not 1 <= expires_in <= 30 * 86400:
        return None
    now = int(time.time())
    rec = {k: v for k, v in previous.items() if k not in (NOTES, "earliest_refresh_at", "expires_in")}
    for key in ("access_token", "refresh_token", "id_token", "token_type", "expires_in", "earliest_refresh_at"):
        if resp.get(key) is not None:
            rec[key] = resp[key]
    if isinstance(resp.get("scope"), str):
        rec["scopes"] = sorted(resp["scope"].split())
    rec["expires_at"] = now + int(expires_in)
    rec["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["refresh_saved_at"] = now
    return rec if usable_record(rec) else None


def earliest_refresh(rec: dict) -> int:
    """earliest_refresh_at as Unix seconds, or 0 when absent or unreadable."""
    value = rec.get("earliest_refresh_at")
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0
    return int(value) if number(value) else 0


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
                if not path.is_symlink():
                    os.chmod(path, 0o600)  # the backup shares its mode
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
    details = details if isinstance(details, dict) else {}
    # Both are parts of input_tokens: what the prompt cache served and what it stored.
    # Taken out of input_tokens, the four fields stay disjoint, as in Anthropic's usage.
    cached, written = n(details.get("cached_tokens")), n(details.get("cache_write_tokens"))
    return {"input_tokens": max(0, n(prompt) - cached - written), "output_tokens": n(output),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": written}


class StreamUsage:
    """Usage from a body relayed as it arrives, whatever its Content-Type says (OpenAI
    sends none on /v1/responses streams): SSE events as they pass, or a JSON document
    kept up to SSE_LINE_MAX and read at the end. The first bytes tell which."""

    def __init__(self):
        self.sse, self.doc, self.kind = SseUsage(), bytearray(), None

    @property
    def ended(self) -> bool:
        """A terminal SSE event has passed. A JSON body has none: it ends with its bytes."""
        return self.kind == "sse" and self.sse.ended

    def feed(self, chunk: bytes) -> None:
        if self.kind is None:
            head = chunk.lstrip()
            if not head:
                return
            self.kind = "json" if head[:1] in (b"{", b"[") else "sse"
        if self.kind == "sse":
            self.sse.feed(chunk)
        elif self.doc is not None:
            self.doc = None if len(self.doc) + len(chunk) > SSE_LINE_MAX else self.doc + chunk

    @property
    def usage(self) -> dict | None:
        if self.kind == "sse":
            return self.sse.usage
        try:
            return usage_of(json.loads(self.doc)) if self.kind == "json" and self.doc else None
        except (ValueError, RecursionError):
            return None


class SseUsage:
    """Usage from an SSE stream fed chunk by chunk; lines may span chunks. Counters
    are cumulative, and Anthropic's arrive split (input in message_start, output in
    message_delta), so each keeps the largest value seen. `ended` turns true once a
    terminal event is complete, at the blank line that closes it."""

    def __init__(self):
        self.decode = codecs.getincrementaldecoder("utf-8")(errors="replace").decode
        self.buf, self.skip, self.usage = "", False, None
        self.terminal, self.ended = False, False

    def feed(self, chunk: bytes) -> None:
        lines = (self.buf + self.decode(chunk)).split("\n")
        self.buf = lines.pop()
        for line in lines:
            if self.skip:  # the tail of an oversized line
                self.skip = False
                continue
            line = line.rstrip("\r")
            if not line:
                self.ended, self.terminal = self.ended or self.terminal, False
            elif line.startswith("event:"):
                self.terminal = self.terminal or line[6:].strip() in TERMINAL
            elif line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    self.terminal = True
                    continue
                if '"usage"' not in data and not any(name in data for name in TERMINAL):
                    continue
                try:
                    event = json.loads(data)
                except (ValueError, RecursionError):
                    continue
                if isinstance(event, dict) and event.get("type") in TERMINAL:
                    self.terminal = True
                usage = usage_of(event)
                if usage:
                    self.usage = {k: max(v, (self.usage or {}).get(k, 0)) for k, v in usage.items()}
        if len(self.buf) > SSE_LINE_MAX:
            self.buf, self.skip = "", True
