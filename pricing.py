"""API-equivalent prices shared by the gateway and offline usage analysis."""

import re
import sys


def token_count(value) -> int:
    return value if type(value) is int and 0 <= value < 10**15 else 0


def openai_cache_writes(details: dict) -> tuple[int, int]:
    """Total write tokens and the subset explicitly reported as 12-hour writes."""
    total = token_count(details.get("cache_write_tokens"))
    split = details.get("cache_write_tokens_details")
    split = split if isinstance(split, dict) else {}
    short = max(token_count(split.get("ttl_30m_tokens")), token_count(details.get("cache_write_30m_tokens")),
                token_count(details.get("cache_write_tokens_30m")))
    long = max(token_count(split.get("ttl_12h_tokens")), token_count(details.get("cache_write_12h_tokens")),
               token_count(details.get("cache_write_tokens_12h")))
    return max(total, short + long), long


# API list prices per MTok, [input, output] or [input, output, cached input], matched
# by the longest pattern found in the model id. Without a cached-input price a cache
# read costs 0.1x input. Claude prices read 2026-11-27 from
# https://platform.claude.com/docs/en/about-claude/pricing.md. Cost columns are
# *API-equivalent* dollars: what this usage would cost pay-per-token.
BUILTIN_PRICES = {
    "fable": [10.0, 50.0], "mythos": [10.0, 50.0],
    "opus": [5.0, 25.0],
    "sonnet": [3.0, 15.0],
    "haiku": [1.0, 5.0],
    "fable-5-2": [8.0, 40.0, 0.20], "mythos-5-2": [8.0, 40.0, 0.20],
    "fable-5-1": [10.0, 50.0, 0.25], "mythos-5-1": [10.0, 50.0, 0.25],
    "fable-5": [10.0, 50.0, 1.0], "mythos-5": [10.0, 50.0, 1.0],
    "opus-5-5": [4.0, 20.0, 0.20], "opus-5": [5.0, 25.0, 0.50],
    "opus-4-8": [5.0, 25.0, 0.50], "opus-4-7": [5.0, 25.0, 0.50],
    "sonnet-5-5": [2.0, 10.0, 0.10], "sonnet-5": [2.0, 10.0, 0.20], "sonnet-4-6": [3.0, 15.0, 0.30],
    "haiku-5-5": [0.10, 0.50, 0.01], "haiku-4-5": [1.0, 5.0, 0.10],
    # OpenAI Standard short-context (up to 272K) rates from
    # https://developers.openai.com/api/docs/pricing. The 2026-11-27 page confirms
    # gpt-6.2-astra, gpt-6.2-sol, gpt-6-astra and gpt-6.1-sol in Standard, and
    # lists gpt-6.1-terra and gpt-6-luna under Flex. Other Standard values retain
    # the 2026-11-19 reading, including gpt-5.6-sol's promotion through Nov 30.
    "gpt-6.2-astra": [8.0, 40.0, 0.4], "gpt-6.2-sol": [2.0, 10.0, 0.1],
    "gpt-6.1-sol": [2.0, 10.0, 0.1], "gpt-6.1-terra": [1.0, 5.0, 0.05],
    "gpt-6-astra": [10.0, 50.0, 1.0], "gpt-6-sol": [2.0, 10.0, 0.2], "gpt-6-luna": [0.1, 0.5, 0.01],
    "gpt-5.6-sol": [4.0, 20.0, 0.4], "gpt-5.6-terra": [2.0, 12.0, 0.2], "gpt-5.6-luna": [0.2, 1.2, 0.02],
    "gpt-5.5": [5.0, 30.0, 0.5], "gpt-5.5-pro": [30.0, 180.0],
    "gpt-5.4": [2.5, 15.0, 0.25], "gpt-5.4-mini": [0.75, 4.5, 0.075], "gpt-5.4-nano": [0.2, 1.25, 0.02],
    "gpt-5.4-pro": [30.0, 180.0],
    "gpt-5.3-codex": [1.75, 14.0, 0.175],
}


def load_prices(extra) -> dict:
    """The built-in table with config.json's prices_per_mtok laid over it entry by
    entry, so an install whose config holds an older copy still gets new entries. A
    malformed entry is reported and skipped: costs are analytics, never a reason to
    stop the proxy."""
    table = dict(BUILTIN_PRICES)
    for pattern, price in (extra.items() if isinstance(extra, dict) else []):
        if (isinstance(pattern, str) and pattern and isinstance(price, list) and len(price) in (2, 3)
                and all(type(v) in (int, float) and 0 <= v < 1e6 for v in price)):
            table[pattern] = price
        else:
            print(f"warning: prices_per_mtok entry {pattern!r} ignored: it needs [input, output] or "
                  "[input, output, cached input], in $ per MTok", file=sys.stderr)
    if not isinstance(extra, dict):
        print("warning: prices_per_mtok ignored: it must map model patterns to prices", file=sys.stderr)
    return table


DEFAULT_PRICE = [5.0, 25.0]
FAST_PRICE_MODELS = ("fable-5-2", "mythos-5-2", "opus-5", "opus-4-8")



def price_pattern_matches(pattern: str, model: str) -> bool:
    # A date starting with 2 is not the -2 revision of a model.
    return pattern in model and (not pattern[-1].isdigit()
                                 or re.search(re.escape(pattern) + r"(?![0-9])", model) is not None)


def price_for(model: str | None, prices: dict, default=DEFAULT_PRICE) -> list[float] | None:
    """The entry of the longest pattern found in the model id, so gpt-5.4-mini is not
    priced as gpt-5.4 whatever the order of the table."""
    if not isinstance(model, str):
        return default
    pattern = max((pat for pat in prices if price_pattern_matches(pat, model)), key=len, default=None)
    return prices[pattern] if pattern else default


def api_cost(model: str | None, usage: dict, provider=None, *, prices=None) -> float:
    """API-equivalent $ for one request, including prompt length, speed and cache TTL.
    An unknown provider model without a price entry costs zero."""
    if prices is None:
        prices = BUILTIN_PRICES
    price = price_for(model, prices, None if provider else DEFAULT_PRICE)
    if price is None:
        return 0.0
    p_in, p_out, *cached = price
    input_tokens = usage.get("input_tokens", 0)
    read_tokens = usage.get("cache_read_input_tokens", 0)
    write_tokens = usage.get("cache_creation_input_tokens", 0)
    long_writes = 0
    if isinstance(split := usage.get("cache_creation"), dict):
        long_writes = token_count(split.get("ephemeral_1h_input_tokens"))
        write_tokens = max(write_tokens, long_writes + token_count(split.get("ephemeral_5m_input_tokens")))
    elif isinstance(details := usage.get("input_tokens_details"), dict):
        total, long_writes = openai_cache_writes(details)
        write_tokens = max(write_tokens, total)
    scale = 1
    if isinstance(model, str):
        if price_pattern_matches("haiku-5-5", model) and input_tokens + read_tokens + write_tokens > 100_000:
            scale = 5
        elif usage.get("speed") == "fast" and any(price_pattern_matches(pat, model) for pat in FAST_PRICE_MODELS):
            scale = 2
    return scale * (
        input_tokens * p_in
        + usage.get("output_tokens", 0) * p_out
        + read_tokens * (cached[0] if cached else p_in * 0.1)
        + ((write_tokens - long_writes) * 1.25 + long_writes * 2) * p_in
    ) / 1_000_000
