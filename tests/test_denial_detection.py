"""D-1 回归：拒绝性回复检测不能把「可以，但有前提」判成拒绝。

真实事故（2026-10-17 的 Xenon 会话）：用户问了一个纯 Q&A 问题，模型回答
「可以，但有几个前提……我无法直接访问 D 盘」，旧实现（整段子串匹配）命中
「无法直接」→ direct_chat 抛 RuntimeError("模型拒绝了无需工具即可完成的请求")
→ 触发备用模型切换。这里锁住新启发式的行为。
"""

from __future__ import annotations

import pytest

from xenon.repl.repl import REPL


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("抱歉，我无法直接访问该路径。", True),
        ("我无法获取这个文件的内容。", True),
        ("I can't access that directory.", True),
        ("I cannot complete this task.", True),
        ("可以，但有几个前提和注意点——我无法直接访问 D 盘。", False),
        ("没问题，我来做。不过这个路径我无法访问。", False),
        ("Sure, I can do that. I cannot guarantee the PR though.", False),
        ("这是很长的分析结果。" * 40 + "我无法直接访问。", False),
        ("", False),
        ("   ", False),
    ],
)
def test_detect_denial_requires_opening_refusal(text: str, expected: bool) -> None:
    assert REPL._detect_denial(text) is expected


def test_detect_denial_ignores_caveat_after_commitment() -> None:
    """带承诺的答案即使后面有「无法」也不应触发模型切换。"""

    text = (
        "可以。流程本身没有技术障碍：克隆 → 实现 → 测试 → PR → 清理。\n"
        "但有一点无法直接保证：push 需要凭据。"
    )
    assert REPL._detect_denial(text) is False
