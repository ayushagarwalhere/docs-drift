"""Central model ID and USD per million tokens; unknown pricing fails closed."""

from dataclasses import dataclass

from docs_drift.llm.types import CallUsage, ModelRequest

DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
MAX_OUTPUT_TOKENS = 1024


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


# Verified 2026-10-18. Update from https://openrouter.ai/anthropic/claude-sonnet-4.6
# and https://platform.claude.com/docs/en/about-claude/pricing before adding models.
PRICES = {DEFAULT_MODEL: Price(3.0, 15.0, 0.30, 3.75)}


def price_for(model: str) -> Price:
    if model not in PRICES:
        raise ValueError(f"No budget pricing for {model}. Add verified rates in llm/pricing.py before live calls.")
    return PRICES[model]


def estimate_request(request: ModelRequest) -> float:
    price = price_for(request.model)
    # UTF-8 bytes bound token count conservatively; reserve worst-case cache-write and output costs.
    byte_count = len((request.system + request.context + request.claim).encode("utf-8")) + 2048
    return (byte_count * max(price.input, price.cache_write) + MAX_OUTPUT_TOKENS * price.output) / 1_000_000


def usage_cost(usage: CallUsage, model: str) -> float:
    price = price_for(model)
    uncached = max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
    return (
        uncached * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / 1_000_000


def format_usage(usage: dict[str, float]) -> str:
    inputs = usage.get("input_tokens", 0)
    cached = usage.get("cache_read_tokens", 0) / inputs if inputs else 0
    return (
        f"LLM: {int(usage.get('claims', 0))} claims, {inputs / 1000:.1f}k input tokens "
        f"({cached:.0%} cached), ${usage.get('cost', 0):.4f}, {usage.get('seconds', 0):.1f} s"
    )
