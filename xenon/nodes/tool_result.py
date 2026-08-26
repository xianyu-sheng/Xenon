"""统一工具结果协议。

工具实现仍然可以返回历史兼容的 ``dict``，但从这里开始每个结果都会
同时携带一份稳定的结构化视图。文本 ``content`` 只负责给模型阅读，
``records``、``total``、``truncated`` 和 ``next_cursor`` 才是程序和分页
逻辑应该依赖的字段。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


TOOL_RESULT_SCHEMA_VERSION = "1.0"


def _first_value(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return default


@dataclass
class ToolResult:
    """Stable, serialisable view of one tool invocation."""

    tool_name: str
    success: bool
    kind: str = "generic"
    source: str | None = None
    content: str = ""
    records: list[Any] = field(default_factory=list)
    total: int | None = None
    matched: int | None = None
    truncated: bool = False
    next_cursor: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    schema_version: str = TOOL_RESULT_SCHEMA_VERSION

    @classmethod
    def from_raw(cls, tool_name: str, raw: dict[str, Any] | None) -> "ToolResult":
        """Build a structured result without changing legacy fields."""
        payload = raw if isinstance(raw, dict) else {}
        action_type = str(payload.get("action_type") or tool_name)
        kind = _kind_for(action_type, payload)

        content = _first_value(payload, "content", "stdout", "output", default="")
        if isinstance(content, list):
            content = "\n".join(str(item) for item in content)
        elif not isinstance(content, str):
            content = str(content or "")

        records = payload.get("records")
        if not isinstance(records, list):
            if action_type == "list_files" and isinstance(payload.get("files"), list):
                records = payload["files"]
            elif action_type == "search_files" and isinstance(
                payload.get("matches"), list
            ):
                records = payload["matches"]
            else:
                records = []

        source = _first_value(
            payload,
            "source",
            "url",
            "path",
            "file_path",
            "repo",
            "tool",
        )
        total = _first_value(
            payload,
            "total",
            "count",
            "match_count",
            "records_detected",
        )
        matched = _first_value(
            payload,
            "matched",
            "returned_count",
            "records_matched",
            "match_count",
        )
        if total is None and records:
            total = len(records)
        if matched is None and records:
            matched = len(records)

        filters: dict[str, Any] = {}
        if isinstance(payload.get("filters"), dict):
            filters.update(payload["filters"])
        for source_key, target_key in (
            ("filter_type", "type"),
            ("filter_start_time", "start_time"),
            ("filter_end_time", "end_time"),
            ("query", "query"),
        ):
            if payload.get(source_key) is not None:
                filters[target_key] = payload[source_key]

        known = {
            "action_type",
            "success",
            "content",
            "stdout",
            "output",
            "files",
            "matches",
            "records",
            "source",
            "url",
            "path",
            "file_path",
            "repo",
            "tool",
            "total",
            "count",
            "match_count",
            "records_detected",
            "matched",
            "returned_count",
            "records_matched",
            "truncated",
            "filtered_content_truncated",
            "next_cursor",
            "filters",
            "filter_type",
            "filter_start_time",
            "filter_end_time",
            "query",
            "error",
            "schema_version",
            "tool_result",
        }
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in known and key != "success"
        }
        return cls(
            tool_name=tool_name,
            success=bool(payload.get("success", False)),
            kind=kind,
            source=str(source) if source is not None else None,
            content=content,
            records=list(records),
            total=_as_int(total),
            matched=_as_int(matched),
            truncated=bool(
                payload.get("truncated")
                or payload.get("filtered_content_truncated")
                or payload.get("next_cursor")
            ),
            next_cursor=(
                str(payload["next_cursor"])
                if payload.get("next_cursor") is not None
                else None
            ),
            filters=filters,
            metadata=metadata,
            error=str(payload["error"]) if payload.get("error") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return asdict(self)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _kind_for(action_type: str, payload: dict[str, Any]) -> str:
    if action_type == "list_files":
        return "file_list"
    if action_type == "search_files":
        return "file_search"
    if action_type in {"web_fetch", "docs_fetch", "github_fetch"}:
        return "web_document"
    if action_type == "mcp_call":
        return "mcp_result"
    if action_type == "command":
        return "command_result"
    if action_type in {"weather", "datetime"}:
        return "realtime_data"
    if not payload.get("success", False):
        return "error"
    return action_type or "generic"


def enrich_tool_result(
    tool_name: str,
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach the canonical view to a legacy result dictionary.

    Existing top-level keys are intentionally preserved.  Consumers can use
    ``result["tool_result"]`` or the duplicated canonical fields while older
    integrations continue reading ``content``, ``files`` and ``count``.
    """
    result = (
        dict(raw)
        if isinstance(raw, dict)
        else {
            "success": False,
            "error": "工具没有返回字典结果",
        }
    )
    structured = ToolResult.from_raw(tool_name, result)
    canonical = structured.to_dict()
    result["schema_version"] = TOOL_RESULT_SCHEMA_VERSION
    result["tool_result"] = canonical
    for key in (
        "kind",
        "source",
        "records",
        "total",
        "matched",
        "truncated",
        "next_cursor",
        "filters",
    ):
        result.setdefault(key, canonical[key])
    return result
