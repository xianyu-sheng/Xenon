"""验证 get_state 描述改动后，模型是否会主动查询义务进度。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.llm import build_llm_loop_strategy, resolve_model_config
from xenon.harness.runner import run_task
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile
from xenon.report import build_report

MODEL = "anthropic/deepseek-v4.1-flash"


def run_one(label, objective, files):
    print("=" * 74)
    print(f"【{label}】{objective[:88]}")
    print("-" * 74)

    work = Path(tempfile.mkdtemp())
    for name, content in files.items():
        (work / name).write_text(content, encoding="utf-8")

    run = run_task(
        objective,
        strategy=build_llm_loop_strategy(
            resolve_model_config([MODEL]),
            LoopProfile(plan=True, actor_plans=True, step_scope=False, step_calls=10),
        ),
        workspace=str(work),
        task_id=f"gs-{label}",
        permission=PermissionMode.AUTO_EDIT,
        finalize=True,
        max_steps=16,
    )

    tools, states = [], []
    for line in Path(run.ledger_path).read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        k = e.get("kind")
        if k == "tool_receipt":
            tools.append(e["payload"]["receipt"].get("tool", "?"))
        elif k == "state_read":
            try:
                data = json.loads(str(e["payload"].get("text") or ""))
            except ValueError:
                continue
            if "all_obligations_met" in data:
                states.append(data["all_obligations_met"])

    report = build_report(Path(run.ledger_path))
    changed = any((work / n).read_text(encoding="utf-8") != c for n, c in files.items())
    print(f"工具: {tools}")
    print(f"get_state 调用: {len(states)} 次, all_obligations_met={states}")
    print(f"gate={report['gate']['decision']}  phase={report['status']['phase']}")
    print(f"文件已改动: {changed}")
    print()


run_one(
    "纯写",
    "calc.py has a bug: div() returns None on zero. Fix calc.py so division by zero raises ValueError instead.",
    {
        "calc.py": "def div(a, b):" + chr(10) + "    return a / b" + chr(10),
        "test_calc.py": "from calc import div" + chr(10) + chr(10)
        + "def test_div():" + chr(10) + "    assert div(6, 3) == 2" + chr(10),
    },
)

run_one(
    "读写混合",
    "Review util.py, then update util.py so clamp() handles reversed bounds. Run the tests with pytest.",
    {
        "util.py": "def clamp(v, lo, hi):" + chr(10) + "    return max(lo, min(hi, v))" + chr(10),
        "test_util.py": "from util import clamp" + chr(10) + chr(10)
        + "def test_clamp_reversed():" + chr(10) + "    assert clamp(5, 10, 0) == 5" + chr(10),
    },
)
