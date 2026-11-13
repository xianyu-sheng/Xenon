"""URL slug helpers used by the acceptance test (no unit tests yet)."""

from __future__ import annotations

import re
import unicodedata

# Characters that do not decompose under NFKD and would otherwise be dropped
# by the ASCII encode step. Fold them to their closest ASCII spelling first.
_TRANSLITERATION = {
    "\u00df": "ss",  # sharp s
    "\u1e9e": "SS",  # capital sharp s
    "\u00e6": "ae",
    "\u00c6": "AE",
    "\u0153": "oe",
    "\u0152": "OE",
    "\u00f8": "o",
    "\u00d8": "O",
    "\u0111": "d",
    "\u0110": "D",
    "\u0142": "l",
    "\u0141": "L",
    "\u00fe": "th",
    "\u00de": "Th",
    "\u00f0": "d",
    "\u00d0": "D",
    "\u0131": "i",
}


def slugify(text: str) -> str:
    """Turn a title into a lowercase ASCII slug separated by single dashes.

    Rules:
    - strip surrounding whitespace
    - lowercase everything
    - remove accents ("Caf\u00e9" -> "cafe")
    - any run of non [a-z0-9] characters becomes one dash
    - leading and trailing dashes are removed
    - an empty result is returned as an empty string
    """

    folded = (text or "").strip().casefold()
    for source, target in _TRANSLITERATION.items():
        folded = folded.replace(source.casefold(), target.casefold())
    normalized = unicodedata.normalize("NFKD", folded)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text)
    return slug.strip("-")


def unique_slugs(titles: list[str]) -> list[str]:
    """Slugify titles, adding -2, -3 ... when two titles collide."""

    seen: dict[str, int] = {}
    result: list[str] = []
    for title in titles:
        base = slugify(title) or "untitled"
        count = seen.get(base, 0) + 1
        seen[base] = count
        result.append(base if count == 1 else f"{base}-{count}")
    return result
