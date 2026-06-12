"""OmniAgent Core — 本地 Agent 运行时守护进程。

提供:
- CoreApp: 核心应用，管理所有运行时组件
- JSON-RPC 2.0 over TCP NDJSON IPC 协议
- AgentRunner: 执行 Agent 任务
- 事件流订阅与广播
"""

__version__ = "0.3.0"
