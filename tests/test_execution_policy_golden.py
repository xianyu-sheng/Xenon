"""Golden corpus for the per-turn execution policy.

These cases come from two real audits:

1. "落盘许可" audit — the classifier mis-read explicit write requests as
   read-only/answer-only (user was told "no permission, ask again next turn")
   and mis-read explicit *prohibitions* as write authorization
   ("不要修改任何文件" → WRITE).
2. "读 .et" audit — a correct READ_ONLY classification still failed because
   the read path could not parse a binary document; the boundary text told
   the model to refuse instead of asking.

The corpus is deliberately phrased the way a user speaks, not the way the
regexes are written: direction complements (写到/输出到/记录到), relative
filenames, negation of mutation verbs, and English equivalents.
"""

from __future__ import annotations

import pytest

from xenon.engine.context import AgentContext
from xenon.engine.evidence_gate import task_requires_write
from xenon.repl.execution_policy import (
    ExecutionLevel,
    classify_execution_policy,
    execution_boundary_text,
)
from xenon.repl.prompt_optimizer import detect_intent


def _classify(text: str):
    return classify_execution_policy(text, intent=detect_intent(text))


# ── A. 明确要求落盘 → 必须 WRITE ──────────────────────────────
EXPLICIT_WRITE_REQUESTS = [
    # 动词 + 方向补语 + 文件名（相对路径）
    "把结果写到 output.txt",
    "输出到 config.yaml",
    "把结果记录到 notes.md 里",
    "写一份总结到 report.md",
    "帮我把这份报告导出到 report.xlsx",
    # 在某个文件里做增补
    "在这个文件里加一个函数",
    "帮我在 utils.py 里补一个 helper",
    "把上面的脚本追加到 setup.py 里",
    # 目标由句式承载（存档/更新）
    "生成一份报告存起来",
    "帮我更新 README 加上安装说明",
    # English equivalents
    "write the result to output.txt",
    "save the output as result.json",
    "append the results to log.txt",
]


@pytest.mark.parametrize("text", EXPLICIT_WRITE_REQUESTS)
def test_explicit_write_requests_authorize_write(text: str) -> None:
    policy = _classify(text)

    assert policy.level >= ExecutionLevel.WRITE, (
        f"{text!r} → {policy.level.name}（{policy.reason}）"
    )


# ── B. 明确禁令 → 必须压过一切肯定式 ─────────────────────────
EXPLICIT_NO_WRITE_REQUESTS = [
    "不要修改任何文件，只在对话里回答",
    "先别改任何文件，只讲解思路",
    "不要动这个文件，给我解释一下",
    "别删除任何文件",
    "不要重写代码",
    "不要改代码，只分析",
    "don't modify any files",
    "do not touch the files",
]


@pytest.mark.parametrize("text", EXPLICIT_NO_WRITE_REQUESTS)
def test_explicit_negation_never_authorizes_write(text: str) -> None:
    policy = _classify(text)

    assert policy.explicit_no_write is True, (
        f"{text!r} 没有识别出显式禁令（{policy.reason}）"
    )
    assert policy.level < ExecutionLevel.WRITE, (
        f"{text!r} → {policy.level.name}（{policy.reason}）"
    )


# ── C. 回归：纯生成/只读不得被新结构误升级 ────────────────────
STILL_ANSWER_ONLY = [
    "帮我写一个排序函数",
    "写一个 Python 爬虫",
    "我想写一个 Python 脚本查询天气",
    "想写个模块",
    "写个快排给我看看",
    "把这段话翻译成英文",
    "只在对话里输出一个排序函数",
]


@pytest.mark.parametrize("text", STILL_ANSWER_ONLY)
def test_pure_generation_stays_answer_only(text: str) -> None:
    assert _classify(text).level < ExecutionLevel.WRITE


STILL_READ_ONLY = [
    'C:\\Users\\Administrator\\Desktop\\校招投递表1.et 读一下这个文件，并总计一下这个文件是在做什么？',
    "帮我把桌面上的校招投递表1.et 读出来并整理成汇总",
    "读一下 output.txt",
    "分析一下 resume.tex",
    "读取 src/main.py 并解释接口",
]


@pytest.mark.parametrize("text", STILL_READ_ONLY)
def test_read_only_requests_stay_read_only(text: str) -> None:
    policy = _classify(text)

    assert policy.level is ExecutionLevel.READ_ONLY, (
        f"{text!r} → {policy.level.name}（{policy.reason}）"
    )
    assert policy.requires_tools is True


# ── D. 边界文本：能力陈述 + 询问路径，而不是让模型复述"没许可" ──
def test_answer_only_boundary_offers_asking_instead_of_flat_refusal() -> None:
    boundary = execution_boundary_text(ExecutionLevel.ANSWER_ONLY)

    # 旧文案是"禁止调用任何工具……绝不能越过"，模型只能转述成"未获得许可"。
    assert "禁止调用任何工具" not in boundary
    assert "询问" in boundary


def test_read_only_boundary_keeps_scope_and_ask_path() -> None:
    boundary = execution_boundary_text(ExecutionLevel.READ_ONLY)

    assert "本轮只允许只读工具" in boundary
    assert "询问" in boundary
    # 不能让模型直接宣布"没权限"，必须给出可执行的升级通道。
    assert "禁止写文件" in boundary


# ── E. 证据门：有本轮契约时不得再独立重算 ────────────────────
def test_task_requires_write_prefers_turn_contract() -> None:
    read_turn = AgentContext({"_execution_level": int(ExecutionLevel.READ_ONLY)})
    write_turn = AgentContext({"_execution_level": int(ExecutionLevel.WRITE)})

    # 文本本身像写任务，但本轮只授权只读 → 以本轮契约为准。
    assert task_requires_write("把结果写到 output.txt", ctx=read_turn) is False
    # 文本本身像问答，但本轮授权了写入 → 以本轮契约为准。
    assert task_requires_write("What does this code do?", ctx=write_turn) is True
    # 没有契约时保持旧行为（向后兼容 library/direct 调用）；语义任务由
    # 分类器负责，正则只认显式写入结构。
    assert task_requires_write("把结果写到 output.txt") is True
    assert task_requires_write("What does this code do?") is False


# ── F. 工具视图：schema / Tip / 闸门共用同一份可用工具 ──────
def test_react_tool_view_respects_turn_level() -> None:
    from xenon.engine.react_engine import ReActEngine
    from xenon.engine.strategy_guide import get_strategy_advice

    engine = ReActEngine(["test/model"])
    engine._active_execution_level = int(ExecutionLevel.READ_ONLY)
    allowed = engine._allowed_tool_names()

    assert "read_file" in allowed
    assert "command" not in allowed
    assert "write_file" not in allowed

    # 旧行为：Tip 用未过滤的 self.tools，会在只读轮次推荐被禁的 command/write_file。
    advice = get_strategy_advice("debug", allowed, "帮我看看这个报错")
    assert advice.tip
    assert "command" not in advice.tip
    assert "write_file" not in advice.tip


# ── G. 二进制文件：结构化引导而不是 UnicodeDecodeError ──────
def test_read_file_binary_returns_structured_guidance(tmp_path) -> None:
    from xenon.nodes.tool_node import ToolNode

    target = tmp_path / "校招投递表1.et"
    # OLE2 复合文档魔数（.et/.xls/.doc 共用）
    target.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)

    result = ToolNode(
        "read",
        action_type="read_file",
        file_path=str(target),
        cwd=str(tmp_path),
    ).execute(AgentContext())

    assert result["success"] is False
    assert "OLE2" in result["error"]
    assert "read_file" in result["error"]
    assert "read_document" in result["error"]
