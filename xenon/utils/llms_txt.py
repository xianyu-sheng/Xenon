"""Deterministic parsing and selection helpers for the llms.txt proposal."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit


_LINK_RE = re.compile(r"^\s*[-*+]\s+\[([^\]]+)]\(([^)]+)\)(?:\s*:\s*(.*))?\s*$")
_TOKEN_RE = re.compile(r"[a-z0-9_.-]+|[\u3400-\u9fff]", re.IGNORECASE)
_KNOWN_FILES = frozenset(
    {
        "llms.txt",
        "llms-full.txt",
        "llms-ctx.txt",
        "llms-ctx-full.txt",
    }
)


@dataclass(frozen=True)
class LLMSTxtLink:
    title: str
    url: str
    description: str = ""
    section: str = ""
    optional: bool = False
    order: int = 0


@dataclass
class LLMSTxtDocument:
    title: str
    summary: str = ""
    details: str = ""
    links: list[LLMSTxtLink] = field(default_factory=list)


def parse_llms_txt(text: str, base_url: str) -> LLMSTxtDocument:
    """Parse the stable subset of the llms.txt Markdown proposal.

    The parser intentionally keeps ordinary Markdown as opaque details. Only
    the required H1, optional summary, H2 groups, and link-list records become
    structure used for deterministic selection.
    """

    lines = str(text or "").lstrip("\ufeff").splitlines()
    title = ""
    summary_lines: list[str] = []
    detail_lines: list[str] = []
    links: list[LLMSTxtLink] = []
    section = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            continue
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            continue
        match = _LINK_RE.match(line)
        if match and section:
            raw_url = match.group(2).strip().strip("<>")
            links.append(
                LLMSTxtLink(
                    title=match.group(1).strip(),
                    url=urljoin(base_url, raw_url),
                    description=(match.group(3) or "").strip(),
                    section=section,
                    optional=section.casefold() == "optional",
                    order=len(links),
                )
            )
            continue
        if not section and stripped.startswith(">"):
            summary_lines.append(stripped[1:].strip())
        elif not section and stripped:
            detail_lines.append(line.rstrip())

    if not title:
        raise ValueError("llms.txt 缺少必需的 H1 标题")
    return LLMSTxtDocument(
        title=title,
        summary="\n".join(line for line in summary_lines if line),
        details="\n".join(detail_lines).strip(),
        links=links,
    )


def select_llms_links(
    document: LLMSTxtDocument,
    query: str = "",
    *,
    max_pages: int = 4,
) -> list[LLMSTxtLink]:
    """Select relevant documentation links without an additional LLM call."""

    limit = max(0, min(int(max_pages), 8))
    if limit == 0:
        return []
    query_tokens = set(_tokens(query))

    scored: list[tuple[int, int, LLMSTxtLink]] = []
    for link in document.links:
        title_tokens = set(_tokens(link.title))
        desc_tokens = set(_tokens(link.description))
        section_tokens = set(_tokens(link.section))
        url_tokens = set(_tokens(link.url))
        score = (
            8 * len(query_tokens & title_tokens)
            + 4 * len(query_tokens & desc_tokens)
            + 2 * len(query_tokens & section_tokens)
            + len(query_tokens & url_tokens)
        )
        if not query_tokens:
            score = 1
        # Optional links remain usable for an explicit matching query but are
        # naturally placed after core documentation otherwise.
        optional_rank = 1 if link.optional and score == 0 else int(link.optional)
        scored.append((score, optional_rank, link))

    if query_tokens and any(score > 0 for score, _, _ in scored):
        scored = [row for row in scored if row[0] > 0]
    scored.sort(key=lambda row: (-row[0], row[1], row[2].order))
    return [link for _, _, link in scored[:limit]]


def llms_candidate_urls(url: str) -> list[str]:
    """Return bounded, deterministic llms.txt discovery candidates."""

    parsed = urlsplit(url)
    basename = parsed.path.rstrip("/").rsplit("/", 1)[-1].casefold()
    clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if basename in _KNOWN_FILES:
        return [clean]

    segments = [part for part in parsed.path.split("/") if part]
    if segments and "." in segments[-1]:
        segments = segments[:-1]

    directories: list[str] = []
    if segments:
        # The spec permits a subpath. Prefer the closest top-level docs root,
        # then the site root; this bounds discovery to at most four requests.
        directories.append("/" + segments[0])
    directories.append("")

    candidates: list[str] = []
    for filename in ("llms.txt", "llms-full.txt"):
        for directory in directories:
            path = f"{directory}/{filename}" if directory else f"/{filename}"
            candidate = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _tokens(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(value or "")]
