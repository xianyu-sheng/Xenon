"""证据链收敛（R 收尾）：引擎不再阻断交付，版本号不再被当文件。

背景（用户实测崩溃）：只读调研任务回答里出现「.github/workflows/ci.yml」
与 Python 版本矩阵「3.10/3.11/3.12」，旧 FileClaimGate 在 finalize_evidence
里 raise → 引擎裸崩。旧设计与新设计脱节：

- 引擎内残留的交付门（finalize raise + PlanExecute 补救块）已移除；
- 校验统一由回合级 TurnGate 判定（fail → 反馈重试 → 熔断）；
- 版本号类 token 不再被 _extract_claimed_files 当作文件声称。
"""

from __future__ import annotations

from xenon.engine.evidence_gate import _extract_claimed_files, verify_file_claims


def test_version_tokens_are_not_claimed_files():
    out = _extract_claimed_files(
        "CI 覆盖 Python 3.10/3.11/3.12；改动在 .github/workflows/ci.yml。"
    )
    assert "3.10/3.11/3.12" not in out
    assert any("ci.yml" in f for f in out)  # 真实路径仍被提取


def test_version_only_claims_pass_verification():
    passed, unverified = verify_file_claims(
        "已创建 3.10/3.11/3.12 版本矩阵文件",
        tracker=None,
    )
    assert passed is True
    assert unverified == []
