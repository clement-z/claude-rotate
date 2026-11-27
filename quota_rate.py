"""Read-only quota-rate estimates from gateway audit and quota samples."""

import errno
import json
import os
import stat
from array import array
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from math import fsum
from statistics import median, quantiles

from pricing import api_cost, price_for, token_count

DAY = 86400
AUDIT_TAIL_BYTES = 32 * 1024 * 1024
AUDIT_TAIL_BACKUPS = 32
AUDIT_TAIL_LINE_BYTES = 1024 * 1024
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
CLAUDE_PATHS = ("/v1/messages", "/v1/chat/completions")
OPENAI_ENDPOINTS = ("v1/responses", "v1/chat/completions")
RESET_TOLERANCE = 60
DROP_NOISE = 1
PROJECTION_STALE_S = 4 * 3600
MIN_FABLE_SHARE_SPREAD = .10
MAX_FIT_DOLLARS_PER_POINT = 1_000_000


@dataclass(frozen=True, slots=True)
class Reading:
    at: float
    percent: float
    reset: float | None
    sampled: bool = False
    completion_time: bool = False


@dataclass(frozen=True, slots=True)
class RequestUsage:
    at: float
    tokens: tuple | None
    cost: float | None
    fable: bool


class ReadingSeries:
    """Store readings in packed columns and unpack only the series being reported."""

    def __init__(self):
        self.times = array("d")
        self.values = array("d")
        self.resets = None
        self.flags = bytearray()

    def __len__(self):
        return len(self.times)

    def __getitem__(self, i):
        flags = self.flags[i]
        reset = self.resets[i] if self.resets is not None else None
        return Reading(self.times[i], self.values[i], reset or None, bool(flags & 1), bool(flags & 2))

    def __setitem__(self, i, point):
        self.times[i], self.values[i] = point.at, point.percent
        if point.reset is not None and self.resets is None:
            self.resets = array("d", [0]) * len(self.times)
        if self.resets is not None:
            self.resets[i] = point.reset or 0
        self.flags[i] = point.sampled | (point.completion_time << 1)

    def append(self, point):
        if point.reset is not None and self.resets is None:
            self.resets = array("d", [0]) * len(self.times)
        if self.resets is not None:
            self.resets.append(point.reset or 0)
        self.times.append(point.at)
        self.values.append(point.percent)
        self.flags.append(point.sampled | (point.completion_time << 1))

    def reverse(self):
        for column in (self.times, self.values, self.flags):
            column.reverse()
        if self.resets is not None:
            self.resets.reverse()


class ScanStopped(Exception):
    pass


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise ScanStopped


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
    readings = defaultdict(ReadingSeries)
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
        if record.get("method", "POST") != "POST":
            continue
        if isinstance(provider, str) and provider in providers:
            path = record.get("path")
            # The audit provider identifies the account even after a URL prefix rename.
            if isinstance(path, str) and path.removeprefix("/").partition("/")[2] in OPENAI_ENDPOINTS:
                traffic[("openai", provider, "codex")].append(request_usage(record, at, prices, provider))
        elif not provider and isinstance(account, str) and account in accounts:
            if record.get("path") not in CLAUDE_PATHS:
                continue
            seen = number(record.get("util_at"))
            # Completion timestamps have whole-second resolution.
            legacy = seen is None or seen > now or seen >= at + 1
            observed = at if legacy else seen
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
    samples = defaultdict(list)
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
            samples[name].append(Reading(at, round(100 * used, 10), reset, sampled=True))
    for name in accounts:
        series = readings[("claude", name, "7d", None)]
        if name not in samples:
            continue
        # Rounding can move a sample ahead of the header it copied.
        sample_keys = {(p.at, p.percent) for p in samples[name]}
        rounded_headers = defaultdict(list)
        for i, point in enumerate(series):
            rounded = round(point.at)
            key = (rounded, point.percent)
            if not point.completion_time and rounded < point.at and key in sample_keys:
                rounded_headers[key].append(i)
        for sample in samples[name]:
            candidates = rounded_headers.get((sample.at, sample.percent), ())
            match = max((i for i in candidates if not changed_reset(series[i].reset, sample.reset)),
                        key=lambda i: series[i].at, default=None)
            if match is None:
                series.append(sample)
            else:
                point = series[match]
                series[match] = replace(point, reset=point.reset or sample.reset, sampled=True)


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


def measurement_intervals(accepted, min_step, covered_from):
    """Build non-overlapping intervals from all readings and from samples alone."""
    intervals, sampled_intervals = [], []
    anchor = sampled_anchor = latest = None
    for latest, restart in accepted:
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


def recent_quota_pace(accepted, now):
    """Points per day over time observed inside segments."""
    previous = None
    rise = elapsed = 0
    for point, restart in accepted:
        if restart:
            previous = None
        if point.at < now - DAY or (point.reset is not None and point.reset <= point.at):
            continue
        if previous is not None:
            rise += point.percent - previous.percent
            elapsed += point.at - previous.at
        previous = point
    return rise * DAY / elapsed if elapsed else None


def fit_fable(rows):
    """Least squares through the origin with one coefficient per dollar stream."""
    ff = fsum(f * f for f, _, _ in rows)
    oo = fsum(o * o for _, o, _ in rows)
    fo = fsum(f * o for f, o, _ in rows)
    determinant = ff * oo - fo * fo
    report = {"n": len(rows), "a": None, "b": None, "fable_usd_per_percent": None, "other_usd_per_percent": None}
    if len(rows) > 1 and determinant > 1e-12 * ff * oo:
        fy = fsum(f * y for f, _, y in rows)
        oy = fsum(o * y for _, o, y in rows)
        a, b = (fy * oo - oy * fo) / determinant, (oy * ff - fy * fo) / determinant
        report.update(a=a, b=b)
        shares = [f / (f + o) for f, o, _ in rows if f + o > 0]
        minimum = max(1e-6 * max(a, b), 1 / MAX_FIT_DOLLARS_PER_POINT)
        if min(a, b) >= minimum and max(shares) - min(shares) >= MIN_FABLE_SHARE_SPREAD:
            report.update(fable_usd_per_percent=1 / a, other_usd_per_percent=1 / b)
    return report


def series_report(points, requests, min_step, covered_from, now, *, general_week=False):
    accepted = list(accepted_readings(points))
    intervals, sampled_intervals, latest = measurement_intervals(accepted, min_step, covered_from)
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
            if general_week:
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
        pace = recent_quota_pace(accepted, now)
        stale = now - latest.at > PROJECTION_STALE_S
        exhausts_at = latest.at if left == 0 else latest.at + DAY * left / pace if pace else None
        hours = max(0, (exhausts_at - now) / 3600) if exhausts_at is not None and not stale else None
        reset_hours = (latest.reset - now) / 3600 if latest.reset is not None else None
        lasts_until_reset = (not stale and latest.reset is not None and left > 0
                             and (pace == 0 or (exhausts_at is not None and exhausts_at > latest.reset)))
        remaining = {"percent": left, "usd": usd,
                     "tokens": {k: left * v for k, v in tokens.items()} if tokens else None,
                     "hours": hours, "reset_in_hours": reset_hours, "lasts_until_reset": lasts_until_reset,
                     "stale": stale, "exhausts_at": exhausts_at,
                     "pace_points_per_day": pace}
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


def analyse(records, history, provider_history, accounts, providers, start, now, min_step, prices, stop=None):
    readings, traffic, reporters, earliest = collect_audit(records, accounts, providers, start, now, prices)
    add_claude_history(readings, history, accounts, start, now)
    add_provider_history(readings, provider_history, providers, start, now)
    for name in providers:
        if not any(kind == "openai" and owner == name for kind, owner, _, _ in readings):
            readings[("openai", name, "codex", None)] = []
    reports = []
    for key in sorted(readings):
        check_stop(stop)
        points = readings.pop(key)
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


def audit_tail(stream, end, size):
    """Yield complete lines in reverse order, using None for an oversized line."""
    start = end - size
    pending = b""
    while end > start:
        offset = max(start, end - 64 * 1024)
        stream.seek(offset)
        parts = stream.read(end - offset).split(b"\n")
        end = offset
        if len(parts) > 1:
            if pending is not None and len(parts[-1]) + len(pending) <= AUDIT_TAIL_LINE_BYTES:
                yield parts[-1] + pending
            else:
                yield None
            yield from reversed(parts[1:-1])
            pending = parts[0]
        elif pending is not None:
            pending = parts[0] + pending if len(parts[0]) + len(pending) <= AUDIT_TAIL_LINE_BYTES else None
    if pending is None or (not start and pending):
        yield pending


def audit_log_lines(audit_path, scan, stop=None):
    scan.update(limited=False, skipped_lines=0)
    try:
        directory = os.open(audit_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    remaining = AUDIT_TAIL_BYTES
    seen_files = set()
    try:
        for n in range(AUDIT_TAIL_BACKUPS + 1):
            check_stop(stop)
            name = f"{audit_path.name}.{n}" if n else audit_path.name
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ELOOP):
                    continue
                raise
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                inode = info.st_dev, info.st_ino
                if not stat.S_ISREG(info.st_mode) or inode in seen_files:
                    continue
                seen_files.add(inode)
                size = min(info.st_size, remaining)
                for line in audit_tail(stream, info.st_size, size):
                    check_stop(stop)
                    if line is None:
                        scan["skipped_lines"] += 1
                    elif line:
                        yield line
                remaining -= size
                if not remaining:
                    scan["limited"] = True
                    break
    finally:
        os.close(directory)


def quota_history_file(path, default, warnings):
    bound = 16 * 1024 * 1024
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > bound:
                raise ValueError("invalid history file size or type")
            data = stream.read(bound + 1)
        if len(data) > bound:
            raise ValueError("history file too large")
        result = json.loads(data)
        if not isinstance(result, type(default)):
            raise ValueError("unexpected history file format")
        return result
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError):
        warnings.append(f"{path.name} could not be read")
    return default


def quota_audit_records(audit_path, scan, stop=None):
    for line in audit_log_lines(audit_path, scan, stop):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            scan["skipped_lines"] += 1
            continue
        if isinstance(record, dict):
            yield record


def quota_limit_label(row):
    limit = {"7d_oi": "Fable 7d", "imagegen_premium": "images"}.get(row["limit"], row["limit"])
    minutes = row["minutes"]
    if minutes == 10080:
        return f"{limit} 7d"
    if minutes:
        window = f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"
        return f"{limit} {window}"
    return limit


def quota_rate_summary(series):
    summary = []
    for row in series:
        latest, remaining, tokens = row["latest"], row["remaining"], row["tokens_per_percent"]
        summary.append({
            "kind": row["kind"], "account": row["name"], "limit": quota_limit_label(row),
            "used_percent": latest["used_percent"] if latest else None,
            "usd_per_percent": row["usd_per_percent"],
            "output_tokens_per_percent": tokens["output_tokens"] if tokens else None,
            "usd_left": remaining["usd"] if remaining else None,
            "time_left": {k: remaining[k] for k in ("hours", "reset_in_hours", "lasts_until_reset",
                                                    "pace_points_per_day", "stale", "exhausts_at")}
            if remaining else None,
            "samples": row["cost_intervals"],
        })
    return summary


def read_report(audit_path, history_path, provider_history_path, accounts, providers, prices, now,
                *, days=14, min_step=5, stop=None):
    scan, claude_errors, openai_errors = {}, [], []
    check_stop(stop)
    history = quota_history_file(history_path, [], claude_errors)
    check_stop(stop)
    provider_history = quota_history_file(provider_history_path, {}, openai_errors)
    if provider_history and provider_history.get("version") != 1:
        openai_errors.append("provider_quota_history.json has an unsupported version")
        provider_history = {}
    series, earliest = analyse(quota_audit_records(audit_path, scan, stop), history, provider_history,
                               accounts, providers, now - days * DAY, now, min_step, prices, stop)
    source_errors = {source: errors for source, errors in (("claude", claude_errors), ("openai", openai_errors))
                     if errors}
    return {"days": days, "min_step": min_step, "audit": {"from": earliest, **scan},
            "warnings": claude_errors + openai_errors, "source_errors": source_errors,
            "summary": quota_rate_summary(series), "series": series}
