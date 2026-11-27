"""Offline tests for claude-rotate switch logic (pick_account / note_response)."""
import asyncio
import json
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

R.save_state = lambda: None  # don't touch the real state.json


class H(dict):
    """httpx.Headers stand-in (case-insensitive get)."""
    def get(self, k, default=None):
        return super().get(k.lower(), default)


def headers(u5=None, u7=None, r5=None, r7=None, status=None, retry_after=None):
    h = H()
    if u5 is not None: h["anthropic-ratelimit-unified-5h-utilization"] = str(u5)
    if u7 is not None: h["anthropic-ratelimit-unified-7d-utilization"] = str(u7)
    if r5 is not None: h["anthropic-ratelimit-unified-5h-reset"] = str(r5)
    if r7 is not None: h["anthropic-ratelimit-unified-7d-reset"] = str(r7)
    if status is not None: h["anthropic-ratelimit-unified-status"] = status
    if retry_after is not None: h["retry-after"] = str(retry_after)
    return h


def fresh_state(active="a"):
    R.STATE = {"active_account": active, "accounts": {}, "events": [], "last_switch_ts": 0}


R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}, "c": {"name": "c"}}
R.THRESHOLD, R.THRESHOLD_7D = 0.8, 0.98
R.STRATEGY, R.COOLDOWN_S, R.SWITCH_MARGIN, R.CF_MARGIN_S = "consume-first", 300, 0.05, 3600
run = asyncio.run
now = time.time()
PASS = 0

def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


# 1. consume-first: pick usable account with soonest weekly reset
fresh_state()
R.STATE["accounts"] = {
    "a": {"util_5h": 0.1, "util_7d": 0.3, "reset_7d": now + 500000},
    "b": {"util_5h": 0.5, "util_7d": 0.6, "reset_7d": now + 100000},  # soonest weekly reset
    "c": {"util_5h": 0.05, "util_7d": 0.1, "reset_7d": now + 300000},
}
check("consume-first picks soonest weekly reset", R.pick_account() == "b")

# 1b. ...but skips unusable (5h over threshold) accounts
R.STATE["accounts"]["b"]["util_5h"] = 0.9
check("consume-first skips 5h-exhausted account", R.pick_account() == "c")

# 1c. least-used strategy picks lowest 5h util
R.STRATEGY = "least-used"
check("least-used picks lowest 5h util", R.pick_account() == "c")
R.STRATEGY = "consume-first"

# 2. burst 429 (util well under 1.0, no rejected status) → pace, no rotation
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.4, "util_7d": 0.2}}
v = run(R.note_response("a", headers(u5=0.4, u7=0.2, retry_after=7), 429))
check("burst 429 verdict is 'burst'", v == "burst")
check("burst 429 does not rotate", R.STATE["active_account"] == "a")
check("burst 429 sets only the default family's pace deadline",
      6 < R.STATE["accounts"]["a"]["pace_by_family"]["default"] - time.time() <= 7.5
      and "fable" not in R.STATE["accounts"]["a"]["pace_by_family"])

# 3. quota 429 (unified-status rejected) → rotate despite cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()  # cooldown active — must not block a hard stop
R.STATE["accounts"] = {
    "a": {"util_5h": 1.0, "util_7d": 0.5, "reset_7d": now + 500000},
    "b": {"util_5h": 0.1, "util_7d": 0.2, "reset_7d": now + 100000},
    "c": {"util_5h": 0.1, "util_7d": 0.2, "reset_7d": now + 300000},
}
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
check("quota 429 switches", v == "switched")
check("quota 429 lands on soonest weekly reset", R.STATE["active_account"] == "b")
check("switch event reason", R.STATE["events"][-1]["reason"] == "quota_exhausted")

# 4. all spent → exhausted
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 1.0, "reset_5h": now + 1000},
    "b": {"util_5h": 1.0, "reset_5h": now + 2000},
    "c": {"util_5h": 1.0, "reset_5h": now + 500},
}
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
check("all-spent quota hit leaves the active route and has no dispatch candidate",
      v == "exhausted" and R.STATE["active_account"] == "a" and R.pick_account() is None)
check("earliest_relief is soonest reset", abs(R.earliest_relief() - (now + 500)) < 5)

# 5. threshold switch respects cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()
R.STATE["accounts"] = {"a": {"util_5h": 0.85}, "b": {"util_5h": 0.1}, "c": {"util_5h": 0.2}}
v = run(R.note_response("a", headers(u5=0.85), 200))
check("threshold switch blocked by cooldown", v is None and R.STATE["active_account"] == "a")
R.family_route("default")["last_switch_ts"] = 0
v = run(R.note_response("a", headers(u5=0.85), 200))
check("threshold switch after cooldown", v == "switched" and R.STATE["active_account"] == "b")

# 6. hysteresis: candidate must beat active by margin
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.81}, "b": {"util_5h": 0.79}, "c": {"util_5h": 0.80}}
v = run(R.note_response("a", headers(u5=0.81), 200))
check("hysteresis blocks ping-pong at the line", v is None and R.STATE["active_account"] == "a")

# 7. proactive consume-first: jump to clearly-sooner weekly reset even below threshold
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 500000},
    "b": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 100000},
    "c": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 300000},
}
v = run(R.note_response("a", headers(u5=0.3, u7=0.4), 200))
check("proactive consume-first switch", v == "switched" and R.STATE["active_account"] == "b")
check("event reason consume_first", R.STATE["events"][-1]["reason"] == "consume_first")
# 7b. margin: near-equal weekly resets don't churn
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 101000},
    "b": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 100000},
}
v = run(R.note_response("a", headers(u5=0.3, u7=0.4), 200))
check("consume-first margin prevents churn", v is None and R.STATE["active_account"] == "a")

# 8. window that passed its reset counts as 0
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 1.0, "reset_5h": now - 10, "util_7d": 0.9, "reset_7d": now - 10}}
check("passed 5h reset → util 0", R.utilization("a") == 0.0)
check("passed 7d reset → util 0", R.util_7d("a") == 0.0)

# 9. priority tiers: preferred tier always beats a sooner weekly reset
R.ACCOUNTS = {"a": {"name": "a", "priority": 2},
              "b": {"name": "b", "priority": 1}, "c": {"name": "c", "priority": 1}}
fresh_state()
R.STATE["accounts"] = {
    "a": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 1000},    # soonest, but backup tier
    "b": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 500000},
    "c": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 100000},
}
check("priority tier beats sooner weekly reset", R.pick_account() == "c")

# 9b. backup tier only used once the preferred tier is spent
R.STATE["accounts"]["b"]["util_5h"] = 0.9
R.STATE["accounts"]["c"]["util_5h"] = 0.9
check("backup tier used when preferred spent", R.pick_account() == "a")

# 10. priority recovery: traffic pulled back once a preferred account has room
fresh_state("a")  # active on the backup
R.STATE["accounts"] = {
    "a": {"util_5h": 0.2, "util_7d": 0.1},
    "b": {"util_5h": 0.1, "util_7d": 0.1},
    "c": {"util_5h": 0.9, "util_7d": 0.1},
}
v = run(R.note_response("a", headers(u5=0.2, u7=0.1), 200))
check("priority recovery switches back", v == "switched" and R.STATE["active_account"] == "b")
check("recovery event reason", R.STATE["events"][-1]["reason"] == "priority_recovery")

# 10b. recovery waits out the cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()
R.STATE["accounts"] = {"a": {"util_5h": 0.2}, "b": {"util_5h": 0.1}, "c": {"util_5h": 0.9}}
v = run(R.note_response("a", headers(u5=0.2), 200))
check("priority recovery respects cooldown", v is None and R.STATE["active_account"] == "a")

# 11. disabled accounts: never picked, active one abandoned immediately
R.ACCOUNTS = {"a": {"name": "a", "disabled": True}, "b": {"name": "b"}, "c": {"name": "c"}}
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.0}, "b": {"util_5h": 0.2}, "c": {"util_5h": 0.1}}
check("disabled excluded from pick", R.pick_account() == "c")
R.STATE["last_switch_ts"] = time.time()  # even inside the cooldown
v = run(R.note_response("a", headers(u5=0.0), 200))
check("disabled active abandoned", v == "switched" and R.STATE["active_account"] == "c")
check("disabled event reason", R.STATE["events"][-1]["reason"] == "account_disabled")

# 11b. Hard exhaustion skips every blocked account, including the disabled one.
fresh_state("b")
R.STATE["accounts"] = {"a": {"util_5h": 1.0, "reset_5h": now + 10},
                       "b": {"util_5h": 1.0, "reset_5h": now + 100},
                       "c": {"util_5h": 1.0, "reset_5h": now + 50}}
check("no fallback dispatches to a blocked or disabled account", R.pick_account() is None)

# 12. a panel pick holds against consume-first until the account reaches a threshold
R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}, "c": {"name": "c"}}
fresh_state("a")
R.STATE["pinned"] = "a"
R.STATE["accounts"] = {"a": {"util_5h": 0.2, "reset_7d": now + 500000},
                       "b": {"util_5h": 0.1, "reset_7d": now + 100000}, "c": {"util_5h": 0.9}}
v = run(R.note_response("a", headers(u5=0.2), 200))
check("pinned account holds against consume-first", v is None and R.STATE["active_account"] == "a")
v = run(R.note_response("a", headers(u5=0.85), 200))
check("pinned account still leaves at its threshold", v == "switched" and R.STATE["active_account"] == "b")
run(R.note_response("b", headers(u5=0.1), 200))
check("the pin ends with the switch", "pinned" not in R.STATE)

# 12b. ...and when the account runs out of quota with nowhere to go
fresh_state("a")
R.STATE["pinned"] = "a"
R.STATE["accounts"] = {"a": {"util_5h": 0.5}, "b": {"util_5h": 1.0, "reset_5h": now + 900},
                       "c": {"util_5h": 1.0, "reset_5h": now + 900}}
v = run(R.note_response("a", headers(u5=1.0, r5=now + 600, status="rejected"), 429))
check("the pin ends at quota exhaustion without a switch", v == "exhausted" and "pinned" not in R.STATE)

# 13. Old files migrate atomically and can be loaded again without rewriting.
legacy = {"active_account": "a", "pinned": "a", "last_switch_ts": now - 100,
          "events": [{"type": "switch", "to": "a"}], "other": {"keep": True},
          "accounts": {"a": {"util_5h": 0.3, "reset_5h": now + 1000, "util_7d": 0.69,
                             "status": "rejected", "last_seen": now - 20, "pace_until": now + 60},
                       "b": {"reset_7d": now + 100000}, "c": {}}}
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "state.json"
    path.write_text(json.dumps(legacy))
    with patch.object(R, "STATE_PATH", path):
        migrated = R.load_state()
        check("legacy state migrates once to general claims and both family routes",
              migrated == json.loads(path.read_text()) and migrated["version"] == R.STATE_VERSION
              and migrated["accounts"]["a"]["claims"] == {
                  "5h": {"utilization": 0.3, "reset": now + 1000, "seen_at": now - 20},
                  "7d": {"utilization": 0.69, "seen_at": now - 20}}
              and migrated["accounts"]["b"]["claims"] == {"7d": {"reset": now + 100000}}
              and migrated["accounts"]["c"]["claims"] == {}
              and migrated["routes"] == {family: {"active_account": "a", "manual_account": "a",
                                                 "last_switch_ts": now - 100}
                                         for family in ("default", "fable")})
        with patch.object(R, "write_state", side_effect=AssertionError("state migrated again")):
            check("reloading keeps claims, event history and the live pace without a shared rejected claim",
                  R.load_state() == migrated and migrated["events"] == legacy["events"]
                  and migrated["other"] == legacy["other"] and "status" not in migrated["accounts"]["a"]
                  and migrated["accounts"]["a"]["pace_by_family"] == {"default": now + 60, "fable": now + 60})
        with patch.object(R, "STATE", migrated), patch.object(R, "BILLING", {}):
            check("migrated reset-only 7d retains capacity with unknown usage counted as zero",
                  R.weekly_snapshot() == {"b": [0.0, now + 100000]}
                  and R.weekly_outlook(now)["capacity"] == 100)
        path.write_text(json.dumps(legacy | {"last_switch_ts": "bad", "accounts": {
            "a": {"pace_until": "not a deadline"}, "b": {"pace_until": now + 60}}}))
        repaired = R.load_state()
        check("legacy migration normalizes cooldown and pace while retaining live numeric deadlines",
              repaired["last_switch_ts"] == 0
              and all(route["last_switch_ts"] == 0 for route in repaired["routes"].values())
              and "pace_until" not in repaired["accounts"]["a"]
              and "pace_by_family" not in repaired["accounts"]["a"]
              and repaired["accounts"]["b"]["pace_until"] == now + 60
              and repaired["accounts"]["b"]["pace_by_family"] == {"default": now + 60, "fable": now + 60})
        repaired["last_switch_ts"] = "bad"
        repaired["routes"]["default"]["last_switch_ts"] = "bad"
        repaired["routes"]["fable"].update(active_account="b", last_switch_ts=now - 10)
        repaired["accounts"]["a"]["pace_until"] = "bad"
        repaired["accounts"]["b"]["pace_by_family"]["default"] = "bad"
        path.write_text(json.dumps(repaired))
        repaired = R.load_state()
        check("already migrated malformed deadlines repair without reseeding the valid Fable route",
              repaired["last_switch_ts"] == repaired["routes"]["default"]["last_switch_ts"] == 0
              and repaired["routes"]["fable"]["active_account"] == "b"
              and repaired["routes"]["fable"]["last_switch_ts"] == now - 10
              and "pace_until" not in repaired["accounts"]["a"]
              and repaired["accounts"]["b"]["pace_by_family"] == {"fable": now + 60})

# 14. Claims are learned from model answers and retired only by authoritative snapshots.
R.STATE = migrated
weekly = headers(u7=0.69, r7=now + 200000, status="allowed")
fable = weekly | {"anthropic-ratelimit-unified-7d_oi-utilization": "0.48",
                  "anthropic-ratelimit-unified-7d_oi-reset": str(now + 100000),
                  "anthropic-ratelimit-unified-7d_oi-status": "allowed",
                  "anthropic-ratelimit-unified-model-x-utilization": "0.7",
                  "anthropic-ratelimit-unified-overage-status": "rejected"}
run(R.note_response("a", httpx.Headers(fable), 200, family=R.model_family("Claude-FaBlE-5-2")))
info = R.STATE["accounts"]["a"]
check("a weekly-only answer retires 5h, keeps general 7d and records Fable utilization as reported",
      set(info["claims"]) == {"7d", "7d_oi", "model-x"} and "util_5h" not in info and "reset_5h" not in info
      and R.util_7d("a") == 0.69 and R.claim_utilization("a", "7d_oi") == 0.48
      and set(R.applicable_claims(info, "default")) == {"7d"}
      and set(R.applicable_claims(info, "fable")) == {"7d", "7d_oi", "model-x"}
      and R.model_family(None) == R.model_family("claude-opus-5") == "default")
run(R.note_response("a", httpx.Headers(weekly | {"anthropic-ratelimit-unified-model-x-utilization": "0.8"}),
                    200, family="default"))
run(R.note_response("a", weekly, 200, family="default"))
run(R.note_response("a", weekly, 200, family=None))
run(R.note_response("a", headers(), 200, family="fable"))
run(R.note_response("a", weekly, 429, family="fable"))
check("default, unrelated, headerless and partial 429 answers keep Fable evidence",
      set(R.applicable_claims(info, "fable")) == {"7d", "7d_oi", "model-x"}
      and R.applicable_claims(info, "default").keys() == {"7d"}
      and info["last_success_claims"] == {"default": ["7d"], "fable": ["7d", "7d_oi", "model-x"]})

malformed = httpx.Headers([*weekly.items(), ("anthropic-ratelimit-unified-7d_oi-utilization", "0.99"),
                           ("anthropic-ratelimit-unified-7d_oi-utilization", "0.1"),
                           ("anthropic-ratelimit-unified-7d_oi-reset", "NaN"),
                           ("anthropic-ratelimit-unified-7d_oi-status", "rejected"),
                           ("anthropic-ratelimit-unified-empty-utilization", "-1")])
run(R.note_response("a", malformed, 200, family="fable"))
check("conflicting or malformed fields keep known values and do not create a zero reading",
      R.claim_utilization("a", "7d_oi") == 0.48 and info["claims"]["7d_oi"]["reset"] == now + 100000
      and info["claims"]["7d_oi"]["status"] == "rejected" and "empty" not in info["claims"])
run(R.note_response("a", weekly, 200, family="fable"))
check("an authoritative Fable answer can retire missing Fable claims",
      set(info["claims"]) == {"7d"} and R.claim_utilization("a", "7d_oi") is None)

run(R.note_response("a", headers(u5=1, r5=now - 1), 429))
check("expired claim utilization is unknown", R.claim_utilization("a", "5h") is None)
run(R.note_response("a", headers(r5=now + 100), 200))
check("a reset alone does not carry old usage into a new window", R.claim_utilization("a", "5h") is None)
run(R.note_response("a", headers(u7=1, r7=now - 1), 200))
run(R.note_response("a", headers(u7=0.42), 200))
check("fresh utilization without a reset replaces expired weekly evidence",
      R.claim_utilization("a", "7d") == R.util_7d("a") == info["util_7d"] == 0.42
      and "reset" not in info["claims"]["7d"] and "reset_7d" not in info)

learned_header = "anthropic-ratelimit-unified-model-x"
run(R.note_response("a", httpx.Headers(weekly | {f"{learned_header}-utilization": "0.5"}), 200))
many_claims = {f"anthropic-ratelimit-unified-malformed-{i}-utilization": "NaN"
               for i in range(R.MAX_LEARNED_CLAIMS)}
many_claims.update({f"anthropic-ratelimit-unified-new-{i}-utilization": "0.2"
                    for i in range(R.MAX_LEARNED_CLAIMS)})
many_claims.update(weekly | {f"{learned_header}-utilization": "1", f"{learned_header}-reset": str(now + 1000)})
run(R.note_response("a", httpx.Headers(many_claims), 200))
check("valid new IDs fill the cap without masking a later existing learned blocker",
      R.claim_utilization("a", "model-x") == 1 and R.blocker_deadline("a", "default", now) == now + 1000
      and len(info["learned_claims"]["default"]) == R.MAX_LEARNED_CLAIMS
      and "new-0" in info["claims"] and not any(key.startswith("malformed-") for key in info["claims"]))

# 15. The chart, projection and billing ledger follow general weekly usage only.
R.STATE["accounts"] = {
    "a": {"util_7d": 1, "reset_7d": now + 10, "claims": {
        "7d": {"utilization": 0.69, "reset": now + 200000},
        "7d_oi": {"utilization": 1, "reset": now + 100000}}},
    "b": {"claims": {"7d_oi": {"utilization": 1, "reset": now + 100000}}}}
with tempfile.TemporaryDirectory() as tmp:
    sample_time = int(now)
    with patch.object(R, "HISTORY_PATH", Path(tmp) / "usage_history.json"), patch.object(
            R, "HISTORY", [{"ts": sample_time - 3600, "accounts": {"a": [0.5, now + 200000]}}]), patch.object(
            R, "ROLLUP", {}), patch.object(R, "BILLING", {}):
        R.record_history(sample_time)
        ledger = R.ROLLUP["quota"][time.strftime("%Y-%m-%d", time.localtime(sample_time))]["a"]
        outlook = R.weekly_outlook(sample_time)
        check("weekly history, projection and billing use general 7d and its reset",
              R.HISTORY[-1]["accounts"] == {"a": [0.69, now + 200000]}
              and R.weekly_reset("a") == now + 200000 and outlook["capacity"] == 100
              and outlook["projection"][0][1:] == [69, 100]
              and abs(ledger[0] - 19) < 0.01 and ledger[1] == 0)

# 16. Relief needs the last applicable blocker to expire on at least one account.
fresh_state()
R.STATE["accounts"] = {
    "a": {"claims": {"7d": {"utilization": 1, "reset": now + 3600},
                     "7d_oi": {"status": "rejected", "reset": now + 10800}}},
    "b": {"claims": {"7d": {"utilization": 0.69, "reset": now + 9000},
                     "7d_oi": {"utilization": 1, "reset": now + 7200}}}}
with patch.object(R, "ACCOUNTS", {name: R.ACCOUNTS[name] for name in ("a", "b")}):
    check("family relief takes the earliest account's last blocker, and default ignores Fable",
          R.earliest_relief("fable", now) == now + 7200 and R.earliest_relief("default", now) == now
          and R.pick_account("fable") is None and R.pick_account("default") == "b")

for representative, expected in (("seven_day", R.RECHECK_S), ("seven_day_oi", 8000)):
    fresh_state()
    ambiguous = headers(u7=0.69, r7=now + 8000, status="rejected") | {
        "anthropic-ratelimit-unified-representative-claim": representative,
        "anthropic-ratelimit-unified-reset": str(now + 8000)}
    run(R.note_response("a", httpx.Headers(ambiguous), 429, family="fable"))
    check(f"aggregate rejection with {representative} blocks only Fable for the supported duration",
          abs(R.blocker_deadline("a", "fable", now) - now - expected) < 5
          and R.blocker_deadline("a", "default", now) == 0)

fresh_state()
run(R.note_response("a", httpx.Headers({"anthropic-ratelimit-unified-7d_oi-status": "rejected"}),
                    429, family="fable"))
check("a rejected Fable claim without a reset uses a bounded recheck",
      now < R.blocker_deadline("a", "fable", now) < now + R.RECHECK_S + 5)
run(R.note_response("a", weekly, 200, family="default"))
check("default success does not erase a rejection on the Fable claim",
      R.blocker_deadline("a", "fable", now) > now)
run(R.note_response("a", weekly, 200, family="fable"))
check("authoritative Fable recovery removes its old hard block", R.blocker_deadline("a", "fable", now) == 0)

print(f"\nall {PASS} checks passed")
