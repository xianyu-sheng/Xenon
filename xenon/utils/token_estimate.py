"""Conservative, provider-neutral token estimates for diagnostics."""

from __future__ import annotations


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x2E80 <= codepoint <= 0x2FFF
        or 0x3000 <= codepoint <= 0x303F
        or 0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for diagnostics, not billing or context enforcement.

    CJK characters are counted as one token each; all other characters use a
    conservative four-characters-per-token ratio.
    """
    if not text:
        return 0
    cjk = sum(1 for character in text if _is_cjk(character))
    other = len(text) - cjk
    return max(1, cjk + (other + 3) // 4)
