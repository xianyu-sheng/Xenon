# Git CI Hook 使用说明

## 功能说明

自动检测每次提交后的 CI 测试状态，如果失败则显示错误并提供修复选项。

## 安装位置

`.git/hooks/post-commit`

## 工作流程

```
git commit -m "message"
    ↓
[CI Hook] 检测到新提交
    ↓
检查是否已推送到远程
    ↓
等待 CI 启动（最多 30s）
    ↓
等待 CI 完成（最多 5 分钟）
    ↓
显示结果：✓ 通过 或 ✗ 失败
```

## 使用示例

### 正常提交

```bash
$ git commit -m "feat: add new feature"
[CI Hook] 检测到新提交: 5ac5e5c0
[CI Hook] 提交尚未推送，请先推送: git push

$ git push origin main
To https://github.com/user/repo.git
   abc123..5ac5e5c  main -> main

# Hook 在推送后自动检查 CI
[CI Hook] 检测到新提交: 5ac5e5c0
[CI Hook] 等待 CI 启动...
[CI Hook] CI 已启动 (Run ID: 12345)
[CI Hook] 等待 CI 完成（最多 5 分钟）...
[CI Hook] CI 运行中... 30s / 300s
[CI Hook] ✓ CI 测试通过！
```

### CI 失败时

```bash
$ git commit -m "feat: add broken feature"
$ git push origin main

[CI Hook] 检测到新提交: bad123
[CI Hook] 等待 CI 启动...
[CI Hook] CI 已启动 (Run ID: 12346)
[CI Hook] 等待 CI 完成...
[CI Hook] ✗ CI 测试失败！

失败的测试：
F401 [*] `module.SomeClass` imported but unused
  --> file.py:10:5

修复选项：
1. 查看完整日志: gh run view 12346 --log-failed
2. 在浏览器中查看: gh run view 12346 --web
3. 运行本地 Ruff: ruff check xenon tests evals --fix
```

## 修复流程

当 CI 失败时：

### 1. 查看错误

```bash
gh run view 12346 --log-failed
```

### 2. 本地修复

```bash
# 运行 Ruff 自动修复
ruff check xenon tests evals --fix

# 或手动修复代码
vim path/to/file.py
```

### 3. 重新提交

```bash
# 如果是小修复，可以 amend
git add -u
git commit --amend --no-edit
git push --force-with-lease

# 或者新提交
git add -u
git commit -m "fix: resolve CI errors"
git push
```

## Hook 配置

### 跳过 CI 检查

如果需要跳过 CI 检查（例如紧急修复）：

```bash
# 临时禁用
chmod -x .git/hooks/post-commit
git commit -m "message"
git push

# 重新启用
chmod +x .git/hooks/post-commit
```

### 调整超时时间

编辑 `.git/hooks/post-commit`：

```bash
# CI 启动超时（默认 30 秒）
MAX_WAIT=30

# CI 完成超时（默认 5 分钟）
MAX_WAIT=300
```

## 依赖要求

- ✅ `gh` CLI (GitHub CLI)
- ✅ `jq` (JSON 处理器)
- ✅ Git 远程仓库

### 安装依赖

```bash
# macOS
brew install gh jq

# Ubuntu/Debian
sudo apt install gh jq

# Arch Linux
sudo pacman -S github-cli jq
```

### 验证依赖

```bash
command -v gh && echo "✓ gh installed"
command -v jq && echo "✓ jq installed"
```

## Hook 状态码

- `0`: CI 通过或跳过检查
- `1`: CI 失败

## 常见问题

### Q: Hook 说"gh CLI 未安装"

**A**: 安装 GitHub CLI：
```bash
# macOS
brew install gh

# Linux
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list
sudo apt update && sudo apt install gh
```

### Q: Hook 说"jq: command not found"

**A**: 安装 jq：
```bash
sudo apt install jq   # Ubuntu/Debian
brew install jq       # macOS
```

### Q: CI 运行时间超过 5 分钟

**A**: 正常情况。Hook 会提示手动检查：
```bash
gh run view <RUN_ID>
```

### Q: 想要完全禁用 Hook

**A**: 删除或重命名 Hook：
```bash
mv .git/hooks/post-commit .git/hooks/post-commit.disabled
```

### Q: Hook 在某些情况下不工作

**A**: Hook 在以下情况会跳过检查：
- 没有远程仓库
- 提交尚未推送
- gh CLI 不可用
- 不是在 GitHub 仓库

## 高级用法

### 在其他 Hook 中使用

可以在 `pre-push` 或其他 Hook 中调用相同的检查逻辑：

```bash
# .git/hooks/pre-push
#!/usr/bin/env bash
source .git/hooks/post-commit
```

### 集成到 CI/CD

Hook 的逻辑也可以用于 CI/CD 流程中的健康检查。

### 自定义通知

修改 Hook 添加通知（如 Slack、邮件）：

```bash
if [ "$CONCLUSION" = "failure" ]; then
    # 发送通知
    curl -X POST https://hooks.slack.com/... \
        -d "{\"text\": \"CI failed for commit $COMMIT_SHA\"}"
fi
```

## 最佳实践

1. **本地先测试**: 提交前运行 `ruff check` 和 `pytest`
2. **小步提交**: 每次提交一个小功能，方便定位问题
3. **及时修复**: CI 失败后立即修复，不要累积
4. **查看日志**: 仔细阅读 CI 错误日志，理解问题根源
5. **使用 amend**: 小修复使用 `git commit --amend` 保持历史整洁

## 相关命令

```bash
# 查看最近的 CI 运行
gh run list --limit 5

# 查看特定运行的详情
gh run view <RUN_ID>

# 查看失败的日志
gh run view <RUN_ID> --log-failed

# 在浏览器中查看
gh run view <RUN_ID> --web

# 重新运行 CI
gh run rerun <RUN_ID>
```

## 贡献

如果你有改进建议，欢迎提交 PR 或 Issue！
