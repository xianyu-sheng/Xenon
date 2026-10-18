"""P1-2：文件声称审计只认完成声明，不把引用路径算成声称。"""

from __future__ import annotations

from xenon.engine.evidence_gate import verify_file_claims


def test_research_answer_quoting_paths_is_not_a_claim() -> None:
    answer = (
        "按改动面排序：#33 落在 `smartbench/frontends/python.py`，"
        "测试范式在 `tests/test_python_frontend_mutation.py`；"
        "fixture 目录已存在，验收标准写得最具体。"
    )
    assert verify_file_claims(answer, None) == (True, [])


def test_completion_claim_is_still_detected() -> None:
    passed, files = verify_file_claims("已创建 smartbench/foo.py", None)
    assert passed is False
    assert files == ["smartbench/foo.py"]


def test_created_with_multiple_files() -> None:
    passed, files = verify_file_claims("创建了 tests/bar.py 和 docs/x.md", None)
    assert passed is False
    assert set(files) == {"tests/bar.py", "docs/x.md"}


def test_plan_style_bare_verb_is_not_a_claim() -> None:
    answer = "计划：创建 fixture，写入 tests/bar.py，并运行测试。"
    assert verify_file_claims(answer, None) == (True, [])
