"""The price table behind the cost columns: the built-in entries, an operator's
prices_per_mtok laid over them entry by entry, the longest matching pattern, and
the optional cached-input price. Runs against its own temporary config.

  python3 tests/test_prices.py
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


tmp = Path(tempfile.mkdtemp())
(tmp / "a.token").write_text("fake-claude-a\n")
(tmp / "config.json").write_text(json.dumps({
    "accounts": [{"name": "a", "token_file": "a.token"}], "devices": {"desk": "dev-desk-fake-key"},
    "refresh_idle_s": 0}))
for _var in list(os.environ):
    if _var.startswith("CLAUDE_ROTATE_"):
        del os.environ[_var]
os.environ.update(CLAUDE_ROTATE_CONFIG=str(tmp / "config.json"), CLAUDE_ROTATE_STATE=str(tmp / "state.json"),
                  CLAUDE_ROTATE_AUDIT=str(tmp / "audit.jsonl"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R  # noqa: E402

MTOK = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}


def with_config(prices):
    """PRICES as a config holding this prices_per_mtok would give, and the warnings."""
    R.CFG["prices_per_mtok"] = prices
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        R.PRICES = R.load_prices()
    return stderr.getvalue()


def cost(model, provider=None):
    return round(R.api_cost(model, MTOK, provider), 6)


with_config({})
openai = {"gpt-6.2-astra": [8.0, 40.0, 0.4], "gpt-6.2-sol": [2.0, 10.0, 0.1], "gpt-6.1-sol": [2.0, 10.0, 0.1],
          "gpt-6.1-terra": [1.0, 5.0, 0.05], "gpt-6-astra": [10.0, 50.0, 1.0], "gpt-6-sol": [2.0, 10.0, 0.2],
          "gpt-6-luna": [0.1, 0.5, 0.01], "gpt-5.6-sol": [4.0, 20.0, 0.4], "gpt-5.6-terra": [2.0, 12.0, 0.2],
          "gpt-5.6-luna": [0.2, 1.2, 0.02], "gpt-5.5": [5.0, 30.0, 0.5], "gpt-5.4": [2.5, 15.0, 0.25],
          "gpt-5.4-mini": [0.75, 4.5, 0.075], "gpt-5.3-codex": [1.75, 14.0, 0.175]}
check("every OpenAI model on the list resolves to its own Standard short-context row",
      all(R.price_for(m, None) == p for m, p in openai.items()))
claude = {"claude-fable-5-2": [8.0, 40.0, 0.20], "claude-mythos-5-2": [8.0, 40.0, 0.20],
          "claude-fable-5-1": [10.0, 50.0, 0.25], "claude-mythos-5-1": [10.0, 50.0, 0.25],
          "claude-fable-5": [10.0, 50.0, 1.0], "claude-mythos-5": [10.0, 50.0, 1.0],
          "claude-mythos-1": [10.0, 50.0], "claude-opus-5-5": [4.0, 20.0, 0.20],
          "claude-opus-5": [5.0, 25.0, 0.50], "claude-opus-4-8": [5.0, 25.0, 0.50],
          "claude-opus-4-7": [5.0, 25.0, 0.50], "claude-opus-3": [5.0, 25.0],
          "claude-sonnet-5-5": [2.0, 10.0, 0.10], "claude-sonnet-5": [2.0, 10.0, 0.20],
          "claude-sonnet-4-6": [3.0, 15.0, 0.30], "claude-sonnet-4": [3.0, 15.0],
          "claude-haiku-5-5": [0.1, 0.5, 0.01], "claude-haiku-4-5": [1.0, 5.0, 0.10],
          "claude-haiku-3": [1.0, 5.0]}
check("Claude models and their dated IDs select the same pricing row",
      all(R.price_for(m) == R.price_for(m + "-20261127") == p for m, p in claude.items()))
check("an unknown Claude model takes the default price, an unknown provider model none",
      R.price_for("claude-x") == R.DEFAULT_PRICE and R.price_for("gpt-6-cyber", None) is None
      and cost("gpt-6-cyber", "openai") == 0.0 and cost("some-model", "lan") == 0.0)
check("the longest pattern wins: -mini, -nano and -pro are not priced as gpt-5.4",
      R.price_for("gpt-5.4-mini") == [0.75, 4.5, 0.075] and R.price_for("gpt-5.4-nano") == [0.2, 1.25, 0.02]
      and R.price_for("gpt-5.4-pro") == [30.0, 180.0] and R.price_for("gpt-5.5-pro") == [30.0, 180.0]
      and R.price_for("gpt-5.4") == [2.5, 15.0, 0.25])
check("a dated or suffixed id takes its family's row",
      R.price_for("gpt-6.2-sol-2026-10-01") == [2.0, 10.0, 0.1] and R.price_for("gpt-6.2-astra-law") == [8.0, 40.0, 0.4])
check("a cached-input price replaces the 0.1x rule; cache writes stay at 1.25x input",
      cost("gpt-6.2-sol", "openai") == 2.0 + 10.0 + 0.1 + 2.5 and cost("gpt-6-sol", "openai") == 2.0 + 10.0 + 0.2 + 2.5)
check("a two-element entry keeps the 0.1x cache read", cost("claude-opus-3") == 5.0 + 25.0 + 0.5 + 6.25)

split = {"ephemeral_5m_input_tokens": 250_000, "ephemeral_1h_input_tokens": 750_000}
mixed = MTOK | {"cache_creation": split}
check("Claude prices mixed cache TTLs, with unsplit writes at the five-minute rate",
      round(R.api_cost("claude-opus-5-5", mixed), 6) == 31.45
      and round(R.api_cost("claude-opus-5-5", mixed | {"cache_creation": {"ephemeral_1h_input_tokens": 750_000}}),
                6) == 31.45
      and round(R.api_cost("claude-opus-5-5", MTOK), 6) == 29.20)
check("Fast mode doubles supported models' token and cache rates",
      all(R.api_cost(m, mixed | {"speed": "fast"}) == 2 * R.api_cost(m, mixed)
          for m in ("claude-fable-5-2", "claude-mythos-5-2", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"))
      and R.api_cost("claude-opus-4-7", mixed | {"speed": "fast"}) == R.api_cost("claude-opus-4-7", mixed))
short = {"input_tokens": 50_000, "output_tokens": 10_000,
         "cache_read_input_tokens": 30_000, "cache_creation_input_tokens": 20_000}
check("Haiku 5.5 uses the higher tier only above 100K prompt tokens, including caches",
      round(R.api_cost("claude-haiku-5-5", short), 8) == .0128
      and round(R.api_cost("claude-haiku-5-5-20261127", short | {"input_tokens": 50_001}), 8) == .0640005)

raw_input = {f"extra_{i}": i for i in range(35)}
raw_input.update(cached_tokens=1_000_000, cache_write_tokens=2_000_000,
                 cache_write_tokens_details={"ttl_30m_tokens": 1_000_000, "ttl_12h_tokens": 1_000_000})
openai_usage = R.providers.usage_of({"usage": {"input_tokens": 4_000_000, "output_tokens": 1_000_000,
                                               "input_tokens_details": raw_input,
                                               "output_tokens_details": {"reasoning_tokens": 42, "tag": "x" * 500}}})
check("OpenAI keeps bounded raw details and removes cache writes from uncached input",
      len(openai_usage["input_tokens_details"]) == 32
      and openai_usage["input_tokens_details"]["cache_write_tokens_details"] == raw_input["cache_write_tokens_details"]
      and openai_usage["output_tokens_details"] == {"reasoning_tokens": 42, "tag": "x" * 200}
      and openai_usage["input_tokens"] == 1_000_000 and openai_usage["cache_creation_input_tokens"] == 2_000_000)
check("OpenAI prices reported 12-hour writes higher and uses 30 minutes for an unsplit count",
      round(R.api_cost("gpt-6.2-sol", openai_usage, "openai"), 6) == 18.6
      and round(R.api_cost("gpt-6.2-sol", openai_usage | {"input_tokens_details": {}}, "openai"), 6) == 17.1)

# The Claude-only copy that installs seeded from config.example.json still hold.
old_copy = {"fable": [10.0, 50.0], "mythos": [10.0, 50.0], "opus": [5.0, 25.0], "sonnet": [3.0, 15.0],
            "haiku": [1.0, 5.0]}
warnings = with_config(old_copy)
check("the old Claude-only copy still gets the OpenAI entries and more specific Claude prices",
      warnings == "" and R.price_for("gpt-6.2-sol", None) == [2.0, 10.0, 0.1]
      and all(R.price_for(m) == p for m, p in claude.items()))
with_config({"opus": [6.0, 30.0]})
check("an operator family override leaves more specific rows in effect",
      R.price_for("claude-opus-3") == [6.0, 30.0] and R.price_for("claude-opus-5-5") == [4.0, 20.0, 0.20]
      and R.price_for("gpt-5.4", None) == [2.5, 15.0, 0.25])
with_config({"gpt-6.2-sol": [3.0, 12.0, 0.3], "gpt-7-sol": [1.0, 2.0, 0.05], "gpt-5.4-mini-fast": [1.5, 9.0]})
check("an operator entry adds a model, overrides a built-in one, and the longest match still wins",
      R.price_for("gpt-7-sol", None) == [1.0, 2.0, 0.05] and R.price_for("gpt-6.2-sol", None) == [3.0, 12.0, 0.3]
      and R.price_for("gpt-5.4-mini-fast") == [1.5, 9.0] and R.price_for("gpt-5.4-mini") == [0.75, 4.5, 0.075])
warnings = with_config({"x": [1.0], "y": "cheap", "z": [1, 2, 3, 4], "w": [-1, 2], "": [1, 2], "opus": [True, 1],
                        "ok": [1, 2]})
check("malformed entries are skipped with a warning each; the built-in entry stays",
      warnings.count("warning: prices_per_mtok entry") == 6 and R.price_for("claude-opus-5-5") == [4.0, 20.0, 0.20]
      and R.price_for("ok") == [1, 2] and "x" not in R.PRICES)
warnings = with_config(["opus", 1])
check("a prices_per_mtok that is not an object is ignored with a warning",
      "prices_per_mtok ignored" in warnings and R.PRICES == R.BUILTIN_PRICES)


async def capture_claude_usage():
    stream = (
        f'data: {json.dumps({"type": "message_start", "message": {"usage": mixed | {"speed": "fast"}}})}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":2000000}}\n\n'
    ).encode()

    class SplitStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(stream), 17):
                yield stream[offset:offset + 17]

    def upstream(req):
        if json.loads(req.content)["stream"]:
            return httpx.Response(200, stream=SplitStream(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"content": [], "usage": mixed | {"speed": "fast"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://stand-in") as R.client:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app), base_url="http://proxy") as proxy:
            for path in ("/v1/messages", "/v1/chat/completions"):
                for streaming in (False, True):
                    r = await proxy.post(path, headers={"authorization": "Bearer dev-desk-fake-key"},
                                         json={"model": "claude-opus-5-5", "stream": streaming,
                                               "messages": [{"role": "user", "content": "hi"}]})
                    assert r.status_code == 200 and (not streaming or path != "/v1/messages" or r.content == stream)
    return [json.loads(line)["usage"] for line in R.AUDIT_PATH.read_text().splitlines()]


audited = asyncio.run(capture_claude_usage())
check("both Claude endpoints audit speed and TTL splits in buffered and split streaming answers",
      len(audited) == 4 and all(u["speed"] == "fast" and u["cache_creation"] == split for u in audited)
      and [u["output_tokens"] for u in audited] == [1_000_000, 2_000_000] * 2)

print(f"\nall {PASS} checks passed")
