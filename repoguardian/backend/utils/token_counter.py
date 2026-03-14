"""
Token counting and context budget management.

Uses tiktoken (cl100k_base encoding, compatible with Claude tokenisation)
to count tokens and enforce per-section budgets when assembling the
ContextPackage.

The budget enforcement strategy:
  1. Always include the full diff (never truncated)
  2. Fill sections in priority order until budget is exhausted
  3. Truncate lower-priority sections rather than drop them entirely
     (partial context is better than none for most agent tasks)
"""

from __future__ import annotations

import logging
from typing import TypeVar

logger = logging.getLogger(__name__)

# ── Encoder ────────────────────────────────────────────────────────────────────

_encoder = None


def _get_encoder():
    global _encoder
    if _encoder is None:
        try:
            import tiktoken
            _encoder = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            logger.warning("tiktoken not installed; using character-based estimation")
            _encoder = _FallbackEncoder()
    return _encoder


class _FallbackEncoder:
    """Rough character-based token estimator (4 chars ≈ 1 token)."""
    def encode(self, text: str) -> list[int]:
        return list(range(len(text) // 4))


# ── Public API ─────────────────────────────────────────────────────────────────


def count_tokens(text: str) -> int:
    """Return the number of tokens in `text`."""
    if not text:
        return 0
    enc = _get_encoder()
    return len(enc.encode(text))


def truncate_to_budget(text: str, max_tokens: int, suffix: str = "\n...[truncated]") -> str:
    """
    Truncate `text` to fit within `max_tokens`, appending `suffix` if cut.
    Preserves whole lines where possible.
    """
    if not text:
        return text

    current_tokens = count_tokens(text)
    if current_tokens <= max_tokens:
        return text

    # Binary search for the right character cutoff
    suffix_tokens = count_tokens(suffix)
    target = max_tokens - suffix_tokens
    if target <= 0:
        return suffix

    enc = _get_encoder()
    tokens = enc.encode(text)
    truncated_tokens = tokens[:target]

    # Decode back to string — tiktoken handles this cleanly
    try:
        truncated_text = enc.decode(truncated_tokens)
    except AttributeError:
        # Fallback encoder
        char_limit = target * 4
        truncated_text = text[:char_limit]

    # Snap to last newline to avoid cutting mid-line
    last_newline = truncated_text.rfind("\n")
    if last_newline > len(truncated_text) // 2:
        truncated_text = truncated_text[:last_newline]

    return truncated_text + suffix


class ContextBudgetManager:
    """
    Manages a total token budget across multiple context sections.

    Usage:
        mgr = ContextBudgetManager(total_budget=60_000)
        diff_text = mgr.allocate("diff", raw_diff, max_tokens=12_000, required=True)
        defs_text = mgr.allocate("definitions", defs, max_tokens=20_000)
        ...
        summary = mgr.summary()
    """

    def __init__(self, total_budget: int) -> None:
        self.total_budget = total_budget
        self.remaining = total_budget
        self._sections: dict[str, int] = {}  # section_name → tokens used

    def allocate(
        self,
        section_name: str,
        text: str,
        max_tokens: int,
        required: bool = False,
    ) -> str:
        """
        Fit `text` into the available budget, up to `max_tokens`.

        Args:
            section_name: Label for logging and summary.
            text:         The content to include.
            max_tokens:   Hard cap for this section.
            required:     If True, include even if over overall budget
                          (diff is the only required section).

        Returns:
            The (possibly truncated) text that fits the budget.
        """
        if not text:
            self._sections[section_name] = 0
            return text

        effective_max = min(max_tokens, self.remaining) if not required else max_tokens
        if effective_max <= 0 and not required:
            logger.debug("Skipping section '%s' — no budget remaining", section_name)
            self._sections[section_name] = 0
            return ""

        result = truncate_to_budget(text, effective_max)
        tokens_used = count_tokens(result)
        self._sections[section_name] = tokens_used
        self.remaining = max(0, self.remaining - tokens_used)

        if tokens_used < count_tokens(text):
            logger.debug(
                "Section '%s' truncated: %d → %d tokens",
                section_name, count_tokens(text), tokens_used,
            )

        return result

    def summary(self) -> dict[str, int]:
        """Return a summary of tokens used per section."""
        return {
            **self._sections,
            "_total_used": self.total_budget - self.remaining,
            "_total_budget": self.total_budget,
            "_remaining": self.remaining,
        }

    @property
    def tokens_used(self) -> int:
        return self.total_budget - self.remaining
