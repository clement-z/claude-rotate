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
     daily totals per device and model for its 7-day, 30-day and all-time views,
     and per-account daily totals for each subscription's billing periods.

GET /rotate/status (device-key auth) returns live state for the admin page;
POST /rotate/switch lets the panel select or rotate the active account.

Other providers (providers.py) sit under their own path prefix, such as /openai/v1/...
for a Sign in with ChatGPT credential, and are forwarded without rotation or pacing.
A device key given as a string reaches the Claude accounts only; an object
{"key": ..., "providers": [...]} names what the device may reach.
"""

import asyncio
import json
import os
import re
import stat
import sys
import tempfile
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, unquote_plus

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import MutableHeaders

import pricing
import providers
from oai_compat import SseToOai, anthropic_to_oai, claude_usage as summarize_usage, oai_to_anthropic
from pricing import BUILTIN_PRICES, DEFAULT_PRICE
from providers import ANTHROPIC, HISTORY_DAYS

ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CLAUDE_ROTATE_CONFIG", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("CLAUDE_ROTATE_STATE", ROOT / "state.json"))
AUDIT_PATH = Path(os.environ.get("CLAUDE_ROTATE_AUDIT", ROOT / "logs" / "audit.jsonl"))

UPSTREAM = "https://api.anthropic.com"
HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
}
# These belong to the local browser or reverse-proxy trust boundary.
# x-openai-actor-authorization is a switch between Codex and this gateway (it turns on
# Codex's image tool for a custom provider); an upstream could read it as a credential.
PRIVATE_HEADERS = {"cookie", "referer", "origin", "forwarded", "x-real-ip", "x-openai-actor-authorization"}

@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(refresh_loop()), asyncio.create_task(providers.maintain_loop(PROVIDERS))]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        save_quota_history(time.time())
        save_ratelimit_headers()


app = FastAPI(lifespan=lifespan)
# Keep up to 64 idle upstream connections, one per admission slot, so streams that
# start together reuse connections instead of each opening a new TLS session.
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0, connect=15.0),
                           limits=httpx.Limits(max_connections=128, max_keepalive_connections=64),
                           cookies=providers.NO_COOKIES)
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
    configured = {acct["name"] for acct in cfg["accounts"]}
    # The example config ships a placeholder device key that is PUBLIC (it's in
    # the repo). It must never be accepted as a live credential: refuse to start
    # while one remains, so add-account-without-add-device can't open the proxy
    # to anyone who read the example (GitHub issue #2).
    devices = cfg.get("devices")
    if not isinstance(devices, dict):
        raise SystemExit("devices must map names to nonempty device keys")
    grants, pins = {}, {}
    for name, entry in devices.items():
        if isinstance(entry, dict):
            if "account_pin" in entry:
                pin = entry["account_pin"]
                if not valid_account_pin(pin, configured):
                    raise SystemExit(f"device '{name}': account_pin needs a configured account and mode strict or fallback")
                pins[name] = pin
            names = entry.get("providers", [ANTHROPIC] if name in pins else None)
            if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names) \
                    or set(entry) - {"key", "providers", "account_pin"}:
                raise SystemExit(f"device '{name}': an object needs \"key\" and a nonempty \"providers\" "
                                 "list unless it has an account_pin")
            if name in pins and ANTHROPIC not in names:
                raise SystemExit(f"device '{name}': account_pin requires the anthropic grant")
            grants[name], entry = names, entry.get("key")
        if not isinstance(entry, str) or not entry.strip():
            raise SystemExit("devices must map names to nonempty device keys")
        devices[name] = entry
    cfg["device_grants"] = grants
    if len(set(devices.values())) != len(devices):
        raise SystemExit("device keys must be unique")
    bad = [d for d, k in devices.items() if "REPLACE-WITH" in k]
    if bad:
        raise SystemExit(
            f"placeholder device key(s) in config.json: {', '.join(bad)} — the example "
            "value is public and must never be a valid credential. "
            "Run: ./setup.sh add-device <name> (it replaces the placeholder).")
    loaded = []
    for acct in cfg["accounts"]:
        w = acct.get("weight", 1)
        if type(w) not in (int, float) or not w > 0:
            raise SystemExit(f"account '{acct['name']}': weight must be a number > 0")
        path = CONFIG_PATH.parent / acct["token_file"]
        if path.exists():
            token = path.read_text().strip()
            if token:
                acct["token"] = token
                loaded.append(acct)
            else:
                print(f"warning: skipping account '{acct['name']}' — {acct['token_file']} is empty", file=sys.stderr)
        else:
            print(f"warning: skipping account '{acct['name']}' — {acct['token_file']} not found",
                  file=sys.stderr)
    if not loaded:
        raise SystemExit("no accounts with tokens configured — run: ./setup.sh add-account <name>")
    cfg["accounts"], cfg["configured_accounts"], cfg["account_pins"] = loaded, configured, pins
    return cfg


def valid_account_pin(pin, accounts) -> bool:
    return (isinstance(pin, dict) and set(pin) == {"account", "mode"}
            and isinstance(pin["account"], str) and pin["account"] in accounts
            and pin["mode"] in ("strict", "fallback"))


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
MAX_SEARCH_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = resource_limit("max_concurrent_requests", 0, allow_zero=True)
# Under a provider with "images": true, request bodies may reach max_image_request_bytes:
# an edit carries reference images, and every request after a generation carries the
# image back to the model. Bodies above max_request_bytes share max_image_bytes_in_flight,
# since each stays in memory until its answer ends.
MAX_IMAGE_REQUEST_BYTES = resource_limit("max_image_request_bytes", 32 * 1024 * 1024)
MAX_IMAGE_BYTES_IN_FLIGHT = resource_limit("max_image_bytes_in_flight", 256 * 1024 * 1024)
MAX_CONCURRENT_IMAGES = resource_limit("max_concurrent_images", 2)
IMAGE_LINE_MAX = 8  # image requests that may wait for one of those slots
AUDIT_CACHE_RECORDS = resource_limit("audit_cache_records", 100_000)
AUDIT_CACHE_BYTES = resource_limit("audit_cache_bytes", 32 * 1024 * 1024)
AUDIT_MAX_BYTES = resource_limit("audit_max_bytes", 16 * 1024 * 1024, minimum=4096)
AUDIT_BACKUPS = resource_limit("audit_backups", 4)


class RequestAdmission:
    """Reject overload before body buffering; retain slots through SSE/holds."""

    def __init__(self, app, limit: int):
        self.app, self.limit, self.active = app, limit, 0

    async def __call__(self, scope, receive, send):
        admin = (scope.get("method"), scope.get("path")) in {
            ("GET", "/rotate/status"), ("GET", "/rotate/stats"), ("GET", "/rotate/panel"),
            ("GET", "/rotate/quota-history"), ("GET", "/rotate/ratelimits"),
            ("POST", "/rotate/switch"), ("POST", "/rotate/device-pin")}
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
PROVIDERS = providers.load_providers(CFG, CONFIG_PATH.parent, STATE_PATH.parent)
PREFIXES = {p.prefix[1:]: p for p in PROVIDERS.values()}
for _device, _names in CFG.get("device_grants", {}).items():
    if set(_names) - {ANTHROPIC} - set(PROVIDERS):
        raise SystemExit(f"device '{_device}': unknown provider(s) "
                         + ", ".join(sorted(set(_names) - {ANTHROPIC} - set(PROVIDERS))))
DEVICE_GRANTS = {d: frozenset(n) for d, n in CFG.get("device_grants", {}).items()}
DEVICE_PIN_OVERRIDES: dict = {}


def granted(device: str, provider: str) -> bool:
    return provider in DEVICE_GRANTS.get(device, (ANTHROPIC,))


def device_pin(device: str) -> tuple[dict | None, str]:
    if device in DEVICE_PIN_OVERRIDES:
        return DEVICE_PIN_OVERRIDES[device], "runtime"
    pin = CFG["account_pins"].get(device)
    return pin, "config" if pin else "automatic"


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


STATE_VERSION = 1
MODEL_FAMILIES = ("default", "fable")
GENERAL_CLAIMS = ("5h", "7d")
KNOWN_CLAIMS = {"5h": MODEL_FAMILIES, "7d": MODEL_FAMILIES, "7d_oi": ("fable",)}
CLAIM_LABELS = {"5h": "5h", "7d": "7d", "7d_oi": "Fable 7d"}
CLAIM_HEADER = re.compile(r"anthropic-ratelimit-unified-([a-z0-9][a-z0-9_-]{0,31})-(utilization|status|reset)")
CLAIM_STATUS = re.compile(r"[a-z][a-z0-9_-]{0,31}")
MAX_LEARNED_CLAIMS = 29


def model_family(model) -> str:
    return "fable" if isinstance(model, str) and "fable" in model.lower() else "default"


def legacy_claims(info: dict) -> dict:
    claims = {}
    for claim in GENERAL_CLAIMS:
        reading = {}
        for field, prefix in (("utilization", "util"), ("reset", "reset")):
            value = info.get(f"{prefix}_{claim}")
            if providers.number(value) and (value > 0 if field == "reset" else value >= 0):
                reading[field] = value
        if reading:
            if providers.number(info.get("last_seen")):
                reading["seen_at"] = info["last_seen"]
            claims[claim] = reading
    return claims


def sync_legacy_claims(info: dict) -> None:
    # The old routing and offline admin still read these fields.
    for claim in GENERAL_CLAIMS:
        reading = info["claims"].get(claim, {})
        for field, prefix in (("utilization", "util"), ("reset", "reset")):
            key = f"{prefix}_{claim}"
            if field in reading:
                info[key] = reading[field]
            else:
                info.pop(key, None)


def seed_routes(state: dict) -> None:
    active = state.get("active_account")
    if active not in ACCOUNTS:
        active = next(iter(ACCOUNTS))
    manual = state.get("pinned")
    if manual != active or ACCOUNTS[active].get("disabled"):
        manual = None
    switched_at = state.get("last_switch_ts")
    if not providers.number(switched_at) or switched_at < 0:
        switched_at = 0
    state["last_switch_ts"] = switched_at
    state["routes"] = {family: {"active_account": active, "last_switch_ts": switched_at,
                                "manual_account": manual} for family in MODEL_FAMILIES}


def write_state(state: dict) -> None:
    # Write-then-rename so a crash mid-write can never leave a truncated
    # state.json (which would refuse to parse on the next start).
    tmp = STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_PATH)
    except OSError as exc:
        storage_error("state_write", exc)


def load_state() -> dict:
    exists = STATE_PATH.exists()
    state = json.loads(STATE_PATH.read_text()) if exists else {
        "active_account": next(iter(ACCOUNTS)),
        "accounts": {name: {} for name in ACCOUNTS},
        "events": [],
    }
    state.pop("latest_ratelimits", None)
    if "version" not in state:
        now = time.time()
        for info in state["accounts"].values():
            info["claims"] = legacy_claims(info)
            sync_legacy_claims(info)
            pace = info.get("pace_until")
            if providers.number(pace) and pace > now:
                info["pace_by_family"] = dict.fromkeys(MODEL_FAMILIES, pace)
            else:
                info.pop("pace_until", None)
            if info.get("status") == "rejected":
                info.pop("status")
        state["version"] = STATE_VERSION
        if "routes" not in state:
            seed_routes(state)
        if exists:
            write_state(state)
    elif state.get("version") == STATE_VERSION:
        repaired = False
        now = time.time()
        for info in state["accounts"].values():
            pace = info.get("pace_until")
            if "pace_until" in info and (not providers.number(pace) or pace <= now):
                del info["pace_until"]
                repaired = True
            if "pace_by_family" in info:
                stored = info["pace_by_family"]
                items = stored.items() if isinstance(stored, dict) else ()
                deadlines = {family: deadline for family, deadline in items
                             if family in MODEL_FAMILIES and providers.number(deadline) and deadline > now}
                if deadlines != stored:
                    info["pace_by_family"] = deadlines
                    repaired = True
        # The offline rotate command still writes the legacy default route.
        route = state["routes"]["default"]
        switched_at = state.get("last_switch_ts", 0)
        for family, entry in state["routes"].items():
            timestamp = entry.get("last_switch_ts")
            if not providers.number(timestamp) or timestamp < 0:
                entry["last_switch_ts"] = (switched_at if family == "default"
                                           and providers.number(switched_at) and switched_at >= 0 else 0)
                repaired = True
        if not providers.number(switched_at) or switched_at < 0:
            state["last_switch_ts"] = route["last_switch_ts"]
            repaired = True
        if state.get("active_account") in ACCOUNTS and (
                state["active_account"] != route["active_account"]
                or state.get("last_switch_ts", 0) != route["last_switch_ts"]
                or state.get("pinned") != route.get("manual_account")):
            seed_routes(state)
            repaired = True
        if repaired:
            write_state(state)
    return state


STATE = load_state()


def save_state() -> None:
    write_state(STATE)


def audit(rec: dict) -> None:
    if isinstance(model := rec.get("model"), str):
        rec["model"] = utf8_text(model)
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
    if len(line) > AUDIT_MAX_BYTES and ("ratelimit" in rec or "rate_limits" in rec):
        rec = {key: value for key, value in rec.items() if key != "ratelimit"}
        if "rate_limits" in rec:
            rec["rate_limits"] = [{key: value for key, value in entry.items() if key != "headers"}
                                  for entry in rec["rate_limits"]]
        rec["ratelimit_truncated"] = True
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


def account_claims(info: dict) -> dict:
    return info["claims"] if "claims" in info else legacy_claims(info)


def applicable_claims(info: dict, family: str) -> dict:
    learned = info.get("learned_claims", {}).get(family, ())
    return {claim: reading for claim, reading in account_claims(info).items()
            if family in KNOWN_CLAIMS.get(claim, ()) or claim in learned}


def claim_reading(name: str, claim: str) -> dict:
    return account_claims(STATE["accounts"].get(name, {})).get(claim, {})


def claim_utilization(name: str, claim: str) -> float | None:
    reading = claim_reading(name, claim)
    reset = reading.get("reset")
    if reset is not None and reset <= time.time():
        return None
    return reading.get("utilization")


def utilization(name: str) -> float:
    return claim_utilization(name, "5h") or 0.0


def util_7d(name: str) -> float:
    return claim_utilization(name, "7d") or 0.0


def priority(name: str) -> int:
    """Lower = preferred. Accounts without a priority share the default tier."""
    return ACCOUNTS[name].get("priority", 100)


PLANS = {"pro": {"price": 20, "weight": 0.2}, "max": {"price": 100, "weight": 1},
         "ultra": {"price": 200, "weight": 2}}  # USD list price a month; weekly quota vs Max
BILLING_ERRORS: list = []


def load_billing() -> dict:
    """Each account's plan timeline from config: [(start ts, start date, plan, restarts
    the billing cycle)]. A broken entry is reported in the panel and ignored: billing is
    analytics and must never stop the proxy."""
    import datetime as dt
    for name, plan in (CFG.get("plans") or {}).items():
        if (isinstance(plan, dict) and type(plan.get("price")) in (int, float) and plan["price"] >= 0
                and type(plan.get("weight")) in (int, float) and plan["weight"] > 0):
            PLANS[name] = plan
        else:
            BILLING_ERRORS.append(f"plan '{name}' needs a price >= 0 and a weight > 0")
    out = {}
    for name, acct in ACCOUNTS.items():
        if "billing" not in acct:
            continue
        timeline = []
        try:
            for e in acct["billing"]:
                start = dt.date.fromisoformat(e["from"])
                if e["plan"] not in PLANS or (timeline and start <= timeline[-1][1]):
                    raise ValueError
                timeline.append((time.mktime(start.timetuple()), start, e["plan"], e.get("renews") is True))
        except (TypeError, KeyError, ValueError, AttributeError):
            timeline = []
        if timeline:
            out[name] = timeline
        else:
            BILLING_ERRORS.append(f"account '{name}': billing needs entries with a 'from' date "
                                  f"(YYYY-MM-DD, increasing) and a plan ({', '.join(PLANS)})")
    for error in BILLING_ERRORS:
        print(f"warning: billing ignored: {error}", file=sys.stderr)
    return out


BILLING = load_billing()


def plan_at(name: str, ts: float):
    """The billing entry in force at ts (the first one before it starts), or None."""
    timeline = BILLING.get(name)
    if not timeline:
        return None
    return next((e for e in reversed(timeline) if e[0] <= ts), timeline[0])


def weight(name: str, ts: float | None = None) -> float:
    """Weekly capacity relative to a Max account (Ultra 2, Pro 0.2): the plan in force
    at ts when the account has billing, else accounts[].weight."""
    entry = plan_at(name, time.time() if ts is None else ts)
    return PLANS[entry[2]]["weight"] if entry else ACCOUNTS[name].get("weight", 1)


def disabled(name: str) -> bool:
    return bool(ACCOUNTS[name].get("disabled"))


RECHECK_S = 300
LAST_BLOCKED_ACCOUNT: dict[str | None, str] = {}


def family_route(family: str) -> dict:
    if "routes" not in STATE:
        seed_routes(STATE)
    return STATE["routes"][family]


def project_default_route() -> None:
    route = family_route("default")
    STATE.update(active_account=route["active_account"], last_switch_ts=route["last_switch_ts"])
    if route.get("manual_account"):
        STATE["pinned"] = route["manual_account"]
    else:
        STATE.pop("pinned", None)


def claim_readings(name: str, family: str) -> dict:
    return applicable_claims(STATE["accounts"].get(name, {}), family)


def current_util(reading: dict, now: float) -> float | None:
    reset = reading.get("reset")
    return None if reset is not None and reset <= now else reading.get("utilization")


def claim_block_until(reading: dict) -> float:
    if reading.get("status") == "rejected" or reading.get("utilization", 0) >= 1:
        return max(reading.get("reset", 0), reading.get("recheck_at", 0))
    return 0


def blocker_deadline(name: str, family: str, now: float) -> float:
    deadline = STATE["accounts"].get(name, {}).get("blocks_by_family", {}).get(family, {}).get("until", 0)
    return max(deadline, max(map(claim_block_until, claim_readings(name, family).values()), default=0))


def claim_thresholds() -> dict:
    return {"5h": THRESHOLD, "7d": THRESHOLD_7D, "7d_oi": THRESHOLD_7D}


def soft_limited(name: str, family: str, now: float) -> bool:
    thresholds = claim_thresholds()
    return any((current_util(reading, now) or 0) >= thresholds.get(claim, float("inf"))
               for claim, reading in claim_readings(name, family).items())


def usable(name: str, family: str = "default") -> bool:
    now = time.time()
    return not disabled(name) and blocker_deadline(name, family, now) <= now and not soft_limited(name, family, now)


def weekly_reset(name: str) -> float:
    return claim_reading(name, "7d").get("reset") or float("inf")


def earliest_relief(family: str = "default", now: float | None = None) -> float:
    now = time.time() if now is None else now
    return min((max(now, blocker_deadline(name, family, now)) for name in ACCOUNTS if not disabled(name)),
               default=now + RECHECK_S)


def next_blocked_account(family: str | None, enabled: list[str], now: float) -> str:
    previous = LAST_BLOCKED_ACCOUNT.get(family)
    if previous in enabled:
        account = enabled[(enabled.index(previous) + 1) % len(enabled)]
    else:
        account = min(enabled, key=lambda name: blocker_deadline(name, family or "default", now))
    LAST_BLOCKED_ACCOUNT[family] = account
    return account


def routing_util(name: str, family: str, now: float) -> float:
    readings = claim_readings(name, family)
    if (short := current_util(readings.get("5h", {}), now)) is not None:
        return short
    return max((current_util(reading, now) or 0 for reading in readings.values()), default=0)


def ordered_pick(pool: list, family: str, now: float) -> str | None:
    return min(pool, key=lambda name: (
        priority(name), weekly_reset(name) if STRATEGY == "consume-first" else 0,
        routing_util(name, family, now)), default=None)


def route_pool(family: str, now: float) -> list:
    return [name for name in ACCOUNTS if not disabled(name) and blocker_deadline(name, family, now) <= now]


def pick_account(family: str = "default") -> str | None:
    now = time.time()
    pool = route_pool(family, now)
    return ordered_pick([name for name in pool if not soft_limited(name, family, now)] or pool, family, now)


def switch_route(family: str, target: str, reason: str, now: float) -> None:
    route = family_route(family)
    previous = route["active_account"]
    route.update(active_account=target, last_switch_ts=now, manual_account=None)
    STATE["events"].append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "switch",
                            "family": family, "from": previous, "to": target, "reason": reason,
                            "util": routing_util(previous, family, now)})
    STATE["events"] = STATE["events"][-200:]
    if family == "default":
        project_default_route()


def select_account(family: str, now: float, *, update: bool = True) -> str | None:
    """Select an automatic account under the state lock."""
    route = family_route(family)
    current = route["active_account"]
    pool = route_pool(family, now)
    under = [name for name in pool if not soft_limited(name, family, now)]
    manual = route.get("manual_account") and current in under
    if update and route.get("manual_account") and not manual:
        route["manual_account"] = None
        if family == "default":
            project_default_route()
        save_state()
    if not pool:
        return None
    if current not in pool:
        target = ordered_pick(under or pool, family, now)
        reason = "account_disabled" if current not in ACCOUNTS or disabled(current) else "quota_exhausted"
    elif not under or manual or now - route["last_switch_ts"] < COOLDOWN_S:
        return current
    else:
        target = ordered_pick(under, family, now)
        if current not in under:
            readings = claim_readings(current, family)
            weekly_over = any((current_util(readings.get(claim, {}), now) or 0) >= THRESHOLD_7D
                              for claim in ("7d", "7d_oi"))
            if (not weekly_over and priority(target) >= priority(current)
                    and routing_util(target, family, now) > routing_util(current, family, now) - SWITCH_MARGIN):
                return current
            reason = f"utilization>={THRESHOLD}"
        elif priority(target) < priority(current):
            reason = "priority_recovery"
        elif STRATEGY == "consume-first" and weekly_reset(target) < weekly_reset(current) - CF_MARGIN_S:
            reason = "consume_first"
        else:
            return current
    if update and target != current:
        switch_route(family, target, reason, now)
        save_state()
    return target


def claim_field(field: str, value: str) -> float | str | None:
    if len(value) > 256:
        return None
    values = [part.strip().lower() for part in value.split(",")]
    if field == "status":
        if CLAIM_STATUS.fullmatch(values[0]) and all(part == values[0] for part in values):
            return values[0]
        return None
    try:
        numbers = [float(part) for part in values]
    except ValueError:
        return None
    number = numbers[0]
    if (providers.number(number) and (number > 0 if field == "reset" else number >= 0)
            and all(part == number for part in numbers)):
        return number
    return None


def parse_claims(headers: httpx.Headers, existing: dict) -> dict:
    claims = {}
    learned_count = 0
    fields = [(*match.groups(), value) for name, value in headers.items()
              if (match := CLAIM_HEADER.fullmatch(name.lower()))]
    for claim, field, value in sorted(fields):
        if claim == "overage" or claim.startswith(("overage-", "overage_")):
            continue
        if claim in KNOWN_CLAIMS or claim in existing:
            claims.setdefault(claim, {})
        parsed = claim_field(field, value)
        if parsed is None:
            continue
        if claim not in claims:
            if learned_count >= MAX_LEARNED_CLAIMS:
                continue
            learned_count += 1
        claims.setdefault(claim, {})[field] = parsed
    return claims


RATELIMIT_HEADERS_PATH = STATE_PATH.with_name("ratelimit_headers.json")
LATEST_RATELIMITS: dict = {}
RATELIMIT_HEADERS_DIRTY = False
MAX_RATELIMIT_MODELS = 16
RATELIMIT_HEADERS_MAX_BYTES = 8 * 1024 * 1024


def utf8_text(value: str, limit: int | None = None) -> str:
    value = value[:limit]
    return value if value.isascii() else value.encode("utf-8", errors="replace").decode("utf-8")


def snapshot_headers(headers: dict) -> dict:
    return {utf8_text(key, 100): utf8_text(value, 200)
            for key, value in list(headers.items())[:40] if isinstance(value, str)}


def remember_ratelimits(account: str, model, path: str, status: int, headers: dict, now: float) -> None:
    global RATELIMIT_HEADERS_DIRTY
    models = LATEST_RATELIMITS.setdefault(account, {})
    key = utf8_text(model, 256) if isinstance(model, str) else ""
    models.pop(key, None)
    models[key] = {"time": now, "status": status, "path": utf8_text(path, 1024),
                   "headers": snapshot_headers(headers)}
    while len(models) > MAX_RATELIMIT_MODELS:
        del models[next(iter(models))]
    RATELIMIT_HEADERS_DIRTY = True


def load_ratelimit_headers() -> None:
    global RATELIMIT_HEADERS_DIRTY
    try:
        with os.fdopen(os.open(RATELIMIT_HEADERS_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > RATELIMIT_HEADERS_MAX_BYTES:
                raise ValueError("invalid rate-limit snapshot file type or size")
            data = f.read(RATELIMIT_HEADERS_MAX_BYTES + 1)
        if len(data) > RATELIMIT_HEADERS_MAX_BYTES:
            raise ValueError("rate-limit snapshot too large")
        saved = json.loads(data)
        if not isinstance(saved, dict):
            raise ValueError("expected an account mapping")
    except FileNotFoundError:
        return
    except (OSError, ValueError, RecursionError) as exc:
        storage_error("ratelimit_headers_load", exc)
        return
    for name in CFG["configured_accounts"]:
        if not isinstance(models := saved.get(name), dict):
            continue
        valid = {}
        for model, row in models.items():
            if (not isinstance(row, dict) or not providers.number(row.get("time")) or row["time"] < 0
                    or type(row.get("status")) is not int or not 100 <= row["status"] <= 599
                    or not isinstance(row.get("path"), str) or not isinstance(row.get("headers"), dict)):
                continue
            valid[utf8_text(model, 256)] = dict(time=row["time"], status=row["status"],
                                                path=utf8_text(row["path"], 1024),
                                                headers=snapshot_headers(row["headers"]))
        if valid:
            LATEST_RATELIMITS[name] = dict(sorted(valid.items(), key=lambda pair: pair[1]["time"])[-MAX_RATELIMIT_MODELS:])
    RATELIMIT_HEADERS_DIRTY = saved != LATEST_RATELIMITS


def save_ratelimit_headers() -> None:
    global RATELIMIT_HEADERS_DIRTY
    if not RATELIMIT_HEADERS_DIRTY:
        return
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=RATELIMIT_HEADERS_PATH.parent,
                                         prefix=".ratelimit_headers.", suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            json.dump(LATEST_RATELIMITS, f)
        tmp.replace(RATELIMIT_HEADERS_PATH)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        storage_error("ratelimit_headers_write", exc)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        RATELIMIT_HEADERS_DIRTY = False


load_ratelimit_headers()


def audit_claude_answer(rec: dict, account: str, family: str | None, reported: dict, headers: dict) -> None:
    claims = {}
    unfamiliar = 0
    for claim, fields in sorted(reported.items()):
        if not fields:
            continue
        if claim not in KNOWN_CLAIMS:
            if unfamiliar >= MAX_LEARNED_CLAIMS:
                continue
            unfamiliar += 1
        claims[claim] = fields
    rec["util"] = {claim: fields["utilization"] for claim, fields in claims.items() if "utilization" in fields}
    claim_ids = sorted(claims)
    previous = STATE.setdefault("audit_claim_ids", {}).setdefault(account, {})
    scope = family or "non_inference"
    if model_family(rec["model"]) == "fable" or previous.get(scope, []) != claim_ids:
        rec["ratelimit"] = headers
    else:
        rec.pop("ratelimit", None)
    previous[scope] = claim_ids


def record_ratelimit_diagnostics(account, model, path, status, headers, at, *, rec=None, family=None, reported=None):
    if rec is not None:
        rec["util"] = {}
        rec.pop("ratelimit", None)
    try:
        raw = rate_limit_headers(headers)
        if rec is not None:
            audit_claude_answer(rec, account, family, reported, raw)
        remember_ratelimits(account, model, path, status, raw, at)
    except Exception as exc:
        storage_error("ratelimit_diagnostics", exc)


def observe_claims(info: dict, claims: dict, reported: dict, status: int, family: str | None, now: float,
                   *, retire_shared: bool) -> None:
    if not reported:
        return
    info["claims"] = claims
    learned = info.setdefault("learned_claims", {}) if family is not None else {}
    members = set(learned.get(family, ()))
    weekly = reported.get("7d", {})
    weekly_snapshot = "utilization" in weekly and weekly.get("reset", 0) > now
    if retire_shared and 200 <= status < 300 and weekly_snapshot and "5h" not in reported:
        claims.pop("5h", None)
    if family is not None and 200 <= status < 300:
        info.setdefault("last_success_claims", {})[family] = sorted(reported)
        if weekly_snapshot:
            info.get("blocks_by_family", {}).pop(family, None)
            if family == "fable" and "7d_oi" not in reported:
                claims.pop("7d_oi", None)
            for claim in members - reported.keys():
                if not any(claim in ids for other, ids in learned.items() if other != family):
                    claims.pop(claim, None)
            members.intersection_update(reported)
    count = sum(claim not in KNOWN_CLAIMS for claim in claims)
    for claim, fields in reported.items():
        if not fields or (claim == "7d_oi" and family != "fable"):
            continue
        if claim not in KNOWN_CLAIMS:
            if family is None or (claim not in claims and count >= MAX_LEARNED_CLAIMS):
                continue
            count += claim not in claims
            members.add(claim)
        reading = claims.setdefault(claim, {})
        if reading.get("reset", now + 1) <= now and (
                fields.get("reset", 0) > now or ("utilization" in fields and "reset" not in fields)):
            # A new window cannot inherit the old window's usage or rejection.
            reading.clear()
        if ("status" not in fields and 200 <= status < 300
                and fields.get("utilization", 1) < 1 and fields.get("reset", 0) > now):
            reading.pop("status", None)
        reading.update(fields, seen_at=now)
        fresh_exhaustion = fields.get("status") == "rejected" or ("reset" not in fields
                                                                  and fields.get("utilization", 0) >= 1)
        if fresh_exhaustion and reading.get("reset", 0) <= now:
            reading["recheck_at"] = now + RECHECK_S
        elif ((reading.get("status") != "rejected" and reading.get("utilization", 0) < 1)
              or reading.get("reset", 0) > now):
            reading.pop("recheck_at", None)
    if family is not None:
        learned[family] = sorted(members)
    sync_legacy_claims(info)


def aggregate_recheck(info: dict, headers: httpx.Headers, family: str, now: float) -> float:
    representative = headers.get("anthropic-ratelimit-unified-representative-claim", "").strip().lower()
    if family == "fable" and (representative in ("seven_day_oi", "7d_oi") or "fable" in representative):
        reading = account_claims(info).get("7d_oi", {})
        for value in (reading.get("reset"), headers.get("anthropic-ratelimit-unified-reset")):
            try:
                reset = float(value)
            except (TypeError, ValueError):
                continue
            if providers.number(reset) and reset > now:
                return reset
    return now + RECHECK_S


async def note_response(account: str, headers: httpx.Headers, status: int, *, family: str | None = "default",
                        repeated_burst: bool = False, policy: str = "automatic",
                        model=None, path: str | None = None, rec: dict | None = None,
                        received_at: float | None = None) -> str | None:
    """Observe headers once and classify an automatic 429 for its own family."""
    async with state_lock:
        info = STATE["accounts"].setdefault(account, {})
        now = time.time()
        claims = account_claims(info)
        reported = parse_claims(headers, claims)
        if path is not None:
            at = now if received_at is None else received_at
            record_ratelimit_diagnostics(account, model, path, status, headers, at,
                                         rec=rec, family=family, reported=reported)
        observe_claims(info, claims, reported, status, family, now,
                       retire_shared=family is not None or policy == "observe")
        aggregate = headers.get("anthropic-ratelimit-unified-status")
        if aggregate:
            info["status"] = aggregate
        info["last_seen"] = now
        if policy == "observe":
            save_state()
            return None
        family = family or "default"
        if status == 429 and blocker_deadline(account, family, now) <= now:
            if aggregate == "rejected" or (family == "fable" and repeated_burst):
                deadline = aggregate_recheck(info, headers, family, now) if aggregate == "rejected" else now + RECHECK_S
                info.setdefault("blocks_by_family", {})[family] = {
                    "until": deadline, "reason": "rejected" if aggregate == "rejected" else "repeated_429"}
            else:
                try:
                    pace = float(headers.get("retry-after", ""))
                except ValueError:
                    pace = 15
                pace = min(max(pace, 0), 60) if providers.number(pace) else 15
                info.setdefault("pace_by_family", {})[family] = now + pace
                save_state()
                return "burst"
        if status == 429:
            if policy == "strict":
                save_state()
                return "strict"
            target = select_account(family, now, update=policy == "automatic")
            save_state()
            return "switched" if target is not None and target != account else "exhausted"
        if policy == "automatic" and family_route(family)["active_account"] == account:
            target = select_account(family, now)
            save_state()
            return "switched" if target is not None and target != account else None
        save_state()
        return None


# Cheapest call that returns the unified headers on a setup-token: /api/oauth/usage
# needs the user:profile scope and count_tokens omits them. A model the plan
# can't use answers 429 with no headers, so replace this one when it retires.
REFRESH_MODEL = "claude-haiku-4-5"
REFRESH_BODY = json.dumps({"model": REFRESH_MODEL, "max_tokens": 1,
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
            await note_response(name, r.headers, r.status_code, family=None, policy="observe",
                                model=REFRESH_MODEL, path="/v1/messages")
        else:
            record_ratelimit_diagnostics(name, REFRESH_MODEL, "/v1/messages", r.status_code, r.headers, time.time())
            print(f"warning: usage refresh of {name}: HTTP {r.status_code} without usage headers",
                  file=sys.stderr)


async def refresh_loop() -> None:
    while True:
        if REFRESH_IDLE_S:
            await refresh_idle_accounts()
        now = time.time()
        record_history(now)
        save_rollup()
        save_quotas()
        save_quota_history(now)
        save_ratelimit_headers()
        await asyncio.sleep(60)


QUOTAS_PATH = STATE_PATH.with_name("provider_quotas.json")
QUOTAS_SAVED = {"text": None}


def load_quotas() -> None:
    """The OpenAI quota figures last seen before a restart: image figures come only with
    image answers, so without this a restart would clear them until the next image."""
    try:
        saved = json.loads(QUOTAS_PATH.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring {QUOTAS_PATH} ({type(exc).__name__})", file=sys.stderr)
        return
    for name, prov in PROVIDERS.items():
        figures = saved.get(name) if isinstance(saved, dict) else None
        if hasattr(prov, "quotas") and isinstance(figures, dict):
            prov.quotas.update({k: v for k, v in figures.items() if isinstance(k, str) and isinstance(v, dict)})
    QUOTAS_SAVED["text"] = json.dumps(quota_figures())


def quota_figures() -> dict:
    return {name: p.quotas for name, p in PROVIDERS.items() if getattr(p, "quotas", None)}


def save_quotas() -> None:
    """Write the figures when they changed (at most once a minute, from the refresh loop)."""
    text = json.dumps(quota_figures())
    if text == QUOTAS_SAVED["text"]:
        return
    tmp = QUOTAS_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(text)
        tmp.replace(QUOTAS_PATH)
        QUOTAS_SAVED["text"] = text
    except OSError as exc:
        storage_error("quotas_write", exc)


QUOTA_HISTORY_PATH = STATE_PATH.with_name("provider_quota_history.json")
QUOTA_HISTORY_VERSION = 1
QUOTA_HISTORY_CLEANUP = False


def quota_histories() -> dict:
    return {name: p.quota_history for name, p in PROVIDERS.items()
            if isinstance(p, providers.OpenAISiwc)}


def quota_history_snapshot(now: float) -> dict:
    return {name: series for name, history in quota_histories().items()
            if (series := history.snapshot(now))}


def load_quota_history() -> None:
    global QUOTA_HISTORY_CLEANUP
    try:
        saved = json.loads(QUOTA_HISTORY_PATH.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError, RecursionError) as exc:
        print(f"warning: ignoring {QUOTA_HISTORY_PATH} ({type(exc).__name__})", file=sys.stderr)
        return
    if (not isinstance(saved, dict) or type(saved.get("version")) is not int
            or saved["version"] != QUOTA_HISTORY_VERSION or not isinstance(saved.get("providers"), dict)):
        print(f"warning: ignoring {QUOTA_HISTORY_PATH} (unsupported history format)", file=sys.stderr)
        return
    now = time.time()
    cutoff = now - HISTORY_DAYS * 86400
    for name, history in quota_histories().items():
        series = saved["providers"].get(name)
        if not isinstance(series, list):
            continue
        for entry in series:
            if not isinstance(entry, dict) or not isinstance(entry.get("points"), list):
                continue
            for point in entry["points"]:
                if (isinstance(point, list) and len(point) == 4
                        and providers.number(point[0]) and cutoff <= point[0] <= now):
                    history.add(entry.get("limit"), entry.get("minutes"), point)
        history.cap_series()
        history.dirty = False
    QUOTA_HISTORY_CLEANUP = saved["providers"] != quota_history_snapshot(now)


def save_quota_history(now: float) -> None:
    global QUOTA_HISTORY_CLEANUP
    histories = quota_histories().values()
    for history in histories:
        history.prune(now)
    if not QUOTA_HISTORY_CLEANUP and not any(h.dirty for h in histories):
        return
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=QUOTA_HISTORY_PATH.parent,
                                         prefix=".provider_quota_history.", suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            json.dump({"version": QUOTA_HISTORY_VERSION, "providers": quota_history_snapshot(now)}, f)
        tmp.replace(QUOTA_HISTORY_PATH)
    except OSError as exc:
        storage_error("quota_history_write", exc)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        QUOTA_HISTORY_CLEANUP = False
        for history in histories:
            history.dirty = False


load_quotas()
load_quota_history()
HISTORY_PATH = STATE_PATH.with_name("usage_history.json")
SAMPLE_S, PROJECT_DAYS = 1800, 7.5


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
    snapshot = {}
    for name in ACCOUNTS:
        reading = claim_reading(name, "7d")
        if not disabled(name) and reading.get("reset"):
            snapshot[name] = [round(util_7d(name), 4), reading["reset"]]
    return snapshot


def record_history(now: float) -> None:
    """One weekly-usage snapshot of every enabled account per SAMPLE_S, kept HISTORY_DAYS."""
    if HISTORY and now - HISTORY[-1]["ts"] < SAMPLE_S:
        return
    snapshot = weekly_snapshot()
    if not snapshot:
        return
    sample = {"ts": round(now), "accounts": snapshot}
    ledger_sample(HISTORY[-1] if HISTORY else None, sample)
    HISTORY.append(sample)
    while HISTORY[0]["ts"] < now - HISTORY_DAYS * 86400:
        HISTORY.pop(0)
    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(HISTORY))
        tmp.replace(HISTORY_PATH)
    except OSError as exc:
        storage_error("history_write", exc)


def weekly_series(now: float) -> list:
    """[ts, used, capacity, spent] in % of one weekly quota. `used` sums each enabled
    account's current weekly usage (weighted), so it drops at that account's reset;
    `capacity` sums the weights. `spent` adds up the weekly rises seen since the first
    sample, so the run-out rate can be measured without resets cancelling usage."""
    points, spent, seen = [], 0.0, {}
    for sample in HISTORY + [{"ts": now, "accounts": weekly_snapshot()}]:
        used = cap = 0.0
        for name, (u, reset) in sample["accounts"].items():
            if name not in ACCOUNTS or disabled(name):
                continue
            w = weight(name, sample["ts"])  # the plan in force then: an upgrade steps capacity
            last_reset, last_u = seen.get(name, (reset, u))  # first sight is a baseline
            if reset > last_reset + 3600:  # a new weekly window: all of it is new usage
                spent += w * u
            elif u > last_u:  # against the last reading: a bought reset lowers usage mid-week
                spent += w * (u - last_u)
            seen[name] = (max(reset, last_reset), u)
            used += w * u
            cap += w
        points.append([round(sample["ts"]), round(used * 100, 1), round(cap * 100, 1), round(spent * 100, 1)])
    return points


def weekly_outlook(now: float) -> dict:
    """The chart's series plus a PROJECT_DAYS projection: spend the last 24 h rate
    consume-first (soonest weekly reset first); while every enabled account is at
    100% usage pauses until the next reset. `eta` is the first such moment."""
    points = weekly_series(now)
    out = {"series": [p[:3] for p in points], "projection": [], "rate_per_day": None,
           "eta": None, "back": None, "back_account": None,
           "capacity": round(sum(weight(n) for n in weekly_snapshot()) * 100)}
    day = [p for p in points if p[0] >= now - 86400]
    # Usage cannot rise while every account is spent, so time at the limit would
    # understate demand: count only the stretches with quota left (as the panel shades).
    span = sum(b[0] - a[0] for a, b in zip(day, day[1:]) if a[2] - a[1] > 0.1)
    if span < 3600:
        out["note"] = "not enough history with quota left yet to measure a rate"
        return out
    rate = (day[-1][3] - day[0][3]) / span  # % of one weekly quota per second
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

    def point(t):
        return [round(t), round(sum(weight(n) * u for n, (u, _) in accts.items()), 1), out["capacity"]]

    t, horizon = now, now + PROJECT_DAYS * 86400
    proj = [point(t)]
    while t < horizon:
        nxt = min(accts, key=lambda n: accts[n][1])
        reset = accts[nxt][1]
        end = min(reset, horizon)
        empty = t + left() / rate
        if empty < end:  # everything spent before the next reset: flat until it
            for a in accts.values():
                a[0] = 100.0
            proj.append(point(empty))
            if out["eta"] is None:
                out.update(eta=round(empty), back=round(reset), back_account=nxt)
        else:
            spend = rate * (end - t)
            for n in sorted(accts, key=lambda n: accts[n][1]):
                take = min(spend, weight(n) * (100 - accts[n][0]))
                accts[n][0] += take / weight(n)
                spend -= take
        proj.append(point(end))
        if end == reset:
            accts[nxt] = [0.0, reset + 7 * 86400]
            proj.append(point(end))
        t = end
    out["projection"] = proj
    return out


def status_claims(info: dict, now: float) -> dict:
    applicable = {family: applicable_claims(info, family) for family in MODEL_FAMILIES}
    thresholds = claim_thresholds()
    out = {}
    for claim, reading in account_claims(info).items():
        reset = reading.get("reset")
        rejected = reading.get("status") == "rejected"
        until = claim_block_until(reading)
        current = current_util(reading, now)
        if (reset is not None and reset <= now) or (current is None and not reading.get("status")):
            freshness = "awaiting_reading"
        else:
            freshness = "current"
        out[claim] = {
            "label": CLAIM_LABELS.get(claim, claim),
            "families": [family for family in MODEL_FAMILIES if claim in applicable[family]],
            "utilization": reading.get("utilization"), "effective_utilization": current,
            "reset": reset, "seen_at": reading.get("seen_at"), "status": reading.get("status"),
            "freshness": freshness, "threshold": thresholds.get(claim),
            "block_reason": ("rejected" if rejected else "exhausted") if until > now else None,
            "blocked_until": until if until > now else None,
        }
    return out


@app.get("/rotate/status")
async def rotate_status(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    now = time.time()
    accounts = {}
    for name in ACCOUNTS:
        info = dict(STATE["accounts"].get(name, {}))
        info["claims"] = status_claims(info, now)
        for claim in GENERAL_CLAIMS:
            reading = info["claims"].get(claim)
            for field, prefix in (("utilization", "util"), ("reset", "reset")):
                key = f"{prefix}_{claim}"
                if reading is not None and reading[field] is not None:
                    info[key] = reading[field]
                else:
                    info.pop(key, None)
            if reading is not None:
                info[f"effective_util_{claim}"] = reading["effective_utilization"] or 0.0
        info["priority"] = priority(name)
        info["disabled"] = disabled(name)
        info["eligible_by_family"] = {family: usable(name, family) for family in MODEL_FAMILIES}
        info["blocks_by_family"] = {f: b for f, b in info.get("blocks_by_family", {}).items() if b["until"] > now}
        if info.get("reset_5h"):
            info["reset_5h_in_min"] = max(0, round((info["reset_5h"] - now) / 60))
        if info.get("reset_7d"):
            info["reset_7d_in_h"] = max(0, round((info["reset_7d"] - now) / 3600, 1))
        accounts[name] = info
    active = {family: family_route(family)["active_account"] for family in MODEL_FAMILIES}
    manual = {family: family_route(family).get("manual_account") for family in MODEL_FAMILIES}
    pins = {}
    for device in CFG["devices"]:
        if granted(device, ANTHROPIC):
            pin, source = device_pin(device)
            pins[device] = {"account_pin": pin, "source": source}
    return {
        "active_account": active["default"],
        "pinned": manual["default"] if manual["default"] == active["default"] else None,
        "active_by_family": active,
        "manual_by_family": manual,
        "device_pins": pins,
        "pin_targets": {name: name in ACCOUNTS and not disabled(name)
                        for name in sorted(CFG["configured_accounts"])},
        "switch_threshold": THRESHOLD,
        "switch_threshold_7d": THRESHOLD_7D,
        "strategy": STRATEGY,
        "hold_max_s": HOLD_MAX_S,
        "accounts": accounts,
        "recent_events": STATE["events"][-10:],
        "storage_errors": STORAGE_ERRORS,
        "ratelimits_available": True,
        "weekly": weekly_outlook(now),
        "providers": {name: p.status() for name, p in PROVIDERS.items()},
    }


@app.get("/rotate/quota-history")
async def rotate_quota_history(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    now = time.time()
    return {"as_of": now, "providers": quota_history_snapshot(now)}


@app.get("/rotate/ratelimits")
async def rotate_ratelimits(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    return {"latest_ratelimits": LATEST_RATELIMITS}


@app.post("/rotate/switch")
async def rotate_switch(request: Request):
    """Panel buttons: {"account": name} makes it active, {"auto": true} hands back to
    the strategy. A pick holds until the account crosses a switch threshold, runs out
    of quota or is disabled."""
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    # Serve adds the tailnet identity to any request, a cross-site form post included:
    # only same-origin JSON, which another page cannot send without a CORS preflight.
    if not same_origin_json(request):
        return JSONResponse({"error": "send same-origin JSON"}, status_code=403)
    try:
        body = json.loads(await bounded_body(request) or b"{}")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    async with state_lock:
        if body.get("auto"):
            for family in MODEL_FAMILIES:
                family_route(family)["manual_account"] = None
            project_default_route()
            save_state()
            return {"active_account": STATE["active_account"], "pinned": None}
        target = body.get("account")
        if not isinstance(target, str) or target not in ACCOUNTS:
            return JSONResponse({"error": "unknown account"}, status_code=404)
        eligible = [family for family in MODEL_FAMILIES if usable(target, family)]
        if not eligible:
            return JSONResponse({"error": f"{target} is disabled or at a switch threshold"},
                                status_code=409)
        now = time.time()
        for family in eligible:
            route = family_route(family)
            if target != route["active_account"]:
                switch_route(family, target, "manual", now)
            route["manual_account"] = target
        project_default_route()
        save_state()
    return {"active_account": STATE["active_account"], "pinned": STATE.get("pinned")}


def same_origin_json(request: Request) -> bool:
    return (request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"
            and request.headers.get("sec-fetch-site", "same-origin") == "same-origin")


@app.post("/rotate/device-pin")
async def rotate_device_pin(request: Request):
    """Set account_pin (null means Auto) or use_config for one device until restart."""
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    if not same_origin_json(request):
        return JSONResponse({"error": "send same-origin JSON"}, status_code=403)
    try:
        body = json.loads(await bounded_body(request))
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("device"), str):
        return JSONResponse({"error": "device and pin action are required"}, status_code=400)
    name = body["device"]
    use_config = set(body) == {"device", "use_config"} and body["use_config"] is True
    if not use_config and (set(body) != {"device", "account_pin"}
                           or (body["account_pin"] is not None
                               and not valid_account_pin(body["account_pin"], CFG["configured_accounts"]))):
        return JSONResponse({"error": "send a valid account_pin, null for Auto, or use_config"},
                            status_code=400)
    async with state_lock:
        if name not in CFG["devices"]:
            return JSONResponse({"error": "unknown device"}, status_code=404)
        if not granted(name, ANTHROPIC):
            return JSONResponse({"error": "device needs the anthropic grant"}, status_code=403)
        if use_config:
            DEVICE_PIN_OVERRIDES.pop(name, None)
        else:
            DEVICE_PIN_OVERRIDES[name] = body["account_pin"]
        pin, source = device_pin(name)
    return {"device": name, "account_pin": pin, "source": source}


def panel_allowed(request: Request) -> bool:
    # Tailscale Serve sets Tailscale-User-Login for tailnet users and strips client
    # copies; startup enforces a loopback bind, so only Serve (or a local process) can add it.
    # The panel runs the Claude accounts, so a key needs that grant to open it.
    device = device_from_request(request)
    return ((PANEL_TAILNET and bool(request.headers.get("tailscale-user-login")))
            or (device is not None and granted(device, ANTHROPIC)))


def device_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    key = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if not key:
        key = request.query_params.get("key", "")
    if not key:
        return None
    return DEVICE_BY_KEY.get(key)


async def bounded_body(request: Request, limit: int | None = None) -> bytes:
    """Bound actual bytes, including bodies without a Content-Length header."""
    body, limit = bytearray(), limit or MAX_REQUEST_BYTES
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="request body too large")
        body.extend(chunk)
    return bytes(body)


async def bounded_response(upstream: httpx.Response, limit: int | None = None) -> bytes:
    """Limit buffered upstream responses; streaming responses stay streaming."""
    body, limit = bytearray(), limit or MAX_RESPONSE_BYTES
    try:
        async for chunk in upstream.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > limit:
                raise HTTPException(status_code=502, detail="upstream response too large")
            body.extend(chunk)
    finally:
        await upstream.aclose()
    return bytes(body)


async def buffered_search(request: Request, upstream: httpx.Response) -> bytes:
    async def wait_for_disconnect():
        # The request body was consumed before forwarding.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    read = asyncio.create_task(bounded_response(upstream, min(MAX_RESPONSE_BYTES, MAX_SEARCH_RESPONSE_BYTES)))
    disconnected = asyncio.create_task(wait_for_disconnect())
    try:
        await asyncio.wait((read, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if disconnected.done():
            disconnected.result()
            raise HTTPException(status_code=499, detail="client disconnected")
        return read.result()
    finally:
        read.cancel()
        disconnected.cancel()
        await asyncio.gather(read, disconnected, return_exceptions=True)
        await upstream.aclose()


def load_prices() -> dict:
    return pricing.load_prices(CFG.get("prices_per_mtok", {}))


PRICES = load_prices()
ALERT_CFG = CFG.get("alerts", {
    "device_tokens_per_hour": 5_000_000,
    "util_warn": 0.8,
})


def price_for(model: str | None, default=DEFAULT_PRICE) -> list[float] | None:
    return pricing.price_for(model, PRICES, default)


def api_cost(model: str | None, usage: dict, provider=None) -> float:
    return pricing.api_cost(model, usage, provider, prices=PRICES)


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
    by_provider: dict = {}
    device_hour_tokens: dict = {}
    device_hour_reqs: dict = {}
    device_meta: dict = {}

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
        provider = rec.get("provider") if isinstance(rec.get("provider"), str) else None
        cost = api_cost(model, usage, provider)
        dev = rec.get("device", "?")
        meta = device_meta.setdefault(dev, {})
        if ts >= meta.get("last_ts", 0):
            meta.update({"last_ts": ts, "last_model": model,
                         "last_account": rec.get("account"), "last_provider": provider})
        bump(by_device, rec.get("device", "?"), usage, cost)
        bump(by_model, model or "?", usage, cost)
        if provider:
            bump(by_provider, provider, usage, cost)  # apart: a provider may share an account's name
        else:
            bump(by_account, rec.get("account", "?"), usage, cost)
        if ts >= hour_ago:
            total = sum(usage.get(k, 0) for k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens"))
            device_hour_tokens[rec.get("device", "?")] = (
                device_hour_tokens.get(rec.get("device", "?"), 0) + total)
            device_hour_reqs[dev] = device_hour_reqs.get(dev, 0) + 1

    limited = now < _AUDIT_CACHE.get("limited_until", 0)
    alerts = ([{"level": "warn", "text": "Statistics are partial: older history was "
                "rotated, skipped or dropped to stay within resource limits."}] if limited else [])
    for dev, toks in device_hour_tokens.items():
        if toks > ALERT_CFG["device_tokens_per_hour"]:
            alerts.append({"level": "warn",
                           "text": f"{dev} consumed {toks:,} tokens in the last hour "
                                   f"(threshold {ALERT_CFG['device_tokens_per_hour']:,})"})
    for name in ACCOUNTS:
        for claim in status_claims(STATE["accounts"].get(name, {}), now).values():
            u = claim["effective_utilization"]
            threshold = claim["threshold"]
            text = f"{name} {claim['label']} window"
            if u is not None and u >= ALERT_CFG["util_warn"]:
                text += f" at {u:.0%}"
                if threshold is not None:
                    text += f" (switch threshold {threshold:.0%})"
            elif not claim["block_reason"]:
                continue
            if claim["block_reason"]:
                text += f" · {claim['block_reason']}"
            alerts.append({"level": "warn", "text": text})

    # Roster covers every registered device, traffic or not.
    devices = {}
    for name, key in CFG["devices"].items():
        meta = device_meta.get(name, {})
        last_ts = meta.get("last_ts")
        devices[name] = {
            "key_suffix": "…" + key[-4:] if len(key) >= 8 else None,  # a short key would show whole
            "online": bool(last_ts and now - last_ts < 600),
            "last_seen_min": round((now - last_ts) / 60) if last_ts else None,
            "last_model": meta.get("last_model"),
            "last_account": meta.get("last_account"),
            "last_provider": meta.get("last_provider"),
            "providers": sorted(DEVICE_GRANTS.get(name, (ANTHROPIC,))),
            "req_1h": device_hour_reqs.get(name, 0),
            "tokens_1h": device_hour_tokens.get(name, 0),
        }

    return {"window": "last 24h (partial)" if limited else "last 24h",
            "history_limited": limited, "retained_records": len(records),
            "devices": devices, "by_device": by_device,
            "by_model": by_model, "by_account": by_account, "by_provider": by_provider, "alerts": alerts}


USAGE_PATH = STATE_PATH.with_name("device_usage.json")
ROLLUP_DAYS, LEDGER_DAYS = 31, 400
ROLLUP: dict = {"days": {}, "total": {"device": {}, "model": {}}, "since": None,
                "accounts": {}, "quota": {}, "blocked": {}}
ROLLUP_DIRTY = False


def roll_up(rec: dict, day: str, ledger_only: bool = False) -> None:
    """Add one audit record to the per-day and all-time totals by device and model, and
    to the account's day in the billing ledger. The audit log rotates and the stats
    cache holds 24 h; this keeps the longer view."""
    global ROLLUP_DIRTY
    model = rec.get("model")
    model = model if isinstance(model, str) else None
    usage = rec.get("usage") or {}
    cost = api_cost(model, usage, rec.get("provider") if isinstance(rec.get("provider"), str) else None)
    if isinstance(rec.get("account"), str):
        bump(ROLLUP.setdefault("accounts", {}).setdefault(day, {}), rec["account"], usage, cost)
    if not ledger_only:
        buckets = ROLLUP["days"].setdefault(day, {"device": {}, "model": {}})
        for kind, key in (("device", str(rec.get("device", "?"))), ("model", model or "?")):
            bump(buckets[kind], key, usage, cost)
            bump(ROLLUP["total"][kind], key, usage, cost)
        ROLLUP["since"] = min(ROLLUP["since"] or day, day)
    ROLLUP_DIRTY = True


def ledger_sample(prev: dict | None, cur: dict) -> None:
    """Credit each account's weekly-quota rise since the previous history sample to its
    day ([rise in % of its weekly quota, seconds at its weekly limit]), and the time
    every account was at its limit to the pool's day."""
    global ROLLUP_DIRTY
    day = time.strftime("%Y-%m-%d", time.localtime(cur["ts"]))
    gap = cur["ts"] - prev["ts"] if prev else 0
    gap = gap if gap <= 2 * SAMPLE_S else 0  # downtime is not time at the limit
    quota = ROLLUP.setdefault("quota", {}).setdefault(day, {})
    for name, (u, reset) in cur["accounts"].items():
        last = (prev or {"accounts": {}})["accounts"].get(name)
        rise = 0.0 if not last else u if reset > last[1] + 3600 else max(0.0, u - last[0])
        q = quota.setdefault(name, [0.0, 0])
        q[0], q[1] = round(q[0] + rise * 100, 2), q[1] + (gap if u >= 1 else 0)
    if cur["accounts"] and all(u >= 1 for u, _ in cur["accounts"].values()):
        blocked = ROLLUP.setdefault("blocked", {})
        blocked[day] = blocked.get(day, 0) + gap
    ROLLUP_DIRTY = True


def load_rollup() -> None:
    """Load the totals; without a usable file, rebuild them once from the audit
    log and its rotated copies, oldest first, and the ledger's quota from the usage
    history. A file from before the ledger gets only the ledger rebuilt."""
    global ROLLUP
    ledger_only = False
    try:
        loaded = json.loads(USAGE_PATH.read_text())
        if isinstance(loaded, dict) and {"days", "total", "since"} <= loaded.keys():
            ROLLUP = loaded
            if "quota" in loaded:
                return
            ledger_only = True
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print(f"warning: rebuilding {USAGE_PATH} from the audit log ({type(exc).__name__})",
              file=sys.stderr)
    ROLLUP.setdefault("quota", {})  # marks the ledger as built, even with no history yet
    for path in [Path(f"{AUDIT_PATH}.{n}") for n in range(AUDIT_BACKUPS, 0, -1)] + [AUDIT_PATH]:
        try:
            with path.open("rb") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("ts"), str):
                        roll_up(rec, rec["ts"][:10], ledger_only)
        except OSError:
            continue
    for prev, cur in zip([None] + HISTORY, HISTORY):
        ledger_sample(prev, cur)


load_rollup()


def save_rollup() -> None:
    global ROLLUP_DIRTY
    if not ROLLUP_DIRTY:
        return
    import datetime as dt
    cutoff = (dt.date.today() - dt.timedelta(days=ROLLUP_DAYS)).isoformat()
    for day in [d for d in ROLLUP["days"] if d < cutoff]:
        del ROLLUP["days"][day]
    cutoff = (dt.date.today() - dt.timedelta(days=LEDGER_DAYS)).isoformat()
    for kind in ("accounts", "quota", "blocked"):
        for day in [d for d in ROLLUP.get(kind, {}) if d < cutoff]:
            del ROLLUP[kind][day]
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


def month_add(d, n: int):
    """d plus n months, on the same day or the month's last one (a 31st renews on the 30th)."""
    import calendar
    import datetime as dt
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, min(d.day, calendar.monthrange(d.year + y, m + 1)[1]))


def billing_periods(timeline: list, today) -> list:
    """[start, end) of each billing period up to the one holding today: monthly from the
    first entry's date, restarting at an entry marked renews. Other plan changes keep
    the renewal date (prorated upgrades)."""
    starts = [e[1] for i, e in enumerate(timeline) if i == 0 or e[3]]
    out = []
    for i, first in enumerate(starts):
        stop = starts[i + 1] if i + 1 < len(starts) else None
        n = 0
        while True:
            start, end = month_add(first, n), month_add(first, n + 1)
            if stop and end >= stop:
                out.append((start, stop))
                break
            out.append((start, end))
            if not stop and end > today:
                break
            n += 1
    return [p for p in out if p[0] <= today]


def share(days: list):
    """Used over capacity, in %, for [(used, capacity)] per day."""
    cap = sum(c for _, c in days)
    return round(100 * sum(u for u, _ in days) / cap, 1) if cap else None


def busiest_week(days: list):
    """The highest share over any 7 recorded days in a row (fewer if that is all there is)."""
    n = min(7, len(days))
    return max((share(days[i:i + n]) for i in range(len(days) - n + 1)), default=None)


def billing_report(today=None) -> dict:
    """Per account with billing: its plan, the next renewal and the last 13 billing
    periods. Each period has the prorated price, the account's weekly use and time at
    its weekly limit, the pool's share of its weekly capacity used (period average and
    busiest 7 days) and time with every account at its limit, the requests and their
    cost-equivalent, and the pool's shares had this account been on the next plan down
    or up. A 7-day span across weekly resets can use more than 100%; an average over
    100% cannot last. Days without history are skipped and counted as not covered."""
    import datetime as dt
    today = today or dt.date.today()
    ranked = sorted(PLANS, key=lambda p: (PLANS[p]["weight"], PLANS[p]["price"]))
    out = {}
    for name, timeline in BILLING.items():
        periods = []
        for start, end in billing_periods(timeline, today)[-13:]:
            span = [start + dt.timedelta(days=n) for n in range((end - start).days)]
            plans = [plan_at(name, time.mktime(d.timetuple()))[2] for d in span]
            i = ranked.index(plans[-1])
            alts = ranked[max(0, i - 1):i] + ranked[i + 1:i + 2]
            row = {"start": start.isoformat(), "end": end.isoformat(), "days": len(span),
                   "elapsed": 0, "covered": 0, "plans": list(dict.fromkeys(plans)),
                   "price": round(sum(PLANS[p]["price"] for p in plans) / len(span), 2),
                   "limited_h": 0.0, "blocked_h": 0.0, "usage": {}}
            own, pool, alt = [], [], {p: [] for p in alts}  # per recorded day: (used, capacity)
            for d, plan in zip(span, plans):
                if d > today:
                    break
                key, ts = d.isoformat(), time.mktime(d.timetuple())
                row["elapsed"] += 1
                for field, v in ROLLUP.get("accounts", {}).get(key, {}).get(name, {}).items():
                    row["usage"][field] = row["usage"].get(field, 0) + v
                quota = {a: q for a, q in ROLLUP.get("quota", {}).get(key, {}).items() if a in ACCOUNTS}
                if name not in quota:
                    continue
                row["covered"] += 1
                row["limited_h"] += quota[name][1] / 3600
                row["blocked_h"] += ROLLUP.get("blocked", {}).get(key, 0) / 3600
                w = PLANS[plan]["weight"]
                own.append((quota[name][0] * w, w * 100 / 7))
                used = sum(q[0] * weight(a, ts) for a, q in quota.items())
                cap = sum(weight(a, ts) for a in quota) * 100 / 7
                pool.append((used, cap))
                for p in alts:
                    alt[p].append((used, cap + (PLANS[p]["weight"] - w) * 100 / 7))
            row.update(used=share(own), peak=busiest_week(own), pool_used=share(pool),
                       pool_peak=busiest_week(pool), limited_h=round(row["limited_h"], 1),
                       blocked_h=round(row["blocked_h"], 1),
                       whatif=[{"plan": p, "price": PLANS[p]["price"] - PLANS[plans[-1]]["price"],
                                "pool_used": share(alt[p]), "pool_peak": busiest_week(alt[p])} for p in alts])
            periods.append(row)
        plan = plan_at(name, time.time())[2]
        out[name] = {"plan": plan, "price": PLANS[plan]["price"],
                     "renews": periods[-1]["end"] if periods else None, "periods": periods[::-1]}
    return out


@app.get("/rotate/stats")
async def rotate_stats(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    out = aggregate_audit() | {"periods": usage_periods(), "billing": billing_report()}
    out["alerts"] = out["alerts"] + [{"level": "warn", "text": f"billing ignored: {e}"} for e in BILLING_ERRORS]
    for name, p in PROVIDERS.items():
        s = p.status()
        if s.get("unsaved"):
            out["alerts"].append({"level": "warn", "text": f"provider {name}: credential not on disk: {s['unsaved']}"})
        if s["reason"]:
            out["alerts"].append({"level": "warn", "text": f"provider {name} is {s['state']}: {s['reason']}"})
        elif s.get("last_error"):
            out["alerts"].append({"level": "warn", "text": f"provider {name}: token refresh failing "
                                  f"({s['last_error']}); the access token in hand lasts {s['access_token_min']} min"})
        im = s.get("images") or {}
        if im.get("unsaved"):
            out["alerts"].append({"level": "warn", "text": f"provider {name}: images login not on disk: {im['unsaved']}"})
        if im.get("reason"):
            out["alerts"].append({"level": "warn", "text": f"provider {name}: image generation is {im['state']}: {im['reason']}"})
        elif im.get("last_error"):
            out["alerts"].append({"level": "warn", "text": f"provider {name}: images login refresh failing "
                                  f"({im['last_error']}); its access token lasts {im['access_token_min']} min"})
        for limit, q in (s.get("quotas") or {}).items():
            for which in ("primary", "secondary"):
                w = q.get(which) or {}
                if (w.get("used_percent") or 0) >= 90:
                    left = w.get("resets_at") and max(0, w["resets_at"] - time.time())
                    out["alerts"].append({"level": "warn", "text": f"provider {name}: quota {limit} at "
                                          f"{w['used_percent']:.0f}%" + (f", resets in {left / 3600:.1f} h" if left else "")})
    return out


PANEL_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>claude-rotate</title>
<style>
 :root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
  --border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--track:#0d366b;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b}
 *{box-sizing:border-box} body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-text-size-adjust:100%;text-size-adjust:100%}
 main{max-width:1180px;margin:0 auto;padding:20px 24px 40px;display:grid;gap:16px}
 header{display:flex;align-items:center;gap:12px;flex-wrap:wrap} h1{font-size:18px;margin:0;font-weight:600}
 .pill{border:1px solid var(--border);border-radius:999px;padding:2px 10px;color:var(--ink2);font-size:13px} #pills{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
 .spacer{flex:1} .muted{color:var(--muted)} .small{font-size:12px}
 .card{position:relative;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;overflow-x:auto}
 .card h2{font-size:13px;font-weight:600;color:var(--ink2);margin:0 0 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .banner{display:flex;gap:10px;padding:10px 14px;border-radius:10px;border:1px solid var(--border);color:var(--ink2);margin-bottom:8px}
 .banner.warn{border-color:rgba(250,178,25,.35);background:rgba(250,178,25,.06)} .banner.warn b{color:var(--warn)}
 .kpis{display:grid;grid-template-columns:1.6fr repeat(4,1fr);gap:16px}
 .hero .v{font-size:48px;font-weight:600;line-height:1.1;margin:4px 0} .label{color:var(--ink2);font-size:13px}
 .tile .v{font-size:26px;font-weight:600;margin:6px 0 2px}
 .accounts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr));gap:16px}
 .acct-head{display:flex;align-items:baseline;gap:8px;margin-bottom:12px;flex-wrap:wrap} .acct-head b{font-size:16px}
 .acct-side{margin-left:auto;display:flex;align-items:baseline;gap:8px}
 .acct-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
 .acct-badges .badge{overflow-wrap:anywhere}
 .badge{font-size:12px;border-radius:999px;padding:1px 8px;border:1px solid var(--border);color:var(--ink2)}
 .meter-row{display:grid;grid-template-columns:28px 1fr 250px;gap:10px;align-items:center;margin:8px 0}
 .meter{position:relative;height:8px;border-radius:4px;background:var(--track)} .meter i{position:absolute;inset:0 auto 0 0;border-radius:4px}
 .meter .tick{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink2);opacity:.6}
 .claim-meter .meter-row{grid-template-columns:66px minmax(48px,1fr) minmax(130px,1.4fr);row-gap:4px}
 .claim-meter .meter-row>span{overflow-wrap:anywhere}
 .legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--ink2);font-weight:400;margin-left:auto}
 .key{display:inline-block;width:16px;border-top:2px solid;vertical-align:4px;margin-right:6px}
 .key.dash{border-top-style:dashed} .key.band{height:10px;border:0;vertical-align:-1px;background:rgba(208,59,59,.3)}
 .wk{display:flex} .wk svg{display:block} .wk-scroll{flex:1;min-width:0;overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--axis) transparent} svg text{fill:var(--muted);font-size:11px;font-family:inherit}
 .tip{position:absolute;z-index:1;pointer-events:none;background:var(--surface);color:var(--ink);box-shadow:0 4px 16px #0008;border:1px solid var(--axis);border-radius:8px;padding:8px 10px;font-size:12px;min-width:170px;max-width:calc(100% - 16px);display:none}
 .tip .row{display:flex;justify-content:space-between;gap:12px} .tip .v{color:var(--ink);font-weight:600}
 .quota-history{margin-top:12px}
 .quota-plot{position:relative;border-top:1px solid var(--grid);padding-top:8px;margin-top:8px}
 .quota-plot header{justify-content:space-between;gap:2px 12px}
 .quota-plot header b{overflow-wrap:anywhere}
 .quota-chart svg{display:block;width:100%;height:148px}
 .sentence{margin-top:8px;color:var(--ink2)}
 table{border-collapse:collapse;width:100%} th,td{padding:8px 10px;border-bottom:1px solid var(--grid);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
 th{color:var(--muted);font-weight:500;font-size:12px} td:first-child,th:first-child,.l{text-align:left} tr:last-child td{border-bottom:0}
 td .sub{display:block;color:var(--muted);font-size:11px} tr.removed td{color:var(--muted)}
 .seg{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-left:auto}
 .seg button{background:none;border:0;color:var(--ink2);padding:4px 12px;font:inherit;font-size:12px;cursor:pointer} .seg button.on{background:var(--grid);color:var(--ink);font-weight:600}
 .split{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}
 .timeline div{display:flex;gap:12px;padding:6px 0;border-bottom:1px solid var(--grid);color:var(--ink2)} .timeline div:last-child{border-bottom:0}
 #billing table+.acct-head{margin-top:20px}
 .linkbtn{background:rgba(57,135,229,.10);border:1px solid rgba(57,135,229,.55);border-radius:6px;color:#8ab8f0;font:inherit;font-size:12px;padding:2px 8px;cursor:pointer}
 .linkbtn:hover:not(:disabled){background:rgba(57,135,229,.22)} .linkbtn:focus-visible{outline:2px solid var(--s1);outline-offset:1px}
 .linkbtn:disabled{opacity:.4;cursor:default} .linkbtn.primary{background:var(--s1);border-color:var(--s1);color:#fff;font-weight:600}
 dialog{background:var(--surface);color:var(--ink);border:1px solid var(--border);border-radius:10px;padding:18px;max-width:min(440px,calc(100% - 24px))} dialog::backdrop{background:rgba(0,0,0,.6)}
 .acts{display:flex;justify-content:flex-end;gap:8px;margin-top:16px} .acts .linkbtn{font-size:13px;padding:5px 14px}
 #pin-form{width:360px} #pin-title{font-weight:600;overflow-wrap:anywhere}
 #pin-form label:not([hidden]){display:grid;gap:4px;margin-top:12px}
 #pin-form select{width:100%;font:inherit;padding:4px}
 @media (max-width:900px){.kpis{grid-template-columns:1fr 1fr}.hero{grid-column:1/-1}.split{grid-template-columns:1fr}
  .meter-row{grid-template-columns:28px 1fr;row-gap:4px}.meter-row>span:last-child{grid-column:2}
  .cards,.cards tbody{display:block} .cards tr:first-child{display:none}
  .cards tr{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px 12px;padding:12px 0;border-bottom:1px solid var(--grid)} .cards tr:last-child{border-bottom:0}
  .cards td{padding:0;border:0;text-align:left;white-space:normal} .cards td:nth-child(n+4):not(:last-child){order:1}
  .cards td[data-l]::before{content:attr(data-l);display:block;color:var(--muted);font-size:11px}}
 @media (max-width:600px){main{padding:12px 12px 32px;gap:12px}.card{padding:14px}.cards tr{grid-template-columns:repeat(2,minmax(0,1fr))}
  th,td{padding:8px 6px} .split :is(th,td):nth-child(4){display:none}}
 @media (max-width:900px){.claim-meter .meter-row{grid-template-columns:66px 1fr}}
</style></head><body><main>
<header><h1>claude-rotate</h1><span class="muted" id="host"></span><span id="pills"></span><span class="spacer"></span><span class="muted small" id="upd"></span></header>
<div id="alerts"></div>
<section class="kpis">
 <div class="card hero"><div class="label">General weekly quota runs out in</div><div class="v" id="hero">–</div><div class="muted" id="hero-sub"></div></div>
 <div class="card tile"><div class="label">Weekly quota left</div><div class="v" id="k-left">–</div><div class="muted small" id="k-left-sub"></div></div>
 <div class="card tile"><div class="label">Burn rate</div><div class="v" id="k-rate">–</div><div class="muted small">of a weekly quota per day</div></div>
 <div class="card tile"><div class="label">Requests, 24 h</div><div class="v" id="k-req">–</div><div class="muted small" id="k-req-sub"></div></div>
 <div class="card tile"><div class="label">Cost-equivalent, 7 d</div><div class="v" id="k-cost">–</div><div class="muted small" id="k-cost-sub"></div></div>
</section>
<section class="accounts" id="accounts"></section>
<section class="accounts" id="oai"></section>
<section class="card"><h2>Weekly quota, all accounts <span class="muted" style="font-weight:400">summed weekly usage &middot; 14 days + 7.5-day projection</span>
 <span class="legend"><span><span class="key" style="border-color:var(--s1)"></span>Usage</span><span><span class="key" style="border-color:var(--s2)"></span>Limit</span>
 <span><span class="key dash" style="border-color:var(--muted)"></span>Projected</span><span><span class="key band"></span>&#9940; Every account at its limit</span></span>
 <button class="linkbtn" id="tbl">Table</button></h2>
 <div id="weekly" class="muted">collecting data</div><div class="tip" id="tip"></div></section>
<section class="card"><h2>Subscriptions <span class="muted" style="font-weight:400">billing periods &middot; USD list prices</span></h2><div id="billing"></div></section>
<section class="card"><h2>Devices</h2><table id="devices" class="cards"></table></section>
<section class="split">
 <div class="card"><h2>By model <span class="seg" id="period"></span></h2><table id="models"></table></div>
 <div class="card"><h2>By account, 24 h</h2><table id="by-account"></table><h2 style="margin-top:20px">Recent switches</h2><div class="timeline" id="events"></div></div>
</section>
<p class="muted small">Cost-equivalent uses API list prices. The 24 h view uses current prices. Stored totals and billing periods keep the prices used when each request was recorded.</p>
</main>
<dialog id="confirm"><div id="cf-text"></div><div class="small" id="cf-err" style="color:var(--crit)"></div>
 <div class="acts"><button class="linkbtn" id="cf-no">Cancel</button><button class="linkbtn primary" id="cf-yes">Switch</button></div></dialog>
<dialog id="pin-form" aria-labelledby="pin-title"><div id="pin-title"></div>
 <p class="small muted">This choice lasts until restart.</p>
 <label>Routing<select id="pin-mode">
  <option value="use_config">Use config</option><option value="auto">Auto until restart</option>
  <option value="strict">Strict pin</option><option value="fallback">Fallback pin</option>
 </select></label>
 <label id="pin-target">Claude account<select id="pin-account"></select></label>
 <div class="small" id="pin-error" role="alert" style="color:var(--crit)"></div>
 <div class="acts"><button class="linkbtn" id="pin-cancel">Cancel</button><button class="linkbtn primary" id="pin-save">Save</button></div></dialog>
<script>
const KEY = new URLSearchParams(location.search).get('key');
const cleanURL = new URL(location.href);
cleanURL.searchParams.delete('key');
history.replaceState(null, '', cleanURL.pathname + cleanURL.search + cleanURL.hash);
const $=id=>document.getElementById(id), H=3600, D=86400, PERIODS={'24 h':'24h','7 d':'7d','30 d':'30d','All time':'all'};
let period='7 d', showTable=false, WK=null, ST={}, PENDING=null, weeklySelection=null;
let quotaHistory=null, quotaHistoryFailed=false;
const quotaCards=new Map();
function esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmt(n){n=n||0;return n>=1e6?(n/1e6).toFixed(n>=1e7?0:1)+'M':n>=1e3?(n/1e3).toFixed(n>=1e4?0:1)+'k':''+Math.round(n)}
function money(v){v=v||0;return '$'+(v>=100?Math.round(v).toLocaleString():v.toFixed(2))}
function tok(b){return (b.input_tokens||0)+(b.output_tokens||0)+(b.cache_write||0)}
function sum(o,f){return Object.values(o||{}).reduce((a,b)=>a+(f(b)||0),0)}
function when(ts){return new Date(ts*1000).toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'})}
function day(ts){return new Date(ts*1000).toLocaleDateString([],{month:'short',day:'numeric'})}
function observed(ts){return new Date(ts*1000).toLocaleString([],{dateStyle:'medium',timeStyle:'long'})}
function dur(s){return s<5400?Math.round(s/60)+' min':s<2*D?(s/H).toFixed(s<10*H?1:0)+' h':(s/D).toFixed(1)+' d'}
function ago(min){return min==null?'never':min<1?'just now':min<60?min+' min ago':(min/60).toFixed(1)+' h ago'}
function cell(b,l){return b?`<td data-l="${l}">${money(b.cost_usd)}<span class="sub">${fmt(tok(b))} tok &middot; ${fmt(b.requests)} req</span></td>`:`<td data-l="${l}" class="muted">&ndash;</td>`}
function meter(label,u,thr,reset,block){
  const sev=block==='rejected'?['var(--crit)',' &#10005; rejected']
    :block||u>=1?['var(--crit)',' &#10005; limit reached']:thr!=null&&u>=thr?['var(--warn)',' &#9888; past switch point']:['var(--s1)',''];
  const fill=u==null?'':`<i style="width:${Math.min(100,u*100)}%;background:${sev[0]}"></i>`;
  const tick=thr==null?'':`<span class="tick" style="left:${thr*100}%"></span>`;
  return `<div class="meter-row"><span class="muted">${label}</span><div class="meter">${fill}${tick}</div>`
    +`<span>${u==null?'':`<b>${Math.round(u*100)}%</b> `}<span class="muted small">${reset}</span><span class="small">${sev[1]}</span></span></div>`}
const familyLabel=family=>family==='fable'?'Fable':'Default';
function claimMeters(account,now){
  const rows=Object.entries(account.claims||{}).filter(([,c])=>c.freshness!=='awaiting_reading'||c.block_reason).map(([id,c])=>{
    const meta=[];
    if(c.status&&c.status!=='allowed'&&!c.block_reason)meta.push(c.status);
    if(c.reset>now)meta.push('resets in '+dur(c.reset-now));
    if(c.blocked_until&&c.blocked_until!==c.reset)meta.push('recheck '+when(c.blocked_until));
    if(c.seen_at&&account.last_seen-c.seen_at>600)meta.push('seen '+ago(Math.round((now-c.seen_at)/60)));
    return meter(esc(c.label||id),c.effective_utilization,c.threshold,esc(meta.filter(Boolean).join(' · ')),c.block_reason)});
  for(const [f,b] of Object.entries(account.blocks_by_family||{}))
    rows.push(`<div class="small muted">${familyLabel(f)} blocked until ${esc(when(b.until))} · ${b.reason==='repeated_429'?'repeated 429s':'rejected'}</div>`);
  return `<div class="claim-meter">${rows.join('')||'<span class="muted">Awaiting quota readings</span>'}</div>`;
}
function wlabel(m){return m==null?'':m%10080===0?(m/10080===1?'7d':m/10080+'w'):m%1440===0?(m/1440===1?'24h':m/1440+'d'):m%60===0?m/60+'h':m+'min'}
function quotas(name,p){const now=Date.now()/1000, q=p.quotas||{}, im=p.images;
  const plan=(Object.values(q).find(f=>f&&f.plan)||{}).plan;
  const wins=f=>['primary','secondary'].map(k=>f&&f[k]).filter(Boolean).sort((a,b)=>(a.window_minutes||0)-(b.window_minutes||0));
  // A window that has reset since the figure counts as 0, as for the Claude accounts.
  const row=(what,f,w)=>{const past=w.resets_at&&w.resets_at<=now;
    return meter(esc(wlabel(w.window_minutes)||'–'),past?0:(w.used_percent||0)/100,0.9,
      esc([what,past?'window reset since':w.resets_at?'resets in '+dur(w.resets_at-now):'','seen '+ago(Math.round((now-(w.seen_at??f.seen_at??now))/60))].filter(Boolean).join(' · ')))};
  const empty=(label,text)=>`<div class="meter-row"><span class="muted">${esc(label)}</span><div class="meter"></div><span class="muted small">${esc(text)}</span></div>`;
  const ids=Object.keys(q), images=ids.filter(id=>/^image/.test(id));
  let rows=q.codex?wins(q.codex).map(w=>row('model',q.codex,w)).join(''):empty('–',im?'model · no figure yet: read every 5 min':'model · read only with an images login');
  if(im)rows+=images.length?images.map(id=>wins(q[id]).map(w=>row('images',q[id],w)).join('')).join(''):empty('24h','images · no figure yet: comes with the next image');
  rows+=ids.filter(id=>id!=='codex'&&!images.includes(id)).map(id=>wins(q[id]).map(w=>row(id,q[id],w)).join('')).join('');
  const dot=im?(['down','unavailable'].includes(im.state)?'crit':im.unsaved||im.usage_error||im.last_error?'warn':'good'):'';
  return `<div class="acct-head"><b>${esc(name)}</b><span class="badge">ChatGPT plan${plan?' '+esc(plan):''}</span>`
    +(im?`<span class="badge" title="${esc(im.reason||im.unsaved||im.last_error||im.usage_error||'')}"><span class="dot" style="background:var(--${dot})"></span>images ${esc(im.state)}</span>`:'')
    +`<span class="acct-side"><span class="muted small">upstream: ${esc(p.state)}</span></span></div>`+rows}
function quotaPlot(host, points, minutes, now, selected){
  const chart=host.querySelector('.quota-chart'), tip=host.querySelector('.tip');
  const width=chart.clientWidth||360, height=148, left=40, right=width-12, top=12, bottom=height-22;
  const span=14*D, start=now-span, X=t=>left+(right-left)*(t-start)/span, Y=u=>top+(100-u)*(bottom-top)/100;
  let grid='', ticks='', lines='', dots='', cycle=null, marked=false;
  const resetFor=p=>p[2]!=null&&Math.abs(p[2]-p[0])<=minutes*60+60?p[2]:null;
  for(const [v,color] of [[0,'--axis'],[25,'--grid'],[50,'--grid'],[75,'--grid'],[100,'--s2']]){
    grid+=`<text x="${left-5}" y="${Y(v)+4}" text-anchor="end">${v}%</text><line x1="${left}" x2="${right}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(${color})"/>`}
  for(const f of (width<280?[0,.5,1]:[0,.25,.5,.75,1])){
    const t=start+f*span;
    grid+=`<text x="${X(t)}" y="${height-5}" text-anchor="${f===0?'start':f===1?'end':'middle'}">${esc(day(t))}</text>`}
  const xy=p=>X(p[0]).toFixed(2)+','+Y(p[1]).toFixed(2);
  let previousJoined=false;
  for(let i=0;i<points.length;i++){
    const p=points[i], prev=points[i-1], reset=resetFor(p), prevReset=prev?resetFor(prev):null;
    const sameCycle=reset!=null&&cycle!=null&&Math.abs(reset-cycle)<=60;
    const joined=prev&&sameCycle&&prevReset!=null&&Math.abs(prevReset-reset)<=60
      &&p[3]==='usage'&&prev[3]==='usage'&&p[0]-prev[0]<=420&&p[1]>=prev[1]
      &&![cycle,prevReset,reset].some(r=>prev[0]<r&&p[0]>=r);
    if(joined) lines+=(previousJoined?'':'M'+xy(prev))+'L'+xy(p);
    else if(prev&&!previousJoined) dots+=quotaDot(prev);
    if(reset!=null){
      if(!sameCycle){cycle=reset; marked=false}
      if(!marked&&start<=reset&&reset<=now){
        marked=true;
        ticks+=`<line class="quota-reset" x1="${X(reset)}" x2="${X(reset)}" y1="${top}" y2="${bottom}" stroke="var(--ink)" opacity=".45" stroke-dasharray="3 4"/>`}}
    previousJoined=joined;
  }
  if(!previousJoined) dots+=quotaDot(points[points.length-1]);
  function quotaDot(p){return `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="2.5" fill="var(--s1)"/>`}
  chart.innerHTML=`<svg width="${width}" height="${height}">${grid}${ticks}<path class="quota-line" d="${lines}" fill="none" stroke="var(--s1)" stroke-width="2"/>${dots}
    <line class="crosshair" x1="0" x2="0" y1="${top}" y2="${bottom}" stroke="var(--ink)" opacity="0"/></svg>`;
  const pct=n=>n.toLocaleString([],{maximumFractionDigits:2})+'%';
  return bindTooltip(chart,chart.querySelector('.crosshair'),tip,(x,y,t)=>{
    if(t!=null){const p=points.find(p=>p[0]===t); return p&&[p,X(t)]}
    let nearest=null, distance=13;
    for(const p of points){const d=Math.hypot(X(p[0])-x,Y(p[1])-y); if(d<distance){nearest=p; distance=d}}
    return nearest&&[nearest,X(nearest[0])];
  },p=>[observed(p[0]),[['Usage',pct(p[1]),'var(--s1)'],['Headroom',pct(100-p[1])],
                        ['Reported reset',p[2]==null?'Unknown':observed(p[2])]]],selected);
}
function renderQuotaHistory(name,card){
  const {plots}=card, series=quotaHistory&&Object.hasOwn(quotaHistory.providers,name)?quotaHistory.providers[name]:[];
  card.width=plots.clientWidth;
  const selected=new Map([...card.selections].map(([key,selection])=>[key,selection()]));
  card.selections.clear();
  plots.replaceChildren();
  const summary=document.createElement('div'); summary.className='muted small';
  summary.textContent=quotaHistory?(series.length?'History as of '+observed(quotaHistory.as_of):'No observations yet')
    :quotaHistoryFailed?'Quota history unavailable':'Loading quota history…';
  plots.append(summary);
  for(const {limit,minutes,points} of series){
    if(!points.length) continue;
    const plot=document.createElement('div'); plot.className='quota-plot';
    plot.innerHTML=`<header><b>${esc(limit)} · ${esc(wlabel(minutes))}</b><span class="muted small">Last observed ${esc(observed(points[points.length-1][0]))}</span></header>
      <div class="quota-chart"></div><div class="tip"></div>`;
    plots.append(plot);
    const key=limit+' '+minutes;
    card.selections.set(key,quotaPlot(plot,points,minutes,quotaHistory.as_of,selected.get(key)));
  }
}
function resizeQuotaPlots(){
  for(const [name,card] of quotaCards) if(card.width!==card.plots.clientWidth) renderQuotaHistory(name,card);
}
function refreshQuotaCards(status){
  const current=new Set();
  for(const [name,p] of Object.entries(status.providers||{})){
    if(p.kind!=='openai-siwc') continue;
    current.add(name);
    if(!quotaCards.has(name)){
      const root=document.createElement('div'), bars=document.createElement('div'), plots=document.createElement('div');
      root.className='card'; plots.className='quota-history'; root.append(bars,plots); $('oai').append(root);
      quotaCards.set(name,{root,bars,plots,selections:new Map()});
    }
    quotaCards.get(name).bars.innerHTML=quotas(name,p);
  }
  for(const [name,card] of quotaCards) if(!current.has(name)){
    card.root.remove(); quotaCards.delete(name);
  }
  resizeQuotaPlots();
}
function panelFetch(path,body){
  const headers=KEY?{Authorization:'Bearer '+KEY}:{};
  const opts={headers,cache:'no-store'};
  if(body){headers['Content-Type']='application/json'; opts.method='POST'; opts.body=JSON.stringify(body)}
  return fetch(path,opts);
}
async function refreshQuotaHistory(){
  try{
    const response=await panelFetch('/rotate/quota-history');
    if(!response.ok) throw new Error('history request failed');
    const data=await response.json();
    if(!Number.isFinite(data.as_of)||!data.providers||typeof data.providers!=='object') return;
    if(quotaHistory&&data.as_of<quotaHistory.as_of) return;
    quotaHistory=data;
  }catch(err){
    if(quotaHistory) return;
    quotaHistoryFailed=true;
  }
  quotaCards.forEach((card,name)=>renderQuotaHistory(name,card));
}
function spark(v){if(!v||!v.length)return '';const w=96,h=24,m=Math.max(...v)||1,y=x=>(h-2-x/m*(h-4)).toFixed(1);
  return `<svg width="${w}" height="${h}"><polyline points="${v.map((x,i)=>(i/(v.length-1)*w).toFixed(1)+','+y(x)).join(' ')}" fill="none" stroke="var(--muted)" stroke-width="1.5"/><circle cx="${w}" cy="${y(v[v.length-1])}" r="3" fill="var(--s1)"/></svg>`}
function billing(b){
  const names=Object.keys(b||{}), pct=v=>v==null?'–':esc(Math.round(v))+'%', date=s=>esc(day(Date.parse(s+'T00:00')/1000));
  if(!names.length) return '<span class="muted small">Add <code>billing</code> to an account in config.json to see its plan, renewal date and monthly use.</span>';
  return names.map(n=>{const a=b[n], left=Math.ceil((Date.parse(a.renews+'T00:00')-Date.now())/D/1000);
    let h=`<div class="acct-head"><b>${esc(n)}</b><span class="badge">${esc(a.plan)} &middot; ${money(a.price)}/mo</span>`
      +(a.renews?`<span class="muted small">renews ${date(a.renews)}${left>=0?' &middot; in '+left+' d':''}</span>`:'')+'</div>'
      +'<table class="cards"><tr><th>Period</th><th class="l">Plan</th><th>Price</th><th>Weekly use</th><th>At its limit</th><th>$ equiv</th><th>All accounts</th></tr>';
    for(const p of a.periods||[]){const u=p.usage||{}, notes=[p.elapsed<p.days?`so far: ${esc(p.elapsed)} of ${esc(p.days)} d`:'',p.covered<p.elapsed?`${esc(p.covered)} of ${esc(p.elapsed)} d recorded`:''].filter(Boolean);
      h+=`<tr><td>${date(p.start)} &ndash; ${date(p.end)}${notes.length?'<span class="sub">'+notes.join(' &middot; ')+'</span>':''}</td>`
        +`<td class="l" data-l="Plan">${(p.plans||[]).map(esc).join(' &rarr; ')}</td><td data-l="Price">${money(p.price)}</td>`
        +`<td data-l="Weekly use">${pct(p.used)}${p.used==null?'':'<span class="sub">busiest week '+pct(p.peak)+'</span>'}</td>`
        +`<td data-l="At its limit">${esc(p.limited_h)} h<span class="sub">all accounts ${esc(p.blocked_h)} h</span></td>`
        +`<td data-l="$ equiv">${money(u.cost_usd)}<span class="sub">${p.price?((u.cost_usd||0)/p.price).toFixed(1)+'&times; the price':''}</span></td>`
        +`<td data-l="All accounts">${p.pool_used==null?'&ndash;':pct(p.pool_used)+(p.pool_used>100?' &#9888;':'')+' &middot; busiest week '+pct(p.pool_peak)+(p.whatif||[]).map(w=>`<span class="sub">on ${esc(w.plan)} (${w.price<0?'&minus;':'+'}$${esc(Math.abs(w.price))}/mo): ${pct(w.pool_used)} &middot; ${pct(w.pool_peak)}${w.pool_used>100?' &#9888;':''}</span>`).join('')}</td></tr>`}
    return h+'</table>'}).join('')
    +'<p class="muted small">Weekly use: the share of its weekly quota the account used over the period, and in its busiest 7 days. All accounts: the same for the combined weekly quota, then with this account on the next plan down or up. An average over 100% cannot last (&#9888;); a busiest week over 100% can fit across weekly resets but means time at the limit.</p>'}
function weekly(p){
  if(!p||!p.series?.length) return 'collecting data';
  const cw=Math.max(300,$('weekly').clientWidth||1100),Hh=cw<600?220:270,L=48,R=12,T=14,B=26, now=p.series[p.series.length-1][0];
  const all=p.series.concat(p.projection.slice(1)), last=p.series[p.series.length-1];
  // The plot scrolls from the first sample to the projection's end and opens centred on now.
  const half=(cw<600?3.5:7.5)*D, x0=Math.min(p.series[0][0],now-half), x1=Math.max(now+half,all[all.length-1][0]);
  const pps=(cw-L-R)/(2*half), st=pps*D<60?2:1;
  const ymax=Math.ceil(Math.max(100,...all.map(q=>q[2]))*1.1/50)*50, gs=ymax>300?100:50;
  const X=t=>(t-x0)*pps, Y=v=>T+(1-v/ymax)*(Hh-T-B), PW=Math.ceil(X(x1)+R);
  const path=(a,i)=>a.map((q,k)=>(k?'L':'M')+X(q[0]).toFixed(1)+','+Y(q[i]).toFixed(1)).join('');
  WK={all,now,x0,pps,start:X(now-half)};
  if(showTable){let h='<table><tr><th>Time</th><th>Usage</th><th>Limit</th><th>Headroom</th></tr>';
    for(const q of all.filter((q,i)=>i%48===0||i===all.length-1)) h+=`<tr><td>${esc(when(q[0]))}${q[0]>now?' (projected)':''}</td><td>${Math.round(q[1])}%</td><td>${Math.round(q[2])}%</td><td>${Math.round(q[2]-q[1])}%</td></tr>`;
    return h+'</table>'+sentence(p,now)}
  let y=`<svg width="${L}" height="${Hh}">`, s=`<svg width="${PW}" height="${Hh}"><rect x="${X(now)}" y="${T}" width="${X(x1)-X(now)}" height="${Hh-T-B}" fill="#fff" opacity=".025"/>`, from=null;
  const band=(a,b)=>`<rect class="hit" x="${X(a)}" y="${T}" width="${(X(b)-X(a)).toFixed(1)}" height="${Hh-T-B}" fill="var(--crit)" opacity=".2"/>`;
  for(const q of all){const hit=q[2]-q[1]<=0.1;  // every account at its weekly limit: usage has met the limit line
    if(hit&&from==null) from=q[0]; else if(!hit&&from!=null){s+=band(from,q[0]);from=null}}
  if(from!=null) s+=band(from,all[all.length-1][0]);
  for(let v=0;v<=ymax;v+=gs){y+=`<text x="${L-6}" y="${Y(v)+4}" text-anchor="end">${v}%</text>`; s+=`<line x1="0" x2="${PW}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v?'var(--grid)':'var(--axis)'}"/>`}
  for(let t=Math.ceil(x0/D/st)*st*D;t<x1;t+=st*D) if(X(t)>18) s+=`<text x="${X(t)}" y="${Hh-8}" text-anchor="middle">${esc(day(t))}</text>`;
  s+=`<line x1="${X(now)}" x2="${X(now)}" y1="${T}" y2="${Hh-B}" stroke="var(--ink2)" opacity=".5"/><text x="${X(now)+4}" y="${T+10}">now</text>${cw<600?'':`<text x="${X(x1)-4}" y="${T+10}" text-anchor="end">projected</text>`}`
    +`<path d="${path(p.series,1)}L${X(now)},${Y(0)}L${X(p.series[0][0])},${Y(0)}Z" fill="var(--s1)" opacity=".1"/>`
    +`<path d="${path(p.series,2)}" fill="none" stroke="var(--s2)" stroke-width="2" stroke-linejoin="round"/><path d="${path(p.series,1)}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>`
    +`<path d="${path(p.projection,2)}" fill="none" stroke="var(--s2)" stroke-width="2" stroke-dasharray="6 5" opacity=".8"/><path d="${path(p.projection,1)}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-dasharray="6 5" opacity=".8"/>`;
  for(let i=1;i<all.length;i++){const dn=all[i-1][1]-all[i][1]; if(dn>=1) s+=`<text x="${X(all[i-1][0])}" y="${Y(all[i-1][1])-8}" text-anchor="middle">reset &minus;${Math.round(dn)}</text>`}  // a weekly reset gave quota back
  for(const [i,c,dy] of [[1,'var(--s1)',18],[2,'var(--s2)',-9]])  // usage label below its dot, limit above
    s+=`<circle cx="${X(now)}" cy="${Y(last[i])}" r="4.5" fill="${c}" stroke="var(--surface)" stroke-width="2"/><text x="${X(now)-8}" y="${Y(last[i])+dy}" text-anchor="end" style="fill:var(--ink)">${Math.round(last[i])}%</text>`;
  return `<div class="wk">${y}</svg><div class="wk-scroll" id="wk-scroll">${s}<line class="crosshair" x1="0" x2="0" y1="${T}" y2="${Hh-B}" stroke="var(--ink)" opacity="0"/></svg></div></div>`+sentence(p,now)}
function sentence(p,now){
  const rate=p.rate_per_day==null?'':`At the current rate (${p.rate_per_day}% of a weekly quota per day), `;
  if(p.eta) return `<div class="sentence">${rate}every account ${p.eta<=now+60?'is at 100% now':'is spent at <b>'+esc(when(p.eta))+'</b>'}; quota returns at ${esc(when(p.back))} when ${esc(p.back_account)} resets.</div>`;
  return `<div class="sentence">${p.note?esc(p.note):rate+'no shortfall in the next 7.5 days.'}</div>`}
function bindTooltip(chart,crosshair,tip,findPoint,format,selected){
  const svg=chart.querySelector('svg');
  let selection=null, tap=null, scroll=chart.scrollLeft;
  function hide(){
    selection=null; tip.style.display='none'; crosshair.setAttribute('opacity','0');
  }
  function show(found,pointerType){
    if(!found){hide(); return}
    const [point,x]=found, r=svg.getBoundingClientRect(), bounds=chart.getBoundingClientRect();
    if(r.left+x<bounds.left-1||r.left+x>bounds.right+1){hide(); return}
    selection={time:point[0],pointerType};
    crosshair.setAttribute('x1',x); crosshair.setAttribute('x2',x); crosshair.setAttribute('opacity','.6');
    const [title,rows]=format(point);
    tip.replaceChildren();
    const head=document.createElement('div'); head.className='muted'; head.textContent=title; tip.append(head);
    for(const [label,value,color] of rows){
      const row=document.createElement('div'), a=document.createElement('span'), b=document.createElement('span');
      row.className='row'; b.className='v'; b.textContent=value; a.textContent=label;
      if(color){const key=document.createElement('span'); key.className='key'; key.style.borderColor=color; a.prepend(key)}
      row.append(a,b); tip.append(row);
    }
    const c=(tip.offsetParent||tip.parentNode).getBoundingClientRect(), px=r.left+x-c.left;
    tip.style.pointerEvents=pointerType==='touch'?'auto':'none';
    tip.style.display='block'; tip.style.top=(r.top-c.top+8)+'px';
    const left=px+16+tip.offsetWidth>c.width?px-16-tip.offsetWidth:px+16;
    tip.style.left=Math.max(8,Math.min(c.width-8-tip.offsetWidth,left))+'px';
  }
  function select(e){
    const r=svg.getBoundingClientRect();
    show(findPoint(e.clientX-r.left,e.clientY-r.top),e.pointerType);
  }
  const moved=e=>Math.hypot(e.clientX-tap.x,e.clientY-tap.y)>8;
  chart.onpointerdown=e=>{
    if(e.pointerType!=='touch') select(e);
    else if(e.isPrimary!==false) tap={id:e.pointerId,x:e.clientX,y:e.clientY};
  };
  chart.onpointermove=e=>{
    if(e.pointerType!=='touch') select(e);
    else if(tap&&e.pointerId===tap.id&&moved(e)){tap=null; hide()}
  };
  chart.onpointerup=e=>{
    if(!tap||e.pointerId!==tap.id) return;
    if(moved(e)) hide(); else select(e);
    tap=null;
  };
  chart.onpointercancel=()=>{tap=null; hide()};
  chart.onpointerleave=e=>{if(e.pointerType!=='touch') hide()};
  chart.onscroll=()=>{if(chart.scrollLeft!==scroll){scroll=chart.scrollLeft; tap=null; hide()}};
  tip.onclick=hide;
  if(selected) show(findPoint(null,null,selected.time),selected.pointerType); else hide();
  return ()=>selection;
}
function refreshWeekly(wk){
  const selected=weeklySelection&&weeklySelection(), sc=$('wk-scroll'), tip=$('tip');
  const keep=sc&&sc.scrollWidth?sc.scrollWidth-sc.clientWidth-sc.scrollLeft:null;
  WK=null;
  $('weekly').innerHTML=weekly(wk);
  const plot=$('wk-scroll');
  if(!plot||!WK){tip.style.display='none'; weeklySelection=null; return}
  const {all,now,x0,pps,start}=WK;
  plot.scrollLeft=keep==null?start:plot.scrollWidth-plot.clientWidth-keep;
  const pct=v=>Math.round(v)+'%';
  weeklySelection=bindTooltip(plot,plot.querySelector('.crosshair'),tip,(x,y,t)=>{
    let point;
    if(t!=null) point=all.find(p=>p[0]===t);
    else{
      const time=x0+x/pps;
      point=all[0];
      for(const p of all) if(Math.abs(p[0]-time)<Math.abs(point[0]-time)) point=p;
    }
    return point&&[point,(point[0]-x0)*pps];
  },q=>[when(q[0])+(q[0]>now?' · projected':''),[
    ['Usage',pct(q[1]),'var(--s1)'],['Limit',pct(q[2]),'var(--s2)'],
    [q[2]-q[1]<=0.1?'Blocked':'Headroom',pct(q[2]-q[1])]]],selected);
}
$('tbl').onclick=()=>{showTable=!showTable; $('tbl').textContent=showTable?'Chart':'Table'; refresh()};
$('period').onclick=e=>{const p=e.target.dataset&&e.target.dataset.p; if(p){period=p; refresh()}};
const eligible=a=>Object.entries(a.eligible_by_family||{}).filter(([,ok])=>ok).map(([f])=>f);
$('pills').onclick=$('accounts').onclick=e=>{const b=e.target.closest&&e.target.closest('button[data-sw],button[data-auto]'); if(!b||b.disabled) return;
  PENDING=b.dataset.auto?{auto:true}:{account:b.dataset.sw};
  $('cf-text').innerHTML=PENDING.auto?`Resume the ${esc(ST.strategy)} strategy for both family routes?`
    :`Prefer <b>${esc(PENDING.account)}</b> for ${eligible(ST.accounts[PENDING.account]).map(familyLabel).join(' and ')} automatic traffic? The preference lasts until a switch threshold or quota limit is reached, or the account is disabled.`;
  $('cf-err').textContent=''; $('confirm').showModal()};
$('cf-no').onclick=()=>$('confirm').close();
$('cf-yes').onclick=async()=>{$('cf-yes').disabled=true;
  try{const r=await panelFetch('/rotate/switch',PENDING);
    if(r.ok){$('confirm').close(); refresh()} else $('cf-err').textContent=(await r.json().catch(()=>({}))).error||'error '+r.status}
  catch(err){$('cf-err').textContent='request failed'} finally{$('cf-yes').disabled=false}};
let pinDevice;
const pinMode=()=>$('pin-mode').value;
const wantsPin=()=>['strict','fallback'].includes(pinMode());
$('pin-mode').onchange=()=>{$('pin-target').hidden=!wantsPin()};
$('devices').onclick=e=>{
  const b=e.target.closest&&e.target.closest('button[data-pin]');
  if(!b)return;
  pinDevice=b.dataset.pin;
  const {account_pin:pin,source}=ST.device_pins[pinDevice];
  $('pin-title').textContent='Claude routing for '+pinDevice;
  $('pin-error').textContent='';
  $('pin-mode').value=source==='runtime'?(pin?pin.mode:'auto'):'use_config';
  $('pin-account').innerHTML=Object.entries(ST.pin_targets||{}).map(([name,available])=>
    `<option value="${esc(name)}">${esc(name)}${available?'':' (unavailable)'}</option>`).join('');
  $('pin-account').value=pin?pin.account:Object.keys(ST.pin_targets||{})[0];
  $('pin-mode').onchange();
  $('pin-form').showModal();
};
$('pin-cancel').onclick=()=>$('pin-form').close();
$('pin-save').onclick=async()=>{
  $('pin-save').disabled=true;
  const mode=pinMode(), payload={device:pinDevice};
  if(mode==='use_config')payload.use_config=true;
  else payload.account_pin=wantsPin()?{mode,account:$('pin-account').value}:null;
  try{
    const r=await panelFetch('/rotate/device-pin',payload);
    if(r.ok){$('pin-form').close(); refresh()}
    else $('pin-error').textContent=(await r.json().catch(()=>({}))).error||'error '+r.status;
  }catch(err){$('pin-error').textContent='request failed'}finally{$('pin-save').disabled=false}
};
async function refresh(){
  const [st, stats] = await Promise.all([
    panelFetch('/rotate/status').then(r=>r.json()), panelFetch('/rotate/stats').then(r=>r.json())]);
  const per=stats.periods||{}, wk=st.weekly||{}, accts=Object.entries(st.accounts||{}); ST=st;
  const active=st.active_by_family||{}, manual=st.manual_by_family||{};
  const canRotate=(name,a)=>eligible(a).some(f=>active[f]!==name);
  const i=accts.findIndex(([n])=>n===active.default);
  const nxt=accts.slice(i+1).concat(accts.slice(0,i+1)).find(([n,a])=>canRotate(n,a))?.[0];
  $('host').textContent=location.host||'';
  $('pills').innerHTML=Object.entries(active).map(([f,n])=>`<span class="pill">${familyLabel(f)}: ${esc(n||'–')}</span>`).join('')
    +`<button class="linkbtn" data-sw="${esc(nxt)}"${nxt?'':' disabled title="no other account is eligible"'}>Rotate</button> <span class="pill">${esc(st.strategy)} &middot; ${accts.length} accounts &middot; ${Object.keys(stats.devices||{}).length} devices</span>`
    +Object.entries(st.providers||{}).map(([n,p])=>`<span class="pill" title="${esc(p.reason||p.unsaved||p.last_error||p.kind)}"><span class="dot" style="background:var(--${['down','unavailable'].includes(p.state)?'crit':p.state!=='ok'||p.unsaved||p.last_error?'warn':'good'})"></span>${esc(n)}</span>`).join('');
  $('alerts').innerHTML=(stats.alerts||[]).map(a=>`<div class="banner ${a.level==='warn'?'warn':''}">&#9888; <span><b>${a.level==='warn'?'Warning':'Note'}</b> ${esc(a.text)}</span></div>`).join('');
  // headline figures
  const last=(wk.series||[]).slice(-1)[0], now=last?last[0]:Date.now()/1000;
  $('hero').textContent=wk.eta?(wk.eta<=now+60?'now':dur(wk.eta-now)):wk.rate_per_day!=null?'> 7.5 days':'–';
  $('hero-sub').textContent=wk.eta?`${when(wk.eta)} · quota returns ${when(wk.back)} when ${wk.back_account} resets`:(wk.note||'no shortfall projected');
  $('k-left').textContent=last?Math.round(last[2]-last[1])+'%':'–'; $('k-left-sub').textContent=`of ${wk.capacity||0}% · ${accts.length} accounts`;
  $('k-rate').textContent=wk.rate_per_day!=null?wk.rate_per_day+'%':'–';
  $('k-req').textContent=fmt(sum(stats.by_device,b=>b.requests)); $('k-req-sub').textContent=fmt(sum(stats.by_device,tok))+' tokens';
  $('k-cost').textContent=money(sum((per['7d']||{}).model,b=>b.cost_usd)); $('k-cost-sub').textContent=money(sum(stats.by_model,b=>b.cost_usd))+' in the last 24 h';
  // accounts
  $('accounts').innerHTML=accts.map(([name,a])=>{const bl=(stats.billing||{})[name];
    const seen=a.last_seen?ago(Math.round((Date.now()/1000-a.last_seen)/60)):'never';
    const badges=Object.entries(active).filter(([,n])=>n===name).map(([f])=>
      `<span class="badge"><span class="dot" style="background:var(--good)"></span>${familyLabel(f)} active${manual[f]===name?' &middot; manual':''}</span>`);
    if(bl)badges.push(`<span class="badge">${esc(bl.plan)}${bl.renews?' &middot; renews '+esc(day(Date.parse(bl.renews+'T00:00')/1000)):''}</span>`);
    if(a.disabled)badges.push('<span class="badge">disabled</span>');
    if(a.priority!==100)badges.push(`<span class="badge">prio ${esc(a.priority)}</span>`);
    return `<div class="card"><div class="acct-head"><b>${esc(name)}</b>`
      +`<span class="acct-side"><span class="muted small">upstream: ${esc(a.status||'n/a')} &middot; seen ${seen}</span>`
      +(eligible(a).some(f=>manual[f]!==name)?`<button class="linkbtn" data-sw="${esc(name)}">Select</button>`:'')
      +(Object.values(manual).includes(name)?'<button class="linkbtn" data-auto="1">Resume auto</button>':'')+'</span></div>'
      +claimMeters(a,Date.now()/1000)
      +(badges.length?`<div class="acct-badges">${badges.join('')}</div>`:'')+'</div>'}).join('');
  refreshQuotaCards(st);
  refreshWeekly(wk);
  $('billing').innerHTML=billing(stats.billing);
  // devices: configured ones, then removed ones that still have history
  const roster=stats.devices||{}, gone=Object.keys((per.all||{}).device||{}).filter(n=>!(n in roster)&&n!=='?');
  let dh=`<tr><th>Device</th><th class="l">Status</th><th class="l">Last model</th><th>24 h</th><th>7 d</th><th>30 d</th><th>All time${per.since?' <span class="muted">since '+esc(per.since)+'</span>':''}</th><th>30-day trend</th></tr>`;
  for(const name of Object.keys(roster).concat(gone)){const d=roster[name], removed=!d;
    const status=removed?'removed':d.online?'<span class="dot" style="background:var(--good)"></span>online':'<span class="dot" style="background:var(--muted)"></span>idle';
    const pins=st.device_pins||{}, pin=d&&Object.hasOwn(pins,name)?pins[name]:null;
    const route=pin?.account_pin, source=pin?.source;
    const routing=route?route.account+' · '+route.mode+' ('+source+')':source==='runtime'?'Auto until restart':'Automatic';
    dh+=`<tr class="${removed?'removed':''}"><td><b>${esc(name)}</b>${d?'<span class="sub">key '+esc(d.key_suffix)+((d.providers||[]).join()!=='anthropic'?' &middot; '+esc((d.providers||[]).join(', ')):'')+'</span>':''}${pin?`<span class="sub">${esc(routing)} <button class="linkbtn" data-pin="${esc(name)}" aria-label="Edit Claude routing for ${esc(name)}">Edit</button></span>`:''}</td><td class="l">${status}${d?'<span class="sub">'+ago(d.last_seen_min)+'</span>':''}</td><td class="l" data-l="Last model">${d&&d.last_provider?esc(d.last_provider)+' &middot; ':''}${esc(d&&d.last_model||'–')}</td>`
      +cell((stats.by_device||{})[name],'24 h')+cell(((per['7d']||{}).device||{})[name],'7 d')+cell(((per['30d']||{}).device||{})[name],'30 d')
      +cell(((per.all||{}).device||{})[name],'All time'+(per.since?' since '+esc(per.since):''))+`<td data-l="30-day trend">${spark((per.trend||{})[name])}</td></tr>`}
  $('devices').innerHTML=dh;
  // models over the chosen period; accounts over 24 h
  $('period').innerHTML=Object.keys(PERIODS).map(p=>`<button data-p="${p}" class="${p===period?'on':''}">${p}</button>`).join('');
  const rows=o=>Object.entries(o||{}).sort((a,b)=>(b[1].cost_usd||0)-(a[1].cost_usd||0)).map(([n,b])=>{const prompt=(b.input_tokens||0)+(b.cache_read||0)+(b.cache_write||0);
    return `<tr><td>${esc(n)}</td><td>${fmt(b.requests)}</td><td>${fmt(tok(b))}</td><td>${prompt?Math.round((b.cache_read||0)/prompt*100):0}%</td><td>${money(b.cost_usd)}</td></tr>`}).join('');
  const head='<tr><th>{}</th><th>Requests</th><th>Tokens</th><th>From cache</th><th>$ equiv</th></tr>';
  $('models').innerHTML=head.replace('{}','Model')+rows(PERIODS[period]==='24h'?stats.by_model:(per[PERIODS[period]]||{}).model);
  $('by-account').innerHTML=head.replace('{}','Account')+rows(stats.by_account)
    +rows(Object.fromEntries(Object.entries(stats.by_provider||{}).map(([n,b])=>[n+' (provider)',b])));
  $('events').innerHTML=(st.recent_events||[]).slice().reverse().map(e=>`<div><span class="muted">${esc(String(e.ts||'').slice(5,16).replace('T',' '))}</span><span>${e.family?familyLabel(e.family)+' · ':''}${esc(e.from)} &rarr; ${esc(e.to)}</span><span class="badge" style="margin-left:auto">${esc(e.reason)}</span></div>`).join('')||'<span class="muted">none</span>';
  $('upd').textContent='updated '+new Date().toLocaleTimeString();
}
refresh(); setInterval(refresh, 10000);
refreshQuotaHistory(); setInterval(refreshQuotaHistory, 60000);
window.addEventListener('resize',resizeQuotaPlots);
</script></body></html>"""


@app.get("/rotate/panel")
async def rotate_panel(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key — open /rotate/panel?key=<device key>"},
                            status_code=401)
    # The panel can switch accounts: no other page may frame it and steer a click.
    return Response(content=PANEL_HTML, media_type="text/html",
                    headers={"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY"})


async def retry_wait(seconds: float, request: Request | None) -> None:
    """Release held request bodies promptly when their caller has left."""
    deadline = time.monotonic() + seconds
    while True:
        if request is not None and await request.is_disconnected():
            raise HTTPException(status_code=499, detail="client disconnected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        interval = min(remaining, 1.0) if request is not None else remaining
        await asyncio.sleep(interval)


RATE_LIMIT_PREFIX = "anthropic-ratelimit-"
MAX_RATE_LIMIT_ATTEMPTS = 10
MAX_RATE_LIMIT_ERROR_BYTES = 64 * 1024


def rate_limit_headers(headers: httpx.Headers) -> dict:
    out = {}
    for name, value in headers.items():
        if name.startswith(RATE_LIMIT_PREFIX):
            out.setdefault(name.removeprefix(RATE_LIMIT_PREFIX)[:100], value[:200])
            if len(out) == 40:
                break
    return out


def rate_limit_error(entry: dict, content: bytes | bytearray, headers: httpx.Headers) -> None:
    if len(content) > MAX_RATE_LIMIT_ERROR_BYTES:
        return
    if "text/event-stream" in headers.get("content-type", ""):
        content = next((line[5:].lstrip() for line in content.splitlines() if line.startswith(b"data:")), content)
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        return
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        fields = {key: error[key][:limit] for key, limit in (("type", 100), ("message", 300))
                  if isinstance(error.get(key), str)}
        if fields:
            entry["error"] = fields


def add_claude_audit_fields(rec: dict, upstream: httpx.Response, account: str, retries: int) -> None:
    rec.update(account=account, status=upstream.status_code)
    if retries:
        rec["retries"] = retries


async def send_upstream(method: str, url: str, fwd_headers: dict, body: bytes, rec: dict,
                        request: Request | None = None, *, family: str | None = None):
    """Choose an account on every attempt and observe its headers before handing off the body."""
    route_family = family or "default"
    hold_deadline = time.monotonic() + HOLD_MAX_S
    retries = 0
    verdict = None
    account = None
    burst_accounts = set()
    retrying = False
    try:
        while True:
            await retry_wait(0, request)
            async with state_lock:
                now = time.time()
                pin, _ = device_pin(rec["device"])
                policy = pin["mode"] if pin else "automatic"
                account = None
                if pin:
                    target = pin["account"]
                    available = target in ACCOUNTS and not disabled(target) and bool(ACCOUNTS[target].get("token"))
                    if policy == "strict" and not available:
                        raise HTTPException(status_code=503, detail=f"strict pin target '{target}' is unavailable")
                    if available and (policy == "strict" or blocker_deadline(target, route_family, now) <= now):
                        account = target
                if account is None:
                    account = select_account(route_family, now, update=policy == "automatic")
                all_blocked = account is None
                if all_blocked:
                    enabled = [name for name in ACCOUNTS if not disabled(name)]
                    if not enabled:
                        raise HTTPException(status_code=503, detail="no enabled accounts")
                    wait = min(earliest_relief(route_family, now) - now, hold_deadline - time.monotonic())
                    if wait > 0:
                        source = family_route(route_family)["active_account"]
                        STATE["events"].append({
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "hold", "family": route_family,
                            "from": source, "to": source, "reason": f"all accounts spent; holding {round(wait)}s"})
                        STATE["events"] = STATE["events"][-200:]
                        save_state()
                    else:
                        account = next_blocked_account(family, enabled, now)
                elif policy != "strict":
                    LAST_BLOCKED_ACCOUNT.pop(family, None)
                if account is not None:
                    info = STATE["accounts"].get(account, {})
                    pace = (0 if policy == "strict" or all_blocked else
                            info.get("pace_by_family", {}).get(route_family, info.get("pace_until", 0)) - now)
                    token = ACCOUNTS[account]["token"]
            if account is None:
                await retry_wait(wait, request)
                continue
            if pace > 0:
                await retry_wait(min(pace, 60), request)
                continue
            if retrying:
                retries += 1
            fwd_headers["authorization"] = f"Bearer {token}"
            outbound = client.build_request(method, url, headers=fwd_headers, content=body)
            upstream = await client.send(outbound, stream=True)
            received_at = time.time()
            try:
                verdict = await note_response(account, upstream.headers, upstream.status_code, family=family,
                                              repeated_burst=account in burst_accounts, policy=policy,
                                              model=rec["model"], path=outbound.url.path, rec=rec,
                                              received_at=received_at)
            except BaseException:
                await upstream.aclose()
                raise
            if upstream.status_code != 429:
                return upstream, account, retries, verdict
            if policy == "strict":
                verdict = "strict"
            retry_after = upstream.headers.get("retry-after")
            entry = {"account": account, "verdict": verdict, "at": received_at,
                     "retry_after": retry_after[:200] if retry_after is not None else None,
                     "headers": {}}
            try:
                entry["headers"] = rate_limit_headers(upstream.headers)
            except Exception as exc:
                storage_error("ratelimit_diagnostics", exc)
            entries = rec.setdefault("rate_limits", [])
            if len(entries) < MAX_RATE_LIMIT_ATTEMPTS:
                entries.append(entry)
            else:
                entries[-1] = entry
                rec["rate_limits_omitted"] = rec.get("rate_limits_omitted", 0) + 1

            retry = (verdict == "exhausted" and time.monotonic() < hold_deadline
                     or verdict in ("switched", "burst") and retries < 5)
            if all_blocked or not retry:
                return upstream, account, retries, verdict
            content = await bounded_response(upstream)
            rate_limit_error(entry, content, upstream.headers)
            if verdict == "burst":
                burst_accounts.add(account)
            else:
                burst_accounts.discard(account)
            retrying = True
    except (Exception, asyncio.CancelledError) as exc:
        if rec.get("rate_limits"):
            status = 500
            if isinstance(exc, HTTPException):
                status = exc.status_code
            elif isinstance(exc, httpx.HTTPError):
                status = 502
            elif isinstance(exc, asyncio.CancelledError):
                status = 499
            rec.update(account=account or rec["rate_limits"][-1]["account"], status=status)
            if retries:
                rec["retries"] = retries
            try:
                audit(rec)
            except Exception as audit_error:
                storage_error("audit_write", audit_error)
        raise


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
    if not granted(device, ANTHROPIC):
        return JSONResponse({"error": {"message": f"claude-rotate: device '{device}' may not use "
                                                  "the Claude accounts", "type": "permission_error"}},
                            status_code=403)
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
    rec = {"device": device, "path": "/v1/chat/completions", "method": "POST", "model": abody.get("model")}
    family = model_family(rec["model"])
    upstream, account, retries, verdict = await send_upstream(
        "POST", "/v1/messages", fwd, json.dumps(abody).encode(), rec, request, family=family)

    add_claude_audit_fields(rec, upstream, account, retries)
    resp_headers = {"x-rotate-account": account} if account else {}
    for h in ("anthropic-ratelimit-unified-5h-utilization",
              "anthropic-ratelimit-unified-7d-utilization",
              "anthropic-ratelimit-unified-5h-reset",
              "anthropic-ratelimit-unified-status"):
        if upstream.headers.get(h):
            resp_headers[h] = upstream.headers[h]
    if upstream.status_code == 429 and upstream.headers.get("retry-after") is not None:
        resp_headers["retry-after"] = upstream.headers["retry-after"]

    if upstream.status_code == 429 and verdict == "exhausted":
        content = await bounded_response(upstream)
        rate_limit_error(rec["rate_limits"][-1], content, upstream.headers)
        now = time.time()
        retry_after = max(1, round(earliest_relief(family, now) - now))
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
                audit(rec)

        return StreamingResponse(relay(), status_code=200, headers=resp_headers,
                                 media_type="text/event-stream")

    content = await bounded_response(upstream)
    if upstream.status_code == 429:
        rate_limit_error(rec["rate_limits"][-1], content, upstream.headers)
    if upstream.status_code != 200:
        try:
            msg = json.loads(content).get("error", {}).get("message") \
                or content[:200].decode(errors="replace")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            msg = content[:200].decode(errors="replace")
        audit(rec)
        return JSONResponse({"error": {"message": f"upstream: {msg}",
                                       "type": "api_error"}},
                            status_code=upstream.status_code, headers=resp_headers)
    data = json.loads(content)
    rec["usage"] = summarize_usage(data.get("usage"))
    audit(rec)
    return JSONResponse(anthropic_to_oai(data), headers=resp_headers)


class StreamBroken(Exception):
    """An upstream stream broke off after the response headers went out."""


class QuietBreaks:
    """A broken stream must reach the client as a broken connection, so the response
    is left unfinished; Uvicorn then closes the connection and logs one line
    ("ASGI callable returned without completing response") instead of a traceback.
    The audit record already names the upstream error."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except StreamBroken:
            pass


app.add_middleware(QuietBreaks)


def connection_nominated(headers) -> set:
    """Headers a Connection header names are hop-by-hop too (RFC 9110, section 7.6.1),
    on every Connection line: Starlette's get() returns only the first."""
    return {t.strip().lower() for k, v in headers.items() if k.lower() == "connection"
            for t in v.split(",") if t.strip()}


def forward_headers(request: Request) -> dict:
    """Client headers minus hop-by-hop, browser and reverse-proxy ones, and x-api-key."""
    drop = HOP | PRIVATE_HEADERS | connection_nominated(request.headers)
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in drop
           and not k.lower().startswith(("tailscale-", "x-forwarded-", "sec-fetch-"))}
    fwd["accept-encoding"] = "identity"
    fwd.pop("x-api-key", None)
    return fwd


def response_headers(upstream: httpx.Response, *extra: str) -> dict:
    drop = HOP | {"content-encoding", *extra} | connection_nominated(upstream.headers)
    return {k: v for k, v in upstream.headers.items() if k.lower() not in drop}


def upstream_path(request: Request, drop: int = 0, confined: bool = False) -> str:
    """The path as the client sent it, percent-encoding kept, without its first `drop`
    segments. Rebuilt from the decoded path, an encoded "?" or "/" would become a
    delimiter, and a segment that decodes to "." or ".." could climb out of the
    upstream's base path once normalized; both are refused. `confined`, for an
    upstream mounted under a base path, also refuses what a server behind it could
    still turn into one: more layers of encoding, or ";" parameters it strips."""
    raw = request.scope.get("raw_path") or quote(request.scope["path"]).encode()
    try:
        raw = raw.split(b"?", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="path must use percent-encoding")
    segments = raw.split("/")[1:]
    for segment in segments:
        forms, plain = [], segment
        for _ in range(4 if confined else 1):
            plain = unquote(plain)
            forms += [plain, plain.split(";", 1)[0]] if confined else [plain]
        if any(f in (".", "..") or "/" in f or "\\" in f for f in forms):
            raise HTTPException(status_code=400, detail="path has a dot segment or an encoded separator")
    return "/" + "/".join(segments[drop:])


def upstream_url(base: str, path: str, query: str) -> httpx.URL:
    """base + path + query, checked to stay on base's origin and under its path."""
    try:
        origin, url = httpx.URL(base), httpx.URL(base.rstrip("/") + path + query)
    except httpx.InvalidURL:
        raise HTTPException(status_code=400, detail="path is not a valid URL path")
    root = origin.raw_path.split(b"?")[0].rstrip(b"/")
    if (url.scheme, url.host, url.port) != (origin.scheme, origin.host, origin.port) \
            or not (url.raw_path + b"/").startswith(root + b"/"):
        raise HTTPException(status_code=400, detail="path leaves the upstream")
    return url


def upstream_query(request: Request) -> str:
    """The raw query, spelling and order kept, without the device key."""
    try:
        query = request.scope["query_string"].decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="query string must use percent-encoding")
    query = "&".join(part for part in query.split("&")
                     if unquote_plus(part.partition("=")[0]) != "key")
    return "?" + query if query else ""


def head_length(response: Response, request: Request, upstream: httpx.Response) -> Response:
    """A HEAD answer carries the length the upstream gave for the body it left out, or
    none: not the 0 Starlette computes from the empty body. A Connection-nominated
    length is hop-by-hop and is not passed on."""
    if request.method == "HEAD":
        length = upstream.headers.get("content-length")
        del response.headers["content-length"]
        if length is not None and "content-length" not in connection_nominated(upstream.headers):
            response.headers["content-length"] = length
    return response


class Hold:
    """Something a request holds until its answer is over. Released once, by release()
    or, for a stream that never started, when the response is dropped."""

    def __init__(self, release):
        self._finalizer = weakref.finalize(self, release)

    def release(self) -> None:
        self._finalizer()


class LargeBodies:
    """Bytes of request bodies above max_request_bytes in memory, under the providers
    with images; each stays until its answer ends."""

    def __init__(self):
        self.held = 0

    def take(self, size: int) -> Hold:
        if size <= MAX_REQUEST_BYTES:
            return Hold(lambda: None)
        if self.held + size > MAX_IMAGE_BYTES_IN_FLIGHT:
            raise HTTPException(status_code=503, headers={"Retry-After": "5"},
                                detail="too many large requests in flight; retry later")
        self.held += size
        return Hold(lambda: self.give(size))

    def give(self, size: int) -> None:
        self.held -= size


class ImageLine:
    """At most max_concurrent_images image requests at the backend, up to IMAGE_LINE_MAX
    more waiting; a waiter whose client leaves gives up its place."""

    def __init__(self):
        self.active = self.waiting = 0

    async def enter(self, alive) -> Hold | None:
        if self.active >= MAX_CONCURRENT_IMAGES:
            if self.waiting >= IMAGE_LINE_MAX:
                return None
            self.waiting += 1
            try:
                while self.active >= MAX_CONCURRENT_IMAGES:
                    if not await alive():
                        raise providers.ClientGone()
                    await asyncio.sleep(0.25)
            finally:
                self.waiting -= 1
        self.active += 1  # no await since the check: one event loop
        return Hold(self.leave)

    def leave(self) -> None:
        self.active -= 1


LARGE_BODIES = LargeBodies()
IMAGE_LINE = ImageLine()


def request_model(request: Request, body: bytes) -> str | None:
    if body and request.method == "POST":
        try:
            model = json.loads(body).get("model")
            return model if isinstance(model, str) else None
        except (ValueError, RecursionError, AttributeError):
            pass
    return None


async def provider_proxy(request: Request, prov):
    """A plain forward to another provider: its own client and credential, the device
    key stripped, no pacing or rotation, so its 429s never touch a Claude account."""
    def refuse(status, message, kind):
        return JSONResponse({"error": {"message": f"claude-rotate: {message}", "type": kind}},
                            status_code=status)

    device = device_from_request(request)
    if device is None:
        return refuse(401, "unknown device key", "authentication_error")
    if not granted(device, prov.name):
        return refuse(403, f"device '{device}' may not use provider '{prov.name}'", "permission_error")
    images = getattr(prov, "images", None)
    path = upstream_path(request, drop=1, confined=prov.kind == "static-bearer"
                         and httpx.URL(prov.upstream).raw_path.strip(b"/") != b"")
    search = request.method == "POST" and path == providers.SEARCH_PATH
    body = await bounded_body(request, MAX_IMAGE_REQUEST_BYTES if images and not search else None)
    if search and images:
        return await codex_proxy(request, prov, device, providers.SEARCH_ENDPOINT, path, body, refuse)
    hold = LARGE_BODIES.take(len(body))
    streaming = False
    try:
        endpoint = providers.IMAGE_PATHS.get(path) if images and request.method == "POST" else None
        if endpoint:
            response = await codex_proxy(request, prov, device, endpoint, path, body, refuse, hold)
        else:
            response = await forward(request, prov, device, path, body, hold, refuse)
        streaming = isinstance(response, StreamingResponse)
        return response
    finally:
        if not streaming:
            hold.release()


async def forward(request: Request, prov, device: str, path: str, body: bytes, hold: Hold, refuse):
    """The provider's own route: its upstream and its credential."""
    fwd = forward_headers(request)
    fwd.pop("authorization", None)
    url = upstream_url(prov.upstream, path, upstream_query(request))
    rec = {"device": device, "provider": prov.name, "path": prov.prefix + path,
           "method": request.method, "model": request_model(request, body)}

    async def alive():
        return not await request.is_disconnected()

    try:
        upstream, retries, notes = await prov.send(request.method, url, fwd, body, alive)
    except providers.ClientGone:
        audit(rec | {"status": 499})
        raise HTTPException(status_code=499, detail="client disconnected")
    except providers.ProviderDown as exc:
        audit(rec | exc.notes | {"status": 503})
        return refuse(503, f"provider '{prov.name}' is unavailable: {exc}", "provider_unavailable")
    except httpx.HTTPError as exc:
        audit(rec | {"status": 502})
        return refuse(502, f"provider '{prov.name}' is unreachable: {providers.connect_failure(exc)}", "api_error")
    rec["status"] = upstream.status_code
    if retries:
        rec["retries"] = retries
    rec.update(notes)
    if hasattr(prov, "record_quotas"):
        prov.record_quotas(providers.quota_snapshots(upstream.headers), "inference", time.time())
    # Upstream cookies belong to the upstream. A provider page opened in a browser must
    # not run script on this origin, where the panel and device keys are used.
    # Uvicorn adds its own Date and Server; the upstream's would make a second of each.
    resp_headers = response_headers(upstream, "set-cookie", "content-security-policy", "date", "server")
    resp_headers["content-security-policy"] = "default-src 'none'; sandbox; frame-ancestors 'none'"
    resp_headers["x-rotate-provider"] = prov.name

    # Relayed as it arrives unless the upstream sent a sized answer, which is complete
    # once read: OpenAI's /v1/responses streams carry no Content-Type, so that header
    # cannot decide. Only a sized answer is buffered, so only it meets max_response_bytes.
    if request.method != "HEAD" and ("text/event-stream" in upstream.headers.get("content-type", "")
                                     or "content-length" not in upstream.headers):
        meter = providers.StreamUsage()

        async def relay():
            done = ended = False
            try:
                async for chunk in upstream.aiter_bytes():
                    meter.feed(chunk)
                    yield chunk
                    ended = meter.ended  # set only once the client has been handed it
                done = True
            except httpx.HTTPError as exc:
                if not ended:
                    # Headers are out, so no 502 can follow: the client must see the stream
                    # break, not a clean end it would take for a complete answer.
                    rec["interrupted"] = f"upstream {type(exc).__name__}"
                    raise StreamBroken() from exc
            finally:
                # A client that hangs up after the terminal event has the whole answer.
                if not done and not ended and "interrupted" not in rec:
                    rec["interrupted"] = "client"
                await upstream.aclose()
                hold.release()
                rec["usage"] = meter.usage
                audit(rec)

        return StreamingResponse(relay(), status_code=upstream.status_code, headers=resp_headers)

    try:
        content = await bounded_response(upstream)
    except HTTPException as exc:  # larger than max_response_bytes; already closed
        audit(rec | {"status": exc.status_code, "upstream_status": upstream.status_code})
        return refuse(exc.status_code, str(exc.detail), "api_error")
    except httpx.HTTPError as exc:
        await upstream.aclose()
        audit(rec | {"status": 502, "upstream_status": upstream.status_code})
        return refuse(502, f"provider '{prov.name}' broke off its answer ({type(exc).__name__})", "api_error")
    try:
        rec["usage"] = providers.usage_of(json.loads(content))
    except (ValueError, RecursionError):
        pass
    audit(rec)
    return head_length(Response(content=content, status_code=upstream.status_code, headers=resp_headers),
                       request, upstream)


async def codex_proxy(request: Request, prov, device: str, endpoint: str, path: str, body: bytes,
                      refuse, hold: Hold | None = None):
    """Forward to the ChatGPT backend with the Codex login, body unchanged."""
    login = prov.images
    search = endpoint == providers.SEARCH_ENDPOINT
    operation = "web search" if search else "image generation"
    rec = {"device": device, "provider": prov.name, "path": prov.prefix + path, "method": "POST",
           "route": "codex-search" if search else "codex-images",
           "model": request_model(request, body) if len(body) <= 65536 else None}
    started = time.monotonic()

    async def alive():
        return not await request.is_disconnected()

    def finish(**extra):
        audit(rec | {"bytes": 0, "duration_s": round(time.monotonic() - started, 1)} | extra)

    def unavailable(status, message, kind):
        finish(status=status)
        return refuse(status, f"{operation} is unavailable: {message}", kind)

    slot = None
    streaming = False
    try:
        try:
            if not search:
                slot = await IMAGE_LINE.enter(alive)
                if slot is None:
                    return unavailable(429, f"{IMAGE_LINE_MAX} image requests are already waiting; retry later",
                                       "rate_limit_error")
            upstream, retries, notes = await login.send(
                "POST", providers.codex_url(endpoint), providers.codex_headers(forward_headers(request), search),
                body, alive)
        except providers.ClientGone:
            finish(status=499)
            raise HTTPException(status_code=499, detail="client disconnected")
        except providers.ProviderDown as exc:
            rec.update(exc.notes)
            return unavailable(503, str(exc), "provider_unavailable")
        except httpx.HTTPError as exc:
            return unavailable(502, f"the ChatGPT backend is unreachable: {providers.connect_failure(exc)}",
                               "api_error")
        if not search:
            login.note_answer(upstream.status_code)
        prov.record_quotas(providers.quota_snapshots(upstream.headers), "inference" if search else "images", time.time())
        rec["status"] = upstream.status_code
        if retries:
            rec["retries"] = retries
        rec.update(notes)
        ctype = upstream.headers.get("content-type", "")
        if (upstream.status_code >= 400 and "json" not in ctype.lower()
                and rec.get("upstream_error") not in providers.REFUSED_OPERATION):
            # A Cloudflare challenge page or the like: Codex hands the body to the model.
            await upstream.aclose()
            finish(upstream_content_type=ctype[:64])
            return refuse(upstream.status_code, f"{operation} failed: the ChatGPT backend answered "
                          f"{upstream.status_code} with {ctype or 'no content type'}", "api_error")
        resp_headers = response_headers(upstream, "set-cookie", "content-security-policy", "date", "server")
        resp_headers["content-security-policy"] = "default-src 'none'; sandbox; frame-ancestors 'none'"
        resp_headers["x-rotate-provider"] = prov.name

        if search:
            try:
                content = await buffered_search(request, upstream)
            except HTTPException as exc:
                finish(status=exc.status_code, upstream_status=upstream.status_code)
                return refuse(exc.status_code, str(exc.detail), "api_error")
            except httpx.HTTPError as exc:
                finish(status=502, upstream_status=upstream.status_code)
                return refuse(502, f"the ChatGPT backend broke off its answer ({type(exc).__name__})", "api_error")
            finish(bytes=len(content))
            return Response(content=content, status_code=upstream.status_code, headers=resp_headers)

        # Codex retries a 5xx, and each retry is a new image. Relay as it arrives.
        async def relay():
            sent, done = 0, False
            try:
                async for chunk in upstream.aiter_bytes():
                    sent += len(chunk)
                    yield chunk
                done = True
            except httpx.HTTPError as exc:
                rec["interrupted"] = f"upstream {type(exc).__name__}"
                raise StreamBroken() from exc
            finally:
                if not done and "interrupted" not in rec:
                    rec["interrupted"] = "client"
                await upstream.aclose()
                slot.release()
                hold.release()
                finish(bytes=sent)

        streaming = True
        return StreamingResponse(relay(), status_code=upstream.status_code, headers=resp_headers)
    finally:
        if slot is not None and not streaming:
            slot.release()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str):
    prov = PREFIXES.get(path.partition("/")[0])
    if prov is not None:
        return await provider_proxy(request, prov)
    device = device_from_request(request)
    if device is None:
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error",
                                        "message": "claude-rotate: unknown device key"}},
            status_code=401)
    if not granted(device, ANTHROPIC):
        return JSONResponse(
            {"type": "error", "error": {"type": "permission_error",
                                        "message": f"claude-rotate: device '{device}' may not use "
                                                   "the Claude accounts"}},
            status_code=403)

    body = await bounded_body(request)
    fwd_headers = forward_headers(request)
    model = request_model(request, body)
    raw_path = upstream_path(request)
    family = model_family(model) if request.method == "POST" and raw_path == "/v1/messages" else None
    url = upstream_url(UPSTREAM, raw_path, upstream_query(request))
    rec = {"device": device, "path": "/" + path, "method": request.method, "model": model}
    upstream, account, retries, _verdict = await send_upstream(
        request.method, url, fwd_headers, body, rec, request, family=family)

    add_claude_audit_fields(rec, upstream, account, retries)
    resp_headers = response_headers(upstream)
    if account:
        resp_headers["x-rotate-account"] = account

    if "text/event-stream" in upstream.headers.get("content-type", ""):
        meter = providers.SseUsage(claude=True)

        async def relay():
            error_body = bytearray() if upstream.status_code == 429 else None
            try:
                async for chunk in upstream.aiter_bytes():
                    if error_body is not None:
                        if len(error_body) + len(chunk) <= MAX_RATE_LIMIT_ERROR_BYTES:
                            error_body.extend(chunk)
                        else:
                            error_body = None
                    meter.feed(chunk)
                    yield chunk
                if error_body is not None:
                    rate_limit_error(rec["rate_limits"][-1], error_body, upstream.headers)
            finally:
                await upstream.aclose()
                rec["usage"] = meter.usage
                audit(rec)

        return StreamingResponse(relay(), status_code=upstream.status_code, headers=resp_headers)

    content = await bounded_response(upstream)
    if upstream.status_code == 429:
        rate_limit_error(rec["rate_limits"][-1], content, upstream.headers)
    try:
        rec["usage"] = summarize_usage(json.loads(content).get("usage"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    audit(rec)
    return Response(content=content, status_code=upstream.status_code, headers=resp_headers)


if __name__ == "__main__":
    uvicorn.run(app, host=CFG.get("bind", "127.0.0.1"), port=CFG.get("port", 8484),
                log_level="warning")
