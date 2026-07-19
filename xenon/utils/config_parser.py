"""
Config Parser — YAML 工作流配置解析器。

将 xenon.yaml 配置文件解析为调度器可直接使用的节点实例字典。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from xenon.engine.context import AgentContext
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
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误，期望 dict，收到: {type(data)}")
    return data


def parse_workflow(config: dict[str, Any]) -> tuple[dict[str, BaseNode], dict[str, list[str]]]:
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

    for node_cfg in raw_nodes:
        node = _build_node(node_cfg, models_config)
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

    return ToolNode(
        node_id=node_id,
        action_type=action_type,
        action=action,
        file_path=cfg.get("file_path"),
        content=cfg.get("content"),
        output_slot=output_slot,
        cwd=cfg.get("cwd"),
        timeout=cfg.get("timeout", 60),
        default_next=default_next,
        encoding=cfg.get("encoding", "utf-8"),
        append=cfg.get("append", False),
        pattern=cfg.get("pattern", "*"),
        max_depth=cfg.get("max_depth", 5),
        search_pattern=cfg.get("search_pattern", ""),
        file_filter=cfg.get("file_filter", ""),
        git_command=cfg.get("git_command", "status"),
        url=cfg.get("url", ""),
        old_text=cfg.get("old_text", ""),
        new_text=cfg.get("new_text", ""),
    )


def _build_router_node(cfg: dict[str, Any], node_id: str, output_slot: str | None) -> RouterNode:
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
