"""Deterministic per-turn archives from the session event log.

When context grows, older turns are archived as *structured* records
(intent, tools with success/failure, key decisions, artifacts, outcome)
instead of being truncated or LLM-summarised.  The archive is rendered as a
compact block that replaces the older raw messages in the model context;
recent turns keep their full messages.

This matches Xenon's event-sourced design: archives are projections of the
log, replayable and free (no extra LLM call), and they double as the summary
source for read-only recall.
"""

from __future__ import annotations


# 归档里每个回合的工具记录上限与输出上限。
_MAX_TOOLS_PER_TURN = 6
_MAX_ARCHIVE_TURNS = 40
_DEFAULT_KEEP_LAST = 6


def build_turn_archives(
    events: list[dict], keep_last: int = _DEFAULT_KEEP_LAST
) -> list[dict]:
    """Fold *events* into per-turn structured archives.

    Turns are delimited by ``turn/user`` events; a turn's span runs until the
    next ``turn/user``.  The last ``keep_last`` turns are kept verbatim and
    every older turn becomes one archive record.
    """

    turns: list[list[dict]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn/user":
            turns.append([event])
        elif turns:
            turns[-1].append(event)

    archived = turns[:-keep_last] if keep_last > 0 else turns
    records: list[dict] = []
    for turn in archived[: _MAX_ARCHIVE_TURNS]:
        records.append(_fold_turn(turn))
    return records


def _fold_turn(turn: list[dict]) -> dict:
    user_event = next(
        (e for e in turn if e.get("type") == "turn/user"), {}
    )
    intent = user_event.get("intent")
    objective = str(user_event.get("text") or "")[:80]
    level = user_event.get("level")

    tools: list[dict] = []
    failures: list[str] = []
    artifacts: list[str] = []
    decisions: list[str] = []
    outcome = ""

    for event in turn:
        kind = event.get("type")
        if kind == "tool/result":
            entry = {"tool": str(event.get("tool") or "")}
            paths = [str(p) for p in event.get("paths") or []]
            if paths:
                entry["target"] = paths[0]
            entry["success"] = bool(event.get("success"))
            if not entry["success"] and event.get("error"):
                message = str(event["error"])[:100]
                entry["error"] = message
                failures.append(f"{entry['tool']}: {message}")
            if entry["success"]:
                artifacts.extend(paths)
            if len(tools) < _MAX_TOOLS_PER_TURN:
                tools.append(entry)
        elif kind == "gate/opened":
            decisions.append(f"门开启({event.get('kind') or 'approval'})")
        elif kind == "gate/resolved":
            decisions.append(f"门→{event.get('outcome') or 'resolved'}")
        elif kind == "goal/opened":
            decisions.append("目标开启")
        elif kind == "goal/paused":
            decisions.append("目标暂停")
        elif kind == "plan_mode/set":
            decisions.append(
                f"计划模式{'开' if event.get('active') else '关'}"
            )
        elif kind == "turn/assistant":
            outcome = str(event.get("preview") or "")[:120]

    return {
        "intent": intent,
        "objective": objective,
        "level": level,
        "continuation": bool(user_event.get("continuation")),
        "deferred": bool(user_event.get("deferred")),
        "tools": tools,
        "failures": failures,
        "artifacts": artifacts[:8],
        "decisions": decisions,
        "outcome": outcome,
    }


def render_archive_block(records: list[dict], budget: int = 1200) -> str:
    """Render archives as one compact, bounded context block."""

    if not records:
        return ""
    lines = ["## 早期回合归档（结构化）"]
    used = len(lines[0])
    for index, record in enumerate(records, start=1):
        parts: list[str] = []
        if record.get("intent"):
            parts.append(f"意图={record['intent']}")
        if record.get("objective"):
            parts.append(f"目标={record['objective'][:50]}")
        tool_bits = []
        for tool in record.get("tools") or []:
            mark = "✓" if tool.get("success") else "✗"
            name = tool.get("tool", "")
            target = tool.get("target", "")
            tool_bits.append(f"{name}{mark}({target[:40]})" if target else f"{name}{mark}")
        if tool_bits:
            parts.append("工具:" + ",".join(tool_bits))
        if record.get("artifacts"):
            parts.append("产物:" + ",".join(record["artifacts"][:4]))
        if record.get("decisions"):
            parts.append("决策:" + ",".join(record["decisions"][:4]))
        if record.get("failures"):
            parts.append("失败:" + ",".join(record["failures"][:2]))
        if record.get("outcome"):
            parts.append(f"结果={record['outcome'][:60]}")
        line = f"- [T{index}] " + " | ".join(parts)
        if used + len(line) + 1 > budget and index > 1:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
