# 引擎残骸清理（#202 / #338 收尾）

对象：拆除后仍留在 `xenon/engine/` 却**没有任何生产调用方**的模块。
方法：先证明机制已有 kernel 等价物，再删除；等价物没有测试的不删。

## 逐条

| 删除的模块 | 行数 | 原职责 | kernel 等价物（有测试） | 判定 |
|---|---:|---|---|---|
| `engine/task_runtime.py` | 703 | `run_with_delivery` / `finalize_verified_work`：跑一次 → 交付 → 重试 → 提交 | `LoopRuntime` 交付事务 + `catalog/finalizer.py`<br>`tests/catalog/test_finalizer.py`(8)、`tests/kernel/test_plan_runtime.py` | 已迁 |
| `engine/task_state_projection.py` | 239 | EvidenceLedger → `TaskState` 的回放桥 | `xenon/ledger/projections.py`<br>`tests/ledger/test_projections.py`(6) | 已迁 |
| `engine/execution_evidence.py` | 195 | 把工具调用压成「变更/测试/失败」摘要 + 读真实 diff | claim→fact 绑定 + 回执效果<br>`tests/kernel/test_controller.py`(19)、`tests/kernel/test_audit.py`(12) | 已迁 |
| `engine/loop_detector.py` | 304 | 循环检测（输出哈希相似、工具模式、错误重复） | `kernel/guard.py::RepeatedCallGuard`（同签名 3 次 → 第 4 次 VETO）<br>`tests/kernel/test_mechanism_parity.py`(19) | 已迁（见下） |
| `engine/evidence_gate.py` 中 6 个 Gate 类 + `default_gates()` | ~300 | 引擎交付管线上的确定性门 | 义务来自计划、覆盖由 V2 binder 判定、claim 用 `since_seq` 绑定、无证据即债务<br>`tests/kernel/test_audit.py`、`test_controller.py` | 已迁 |

保留在 `evidence_gate.py` 的：`GateVerdict`（`nodes/tool_executor`、`evidence_runtime` 使用）、`verify_file_claims`（`turn/gate` 使用）。它们是**活代码**，不是残骸。

## 关于循环检测：两条被有意放弃的语义

`loop_detector` 有三类判定，只迁了第一类：

| 判定 | 处置 | 理由 |
|---|---|---|
| 连续 3 次相同的工具调用 → 硬停 | **已迁** | `RepeatedCallGuard` 的确定性签名等价，且不会误杀 |
| 输出/思考文本「相似」（模糊匹配） | **放弃** | #323 已判定：启发式会误杀，`Think` 文本略有差异不算原地打转 |
| 错误信息重复 | **由签名覆盖** | 同样的失败调用签名相同，必然被第一类抓到 |

未迁的两类不是遗漏：内核里它们由 `max_steps` / `max_repair_attempts` 兜底——会被预算阻断，只是不会提前判定。

## 测试处置

| 测试 | 处置 |
|---|---|
| `test_loop_detector.py`(16) | 删除（被测模块已删，机制由 `test_mechanism_parity` 覆盖） |
| `test_execution_evidence.py`(5) | 删除（同上，由 kernel controller/audit 覆盖） |
| `test_evidence_gate.py`(15) | 删除（测的是已删的 5 个具体 Gate） |
| `test_fact_binding_gate.py`(10)、`test_fix_binding_gate.py`(13) | 删除（测的是已删的 Gate 类；"盲编辑拦截"由 `ToolExecutor.before_tool` 承担，`test_online_evidence_gate` 覆盖） |
| `test_evidence_resume.py` | **迁移**：保留"证据账本可随会话持久化并完整还原"，去掉引擎期回放桥（现有断言直接比较还原后的账本事件） |
| `test_evidence_runtime_facade.py`、`test_online_evidence_gate.py` | **迁移夹具**：把 `FileClaimGate()` / `FactBindingGate()` 换成局部 stub，断言不变（"门面接受任意 gate 对象"） |

## 结果

```text
xenon/engine/            43 文件 → 21 文件（全部为在役器官）
删除行数                 1741（含 evidence_gate 的 6 个 Gate 类）
pytest -q                3403 passed, 96 skipped
三道守卫                 engine-skeleton / kernel purity / docs-reference 全 OK
```
