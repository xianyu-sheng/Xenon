# Xenon v0.7.3

Xenon v0.7.3 是一个面向 DeepSeek 的终端 AI 编程工作区，提供多模型对话、工具执行、自动路由、持久记忆和会话恢复。

## 主要能力

- DeepSeek V4 模型发现、原生工具调用与思考消息连续性。
- Cache Rails：按模型和执行契约维护追加式提示词轨道，并提供 `/cache`、`/cost` 诊断。
- direct、ReAct、Plan-Execute、Reflection、Novel 及组合执行引擎。
- 工具权限确认、超时、断路器、结构化结果和中断恢复点。
- 会话、项目本地、项目共享和用户全局四级记忆；自动候选需用户确认后写入。
- Agent Skills、MCP（stdio/HTTP/SSE）、Ark 与 OpenAI-compatible Provider。

## 安装

中国大陆网络环境优先使用 Gitee 镜像：

```bash
pip install -U "git+https://gitee.com/xianyu-sheng123/Xenon.git@v0.7.3"
xenon
```

GitHub 主仓库：<https://github.com/xianyu-sheng/Xenon/releases/tag/v0.7.3>

## 变更记录

完整变更记录见 [`CHANGELOG.md`](../CHANGELOG.md) 中的 v0.7.3 条目。
