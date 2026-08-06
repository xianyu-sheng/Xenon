# SWE-bench 30 实例基线快照（2026-08-06）

本目录保存 30 实例 × 5 引擎 SWE-bench_Lite 官方评测的**基线快照**，
用于在线验证链路（Evidence Runtime）完成后的**回归对比基准**。

## 结果汇总

| 引擎 | 通过 | 评分补丁数 | 通过率 |
|------|------|-----------|--------|
| react | 9 | 21 | 42.9% |
| plan-execute | 4 | 9 | 44.4% |
| plan-react | 6 | 12 | 50.0% |
| plan-reflection | 8 | 18 | 44.4% |
| react-reflection | 11 | 23 | 47.8% |
| **合计** | **38** | **83** | **45.8%** |

- 150 单元格全部有生成记录；83 个有效补丁全部完成官方 harness 评分。
- 批次拆分：full30 原始补丁 36/66 = 54.5%；重跑恢复补丁 2/17 = 11.8%。

## 文件说明

- `swebench30_baseline_summary.json` — 汇总（每引擎通过/总数/通过率）
- `swebench30_baseline_detail.json` — 逐 (instance_id, engine) 评分明细
- `swebench30_baseline_{engine}.jsonl` — 各引擎有效预测（SWE-bench 标准评分格式）

## 回归验证方法论（重要）

基线补丁是**多模型混合产物**（deepseek-v4-pro / doubao-seed-2-0-lite /
deepseek-v4-flash / glm-5.1，经 heyroute 中转）。因此：

1. **重跑必须使用与基线相同的模型与配置**，否则结果差异无法归因于框架
   （模型变化会淹没框架变化）。
2. 建议重跑集合：rootfix_3 的 3 实例 × 5 引擎（15 单元格）或完整 15 实例，
   优先复用已完成单元格。
3. 判定标准：
   - 通过率 ≥ 基线 → 无回归
   - 通过率显著提升 → 在线验证链有效
   - 通过率下降 → 需检查 Gate 误杀（尤其 FactBindingGate 的盲写阻断）

## 生成命令（复现）

```bash
# 生成阶段（每引擎一个预测 JSONL）
python -m evals.swebench_xenon --instance-id ... --model heyroute/deepseek-v4-flash \
  --engines react --predictions ... --traces ... --prepared-root /tmp/swebench_run

# 官方评分
python3 -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite --split test \
  --predictions_path swebench30_baseline_{engine}.jsonl \
  --max_workers 1 --run_id xenon-baseline-{engine} --namespace swebench --timeout 900
```
