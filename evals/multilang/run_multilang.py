"""多语言 eval 运行器包装：把 RealAgent.run_task 包装为 run_real 接口。"""
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from evals.runner import RealAgent


def run_real(task, *, model, max_turns=3, workdir=None):
    """运行一个 real eval 任务。

    Args:
        task: dict 含 id/category/prompt/expected_tools/success_criteria
        model: model_id 如 "deepseek/deepseek-v4-pro"
        max_turns: 多轮次数
        workdir: 工作目录沙箱

    Returns:
        dict with success, total_tokens (real LLM usage), tool_calls, etc.
    """
    # 注册 usage tracker 收集真实 token
    from xenon.utils.llm_client import UsageTracker
    tracker = UsageTracker()
    try:
        agent = RealAgent(model=model, max_turns=max_turns, workdir=workdir)
        result = agent.run_task(task)
        # total_tokens: 实际 LLM 调用的总 token
        result["total_tokens"] = tracker.total_tokens()
        result["total_calls"] = tracker.total_calls()
        return result
    finally:
        tracker.close()
