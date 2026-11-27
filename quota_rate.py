"""Read-only quota-rate estimates from gateway audit and quota samples."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from math import fsum
from statistics import median, quantiles

from pricing import api_cost, price_for, token_count

DAY = 86400
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
CLAUDE_PATHS = ("/v1/messages", "/v1/chat/completions")
OPENAI_PATHS = ("/v1/responses", "/v1/chat/completions")
RESET_TOLERANCE = 60
DROP_NOISE = 1


@dataclass(frozen=True)
class Reading:
    at: float
    percent: float
    reset: float | None
    sampled: bool = False
    completion_time: bool = False


@dataclass(frozen=True)
class RequestUsage:
    at: float
    tokens: tuple | None
    cost: float | None
    fable: bool


def mapping(value):
    return value if isinstance(value, dict) else {}


def number(value):
    return value if type(value) in (int, float) and 0 <= value < 1e15 else None


def reset_time(value):
    try:
        return number(float(value)) or None
    except (TypeError, ValueError, OverflowError):
        return None


def audit_time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, OSError, OverflowError):
        return None


def in_range(at, start, now):
    return at is not None and start <= at <= now


def request_usage(record, at, prices, provider=None):
    model = record.get("model")
    fable = isinstance(model, str) and "fable" in model.lower()
    usage = mapping(record.get("usage"))
    tokens = None
    cost = None
    if any(type(usage.get(k)) is int and number(usage[k]) is not None for k in TOKEN_FIELDS):
        tokens = tuple(token_count(usage.get(k)) for k in TOKEN_FIELDS)
        if not provider or price_for(model, prices, None) is not None:
            priced = usage | dict(zip(TOKEN_FIELDS, tokens))
            cost = api_cost(model, priced, provider, prices=prices)
    return RequestUsage(at, tokens, cost, fable)


def collect_audit(records, accounts, providers, start, now, prices):
    readings = defaultdict(list)
    traffic = defaultdict(list)
    reporters = defaultdict(set)
    earliest = None
    model_paths = {name: tuple(prefix + path for path in OPENAI_PATHS) for name, prefix in providers.items()}
    for record in records:
        at = audit_time(record.get("ts"))
        if at is None or at > now:
            continue
        earliest = at if earliest is None else min(at, earliest)
        if at < start:
            continue
        provider = record.get("provider")
        account = record.get("account")
        if record.get("method", "POST") != "POST":
            continue
        if isinstance(provider, str) and provider in providers:
            if record.get("path") in model_paths[provider]:
                traffic[("openai", provider, "codex")].append(request_usage(record, at, prices, provider))
        elif not provider and isinstance(account, str) and account in accounts:
            if record.get("path") not in CLAUDE_PATHS:
                continue
            seen = number(record.get("util_at"))
            # Completion timestamps have whole-second resolution.
            legacy = seen is None or seen > now or seen >= at + 1
            observed = at if legacy else seen
            earliest = min(earliest, observed)
            if observed < start:
                continue
            request = request_usage(record, observed, prices)
            traffic[("claude", account)].append(request)
            resets = mapping(record.get("ratelimit"))
            for claim, value in mapping(record.get("util")).items():
                used = number(value)
                if used is None or (claim == "7d_oi" and not request.fable):
                    continue
                key = ("claude", account, claim, None)
                reporters[key].add(request.fable)
                reset = reset_time(resets.get(f"unified-{claim}-reset"))
                readings[key].append(Reading(observed, round(100 * used, 10), reset, completion_time=legacy))
    # Ties use the audit file's write order. Do not discard any readings.
    for series in readings.values():
        series.reverse()
    for requests in traffic.values():
        requests.sort(key=lambda r: r.at)
    return readings, traffic, reporters, earliest


def add_claude_history(readings, history, accounts, start, now):
    for name in accounts:
        readings.setdefault(("claude", name, "7d", None), [])
    for sample in history:
        sample = mapping(sample)
        at = number(sample.get("ts"))
        if not in_range(at, start, now):
            continue
        for name, point in mapping(sample.get("accounts")).items():
            if name not in accounts or not isinstance(point, list) or len(point) != 2:
                continue
            used, reset = number(point[0]), reset_time(point[1])
            if used is None or (reset is not None and reset <= at):
                continue
            readings[("claude", name, "7d", None)].append(Reading(at, round(100 * used, 10), reset, sampled=True))


def add_provider_history(readings, history, providers, start, now):
    for name, series in mapping(history.get("providers")).items():
        if name not in providers or not isinstance(series, list):
            continue
        for spec in series:
            spec = mapping(spec)
            limit, minutes = spec.get("limit"), spec.get("minutes")
            if not isinstance(limit, str) or not limit or type(minutes) is not int or minutes <= 0:
                continue
            points = spec.get("points")
            if not isinstance(points, list):
                continue
            for point in points:
                if not isinstance(point, list) or len(point) != 4:
                    continue
                at, used = number(point[0]), number(point[1])
                if in_range(at, start, now) and used is not None and used <= 100:
                    readings[("openai", name, limit, minutes)].append(
                        Reading(at, used, reset_time(point[2]), sampled=point[3] == "usage"))


def changed_reset(estimate, *known):
    return estimate is not None and any(
        reset is not None and abs(estimate - reset) > RESET_TOLERANCE for reset in known)


def accepted_readings(readings):
    points = sorted(readings, key=lambda r: r.at)
    latest = deadline = cycle = None
    for i, point in enumerate(points):
        if point.reset is not None and point.reset <= point.at < (deadline or 0):
            continue  # a stale sample from an expired window
        restart = ((deadline is not None and deadline <= point.at)
                   or changed_reset(point.reset, cycle, deadline))
        if restart:
            deadline = cycle = None
        elif latest is not None and point.percent < latest.percent:
            following = points[i + 1] if i + 1 < len(points) else None
            reset = point.reset or deadline
            confirmed = (following is not None and following.at > point.at and following.percent <= point.percent
                         and (reset is None or following.at < reset)
                         and not changed_reset(following.reset, cycle, reset))
            if round(latest.percent - point.percent, 10) <= DROP_NOISE and not confirmed:
                continue
            restart = True
        deadline = point.reset or deadline
        cycle = cycle or deadline
        latest = replace(point, reset=deadline)
        yield latest, restart


def measurement_intervals(readings, min_step, covered_from):
    """Build non-overlapping intervals from all readings and from samples alone."""
    intervals, sampled_intervals = [], []
    anchor = sampled_anchor = latest = None
    for latest, restart in accepted_readings(readings):
        if restart:
            anchor = sampled_anchor = None
        if covered_from is None or latest.at < covered_from or (
                latest.reset is not None and latest.reset <= latest.at):
            continue
        if anchor is None:
            anchor = latest
        rise = round(latest.percent - anchor.percent, 10)
        if rise >= min_step and latest.at > anchor.at:
            intervals.append((anchor.at, latest.at, rise))
            anchor = latest
        if latest.sampled:
            if sampled_anchor is None:
                sampled_anchor = latest
            if round(latest.percent - sampled_anchor.percent, 10) >= min_step and latest.at > sampled_anchor.at:
                sampled_intervals.append((sampled_anchor.at, latest.at))
                sampled_anchor = latest
    return intervals, sampled_intervals, latest


def fit_fable(rows):
    """Least squares through the origin with one coefficient per dollar stream."""
    ff = fsum(f * f for f, _, _ in rows)
    oo = fsum(o * o for _, o, _ in rows)
    fo = fsum(f * o for f, o, _ in rows)
    determinant = ff * oo - fo * fo
    report = {"n": len(rows), "a": None, "b": None}
    if len(rows) > 1 and determinant > 1e-12 * ff * oo:
        fy = fsum(f * y for f, _, y in rows)
        oy = fsum(o * y for _, o, y in rows)
        report.update(a=(fy * oo - oy * fo) / determinant, b=(oy * ff - fy * fo) / determinant)
    return report


def series_report(points, requests, min_step, covered_from, now, *, general_week=False):
    intervals, sampled_intervals, latest = measurement_intervals(points, min_step, covered_from)
    dollar_rates, token_rates, fit_rows = [], [], []
    times = [request.at for request in requests] if requests is not None else []
    sample_count = len(sampled_intervals)
    outside = None
    if requests is not None and sample_count:
        outside = sum(bisect_right(times, start) == bisect_right(times, end) for start, end in sampled_intervals)
    for start, end, rise in intervals if requests is not None else ():
        period = requests[bisect_right(times, start):bisect_right(times, end)]
        if not period:
            continue
        if all(request.tokens is not None for request in period):
            token_rates.append(tuple(sum(t) / rise for t in zip(*(request.tokens for request in period))))
        if all(request.cost is not None for request in period):
            fable = fsum(request.cost for request in period if request.fable)
            other = fsum(request.cost for request in period if not request.fable)
            dollar_rates.append((fable + other) / rise)
            fit_rows.append((fable, other, rise))
    dollars = None
    if dollar_rates:
        q1, _, q3 = quantiles(dollar_rates, n=4, method="inclusive") if len(dollar_rates) > 1 else (None, None, None)
        dollars = {"median": median(dollar_rates), "q1": q1, "q3": q3}
    tokens = dict(zip(TOKEN_FIELDS, map(median, zip(*token_rates)))) if token_rates else None
    remaining = None
    if latest and (latest.reset is None or latest.reset > now):
        left = max(0, 100 - latest.percent)
        usd = left * dollars["median"] if dollars else None
        hours = None
        if usd is not None and requests is not None and covered_from is not None and covered_from <= now - DAY:
            day = requests[bisect_right(times, now - DAY):]
            if all(request.cost is not None for request in day):
                pace = fsum(request.cost for request in day)
                if pace > 0:
                    hours = 24 * usd / pace
        remaining = {"percent": left, "usd": usd,
                     "tokens": {k: left * v for k, v in tokens.items()} if tokens else None, "hours": hours}
    result = {
        "latest": {"at": latest.at, "used_percent": latest.percent, "reset": latest.reset} if latest else None,
        "intervals": len(intervals), "cost_intervals": len(dollar_rates), "token_intervals": len(token_rates),
        "completion_time_readings": sum(point.completion_time for point in points),
        "traffic_attributed": requests is not None,
        "sampled_intervals": sample_count, "outside_intervals": outside,
        "outside_share": outside / sample_count if outside is not None else None,
        "usd_per_percent": dollars, "tokens_per_percent": tokens, "remaining": remaining,
    }
    if general_week:
        result["fable_fit"] = fit_fable(fit_rows)
    return result


def analyse(records, history, provider_history, accounts, providers, start, now, min_step, prices):
    readings, traffic, reporters, earliest = collect_audit(records, accounts, providers, start, now, prices)
    add_claude_history(readings, history, accounts, start, now)
    add_provider_history(readings, provider_history, providers, start, now)
    for name in providers:
        if not any(kind == "openai" and owner == name for kind, owner, _, _ in readings):
            readings[("openai", name, "codex", None)] = []
    reports = []
    for key, points in sorted(readings.items()):
        kind, name, limit, minutes = key
        note = None
        if kind == "claude":
            requests = traffic[("claude", name)]
            if limit == "7d_oi":
                requests = [request for request in requests if request.fable]
            elif limit not in ("5h", "7d"):
                requests = [request for request in requests if request.fable in reporters[key]]
        elif limit == "codex":
            requests = traffic[(kind, name, limit)]
            note = "Attributing Sign in with ChatGPT inference to the Codex limit is unconfirmed"
        else:
            requests = None
            if limit == "imagegen_premium":
                note = "Image cost and tokens are unavailable because the audit records bytes only"
        report = series_report(points, requests, min_step, earliest, now,
                               general_week=kind == "claude" and limit == "7d")
        reports.append({"kind": kind, "name": name, "limit": limit, "minutes": minutes, "traffic_note": note, **report})
    return reports, earliest
