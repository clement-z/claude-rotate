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
    "opus-4-6": [5.0, 25.0, 0.50], "opus-4-5": [5.0, 25.0, 0.50],
    "opus-4-1": [15.0, 75.0, 1.50], "opus-4": [15.0, 75.0, 1.50],
    "sonnet-5-5": [2.0, 10.0, 0.10], "sonnet-5": [2.0, 10.0, 0.20], "sonnet-4-6": [3.0, 15.0, 0.30],
    "sonnet-4-5": [3.0, 15.0, 0.30], "sonnet-4": [3.0, 15.0, 0.30],
    "haiku-5-5": [0.10, 0.50, 0.01], "haiku-4-5": [1.0, 5.0, 0.10],
    # OpenAI Standard short-context token rates from the expanded tables in
    # https://developers.openai.com/api/docs/pricing, read 2026-11-27.
    "gpt-6.2-astra": [8.0, 40.0, 0.4], "gpt-6.2-sol": [2.0, 10.0, 0.1],
    "gpt-6.1-sol": [2.0, 10.0, 0.1], "gpt-6.1-terra": [1.0, 5.0, 0.05],
    "gpt-6-astra": [10.0, 50.0, 1.0], "gpt-6-sol": [2.0, 10.0, 0.2], "gpt-6-luna": [0.1, 0.5, 0.01],
    "gpt-5.6-sol": [4.0, 20.0, 0.4], "gpt-5.6-terra": [2.0, 12.0, 0.2], "gpt-5.6-luna": [0.2, 1.2, 0.02],
    "gpt-5.5": [5.0, 30.0, 0.5], "gpt-5.5-pro": [30.0, 180.0],
    "gpt-5.4": [2.5, 15.0, 0.25], "gpt-5.4-mini": [0.75, 4.5, 0.075], "gpt-5.4-nano": [0.2, 1.25, 0.02],
    "gpt-5.4-pro": [30.0, 180.0],
    "gpt-5.3-codex": [1.75, 14.0, 0.175],
    "gpt-5.2": [1.75, 14.0, 0.175], "gpt-5.2-pro": [21.0, 168.0],
    "gpt-5.1": [1.25, 10.0, 0.125], "gpt-5": [1.25, 10.0, 0.125],
    "gpt-5-mini": [0.25, 2.0, 0.025], "gpt-5-nano": [0.05, 0.4, 0.005],
    "gpt-5-pro": [15.0, 120.0], "gpt-5-search-api": [1.25, 10.0, 0.125],
    "chat-latest": [5.0, 30.0, 0.50],
    "gpt-4.1": [2.0, 8.0, 0.50], "gpt-4.1-mini": [0.40, 1.60, 0.10],
    "ft:gpt-4.1": [3.0, 12.0, 0.75], "ft:gpt-4.1-mini": [0.80, 3.20, 0.20],
    "gpt-4o": [2.50, 10.0, 1.25], "gpt-4o-mini": [0.15, 0.60, 0.075],
    "ft:gpt-4o": [3.75, 15.0, 1.875], "ft:gpt-4o-mini": [0.30, 1.20, 0.15],
    "o3": [2.0, 8.0, 0.50], "o3-pro": [20.0, 80.0],
    "gpt-6-cyber": [20.0, 100.0, 2.0], "gpt-5.6-cyber": [12.5, 75.0, 1.25],
    "gpt-5.5-cyber-preview": [12.5, 75.0, 1.25],
    "gpt-6-rosalind": [10.0, 50.0, 1.0], "gpt-rosalind-research": [5.0, 25.0, 0.50],
    "gpt-rosalind-discovery": [5.0, 25.0, 0.50],
    "gpt-6.2-astra-law": [8.0, 40.0, 0.40], "gpt-6-astra-law": [10.0, 50.0, 1.0],
    "gpt-6.2-astra-healthcare": [8.0, 40.0, 0.40], "gpt-6.2-sol-healthcare": [2.0, 10.0, 0.10],
    "gpt-6-astra-healthcare": [10.0, 50.0, 1.0], "gpt-6.1-sol-healthcare": [2.0, 10.0, 0.10],
    "text-embedding-3-small": [0.02, 0.0], "text-embedding-3-large": [0.13, 0.0],
    "text-embedding-ada-002": [0.10, 0.0], "omni-moderation-latest": [0.0, 0.0],
    # Text-token rates for media models. Media-specific units are not metered.
    "gpt-image-2.6-sunburst": [5.0, 0.0, 1.25], "gpt-image-2.6-flare": [5.0, 0.0, 1.25],
    "gpt-image-2.5-sunburst": [5.0, 0.0, 1.25], "gpt-image-2.5-flare": [5.0, 0.0, 1.25],
    "gpt-image-2": [5.0, 0.0, 1.25], "gpt-image-1.5": [5.0, 10.0, 1.25],
    "gpt-image-1-mini": [2.0, 0.0, 0.20], "chatgpt-image-latest": [5.0, 10.0, 1.25],
    "gpt-realtime-2.1": [4.0, 24.0, 0.40], "gpt-realtime-2": [4.0, 24.0, 0.40],
    "gpt-realtime-2.1-mini": [0.60, 2.40, 0.06], "gpt-realtime-mini": [0.60, 2.40, 0.06],
    "gpt-realtime-1.5": [4.0, 16.0, 0.40], "gpt-realtime": [4.0, 16.0, 0.40],
    "gpt-audio-1.5": [2.50, 10.0], "gpt-audio": [2.50, 10.0], "gpt-audio-mini": [0.60, 2.40],
    "gpt-4o-transcribe": [2.50, 10.0], "gpt-4o-transcribe-diarize": [2.50, 10.0],
    "gpt-4o-mini-transcribe": [1.25, 5.0], "gpt-4o-mini-tts": [0.60, 0.0],
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
    pattern = max((pat for pat in prices if (
        price_pattern_matches(pat, model) if pat in BUILTIN_PRICES else pat in model
    )), key=len, default=None)
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
    long_writes, long_rate = 0, 2.0
    if isinstance(split := usage.get("cache_creation"), dict):
        long_writes = token_count(split.get("ephemeral_1h_input_tokens"))
        write_tokens = max(write_tokens, long_writes + token_count(split.get("ephemeral_5m_input_tokens")))
    else:
        details = usage.get("input_tokens_details")
        long_writes = token_count(usage.get("cache_write_12h_input_tokens"))
        if isinstance(details, dict):
            total, reported_long = openai_cache_writes(details)
            long_writes = max(long_writes, reported_long)
            write_tokens = max(write_tokens, total)
        write_tokens = max(write_tokens, long_writes)
        long_rate = 2.5
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
        + ((write_tokens - long_writes) * 1.25 + long_writes * long_rate) * p_in
    ) / 1_000_000
