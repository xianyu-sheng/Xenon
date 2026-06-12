"""JSON-RPC 2.0 命令和响应类型定义 — Pydantic 强类型。

借鉴 KamaClaude 的类型化 IPC 协议设计:
- 每个命令/响应都是独立的 Pydantic BaseModel
- 使用 Literal type 字段做判别联合
- 所有字段都有明确的类型注解
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field


# ── 基础 IPC 类型 ───────────────────────────────────────────

class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 请求。"""
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 响应。"""
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    result: Any = None
    error: dict[str, Any] | None = None


class JsonRpcNotification(BaseModel):
    """JSON-RPC 2.0 通知（无 id，不需要响应）。"""
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


# ── 命令类型 ────────────────────────────────────────────────

class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str = "unknown"


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str
    mode: str = "react"  # direct | react | plan-execute | reflection


class AgentRunResult(BaseModel):
    run_id: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str] = Field(default_factory=list)  # fnmatch 模式
    scope: str = "global"  # "global" | "run:<run_id>"


class EventSubscribeResult(BaseModel):
    subscription_id: str


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: str = "chat"  # "chat" | "one_shot"
    title: str = ""


class SessionCreateResult(BaseModel):
    session_id: str


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str
    mode: str = "react"


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    decision: str  # "allow_once" | "always_allow" | "deny_once" | "always_deny"


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SetModelCommand(BaseModel):
    type: Literal["set_model"] = "set_model"
    model_ids: list[str] = Field(default_factory=list)


class SetModelResult(BaseModel):
    ok: bool = True
    current_models: list[str] = Field(default_factory=list)


# ── 命令联合类型 ────────────────────────────────────────────

Command = Annotated[
    PingCommand
    | AgentRunCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionSendMessageCommand
    | SessionGetHistoryCommand
    | SessionCloseCommand
    | SessionCompactCommand
    | PermissionRespondCommand
    | SetModelCommand,
    Discriminator("type"),
]

# 命令 type → 类映射
COMMAND_MAP: dict[str, type] = {
    "core.ping": PingCommand,
    "agent.run": AgentRunCommand,
    "event.subscribe": EventSubscribeCommand,
    "session.create": SessionCreateCommand,
    "session.send_message": SessionSendMessageCommand,
    "session.get_history": SessionGetHistoryCommand,
    "session.close": SessionCloseCommand,
    "session.compact": SessionCompactCommand,
    "permission.respond": PermissionRespondCommand,
    "set_model": SetModelCommand,
}
