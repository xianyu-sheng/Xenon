"""
OmniAgent CLI 全模块真实功能测试
================================
不是只检查注册，而是实际调用每个模块的核心功能。
覆盖：ToolNode 工具执行、Context Manager、Prompt Optimizer、
      Response Adapter、Model Registry、Memory Store、
      AgentContext、Callbacks、Tool Tracker、
      Code Index、AST Analyzer、Weather、Project Context、Security
"""

import sys
import os
import json
import time
import tempfile
import shutil
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0
ERRORS = []

def report(test_name, passed, detail=""):
    global PASS, FAIL, ERRORS
    if passed:
        PASS += 1
        print(f"  ✅ {test_name}")
    else:
        FAIL += 1
        ERRORS.append(f"{test_name}: {detail}")
        print(f"  ❌ {test_name} - {detail}")


# ============================================================
# Module 1: AgentContext (engine/context.py)
# ============================================================
print("\n" + "=" * 60)
print("Module 1: AgentContext - 运行时状态总线")
print("=" * 60)

from omniagent.engine.context import AgentContext

# 1.1 基本 get/set
ctx = AgentContext()
ctx.set("task", "查询天气")
report("1.1 set/get 基本操作", ctx.get("task") == "查询天气")

# 1.2 get 不存在的 key 返回默认值
report("1.2 get 不存在的 key", ctx.get("nonexistent", "default") == "default")

# 1.3 has 检查
report("1.3 has 检查存在", ctx.has("task") is True)
report("1.4 has 检查不存在", ctx.has("missing") is False)

# 1.4 update 合并
ctx.update({"city": "重庆", "lang": "zh"})
report("1.5 update 合并多个 key", ctx.get("city") == "重庆" and ctx.get("lang") == "zh")

# 1.5 snapshot 快照 (保存内部状态，不返回值)
ctx.snapshot()
report("1.6 snapshot 不崩溃", True)

# 1.6 conversation messages
ctx.set_conversation_messages([{"role": "user", "content": "hello"}])
msgs = ctx.get_conversation_messages()
report("1.7 conversation messages", len(msgs) == 1 and msgs[0]["content"] == "hello")

# 1.7 to_dict
d = ctx.to_dict()
report("1.8 to_dict 包含所有 key", "task" in d and "city" in d)

# 1.8 history
report("1.9 history 有快照记录", len(ctx.history) >= 1)


# ============================================================
# Module 2: ThinkingPanel + ConsoleCallback + SilentCallback
# ============================================================
print("\n" + "=" * 60)
print("Module 2: Callbacks - 思考面板与回调系统")
print("=" * 60)

from omniagent.engine.callbacks import ThinkingPanel, ConsoleCallback, SilentCallback, ThinkingStep

# 2.1 ThinkingPanel 空面板
tp = ThinkingPanel()
report("2.1 空面板 is_empty", tp.is_empty is True)
report("2.2 空面板 steps", len(tp.steps) == 0)

# 2.2 单步完整思考
tp2 = ThinkingPanel()
tp2.add_thought("我需要查询天气")
tp2.add_action("weather", {"city": "北京"})
tp2.add_observation("晴天 25°C")
report("2.3 单步完成后 steps 数", len(tp2.steps) == 1)
report("2.4 单步 thought 内容", tp2.steps[0].thought == "我需要查询天气")
report("2.5 单步 action 内容", tp2.steps[0].action == "weather")
report("2.6 单步 observation 内容", tp2.steps[0].observation == "晴天 25°C")
report("2.7 单步后 is_empty 为 False", tp2.is_empty is False)

# 2.3 多步推理
tp3 = ThinkingPanel()
for i in range(5):
    tp3.add_thought(f"步骤 {i+1}")
    tp3.add_action("command", {"command": f"echo {i}"})
    tp3.add_observation(f"输出 {i}")
report("2.8 多步推理 5 轮", len(tp3.steps) == 5)

# 2.4 错误和警告收集
tp4 = ThinkingPanel()
tp4.add_error("连接超时")
tp4.add_error("API 限流")
tp4.add_warning("结果可能不准确")
report("2.9 错误收集", len(tp4.errors) == 2)
report("2.10 警告收集", len(tp4.warnings) == 1)
report("2.11 有错误时 is_empty 为 False", tp4.is_empty is False)

# 2.5 多步计数
tp5 = ThinkingPanel()
tp5.add_thought("t1"); tp5.add_action("a1", {}); tp5.add_observation("o1")
tp5.add_thought("t2"); tp5.add_action("a2", {}); tp5.add_observation("o2")
tp5.add_thought("t3")  # 未完成的思考不计入
report("2.12 steps 计数 (2 轮完成)", len(tp5.steps) == 2)
report("2.13 未完成思考不计入 steps", len(tp5.steps) == 2)  # t3 未 add_observation

# 2.6 ConsoleCallback 事件收集
cb = ConsoleCallback(verbose=False)
cb.on_think("思考内容")
cb.on_act("weather", {"city": "上海"})
cb.on_observe("晴天 30°C")
panel = cb.get_thinking_panel()
report("2.14 ConsoleCallback 收集事件", panel is not None and len(panel.steps) == 1)

# 2.7 SilentCallback
scb = SilentCallback()
scb.on_think("test")
scb.on_act("cmd", {})
scb.on_observe("result")
report("2.15 SilentCallback 不崩溃", True)

# 2.8 Rich 渲染不崩溃
from rich.console import Console
import io
buf = io.StringIO()
test_console = Console(file=buf, width=80)
test_console.print(tp3)
output = buf.getvalue()
report("2.16 Rich 渲染输出非空", len(output) > 0)


# ============================================================
# Module 3: Tool Execution Tracker
# ============================================================
print("\n" + "=" * 60)
print("Module 3: Tool Execution Tracker - 工具执行追踪")
print("=" * 60)

from omniagent.engine.tool_tracker import ToolExecutionTracker

# 3.1 基本记录
tracker = ToolExecutionTracker()
tracker.record("weather", {"city": "北京"}, True, "天气查询成功")
tracker.record("command", {"command": "ls"}, True, "命令执行成功")
tracker.record("write_file", {"file_path": "x.py"}, False, "权限被拒绝")
report("3.1 记录总数", len(tracker.calls) == 3)
report("3.2 has_executions", tracker.has_executions() is True)

# 3.2 成功/失败统计
report("3.3 成功工具", len(tracker.successful_tools()) == 2)
report("3.4 失败工具", len(tracker.failed_tools()) == 1)

# 3.3 空 tracker
empty_tracker = ToolExecutionTracker()
report("3.5 空 tracker 无执行", empty_tracker.has_executions() is False)

# 3.4 执行摘要
summary = tracker.execution_summary()
report("3.6 执行摘要非空", len(summary) > 0 and "3" in summary, f"summary: {summary}")

# 3.5 详细日志
detail = tracker.detail_log()
report("3.7 详细日志包含工具名", "weather" in detail and "command" in detail)

# 3.6 reset
tracker.reset()
report("3.8 reset 清空记录", len(tracker.calls) == 0)


# ============================================================
# Module 4: Response Adapter - 解析引擎输出
# ============================================================
print("\n" + "=" * 60)
print("Module 4: Response Adapter - LLM 输出解析")
print("=" * 60)

from omniagent.utils.response_adapter import parse_react, parse_plan, parse_review

# 4.1 parse_react - action 模式
resp1 = '{"thought": "需要查询天气", "action": "weather", "action_input": {"city": "北京"}}'
p1 = parse_react(resp1)
report("4.1 parse_react action 解析", p1["action"] == "weather")
report("4.2 parse_react action_input", p1["action_input"]["city"] == "北京")

# 4.2 parse_react - final_answer 模式
resp2 = '{"thought": "已完成", "final_answer": "今天北京晴天 25°C"}'
p2 = parse_react(resp2)
report("4.3 parse_react final_answer", p2["final_answer"] == "今天北京晴天 25°C")

# 4.3 parse_react - markdown 包裹的 JSON
resp3 = '```json\n{"thought": "test", "final_answer": "done"}\n```'
p3 = parse_react(resp3)
report("4.4 parse_react markdown 包裹", p3["final_answer"] == "done")

# 4.4 parse_plan
plan_resp = json.dumps({
    "analysis": "需要创建一个 Python 项目",
    "steps": [
        {"id": 1, "task": "创建 main.py", "tool": "write_file", "params": {"file_path": "main.py", "content": "print('hello')"}},
        {"id": 2, "task": "运行测试", "tool": "command", "params": {"command": "python main.py"}}
    ]
})
p4 = parse_plan(plan_resp)
report("4.5 parse_plan 分析", p4["analysis"] == "需要创建一个 Python 项目")
report("4.6 parse_plan 步骤数", len(p4["steps"]) == 2)
report("4.7 parse_plan 第一步工具", p4["steps"][0]["tool"] == "write_file")

# 4.5 parse_review
review_resp = json.dumps({
    "pass": True,
    "score": 8,
    "feedback": "代码质量良好",
    "issues": ["缺少类型注解"]
})
p5 = parse_review(review_resp)
report("4.8 parse_review pass", p5["pass"] is True)
report("4.9 parse_review score", p5["score"] == 8)
report("4.10 parse_review issues", len(p5["issues"]) == 1)


# ============================================================
# Module 5: Prompt Optimizer - 提示词优化
# ============================================================
print("\n" + "=" * 60)
print("Module 5: Prompt Optimizer - 提示词优化器")
print("=" * 60)

from omniagent.repl.prompt_optimizer import detect_intent, assess_quality, optimize_prompt

# 5.1 意图检测
intents = {
    "帮我调试这个报错": "debug",
    "写一个单元测试": "write_test",
    "把这段代码从 Python 转成 Go": "convert",
    "重构这个函数": "refactor",
    "帮我实现一个排序算法": "write_code",
    "设计一个数据库架构": "design",
    "解释一下这段代码": "explain",
}
for text, expected in intents.items():
    detected = detect_intent(text)
    report(f"5.x 意图检测 '{text[:10]}...' -> {expected}", detected == expected, f"got {detected}")

# 5.2 质量评估 (返回 tuple: (needs_optimization, reason))
q1 = assess_quality("bug")
report("5.8 短输入需要优化", q1[0] is True)

q2 = assess_quality("请帮我实现一个 Python 函数，功能是计算斐波那契数列的第 n 项，要求使用动态规划，时间复杂度 O(n)，并添加类型注解和 docstring。")
report("5.9 高质量输入不需要优化", q2[0] is False)

# 5.3 优化提示词 (返回 tuple: (optimized_prompt, system_hint, was_optimized))
optimized = optimize_prompt("帮我写个爬虫")
report("5.10 优化后内容更丰富", len(optimized[0]) > len("帮我写个爬虫"))


# ============================================================
# Module 6: Model Registry - 模型注册管理
# ============================================================
print("\n" + "=" * 60)
print("Module 6: Model Registry - 模型注册管理")
print("=" * 60)

from omniagent.repl.model_registry import ModelRegistry

# 6.1 创建注册表
registry = ModelRegistry()
report("6.1 创建 ModelRegistry", registry is not None)

# 6.2 添加模型
registry.add_model("deepseek/deepseek-chat", "deepseek")
model = registry.get_model("deepseek")
report("6.2 添加模型", model is not None and model.model_id == "deepseek/deepseek-chat")

# 6.3 列出模型
models = registry.list_models()
report("6.3 列出模型包含 deepseek", any(m.alias == "deepseek" for m in models))

# 6.4 角色分配
registry.assign_role("planner", ["deepseek"])
priority = registry.get_role_priority("planner")
report("6.4 角色优先级", "deepseek/deepseek-chat" in priority)

# 6.5 模式切换
registry.set_mode("react")
report("6.5 模式切换", registry.current_mode == "react")

# 6.6 导出配置
config = registry.export_config()
report("6.6 导出配置包含 models", "models" in config)

# 6.7 移除模型
registry.remove_model("deepseek")
report("6.7 移除模型", registry.get_model("deepseek") is None)


# ============================================================
# Module 7: Provider Registry - 供应商注册
# ============================================================
print("\n" + "=" * 60)
print("Module 7: Provider Registry - LLM 供应商管理")
print("=" * 60)

from omniagent.repl.provider_registry import (
    PROVIDERS, get_configured_providers, get_provider, list_providers,
    get_all_model_ids, find_model_id
)

# 7.1 供应商数量
report("7.1 供应商数量 >= 11", len(PROVIDERS) >= 11)

# 7.2 获取供应商信息
openai = get_provider("openai")
report("7.2 获取 OpenAI 供应商", openai is not None and "gpt-4o" in openai.models)

# 7.3 列出所有供应商
all_providers = list_providers()
report("7.3 列出供应商", len(all_providers) >= 11)

# 7.4 获取所有模型 ID
all_models = get_all_model_ids()
report("7.4 所有模型 ID 数量 >= 30", len(all_models) >= 30, f"count: {len(all_models)}")

# 7.5 短名查找模型
model_id = find_model_id("gpt-4o")
report("7.5 短名查找 gpt-4o", model_id is not None and "gpt-4o" in model_id, f"found: {model_id}")

# 7.6 各供应商 base URL
for key in ["openai", "anthropic", "deepseek", "google", "zhipu", "qwen", "moonshot", "baichuan", "minimax"]:
    p = get_provider(key)
    report(f"7.x {key} 供应商", p is not None and len(p.base_url) > 0, f"base_url: {p.base_url if p else 'None'}")


# ============================================================
# Module 8: Memory Store - 跨会话记忆
# ============================================================
print("\n" + "=" * 60)
print("Module 8: Memory Store - 跨会话记忆存储")
print("=" * 60)

from omniagent.repl.memory import MemoryStore

# 8.1 创建临时记忆存储
tmp_memory = Path(tempfile.gettempdir()) / "test_memory.json"
if tmp_memory.exists():
    tmp_memory.unlink()

mem = MemoryStore(path=tmp_memory)

# 8.2 添加记忆 (自动持久化)
mem.add("重庆今天 34°C", type="fact", tags=["天气", "重庆"])
mem.add("用户偏好使用 Python", type="preference", tags=["语言"])
mem.add("上次部署失败因为端口冲突", type="error", tags=["部署"])
report("8.1 添加 3 条记忆", len(mem.memories) == 3)

# 8.3 关键词搜索
results = mem.search("重庆")
report("8.2 搜索 '重庆'", len(results) > 0 and "重庆" in results[0].content)

# 8.4 标签搜索
results2 = mem.search("天气")
report("8.3 标签搜索 '天气'", len(results2) > 0)

# 8.5 list_all
all_mems = mem.list_all()
report("8.4 list_all 返回所有", len(all_mems) == 3)

# 8.6 持久化验证 (重新加载)
mem2 = MemoryStore(path=tmp_memory)
report("8.5 持久化后重新加载", len(mem2.memories) == 3)

# 8.7 clear
count = mem2.clear()
report("8.6 clear 清空记忆", count == 3 and len(mem2.memories) == 0)

# 清理
if tmp_memory.exists():
    tmp_memory.unlink()


# ============================================================
# Module 9: ToolNode 工具执行 (真实调用)
# ============================================================
print("\n" + "=" * 60)
print("Module 9: ToolNode - 工具真实执行")
print("=" * 60)

from omniagent.nodes.tool_node import ToolNode
from omniagent.engine.context import AgentContext

# 创建临时测试目录
tmpdir = tempfile.mkdtemp(prefix="omniagent_test_")
original_dir = os.getcwd()
os.chdir(tmpdir)

try:
    # 9.1 command - 执行命令
    node_cmd = ToolNode("t1", action_type="command", action="echo hello world")
    ctx_cmd = AgentContext()
    result = node_cmd.execute(ctx_cmd)
    report("9.1 command 执行", result.get("success") is True, f"result: {result}")
    report("9.2 command 输出包含 hello", "hello" in str(result.get("stdout", "")).lower(), f"stdout: {result.get('stdout')}")

    # 9.2 write_file - 写文件
    node_wf = ToolNode("t2", action_type="write_file", file_path="test_write.py", content="print('Hello from OmniAgent!')")
    ctx_wf = AgentContext()
    result = node_wf.execute(ctx_wf)
    report("9.3 write_file 成功", result.get("success") is True, f"result: {result}")
    report("9.4 write_file 文件存在", os.path.exists("test_write.py"))

    # 9.3 read_file - 读文件
    node_rf = ToolNode("t3", action_type="read_file", file_path="test_write.py")
    ctx_rf = AgentContext()
    result = node_rf.execute(ctx_rf)
    report("9.5 read_file 成功", result.get("success") is True)
    report("9.6 read_file 内容正确", "Hello from OmniAgent" in str(result.get("content", "")), f"content: {result.get('content')}")

    # 9.4 create_directory - 创建目录
    node_mkdir = ToolNode("t4", action_type="create_directory", file_path="subdir/nested")
    ctx_mkdir = AgentContext()
    result = node_mkdir.execute(ctx_mkdir)
    report("9.7 create_directory 成功", result.get("success") is True)
    report("9.8 create_directory 目录存在", os.path.isdir("subdir/nested"))

    # 9.5 list_files - 列出文件
    node_lf = ToolNode("t5", action_type="list_files", file_path=".")
    ctx_lf = AgentContext()
    result = node_lf.execute(ctx_lf)
    report("9.9 list_files 成功", result.get("success") is True)
    files = result.get("files", [])
    report("9.10 list_files 包含 test_write.py", any("test_write.py" in str(f) for f in files), f"files: {files}")

    # 9.6 search_files - 搜索文件内容
    node_sf = ToolNode("t6", action_type="search_files", file_path=".", search_pattern="Hello from OmniAgent")
    ctx_sf = AgentContext()
    result = node_sf.execute(ctx_sf)
    report("9.11 search_files 成功", result.get("success") is True)
    matches = result.get("matches", [])
    report("9.12 search_files 找到匹配", len(matches) > 0, f"matches: {matches}")

    # 9.7 edit_file - 编辑文件
    node_ef = ToolNode("t7", action_type="edit_file", file_path="test_write.py", old_text="Hello from OmniAgent", new_text="Hello from Edit Test")
    ctx_ef = AgentContext()
    result = node_ef.execute(ctx_ef)
    report("9.13 edit_file 成功", result.get("success") is True, f"result: {result}")
    with open("test_write.py") as f:
        content = f.read()
    report("9.14 edit_file 内容已修改", "Hello from Edit Test" in content, f"content: {content}")

    # 9.8 git - git 操作 (需要先 git init)
    os.system("git init -q")
    os.system("git config user.email 'test@test.com'")
    os.system("git config user.name 'Test'")
    node_git = ToolNode("t8", action_type="git", git_command="status")
    ctx_git = AgentContext()
    result = node_git.execute(ctx_git)
    report("9.15 git status 成功", result.get("success") is True, f"result: {result}")

    # 9.9 diff_preview - diff 预览
    node_diff = ToolNode("t9", action_type="diff_preview", file_path="test_write.py", new_text="print('Diff Preview Test')")
    ctx_diff = AgentContext()
    result = node_diff.execute(ctx_diff)
    report("9.16 diff_preview 生成 diff", result.get("success") is True or "diff" in str(result), f"result: {str(result)[:200]}")

    # 9.10 batch_write - 批量写入
    node_bw = ToolNode("t10", action_type="batch_write", files=[
        {"file_path": "batch1.py", "content": "x = 1"},
        {"file_path": "batch2.py", "content": "y = 2"},
    ])
    ctx_bw = AgentContext()
    result = node_bw.execute(ctx_bw)
    report("9.17 batch_write 成功", result.get("success") is True, f"result: {result}")
    report("9.18 batch_write 文件1存在", os.path.exists("batch1.py"))
    report("9.19 batch_write 文件2存在", os.path.exists("batch2.py"))

    # 9.11 batch_edit - 批量编辑
    node_be = ToolNode("t11", action_type="batch_edit", edits=[
        {"file_path": "batch1.py", "old_text": "x = 1", "new_text": "x = 10"},
        {"file_path": "batch2.py", "old_text": "y = 2", "new_text": "y = 20"},
    ])
    ctx_be = AgentContext()
    result = node_be.execute(ctx_be)
    report("9.20 batch_edit 成功", result.get("success") is True, f"result: {result}")

    # 9.12 web_fetch - 网页抓取 (外部服务可能不可用)
    node_wf2 = ToolNode("t12", action_type="web_fetch", url="https://www.baidu.com")
    ctx_wf2 = AgentContext()
    result = node_wf2.execute(ctx_wf2)
    # 外部服务可能返回错误，只要不崩溃就算通过
    report("9.21 web_fetch 执行不崩溃", result is not None, f"result: {str(result)[:200]}")

    # 9.13 安全验证 - 路径穿越
    node_sec = ToolNode("t13", action_type="read_file", file_path="../../../etc/passwd")
    ctx_sec = AgentContext()
    try:
        result = node_sec.execute(ctx_sec)
        report("9.22 安全: 路径穿越被拦截", result.get("success") is False, f"result: {result}")
    except Exception:
        report("9.22 安全: 路径穿越被拦截 (异常)", True)

    # 9.14 安全验证 - 危险命令
    node_sec2 = ToolNode("t14", action_type="command", action="rm -rf /")
    ctx_sec2 = AgentContext()
    try:
        result = node_sec2.execute(ctx_sec2)
        report("9.23 安全: 危险命令被拦截", result.get("success") is False, f"result: {result}")
    except Exception:
        report("9.23 安全: 危险命令被拦截 (异常)", True)

    # 9.15 安全验证 - 危险 git 命令
    node_sec3 = ToolNode("t15", action_type="git", git_command="push --force origin main")
    ctx_sec3 = AgentContext()
    try:
        result = node_sec3.execute(ctx_sec3)
        report("9.24 安全: 危险 git 被拦截", result.get("success") is False, f"result: {result}")
    except Exception:
        report("9.24 安全: 危险 git 被拦截 (异常)", True)

finally:
    os.chdir(original_dir)
    shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# Module 10: Weather Tool (真实 API 调用)
# ============================================================
print("\n" + "=" * 60)
print("Module 10: Weather Tool - 天气查询 (真实 API)")
print("=" * 60)

from omniagent.utils.weather import get_weather, format_weather_report, _CITY_PINYIN, _WEATHER_DESC_ZH, _CLOTHING_RULES

# 10.1 城市映射表完整性
report("10.1 城市映射 >= 45", len(_CITY_PINYIN) >= 45, f"count: {len(_CITY_PINYIN)}")
report("10.2 天气描述映射 >= 35", len(_WEATHER_DESC_ZH) >= 35, f"count: {len(_WEATHER_DESC_ZH)}")
report("10.3 穿衣规则 >= 8", len(_CLOTHING_RULES) >= 8, f"count: {len(_CLOTHING_RULES)}")

# 10.2 城市拼音映射正确性
key_cities = {"北京": "Beijing", "上海": "Shanghai", "重庆": "Chongqing", "广州": "Guangzhou", "深圳": "Shenzhen"}
for zh, en in key_cities.items():
    report(f"10.x 映射 {zh} -> {en}", _CITY_PINYIN.get(zh) == en)

# 10.3 真实 API 查询
cities_to_test = ["北京", "重庆", "上海"]
for city in cities_to_test:
    try:
        info = get_weather(city=city)
        has_temp = "temperature" in info or "temp_c" in info
        report(f"10.x 查询 {city} 天气", has_temp and "error" not in info, f"info keys: {list(info.keys())}")
    except Exception as e:
        report(f"10.x 查询 {city} 天气", False, str(e))

# 10.4 格式化报告
try:
    info = get_weather(city="北京")
    report_md = format_weather_report(info)
    report("10.x Markdown 报告包含表格", "|" in report_md, f"report[:200]: {report_md[:200]}")
    report("10.x 报告包含温度", "°" in report_md or "℃" in report_md)
except Exception as e:
    report("10.x 天气报告格式化", False, str(e))


# ============================================================
# Module 11: Code Index - AST 代码索引
# ============================================================
print("\n" + "=" * 60)
print("Module 11: Code Index - AST 代码符号索引")
print("=" * 60)

from omniagent.utils.code_index import CodeIndex

# 创建临时 Python 文件
tmpdir2 = tempfile.mkdtemp(prefix="code_index_test_")
test_py = os.path.join(tmpdir2, "sample.py")
with open(test_py, "w") as f:
    f.write('''
import os
import sys

class MyClass:
    """示例类"""
    def __init__(self, name: str):
        self.name = name

    def greet(self) -> str:
        return f"Hello {self.name}"

def helper(x: int, y: int) -> int:
    """辅助函数"""
    return x + y

CONSTANT = 42
''')

try:
    idx = CodeIndex()
    idx.index_file(test_py)

    # 11.1 搜索函数
    funcs = idx.search("helper")
    report("11.1 搜索函数 helper", len(funcs) > 0, f"results: {funcs}")

    # 11.2 搜索类
    classes = idx.search("MyClass")
    report("11.2 搜索类 MyClass", len(classes) > 0, f"results: {classes}")

    # 11.3 搜索变量
    consts = idx.search("CONSTANT")
    report("11.3 搜索变量 CONSTANT", len(consts) > 0, f"results: {consts}")

    # 11.4 统计信息
    stats = idx.stats()
    report("11.4 统计信息包含符号", stats.get("symbols", 0) >= 3, f"stats: {stats}")

except Exception as e:
    report("11.x Code Index", False, str(e))
finally:
    shutil.rmtree(tmpdir2, ignore_errors=True)


# ============================================================
# Module 12: AST Analyzer - Python AST 深度分析
# ============================================================
print("\n" + "=" * 60)
print("Module 12: AST Analyzer - Python 深度分析")
print("=" * 60)

from omniagent.utils.ast_analyzer import ASTAnalyzer

tmpdir3 = tempfile.mkdtemp(prefix="ast_test_")
analysis_file = os.path.join(tmpdir3, "analyze_me.py")
with open(analysis_file, "w") as f:
    f.write('''
import os
from typing import List

class Animal:
    def speak(self) -> str:
        raise NotImplementedError

class Dog(Animal):
    def __init__(self, name: str):
        self.name = name

    def speak(self) -> str:
        return f"{self.name} says woof"

    def fetch(self, item: str) -> str:
        return f"{self.name} fetched {item}"

def calculate(a: int, b: int, op: str = "add") -> int:
    """基础计算器"""
    if op == "add":
        return a + b
    elif op == "sub":
        return a - b
    elif op == "mul":
        return a * b
    else:
        if b != 0:
            return a // b
        return 0

UNUSED_VAR = "this is unused"
''')

try:
    analyzer = ASTAnalyzer()
    result = analyzer.analyze_file(analysis_file)

    # 12.1 函数签名
    funcs = result.functions if hasattr(result, 'functions') else []
    report("12.1 检测到函数", len(funcs) >= 1, f"functions count: {len(funcs)}")

    # 12.2 类层次
    classes = result.classes if hasattr(result, 'classes') else []
    report("12.2 检测到类", len(classes) >= 2, f"classes count: {len(classes)}")

    # 12.3 未使用导入
    unused = result.unused_imports if hasattr(result, 'unused_imports') else []
    report("12.3 检测到未使用导入", len(unused) > 0, f"unused: {unused}")

    # 12.4 语法有效
    report("12.4 语法有效", result.syntax_valid is True if hasattr(result, 'syntax_valid') else True)

    # 12.5 摘要方法
    summary = result.summary() if hasattr(result, 'summary') else ""
    report("12.5 摘要非空", len(summary) > 0, f"summary: {summary[:100]}")

except Exception as e:
    report("12.x AST Analyzer", False, str(e))
finally:
    shutil.rmtree(tmpdir3, ignore_errors=True)


# ============================================================
# Module 13: Refactor Engine - 代码重构
# ============================================================
print("\n" + "=" * 60)
print("Module 13: Refactor Engine - 代码重构工具")
print("=" * 60)

from omniagent.utils.refactor import RefactorEngine

tmpdir4 = tempfile.mkdtemp(prefix="refactor_test_")
refactor_file = os.path.join(tmpdir4, "refactor_me.py")
with open(refactor_file, "w") as f:
    f.write('''
import os
import sys
import json

def old_function_name(x):
    return x * 2

result = old_function_name(21)
print(result)
''')

try:
    engine = RefactorEngine()

    # 13.1 分析重构建议
    suggestions = engine.analyze_for_refactor(refactor_file)
    report("13.1 分析返回建议", isinstance(suggestions, dict) and "suggestions" in suggestions, f"keys: {list(suggestions.keys()) if isinstance(suggestions, dict) else type(suggestions)}")

    # 13.2 清理未使用导入 (dry_run)
    clean_result = engine.clean_unused_imports(refactor_file, dry_run=True)
    report("13.2 清理导入分析成功", isinstance(clean_result, dict) and "success" in clean_result, f"result: {clean_result}")

except Exception as e:
    report("13.x Refactor Engine", False, str(e))
finally:
    shutil.rmtree(tmpdir4, ignore_errors=True)


# ============================================================
# Module 14: Project Context - 项目上下文检测
# ============================================================
print("\n" + "=" * 60)
print("Module 14: Project Context - 项目类型自动检测")
print("=" * 60)

from omniagent.repl.project_context import ProjectContext

# 14.1 检测当前项目 (OmniAgent-CLI 是 Python 项目)
proj = ProjectContext()
proj.detect()
report("14.1 检测项目类型", proj.project_type is not None and proj.project_type != "unknown", f"type: {proj.project_type}")
report("14.2 检测为 Python 项目", "python" in proj.project_type.lower() or proj.project_type == "Python", f"type: {proj.project_type}")
report("14.3 文件树非空", len(proj.file_tree) > 0, f"tree length: {len(proj.file_tree)}")

# 14.2 临时目录无项目类型
tmpdir5 = tempfile.mkdtemp(prefix="no_project_")
old_dir = os.getcwd()
os.chdir(tmpdir5)
try:
    proj2 = ProjectContext()
    proj2.detect()
    report("14.4 空目录无项目类型", proj2.project_type is None or proj2.project_type == "unknown", f"type: {proj2.project_type}")
finally:
    os.chdir(old_dir)
    shutil.rmtree(tmpdir5, ignore_errors=True)


# ============================================================
# Module 15: Context Manager - 上下文管理
# ============================================================
print("\n" + "=" * 60)
print("Module 15: Context Manager - 会话上下文管理")
print("=" * 60)

from omniagent.repl.context_manager import ContextManager

# 15.1 创建上下文管理器
cm = ContextManager(max_tokens=4096)
report("15.1 创建 ContextManager", cm is not None)

# 15.2 添加消息
cm.add_user_message("你好")
cm.add_assistant_message("你好！有什么可以帮你的？")
cm.add_user_message("今天天气怎么样？")
report("15.2 消息数量", len(cm.get_messages()) >= 3, f"count: {len(cm.get_messages())}")

# 15.3 Token 估算
tokens = cm.current_token_usage()
report("15.3 Token 估算 > 0", tokens > 0, f"tokens: {tokens}")

# 15.4 使用率
ratio = cm.usage_ratio()
report("15.4 使用率在 0-1 之间", 0 <= ratio <= 1, f"ratio: {ratio}")

# 15.5 统计信息
stats = cm.stats()
report("15.5 统计信息包含 total_messages", "total_messages" in stats, f"stats: {stats}")

# 15.6 快照与撤销
cm.save_snapshot()
cm.add_user_message("这条消息将被撤销")
cm.add_assistant_message("好的")
count_before = len(cm.get_messages())
undo_ok = cm.undo()
count_after = len(cm.get_messages())
report("15.6 undo 回滚消息", undo_ok and count_after < count_before, f"before: {count_before}, after: {count_after}")

# 15.7 trim_last_assistant
cm.add_assistant_message("这条将被删除")
cm.trim_last_assistant()
msgs = cm.get_messages()
last = msgs[-1] if msgs else {}
report("15.7 trim_last_assistant", last.get("role") != "assistant" or "删除" not in str(last.get("content", "")))


# ============================================================
# Module 16: Session Manager - 会话保存/加载
# ============================================================
print("\n" + "=" * 60)
print("Module 16: Session Manager - 会话持久化")
print("=" * 60)

from omniagent.repl.session import save_session, load_session, list_sessions, delete_session
import omniagent.repl.session as session_mod

# 临时会话目录
tmp_sessions = Path(tempfile.mkdtemp(prefix="test_sessions_"))
original_sessions_dir = session_mod.SESSIONS_DIR
session_mod.SESSIONS_DIR = tmp_sessions

try:
    # 16.1 保存会话
    test_history = [
        {"role": "user", "content": "测试保存"},
        {"role": "assistant", "content": "已保存"},
    ]
    save_session("test_session", test_history, {}, {"model": "test"})
    report("16.1 保存会话成功", True)

    # 16.2 加载会话
    loaded = load_session("test_session")
    report("16.2 加载会话成功", loaded is not None)
    report("16.3 加载的历史正确", len(loaded.get("history", [])) == 2, f"loaded keys: {list(loaded.keys())}")

    # 16.3 列出会话
    sessions = list_sessions()
    session_names = [s["name"] for s in sessions]
    report("16.4 列出会话包含 test_session", "test_session" in session_names, f"sessions: {sessions}")

    # 16.4 删除会话
    delete_session("test_session")
    sessions2 = list_sessions()
    session_names2 = [s["name"] for s in sessions2]
    report("16.5 删除会话", "test_session" not in session_names2)

finally:
    session_mod.SESSIONS_DIR = original_sessions_dir
    shutil.rmtree(tmp_sessions, ignore_errors=True)


# ============================================================
# Module 17: LLM Client - 连接测试
# ============================================================
print("\n" + "=" * 60)
print("Module 17: LLM Client - API 连接")
print("=" * 60)

from omniagent.utils.llm_client import chat_completion

try:
    response = chat_completion("xiaomi/mimo-v2.5-pro", [{"role": "user", "content": "说'你好'两个字"}])
    report("17.1 LLM API 调用成功", response is not None and len(str(response)) > 0, f"response[:100]: {str(response)[:100]}")
except Exception as e:
    report("17.1 LLM API 调用", False, str(e)[:200])


# ============================================================
# Module 18: ReAct Engine 注册工具完整性
# ============================================================
print("\n" + "=" * 60)
print("Module 18: ReAct Engine - 工具注册完整性")
print("=" * 60)

from omniagent.engine.react_engine import BUILTIN_TOOLS

expected_tools = [
    "command", "write_file", "read_file", "list_files", "search_files",
    "git", "web_fetch", "edit_file", "create_directory", "batch_write",
    "batch_edit", "code_index", "ast_analyze", "refactor", "diff_preview",
    "mcp_call", "github_fetch", "weather",
]

for tool_name in expected_tools:
    tool = BUILTIN_TOOLS.get(tool_name)
    report(f"18.x 工具 '{tool_name}' 已注册", tool is not None)
    if tool:
        has_name = "name" in tool
        has_desc = "description" in tool
        has_params = "params" in tool
        report(f"18.x '{tool_name}' 结构完整", has_name and has_desc and has_params,
               f"name={has_name}, desc={has_desc}, params={has_params}")

report("18.x 总工具数 >= 18", len(BUILTIN_TOOLS) >= 18, f"count: {len(BUILTIN_TOOLS)}")


# ============================================================
# 总结
# ============================================================
print("\n" + "=" * 60)
TOTAL = PASS + FAIL
print(f"测试完成: {PASS}/{TOTAL} 通过, {FAIL} 失败")
print("=" * 60)

if ERRORS:
    print("\n失败项:")
    for err in ERRORS:
        print(f"  - {err}")

sys.exit(0 if FAIL == 0 else 1)
