"""claude-rotate Phase 1: account-rotating auth proxy for Claude Code (Max subscriptions).

Devices point at this proxy and authenticate with an internal device key:

    export ANTHROPIC_BASE_URL=http://my-server:8484   # over tailscale
    export CLAUDE_CODE_OAUTH_TOKEN=<device key from config.json>

Per request, the proxy:
  1. maps the device key -> device name (401 if unknown),
  2. swaps in the active account's real setup-token,
  3. forwards to api.anthropic.com (transparent, incl. SSE streaming),
  4. records usage + account utilization (from anthropic-ratelimit-unified-*
     headers) to logs/audit.jsonl and state.json,
  5. rotates accounts: consume-first by default (burn the account whose weekly
     window resets soonest — use-it-or-lose-it), with cooldown + hysteresis so
     accounts never ping-pong at the threshold,
  6. tells spent quota apart from per-minute burst 429s — a burst paces the
     same account (keeps the warm prompt cache) instead of rotating,
  7. optionally holds a request open until the soonest window reset when every
     account is spent (hold_max_s), so unattended runs finish on their own,
  8. refreshes the usage of idle accounts with a 1-token request every
     refresh_idle_s, so quota spent outside the proxy is seen before a switch,
  9. keeps 14 days of weekly usage for the panel's chart and run-out estimate,
     and daily totals per device and model for its 7-day, 30-day and all-time views.

GET /rotate/status (device-key auth) returns live state for the admin page.
"""

import asyncio
import json
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import MutableHeaders

from oai_compat import SseToOai, anthropic_to_oai, oai_to_anthropic

ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CLAUDE_ROTATE_CONFIG", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("CLAUDE_ROTATE_STATE", ROOT / "state.json"))
AUDIT_PATH = Path(os.environ.get("CLAUDE_ROTATE_AUDIT", ROOT / "logs" / "audit.jsonl"))

UPSTREAM = "https://api.anthropic.com"
HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
}
# These belong to the local browser or reverse-proxy trust boundary.
PRIVATE_HEADERS = {"cookie", "referer", "origin", "forwarded", "x-real-ip"}

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(refresh_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
client =httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0, connect=15.0))
state_lock = asyncio.Lock()


class PrivateResponseHeaders:
    """Wrap only send, preserving receive/disconnect signals for held requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_private(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, send_private if scope["type"] == "http" else send)


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    loaded = []
    for acct in cfg["accounts"]:
        w = acct.get("weight", 1)
        if type(w) not in (int, float) or not w > 0:
            raise SystemExit(f"account '{acct['name']}': weight must be a number > 0")
        path = CONFIG_PATH.parent / acct["token_file"]
        if path.exists():
            acct["token"] = path.read_text().strip()
            loaded.append(acct)
        else:
            print(f"warning: skipping account '{acct['name']}' — {acct['token_file']} not found",
                  file=sys.stderr)
    if not loaded:
        raise SystemExit("no accounts with tokens configured — run: ./setup.sh add-account <name>")
    cfg["accounts"] = loaded
    # The example config ships a placeholder device key that is PUBLIC (it's in
    # the repo). It must never be accepted as a live credential: refuse to start
    # while one remains, so add-account-without-add-device can't open the proxy
    # to anyone who read the example (GitHub issue #2).
    devices = cfg.get("devices")
    if not isinstance(devices, dict) or any(
            not isinstance(k, str) or not k.strip() for k in devices.values()):
        raise SystemExit("devices must map names to nonempty device keys")
    if len(set(devices.values())) != len(devices):
        raise SystemExit("device keys must be unique")
    bad = [d for d, k in devices.items() if "REPLACE-WITH" in k]
    if bad:
        raise SystemExit(
            f"placeholder device key(s) in config.json: {', '.join(bad)} — the example "
            "value is public and must never be a valid credential. "
            "Run: ./setup.sh add-device <name> (it replaces the placeholder).")
    return cfg


CFG = load_config()


def resource_limit(name: str, default: int, *, allow_zero=False, minimum=1) -> int:
    """Deployment overrides are non-secret; invalid limits fail at startup."""
    value = CFG.get(name, default)
    override = os.environ.get("CLAUDE_ROTATE_" + name.upper())
    if override is not None:
        try:
            value = int(override)
        except ValueError:
            raise SystemExit(f"{name} must be an integer")
    lower = 0 if allow_zero else minimum
    if type(value) is not int or value < lower:
        raise SystemExit(f"{name} must be an integer >= {lower}")
    return value


MAX_REQUEST_BYTES = resource_limit("max_request_bytes", 32 * 1024 * 1024)
MAX_RESPONSE_BYTES = resource_limit("max_response_bytes", 32 * 1024 * 1024)
MAX_CONCURRENT_REQUESTS = resource_limit("max_concurrent_requests", 0, allow_zero=True)
AUDIT_CACHE_RECORDS = resource_limit("audit_cache_records", 100_000)
AUDIT_CACHE_BYTES = resource_limit("audit_cache_bytes", 32 * 1024 * 1024)
AUDIT_MAX_BYTES = resource_limit("audit_max_bytes", 16 * 1024 * 1024, minimum=4096)
AUDIT_BACKUPS = resource_limit("audit_backups", 4)


class RequestAdmission:
    """Reject overload before body buffering; retain slots through SSE/holds."""

    def __init__(self, app, limit: int):
        self.app, self.limit, self.active = app, limit, 0

    async def __call__(self, scope, receive, send):
        admin = scope.get("method") == "GET" and scope.get("path") in {
            "/rotate/status", "/rotate/stats", "/rotate/panel"}
        if scope["type"] != "http" or not self.limit or admin:
            return await self.app(scope, receive, send)
        # One event loop, with no await between the check and increment.
        if self.active >= self.limit:
            response = JSONResponse({"error": "proxy at capacity; retry later"},
                                    status_code=503, headers={"Retry-After": "5"})
            return await response(scope, receive, send)
        self.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1


app.add_middleware(RequestAdmission, limit=MAX_CONCURRENT_REQUESTS)
app.add_middleware(PrivateResponseHeaders)
DEVICE_BY_KEY = {v: k for k, v in CFG["devices"].items()}
ACCOUNTS = {a["name"]: a for a in CFG["accounts"]}
THRESHOLD = CFG.get("switch_threshold", 0.8)
THRESHOLD_7D = CFG.get("switch_threshold_7d", 0.98)
STRATEGY = CFG.get("strategy", "consume-first")  # or "least-used"
COOLDOWN_S = CFG.get("switch_cooldown_s", 300)
SWITCH_MARGIN = CFG.get("switch_margin", 0.05)
CF_MARGIN_S = CFG.get("consume_first_margin_s", 3600)
HOLD_MAX_S = CFG.get("hold_max_s", 0)  # 0 = return 429 when every account is spent
REFRESH_IDLE_S = resource_limit("refresh_idle_s", 1800, allow_zero=True)  # 0 = off
PANEL_TAILNET = CFG.get("panel_tailnet", False)
if type(PANEL_TAILNET) is not bool:
    raise SystemExit("panel_tailnet must be true or false")
if PANEL_TAILNET and CFG.get("bind", "127.0.0.1") not in ("127.0.0.1", "::1", "localhost"):
    raise SystemExit("panel_tailnet needs bind 127.0.0.1 behind Tailscale Serve: on any other "
                     "address a client could send Tailscale-User-Login itself and skip the key")
STORAGE_ERRORS: dict = {}


def storage_error(operation: str, error: Exception) -> None:
    """Keep serving after a full/unavailable data disk, without silent loss."""
    now = time.time()
    prior = STORAGE_ERRORS.get(operation, {})
    if now - prior.get("last_warning", 0) >= 60:
        print(f"warning: {operation} failed ({type(error).__name__}, errno {getattr(error, 'errno', None)}); "
              "continuing with in-memory state; check data storage", file=sys.stderr)
        prior["last_warning"] = now
    prior.update(count=prior.get("count", 0) + 1, last_failure=now)
    STORAGE_ERRORS[operation] = prior


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "active_account": CFG["accounts"][0]["name"],
        "accounts": {a["name"]: {} for a in CFG["accounts"]},
        "events": [],
    }


STATE = load_state()


def save_state() -> None:
    # Write-then-rename so a crash mid-write can never leave a truncated
    # state.json (which would refuse to parse on the next start).
    tmp = STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(STATE, indent=2))
        tmp.replace(STATE_PATH)
    except OSError as exc:
        storage_error("state_write", exc)


def audit(rec: dict) -> None:
    try:
        _write_audit(rec)
    except (OSError, ValueError) as exc:
        storage_error("audit_write", exc)
    roll_up(rec, time.strftime("%Y-%m-%d"))


def _write_audit(rec: dict) -> None:
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    # A single process writes synchronously, so rollover cannot interleave with
    # another request. Each append reopens the path; external rename also works.
    line = (json.dumps(rec) + "\n").encode("utf-8")
    if len(line) > AUDIT_MAX_BYTES:
        # A client-controlled model/path must not defeat the disk bound.
        rec = dict(rec, model=str(rec.get("model", ""))[:256],
                   path=str(rec.get("path", ""))[:1024], metadata_truncated=True)
        line = (json.dumps(rec) + "\n").encode("utf-8")
    if len(line) > AUDIT_MAX_BYTES:
        raise ValueError("audit_max_bytes is too small for one audit record")
    if AUDIT_PATH.exists() and AUDIT_PATH.stat().st_size + len(line) > AUDIT_MAX_BYTES:
        for n in range(AUDIT_BACKUPS, 0, -1):
            source = AUDIT_PATH if n == 1 else Path(f"{AUDIT_PATH}.{n - 1}")
            if source.exists():
                source.replace(Path(f"{AUDIT_PATH}.{n}"))
    with AUDIT_PATH.open("ab") as f:
        f.write(line)


def utilization(name: str) -> float:
    info = STATE["accounts"].get(name, {})
    now = time.time()
    # A window that has reset no longer counts against the account.
    if info.get("reset_5h") and now >= info["reset_5h"]:
        return 0.0
    return info.get("util_5h", 0.0)


def util_7d(name: str) -> float:
    info = STATE["accounts"].get(name, {})
    if info.get("reset_7d") and time.time() >= info["reset_7d"]:
        return 0.0
    return info.get("util_7d", 0.0)


def priority(name: str) -> int:
    """Lower = preferred. Accounts without a priority share the default tier."""
    return ACCOUNTS[name].get("priority", 100)


def weight(name: str) -> float:
    """Weekly capacity relative to the other accounts (a Max 20x is 4 of a Max 5x)."""
    return ACCOUNTS[name].get("weight", 1)


def disabled(name: str) -> bool:
    return bool(ACCOUNTS[name].get("disabled"))


def usable(name: str) -> bool:
    return (not disabled(name)
            and utilization(name) < THRESHOLD and util_7d(name) < THRESHOLD_7D)


def weekly_reset(name: str) -> float:
    return STATE["accounts"].get(name, {}).get("reset_7d") or float("inf")


def earliest_relief() -> float:
    """Soonest moment any account's 5h window resets (for hold-until-reset)."""
    now = time.time()
    resets = [i.get("reset_5h") for i in STATE["accounts"].values()
              if i.get("reset_5h") and i["reset_5h"] > now]
    return min(resets) if resets else now + 300


def pick_account() -> str | None:
    """Usable = not disabled, both windows under their thresholds (a passed
    reset counts as 0). Priority tiers come first: a lower `priority` account
    always beats a higher one, and the strategy orders accounts *within* a tier.

    consume-first (default): burn the usable account whose weekly window resets
    soonest — weekly quota is use-it-or-lose-it, so spend the perishable one.
    least-used: lowest 5h utilization.
    If nothing is usable, the non-disabled account whose 5h window resets soonest.
    """
    under = [n for n in ACCOUNTS if usable(n)]
    if under:
        if STRATEGY == "consume-first":
            return min(under, key=lambda n: (priority(n), weekly_reset(n), utilization(n)))
        return min(under, key=lambda n: (priority(n), utilization(n)))
    pool = [n for n in ACCOUNTS if not disabled(n)]
    if not pool:
        return None
    return min(pool, key=lambda n: STATE["accounts"].get(n, {}).get("reset_5h", 0))


async def note_response(account: str, headers: httpx.Headers, status: int) -> str | None:
    """Update account state from unified rate-limit headers; maybe switch.

    Returns what happened: "burst" (per-minute 429, paced — no rotation),
    "switched" (active account changed, retry can ride it), "exhausted"
    (quota gone and nowhere better to go), or None.
    """
    def _f(h):
        v = headers.get(h)
        return float(v) if v is not None else None

    async with state_lock:
        info = STATE["accounts"].setdefault(account, {})
        for key, header in (
            ("util_5h", "anthropic-ratelimit-unified-5h-utilization"),
            ("util_7d", "anthropic-ratelimit-unified-7d-utilization"),
            ("reset_5h", "anthropic-ratelimit-unified-5h-reset"),
            ("reset_7d", "anthropic-ratelimit-unified-7d-reset"),
        ):
            val = _f(header)
            if val is not None:
                info[key] = val
        if headers.get("anthropic-ratelimit-unified-status"):
            info["status"] = headers["anthropic-ratelimit-unified-status"]
        now = time.time()
        info["last_seen"] = now

        quota_hit = info.get("status") == "rejected" or (
            status == 429 and (utilization(account) >= 1.0 or util_7d(account) >= 1.0))
        if status == 429 and not quota_hit:
            # Per-minute burst limit, not spent quota. Rotating would just move
            # the burst to the next account and drop its warm prompt cache —
            # pace this account briefly instead.
            try:
                pace = min(float(headers.get("retry-after", "")), 60.0)
            except ValueError:
                pace = 15.0
            info["pace_until"] = now + pace
            save_state()
            return "burst"

        verdict = None
        if STATE["active_account"] != account:
            # Another request already rotated; a retry rides the new active.
            verdict = "switched" if quota_hit else None
        else:
            cooled = now - STATE.get("last_switch_ts", 0) >= COOLDOWN_S
            over = utilization(account) >= THRESHOLD or util_7d(account) >= THRESHOLD_7D
            nxt = pick_account()
            reason = None
            if nxt is not None and nxt != account:
                if quota_hit:
                    reason = "quota_exhausted"  # hard stop: no cooldown, no margin
                elif disabled(account):
                    reason = "account_disabled"  # hard rule: leave immediately
                elif over and cooled:
                    # Hysteresis: only move to a meaningfully better account, so
                    # two accounts hovering at the line never ping-pong. A move
                    # to a preferred tier, or off a week-exhausted account,
                    # skips the margin.
                    if (util_7d(account) >= THRESHOLD_7D
                            or priority(nxt) < priority(account)
                            or utilization(nxt) <= utilization(account) - SWITCH_MARGIN):
                        reason = f"utilization>={THRESHOLD}"
                elif cooled and usable(nxt):
                    if priority(nxt) < priority(account):
                        # A preferred account has room again — pull traffic back.
                        reason = "priority_recovery"
                    elif (STRATEGY == "consume-first"
                          and weekly_reset(nxt) < weekly_reset(account) - CF_MARGIN_S):
                        # Use-it-or-lose-it: proactively jump to an account whose
                        # weekly window expires clearly sooner than the active one's.
                        reason = "consume_first"
            if reason:
                STATE["active_account"] = nxt
                STATE["last_switch_ts"] = now
                STATE["events"].append({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "type": "switch",
                    "from": account, "to": nxt,
                    "reason": reason,
                    "util": utilization(account),
                })
                STATE["events"] = STATE["events"][-200:]
                verdict = "switched"
            elif quota_hit:
                verdict = "exhausted"
        save_state()
        return verdict


# Cheapest call that returns the unified headers on a setup-token: /api/oauth/usage
# needs the user:profile scope and count_tokens omits them. A model the plan
# can't use answers 429 with no headers, so replace this one when it retires.
REFRESH_BODY = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 1,
                           "messages": [{"role": "user", "content": "."}]}).encode()
_REFRESH_TRIED: dict = {}


async def refresh_idle_accounts() -> None:
    """Usage headers only arrive with traffic, so an idle account's numbers go
    stale and quota spent outside the proxy (claude.ai, a direct Claude Code
    login) stays invisible until a switch lands on it. Send each account not
    seen or tried for REFRESH_IDLE_S a 1-token request and record its headers."""
    now = time.time()
    for name in ACCOUNTS:
        last = max(STATE["accounts"].get(name, {}).get("last_seen", 0), _REFRESH_TRIED.get(name, 0))
        if disabled(name) or now - last < REFRESH_IDLE_S:
            continue
        _REFRESH_TRIED[name] = now
        try:
            r = await client.post("/v1/messages", content=REFRESH_BODY, headers={
                "authorization": f"Bearer {ACCOUNTS[name]['token']}",
                "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20",
                "content-type": "application/json"})
        except httpx.HTTPError as exc:
            print(f"warning: usage refresh of {name} failed ({type(exc).__name__})", file=sys.stderr)
            continue
        if "anthropic-ratelimit-unified-status" in r.headers:
            await note_response(name, r.headers, r.status_code)
        else:
            # note_response would read a headerless 429 as a burst and pace the account.
            print(f"warning: usage refresh of {name}: HTTP {r.status_code} without usage headers",
                  file=sys.stderr)


async def refresh_loop() -> None:
    while True:
        if REFRESH_IDLE_S:
            await refresh_idle_accounts()
        record_history(time.time())
        save_rollup()
        await asyncio.sleep(60)


HISTORY_PATH = STATE_PATH.with_name("usage_history.json")
HISTORY_DAYS, SAMPLE_S, PROJECT_DAYS = 14, 1800, 7


def load_history() -> list:
    try:
        history = json.loads(HISTORY_PATH.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        # The chart is disposable; a damaged file must not stop the proxy.
        print(f"warning: ignoring {HISTORY_PATH} ({type(exc).__name__})", file=sys.stderr)
        return []
    return history if isinstance(history, list) else []


HISTORY = load_history()


def weekly_snapshot() -> dict:
    return {n: [round(util_7d(n), 4), STATE["accounts"][n]["reset_7d"]] for n in ACCOUNTS
            if not disabled(n) and STATE["accounts"].get(n, {}).get("reset_7d")}


def record_history(now: float) -> None:
    """One weekly-usage snapshot of every enabled account per SAMPLE_S, kept HISTORY_DAYS."""
    if HISTORY and now - HISTORY[-1]["ts"] < SAMPLE_S:
        return
    snapshot = weekly_snapshot()
    if not snapshot:
        return
    HISTORY.append({"ts": round(now), "accounts": snapshot})
    while HISTORY[0]["ts"] < now - HISTORY_DAYS * 86400:
        HISTORY.pop(0)
    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(HISTORY))
        tmp.replace(HISTORY_PATH)
    except OSError as exc:
        storage_error("history_write", exc)


def weekly_series(now: float) -> list:
    """[ts, used, limit] in % of one weekly quota. `used` sums each enabled account's
    weekly rises (weighted); `limit` = used + what every account has left, so it steps
    up at a reset by what that account used in the closing window — the rest expired."""
    points, used, seen = [], 0.0, {}
    for sample in HISTORY + [{"ts": now, "accounts": weekly_snapshot()}]:
        left = 0.0
        for name, (u, reset) in sample["accounts"].items():
            if name not in ACCOUNTS or disabled(name):
                continue
            # First sight counts as a new window, so the chart opens at the current
            # week's usage against the full limit (the sum of the weights).
            last_reset, top = seen.get(name, (float("-inf"), 0.0))
            if reset > last_reset + 3600:  # a new weekly window: all of it is new usage
                used, top = used + weight(name) * u, u
            elif u > top:
                used, top = used + weight(name) * (u - top), u
            seen[name] = (max(reset, last_reset), top)
            left += weight(name) * (1 - u)
        points.append([round(sample["ts"]), round(used * 100, 1), round((used + left) * 100, 1)])
    return points


def weekly_outlook(now: float) -> dict:
    """The chart's series plus a PROJECT_DAYS projection: spend the last 24 h rate
    consume-first (soonest weekly reset first); while every enabled account is at
    100% usage pauses until the next reset. `eta` is the first such moment."""
    points = weekly_series(now)
    out = {"series": points, "projection": [], "rate_per_day": None,
           "eta": None, "back": None, "back_account": None,
           "capacity": round(sum(weight(n) for n in weekly_snapshot()) * 100)}
    day = [p for p in points if p[0] >= now - 86400]
    # Usage cannot rise while every account is spent, so time at the limit would
    # understate demand: count only the stretches with quota left (as the panel shades).
    span = sum(b[0] - a[0] for a, b in zip(day, day[1:]) if a[2] - a[1] > 0.1)
    if span < 3600:
        out["note"] = "not enough history with quota left yet to measure a rate"
        return out
    rate = (day[-1][1] - day[0][1]) / span  # % of one weekly quota per second
    out["rate_per_day"] = round(rate * 86400, 1)
    accts = {n: [u * 100, reset] for n, (u, reset) in weekly_snapshot().items()}
    if rate <= 0 or not accts:
        out["note"] = "weekly usage did not rise over the last 24 h"
        return out
    for a in accts.values():
        while a[1] <= now:  # reset passed with no reading since: assume the next week follows
            a[1] += 7 * 86400

    def left():
        return sum(weight(n) * (100 - u) for n, (u, _) in accts.items())

    t, used, horizon = now, points[-1][1], now + PROJECT_DAYS * 86400
    proj = [[round(t), used, round(used + left(), 1)]]
    while t < horizon:
        nxt = min(accts, key=lambda n: accts[n][1])
        reset = accts[nxt][1]
        end = min(reset, horizon)
        empty = t + left() / rate
        if empty < end:  # everything spent before the next reset: flat until it
            used += left()
            for a in accts.values():
                a[0] = 100.0
            proj.append([round(empty), round(used, 1), round(used, 1)])
            if out["eta"] is None:
                out.update(eta=round(empty), back=round(reset), back_account=nxt)
        else:
            spend = rate * (end - t)
            used += spend
            for n in sorted(accts, key=lambda n: accts[n][1]):
                take = min(spend, weight(n) * (100 - accts[n][0]))
                accts[n][0] += take / weight(n)
                spend -= take
        proj.append([round(end), round(used, 1), round(used + left(), 1)])
        if end == reset:
            accts[nxt] = [0.0, reset + 7 * 86400]
            proj.append([round(end), round(used, 1), round(used + left(), 1)])
        t = end
    out["projection"] = proj
    return out


def usage_from_sse(text: str, sink: dict) -> None:
    for line in text.splitlines():
        if line.startswith("data:") and '"usage"' in line:
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            usage = obj.get("usage") or obj.get("message", {}).get("usage")
            if usage:
                for k in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens"):
                    if usage.get(k):
                        sink[k] = max(sink.get(k, 0), usage[k])


def summarize_usage(usage: dict | None) -> dict | None:
    if not usage:
        return None
    return {k: usage.get(k, 0) for k in (
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


@app.get("/rotate/status")
async def rotate_status(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    now = time.time()
    accounts = {}
    for name in ACCOUNTS:
        info = dict(STATE["accounts"].get(name, {}))
        info["effective_util_5h"] = utilization(name)
        info["effective_util_7d"] = util_7d(name)
        info["priority"] = priority(name)
        info["disabled"] = disabled(name)
        if info.get("reset_5h"):
            info["reset_5h_in_min"] = max(0, round((info["reset_5h"] - now) / 60))
        if info.get("reset_7d"):
            info["reset_7d_in_h"] = max(0, round((info["reset_7d"] - now) / 3600, 1))
        accounts[name] = info
    return {
        "active_account": STATE["active_account"],
        "switch_threshold": THRESHOLD,
        "switch_threshold_7d": THRESHOLD_7D,
        "strategy": STRATEGY,
        "hold_max_s": HOLD_MAX_S,
        "accounts": accounts,
        "recent_events": STATE["events"][-10:],
        "storage_errors": STORAGE_ERRORS,
        "weekly": weekly_outlook(now),
    }


def panel_allowed(request: Request) -> bool:
    # Tailscale Serve sets Tailscale-User-Login for tailnet users and strips client
    # copies; startup enforces a loopback bind, so only Serve (or a local process) can add it.
    return ((PANEL_TAILNET and bool(request.headers.get("tailscale-user-login")))
            or device_from_request(request) is not None)


def device_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    key = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if not key:
        key = request.query_params.get("key", "")
    if not key:
        return None
    return DEVICE_BY_KEY.get(key)


async def bounded_body(request: Request) -> bytes:
    """Bound actual bytes, including bodies without a Content-Length header."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")
        body.extend(chunk)
    return bytes(body)


async def bounded_response(upstream: httpx.Response) -> bytes:
    """Limit buffered upstream responses; streaming responses stay streaming."""
    body = bytearray()
    try:
        async for chunk in upstream.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise HTTPException(status_code=502, detail="upstream response too large")
            body.extend(chunk)
    finally:
        await upstream.aclose()
    return bytes(body)


# API list prices per MTok (input, output), matched by substring of the model id.
# Cost columns are *API-equivalent* dollars: what this usage would cost pay-per-token.
# Override in config.json under "prices_per_mtok".
PRICES = CFG.get("prices_per_mtok", {
    "fable": [10.0, 50.0], "mythos": [10.0, 50.0],
    "opus": [5.0, 25.0],
    "sonnet": [3.0, 15.0],
    "haiku": [1.0, 5.0],
})
DEFAULT_PRICE = [5.0, 25.0]
ALERT_CFG = CFG.get("alerts", {
    "device_tokens_per_hour": 5_000_000,
    "expensive_model_patterns": ["opus", "fable", "mythos"],
    "util_warn": 0.8,
})


def price_for(model: str | None) -> list[float]:
    model = model if isinstance(model, str) else None
    for pat, p in PRICES.items():
        if model and pat in model:
            return p
    return DEFAULT_PRICE


def api_cost(model: str | None, usage: dict) -> float:
    """API-equivalent $ for one request: in + out + cache read (0.1x) + cache write (1.25x)."""
    p_in, p_out = price_for(model)
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.1
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
    ) / 1_000_000


# Incremental, bounded tail of the current audit file. Counts become partial
# after rotation or either retention limit; the API/panel explicitly says so.
_AUDIT_CACHE: dict = {"offset": 0, "records": deque(), "sig": b""}


def _tail_audit() -> None:
    import datetime as dt

    cache = _AUDIT_CACHE
    cache.setdefault("bytes", 0)
    cache.setdefault("limited_until", 0)
    now = time.time()
    if not AUDIT_PATH.exists():
        if cache["offset"]:
            cache["limited_until"] = now + 86400
        cache["offset"] = 0
        cache["sig"] = b""
        cache["bytes"] = 0
        cache["identity"] = None
        cache["records"].clear()
        return
    day_ago = now - 86400
    with AUDIT_PATH.open("rb") as f:
        stat = os.fstat(f.fileno())
        size = stat.st_size
        identity = (stat.st_dev, stat.st_ino)
        head = f.read(64)
        # A shrunken file, or changed leading bytes, means audit.jsonl was
        # rotated or rewritten out from under us — the cached window no
        # longer describes this file, so rebuild from the top. (Size alone
        # can't catch truncate-and-rewrite-bigger; the head fingerprint can.)
        if (cache.get("identity", identity) != identity
                or size < cache["offset"] or not head.startswith(cache["sig"])):
            cache["limited_until"] = now + 86400
            cache["offset"] = 0
            cache["bytes"] = 0
            cache["records"].clear()
        cache["identity"] = identity
        cache["sig"] = head
        # After restart, recent rotated files mean the current log is not the
        # whole 24h history. Retained archives remain available for offline use.
        previous = Path(f"{AUDIT_PATH}.1")
        if previous.exists():
            cache["limited_until"] = max(cache["limited_until"],
                                         previous.stat().st_mtime + 86400)
        if size == cache["offset"]:
            return
        if size - cache["offset"] > AUDIT_CACHE_BYTES:
            cache["offset"] = size - AUDIT_CACHE_BYTES
            cache["limited_until"] = now + 86400
            cache["bytes"] = 0
            cache["records"].clear()
            f.seek(cache["offset"] - 1)
            # Skip a first fragment without reading an unbounded long line.
            begins_mid_line = f.read(1) != b"\n"
        else:
            begins_mid_line = False
        f.seek(cache["offset"])
        chunk = f.read(min(AUDIT_CACHE_BYTES, size - cache["offset"]))
    if begins_mid_line:
        start = chunk.find(b"\n") + 1
        if not start:
            # The tail is one oversized fragment; consume it without caching.
            cache["offset"] += len(chunk)
            return
        cache["offset"] += start
        chunk = chunk[start:]
    # Consume complete lines only; a trailing partial line (an append in
    # flight from a writer outside this process) is left for the next call.
    end = chunk.rfind(b"\n") + 1
    cache["offset"] += end
    for line in chunk[:end].splitlines():
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            cache["limited_until"] = now + 86400
            continue
        if not isinstance(rec, dict):
            cache["limited_until"] = now + 86400
            continue
        try:
            rec["_ts"] = dt.datetime.strptime(
                rec.get("ts", ""), "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except (ValueError, TypeError):
            cache["limited_until"] = now + 86400
            continue
        if rec["_ts"] >= day_ago:
            rec["_bytes"] = len(line) + 1
            cache["records"].append(rec)
            cache["bytes"] += rec["_bytes"]
            while (len(cache["records"]) > AUDIT_CACHE_RECORDS
                   or cache["bytes"] > AUDIT_CACHE_BYTES):
                removed = cache["records"].popleft()
                cache["bytes"] -= removed["_bytes"]
                cache["limited_until"] = max(cache["limited_until"],
                                             removed["_ts"] + 86400)


def bump(bucket, key, usage, cost):
    b = bucket.setdefault(key, {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                                "cache_read": 0, "cache_write": 0, "cost_usd": 0.0})
    b["requests"] += 1
    b["input_tokens"] += usage.get("input_tokens", 0)
    b["output_tokens"] += usage.get("output_tokens", 0)
    b["cache_read"] += usage.get("cache_read_input_tokens", 0)
    b["cache_write"] += usage.get("cache_creation_input_tokens", 0)
    b["cost_usd"] += cost


def aggregate_audit() -> dict:
    """Roll up the last 24h of audit.jsonl by device/model/account, with alerts."""
    now = time.time()
    hour_ago = now - 3600
    day_ago = now - 86400
    by_device: dict = {}
    by_model: dict = {}
    by_account: dict = {}
    device_hour_tokens: dict = {}
    device_hour_reqs: dict = {}
    device_meta: dict = {}
    expensive_hour: dict = {}

    _tail_audit()
    records = _AUDIT_CACHE["records"]
    while records and records[0]["_ts"] < day_ago:
        _AUDIT_CACHE["bytes"] -= records.popleft().get("_bytes", 0)
    for rec in records:
        # Old logs may contain arbitrary JSON values supplied as `model`.
        model = rec.get("model")
        model = model if isinstance(model, str) else None
        usage = rec.get("usage") or {}
        ts = rec["_ts"]
        cost = api_cost(model, usage)
        dev = rec.get("device", "?")
        meta = device_meta.setdefault(dev, {})
        if ts >= meta.get("last_ts", 0):
            meta.update({"last_ts": ts, "last_model": model,
                         "last_account": rec.get("account")})
        bump(by_device, rec.get("device", "?"), usage, cost)
        bump(by_model, model or "?", usage, cost)
        bump(by_account, rec.get("account", "?"), usage, cost)
        if ts >= hour_ago:
            total = sum(usage.get(k, 0) for k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens"))
            device_hour_tokens[rec.get("device", "?")] = (
                device_hour_tokens.get(rec.get("device", "?"), 0) + total)
            device_hour_reqs[dev] = device_hour_reqs.get(dev, 0) + 1
            if any(p in (model or "") for p in ALERT_CFG["expensive_model_patterns"]):
                key = f'{rec.get("device", "?")} / {model}'
                expensive_hour[key] = expensive_hour.get(key, 0) + 1

    limited = now < _AUDIT_CACHE.get("limited_until", 0)
    alerts = ([{"level": "warn", "text": "Statistics are partial: older history was "
                "rotated, skipped or dropped to stay within resource limits."}] if limited else [])
    for dev, toks in device_hour_tokens.items():
        if toks > ALERT_CFG["device_tokens_per_hour"]:
            alerts.append({"level": "warn",
                           "text": f"{dev} consumed {toks:,} tokens in the last hour "
                                   f"(threshold {ALERT_CFG['device_tokens_per_hour']:,})"})
    for key, n in expensive_hour.items():
        alerts.append({"level": "info", "text": f"expensive model in use: {key} — {n} requests in the last hour"})
    for name in ACCOUNTS:
        u = utilization(name)
        if u >= ALERT_CFG["util_warn"]:
            alerts.append({"level": "warn", "text": f"{name} 5h window at {u:.0%} (switch threshold {THRESHOLD:.0%})"})

    # Roster covers every registered device, traffic or not.
    devices = {}
    for name, key in CFG["devices"].items():
        meta = device_meta.get(name, {})
        last_ts = meta.get("last_ts")
        devices[name] = {
            "key_suffix": "…" + key[-4:],
            "online": bool(last_ts and now - last_ts < 600),
            "last_seen_min": round((now - last_ts) / 60) if last_ts else None,
            "last_model": meta.get("last_model"),
            "last_account": meta.get("last_account"),
            "req_1h": device_hour_reqs.get(name, 0),
            "tokens_1h": device_hour_tokens.get(name, 0),
        }

    return {"window": "last 24h (partial)" if limited else "last 24h",
            "history_limited": limited, "retained_records": len(records),
            "devices": devices, "by_device": by_device,
            "by_model": by_model, "by_account": by_account, "alerts": alerts}


USAGE_PATH = STATE_PATH.with_name("device_usage.json")
ROLLUP_DAYS = 31
ROLLUP: dict = {"days": {}, "total": {"device": {}, "model": {}}, "since": None}
ROLLUP_DIRTY = False


def roll_up(rec: dict, day: str) -> None:
    """Add one audit record to the per-day and all-time totals by device and model.
    The audit log rotates and the stats cache holds 24 h; this keeps the longer view."""
    global ROLLUP_DIRTY
    model = rec.get("model")
    model = model if isinstance(model, str) else None
    usage = rec.get("usage") or {}
    cost = api_cost(model, usage)
    buckets = ROLLUP["days"].setdefault(day, {"device": {}, "model": {}})
    for kind, key in (("device", str(rec.get("device", "?"))), ("model", model or "?")):
        bump(buckets[kind], key, usage, cost)
        bump(ROLLUP["total"][kind], key, usage, cost)
    ROLLUP["since"] = min(ROLLUP["since"] or day, day)
    ROLLUP_DIRTY = True


def load_rollup() -> None:
    """Load the totals; without a usable file, rebuild them once from the audit
    log and its rotated copies, oldest first."""
    global ROLLUP
    try:
        loaded = json.loads(USAGE_PATH.read_text())
        if isinstance(loaded, dict) and {"days", "total", "since"} <= loaded.keys():
            ROLLUP = loaded
            return
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print(f"warning: rebuilding {USAGE_PATH} from the audit log ({type(exc).__name__})",
              file=sys.stderr)
    for path in [Path(f"{AUDIT_PATH}.{n}") for n in range(AUDIT_BACKUPS, 0, -1)] + [AUDIT_PATH]:
        try:
            with path.open("rb") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("ts"), str):
                        roll_up(rec, rec["ts"][:10])
        except OSError:
            continue


load_rollup()


def save_rollup() -> None:
    global ROLLUP_DIRTY
    if not ROLLUP_DIRTY:
        return
    import datetime as dt
    cutoff = (dt.date.today() - dt.timedelta(days=ROLLUP_DAYS)).isoformat()
    for day in [d for d in ROLLUP["days"] if d < cutoff]:
        del ROLLUP["days"][day]
    tmp = USAGE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(ROLLUP))
        tmp.replace(USAGE_PATH)
        ROLLUP_DIRTY = False
    except OSError as exc:
        storage_error("usage_write", exc)


def usage_periods() -> dict:
    """7-day, 30-day and all-time totals by device and model, in calendar days with
    today included, plus each device's daily cost-equivalent over the last 30 days."""
    import datetime as dt
    today = dt.date.today()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]

    def total(span):
        out = {"device": {}, "model": {}}
        for day in span:
            for kind, buckets in ROLLUP["days"].get(day, {}).items():
                for key, b in buckets.items():
                    acc = out[kind].setdefault(key, dict.fromkeys(b, 0))
                    for field, v in b.items():
                        acc[field] += v
        return out

    trend = {dev: [round(ROLLUP["days"].get(d, {}).get("device", {}).get(dev, {}).get("cost_usd", 0), 2)
                   for d in days] for dev in ROLLUP["total"]["device"]}
    return {"since": ROLLUP["since"], "7d": total(days[-7:]), "30d": total(days),
            "all": ROLLUP["total"], "trend": trend}


@app.get("/rotate/stats")
async def rotate_stats(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    return aggregate_audit() | {"periods": usage_periods()}


PANEL_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>claude-rotate</title>
<style>
 :root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
  --border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--track:#0d366b;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b}
 *{box-sizing:border-box} body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
 main{max-width:1180px;margin:0 auto;padding:20px 24px 40px;display:grid;gap:16px}
 header{display:flex;align-items:center;gap:12px;flex-wrap:wrap} h1{font-size:18px;margin:0;font-weight:600}
 .pill{border:1px solid var(--border);border-radius:999px;padding:2px 10px;color:var(--ink2);font-size:13px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
 .spacer{flex:1} .muted{color:var(--muted)} .small{font-size:12px}
 .card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;overflow-x:auto}
 .card h2{font-size:13px;font-weight:600;color:var(--ink2);margin:0 0 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .banner{display:flex;gap:10px;padding:10px 14px;border-radius:10px;border:1px solid var(--border);color:var(--ink2);margin-bottom:8px}
 .banner.warn{border-color:rgba(250,178,25,.35);background:rgba(250,178,25,.06)} .banner.warn b{color:var(--warn)}
 .kpis{display:grid;grid-template-columns:1.6fr repeat(4,1fr);gap:16px}
 .hero .v{font-size:48px;font-weight:600;line-height:1.1;margin:4px 0} .label{color:var(--ink2);font-size:13px}
 .tile .v{font-size:26px;font-weight:600;margin:6px 0 2px}
 .accounts{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px}
 .acct-head{display:flex;align-items:baseline;gap:8px;margin-bottom:12px;flex-wrap:wrap} .acct-head b{font-size:16px}
 .badge{font-size:12px;border-radius:999px;padding:1px 8px;border:1px solid var(--border);color:var(--ink2)}
 .meter-row{display:grid;grid-template-columns:28px 1fr 250px;gap:10px;align-items:center;margin:8px 0}
 .meter{position:relative;height:8px;border-radius:4px;background:var(--track)} .meter i{position:absolute;inset:0 auto 0 0;border-radius:4px}
 .meter .tick{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink2);opacity:.6}
 .legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--ink2);font-weight:400;margin-left:auto}
 .key{display:inline-block;width:16px;border-top:2px solid;vertical-align:4px;margin-right:6px}
 .key.dash{border-top-style:dashed} .key.band{height:10px;border:0;vertical-align:-1px;background:rgba(208,59,59,.3)}
 #weekly{position:relative} svg text{fill:var(--muted);font-size:11px;font-family:inherit}
 .tip{position:absolute;pointer-events:none;background:#262624;border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;min-width:170px;display:none;top:20px}
 .tip .row{display:flex;justify-content:space-between;gap:12px} .tip .v{color:var(--ink);font-weight:600}
 .sentence{margin-top:8px;color:var(--ink2)}
 table{border-collapse:collapse;width:100%} th,td{padding:8px 10px;border-bottom:1px solid var(--grid);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
 th{color:var(--muted);font-weight:500;font-size:12px} td:first-child,th:first-child,.l{text-align:left} tr:last-child td{border-bottom:0}
 td .sub{display:block;color:var(--muted);font-size:11px} tr.removed td{color:var(--muted)}
 .seg{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-left:auto}
 .seg button{background:none;border:0;color:var(--ink2);padding:4px 12px;font:inherit;font-size:12px;cursor:pointer} .seg button.on{background:var(--grid);color:var(--ink);font-weight:600}
 .split{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}
 .timeline div{display:flex;gap:12px;padding:6px 0;border-bottom:1px solid var(--grid);color:var(--ink2)} .timeline div:last-child{border-bottom:0}
 .linkbtn{background:none;border:1px solid var(--border);border-radius:6px;color:var(--ink2);font:inherit;font-size:12px;padding:2px 8px;cursor:pointer}
 @media (max-width:900px){.kpis,.split{grid-template-columns:1fr}.meter-row{grid-template-columns:28px 1fr}}
</style></head><body><main>
<header><h1>claude-rotate</h1><span class="muted" id="host"></span><span id="pills"></span><span class="spacer"></span><span class="muted small" id="upd"></span></header>
<div id="alerts"></div>
<section class="kpis">
 <div class="card hero"><div class="label">Every account spent in</div><div class="v" id="hero">–</div><div class="muted" id="hero-sub"></div></div>
 <div class="card tile"><div class="label">Weekly quota left</div><div class="v" id="k-left">–</div><div class="muted small" id="k-left-sub"></div></div>
 <div class="card tile"><div class="label">Burn rate</div><div class="v" id="k-rate">–</div><div class="muted small">of a weekly quota per day</div></div>
 <div class="card tile"><div class="label">Requests, 24 h</div><div class="v" id="k-req">–</div><div class="muted small" id="k-req-sub"></div></div>
 <div class="card tile"><div class="label">Cost-equivalent, 7 d</div><div class="v" id="k-cost">–</div><div class="muted small" id="k-cost-sub"></div></div>
</section>
<section class="accounts" id="accounts"></section>
<section class="card"><h2>Weekly quota, all accounts <span class="muted" style="font-weight:400">14 days + 7-day projection</span>
 <span class="legend"><span><span class="key" style="border-color:var(--s1)"></span>Usage</span><span><span class="key" style="border-color:var(--s2)"></span>Limit</span>
 <span><span class="key dash" style="border-color:var(--muted)"></span>Projected</span><span><span class="key band"></span>&#9940; Every account at its limit</span></span>
 <button class="linkbtn" id="tbl">Table</button></h2>
 <div id="weekly" class="muted">collecting data</div><div class="tip" id="tip"></div></section>
<section class="card"><h2>Devices</h2><table id="devices"></table></section>
<section class="split">
 <div class="card"><h2>By model <span class="seg" id="period"></span></h2><table id="models"></table></div>
 <div class="card"><h2>By account, 24 h</h2><table id="by-account"></table><h2 style="margin-top:20px">Recent switches</h2><div class="timeline" id="events"></div></div>
</section>
<p class="muted small">Cost-equivalent = API list prices (cache read 0.1&times;, write 1.25&times;); actual spend is covered by the subscriptions.</p>
</main>
<script>
const KEY = new URLSearchParams(location.search).get('key');
const cleanURL = new URL(location.href);
cleanURL.searchParams.delete('key');
history.replaceState(null, '', cleanURL.pathname + cleanURL.search + cleanURL.hash);
const $=id=>document.getElementById(id), H=3600, D=86400, PERIODS={'24 h':'24h','7 d':'7d','30 d':'30d','All time':'all'};
let period='7 d', showTable=false, WK=null;
function esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmt(n){n=n||0;return n>=1e6?(n/1e6).toFixed(n>=1e7?0:1)+'M':n>=1e3?(n/1e3).toFixed(n>=1e4?0:1)+'k':''+Math.round(n)}
function money(v){v=v||0;return '$'+(v>=100?Math.round(v).toLocaleString():v.toFixed(2))}
function tok(b){return (b.input_tokens||0)+(b.output_tokens||0)+(b.cache_write||0)}
function sum(o,f){return Object.values(o||{}).reduce((a,b)=>a+(f(b)||0),0)}
function when(ts){return new Date(ts*1000).toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'})}
function day(ts){return new Date(ts*1000).toLocaleDateString([],{month:'short',day:'numeric'})}
function dur(s){return s<5400?Math.round(s/60)+' min':s<2*D?(s/H).toFixed(s<10*H?1:0)+' h':(s/D).toFixed(1)+' d'}
function ago(min){return min==null?'never':min<1?'just now':min<60?min+' min ago':(min/60).toFixed(1)+' h ago'}
function cell(b){return b?`<td>${money(b.cost_usd)}<span class="sub">${fmt(tok(b))} tok &middot; ${fmt(b.requests)} req</span></td>`:'<td class="muted">&ndash;</td>'}
function meter(label,u,thr,reset){
  const sev=u>=1?['var(--crit)',' &#10005; limit reached']:u>=thr?['var(--warn)',' &#9888; past switch point']:['var(--s1)',''];
  return `<div class="meter-row"><span class="muted">${label}</span><div class="meter"><i style="width:${Math.min(100,u*100)}%;background:${sev[0]}"></i><span class="tick" style="left:${thr*100}%"></span></div>`
    +`<span><b>${Math.round(u*100)}%</b> <span class="muted small">${reset}</span><span class="small">${sev[1]}</span></span></div>`}
function spark(v){if(!v||!v.length)return '';const w=96,h=24,m=Math.max(...v)||1,y=x=>(h-2-x/m*(h-4)).toFixed(1);
  return `<svg width="${w}" height="${h}"><polyline points="${v.map((x,i)=>(i/(v.length-1)*w).toFixed(1)+','+y(x)).join(' ')}" fill="none" stroke="var(--muted)" stroke-width="1.5"/><circle cx="${w}" cy="${y(v[v.length-1])}" r="3" fill="var(--s1)"/></svg>`}
function weekly(p){
  if(!p||!p.series.length) return 'collecting data';
  const W=1100,Hh=270,L=48,R=12,T=14,B=26, now=p.series[p.series.length-1][0], x0=now-14*D, x1=now+7*D;
  const all=p.series.concat(p.projection.slice(1)), last=p.series[p.series.length-1];
  const ymax=Math.ceil(Math.max(100,...all.map(q=>q[2]))*1.08/100)*100;
  const X=t=>L+(Math.max(t,x0)-x0)/(x1-x0)*(W-L-R), Y=v=>T+(1-v/ymax)*(Hh-T-B);
  const path=(a,i)=>a.map((q,k)=>(k?'L':'M')+X(q[0]).toFixed(1)+','+Y(q[i]).toFixed(1)).join('');
  WK={all,now,x0,x1,L,R,W};
  if(showTable){let h='<table><tr><th>Time</th><th>Usage</th><th>Limit</th><th>Headroom</th></tr>';
    for(const q of all.filter((q,i)=>i%48===0||i===all.length-1)) h+=`<tr><td>${esc(when(q[0]))}${q[0]>now?' (projected)':''}</td><td>${Math.round(q[1])}%</td><td>${Math.round(q[2])}%</td><td>${Math.round(q[2]-q[1])}%</td></tr>`;
    return h+'</table>'+sentence(p,now)}
  let s=`<svg viewBox="0 0 ${W} ${Hh}" width="100%"><rect x="${X(now)}" y="${T}" width="${X(x1)-X(now)}" height="${Hh-T-B}" fill="#fff" opacity=".025"/>`, from=null;
  const band=(a,b)=>`<rect class="hit" x="${X(a)}" y="${T}" width="${(X(b)-X(a)).toFixed(1)}" height="${Hh-T-B}" fill="var(--crit)" opacity=".2"/>`;
  for(const q of all){const hit=q[2]-q[1]<=0.1;  // every account at its weekly limit: usage has met the limit line
    if(hit&&from==null) from=q[0]; else if(!hit&&from!=null){s+=band(from,q[0]);from=null}}
  if(from!=null) s+=band(from,all[all.length-1][0]);
  for(let v=0;v<=ymax;v+=100) s+=`<line x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v?'var(--grid)':'var(--axis)'}"/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end">${v}%</text>`;
  for(let t=Math.ceil(x0/D/2)*2*D;t<x1;t+=2*D) s+=`<text x="${X(t)}" y="${Hh-8}" text-anchor="middle">${esc(day(t))}</text>`;
  s+=`<line x1="${X(now)}" x2="${X(now)}" y1="${T}" y2="${Hh-B}" stroke="var(--ink2)" opacity=".5"/><text x="${X(now)+4}" y="${T+10}">now</text><text x="${X(x1)-4}" y="${T+10}" text-anchor="end">projected</text>`
    +`<path d="${path(p.series,1)}L${X(now)},${Y(0)}L${X(p.series[0][0])},${Y(0)}Z" fill="var(--s1)" opacity=".1"/>`
    +`<path d="${path(p.series,2)}" fill="none" stroke="var(--s2)" stroke-width="2" stroke-linejoin="round"/><path d="${path(p.series,1)}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>`
    +`<path d="${path(p.projection,2)}" fill="none" stroke="var(--s2)" stroke-width="2" stroke-dasharray="6 5" opacity=".8"/><path d="${path(p.projection,1)}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-dasharray="6 5" opacity=".8"/>`;
  for(let i=1;i<all.length;i++){const up=all[i][2]-all[i-1][2]; if(up>=1) s+=`<text x="${X(all[i][0])}" y="${Y(all[i][2])-6}" text-anchor="middle">+${Math.round(up)}</text>`}  // a weekly reset gave quota back
  for(const [i,c,dy] of [[1,'var(--s1)',18],[2,'var(--s2)',-9]])  // usage label below its dot, limit above
    s+=`<circle cx="${X(now)}" cy="${Y(last[i])}" r="4.5" fill="${c}" stroke="var(--surface)" stroke-width="2"/><text x="${X(now)-8}" y="${Y(last[i])+dy}" text-anchor="end" style="fill:var(--ink)">${Math.round(last[i])}%</text>`;
  return s+`<line id="xh" x1="0" x2="0" y1="${T}" y2="${Hh-B}" stroke="var(--ink2)" opacity="0"/></svg>`+sentence(p,now)}
function sentence(p,now){
  const rate=p.rate_per_day==null?'':`At the current rate (${p.rate_per_day}% of a weekly quota per day), `;
  if(p.eta) return `<div class="sentence">${rate}every account ${p.eta<=now+60?'is at 100% now':'is spent at <b>'+esc(when(p.eta))+'</b>'}; quota returns at ${esc(when(p.back))} when ${esc(p.back_account)} resets.</div>`;
  return `<div class="sentence">${p.note?esc(p.note):rate+'no shortfall in the next 7 days.'}</div>`}
$('weekly').onpointermove=e=>{
  const svg=e.currentTarget.querySelector('svg'); if(!svg||!WK) return;
  const r=svg.getBoundingClientRect(), t=WK.x0+((e.clientX-r.left)/r.width*WK.W-WK.L)/(WK.W-WK.L-WK.R)*(WK.x1-WK.x0);
  let q=WK.all[0]; for(const p of WK.all) if(Math.abs(p[0]-t)<Math.abs(q[0]-t)) q=p;
  const xh=svg.querySelector('#xh'), x=WK.L+(Math.max(q[0],WK.x0)-WK.x0)/(WK.x1-WK.x0)*(WK.W-WK.L-WK.R);
  xh.setAttribute('x1',x); xh.setAttribute('x2',x); xh.setAttribute('opacity','.6');
  const tip=$('tip'); tip.replaceChildren();
  const head=document.createElement('div'); head.className='muted'; head.textContent=when(q[0])+(q[0]>WK.now?' · projected':''); tip.append(head);
  for(const [k,v,c] of [['Usage',q[1],'var(--s1)'],['Limit',q[2],'var(--s2)'],[q[2]-q[1]<=0.1?'Blocked':'Headroom',q[2]-q[1],'']]){
    const row=document.createElement('div'), a=document.createElement('span'), b=document.createElement('span');
    row.className='row'; b.className='v'; b.textContent=Math.round(v)+'%';
    if(c){const key=document.createElement('span'); key.className='key'; key.style.borderColor=c; a.append(key)} a.append(k); row.append(a,b); tip.append(row)}
  tip.style.display='block'; tip.style.left=Math.min(e.clientX-r.left+30,r.width-160)+'px'};
$('weekly').onpointerleave=()=>{$('tip').style.display='none'; const xh=document.querySelector('#xh'); if(xh) xh.setAttribute('opacity','0')};
$('tbl').onclick=()=>{showTable=!showTable; $('tbl').textContent=showTable?'Chart':'Table'; refresh()};
$('period').onclick=e=>{const p=e.target.dataset&&e.target.dataset.p; if(p){period=p; refresh()}};
async function refresh(){
  const [st, stats] = await Promise.all([
    fetch('/rotate/status',{headers:KEY?{Authorization:'Bearer '+KEY}:{},cache:'no-store'}).then(r=>r.json()),
    fetch('/rotate/stats',{headers:KEY?{Authorization:'Bearer '+KEY}:{},cache:'no-store'}).then(r=>r.json())]);
  const per=stats.periods||{}, wk=st.weekly||{}, accts=Object.entries(st.accounts||{});
  $('host').textContent=location.host||'';
  $('pills').innerHTML=`<span class="pill"><span class="dot" style="background:var(--good)"></span>active: ${esc(st.active_account)}</span> <span class="pill">${esc(st.strategy)} &middot; ${accts.length} accounts &middot; ${Object.keys(stats.devices||{}).length} devices</span>`;
  $('alerts').innerHTML=(stats.alerts||[]).map(a=>`<div class="banner ${a.level==='warn'?'warn':''}">&#9888; <span><b>${a.level==='warn'?'Warning':'Note'}</b> ${esc(a.text)}</span></div>`).join('');
  // headline figures
  const last=(wk.series||[]).slice(-1)[0], now=last?last[0]:Date.now()/1000;
  $('hero').textContent=wk.eta?(wk.eta<=now+60?'now':dur(wk.eta-now)):wk.rate_per_day!=null?'> 7 days':'–';
  $('hero-sub').textContent=wk.eta?`${when(wk.eta)} · quota returns ${when(wk.back)} when ${wk.back_account} resets`:(wk.note||'no shortfall projected');
  $('k-left').textContent=last?Math.round(last[2]-last[1])+'%':'–'; $('k-left-sub').textContent=`of ${wk.capacity||0}% · ${accts.length} accounts`;
  $('k-rate').textContent=wk.rate_per_day!=null?wk.rate_per_day+'%':'–';
  $('k-req').textContent=fmt(sum(stats.by_device,b=>b.requests)); $('k-req-sub').textContent=fmt(sum(stats.by_device,tok))+' tokens';
  $('k-cost').textContent=money(sum((per['7d']||{}).model,b=>b.cost_usd)); $('k-cost-sub').textContent=money(sum(stats.by_model,b=>b.cost_usd))+' in the last 24 h';
  // accounts
  $('accounts').innerHTML=accts.map(([name,a])=>{const u5=a.effective_util_5h||0, u7=a.effective_util_7d||0;
    const seen=a.last_seen?ago(Math.round((Date.now()/1000-a.last_seen)/60)):'never';
    return `<div class="card"><div class="acct-head"><b>${esc(name)}</b>${name===st.active_account?'<span class="badge"><span class="dot" style="background:var(--good)"></span>active</span>':''}`
      +(a.disabled?'<span class="badge">disabled</span>':'')+(a.priority!==100?`<span class="badge">prio ${esc(a.priority)}</span>`:'')
      +`<span class="muted small" style="margin-left:auto">upstream: ${esc(a.status||'n/a')} &middot; seen ${seen}</span></div>`
      +meter('5h',u5,st.switch_threshold,a.reset_5h_in_min>0?'resets in '+dur(a.reset_5h_in_min*60):'window idle')
      +meter('7d',u7,st.switch_threshold_7d,a.reset_7d_in_h>0?'resets in '+dur(a.reset_7d_in_h*H):'window idle')+'</div>'}).join('');
  $('weekly').innerHTML=weekly(wk);
  // devices: configured ones, then removed ones that still have history
  const roster=stats.devices||{}, gone=Object.keys((per.all||{}).device||{}).filter(n=>!(n in roster)&&n!=='?');
  let dh=`<tr><th>Device</th><th class="l">Status</th><th class="l">Last model</th><th>24 h</th><th>7 d</th><th>30 d</th><th>All time${per.since?' <span class="muted">since '+esc(per.since)+'</span>':''}</th><th>30-day trend</th></tr>`;
  for(const name of Object.keys(roster).concat(gone)){const d=roster[name], removed=!d;
    const status=removed?'removed':d.online?'<span class="dot" style="background:var(--good)"></span>online':'<span class="dot" style="background:var(--muted)"></span>idle';
    dh+=`<tr class="${removed?'removed':''}"><td><b>${esc(name)}</b>${d?'<span class="sub">key '+esc(d.key_suffix)+'</span>':''}</td><td class="l">${status}${d?'<span class="sub">'+ago(d.last_seen_min)+'</span>':''}</td><td class="l">${esc(d&&d.last_model||'–')}</td>`
      +cell((stats.by_device||{})[name])+cell(((per['7d']||{}).device||{})[name])+cell(((per['30d']||{}).device||{})[name])+cell(((per.all||{}).device||{})[name])+`<td>${spark((per.trend||{})[name])}</td></tr>`}
  $('devices').innerHTML=dh;
  // models over the chosen period; accounts over 24 h
  $('period').innerHTML=Object.keys(PERIODS).map(p=>`<button data-p="${p}" class="${p===period?'on':''}">${p}</button>`).join('');
  const rows=o=>Object.entries(o||{}).sort((a,b)=>(b[1].cost_usd||0)-(a[1].cost_usd||0)).map(([n,b])=>{const prompt=(b.input_tokens||0)+(b.cache_read||0)+(b.cache_write||0);
    return `<tr><td>${esc(n)}</td><td>${fmt(b.requests)}</td><td>${fmt(tok(b))}</td><td>${prompt?Math.round((b.cache_read||0)/prompt*100):0}%</td><td>${money(b.cost_usd)}</td></tr>`}).join('');
  const head='<tr><th>{}</th><th>Requests</th><th>Tokens</th><th>From cache</th><th>$ equiv</th></tr>';
  $('models').innerHTML=head.replace('{}','Model')+rows(PERIODS[period]==='24h'?stats.by_model:(per[PERIODS[period]]||{}).model);
  $('by-account').innerHTML=head.replace('{}','Account')+rows(stats.by_account);
  $('events').innerHTML=(st.recent_events||[]).slice().reverse().map(e=>`<div><span class="muted">${esc(String(e.ts||'').slice(5,16).replace('T',' '))}</span><span>${esc(e.from)} &rarr; ${esc(e.to)}</span><span class="badge" style="margin-left:auto">${esc(e.reason)}</span></div>`).join('')||'<span class="muted">none</span>';
  $('upd').textContent='updated '+new Date().toLocaleTimeString();
}
refresh(); setInterval(refresh, 10000);
</script></body></html>"""


@app.get("/rotate/panel")
async def rotate_panel(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key — open /rotate/panel?key=<device key>"},
                            status_code=401)
    return Response(content=PANEL_HTML, media_type="text/html")


async def retry_wait(seconds: float, request: Request | None) -> None:
    """Release held request bodies promptly when their caller has left."""
    deadline = time.monotonic() + seconds
    while True:
        if request is not None and await request.is_disconnected():
            raise HTTPException(status_code=499, detail="client disconnected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 1.0) if request is not None else remaining)


async def send_upstream(method: str, url: str, fwd_headers: dict, body: bytes,
                        request: Request | None = None):
    """The rotation loop shared by the transparent proxy and the OpenAI endpoint:
    pick the active account, pace bursts, retry through switches, optionally hold
    until the soonest window reset. Returns (upstream, account, retries, noted,
    verdict) — noted means note_response already ran for the returned response;
    verdict is note_response's word for the LAST 429 seen ("burst" / "switched" /
    "exhausted" / None)."""
    hold_deadline = time.time() + HOLD_MAX_S if HOLD_MAX_S else None
    retries = 0
    noted = False
    verdict = None
    while True:
        await retry_wait(0, request)
        account = STATE.get("active_account")
        if account not in ACCOUNTS or disabled(account):
            account = pick_account()
            if account is None:
                raise HTTPException(status_code=503, detail="no enabled accounts")
            STATE["active_account"] = account
            save_state()
        fwd_headers["authorization"] = f"Bearer {ACCOUNTS[account]['token']}"
        pace = STATE["accounts"].get(account, {}).get("pace_until", 0) - time.time()
        if pace > 0:
            await retry_wait(min(pace, 60), request)
        upstream = await client.send(
            client.build_request(method, url, headers=fwd_headers, content=body),
            stream=True)
        if upstream.status_code != 429:
            return upstream, account, retries, noted, verdict
        verdict = await note_response(account, upstream.headers, 429)
        wait = 0.0
        retry = verdict in ("switched", "burst") and retries < 5
        if not retry and verdict == "exhausted" and hold_deadline:
            # Every account is spent: hold the request open until the soonest
            # window reset instead of failing, so unattended runs finish.
            wait = min(earliest_relief() + 5, hold_deadline) - time.time()
            retry = wait > 0
        if not retry:
            noted = True
            return upstream, account, retries, noted, verdict
        await bounded_response(upstream)
        retries += 1
        if wait > 0:
            async with state_lock:
                STATE["events"].append({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "hold",
                    "from": account, "to": account,
                    "reason": f"all accounts spent; holding {round(wait)}s"})
                STATE["events"] = STATE["events"][-200:]
                save_state()
            await retry_wait(wait, request)


@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """OpenAI-compatible front door (Phase 3): any OpenAI-format client or router
    rides the rotated Max capacity through here. Translation to /v1/messages with
    the oauth beta header; NO client headers are forwarded, so caller identity
    stops at this proxy — upstream sees one consumer per device key. When every
    account's window is spent this returns 503 + Retry-After (a breaker-friendly
    signal for routers), unlike the transparent proxy below, which stays
    transparent because Claude Code handles Anthropic's own 429s natively."""
    device = device_from_request(request)
    if device is None:
        return JSONResponse({"error": {"message": "claude-rotate: unknown device key",
                                       "type": "authentication_error"}}, status_code=401)
    try:
        oai = json.loads(await bounded_body(request))
    except (ValueError, RecursionError):
        return JSONResponse({"error": {"message": "body is not valid JSON",
                                       "type": "invalid_request_error"}}, status_code=400)
    abody, err = oai_to_anthropic(oai)
    if err:
        return JSONResponse({"error": {"message": err, "type": "invalid_request_error"}},
                            status_code=400)
    fwd = {"content-type": "application/json", "accept-encoding": "identity",
           "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}
    upstream, account, retries, noted, verdict = await send_upstream(
        "POST", "/v1/messages", fwd, json.dumps(abody).encode(), request)

    rec = {"device": device, "account": account, "path": "/v1/chat/completions", "method": "POST",
           "model": abody.get("model"), "status": upstream.status_code}
    if retries:
        rec["retries"] = retries
    resp_headers = {"x-rotate-account": account}
    for h in ("anthropic-ratelimit-unified-5h-utilization",
              "anthropic-ratelimit-unified-7d-utilization",
              "anthropic-ratelimit-unified-5h-reset",
              "anthropic-ratelimit-unified-status"):
        if upstream.headers.get(h):
            resp_headers[h] = upstream.headers[h]

    if upstream.status_code == 429 and verdict == "exhausted":
        await bounded_response(upstream)
        retry_after = max(1, round(earliest_relief() - time.time()))
        rec["status"] = 503
        audit(rec)
        return JSONResponse(
            {"error": {"message": "claude-rotate: every account's window is spent; "
                                  f"retry in {retry_after}s",
                       "type": "overloaded_error"}},
            status_code=503, headers=resp_headers | {"retry-after": str(retry_after)})

    if abody.get("stream") and upstream.status_code == 200 \
            and "text/event-stream" in upstream.headers.get("content-type", ""):
        tr = SseToOai()

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    out = tr.feed(chunk.decode("utf-8", errors="replace"))
                    if out:
                        yield out.encode()
                yield tr.tail().encode()
            finally:
                await upstream.aclose()
                rec["usage"] = summarize_usage(tr.usage)
                rec["util_5h"] = upstream.headers.get(
                    "anthropic-ratelimit-unified-5h-utilization")
                audit(rec)
                await note_response(account, upstream.headers, upstream.status_code)

        return StreamingResponse(relay(), status_code=200, headers=resp_headers,
                                 media_type="text/event-stream")

    content = await bounded_response(upstream)
    if not noted:
        await note_response(account, upstream.headers, upstream.status_code)
    rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
    if upstream.status_code != 200:
        try:
            msg = json.loads(content).get("error", {}).get("message") \
                or content[:200].decode(errors="replace")
        except (json.JSONDecodeError, AttributeError):
            msg = content[:200].decode(errors="replace")
        audit(rec)
        return JSONResponse({"error": {"message": f"upstream: {msg}",
                                       "type": "api_error"}},
                            status_code=upstream.status_code, headers=resp_headers)
    data = json.loads(content)
    rec["usage"] = summarize_usage(data.get("usage"))
    audit(rec)
    return JSONResponse(anthropic_to_oai(data), headers=resp_headers)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str):
    device = device_from_request(request)
    if device is None:
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error",
                                        "message": "claude-rotate: unknown device key"}},
            status_code=401)

    body = await bounded_body(request)
    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP | PRIVATE_HEADERS
                   and not k.lower().startswith(("tailscale-", "x-forwarded-", "sec-fetch-"))}
    fwd_headers["accept-encoding"] = "identity"
    fwd_headers.pop("x-api-key", None)

    model = None
    if body and request.method == "POST":
        try:
            model = json.loads(body).get("model")
            if not isinstance(model, str):
                model = None
        except (ValueError, RecursionError, AttributeError):
            pass

    # Preserve raw upstream query spelling/order while removing local credentials.
    try:
        query = request.scope["query_string"].decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="query string must use percent-encoding")
    query = "&".join(part for part in query.split("&")
                     if unquote_plus(part.partition("=")[0]) != "key")
    url = "/" + path + ("?" + query if query else "")
    upstream, account, retries, noted, _verdict = await send_upstream(
        request.method, url, fwd_headers, body, request)

    rec = {"device": device, "account": account, "path": "/" + path, "method": request.method,
           "model": model, "status": upstream.status_code}
    if retries:
        rec["retries"] = retries
    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP and k.lower() != "content-encoding"}
    resp_headers["x-rotate-account"] = account

    if "text/event-stream" in upstream.headers.get("content-type", ""):
        usage_acc: dict = {}

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    usage_from_sse(chunk.decode("utf-8", errors="replace"), usage_acc)
                    yield chunk
            finally:
                await upstream.aclose()
                rec["usage"] = summarize_usage(usage_acc)
                rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
                audit(rec)
                await note_response(account, upstream.headers, upstream.status_code)

        return StreamingResponse(relay(), status_code=upstream.status_code,
                                 headers=resp_headers,
                                 media_type=upstream.headers.get("content-type"))

    content = await bounded_response(upstream)
    try:
        rec["usage"] = summarize_usage(json.loads(content).get("usage"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
    audit(rec)
    if not noted:
        await note_response(account, upstream.headers, upstream.status_code)
    return Response(content=content, status_code=upstream.status_code, headers=resp_headers)


if __name__ == "__main__":
    uvicorn.run(app, host=CFG.get("bind", "127.0.0.1"), port=CFG.get("port", 8484),
                log_level="warning")
