# Xenon 外部 Benchmark 运行边界

外部 benchmark 与 Xenon 内部回归评测是两层结果，不能互相替代。

| Benchmark | 适用能力 | 当前状态 | Xenon 接入方式 |
| --- | --- | --- | --- |
| SWE-bench / SWE-bench Lite | 真实 GitHub issue 修复 | 官方 Lite test split、`swebench==4.1.0` 和 Docker harness 已安装；官方 smoke 已通过 | `evals/swebench_xenon.py` 只生成标准 prediction；官方 harness 独立应用 patch、运行 test patch 和判定 resolved |
| Terminal-Bench | 终端、shell、环境交互 | 当前 20 个 fixture 是本地可运行子集，不宣称官方分数 | 将 Terminal-Bench task adapter 转换为 fixture + 命令断言 |
| AgentBench | OS/DB/Web/知识图谱/购物等环境 Agent | 未安装其环境服务 | 每个环境单独 adapter，记录环境状态和最终状态，不合并成单一工具调用分数 |
| GAIA | 多步骤个人助理任务 | 未下载任务与附件 | 需要文件/网络工具沙箱和人工/程序化答案校验 |
| WebArena | 浏览器 DOM 交互 | 当前环境未安装浏览器环境 | 需要 BrowserGym/WebArena 服务、浏览器隔离和页面状态断言 |
| τ-bench / ToolBench | 工具选择、参数和顺序 | Xenon 内部工具生命周期套件可先覆盖同类信号 | 接入官方工具 API，单独统计 tool policy、参数、顺序和最终状态 |

当前可以可靠运行 Xenon 本地两套诊断，以及 SWE-bench Lite 官方 harness。首个真实 Xenon ReAct smoke 的原始预测、官方报告和测试日志保存在 `evals/reports/official/swebench-lite/2026-07-28-react-smoke/`。单实例 smoke 只证明端到端接入正确，不代表 300 题完整 split 的总体正确率。其余外部 benchmark 只有在对应官方数据集、环境服务和判定器就绪后才会生成官方结果，不能用本地 proxy 冒充。
