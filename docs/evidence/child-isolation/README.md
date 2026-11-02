# 并行子任务的 worktree 隔离（#358 / #204 切片）

真实模型、真实仓库。复现：

```bash
cd D:/Xenon
PYTHONIOENCODING=utf-8 python docs/evidence/child-isolation/clobber_probe.py deepseek/deepseek-v4-flash
```

## 修复前：一个子任务的白干被另一个覆盖，两边都以为成功了

两个子任务并行（独立进程、各自账本），各改 `shared.py` 里**不同**的函数：

```text
clob-sub1-812f20  phase=blocked tools=2
clob-sub2-e843d4  phase=blocked tools=5

--- shared.py after ---
def one():
    return 1      ← sub1 的修改没了
def two():
    return 2
def three():
    return 333    ← 只有 sub2 的活下来

one() changed: False
three() changed: True
both survived: False
```

两个子任务共享同一个 workspace。后写的那个读到的是旧内容，写回时把前一个的修改整段覆盖。**两个账本里都没有任何记录说「你的成果被别人覆盖了」**——它们各自都以为改了文件。

`docs/design/parallel-branching.md` 早就写了「写冲突必须静态可判（target 集合不相交）」。但子任务目标是自然语言，**没有静态 target 集合可判**，所以只能靠隔离。

## 修复后：各自 worktree，patch 显式回流

```text
  apply clob-sub1-1b4a6d: True (applied) isolated=True
  apply clob-sub2-6858ca: True (applied) isolated=True

--- shared.py after applying both patches ---
def one():
    return 111
def two():
    return 2
def three():
    return 333

one() changed: True
three() changed: True
both survived: True
```

`git worktree list` 只剩主工作树，无残留目录。

## 实现要点

| 关注点 | 做法 |
|---|---|
| 隔离单位 | 每个子任务一个 `git worktree add --detach <dir> <base>`，共享对象库，零拷贝 |
| 成果形式 | 子任务结束后 `git diff --binary <base>`，落到 `<child_id>.patch`，路径进 `ChildOutcome.patch_path` |
| 合流 | `apply_patch()` 显式调用，`git apply --3way --ignore-whitespace`；**不自动合并** |
| 冲突 | 返回 `(False, reason)`，调用方开债务；第一个子任务的成果保持原样 |
| 默认 | `run_child_tasks`（并行）默认开启；单个顺序子任务保持原行为（`isolate=None`） |
| 非 git 工作区 | 不报错，`isolated=False` + `isolation_note` 说明原因，**不假装隔离** |
| 清理 | `git worktree remove --force` + `worktree prune`，失败不影响结果 |

### 两个必须踩过才知道的坑

**1. `git apply` 的上下文匹配会误报冲突。** 一个 5 行的小文件，两个子任务改不同函数，两边的 hunk 上下文都覆盖整文件 → 直接 apply 会失败。这不是语义冲突，是补丁应用的局限。解法是让 `--3way` 真正可用：**在一次性 worktree 里 commit 子任务的成果**，这样 post-image blob 存在于对象库，3-way merge 才能做。

**2. `core.autocrlf=true` 会让 `git apply` 直接拒绝。** 工作区是 CRLF、补丁是 LF，pre-image 字节比对失败。加 `--ignore-whitespace` 后 CRLF 与 LF 仓库都能正确合并——它放宽的是空白比对，不影响 `--3way` 的合并语义。

## 语义边界

- **不自动合并。** 合并冲突需要有裁定者，而裁定者不该是模型；自动合并会把「两个子任务改了同一行」变成静默胜负——正是这次 bug 的形态。
- **不因为隔离而改变证据规则。** 子任务的 receipts 仍然是子任务的；父级仍然只用自己的 `GitWorktreeProbe` 说话（`#287`）。
- **不共享 workspace 的写。** 隔离之后，父级看到的工作区只在它显式 apply 之后才变化。

## 已知残留

- 父账仍无 `batch_id`（无法表达「这批属于同一次派发」）；
- apply 的顺序由调用方决定，没有自动排序或依赖分析——这是「不做 DAG」的直接后果，也是刻意的。
