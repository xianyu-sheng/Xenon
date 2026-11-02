# 只读越界沙箱（#378）：读放行、写拦截

## 为什么改

旧行为：`xenon/nodes/tool_node.py::_validate_path` 对**读写一视同仁**地拦越界。
agent 读不了工作区外的任何文件（上级目录的配置、兄弟仓库的 README、
`/etc/hosts`），只能靠 `XENON_EXTRA_ROOTS` 逃生——又一个「被绕过的安全措施」。

原则：**读取是免费的，写入和执行不是**。容器（真沙箱，#123）是最终方案；
这一条是它的最便宜 80%。

## 真实运行（deepseek/deepseek-v4-flash）

```text
$ python docs/evidence/read-sandbox/read_probe.py deepseek/deepseek-v4-flash
1. read outside:      accepted=True   [tool_receipt]
2. read credential:   accepted=False  [禁止读取敏感凭证文件: ...credentials.yaml]
3. write outside:     accepted=False  [policy_decision:veto]

PASS: read outside allowed, credential read blocked, write outside blocked
```

三条边界同时被真实模型走通。

## 实现

`_validate_path(file_path, for_write=...)`：

- `for_write=False`（read_file / list_files / search_files / code_index /
  ast_analyze / document_tools 等只读路径）越界**放行**——但仍走
  `_real_path_for_check()` 解析符号链接后返回 resolved 路径，调用方绝不拿
  模型给的原始路径；凭证检查（`_USER_SENSITIVE`：`.ssh`、`.gnupg`、`.aws`、
  `credentials`、`id_rsa` 等）位置无关地仍然生效。
- `for_write=True`（file_mutation 全部写路径）**零变化**：围栏 + 敏感路径检查。
- `run_tests` 的测试路径改用 `for_write=True` 校验——跑测试是**执行**不是读，
  执行必须留在沙箱内（`tests/test_run_tests_adapter.py` 锁住）。
- shell 命令不在此列（execute 级，需审批）。

## 防线层次（写越界现在是双保险）

1. **策略层**：kernel `check_intent` 拒绝工作区外 edit（`edit outside workspace`）；
2. **执行器层**：`_validate_path(for_write=True)` 抛 `SecurityError`。

## 测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_symlink_fence.py` | 写穿符号链接仍拦；读穿符号链接到**凭证**仍拦；读穿符号链接到普通文件放行；直接读越界放行 |
| `tests/test_extra_allowed_roots.py` | 读越界无需 `XENON_EXTRA_ROOTS`；写越界仍需 |
| `tests/test_tool_audit.py` | 读凭证越界拦截；读普通文件越界放行 |
| `tests/test_run_tests_adapter.py` | 执行越界测试路径仍拒绝（执行边界不动） |

全量：3750 passed, 97 skipped；ruff / 三重守卫通过。
