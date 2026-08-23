# Xenon Git Hook - 自动安装系统

## 功能特性

✅ **自动安装** - 每次 `git commit` 后自动更新 Xenon  
✅ **跨会话复用** - 项目级虚拟环境，无需重复安装  
✅ **智能缓存** - 仅在代码变更时重新安装  
✅ **并发安全** - 文件锁机制防止冲突  
✅ **可编辑模式** - 支持开发调试

## 安装

Git hook 已自动配置：
```bash
scripts/post-commit-xenon  # hook 脚本
.git/hooks/post-commit     # 符号链接
```

## 使用方式

### 1. 自动触发（推荐）

每次提交都会自动检查并安装：
```bash
git commit -m "your changes"
# [Xenon Hook] ✓ Xenon 0.8.5 已安装
```

### 2. 手动触发

```bash
.git/hooks/post-commit
```

### 3. 激活虚拟环境

```bash
source .xenon-venv/bin/activate
xenon --version
# xenon 0.8.5
```

### 4. 退出虚拟环境

```bash
deactivate
```

## 工作原理

### 智能缓存机制

Hook 会检查以下条件决定是否需要重新安装：

1. **虚拟环境不存在** → 创建并安装
2. **版本文件不存在** → 重新安装
3. **代码有变更** → 检查 `xenon/` 或 `pyproject.toml` 是否修改
   - 有变更 → 重新安装
   - 无变更 → 跳过（跨会话复用）

### 并发安全

使用文件锁 `.xenon-install.lock` 防止多个进程同时安装：
- 获取锁超时：30 秒
- 如果挂起：手动删除 `rm -rf .xenon-install.lock`

### 版本追踪

`.xenon-venv/.xenon-version` 记录当前安装对应的 commit hash，
用于判断代码是否变更。

## 文件结构

```
Xenon/
├── .xenon-venv/              # 项目级虚拟环境
│   ├── bin/xenon             # Xenon 可执行文件
│   ├── .xenon-version        # 版本追踪文件
│   └── ...
├── .xenon-install.lock       # 安装锁（临时）
├── scripts/
│   └── post-commit-xenon     # Hook 脚本
└── .git/hooks/
    └── post-commit           # 符号链接 → ../../scripts/post-commit-xenon
```

## 示例输出

### 首次安装
```bash
$ git commit -m "test"
[Xenon Hook] 检测到新提交: 58d71031
[Xenon Hook] 创建项目级虚拟环境: /home/user/Xenon/.xenon-venv
[Xenon Hook] 安装 Xenon (可编辑模式)...
[Xenon Hook] ✓ Xenon 0.8.5 已安装
[Xenon Hook] 虚拟环境: /home/user/Xenon/.xenon-venv
[Xenon Hook] 激活命令: source /home/user/Xenon/.xenon-venv/bin/activate
[Xenon Hook] 运行命令: xenon
[Xenon Hook] 可执行文件: /home/user/Xenon/.xenon-venv/bin/xenon
```

### 跨会话复用（无变更）
```bash
$ git commit -m "docs update"
[Xenon Hook] 检测到新提交: a1b2c3d4
[Xenon Hook] 代码未变更，跳过安装
[Xenon Hook] ✓ Xenon 已是最新版本 (跨会话复用)
[Xenon Hook] 激活: source /home/user/Xenon/.xenon-venv/bin/activate
```

### 代码变更（重新安装）
```bash
$ git commit -m "fix: tool executor"
[Xenon Hook] 检测到新提交: e5f6g7h8
[Xenon Hook] 检测到代码变更，需要重新安装
[Xenon Hook] 安装 Xenon (可编辑模式)...
[Xenon Hook] ✓ Xenon 0.8.5 已安装
```

## 故障排除

### 1. 锁超时

```bash
[Xenon Hook] ✗ 获取锁超时，可能有其他安装进程挂起
[Xenon Hook] 可以手动删除: rm -rf /home/user/Xenon/.xenon-install.lock
```

**解决方法：**
```bash
rm -rf .xenon-install.lock
.git/hooks/post-commit  # 重新运行
```

### 2. 安装失败

检查日志：
```bash
cat /tmp/xenon-install.log
```

### 3. 虚拟环境损坏

重建虚拟环境：
```bash
rm -rf .xenon-venv
.git/hooks/post-commit
```

### 4. Python 版本问题

指定 Python 版本：
```bash
PYTHON=python3.12 .git/hooks/post-commit
```

## 配置

### 环境变量

- `PYTHON` - 指定 Python 解释器（默认：`python3`）

示例：
```bash
export PYTHON=python3.12
git commit -m "use specific python"
```

### 禁用 Hook（临时）

```bash
git commit --no-verify -m "skip hook"
```

### 卸载 Hook

```bash
rm .git/hooks/post-commit
```

## 优势

相比全局安装的优势：

1. **隔离性** - 项目级虚拟环境，不影响系统
2. **可复现** - 开发和生产使用相同版本
3. **自动化** - 无需手动 `pip install`
4. **高效** - 智能缓存，跨会话复用
5. **安全** - 文件锁防止并发冲突

## 与全局安装对比

| 特性 | Git Hook 安装 | 全局安装 |
|------|--------------|----------|
| 自动更新 | ✅ | ❌ |
| 环境隔离 | ✅ | ❌ |
| 跨会话复用 | ✅ | ✅ |
| 可编辑模式 | ✅ | ❌ |
| 并发安全 | ✅ | N/A |
| 开发友好 | ✅ | ❌ |

## 常见问题

**Q: Hook 会拖慢 commit 速度吗？**  
A: 不会。智能缓存机制下，无代码变更时只需 <0.1s 检查。

**Q: 多个终端同时 commit 会冲突吗？**  
A: 不会。文件锁机制保证只有一个进程安装，其他进程等待。

**Q: 可以用于 CI/CD 吗？**  
A: 可以，但 CI 建议用 `pip install -e .` 直接安装。

**Q: 虚拟环境会占用多少空间？**  
A: 约 50-100 MB（包含依赖）。

## 进阶用法

### 开发模式

可编辑模式下，代码修改立即生效：
```bash
# 修改 xenon/engine/executor.py
source .xenon-venv/bin/activate
xenon  # 使用修改后的代码
```

### 多项目共享

每个 Xenon 项目都有独立虚拟环境：
```bash
~/Xenon/.xenon-venv          # 主开发环境
~/my-fork/.xenon-venv        # Fork 环境
~/experiment/.xenon-venv     # 实验分支
```

### 清理所有虚拟环境

```bash
find . -name ".xenon-venv" -type d -exec rm -rf {} +
```

---

**维护者：** Xenon 项目  
**版本：** 1.0  
**最后更新：** 2026-08-23
