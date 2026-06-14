"""直接调用 OmniAgent 查询天气"""
import sys, io, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from omniagent.engine.react_engine import ReActEngine
from omniagent.engine.context import AgentContext
from omniagent.engine.callbacks import EngineCallback
from omniagent.repl.repl import REPL

class CB(EngineCallback):
    def __init__(self): self.tools = []; self.warnings = []
    def on_act(self, a, p): self.tools.append((a, p))
    def on_think(self, t): pass
    def on_warning(self, m): self.warnings.append(m)
    def on_step(self, *a): pass
    def on_step_done(self, *a): pass
    def on_observe(self, o): pass
    def on_finish(self, r): pass

MODEL = ["deepseek/deepseek-v4-pro"]
user_input = "你好，今天是几月几号？星期几？今天重庆天气怎么样，应该穿什么衣服？"

cb = CB()
ctx = AgentContext()
iterations = REPL._estimate_react_iterations(user_input)
print(f"自适应迭代预算: {iterations} 轮")

engine = ReActEngine(model_priority=MODEL, max_iterations=iterations, callback=cb)
t0 = time.time()
result = engine.run(user_input, context=ctx)
elapsed = time.time() - t0

print()
print("=" * 60)
print(result)
print("=" * 60)
print(f"\n工具调用: {len(cb.tools)} 次 | 警告: {len(cb.warnings)} | 耗时: {elapsed:.0f}s | 输出: {len(result)} 字符")

# 检查
issues = []
if "Beijing" in result and "Chongqing" not in result: issues.append("仍显示Beijing而非重庆")
if "重庆" not in result: issues.append("未提及重庆")
if len(result) < 50: issues.append("输出过短")
if "温度" not in result: issues.append("缺少温度信息")
if "穿衣" not in result: issues.append("缺少穿衣建议")
if issues:
    print(f"\n问题: {', '.join(issues)}")
else:
    print("\n✓ 全部检查通过")
