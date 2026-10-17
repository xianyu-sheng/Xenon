# 复现 Xenon 的 SWE-bench 盲测（完整指南）

> 目的：让任何人跑出**可并排比较**的数字。  
> 本指南把协议、操作步骤、验收标准、以及**我们自己踩过的坑**全部写清，避免"跑错了还以为在比"。

---

## 1. 协议（不可改动的部分）

| 项 | 取值 | 说明 |
|---|---|---|
| 数据集 | `SWE-bench/SWE-bench_Lite`，`test` split | 官方数据 |
| 样本 | 下列 manifest 之一（**不得改动、不得换样本**） | 见 §2 |
| 引擎 | **单一 `react`** | 不是多引擎并集、不是 best-of-N |
| 模型参数 | `--reasoning-effort off` | 官方 API 必须关闭思考模式 |
| 预算 | `--max-steps 120` | 与所有历史数字一致 |
| 超时 | `--request-timeout 60 --engine-timeout 900` | 同上 |
| **盲测开关** | `XENON_EVAL_APPLY_TEST_PATCH=0` | 解题阶段**不**应用官方 test patch |
| **禁提示开关** | `XENON_EVAL_FAIL_HINT=0` | 提示词**不**含 FAIL_TO_PASS 测试名 |
| 禁止设置 | `XENON_MAX_ITERATIONS` / `XENON_MAX_TOKENS` / `XENON_CHECKPOINT_STEPS` | 会覆盖协议参数 |
| 判定 | 官方 `swebench==4.1.0` harness | 不允许自算分 |
| 重复 | 每条件 **3 次独立运行**，逐次与均值都报告 | 单次极差可达 4 例 ≈ 13pp |

## 2. 两套样本（二选一，或都跑）

| 样本 | manifest | digest（必须核对） | 抽法 |
|---|---|---|---|
| 固定 30 例 | `evals/manifests/swebench-lite-30-seed20260729.json` | `efc755a1…` | `evals/swebench_select.py --size 30 --seed 20260729` |
| 零重叠留出集 | `evals/manifests/swebench-lite-30-heldout-seed20261016.json` | `e594893c…` | 同规则 `--seed 20261016 --exclude` 原样本（池 270 例） |

> **推荐留出集**：它与我们所有调优过程零重叠，是新数字的主证据。

## 3. 环境准备

| 项 | 要求 |
|---|---|
| OS | Linux x86_64（WSL2 也可，需 Docker） |
| Docker | ≥ 27，能拉 `swebench/sweb.eval.*` 镜像；国内建议配镜像站 |
| 磁盘 | **≥ 150GB**（30 个实例镜像 60–90GB + 工作树 ~20GB） |
| 内存 | ≥ 8GB（两路并行建议 12GB+） |
| Python | 3.10–3.12 |

```bash
git clone --depth 1 https://github.com/xianyu-sheng/Xenon.git && cd Xenon
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install "swebench==4.1.0" datasets docker   # 必须是 4.1.0（5.x 模块结构不同）
```

## 4. 操作步骤（按顺序，别跳步）

### 4.1 预热镜像（**必须在判定开始前完成**）

```bash
MANIFEST=evals/manifests/swebench-lite-30-heldout-seed20261016.json
IDS=$(python -c "import json;print(' '.join(json.load(open('$MANIFEST'))['instance_ids']))")
for iid in $IDS; do
  img="swebench/sweb.eval.x86_64.${iid//__/_1776_}"
  timeout 7200 docker pull "$img" || echo "❌ $img"
done
```

### 4.2 预热 testbed 缓存（**大仓库必做**）

runtime 用 Docker SDK 的 `get_archive` 流式提取 `/testbed`（含 `.git`）。django/matplotlib 这类大仓库会**断流或超时**（我们踩过），所以先用 `docker cp` 建好缓存：

```bash
CACHE=./work/_official_source
for iid in $IDS; do
  img="swebench/sweb.eval.x86_64.${iid//__/_1776_}"
  img_id=$(docker image inspect "$img" --format '{{.Id}}')
  dir="${img_id#sha256:}"; dir="${dir:0:16}"
  dest="$CACHE/$iid/$dir/testbed"
  [ -d "$dest" ] && continue
  mkdir -p "$CACHE/$iid/$dir"
  cid=$(docker create "$img")
  timeout 1800 docker cp "$cid:/testbed" "$dest" && echo "✅ $iid"
  docker rm -f "$cid" >/dev/null
done
```

> 缓存布局必须精确：`_official_source/<instance_id>/<镜像ID前16位>/testbed`

### 4.3 运行 agent（盲测）

```bash
ARGS=(); for iid in $IDS; do ARGS+=(--instance-id "$iid"); done

XENON_EVAL_APPLY_TEST_PATCH=0 \
XENON_EVAL_FAIL_HINT=0 \
XENON_MAX_ITERATIONS= XENON_MAX_TOKENS= XENON_CHECKPOINT_STEPS= \
.venv/bin/python evals/swebench_xenon.py "${ARGS[@]}" \
  --prepared-root ./work-exp1 \
  --engines react --max-steps 120 \
  --model <your-model> --reasoning-effort off \
  --request-timeout 60 --engine-timeout 900 \
  --predictions exp1-preds.jsonl --traces exp1-traces.jsonl
```

检查：`wc -l exp1-preds.jsonl` 必须是 **30**。

### 4.4 官方判定

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite --split test \
  --predictions_path exp1-preds.jsonl \
  --run_id xenon-<your-model>-exp1 \
  --max_workers 2 --cache_level env
```

报告会写到当前目录，文件名形如 `xenon__react__<model>-....<run_id>.json`。

### 4.5 重复 3 次

**每次用全新的 `--prepared-root`**（`./work-exp2`、`./work-exp3`）。
⚠️ 不要复用同一个 prepared-root：`--resume-completed` / `.xenon_result.json` 会让第二次直接复用第一次的结果，等于没重复。

## 5. 验收标准：怎样才算"跑对了"

提交结果前逐条打勾：

- [ ] manifest 的 `digest` 与 §2 表格一致；30 个实例 ID 全部来自该 manifest
- [ ] 运行环境里 **没有** `XENON_MAX_ITERATIONS` / `XENON_MAX_TOKENS` / `XENON_CHECKPOINT_STEPS`
- [ ] `XENON_EVAL_APPLY_TEST_PATCH=0` 且 `XENON_EVAL_FAIL_HINT=0`
- [ ] **工作树里没有 test patch 提交**：
      `git -C work-exp1/<iid>/react log --oneline | grep -c "apply test_patch"` → 应为 `0`
- [ ] 提示词里没有 FAIL_TO_PASS 测试名（抽查 traces）
- [ ] 只用了 `react`，`--max-steps 120`，`--reasoning-effort off`
- [ ] `predictions.jsonl` 恰好 30 行
- [ ] 判定使用 **官方 harness（swebench 4.1.0）+ 上面的原命令**
- [ ] 报告了 `resolved / submitted(=30)`；**空补丁单独列出但计入未解出**
- [ ] 跑了 3 次，逐次 + 均值都给出；没有剔除任何实例
- [ ] 注明模型的完整名称与调用日期（模型版本变化会影响分数）

## 6. 计分口径（唯一合法算法）

```
resolved = 官方报告中的 resolved_instances
rate     = resolved / submitted        # submitted 恒为 30
空补丁   = 报告中的 empty_patch_instances（单独列出，但计入未解出）
```

**不允许**：自写测试脚本算分、剔除空补丁、剔除困难实例、换样本、取多次最好一次（best-of-N）而不声明。

## 7. 常见错误清单（我们全部踩过）

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 多引擎/并集/best-of-N | 不可比（旧版"40%"就是这个坑） | 只用 `react` 单引擎 |
| 应用 test patch（测试可见） | 虚高（实测 **+16.7pp**） | `XENON_EVAL_APPLY_TEST_PATCH=0` |
| 注入 FAIL_TO_PASS 测试名 | 泄题（v0.8.3+ 曾默认注入） | `XENON_EVAL_FAIL_HINT=0` |
| 自算分 / 剔除空补丁 | 口径不一致，数字不可信 | 官方 harness + resolved/30 |
| 沿用默认步数 | 旧版默认 3/40，预算不一致 | 显式 `--max-steps 120` |
| 设置了 `XENON_MAX_ITERATIONS` | 覆盖 120 步预算 | 运行前 unset |
| 只跑 1 次 | 噪声可达 13pp | 3 次取均值 |
| 换样本 | 与我们的数字不可比 | 用 §2 的 manifest |
| 判定期间拉镜像 | 被 harness 的 `clean_images()` 删掉 | 镜像必须在判定开始**前**就位 |
| 复用 prepared-root 做重复 | 结果被复用，等于没跑 | 每次全新 root |

## 8. 判定期间的坑（复现排查表）

| 症状 | 原因 | 处理 |
|---|---|---|
| `ReadTimeout: UnixHTTPConnectionPool ... read timeout` | 并发 prepare + 镜像解包/构建压住 Docker API | 预热镜像与缓存；给 SDK `docker.from_env(timeout=600)` |
| `Can't add file /testbed/.git/...: broken pipe` | SDK `get_archive` 提取大仓库断流 | 用 §4.2 的 `docker cp` 预热缓存 |
| 判定中途 `sweb.eval.*` 镜像消失 | harness `clean_images()` 会删"判定期间新出现"的镜像 | 判定前把镜像全部拉好 |
| `ModuleNotFoundError: swebench.harness.run_evaluation` | 装了 swebench 5.x | `pip install "swebench==4.1.0"` |
| 数据集下载失败 | 无网/被墙 | `HF_ENDPOINT=https://hf-mirror.com HF_HUB_OFFLINE=1`（先缓存） |
| `raw.githubusercontent` 卡住 | 校验 requirements 时的网络抽风 | 重试或加超时补丁 |

## 9. 结果提交模板（贴到 [#162](https://github.com/xianyu-sheng/Xenon/issues/162)）

```
Model:        <完整模型名>（调用日期 YYYY-MM-DD）
Engine:       react / max-steps 120 / reasoning off
Protocol:     blind（APPLY_TEST_PATCH=0, FAIL_HINT=0）
Manifest:     <heldout|seed20260729>（digest: <...>）
Runs:
  exp1: run_id=..., resolved=X/30, empty=Y
  exp2: run_id=..., resolved=X/30, empty=Y
  exp3: run_id=..., resolved=X/30, empty=Y
Mean:         X/30 = Z%
Artifacts:    predictions sha256=...，官方报告 JSON 附上
Deviations:   <如有，逐条列出；没有就写 none>
```

## 10. 我们会怎么处理

- 把数字**并排**写进 README 的评测表（注明模型、协议、样本），不挑数据、不隐藏失败。
- 如果协议有偏差（例如测试可见、步数不同、样本不同），会标注为"不可比变体"。
- 复现失败也欢迎开帖：把日志贴上来，我们和社区一起排查。
