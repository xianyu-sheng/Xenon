"""Regression tests for per-turn side-effect boundaries."""

from __future__ import annotations

import ast

import pytest

from xenon.engine.react_engine import ReActEngine
from xenon.engine.context import AgentContext
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes.tool_executor import ToolExecutor, execution_policy_denial
from xenon.repl.code_response import validate_code_response
from xenon.repl.difficulty_estimator import DifficultyEstimator
from xenon.repl.execution_policy import (
    ExecutionLevel,
    bind_execution_boundary,
    classify_execution_policy,
    strip_execution_boundary,
)
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.prompt_optimizer import detect_intent
from xenon.repl.repl import REPL


def test_turn_boundary_is_idempotent_and_ignored_by_intent_classification():
    original = "请解释快速排序，不要调用工具"
    bound = bind_execution_boundary(original, ExecutionLevel.ANSWER_ONLY)

    assert bind_execution_boundary(bound, ExecutionLevel.ANSWER_ONLY) == bound
    assert strip_execution_boundary(bound) == original
    assert ReActEngine._input_requires_tools(bound) is False


@pytest.mark.parametrize(
    "text",
    [
        "使用python为我写一个快速排序的核心算法代码 输出到对话区域 不写入文件",
        "为我写一个python实现的快速排序的核心算法代码，并给出详细注释，输出到对话区域",
        "Write a quicksort implementation. Output it in the chat and do not create files.",
        "写一个 Python 爬虫",
    ],
)
def test_code_generation_without_side_effect_authorization_is_answer_only(text):
    intent = detect_intent(text)
    policy = classify_execution_policy(text, intent=intent)

    assert intent == "write_code"
    assert policy.level is ExecutionLevel.ANSWER_ONLY
    assert policy.requires_tools is False
    assert REPL._detect_tool_need(text, intent=intent) is False
    assert DifficultyEstimator._needs_tools(text, intent) is False
    assert ReActEngine._input_requires_tools(text) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("把代码保存到 /tmp/quicksort.py，不要运行", ExecutionLevel.WRITE),
        ("创建一个 hello.py 文件，内容是 print('hello')", ExecutionLevel.WRITE),
        ("写一个 hello.py 文件，内容是 print('hello')", ExecutionLevel.WRITE),
        ("写入 /tmp/quicksort.py，然后运行测试", ExecutionLevel.EXECUTE),
        ("读取 src/main.py 并解释接口", ExecutionLevel.READ_ONLY),
        ("继续，读简历文件", ExecutionLevel.READ_ONLY),
        ("允许你读工具？以后都可以读", ExecutionLevel.READ_ONLY),
        ("resume.tex 请你分析候选人的经历", ExecutionLevel.READ_ONLY),
        ("今天苏州天气怎么样", ExecutionLevel.READ_ONLY),
        ("请修复这个 bug", ExecutionLevel.WRITE),
    ],
)
def test_explicit_actions_map_to_their_maximum_level(text, expected):
    policy = classify_execution_policy(text, intent=detect_intent(text))

    assert policy.level is expected
    assert policy.requires_tools is True


def test_chat_only_constraint_wins_over_execute_words():
    text = "给出可运行并通过测试的 Python 代码，只输出到对话中，不写入文件，也不要执行"
    policy = classify_execution_policy(text, intent="write_code")

    assert policy.level is ExecutionLevel.ANSWER_ONLY
    assert policy.explicit_no_write is True
    assert policy.explicit_no_execute is True


def test_research_request_uses_final_request_clause_not_background_plan():
    text = (
        "我打算将你提交到一些大模型厂商的官方 agent 接入工具，"
        "但是 DeepSeek 的 PR 维护太慢了，请你查一下哪个大模型厂商维护更快，"
        "比如豆包、智普这些？"
    )

    intent = detect_intent(text)
    policy = classify_execution_policy(text, intent=intent)

    assert intent == "research"
    assert policy.level is ExecutionLevel.READ_ONLY
    assert policy.reason == "信息查询或资料调研只允许只读工具"
    assert execution_policy_denial(
        "clone_repo",
        {"repo": "THUDM/AgentBench"},
        AgentContext({"_execution_level": int(policy.level)}),
    ) is not None


@pytest.mark.parametrize(
    "text",
    [
        "我计划以后提交 PR，请你先调研一下哪些厂商的社区维护更活跃",
        "我想把项目推送到其他平台，帮我比较这些平台的 PR 响应速度",
    ],
)
def test_hypothetical_write_background_does_not_authorize_writes(text):
    policy = classify_execution_policy(text, intent=detect_intent(text))

    assert policy.level is ExecutionLevel.READ_ONLY


def test_document_paths_before_request_cue_still_authorize_read_only():
    """路径证据不能因最后一个“请你”而被裁掉。

    简历、论文等文档通常先给出绝对路径，再写“请你分析”。这类请求
    必须进入 ReAct，并保留只读权限；不能落到没有 tools schema 的 direct。
    """
    text = (
        "/media/xianyu-sheng/32 GB/去实习需要拷贝的文件/工作/2027-ai agent-校招简历-v3.pdf\n"
        "/media/xianyu-sheng/32 GB/去实习需要拷贝的文件/工作/秋招简历_v6_Kimi版.tex "
        "请你分析一下我的秋招简历寻找 AI Agent 开发岗位是否有优势"
    )

    policy = classify_execution_policy(text, intent=detect_intent(text))

    assert policy.level is ExecutionLevel.READ_ONLY
    assert policy.requires_tools is True
    assert ReActEngine._input_requires_tools(text) is True


def test_repository_url_before_request_cue_still_authorizes_read_only():
    """Repository evidence before the final “请你” must not be discarded."""
    text = (
        "https://github.com/deepseek-ai/deepseek-harness"
        "我想要学习 DeepSeek Harness，请你带着我来学习"
    )

    policy = classify_execution_policy(text, intent=detect_intent(text))

    assert policy.level is ExecutionLevel.READ_ONLY
    assert policy.requires_tools is True
    assert "本轮只允许只读工具" in bind_execution_boundary(text, policy.level)


@pytest.mark.parametrize(
    "text",
    [
        "继续，读简历文件",
        "请读取 /tmp/resume.tex",
        "允许你读工具读取 /tmp/resume.pdf",
    ],
)
def test_spoken_read_requests_route_to_tools(text):
    policy = classify_execution_policy(text, intent=detect_intent(text))

    assert policy.level is ExecutionLevel.READ_ONLY
    assert policy.requires_tools is True


def test_plain_can_in_background_is_not_treated_as_request_cue():
    text = "允许你读工具？谁跟你说的不能读工具了？以后都可以读"

    policy = classify_execution_policy(text, intent=detect_intent(text))

    assert policy.level is ExecutionLevel.READ_ONLY


@pytest.mark.parametrize(
    "text",
    [
        "请你把当前修改提交到 GitHub",
        "现在提交",
        "帮我推送一下",
    ],
)
def test_explicit_git_requests_still_authorize_write(text):
    policy = classify_execution_policy(text, intent=detect_intent(text))

    assert policy.level is ExecutionLevel.WRITE


def test_valid_raw_python_is_normalized_to_a_fenced_block():
    checked = validate_code_response(
        "用 Python 写一个加法函数",
        "def add(a: int, b: int) -> int:\n    return a + b",
    )

    assert checked.valid is True
    assert checked.content.startswith("```python\n")
    code = checked.content.removeprefix("```python\n").removesuffix("\n```")
    ast.parse(code)


@pytest.mark.parametrize(
    ("response", "reason_fragment"),
    [
        ("[Any], low: int, high: int) -> int:\n    return low", "Python 代码不完整"),
        ("```python\ndef quick_sort(values):\n    return values", "代码块未闭合"),
        (
            '<||DSML||tool_calls><||DSML||invoke name="write_file">',
            "工具协议",
        ),
    ],
)
def test_corrupted_code_is_rejected_before_render(response, reason_fragment):
    checked = validate_code_response("用 Python 写快速排序", response)

    assert checked.valid is False
    assert reason_fragment in checked.reason


@pytest.mark.parametrize(
    ("authorized", "tool_name", "params", "blocked"),
    [
        (0, "read_file", {"file_path": "README.md"}, True),
        (1, "read_file", {"file_path": "README.md"}, False),
        (1, "write_file", {"file_path": "x.py", "content": "pass"}, True),
        (2, "write_file", {"file_path": "x.py", "content": "pass"}, False),
        (2, "command", {"action": "python x.py"}, True),
        (3, "command", {"action": "python x.py"}, False),
        (1, "mcp_call", {"tool_name": "weather:get"}, False),
        (1, "mcp_call", {"tool_name": "issues:create"}, True),
    ],
)
def test_tool_boundary_is_enforced_below_the_router(
    authorized, tool_name, params, blocked,
):
    context = AgentContext({"_execution_level": authorized})

    reason = execution_policy_denial(tool_name, params, context)

    assert (reason is not None) is blocked


def test_tool_executor_blocks_write_before_toolnode(monkeypatch):
    executed: list[str] = []

    def fake_execute(self, context):
        executed.append(self.action_type)
        return {"success": True, "content": "unexpected"}

    monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", fake_execute)
    context = AgentContext({"_execution_level": 1})
    tracker = ToolExecutionTracker()

    result = ToolExecutor().execute(
        "write_file",
        {"file_path": "x.py", "content": "pass"},
        context,
        tracker,
        tools={"write_file": {"name": "write_file"}},
    )

    assert result.success is False
    assert "本轮执行策略" in result.observation
    assert executed == []


def test_read_only_research_hides_and_blocks_clone_repo(monkeypatch):
    executed: list[str] = []

    def fake_execute(self, context):
        executed.append(self.action_type)
        return {"success": True, "content": "unexpected"}

    monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", fake_execute)
    context = AgentContext({"_execution_level": int(ExecutionLevel.READ_ONLY)})
    result = ToolExecutor().execute(
        "clone_repo",
        {"repo": "THUDM/AgentBench"},
        context,
        tools={"clone_repo": {"name": "clone_repo"}},
    )

    assert result.success is False
    assert "只读" in result.observation
    assert executed == []

    engine = ReActEngine(["test/model"])
    engine._active_execution_level = int(ExecutionLevel.READ_ONLY)
    visible_tools = {
        item["function"]["name"] for item in engine._build_tools_schema()
    }
    assert "github_fetch" in visible_tools
    assert "clone_repo" not in visible_tools


def test_code_text_that_mentions_a_saved_file_stays_in_direct(monkeypatch):
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl.ctx_mgr.add_user_message("输出示例代码")
    rendered: list[str] = []
    rerouted: list[str] = []
    monkeypatch.setattr(
        repl,
        "_blocking_response",
        lambda *_args: '```python\nprint("文件已保存")\n```',
    )
    monkeypatch.setattr(
        repl,
        "_render_assistant_text",
        lambda content, **_kwargs: rendered.append(content),
    )
    monkeypatch.setattr(
        repl,
        "_run_react_engine",
        lambda *_args: rerouted.append("react"),
    )

    repl._run_direct(
        "用 Python 输出提示文字",
        ["openai/test"],
        intent="write_code",
        execution_policy=classify_execution_policy(
            "用 Python 输出提示文字",
            intent="write_code",
        ),
    )

    assert rerouted == []
    assert rendered == ['```python\nprint("文件已保存")\n```']


@pytest.mark.parametrize(
    "text",
    [
        "帮我重构这个模块",
        "更新文档里的安装步骤",
        "纠正这个错误",
        "改进这个函数的实现",
        "删除多余的日志代码",
        "把重复代码重构成函数",
        "优化一下这个算法",
        "修改配置文件",
        "把这个函数改成异步的",
    ],
)
def test_chinese_mutation_requests_authorize_write(text):
    """中文修改类请求曾整体漏判为 ANSWER_ONLY（英文同义请求正确到 WRITE）。

    该分类器是全引擎共享层（ReAct/PlanExecute/EvidenceGate 的
    task_requires_write 都走这里），漏判会让 Agent 把「重构这个模块」
    当成闲聊回答，不调用任何工具。
    """
    policy = classify_execution_policy(text, intent=detect_intent(text))
    assert policy.level >= ExecutionLevel.WRITE


@pytest.mark.parametrize(
    "text",
    [
        "分析一下这个模块",
        "这个函数是干什么的",
        "解释这段代码的逻辑",
        "写一个 Python 爬虫",
        "把这个概念解释一下",
        "把这段话翻译成英文",
    ],
)
def test_chinese_readonly_or_answer_requests_not_escalated(text):
    """防过冲：只读/问答/纯生成请求不得因新模式被误判成 WRITE。"""
    policy = classify_execution_policy(text, intent=detect_intent(text))
    assert policy.level in (ExecutionLevel.ANSWER_ONLY, ExecutionLevel.READ_ONLY)


@pytest.mark.parametrize(
    "text",
    [
        "我需要一个 config.yaml",
        "给我一份 README.md",
        "帮我把这段代码存起来",
        "把结果保存下来",
        "搞定这个 bug",
        "处理一下这个报错",
        "解决这个崩溃问题",
        "give me a config file",
    ],
)
def test_implicit_write_requests_authorize_write(text):
    """目标由句式承载、没有显式写入动词的请求也须授权写入。

    这类请求此前全部落到 ANSWER_ONLY：LLM 拿不到写工具，只能把内容贴回
    对话，用户看到的就是「明明让他写文件，他却只是聊天」。
    """
    policy = classify_execution_policy(text, intent=detect_intent(text))
    assert policy.level >= ExecutionLevel.WRITE


@pytest.mark.parametrize(
    "text",
    [
        "看看这个项目的结构",
        "了解一下这份代码",
        "梳理下这个模块的逻辑",
        "这个函数在哪里定义的",
    ],
)
def test_implicit_read_requests_authorize_read(text):
    """认知类动词 + 外部实体应至少授权只读工具。"""
    policy = classify_execution_policy(text, intent=detect_intent(text))
    assert policy.level >= ExecutionLevel.READ_ONLY


@pytest.mark.parametrize(
    "text",
    [
        # 显式禁令必须压过隐含写入句式（此前「不要写文件」里的「要写文件」
        # 会被需求句式命中，把禁令读成授权）。
        "不要写文件，只在对话里给我代码",
        "别写文件",
        "不用创建文件，直接告诉我",
        "无需保存到磁盘",
        "不要使用任何工具",
        # 纯生成请求要的是代码本身，不是磁盘上的文件。
        "我想写一个 Python 脚本查询天气",
        "想写个模块",
        "写个快排给我看看",
    ],
)
def test_implicit_write_does_not_override_explicit_limits(text):
    """防过冲：显式禁令与纯生成请求不得被隐含写入模式升级。"""
    policy = classify_execution_policy(text, intent=detect_intent(text))
    assert policy.level is ExecutionLevel.ANSWER_ONLY


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("先把 utils.py 里的重复代码抽成函数，然后更新所有调用点", "plan-execute"),
        ("重构整个项目的日志模块", "plan-execute"),
        ("把所有测试文件迁移到 pytest", "plan-execute"),
        ("帮我写个函数，记得加上单元测试", "reflection"),
        ("修复这个 bug，并确保不要遗漏边界情况", "reflection"),
        ("重构所有模块，并确保每步都检查", "plan-reflection"),
        # 质量诉求分支必须优先于复杂度分支，否则 reflection 会被 plan-react 遮蔽。
        # 此用例验证：即使任务复杂度高（0.75+），只要有质量诉求+代码意图，
        # 应优先推荐 reflection 而非 plan-react。
        ("重构缓存模块的并发逻辑，记得加测试验证线程安全", "reflection"),
    ],
)
def test_engine_recommendation_matches_task_shape(text, expected):
    """范式推荐按任务结构选择，且置信度足以触发 REPL 自动切换。"""
    profile = DifficultyEstimator().estimate(text, [])
    assert profile.recommended_engine == expected
    assert profile.engine_confidence >= 0.6


@pytest.mark.parametrize("text", ["你好", "解释一下闭包", "什么是装饰器"])
def test_no_tool_turns_stay_direct(text):
    """无需工具的轮次不推荐范式——引擎循环的价值全在工具调用上。"""
    profile = DifficultyEstimator().estimate(text, [])
    assert profile.recommended_engine == "direct"
    assert profile.engine_confidence == 0.0
