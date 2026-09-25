"""Token prices and cost accounting.

Prices are USD per million tokens from platform.claude.com/docs/en/about-claude/pricing
(checked 2026-09-23): base input, output, 5-minute cache write, cache read.
The Batch API halves every price.
"""

from __future__ import annotations

from catchrate.agent.llm import Usage

PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.00, 50.00, 12.50, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
}


def cost_usd(
    model: str,
    usage: Usage,
    *,
    batch: bool = False,
    prices: dict[str, tuple[float, float, float, float]] | None = None,
) -> float | None:
    """Dollar cost of `usage` on `model`, or None when the model has no known price."""
    table = PRICES if prices is None else prices
    if model not in table:
        return None
    inp, out, write, read = table[model]
    total = (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_write_tokens * write
        + usage.cache_read_tokens * read
    ) / 1_000_000
    return total / 2 if batch else total
