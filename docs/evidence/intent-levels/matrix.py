"""意图分类器对常见中文任务的判定矩阵。

目的：把"写一个 hello.py 被判 read_only"从一次偶发观察变成可复现的证据表，
好判断这是分类器的 bug，还是我们对 READ_ONLY 的理解有偏差。
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"D:\Xenon")

from xenon.repl.execution_policy import ExecutionLevel  # noqa: E402
from xenon.repl.turn_contract import build_turn_contract  # noqa: E402

CASES: list[tuple[str, str]] = [
    # (输入, 人们会期望的最低级别)
    ("写一个 hello.py", "write"),
    ("创建一个 hello.py，内容是 print('hi')", "write"),
    ("帮我修改 calc.py 里的 add 函数", "write"),
    ("把 config.py 里的 DEBUG 改成 False", "write"),
    ("新建一个 tests 目录", "write"),
    ("运行 pytest", "exec"),
    ("执行 python -c \"print(1)\"", "exec"),
    ("跑一下测试", "exec"),
    ("安装 requirements.txt 里的依赖", "exec"),
    ("解释一下 Python 的 GIL", "read"),
    ("看一下 README", "read"),
    ("这个文件里有几个函数", "read"),
    ("搜索所有用到 foo 的地方", "read"),
    ("列出当前目录文件", "read"),
    # commit 在 AUTO_EDIT 下直接放行，是写不是执行；push 走独立硬规则。
    ("帮我提交代码", "write"),
    ("把这个项目 push 到 origin", "write"),
]

NAME = {
    int(ExecutionLevel.ANSWER_ONLY): "ANSWER_ONLY",
    int(ExecutionLevel.READ_ONLY): "READ_ONLY",
    int(ExecutionLevel.WRITE): "WRITE",
    int(ExecutionLevel.EXECUTE): "EXECUTE",
}

MINIMUM = {"read": 0, "write": int(ExecutionLevel.WRITE), "exec": int(ExecutionLevel.EXECUTE)}

failures: list[str] = []
print(f"{'期望至少':<10} {'实际':<12} {'判定':<5} 输入")
print("-" * 78)
for text, want in CASES:
    contract = build_turn_contract(text)
    level = int(contract.level)
    ok = level >= MINIMUM[want]
    if not ok:
        failures.append(text)
    print(
        f"{want:<10} {NAME.get(level, str(level)):<12} "
        f"{'PASS' if ok else 'FAIL':<5} {text}"
    )

print()
print(f"共 {len(CASES)} 条，失败 {len(failures)} 条")
for text in failures:
    print(f"  ✗ {text}")
raise SystemExit(1 if failures else 0)
