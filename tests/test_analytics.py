"""Offline tests for claude-rotate analytics: SSE usage capture, pricing,
cost math, and aggregate_audit rollups/alerts/roster."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


# --- usage_from_sse: max-accumulate across events, ignore junk ---
sink = {}
sse = (
    'event: message_start\n'
    'data: {"message": {"usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 200}}}\n\n'
    'event: message_delta\n'
    'data: {"usage": {"output_tokens": 42}}\n\n'
    'data: {broken json with "usage" inside}\n'
    'data: [DONE]\n'
)
R.usage_from_sse(sse, sink)
check("sse captures input_tokens", sink.get("input_tokens") == 10)
check("sse max-accumulates output_tokens", sink.get("output_tokens") == 42)
check("sse captures cache reads", sink.get("cache_read_input_tokens") == 200)

# --- summarize_usage ---
check("summarize None → None", R.summarize_usage(None) is None)
check("summarize fills zeros", R.summarize_usage({"input_tokens": 5}) == {
    "input_tokens": 5, "output_tokens": 0,
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

# --- price_for: substring match + default ---
check("opus priced", R.price_for("claude-opus-5") == [5.0, 25.0])
check("fable priced", R.price_for("claude-fable-5") == [10.0, 50.0])
check("unknown model → default", R.price_for("weird-model-9") == R.DEFAULT_PRICE)
check("None model → default", R.price_for(None) == R.DEFAULT_PRICE)

# --- api_cost: in + out + cache read 0.1x + cache write 1.25x ---
u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
     "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
# opus: 5 + 25 + 0.5 + 6.25
check("api_cost math (opus)", abs(R.api_cost("claude-opus-5", u) - 36.75) < 1e-9)
check("api_cost empty usage is 0", R.api_cost("claude-opus-5", {}) == 0.0)

# --- aggregate_audit: rollups, window cut-off, alerts, roster ---
tmp = Path(tempfile.mkdtemp())
R.AUDIT_PATH = tmp / "audit.jsonl"
R.CFG["devices"] = {"laptop": "key-laptop-abcd", "ci-box": "key-ci-efgh", "idle-dev": "key-idle-ijkl"}
R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}}
R.STATE = {"active_account": "a", "events": [],
           "accounts": {"a": {"util_5h": 0.85, "reset_5h": time.time() + 3600}, "b": {}}}

FMT = "%Y-%m-%dT%H:%M:%S%z"
now = time.time()


def rec(ts, device, model, account, inp=0, out=0, cr=0, cw=0):
    return {"ts": time.strftime(FMT, time.localtime(ts)), "device": device,
            "model": model, "account": account, "status": 200,
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}


records = [
    rec(now - 60, "laptop", "claude-opus-5", "a", inp=1000, out=500),        # last hour, expensive
    rec(now - 7200, "laptop", "claude-haiku-4-5", "a", inp=2000, out=100),   # today, outside hour
    rec(now - 90000, "laptop", "claude-opus-5", "a", inp=9999, out=9999),    # >24h → excluded
    rec(now - 120, "ci-box", "claude-sonnet-5", "b", inp=6_000_000, out=10), # token/hour alert
]
with R.AUDIT_PATH.open("w") as f:
    for r in records:
        f.write(json.dumps(r) + "\n")
    f.write("not json\n")  # must be skipped, not crash

agg = R.aggregate_audit()

check("old record excluded from device rollup",
      agg["by_device"]["laptop"]["requests"] == 2)
check("device rollup sums tokens",
      agg["by_device"]["laptop"]["input_tokens"] == 3000)
check("model rollup present", agg["by_model"]["claude-opus-5"]["requests"] == 1)
check("account rollup present", agg["by_account"]["b"]["requests"] == 1)
check("cost accumulated", agg["by_device"]["laptop"]["cost_usd"] > 0)

alert_texts = " | ".join(a["text"] for a in agg["alerts"])
check("token/hour alert fires", "ci-box" in alert_texts and "6,000,010" in alert_texts)
check("expensive-model alert fires", "laptop / claude-opus-5" in alert_texts)
check("util warn alert fires", "a 5h window at 85%" in alert_texts)

devs = agg["devices"]
check("roster covers idle device too", "idle-dev" in devs)
check("recent device online", devs["laptop"]["online"] is True)
check("idle device offline, never seen",
      devs["idle-dev"]["online"] is False and devs["idle-dev"]["last_seen_min"] is None)
check("roster last-hour counters", devs["ci-box"]["req_1h"] == 1
      and devs["ci-box"]["tokens_1h"] == 6_000_010)
check("roster shows key suffix", devs["laptop"]["key_suffix"] == "…abcd")
check("roster last model/account", devs["laptop"]["last_model"] == "claude-opus-5"
      and devs["laptop"]["last_account"] == "a")

# --- weekly chart: summed usage dropping at resets, weights, run-out projection ---
H = 3600
old_a, new_a, rb = now - 20 * H, now - 20 * H + 7 * 86400, now + 50 * H


def weekly_state(ua, ub):
    R.STATE = {"active_account": "a", "events": [], "accounts": {
        "a": {"util_7d": ua, "reset_7d": new_a}, "b": {"util_7d": ub, "reset_7d": rb}}}


R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}}
R.HISTORY = [{"ts": now - 30 * H, "accounts": {"a": [0.90, old_a], "b": [0.20, rb]}},
             {"ts": now - 20 * H, "accounts": {"a": [0.00, old_a], "b": [0.20, rb]}},  # a's week reset
             {"ts": now - 10 * H, "accounts": {"a": [0.10, new_a], "b": [0.30, rb]}}]
weekly_state(0.30, 0.30)
s = R.weekly_series(now)
check("chart sums each account's current weekly usage, dropping at a reset",
      [p[1] for p in s] == [110, 20, 40, 60])
check("capacity sums the weights", [p[2] for p in s] == [200, 200, 200, 200])
check("spent counts rises after the first sample, a new window in full",
      [p[3] for p in s] == [0, 0, 20, 40])

out = R.weekly_outlook(now)
check("rate taken over the last 24 h; the series goes out without spent",
      out["rate_per_day"] == 48.0 and out["capacity"] == 200
      and [len(p) for p in out["series"]] == [3, 3, 3, 3])
proj = out["projection"]
# b resets at +50 h with its week fully used (-100); everything is spent at +120 h and
# usage pauses until a resets at +148 h (-100); then it resumes until the 7.5-day horizon.
check("7.5-day projection drops usage at each reset and pauses while all is spent",
      [p[1] for p in proj] == [60, 160, 60, 200, 200, 100, 164])
check("projected capacity stays flat", all(p[2] == 200 for p in proj))
check("ETA is the first shortfall; quota returns at a's reset",
      abs(out["eta"] - (now + 120 * H)) <= 5 and out["back"] == round(new_a)  # ts rounding
      and out["back_account"] == "a" and proj[-1][0] == round(now + 7.5 * 86400))

weekly_state(0.95, 0.95)
out = R.weekly_outlook(now)
check("run-out ETA when the remaining 10 points go at 8.5 points/h",
      abs(out["eta"] - (now + 10 / 8.5 * H)) <= 1 and out["back"] == round(rb)
      and out["back_account"] == "b")

R.ACCOUNTS["b"]["weight"] = 0.25  # b becomes a quarter-size plan
weekly_state(0.30, 0.30)
check("weights scale an account's share", R.weekly_series(now)[-1][1:3] == [37.5, 125])
R.ACCOUNTS["b"] = {"name": "b", "disabled": True}
check("disabled accounts leave the chart", R.weekly_series(now)[-1][1:3] == [30, 100])
R.ACCOUNTS["b"] = {"name": "b"}

R.HISTORY = [{"ts": now - 20 * H, "accounts": {"a": [0.50, now + 5 * H], "b": [0.50, rb]}},
             {"ts": now - 10 * H, "accounts": {"a": [1.00, now + 5 * H], "b": [1.00, rb]}}]
R.STATE = {"active_account": "a", "events": [], "accounts": {
    "a": {"util_7d": 1.0, "reset_7d": now + 5 * H}, "b": {"util_7d": 1.0, "reset_7d": rb}}}
out = R.weekly_outlook(now)
check("rate ignores time at the limit: 100 points in the 10 h with quota left",
      out["rate_per_day"] == 240.0 and abs(out["eta"] - now) <= 1 and out["back_account"] == "a")

R.HISTORY = []
check("no rate without an hour of history", R.weekly_outlook(now)["eta"] is None
      and "not enough history" in R.weekly_outlook(now)["note"])

R.HISTORY_PATH = tmp / "usage_history.json"
R.HISTORY = [{"ts": now - 15 * 86400, "accounts": {"a": [0.1, old_a]}}]
R.record_history(now)
check("history sample saved, 15-day-old one dropped",
      [h["ts"] for h in R.HISTORY] == [round(now)]
      and json.loads(R.HISTORY_PATH.read_text()) == R.HISTORY)
R.record_history(now + 600)
R.record_history(now + 1801)  # stored ts is rounded
check("one sample per 30 min", len(R.HISTORY) == 2)

# --- device/model totals: 7 d, 30 d, all time, trend; backfill from the audit logs ---
import datetime as dt
today = dt.date.today()


def iso(n):
    return (today - dt.timedelta(days=n)).isoformat()


def empty_rollup():
    return {"days": {}, "total": {"device": {}, "model": {}}, "since": None}


use = {"input_tokens": 1_000_000, "output_tokens": 10}
R.ROLLUP = empty_rollup()
R.roll_up({"device": "laptop", "model": "claude-opus-5", "usage": use}, iso(0))
R.roll_up({"device": "laptop", "model": "claude-opus-5", "usage": use}, iso(0))
R.roll_up({"device": "old-box", "model": {"bad": 1}, "usage": use}, iso(8))
per = R.usage_periods()
check("7 d covers today and the 6 days before",
      per["7d"]["device"]["laptop"]["requests"] == 2 and "old-box" not in per["7d"]["device"])
check("30 d and all time include older days",
      per["30d"]["device"]["old-box"]["requests"] == 1 and per["all"]["device"]["old-box"]["requests"] == 1)
check("non-string models fold into '?'", "?" in per["all"]["model"])
check("trend is 30 daily costs ending today", len(per["trend"]["laptop"]) == 30
      and per["trend"]["laptop"][-1] == 10.0 and per["trend"]["old-box"][-9] == 5.0)
check("since is the oldest day seen", per["since"] == iso(8))

R.AUDIT_PATH, R.USAGE_PATH = tmp / "backfill.jsonl", tmp / "device_usage.json"


def line(day, dev):
    return json.dumps({"ts": f"{day}T10:00:00+0000", "device": dev, "model": "claude-sonnet-5",
                       "usage": use}) + "\n"


Path(f"{R.AUDIT_PATH}.1").write_text(line(iso(40), "gone-box") + "not json\n")
R.AUDIT_PATH.write_text(line(iso(0), "laptop"))
R.ROLLUP = empty_rollup()
R.load_rollup()
check("first start rebuilds totals from the rotated copy and the current log",
      R.ROLLUP["total"]["device"]["gone-box"]["requests"] == 1
      and R.ROLLUP["total"]["device"]["laptop"]["requests"] == 1 and R.ROLLUP["since"] == iso(40))
R.save_rollup()
saved = json.loads(R.USAGE_PATH.read_text())
check("saved totals keep all time but only 31 days of daily buckets",
      iso(40) not in saved["days"] and saved["total"]["device"]["gone-box"]["requests"] == 1)
R.ROLLUP = empty_rollup()
R.load_rollup()
check("an existing file is loaded, not rebuilt", R.ROLLUP == saved)
R.audit({"device": "laptop", "model": "claude-sonnet-5", "usage": use})
check("every audited request is added", R.ROLLUP["total"]["device"]["laptop"]["requests"] == 2)

# --- billing: plan timelines, billing periods, the daily ledger and the report ---
D = dt.date
check("a month later keeps the day or takes the month's last",
      R.month_add(D(2027, 1, 31), 1) == D(2027, 2, 28) and R.month_add(D(2027, 1, 31), 2) == D(2027, 3, 31)
      and R.month_add(D(2028, 1, 31), 1) == D(2028, 2, 29) and R.month_add(D(2026, 11, 14), 2) == D(2027, 1, 14))
up = [(0, D(2026, 9, 14), "max", False), (0, D(2026, 10, 20), "ultra", False)]
check("an upgrade keeps the renewal date", R.billing_periods(up, D(2026, 11, 3))
      == [(D(2026, 9, 14), D(2026, 10, 14)), (D(2026, 10, 14), D(2026, 11, 14))])
restart = [(0, D(2026, 9, 5), "pro", False), (0, D(2026, 10, 20), "max", True)]
check("an entry marked renews restarts the cycle", R.billing_periods(restart, D(2026, 11, 25))
      == [(D(2026, 9, 5), D(2026, 10, 5)), (D(2026, 10, 5), D(2026, 10, 20)),
          (D(2026, 10, 20), D(2026, 11, 20)), (D(2026, 11, 20), D(2026, 12, 20))])

R.BILLING_ERRORS.clear()
R.CFG = {"plans": {"team": {"price": 30, "weight": 0.3}, "bad": {"price": -1}}}
R.ACCOUNTS = {"a": {"name": "a", "billing": [{"from": "2026-09-14", "plan": "max"},
                                             {"from": "2026-10-20", "plan": "ultra"}]},
              "b": {"name": "b"},
              "c": {"name": "c", "billing": [{"from": "2026-10-09", "plan": "max"},
                                             {"from": "2026-10-05", "plan": "max"}]}}
R.BILLING = R.load_billing()
del R.PLANS["team"]
check("bad plans and out-of-order timelines are reported and ignored, not fatal",
      set(R.BILLING) == {"a"} and "bad" not in R.PLANS and len(R.BILLING_ERRORS) == 2)
midnight = lambda d: time.mktime(d.timetuple())
check("weight follows the plan in force",
      R.weight("a", midnight(D(2026, 10, 1))) == 1 and R.weight("a", midnight(D(2026, 10, 20))) == 2
      and R.weight("b") == 1)

R.ROLLUP = empty_rollup()
base = midnight(today - dt.timedelta(days=1)) + 10 * H
r1, r2 = base + 3 * 86400, base + 9 * 86400
samples = [{"ts": base, "accounts": {"a": [0.50, r1], "b": [0.90, r2]}},
           {"ts": base + 1800, "accounts": {"a": [0.60, r1], "b": [1.00, r2]}},
           {"ts": base + 3600, "accounts": {"a": [0.05, r1 + 7 * 86400], "b": [1.00, r2]}},  # a's reset
           {"ts": base + 5400, "accounts": {"a": [1.00, r1 + 7 * 86400], "b": [1.00, r2]}},
           {"ts": base + 6 * H, "accounts": {"a": [1.00, r1 + 7 * 86400], "b": [1.00, r2]}}]  # after downtime
for prev, cur in zip([None] + samples, samples):
    R.ledger_sample(prev, cur)
q = R.ROLLUP["quota"][iso(1)]
check("ledger credits rises, a new window in full, and time at the weekly limit",
      q["a"] == [110.0, 1800] and q["b"] == [10.0, 5400])
check("pool time at the limit, not counting downtime", R.ROLLUP["blocked"][iso(1)] == 1800)

R.ROLLUP = empty_rollup()
R.ROLLUP.update(quota={}, blocked={"2026-10-21": 1800}, accounts={"2026-10-20": {"a": {"requests": 3, "cost_usd": 361.3}}})
for n in range(21):  # Oct 14 .. Nov 3, a missing on Oct 25
    day = (D(2026, 10, 14) + dt.timedelta(days=n)).isoformat()
    R.ROLLUP["quota"][day] = {"b": [5.0, 0]} | ({} if day == "2026-10-25" else {"a": [10.0, 3600 if n == 0 else 0]})
rep = R.billing_report(D(2026, 11, 3))["a"]
cur, prev = rep["periods"]
check("report: current plan, renewal and newest period first",
      rep["plan"] == "ultra" and rep["renews"] == "2026-11-14" and cur["start"] == "2026-10-14")
check("an upgrade inside the period prorates its price by days",
      cur["plans"] == ["max", "ultra"] and cur["price"] == round((6 * 100 + 25 * 200) / 31, 2))
check("days elapsed and recorded", cur["days"] == 31 and cur["elapsed"] == 21 and cur["covered"] == 20)
check("weekly use is the share of the quota the plan gave, whatever the plan", cur["used"] == 70.0)
check("pool share: 15 of 200/7 a day on Max days, 25 of 300/7 on Ultra days",
      cur["pool_used"] == round(100 * (6 * 15 + 14 * 25) * 7 / (6 * 200 + 14 * 300), 1) and cur["pool_peak"] == 58.3)
check("on the next plan down every day has 200/7",
      cur["whatif"] == [{"plan": "max", "price": -100, "pool_used": round(100 * 440 * 7 / (20 * 200), 1),
                         "pool_peak": 87.5}])
check("hours at the limit and the period's requests and cost",
      cur["limited_h"] == 1.0 and cur["blocked_h"] == 0.5 and cur["usage"] == {"requests": 3, "cost_usd": 361.3})
check("a period without history has no use figures; Max compares to Pro and Ultra",
      prev["covered"] == 0 and prev["used"] is None and prev["price"] == 100
      and [w["plan"] for w in prev["whatif"]] == ["pro", "ultra"])

R.ROLLUP = {"days": {}, "total": {"device": {}, "model": {}}, "since": None}  # a file from before the ledger
R.USAGE_PATH.write_text(json.dumps(R.ROLLUP))
R.AUDIT_PATH.write_text(json.dumps({"ts": f"{iso(2)}T10:00:00+0000", "device": "laptop", "account": "a",
                                    "model": "claude-sonnet-5", "usage": use}) + "\n")
R.HISTORY = samples
R.load_rollup()
check("a file from before the ledger gets only the ledger rebuilt",
      R.ROLLUP["accounts"][iso(2)]["a"]["requests"] == 1 and R.ROLLUP["quota"][iso(1)]["a"] == [110.0, 1800]
      and R.ROLLUP["days"] == {})

print(f"\nall {PASS} checks passed")
