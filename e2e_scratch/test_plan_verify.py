from pathlib import Path


def test_plan_verify_content():
    content = Path("e2e_scratch/plan_verify.txt").read_text(encoding="utf-8").strip()
    assert content == "hello"
