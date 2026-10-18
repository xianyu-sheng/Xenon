"""P0-1：规划 JSON 的裸键名修复。"""

from __future__ import annotations

import json

from xenon.utils.response_adapter import _repair_bare_keys, parse_plan


def test_bare_keys_are_quoted() -> None:
    raw = '{"analysis":"x","steps":[{"id":1,"task":"do",tool":null,"params":{}}]}'
    fixed = _repair_bare_keys(raw)
    data = json.loads(fixed)
    assert data["steps"][0]["id"] == 1
    assert data["steps"][0]["task"] == "do"


def test_partial_quote_key_form_is_quoted() -> None:
    # 真实事故形态：{"id":26,task":"查看..."} —— 键名缺开引号、多一个闭引号
    raw = '{"analysis":"x","steps":[{"id":26,task":"查看工作区","tool":"git"}]}'
    fixed = _repair_bare_keys(raw)
    data = json.loads(fixed)
    assert data["steps"][0]["task"] == "查看工作区"
    assert data["steps"][0]["tool"] == "git"


def test_string_values_are_not_corrupted() -> None:
    raw = '{"note":"keep {this: that} and a: b","n":1}'
    assert _repair_bare_keys(raw) == raw


def test_parse_plan_recovers_bare_key_plan() -> None:
    raw = '{"analysis":"a","steps":[{"id":1,task":"do it","tool":null,"params":{}}]}'
    plan = parse_plan(raw)
    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["task"] == "do it"
