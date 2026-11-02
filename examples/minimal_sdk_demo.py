"""最小 SDK 示例：在受约束的工作区里跑一个任务并读取判定。

运行前提：
  1. 已安装 Xenon（Releases whl 或源码 pip install -e .）；
  2. 已在 ~/.xenon/credentials.yaml 配置好可用模型的 Key，
     或在下方显式传入 model_priority 并确保对应环境变量存在。

运行：
  python examples/minimal_sdk_demo.py
"""

from pathlib import Path

import xenon


def main() -> None:
    workspace = Path.cwd()

    # 文本结果：库调用默认绑定工作区围栏，且不会产生交付提交
    answer = xenon.run_task(
        "列出当前目录下的 Python 文件，并简要说明各自用途",
        workspace=workspace,
        engine="react",
        model_priority=["deepseek/deepseek-chat"],
    )
    print("=== answer ===")
    print(answer)

    # 判定结果：模型说辞之外，harness 实际接受了吗？证据等级是什么？
    outcome = xenon.run_task_result(
        "检查当前目录是否存在 README.md，并报告结果",
        workspace=workspace,
        engine="react",
        model_priority=["deepseek/deepseek-chat"],
    )
    print("=== verdict ===")
    print("accepted:", outcome.accepted)
    print("evidence:", outcome.evidence)
    print("debt:", outcome.debt)
    print("ledger:", outcome.ledger_path)


if __name__ == "__main__":
    main()
