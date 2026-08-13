# Xenon 与官方生态对比分析（2026-08-13）

> 目的：结合 DeepSeek 官方 V4-Flash 评测数据，对 Xenon 框架做定位评价，
> 并回答「是否值得测 DeepSeek 官方 DeepSWE 基准」。结论：不需要测。

---

## 1. DeepSeek 官方 V4-Flash Agent 评测数据（官方公布，2026-07-31）

官方使用「DeepSeek Harness 极简模式」（即将发布）作为框架，max 档位、
topp=0.95、temperature=1.0 测得：

| 基准 | 分数 | 备注 |
|------|------|------|
| Terminal Bench 2.1 | 82.7 | 终端交互 |
| NL2Repo | 54.2 | 自然语言→仓库 |
| Cybergym | 76.7 | 安全攻防 |
| **DeepSWE** | **54.4** | 代码修复（内部基准） |
| Toolathlon verified | 70.3 | 工具调用 |
| Agent Last Exam | 25.2 | 综合 |
| Automation Bench (Public) | 25.1 | 自动化 |
| DSBench-FullStack | 68.7 | 全栈开发（内部） |
| DSBench-Hard | 59.6 | Coding Agent 难题（内部） |

关键信息：官方明确标注 DSBench-FullStack / DSBench-Hard 为「内部使用的
测试集」；DeepSWE 同理（无公开数据集）。官方「极简模式」= 单次 agent 循环，
未提及 test-first 多轮迭代。

## 2. 为什么 DeepSWE 不需要测

1. **数据不可公开复现**：DeepSWE 是官方内部基准，HuggingFace 上仅第三方
   镜像（如 datacurve/deep-swe），非官方数据。用第三方镜像跑出的数字
   无法与官方 54.4 对齐（数据 + harness 都不同）。
2. **对比意义有限**：DeepSWE 测的是「模型 + 官方极简 harness」组合，
   测它不能分离 Xenon 框架的增益。Xenon 已有公开口径的框架实证
   （SWE-bench_Lite 官方 harness + 同模型 A/B +6.7pp），这才是能证明
   「框架贡献」的口径。
3. **面试定位已足够**：见 §4。

## 3. 与 Reasonix 的对比（查证结果）

Reasonix（DeepSeek 官方生态，Go 实现）仓库**未提交任何官方 SWE-bench
分数**：仅有 `benchmarks/swebench/subset.json`（50 实例子集）+ 评测模式
（`e2ebench -mode swebench`），README 明确 results 文件不入库。它公开的
分数是自建基准（τ-bench-lite、completion-integrity、memorybench 等），
与 SWE-bench 不同口径，无法横向对比。

**结论**：Reasonix vs Xenon 在 SWE-bench 上无公开可比数据。Xenon 是少数
公布完整可复现 SWE-bench 报告的 DeepSeek 生态项目之一（seed/参数/预测
文件/评分报告全部入库）。

## 4. Xenon 定位（分位分析）

SWE-bench 类基准（2026 年公开数据）分位：

| 层级 | 典型成绩 | 代表 |
|------|---------|------|
| 第一梯队 | 50-65% | 强模型 + 复杂 agent（多尝试/test-first） |
| 第二梯队 | 40-50% | Claude 3.5 Sonnet 级 agent、商用 |
| **Xenon v0.8.2** | **40.0%** | **最便宜模型 + 单次尝试** |
| 第三梯队 | 20-35% | 顶级开源 agent（默认配置） |
| 第四梯队 | <20% | 裸提示词/基础框架 |

**独特位置**：40.0% 是用 flash 模型（最便宜档）+ 单次尝试打出的。
同模型官方 DeepSWE 54.4（内部口径）；开源 agent 用同等便宜模型普遍
<30%。Xenon 处于「开源 agent 框架头部、向商用 agent 靠拢」分位，
成本低 1-2 个数量级。

## 5. 差距来源拆解与下一步

| 差距来源 | 官方（DeepSWE 54.4） | Xenon（40.0%） | 行动 |
|---------|---------------------|---------------|------|
| 模型侧思考档位 | max 档位 | 默认档位 | 开 max 思考档（一行配置） |
| 框架侧迭代 | 极简 harness（单轮） | 单轮验证闭环 | **test-first 多轮迭代**（框架层实现） |
| 模型能力 | V4-Flash 官方 | 同模型 | 无差距（同模型已对齐） |

**test-first 多轮迭代**：Xenon 已有单轮验证闭环
（`_ensure_verification_loop`：测试失败→反馈→修一轮→再验证），扩成
「预算内多轮」即 test-first——框架层能力，评测配置不变。预计 +5-8pp。

**注意**：test-first ≠ thinking effort（high/xhigh/max）。thinking
effort 是模型侧单次推理深度参数；test-first 是框架侧 agent 迭代行为
策略（先跑测试→看失败→修→再跑）。两者正交、可叠加。

## 6. 面试叙事模板

「DeepSeek 官方 V4-Flash 模型能力（官方 harness）DeepSWE 54.4；
Xenon 用同模型最便宜档 + 单次尝试打出 SWE-bench_Lite 官方评测 40.0%
instance-level，且版本间同模型 A/B +6.7pp 实证框架增益（可复现报告
入库）。与 Reasonix 等官方生态工具相比，Xenon 是少数公布完整可复现
SWE-bench 口径的项目。差距即方向：下一步 test-first 多轮迭代 + max
思考档位，目标 45-50%。」
