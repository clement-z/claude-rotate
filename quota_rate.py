"""Read-only quota-rate estimates from gateway audit and quota samples."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import fsum
from statistics import median, quantiles

from pricing import api_cost, price_for, token_count

DAY = 86400
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
CLAUDE_PATHS = ("/v1/messages", "/v1/chat/completions")


@dataclass(frozen=True)
class Reading:
    at: float
    percent: float | None
    reset: float | None


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
    readings = defaultdict(dict)
    traffic = defaultdict(list)
    reporters = defaultdict(set)
    earliest = None
    for record in records:
        at = audit_time(record.get("ts"))
        if at is None or at > now:
            continue
        earliest = at if earliest is None else min(at, earliest)
        if at < start:
            continue
        provider = record.get("provider")
        account = record.get("account")
        if isinstance(provider, str) and provider in providers:
            # The audit names the image route. Other OpenAI inference shares the Codex pool.
            limit = "imagegen_premium" if record.get("route") == "codex-images" else "codex"
            traffic[("openai", provider, limit)].append(request_usage(record, at, prices, provider))
        elif not provider and isinstance(account, str) and account in accounts:
            if record.get("path") not in CLAUDE_PATHS:
                continue
            request = request_usage(record, at, prices)
            traffic[("claude", account)].append(request)
            resets = mapping(record.get("ratelimit"))
            for claim, value in mapping(record.get("util")).items():
                used = number(value)
                if used is None or (claim == "7d_oi" and not request.fable):
                    continue
                key = ("claude", account, claim, None)
                series = readings[key]
                reporters[key].add(request.fable)
                reset = reset_time(resets.get(f"unified-{claim}-reset"))
                # Audit read order is newest first. Keep the last reading in a second.
                if at not in series:
                    series[at] = Reading(at, round(100 * used, 10), reset)
                elif series[at].reset is None and reset is not None:
                    series[at] = Reading(at, series[at].percent, reset)
    for requests in traffic.values():
        requests.sort(key=lambda r: r.at)
    return readings, traffic, reporters, earliest


def add_claude_history(readings, history, accounts, start, now):
    first_audit = {name: min(readings[("claude", name, "7d", None)], default=float("inf")) for name in accounts}
    for sample in history:
        sample = mapping(sample)
        at = number(sample.get("ts"))
        if not in_range(at, start, now):
            continue
        for name, point in mapping(sample.get("accounts")).items():
            if name not in accounts or not isinstance(point, list) or len(point) != 2:
                continue
            used, reset = number(point[0]), reset_time(point[1])
            if used is None:
                continue
            point = Reading(at, round(100 * used, 10) if at < first_audit[name] else None, reset)
            series = readings[("claude", name, "7d", None)]
            if at not in series:
                series[at] = point
            elif series[at].reset is None:
                series[at] = Reading(at, series[at].percent, reset)


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
                    readings[("openai", name, limit, minutes)][at] = Reading(at, used, reset_time(point[2]))


def measurement_intervals(readings, min_step, covered_from):
    """Greedy non-overlapping intervals, each inside one observed window."""
    intervals = []
    anchor = latest = None
    deadline = None
    for point in sorted(readings.values(), key=lambda r: r.at):
        if deadline is not None and deadline <= point.at:
            anchor = None
            deadline = None
        if point.reset is not None:
            deadline = point.reset
        if point.percent is None:
            continue
        if latest is not None and point.percent < latest.percent:
            anchor = None
        latest = Reading(point.at, point.percent, deadline)
        if deadline is not None and deadline <= point.at:
            anchor = None
            continue
        if covered_from is None or latest.at < covered_from:
            continue
        if anchor is None:
            anchor = latest
        rise = round(latest.percent - anchor.percent, 10)
        if rise >= min_step:
            intervals.append((anchor.at, latest.at, rise))
            anchor = latest
    return intervals, latest


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
    intervals, latest = measurement_intervals(points, min_step, covered_from)
    dollar_rates, token_rates, fit_rows = [], [], []
    outside = 0
    times = [request.at for request in requests] if requests is not None else []
    for start, end, rise in intervals if requests is not None else ():
        period = requests[bisect_right(times, start):bisect_right(times, end)]
        if not period:
            outside += 1
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
        "traffic_attributed": requests is not None,
        "outside_intervals": outside if requests is not None else None,
        "outside_share": outside / len(intervals) if intervals and requests is not None else None,
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
            readings[("openai", name, "codex", None)] = {}
    reports = []
    for key, points in sorted(readings.items()):
        kind, name, limit, minutes = key
        if kind == "claude":
            requests = traffic[("claude", name)]
            if limit == "7d_oi":
                requests = [request for request in requests if request.fable]
            elif limit not in ("5h", "7d"):
                requests = [request for request in requests if request.fable in reporters[key]]
        else:
            key = (kind, name, limit)
            requests = traffic[key] if limit in ("codex", "imagegen_premium") else None
        reports.append({"kind": kind, "name": name, "limit": limit, "minutes": minutes,
                        **series_report(points, requests, min_step, earliest, now,
                                        general_week=kind == "claude" and limit == "7d")})
    return reports, earliest
