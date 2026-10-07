"""Read-only retrieval over session event logs.

Only *fact* events are indexed.  Authority events (gates, approvals) are
structurally excluded, so a recall can resurface what happened but can never
resurrect a stale permission.  The scan is bounded: most recent files first,
capped file count, capped output size.
"""

from __future__ import annotations

import re
from pathlib import Path

from xenon.session.events import FACT_EVENT_TYPES, session_events_dir

_REFERENCE_SIGNAL = re.compile(
    r"(之前|上次|上一个|刚才|前面|早先|历史|那个任务|继续.{0,6}(?:任务|项目|做|搞)"
    r"|previously|earlier|last\s+time|before)",
    re.IGNORECASE,
)
_PATH_ANCHOR = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'，。；)）]+|[\w.-]+\.[A-Za-z0-9]{1,8}"
)
_LATIN_ANCHOR = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")


def needs_recall(text: str) -> bool:
    """True when the request explicitly points back at earlier work."""

    return bool(_REFERENCE_SIGNAL.search(text or ""))


def _bigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN.findall(text or ""):
        for index in range(len(run) - 1):
            grams.add(run[index : index + 2])
    return grams


def extract_anchors(text: str) -> tuple[set[str], set[str]]:
    """Return (path/token anchors, CJK bigrams) for scoring."""

    tokens = {match.lower() for match in _LATIN_ANCHOR.findall(text or "")}
    paths = {match.lower().replace("\\", "/") for match in _PATH_ANCHOR.findall(text or "")}
    tokens |= paths
    return tokens, _bigrams(text)


def _candidate_text(event: dict) -> str:
    kind = str(event.get("type") or "")
    if kind == "turn/user":
        return str(event.get("text") or "")
    if kind == "turn/assistant":
        return str(event.get("preview") or "")
    if kind == "tool/result":
        paths = " ".join(str(p) for p in event.get("paths") or [])
        return f"{event.get('tool', '')} {paths}".strip()
    if kind == "goal/opened":
        return str(event.get("objective") or "")
    return ""


def _score(tokens: set[str], grams: set[str], text: str) -> float:
    if not text:
        return 0.0
    lowered = text.lower().replace("\\", "/")
    score = 0.0
    for token in tokens:
        if token and token in lowered:
            score += 4.0 if ("/" in token or "." in token) else 1.0
    text_grams = _bigrams(text)
    score += len(grams & text_grams) * 1.0
    return score


def _format(event: dict) -> str:
    kind = str(event.get("type") or "")
    text = " ".join(_candidate_text(event).split())
    if len(text) > 160:
        text = text[:157] + "..."
    return f"- [{kind}] {text}"


def recall(
    text: str,
    *,
    directory: str | Path | None = None,
    limit: int = 3,
    char_budget: int = 400,
    max_files: int = 20,
    min_score: float = 3.0,
) -> str:
    """Return a bounded read-only recall block, or "" when nothing is relevant."""

    tokens, grams = extract_anchors(text)
    if not tokens and not grams:
        return ""
    root = Path(directory) if directory else session_events_dir()
    if not root.is_dir():
        return ""

    try:
        files = sorted(
            root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:max_files]
    except OSError:
        return ""

    scored: list[tuple[float, float, str]] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        import json

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") not in FACT_EVENT_TYPES:
                continue
            candidate = _candidate_text(event)
            score = _score(tokens, grams, candidate)
            if score >= min_score:
                scored.append((score, float(event.get("at") or 0.0), _format(event)))

    if not scored:
        return ""
    scored.sort(key=lambda item: (-item[0], -item[1]))
    lines: list[str] = []
    used = 0
    for _score_value, _at, line in scored:
        if line in lines:
            continue
        if used + len(line) + 1 > char_budget and lines:
            break
        lines.append(line)
        used += len(line) + 1
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return "## 历史检索（只读参考，不构成授权）\n" + "\n".join(lines)
