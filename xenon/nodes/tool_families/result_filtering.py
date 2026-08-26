"""Deterministic filtering for large realtime tool responses."""

from __future__ import annotations

import json
import re
from typing import Any

from xenon.engine.context import AgentContext

_CLOCK_LINE_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")
_DURATION_RE = re.compile(r"\d+\s*(?:时|小时).{0,4}\d*\s*分|\d+\s*分")
_TRAIN_CODE_RE = re.compile(r"[A-Z]{1,3}\d{1,6}", re.IGNORECASE)
_CHINESE_NUMBER = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _small_chinese_number(value: str) -> int | None:
    """Parse the small Chinese numerals used in clock expressions."""
    if value.isdigit():
        return int(value)
    if value in _CHINESE_NUMBER:
        return _CHINESE_NUMBER[value]
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CHINESE_NUMBER.get(left, 1) if left else 1
        ones = _CHINESE_NUMBER.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _clock_value(hour_text: str, minute_text: str, period: str) -> str | None:
    hour = _small_chinese_number(hour_text)
    if hour is None or not 0 <= hour <= 23:
        return None
    minute = 30 if minute_text == "半" else int(minute_text or 0)
    if not 0 <= minute <= 59:
        return None
    if period in {"下午", "傍晚", "晚上", "夜里", "夜间"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in {"凌晨", "早上", "上午"} and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _infer_time_window(text: str) -> tuple[str | None, str | None]:
    """Extract a deterministic HH:MM window from natural-language constraints."""
    source = text or ""
    range_match = re.search(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(?:-|~|～|至|到)\s*"
        r"([01]?\d|2[0-3]):([0-5]\d)\b",
        source,
    )
    if range_match:
        return (
            f"{int(range_match.group(1)):02d}:{range_match.group(2)}",
            f"{int(range_match.group(3)):02d}:{range_match.group(4)}",
        )

    clock_pattern = re.compile(
        r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间)?\s*"
        r"(?P<hour>[零〇一二两三四五六七八九十\d]{1,3})\s*"
        r"(?:点|时)(?:(?P<minute>半|[0-5]?\d)\s*分?)?\s*"
        r"(?P<direction>之后|以后|及以后|起|之前|以前|及以前|前)"
    )
    start: str | None = None
    end: str | None = None
    for match in clock_pattern.finditer(source):
        value = _clock_value(
            match.group("hour"),
            match.group("minute") or "",
            match.group("period") or "",
        )
        if value is None:
            continue
        if match.group("direction") in {"之后", "以后", "及以后", "起"}:
            start = value
        else:
            end = value

    for match in re.finditer(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(之后|以后|及以后|起|之前|以前|及以前)",
        source,
    ):
        value = f"{int(match.group(1)):02d}:{match.group(2)}"
        if match.group(3) in {"之后", "以后", "及以后", "起"}:
            start = value
        else:
            end = value
    return start, end


def _prefilter_time_records(
    text: str,
    *,
    start_time: str | None,
    end_time: str | None,
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    """Select schedule-like records by departure time before output truncation."""
    start_minutes = int(start_time[:2]) * 60 + int(start_time[3:]) if start_time else 0
    end_minutes = (
        int(end_time[:2]) * 60 + int(end_time[3:]) if end_time else 23 * 60 + 59
    )

    # Prefer preserving structured API/MCP responses when they contain a list
    # of objects with a recognizable departure-time field.
    try:
        parsed_json = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_json = None
    time_keys = {
        "departure_time",
        "depart_time",
        "start_time",
        "departuretime",
        "departtime",
        "starttime",
        "出发时间",
        "发车时间",
        "开车时间",
    }

    def filter_json(value: Any) -> tuple[Any, int, int]:
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
        ):
            detected = 0
            selected_items: list[dict[str, Any]] = []
            for item in value:
                raw_time = next(
                    (
                        field
                        for key, field in item.items()
                        if str(key).casefold() in time_keys
                    ),
                    None,
                )
                match = re.search(
                    r"\b([01]?\d|2[0-3]):([0-5]\d)\b", str(raw_time or "")
                )
                if not match:
                    continue
                detected += 1
                minutes = int(match.group(1)) * 60 + int(match.group(2))
                if start_minutes <= minutes <= end_minutes:
                    selected_items.append(item)
            if detected:
                return selected_items, detected, len(selected_items)
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, str) for item in value)
        ):
            detected_strings = 0
            selected_strings: list[str] = []
            for item in value:
                # 12306-style records are pipe-delimited strings whose first
                # clock token is the departure time.
                match = re.search(r"(?:^|\|)([01]?\d|2[0-3]):([0-5]\d)(?:\||$)", item)
                if not match:
                    continue
                detected_strings += 1
                minutes = int(match.group(1)) * 60 + int(match.group(2))
                if start_minutes <= minutes <= end_minutes:
                    selected_strings.append(item)
            if detected_strings:
                return selected_strings, detected_strings, len(selected_strings)
        if isinstance(value, dict):
            for key, child in value.items():
                filtered_child, detected, matched = filter_json(child)
                if detected:
                    copied = dict(value)
                    copied[key] = filtered_child
                    return copied, detected, matched
        return value, 0, 0

    if parsed_json is not None:
        filtered_json, detected, matched = filter_json(parsed_json)
        if detected:
            filtered = json.dumps(filtered_json, ensure_ascii=False, indent=2)
            truncated = len(filtered) > max_chars
            if truncated:
                filtered = filtered[:max_chars] + "\n... (筛选后的 JSON 已截断)"
            return filtered, {
                "prefilter_applied": True,
                "filter_type": "time_window_json",
                "filter_start_time": start_time,
                "filter_end_time": end_time,
                "records_detected": detected,
                "records_matched": matched,
                "original_content_length": len(text),
                "filtered_content_length": len(filtered),
                "filtered_content_truncated": truncated,
            }

    lines = text.splitlines()
    candidates: list[tuple[int, int]] = []
    all_clock_lines: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _CLOCK_LINE_RE.fullmatch(line)
        if not match:
            continue
        minutes = int(match.group(1)) * 60 + int(match.group(2))
        all_clock_lines.append((index, minutes))
        lookahead = [
            item.strip() for item in lines[index + 1 : index + 18] if item.strip()
        ][:7]
        if any(_DURATION_RE.search(item) for item in lookahead) and any(
            _TRAIN_CODE_RE.fullmatch(item) for item in lookahead
        ):
            candidates.append((index, minutes))

    # Structured departure rows are preferred.  A generic time-line fallback
    # still preserves useful tail context for unfamiliar list formats.
    record_starts = (
        candidates
        if candidates
        else (all_clock_lines if len(all_clock_lines) >= 3 else [])
    )
    if not record_starts:
        return text, {}
    selected: list[str] = []
    for offset, (line_index, minutes) in enumerate(record_starts):
        if not start_minutes <= minutes <= end_minutes:
            continue
        next_index = (
            record_starts[offset + 1][0]
            if offset + 1 < len(record_starts)
            else min(len(lines), line_index + 30)
        )
        selected.append("\n".join(lines[line_index:next_index]).strip())

    label = f"{start_time or '00:00'}–{end_time or '23:59'}"
    header = (
        f"[已在截断前应用时间筛选：{label}；"
        f"识别 {len(record_starts)} 条记录，命中 {len(selected)} 条]\n"
    )
    filtered = header + (
        "\n\n".join(selected) if selected else "未发现符合时间条件的记录。"
    )
    truncated = len(filtered) > max_chars
    if truncated:
        suffix = "\n\n... (筛选后的内容仍超出字符预算，已截断)"
        filtered = filtered[: max(0, max_chars - len(suffix))] + suffix
    return filtered, {
        "prefilter_applied": True,
        "filter_type": "time_window",
        "filter_start_time": start_time,
        "filter_end_time": end_time,
        "records_detected": len(record_starts),
        "records_matched": len(selected),
        "original_content_length": len(text),
        "filtered_content_length": len(filtered),
        "filtered_content_truncated": truncated,
    }


def _prefilter_keyword_context(
    text: str,
    *,
    query: str,
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    """Keep bounded line windows around selective keyword matches."""
    tokens = re.findall(r"[A-Za-z0-9_.-]{2,}|[\u3400-\u9fff]{2,}", query)
    stopwords = {
        "查询",
        "结果",
        "数据",
        "内容",
        "信息",
        "筛选",
        "过滤",
        "search",
        "query",
    }
    tokens = [
        token.casefold() for token in tokens if token.casefold() not in stopwords
    ][:8]
    if not tokens:
        return text, {}
    lines = text.splitlines()
    matches = [
        index
        for index, line in enumerate(lines)
        if any(token in line.casefold() for token in tokens)
    ]
    # A keyword present almost everywhere (for example a destination name in
    # every timetable row) does not reduce the response and should not replace
    # the normal truncation path.
    if not matches or len(matches) > max(30, int(len(lines) * 0.6)):
        return text, {}
    selected_indexes: set[int] = set()
    for index in matches:
        selected_indexes.update(range(max(0, index - 4), min(len(lines), index + 7)))
    selected = "\n".join(
        line for index, line in enumerate(lines) if index in selected_indexes
    ).strip()
    header = (
        f"[已在截断前应用关键词筛选：{', '.join(tokens)}；命中 {len(matches)} 处]\n"
    )
    filtered = header + selected
    truncated = len(filtered) > max_chars
    if truncated:
        suffix = "\n\n... (筛选后的内容仍超出字符预算，已截断)"
        filtered = filtered[: max(0, max_chars - len(suffix))] + suffix
    return filtered, {
        "prefilter_applied": True,
        "filter_type": "keyword",
        "filter_query": query[:300],
        "keyword_matches": len(matches),
        "original_content_length": len(text),
        "filtered_content_length": len(filtered),
        "filtered_content_truncated": truncated,
    }


class ResultFilteringMixin:
    """Apply user time/keyword constraints before response truncation."""

    def _prefilter_result_text(
        self,
        text: str,
        context: AgentContext,
    ) -> tuple[str, dict[str, Any]]:
        """Apply user list constraints before any prefix truncation."""
        constraint_source = str(
            context.get("_query_constraint_source")
            or context.get("_current_user_request")
            or ""
        )
        inferred_start, inferred_end = _infer_time_window(
            f"{constraint_source}\n{self.url}"
        )

        def valid_clock(value: Any) -> str | None:
            match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", str(value or ""))
            if not match:
                return None
            return f"{int(match.group(1)):02d}:{match.group(2)}"

        start_time = valid_clock(self.start_time) or inferred_start
        end_time = valid_clock(self.end_time) or inferred_end
        try:
            max_chars = max(1000, min(int(self.max_chars), 30000))
        except (TypeError, ValueError):
            max_chars = 12000
        if start_time or end_time:
            return _prefilter_time_records(
                text,
                start_time=start_time,
                end_time=end_time,
                max_chars=max_chars,
            )
        query = self._resolve_template(self.query, context).strip()
        if query:
            return _prefilter_keyword_context(
                text,
                query=query,
                max_chars=max_chars,
            )
        return text, {}
