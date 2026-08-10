from xenon.engine.strategy_guide import get_strategy_advice


def test_write_tools_are_presented_as_alternatives_not_mandatory_sequence():
    advice = get_strategy_advice(
        "debug",
        {"command", "read_file", "edit_file", "write_file"},
        "修复 a.py",
    )
    assert "edit_file 或 write_file" in advice.prompt
    assert "edit_file → write_file" not in advice.prompt
