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

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3600


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
        "accounts": [{"name": n, "token_file": n + ".token"} for n in ("a", "b")],
        "devices": {}, "providers": {"work": {"kind": "openai-siwc"}, "lan": {"kind": "static-bearer"}},
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
               "model": model, "path": "/v1/messages", "status": 200, "usage": {"output_tokens": output}}
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
        audit_row(-21, account=None, provider="work", model="gpt-6.2-sol", path="/openai/v1/responses",
                  usage={"input_tokens": 125_000, "output_tokens": 100_000, "cache_read_input_tokens": 20_000,
                         "cache_creation_input_tokens": 10_000}),
        audit_row(-20, util={"7d": .35}),
        audit_row(-17, **fable, util={"7d": .45, "7d_oi": .20, "learned": .40}),
        audit_row(-14, model=fable["model"], output=1_000_000, util={"7d": .10, "7d_oi": .05, "learned": .10}),
        audit_row(-11, **fable, util={"7d": .20, "7d_oi": .15, "learned": .20}),
        audit_row(-8, util={"7d": .90}, ratelimit={"unified-7d-reset": str(at(158))}),
        audit_row(-5, util={"7d": .95}),
        audit_row(-4, account="b", util={"7d": .03}),
    ])
    (state / "usage_history.json").write_text(json.dumps([
        {"ts": at(h), "accounts": {"a": [u, at(-10)]}}
        for h, u in ((-30, .10), (-28, .15), (-26, .20), (-22, .85))
    ]))
    (state / "provider_quota_history.json").write_text(json.dumps({"version": 1, "providers": {"work": [
        {"limit": "codex", "minutes": 10080,
         "points": [[at(h), u, at(110), "usage"] for h, u in ((-25, 10), (-20, 15), (-12, 20))]},
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
          and general["latest"] == {"at": at(-5), "used_percent": 95, "reset": at(158)})
    check("outside rise is counted separately from the rate estimates",
          general["outside_intervals"] == 1 and math.isclose(general["outside_share"], 1 / 7))
    check("general weekly fit distinguishes Fable and other costs",
          general["fable_fit"] == {"n": 6, "a": 2.0, "b": 1.0})
    check("latest reading gives cost remaining and time at the last full 24 h gateway pace",
          general["remaining"]["percent"] == 5 and general["remaining"]["usd"] == 3.75
          and math.isclose(general["remaining"]["hours"], 24 * 3.75 / 70))
    fable_week = rows["a", "7d_oi"]
    check("Fable weekly and an additional Fable claim include only their reporting family",
          fable_week["intervals"] == 2 and fable_week["cost_intervals"] == 2
          and fable_week["usd_per_percent"]["median"] == .5
          and fable_week["tokens_per_percent"]["output_tokens"] == 12_500
          and fable_week["remaining"]["usd"] == 42.5
          and rows["a", "learned"]["usd_per_percent"]["median"] == .5)
    codex = rows["work", "codex"]
    check("OpenAI weekly uses provider readings and normalized audit token types",
          codex["kind"] == "openai" and codex["minutes"] == 10080 and codex["outside_share"] == .5
          and codex["usd_per_percent"]["q1"] is None and math.isclose(codex["usd_per_percent"]["median"], .2554)
          and codex["tokens_per_percent"] == {"input_tokens": 25_000, "output_tokens": 20_000,
                                            "cache_read_input_tokens": 4_000, "cache_creation_input_tokens": 2_000}
          and not rows["work", "another"]["traffic_attributed"])
    check("sparse account shows its latest utilization and plain insufficient estimates",
          rows["b", "7d"]["usd_per_percent"] is None and rows["b", "7d"]["remaining"]["hours"] is None
          and rows["b", "7d"]["fable_fit"] == {"n": 0, "a": None, "b": None})

    r = run("--days", "1", "--min-step", "25")
    check("human output uses flags and marks insufficient data",
          r.returncode == 0 and "over 1 days (minimum rise 25 points)" in r.stdout
          and "Cost per 1%: not enough data" in r.stdout and "work / codex, 10080 min" in r.stdout)
    r = run("--min-step", "nan")
    check("invalid thresholds are rejected", r.returncode == 1 and "--min-step" in r.stderr)
    check("the installed command leaves logs, state, configuration and release files alone",
          all(path.read_bytes() == data for path, data in before.items()) and not list(release.rglob("__pycache__")))
