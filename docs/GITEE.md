# Gitee 国内社区入口

Xenon 采用“GitHub 主仓库 + Gitee 国内镜像”的协作方式：

- GitHub：<https://github.com/xianyu-sheng/Xenon>，负责源码主线、Pull Request、版本源头和国际社区。
- Gitee：<https://gitee.com/xianyu-sheng123/Xenon>，提供国内网络环境下的下载入口、中文使用反馈和 Gitee 平台展示。

## Gitee 仓库资料建议

可将下面的内容直接用于 Gitee 的项目简介和标签设置：

- 项目简介：`面向 DeepSeek 的终端 AI 编程工作区：多模型路由、上下文缓存、工具安全、记忆、MCP 与 Agent Skills。`
- 分类建议：人工智能 / 开发工具 / Python
- 标签建议：`deepseek`、`ai-agent`、`coding-agent`、`mcp`、`python`、`tui`、`cli`、`prompt-cache`
- 官方主页：<https://github.com/xianyu-sheng/Xenon>

## 国内用户安装

```bash
pip install -U "git+https://gitee.com/xianyu-sheng123/Xenon.git@v0.7.3"
xenon
```

也可以下载源码进行开发安装：

```bash
git clone https://gitee.com/xianyu-sheng123/Xenon.git
cd Xenon
pip install -e ".[dev]"
```

## Issue 分工

Gitee Issues 适合中文安装问题和使用反馈；GitHub Issues 适合代码缺陷、功能设计和正式协作。涉及源码的问题会由维护者同步到 GitHub，避免两套开发历史分叉。

## 发布同步

GitHub 的标签和 Release 是版本源头。每次 GitHub 发布后，在 Gitee 创建同版本的正式 Release，并附上：

1. 对应版本标签；
2. 精简版变更说明；
3. Gitee 和 GitHub 安装命令；
4. `dist/` 中的 wheel 与 sdist（如该版本提供构建产物）。

当前版本的 Gitee Release 文案见 [`GITEE_RELEASE_v0.7.3.md`](GITEE_RELEASE_v0.7.3.md)。
