"""
Config Parser — YAML 工作流配置解析器。

将 xenon.yaml 配置文件解析为调度器可直接使用的节点实例字典。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from xenon.nodes.base import BaseNode
from xenon.nodes.llm_node import LLMNode
from xenon.nodes.router_node import RouterNode
from xenon.nodes.tool_node import ToolNode

logger = logging.getLogger(__name__)


def load_yaml(path: str | Path) -> dict[str, Any]:
    """加载并解析 YAML 配置文件。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"配置文件 YAML 语法错误: {path}: {e}") from e
    if data is None:
        raise ValueError(f"配置文件为空: {path}")
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误，期望 dict，收到: {type(data).__name__}")
    return data


def parse_workflow(
    config: dict[str, Any],
) -> tuple[dict[str, BaseNode], dict[str, list[str]]]:
    """
    解析工作流配置，返回 (nodes_dict, models_dict)。

    Args:
        config: 完整的 YAML 配置字典。

    Returns:
        nodes_dict: {node_id: BaseNode 实例}
        models_dict: 全局模型优先级配置，如 {"planner": ["anthropic/...", "openai/..."]}
    """
    version = config.get("version", "1.0")
    logger.info(f"解析工作流配置，版本: {version}")

    # 提取全局模型优先级
    models_config: dict[str, list[str]] = {}
    raw_models = config.get("models", {})
    if not isinstance(raw_models, dict):
        raise ValueError(
            f"models 字段格式错误，期望 dict，收到: {type(raw_models).__name__}"
        )
    for role, value in raw_models.items():
        if isinstance(value, str):
            models_config[role] = [value]
        elif isinstance(value, list):
            models_config[role] = value
        else:
            raise ValueError(f"models.{role} 格式错误: {value}")

    # 构建节点
    nodes: dict[str, BaseNode] = {}
    raw_nodes = config.get("nodes", [])
    if not raw_nodes:
        raise ValueError("配置中未定义任何 nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError(
            f"nodes 字段格式错误，期望 list，收到: {type(raw_nodes).__name__}"
        )

    for i, node_cfg in enumerate(raw_nodes):
        if not isinstance(node_cfg, dict):
            raise ValueError(
                f"第 {i + 1} 个节点配置格式错误，期望 dict，"
                f"收到: {type(node_cfg).__name__}: {node_cfg!r}"
            )
        try:
            node = _build_node(node_cfg, models_config)
        except ValueError:
            raise
        except Exception as e:
            node_id = node_cfg.get("id", f"#{i + 1}")
            raise ValueError(f"节点 {node_id} 配置错误: {e}") from e
        if node.id in nodes:
            raise ValueError(f"节点 id 重复: {node.id}")
        nodes[node.id] = node

    return nodes, models_config


def _build_node(cfg: dict[str, Any], models: dict[str, list[str]]) -> BaseNode:
    """根据单个节点配置构建对应的节点实例。"""
    node_id = cfg.get("id")
    node_type = cfg.get("type")
    output_slot = cfg.get("output_slot")
    default_next = cfg.get("next")  # YAML 中的 next 字段 -> 节点的 default_next

    if not node_id:
        raise ValueError(f"节点配置缺少 id: {cfg}")
    if not node_type:
        raise ValueError(f"节点 {node_id} 缺少 type")

    if node_type == "llm":
        return _build_llm_node(cfg, models, node_id, output_slot, default_next)
    elif node_type == "tool":
        return _build_tool_node(cfg, node_id, output_slot, default_next)
    elif node_type == "router":
        return _build_router_node(cfg, node_id, output_slot)
    else:
        raise ValueError(f"节点 {node_id} 的 type 不支持: {node_type}")


def _build_llm_node(
    cfg: dict[str, Any],
    models: dict[str, list[str]],
    node_id: str,
    output_slot: str | None,
    default_next: str | None,
) -> LLMNode:
    """构建 LLMNode，解析模型优先级。"""
    # model 字段可以是：
    #   1. "planner" — 引用全局 models 配置
    #   2. "anthropic/claude-3-5-sonnet" — 直接指定
    #   3. 列表 — 直接作为优先级
    raw_model = cfg.get("model", "")
    if isinstance(raw_model, list):
        model_priority = raw_model
    elif raw_model in models:
        model_priority = models[raw_model]
    elif "/" in raw_model:
        model_priority = [raw_model]
    else:
        raise ValueError(
            f"节点 {node_id} 的 model 字段无法解析: {raw_model}。"
            f"可用的全局角色: {list(models.keys())}"
        )

    return LLMNode(
        node_id=node_id,
        model_priority=model_priority,
        prompt=cfg.get("prompt", ""),
        output_slot=output_slot,
        system_prompt=cfg.get("system_prompt"),
        max_tokens=cfg.get("max_tokens", 4096),
        temperature=cfg.get("temperature", 0.7),
        default_next=default_next,
    )


def _build_tool_node(
    cfg: dict[str, Any],
    node_id: str,
    output_slot: str | None,
    default_next: str | None,
) -> ToolNode:
    """构建 ToolNode，支持所有 action_type。"""
    action_type = cfg.get("action_type", "command")
    action = cfg.get("action", "")

    if action_type == "command" and not action:
        raise ValueError(f"ToolNode {node_id} (command) 缺少 action")

    # 已知 key 白名单：拼写错误的 key 会静默丢失，提前报错
    known_keys = {
        "id",
        "type",
        "output_slot",
        "next",
        "action_type",
        "action",
        "file_path",
        "content",
        "cwd",
        "timeout",
        "encoding",
        "append",
        "pattern",
        "max_depth",
        "limit",
        "cursor",
        "search_pattern",
        "file_filter",
        "git_command",
        "url",
        "start_time",
        "end_time",
        "max_pages",
        "max_chars",
        "old_text",
        "new_text",
        "files",
        "edits",
        "symbol",
        "query",
        "old_name",
        "new_name",
        "refactor_action",
        "tool_name",
        "tool_args",
        "mcp_server",
        "repo",
        "github_action",
        "github_path",
        "branch",
        "city",
        "lang",
        "description",
        "python_function",
        "command_template",
        "params",
        "security_enabled",
        "start_line",
        "max_lines",
        "line",
        "column",
    }
    unknown = set(cfg) - known_keys
    if unknown:
        raise ValueError(f"ToolNode {node_id} 存在未知配置字段: {sorted(unknown)}")

    tool_kwargs = {
        k: cfg[k]
        for k in known_keys - {"id", "type", "output_slot", "next"}
        if k in cfg
    }
    return ToolNode(
        node_id=node_id,
        output_slot=output_slot,
        default_next=default_next,
        **tool_kwargs,
    )


def _build_router_node(
    cfg: dict[str, Any], node_id: str, output_slot: str | None
) -> RouterNode:
    """构建 RouterNode。"""
    rules = cfg.get("rules", [])
    if not rules:
        raise ValueError(f"RouterNode {node_id} 缺少 rules")
    return RouterNode(
        node_id=node_id,
        rules=rules,
        default_next=cfg.get("default_next"),
        output_slot=output_slot,
    )
