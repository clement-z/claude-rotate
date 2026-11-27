"""Offline quota-rate CLI with synthetic history and rotated audit logs."""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3600
sys.path.insert(0, str(ROOT))
import quota_rate


def check(label, condition):
    assert condition, label
    print(f"ok   {label}")


with tempfile.TemporaryDirectory(prefix="quota-rate-test-") as scratch:
    root = Path(scratch)
    # Exercise the installed layout with -I and no dependency on the runtime package.
    release = root / "release"
    (release / "deploy").mkdir(parents=True)
    for path in ("deploy/claude_rotate_admin.py", "pricing.py", "quota_rate.py"):
        shutil.copyfile(ROOT / path, release / path)
    config = root / "etc/claude-rotate/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({
        "accounts": [{"name": "a", "token_file": "a.token", "billing": [
            {"from": "2000-01-01", "plan": "pro"},
            {"from": "2020-01-01", "plan": "team", "renews": True},
            {"from": "2099-01-01", "plan": "ultra"},
        ]}, {"name": "b", "token_file": "b.token"}],
        "plans": {"team": {"price": 120, "weight": 1}, "chatgpt-prolite": {"price": 50}},
        "devices": {}, "providers": {"work": {"kind": "openai-siwc", "prefix": "/openai",
                                              "billing": [{"from": "2020-01-01", "plan": "chatgpt-prolite"}]},
                                     "lan": {"kind": "static-bearer"}},
    }))
    state = root / "var/lib/claude-rotate"
    state.mkdir(parents=True)
    audit = root / "var/log/claude-rotate/audit.jsonl"
    audit.parent.mkdir(parents=True)
    now = int(time.time())

    def at(hours):
        return now + hours * HOUR

    def audit_row(hours, *, account="a", model="claude-opus-5", output=200_000, util=None, **extra):
        row = {"ts": datetime.fromtimestamp(at(hours), timezone.utc).isoformat(), "account": account,
               "model": model, "path": "/v1/messages", "method": "POST",
               "status": 200, "usage": {"output_tokens": output}}
        if util is not None:
            row["util"] = util
        return row | extra

    def save_rows(path, rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def run(*args):
        return subprocess.run([sys.executable, "-I", str(release / "deploy/claude_rotate_admin.py"), "quota-rate", *args],
                              env=dict(os.environ, CLAUDE_ROTATE_PREFIX=scratch), cwd=root,
                              capture_output=True, text=True, timeout=10)

    fable = {"model": "claude-fable-5-2", "output": 125_000}
    save_rows(audit.with_suffix(".jsonl.1"), [
        audit_row(-30, output=0), audit_row(-28),
        audit_row(-23, **fable, util={"7d": .30, "7d_oi": .10, "learned": .30},
                  ratelimit={"unified-7d-reset": str(at(-10)), "unified-7d_oi-reset": str(at(10))}),
    ])
    save_rows(audit, [
        audit_row(-22, account=None, provider="work", path="/openai/v1/models", method="GET", usage=None),
        audit_row(-21, account=None, provider="work", model="gpt-6.2-sol", path="/openai/v1/responses",
                  usage={"input_tokens": 125_000, "output_tokens": 100_000, "cache_read_input_tokens": 20_000,
                         "cache_creation_input_tokens": 10_000}),
        audit_row(-20, util={"7d": .35}),
        audit_row(-17, **fable, util={"7d": .45, "7d_oi": .20, "learned": .40}),
        audit_row(-16, account=None, provider="work", path="/openai/v1/models", method="GET", usage=None),
        audit_row(-15, account=None, provider="work", path="/openai/v1/alpha/search", route="codex-search", usage=None),
        audit_row(-14, model=fable["model"], output=1_000_000, util={"7d": .10, "7d_oi": .05, "learned": .10}),
        audit_row(-13, account=None, provider="work", path="/openai/v1/images/generations", route="codex-images",
                  usage=None, bytes=1000),
        audit_row(-11, **fable, util={"7d": .20, "7d_oi": .15, "learned": .20}),
        audit_row(-8, util={"7d": .90}, ratelimit={"unified-7d-reset": str(at(158))}),
        audit_row(-1, util={"7d": .95}),
        audit_row(-4, account="b", util={"7d": .03}),
    ])
    (state / "usage_history.json").write_text(json.dumps([
        {"ts": at(h), "accounts": {"a": [u, at(-10)]}}
        for h, u in ((-30, .10), (-28, .15), (-26, .20), (-22, .30))
    ]))
    (state / "provider_quota_history.json").write_text(json.dumps({"version": 1, "providers": {"work": [
        {"limit": "codex", "minutes": 10080,
         "points": [[at(h), u, at(110), "usage"] for h, u in ((-25, 10), (-20, 15), (-12, 20))]},
        {"limit": "imagegen_premium", "minutes": 1440,
         "points": [[at(h), u, at(1), "images"] for h, u in ((-15, 10), (-12, 20))]},
        {"limit": "another", "minutes": 300, "points": [[at(-1), 20, at(1), "inference"]]},
    ]}}))
    before = {path: path.read_bytes() for path in [config, audit, audit.with_suffix(".jsonl.1"), *state.iterdir()]}

    r = run("--json")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    rows = {(row["name"], row["limit"]): row for row in report["series"]}
    general = rows["a", "7d"]
    check("default lookback and rotated audit recover weekly samples before util",
          report["days"] == 14 and report["min_step"] == 5 and report["audit"]["from"] == at(-30)
          and general["intervals"] == 7 and general["cost_intervals"] == 6)
    check("mid-window drop and scheduled reset split intervals, even when usage rises across the reset",
          general["usd_per_percent"] == {"median": .75, "q1": .5, "q3": 1}
          and general["latest"] == {"at": at(-1), "used_percent": 95, "reset": at(158)})
    check("outside rise uses independently sampled intervals",
          general["sampled_intervals"] == 3 and general["outside_intervals"] == 1
          and math.isclose(general["outside_share"], 1 / 3))
    check("general weekly fit distinguishes Fable and other costs",
          all(general["fable_fit"][key] == value for key, value in {
              "n": 6, "a": 2.0, "b": 1.0, "fable_usd_per_percent": .5, "other_usd_per_percent": 1.0,
              "relative_quota_per_dollar": 2,
          }.items()))
    week_price = 120 * 7 / (365.25 / 12)
    check("weekly value uses the effective custom monthly price for the median, IQR and fit",
          math.isclose(general["subscription_window_usd"], week_price)
          and all(math.isclose(general["value_multiple"][key], cost * 100 / week_price)
                  for key, cost in general["usd_per_percent"].items())
          and math.isclose(general["fable_fit"]["fable_value_multiple"], 50 / week_price)
          and math.isclose(general["fable_fit"]["other_value_multiple"], 100 / week_price))
    day_price = quota_rate.subscription_window_price(120, 1440)
    check("a daily window uses one day of the monthly price and a zero price has no multiple",
          math.isclose(day_price, 120 / (365.25 / 12))
          and math.isclose(quota_rate.subscription_value(.75, day_price), 75 / (120 / (365.25 / 12)))
          and quota_rate.subscription_window_price(0, 1440) is None)
    check("time left adds each segment's rise inside the last 24 h and excludes reset jumps",
          general["remaining"]["percent"] == 5 and general["remaining"]["usd"] == 3.75
          and general["remaining"]["pace_points_per_day"] == 45
          and math.isclose(general["remaining"]["hours"], 5 / 3, abs_tol=.01))
    fable_week = rows["a", "7d_oi"]
    check("Fable weekly and an additional Fable claim include only their reporting family",
          fable_week["intervals"] == 2 and fable_week["cost_intervals"] == 2
          and fable_week["usd_per_percent"]["median"] == .5
          and fable_week["tokens_per_percent"]["output_tokens"] == 12_500
          and fable_week["remaining"]["usd"] == 42.5
          and math.isclose(fable_week["value_multiple"]["median"], 50 / week_price)
          and rows["a", "learned"]["usd_per_percent"]["median"] == .5)
    check("Fable-only history has no independent estimate of outside use",
          fable_week["outside_share"] is None and fable_week["sampled_intervals"] == 0)
    codex = rows["work", "codex"]
    check("OpenAI weekly uses provider readings and normalized audit token types",
          codex["kind"] == "openai" and codex["minutes"] == 10080 and codex["outside_share"] == .5
          and codex["usd_per_percent"]["q1"] is None and math.isclose(codex["usd_per_percent"]["median"], .2554)
          and codex["tokens_per_percent"] == {"input_tokens": 25_000, "output_tokens": 20_000,
                                            "cache_read_input_tokens": 4_000, "cache_creation_input_tokens": 2_000}
          and not rows["work", "another"]["traffic_attributed"])
    check("OpenAI metadata and search do not count as Codex model traffic",
          codex["sampled_intervals"] == 2 and codex["outside_intervals"] == 1 and codex["cost_intervals"] == 1)
    images = rows["work", "imagegen_premium"]
    check("image byte counts cannot estimate dollars or tokens",
          images["intervals"] == 1 and not images["traffic_attributed"] and images["usd_per_percent"] is None
          and images["tokens_per_percent"] is None and images["remaining"]["usd"] is None)
    check("OpenAI billing prices the Codex week and the image day",
          math.isclose(codex["value_multiple"]["median"], .2554 * 100 / (50 * 7 / (365.25 / 12)))
          and math.isclose(images["subscription_window_usd"], 50 / (365.25 / 12))
          and images["value_multiple"] is None)
    priced_image = dict(images, usd_per_percent={"median": .5, "q1": None, "q3": None})
    quota_rate.add_subscription_values([priced_image], {("openai", "work"): 50})
    check("the value conversion uses the 24-hour window when given an image rate",
          math.isclose(priced_image["value_multiple"]["median"], 365.25 / 12))
    check("sparse account shows its latest utilization and plain insufficient estimates",
          rows["b", "7d"]["usd_per_percent"] is None and rows["b", "7d"]["remaining"]["hours"] is None
          and rows["b", "7d"]["fable_fit"]["n"] == 0 and rows["b", "7d"]["fable_fit"]["a"] is None)
    summary = {(row["account"], row["limit"]): row for row in report["summary"]}
    check("JSON has one summary row per series with labels, medians, output tokens and priced intervals",
          len(summary) == len(rows) and {("a", "Fable 7d"), ("work", "codex 7d"), ("work", "images 24h")} <= summary.keys()
          and summary["a", "7d"] == {
              "kind": "claude", "account": "a", "limit": "7d", "used_percent": 95,
              "usd_per_percent": {"median": .75, "q1": .5, "q3": 1},
              "value_multiple": general["value_multiple"], "subscription_window_usd": general["subscription_window_usd"],
              "output_tokens_per_percent": general["tokens_per_percent"]["output_tokens"], "usd_left": 3.75,
              "time_left": {k: general["remaining"][k] for k in
                            ("hours", "reset_in_hours", "lasts_until_reset", "pace_points_per_day", "stale", "exhausts_at")},
              "samples": 6,
              "legacy_history_samples": 4,
          })
    unpriced_codex = dict(codex)
    quota_rate.add_subscription_values([unpriced_codex], {("claude", "work"): 50})
    check("unpriced accounts and providers do not borrow each other's billing",
          summary["b", "7d"]["value_multiple"] is None and summary["b", "7d"]["subscription_window_usd"] is None
          and unpriced_codex["usd_per_percent"] is not None and unpriced_codex["value_multiple"] is None)

    r = run()
    summary_output = r.stdout
    table, sep, details = summary_output.partition("\nclaude a / 7d")
    check("the summary shows value ranges and a price hint while details retain per-point costs",
          r.returncode == 0 and sep and "Account" in table and "Samples" in table
          and all(label in table for label in ("Value ($/$)", "2.7x (1.8–3.6x)", "Fable 7d", "codex 7d",
                                               "images 24h", "1.67 h"))
          and table.count("Add billing to an account or OpenAI provider in config.json") == 1
          and "Fable-only use of the Fable allowance" in table
          and "Cost per 1%: $0.75 median" in details and "Tokens per 1%:" in details
          and "Fable 2.0x the quota per $ of other models (Fable 1.8x, other 3.6x API $ per subscription $)" in details
          and next(line for line in table.splitlines() if "codex 7d" in line).split()[4] == "2.2x")

    r = run("--days", "1", "--min-step", "25")
    check("human output uses flags and marks insufficient data",
          r.returncode == 0 and "over 1 days (minimum rise 25 points)" in r.stdout
          and "Cost per 1%: not enough data" in r.stdout and "work / codex, 10080 min" in r.stdout)
    check("human output identifies legacy timestamps and OpenAI attribution limits",
          "older audit readings use request completion time" in r.stdout
          and "older weekly samples lack upstream timestamps" in r.stdout
          and "Attributing Sign in with ChatGPT inference to the Codex limit is unconfirmed" in r.stdout
          and "Image cost and tokens are unavailable because the audit records bytes only" in r.stdout)
    r = run("--min-step", "nan")
    check("invalid thresholds are rejected", r.returncode == 1 and "--min-step" in r.stderr)
    check("the installed command leaves logs, state, configuration and release files alone",
          all(path.read_bytes() == data for path, data in before.items()) and not list(release.rglob("__pycache__")))

    def rows_for(*args):
        result = run("--json", *args)
        assert result.returncode == 0, result.stderr
        return {(row["name"], row["limit"]): row for row in json.loads(result.stdout)["series"]}

    audit.with_suffix(".jsonl.1").unlink()
    (state / "usage_history.json").unlink()
    save_rows(audit, [
        audit_row(-12, account="b", util={"7d": .10}),
        audit_row(-10, account="b", util={"7d": .20}),
        audit_row(-10, account="b", util={"7d": .05}),
        audit_row(-8, account="b", util={"7d": .15}),
        audit_row(-7, util_at=at(-9) + .25, util={"7d": .20}),
        audit_row(-6, util_at=at(-9) + .1, util={"7d": .10}),
        audit_row(-5, util_at=at(-5) + .98, util={"7d": .20}),
    ])
    rows = rows_for()
    check("overlapping streams contribute once in header order even within one second",
          rows["a", "7d"]["intervals"] == 1 and rows["a", "7d"]["cost_intervals"] == 1
          and rows["a", "7d"]["completion_time_readings"] == 0
          and rows["a", "7d"]["usd_per_percent"]["median"] == .5)
    check("same-second legacy readings preserve a real drop and flag completion-time ordering",
          rows["b", "7d"]["intervals"] == 2 and rows["b", "7d"]["cost_intervals"] == 2
          and rows["b", "7d"]["completion_time_readings"] == 4)

    save_rows(audit, [audit_row(-12 + i, util={"7d": used})
                      for i, used in enumerate((.20, .19, .20, .25, .24, .24, .29))])
    noise = rows_for()["a", "7d"]
    check("an isolated one-point drop is ignored and a sustained one-point drop starts a segment",
          noise["cost_intervals"] == 2 and noise["usd_per_percent"] == {"median": 2.5, "q1": 2.25, "q3": 2.75})

    old_reset, new_reset = at(-6), at(162)
    save_rows(audit, [
        audit_row(-12, util={"7d": .10}, ratelimit={"unified-7d-reset": str(old_reset)}),
        audit_row(-11, util={"7d": .20}),
        audit_row(-8, provider="work", account=None, path="/openai/v1/models", method="GET"),
        audit_row(-3, provider="work", account=None, path="/old-prefix/v1/chat/completions",
                  model="gpt-5.2", output=1_000_000),
    ])
    (state / "usage_history.json").write_text(json.dumps([
        {"ts": at(h), "accounts": {"a": [u, reset]}}
        for h, u, reset in ((-12, .10, old_reset), (-11, .20, old_reset), (-9, .35, old_reset),
                            (-5.5, 0, old_reset), (-5, .40, new_reset), (-4, .50, new_reset))
    ]))
    (state / "provider_quota_history.json").write_text(json.dumps({"version": 1, "providers": {"work": [
        {"limit": "codex", "minutes": 10080,
         "points": [[stamp, used, reset, "usage"] for stamp, used, reset in (
             (at(-9), 10, at(-4) + 20), (at(-4) + 5, 50, new_reset), (at(-2), 55, new_reset + 30))]},
    ]}}))
    rows = rows_for()
    general = rows["a", "7d"]
    check("samples after the first audit reading count outside use and set headroom after an idle reset",
          general["sampled_intervals"] == 3 and general["outside_intervals"] == 2
          and general["outside_share"] == 2 / 3 and general["cost_intervals"] == 1
          and general["latest"] == {"at": at(-4), "used_percent": 50, "reset": new_reset}
          and general["remaining"]["percent"] == 50)
    codex = rows["work", "codex"]
    check("a deadline shift breaks the cycle before the old reset and nearby estimates stay joined",
          codex["intervals"] == 1)
    check("OpenAI model traffic under an old URL prefix still counts for the audit provider",
          codex["intervals"] == 1 and codex["cost_intervals"] == 1
          and codex["usd_per_percent"]["median"] == 2.8 and codex["outside_intervals"] == 0)

    save_rows(audit, [
        audit_row(-11, output=0),
        audit_row(-10, util_at=at(-10) + .1, util={"7d": .10}),
        audit_row(-10, util_at=at(-10) + .2, util={"7d": .20}),
    ])
    (state / "usage_history.json").write_text(json.dumps([
        {"ts": at(-10) - 1, "accounts": {"a": [.10, new_reset]}},
        {"ts": at(-10), "accounts": {"a": [.20, new_reset]}},
    ]))
    general = rows_for()["a", "7d"]
    check("a rounded weekly sample and its matching header count the rise once",
          general["intervals"] == 1 and general["cost_intervals"] == 1
          and general["sampled_intervals"] == 1 and general["outside_intervals"] == 0
          and general["latest"]["at"] == at(-10) + .2)

    save_rows(audit, [
        audit_row(-18, util_at=at(-31), util={"7d": .10}),
        audit_row(-15),
    ])
    (state / "usage_history.json").write_text(json.dumps([
        {"ts": at(h), "accounts": {"a": [u, new_reset]}}
        for h, u in ((-30, .10), (-26, .20), (-16, .25), (-14, .30))
    ]))
    r = run("--json")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    general = next(row for row in report["series"] if row["name"] == "a" and row["limit"] == "7d")
    check("a long stream does not extend coverage past the earliest retained completion",
          report["audit"]["from"] == at(-18) and general["sampled_intervals"] == 1
          and general["cost_intervals"] == 1 and general["remaining"]["stale"])

    save_rows(audit, [
        audit_row(-22, util={"7d": .10}),
        audit_row(-21, model=fable["model"], output=25_000, util={"7d": .15}),
        audit_row(-20, model=fable["model"], output=50_000),
        audit_row(-19, output=40_000, util={"7d": .20}),
        audit_row(-2.5, model=fable["model"], output=0, util={"7d_oi": .10}),
        audit_row(-.5, model=fable["model"], output=0, util={"7d_oi": .11}),
    ])
    (state / "usage_history.json").write_text(json.dumps([
        {"ts": at(h), "accounts": {"b": [used, at(105.6)]}}
        for h, used in ((-22, 0), (-2, .01))
    ]))
    rows = rows_for()
    b = rows["b", "7d"]
    check("sub-threshold readings project past the reset without any priced intervals",
          b["cost_intervals"] == 0 and math.isclose(b["remaining"]["hours"], 1978, abs_tol=.01)
          and b["remaining"]["lasts_until_reset"] and b["remaining"]["usd"] is None)
    r = run()
    projection_output = r.stdout
    check("the report caps time at the reset and uses days for uncapped projections over 48 hours",
          r.returncode == 0 and "won't run out before the reset in 4.40 d" in projection_output
          and "Time left at the last 24 h quota pace: 7.40 d" in projection_output
          and "1978.00 h" not in projection_output and "177.50 h" not in projection_output)
    fit = rows["a", "7d"]["fable_fit"]
    check("a non-positive fit coefficient keeps raw JSON coefficients and makes the estimate unavailable",
          fit["a"] == 5 and fit["b"] == -5 and fit["fable_usd_per_percent"] is None
          and fit["other_usd_per_percent"] is None and "7d fit: not enough data (n=2)" in projection_output)

    fit = quota_rate.fit_fable([(.1, .5, 5), (2, 1, 10)])
    check("a near-zero coefficient from floating-point noise is not inverted",
          abs(fit["a"]) < 1e-12 and fit["b"] > 0 and fit["fable_usd_per_percent"] is None)
    fit = quota_rate.fit_fable([(1, 1, 10), (1.1, 1, 10.5)])
    check("positive coefficients are insufficient when Fable's share barely changes",
          fit["a"] > 0 and fit["b"] > 0 and fit["other_usd_per_percent"] is None)
    fit = quota_rate.fit_fable([(2, 8, 10), (3, 7, 10)])
    check("a spread of exactly ten percentage points passes the Fable fit threshold",
          math.isclose(fit["fable_usd_per_percent"], 1) and math.isclose(fit["other_usd_per_percent"], 1))

    (state / "usage_history.json").unlink()
    save_rows(audit, [
        audit_row(-23.5, model=fable["model"], util={"7d_oi": .55},
                  ratelimit={"unified-7d_oi-reset": str(at(-10))}),
        audit_row(-22.5, model=fable["model"], util={"7d_oi": .65}),
        audit_row(-7, util={"7d": .10}),
        audit_row(-6, util={"7d": .20}),
        audit_row(-3, account="b", model=fable["model"], util={"7d_oi": .80, "7d": .88},
                  ratelimit={"unified-7d_oi-reset": str(at(2))}),
        audit_row(-2, account="b", model=fable["model"], util={"7d_oi": .85, "7d": .95}),
        audit_row(-1.5, model=fable["model"], util={"7d_oi": .10},
                  ratelimit={"unified-7d_oi-reset": str(at(10))}),
        audit_row(-.5, model=fable["model"], util={"7d_oi": .20}),
    ])
    rows = rows_for()
    remaining = rows["b", "7d_oi"]["remaining"]
    check("exhaustion is projected from the last reading and compared with now and reset",
          remaining["exhausts_at"] == at(1) and math.isclose(remaining["hours"], 1, abs_tol=.01)
          and not remaining["lasts_until_reset"] and rows["b", "7d"]["remaining"]["hours"] == 0)
    remaining = rows["a", "7d_oi"]["remaining"]
    check("two one-hour rises across a 21-hour reset gap give 240 points a day",
          remaining["pace_points_per_day"] == 240 and math.isclose(remaining["hours"], 7.5, abs_tol=.01))
    remaining = rows["a", "7d"]["remaining"]
    check("a projection from a reading over four hours old is marked stale",
          remaining["stale"] and remaining["hours"] is None and "projection stale" in run().stdout)

    save_rows(audit, [audit_row(-6, util={"7d": .79}), audit_row(-5, util={"7d": .80})])
    (state / "usage_history.json").write_text(json.dumps([
        {"ts": at(h), "accounts": {"a": [.80, new_reset]}, "seen_at": {"a": at(-5)}}
        for h in (-4.5, -3, -2, -1, -.5)
    ]))
    general = rows_for()["a", "7d"]
    check("copies of a cached weekly reading do not make its projection fresh or flatten the pace",
          general["latest"]["at"] == at(-5) and general["remaining"]["stale"]
          and general["remaining"]["pace_points_per_day"] == 24
          and not general["remaining"]["lasts_until_reset"] and general["legacy_history_samples"] == 0)

    accepted = [
        (quota_rate.Reading(at(-4), 10, None), False),
        (quota_rate.Reading(at(-4), 20, None), False),
        (quota_rate.Reading(at(-3), 0, None), True),
        (quota_rate.Reading(at(-3) + 30, 10, None), False),
        (quota_rate.Reading(at(-2), 0, None), True),
        (quota_rate.Reading(at(-1), 5, None), False),
    ]
    check("zero-duration and sub-minute segments do not contribute to pace",
          quota_rate.recent_quota_pace(accepted, now) == 120)

    stop = Event()
    lines = quota_rate.audit_log_lines(audit, {}, stop)
    next(lines)
    stop.set()
    try:
        next(lines)
    except quota_rate.ScanStopped:
        cancelled = True
    else:
        cancelled = False
    check("the audit reader checks the stop flag before the next line", cancelled)
