# 贡献指南

感谢你关注 Xenon。Xenon 是一个面向终端和编程任务的开源 AI Agent 工作区，欢迎提交问题、文档改进和代码贡献。

## 反馈渠道

- Gitee Issues：中文安装问题、使用反馈和复现信息。
- GitHub Issues：代码缺陷、功能设计和跨平台讨论。
- GitHub Pull Requests：正式代码合并入口。

如果 Gitee Issue 涉及代码缺陷，维护者可能会将它关联或迁移到 GitHub，以保持单一开发主线。

## 提交 Issue

请尽量提供：

- Xenon 版本（例如 `xenon --version` 的输出）。
- Python 版本和操作系统。
- 使用的模型或 Provider（不要粘贴 API Key）。
- 最小复现步骤、实际结果和期望结果。
- 相关命令、错误信息和经过脱敏的日志。

## 提交 Pull Request

1. 从 GitHub 的 `main` 分支创建分支。
2. 保持改动聚焦，并为行为变化补充测试或文档。
3. 在本地运行：

   ```bash
   ruff check xenon tests
   pytest -q -m "not live"
   ```

4. 在 PR 描述中说明背景、方案和验证结果。

请不要提交凭证、个人配置、构建产物或供应商真实响应。Gitee 镜像不作为独立开发分支使用。
