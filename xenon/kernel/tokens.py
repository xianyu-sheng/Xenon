"""Exact token counting for the context budget.

The kernel used to size its working set with ``len(text) / 2``.  That is wrong
in both directions and it is wrong differently per language: CJK text costs
roughly one token per character while English costs about one per four.  A
Chinese task therefore silently overflowed the window while an English one
wasted half of it, and the 70%-of-window compaction trigger could never fire
at the right moment because the number it read was fiction.

``tiktoken`` is the real counter.  It is a dependency, not a guarantee: a
model whose tokenizer is not in the table (or an install without the package)
must still run, so a documented heuristic backs it up.  The heuristic is
better than ``chars/2`` but it is still an estimate, and callers that care
about the difference can ask which one answered.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass

__all__ = ["count_tokens", "estimate", "reset_encoder_cache"]

logger = logging.getLogger(__name__)

#: Encoding used when a model's own tokenizer is unavailable.  cl100k_base is
#: close enough to every modern OpenAI/Anthropic/DeepSeek tokenizer for a
#: budget decision; it is never used to *report* usage, only to size a view.
_FALLBACK_ENCODING = "cl100k_base"

_CJK = re.compile(
    r"[一-鿿㐀-䶿豈-﫿"  # 中日韩统一表意
    r"぀-ヿ"  # 日文假名
    r"가-힯]"  # 韩文
)


@dataclass(frozen=True, slots=True)
class Count:
    tokens: int
    exact: bool


@functools.lru_cache(maxsize=8)
def _encoder(name: str):
    try:
        import tiktoken

        return tiktoken.get_encoding(name)
    except Exception:  # noqa: BLE001 — no tokenizer is a supported state
        logger.debug("tokenizer %r unavailable; using heuristic", name)
        return None


def reset_encoder_cache() -> None:
    _encoder.cache_clear()


def _heuristic(text: str) -> int:
    """A documented estimate, used only when no tokenizer answered.

    CJK is near 1 token per character; latin text is near 1 per 4 characters
    with a floor for symbol-dense code and JSON.
    """

    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return cjk + max(1, rest // 4)


def estimate(text: str, *, encoding: str = "") -> Count:
    """Count tokens, reporting whether the number is exact."""

    if not text:
        return Count(0, True)
    encoder = _encoder(encoding or _FALLBACK_ENCODING)
    if encoder is not None:
        try:
            return Count(len(encoder.encode(text, disallowed_special=())), True)
        except Exception:  # noqa: BLE001 — fall through to the estimate
            logger.debug("tokenizer failed on input; using heuristic")
    return Count(_heuristic(text), False)


def count_tokens(text: str, *, encoding: str = "") -> int:
    """Just the number, for callers that do not need the provenance."""

    return estimate(text, encoding=encoding).tokens
