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
# First path segments the Claude passthrough and the admin endpoints use.
RESERVED = {"v1", "api", "rotate", ANTHROPIC}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
URL_RE = re.compile(r"^https?://[^/?#\s]+(/[^?#\s]*)?$")
TIMEOUT = httpx.Timeout(600.0, connect=15.0)
SIWC_RESOURCE = "https://api.openai.com/v1"  # the token's audience, fixed by the flow
# Refresh errors that only a new sign-in repairs.
FATAL = {"invalid_grant", "invalid_client", "refresh_token_reused",
         "refresh_token_expired", "refresh_token_invalidated"}
REFRESH_AHEAD_S = 600  # the background loop refreshes this long before expiry
REFRESH_INLINE_S = 60  # below this a request refreshes first, earliest_refresh_at or not
SSE_LINE_MAX = 8 * 1024 * 1024  # a longer SSE line is skipped for usage, still relayed


class ProviderDown(Exception):
    """The provider cannot serve now. The message is safe to show the caller."""


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
        if not isinstance(spec, dict):
            raise SystemExit(f"provider '{name}' must be an object")
        prefix = spec.get("prefix", "/" + name)
        segment = prefix[1:] if isinstance(prefix, str) and prefix.startswith("/") else ""
        if not NAME_RE.match(segment) or segment in RESERVED or segment in seen:
            raise SystemExit(f"provider '{name}': prefix must be one unique path segment such as "
                             f"/{name}, and not /v1, /api, /rotate or /anthropic")
        seen.add(segment)
        spec = dict(spec, prefix="/" + segment)
        for key in ("upstream", "auth_url"):
            if key in spec and not (isinstance(spec[key], str) and URL_RE.match(spec[key])):
                raise SystemExit(f"provider '{name}': {key} must be an http(s) URL")
        kind = spec.get("kind")
        if kind == "static-bearer":
            if "upstream" not in spec or not isinstance(spec.get("token_file"), str) \
                    or not spec["token_file"].strip():
                raise SystemExit(f"provider '{name}': static-bearer needs upstream and token_file")
            table[name] = StaticBearer(name, spec, config_dir)
        elif kind == "openai-siwc":
            spec.setdefault("credential_file", f"{name}-credential.json")
            if not (isinstance(spec["credential_file"], str) and FILE_RE.match(spec["credential_file"])):
                raise SystemExit(f"provider '{name}': credential_file must be a file name "
                                 "(it lives in the state directory, which the service may write)")
            table[name] = OpenAISiwc(name, spec, state_dir)
        else:
            raise SystemExit(f"provider '{name}': kind must be static-bearer or openai-siwc")
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


class StaticBearer:
    kind = "static-bearer"

    def __init__(self, name: str, spec: dict, config_dir: Path):
        self.name, self.prefix, self.upstream = name, spec["prefix"], spec["upstream"]
        self.client = httpx.AsyncClient(base_url=self.upstream, timeout=TIMEOUT)
        try:
            self._token = (config_dir / spec["token_file"]).read_text().strip()
        except OSError:
            self._token = ""
        self.down = None if self._token else f"token file {spec['token_file']} is missing or empty"

    async def send(self, method: str, url: str, headers: dict, body: bytes):
        """(streamed upstream response, retries)."""
        if self.down:
            raise ProviderDown(self.down)
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

    def __init__(self, name: str, spec: dict, state_dir: Path):
        self.name, self.prefix = name, spec["prefix"]
        # upstream and auth_url exist for stand-ins; the real flow uses the defaults.
        self.upstream = spec.get("upstream", "https://api.openai.com")
        self.token_url = spec.get("auth_url", "https://auth.openai.com").rstrip("/") \
            + "/api/accounts/oauth/token"
        self.path = state_dir / spec["credential_file"]
        self.client = httpx.AsyncClient(base_url=self.upstream, timeout=TIMEOUT)
        self.auth = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0))
        self.lock = asyncio.Lock()
        self.down = self.unsaved = self.last_error = None
        self.refreshes = 0
        self.rec = self._load()

    def _load(self) -> dict:
        try:
            rec = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.down = (f"no credential {self.path.name} in the state directory: sign in "
                         "with siwc_login.py and install the file")
            return {}
        except (OSError, ValueError) as exc:
            self.down = f"credential {self.path.name} is unreadable ({type(exc).__name__})"
            return {}
        if not isinstance(rec, dict) or not all(
                isinstance(rec.get(k), str) and rec[k] for k in ("access_token", "refresh_token", "client_id")):
            self.down = f"credential {self.path.name} lacks a token or the client_id"
            return {}
        return rec

    def _remaining(self) -> float:
        expires = self.rec.get("expires_at")
        return expires - time.time() if type(expires) in (int, float) else 0.0

    async def token(self) -> str:
        """A usable access token, refreshing first when it is about to expire."""
        if not self.down and self._remaining() < REFRESH_INLINE_S:
            await self.refresh(self.rec.get("access_token"))
        if self.down:
            raise ProviderDown(self.down)
        if self._remaining() <= 0:
            raise ProviderDown("the access token expired and the refresh failed ("
                               + (self.last_error or "no detail") + "); retrying every minute")
        return self.rec["access_token"]

    async def send(self, method: str, url: str, headers: dict, body: bytes):
        """(streamed upstream response, retries). An upstream 401 gets one refresh and
        one retry; the body is already buffered, so the retry resends it unchanged."""
        retries = 0
        while True:
            token = await self.token()
            request = self.client.build_request(
                method, url, headers=headers | {"authorization": f"Bearer {token}"}, content=body)
            upstream = await self.client.send(request, stream=True)
            if upstream.status_code != 401 or retries:
                return upstream, retries
            await upstream.aclose()
            await self.refresh(token, force=True)
            retries = 1

    async def refresh(self, seen: str | None, force: bool = False) -> None:
        """Serialized. A caller that waited on the lock finds the token it saw already
        replaced and returns, so a rotating refresh token is never sent twice."""
        async with self.lock:
            if self.down or not self.rec or self.rec.get("access_token") != seen:
                return
            if not force and self._remaining() > REFRESH_INLINE_S \
                    and time.time() < earliest_refresh(self.rec):
                return  # too early for the server; the token in hand is still good
            try:
                r = await self.auth.post(self.token_url, headers={"accept": "application/json"}, data={
                    "grant_type": "refresh_token", "client_id": self.rec["client_id"],
                    "refresh_token": self.rec["refresh_token"], "resource": SIWC_RESOURCE})
            except httpx.HTTPError as exc:
                self.last_error = f"token endpoint unreachable ({type(exc).__name__})"
                return
            try:
                data = r.json()
            except ValueError:
                data = None
            if r.status_code != 200:
                code = error_code(data)
                if r.status_code in (400, 401, 403) and code in FATAL:
                    self.down = (f"the refresh was refused ({code}): sign in again with "
                                 "siwc_login.py and install the new credential")
                else:
                    self.last_error = f"token endpoint answered {r.status_code}" + (f" ({code})" if code else "")
                return
            if not (isinstance(data, dict) and isinstance(data.get("access_token"), str)
                    and isinstance(data.get("refresh_token"), str)):
                self.last_error = "the refresh answer lacks a token; credential kept"
                return
            rec = record_from(data, self.rec)
            # The old refresh token is dead from here on. Disk first, then use; if the
            # write fails the new tokens still serve from memory (nothing better is
            # left), and the save is retried every minute.
            self._save(rec)
            self.rec, self.last_error = rec, None
            self.refreshes += 1

    def _save(self, rec: dict) -> None:
        try:
            write_private(self.path, json.dumps(rec, indent=2) + "\n")
            self.unsaved = None
        except OSError as exc:
            self.unsaved = (f"the refreshed credential could not be saved ({type(exc).__name__}); "
                            "a restart now would need a new sign-in")

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
        return {"kind": self.kind, "prefix": self.prefix, "upstream": self.upstream,
                "state": "down" if self.down else "ok", "reason": self.down,
                "last_error": self.last_error, "unsaved": self.unsaved,
                "access_token_min": round(self._remaining() / 60) if self.rec else None,
                # Refresh tokens live 30 days from their last rotation.
                "refresh_token_days": round(30 - (now - saved) / 86400, 1)
                if type(saved) in (int, float) else None,
                "refreshes": self.refreshes, "client_id": self.rec.get("client_id"),
                "host_id": self.rec.get("ext_agent_host_id"), "scopes": self.rec.get("scopes")}


def error_code(data) -> str | None:
    """The OAuth error code of a token-endpoint answer, only if it looks like one."""
    err = data.get("error") if isinstance(data, dict) else None
    code = err.get("code") if isinstance(err, dict) else err
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else None


def record_from(resp: dict, previous: dict) -> dict:
    """Credential record from a token response; fields a refresh omits are kept.
    Same shape as siwc_login.py writes."""
    now = int(time.time())
    rec = dict(previous)
    for key in ("access_token", "refresh_token", "id_token", "token_type", "expires_in", "earliest_refresh_at"):
        if resp.get(key) is not None:
            rec[key] = resp[key]
    if isinstance(resp.get("scope"), str):
        rec["scopes"] = sorted(resp["scope"].split())
    expires_in = resp.get("expires_in", 3600)
    rec["expires_at"] = now + (int(expires_in) if type(expires_in) in (int, float) else 3600)
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
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
    if not isinstance(usage, dict) and isinstance(event.get("response"), dict):
        usage = event["response"].get("usage")  # response.completed and friends
    if not isinstance(usage, dict):
        return None

    def n(value):
        return value if type(value) is int and value >= 0 else 0

    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
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
    """Usage from an SSE stream fed chunk by chunk; lines may span chunks."""

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
                    self.usage = usage
        if len(self.buf) > SSE_LINE_MAX:
            self.buf, self.skip = "", True
