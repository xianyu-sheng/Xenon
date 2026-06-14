"""直接用修复后的 PlanExecuteEngine 分析项目"""
import sys, io, time, re
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from rich.console import Console
from rich.rule import Rule
console = Console()

from omniagent.engine.plan_execute_engine import PlanExecuteEngine
from omniagent.engine.context import AgentContext
from omniagent.engine.callbacks import EngineCallback

class CB(EngineCallback):
    def __init__(self): self.tools = []; self.warnings = []
    def on_act(self, a, p): self.tools.append((a, p))
    def on_think(self, t): pass
    def on_warning(self, m): self.warnings.append(m)
    def on_step(self, sid, t, task): print(f"  [{sid}/{t}] {task[:120]}")
    def on_step_done(self, sid, ok, s):
        icon = "OK" if ok else "FAIL"
        print(f"    [{icon}] {s[:200]}")
    def on_finish(self, r): pass
    def on_observe(self, o): pass

MODEL = ["deepseek/deepseek-v4-pro"]

console.print(Rule("OmniAgent Plan-Execute 分析 D:\\语音版的日历工具"))

cb = CB()
ctx = AgentContext()
engine = PlanExecuteEngine(model_priority=MODEL, max_steps=20, callback=cb)

user_input = (
    "请你分析一下这个本地项目文件的不足以及如何优化？给出详细的分析报告\n"
    "项目路径: D:\\语音版的日历工具"
)

t0 = time.time()
result = engine.run(user_input, context=ctx)
elapsed = time.time() - t0

console.print(Rule("分析结果"))
console.print(result)
console.print()
console.print(f"[bold]统计:[/bold] {len(cb.tools)} 工具, {len(cb.warnings)} 警告, {len(result)} 字符, {elapsed:.0f}s")

# 质量检查
issues = []
if "空壳" in result: issues.append("含'空壳'表述")
if "均不存在" in result: issues.append("含'均不存在'表述")
if "无对应代码" in result: issues.append("含'无对应代码'表述")
if result.strip().startswith(("我将", "接下来", "继续")): issues.append("空洞开头")
if len(result) < 300: issues.append(f"过短({len(result)}字)")

if issues:
    console.print(f"[red]问题: {', '.join(issues)}[/red]")
else:
    console.print("[green]质量检查全部通过[/green]")
