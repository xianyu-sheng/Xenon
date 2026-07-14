"""
DAGScheduler — 核心图调度器。

通过 while 循环不断执行当前节点，根据 RouterNode 的返回值获取下一跳，
直到遇到终止条件（无下一跳 / 到达 end 节点 / 超过最大步数）。
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.base import BaseNode
from omniagent.nodes.router_node import RouterNode

logger = logging.getLogger(__name__)


class DAGScheduler:
    """
    图调度器。

    使用方式:
        scheduler = DAGScheduler(nodes, start_node_id="generate_plan")
        result = scheduler.run(context)
    """

    def __init__(
        self,
        nodes: dict[str, BaseNode],
        start_node_id: str,
        *,
        max_steps: int = 20,
    ) -> None:
        if not nodes:
            raise ValueError("nodes 不能为空")
        if start_node_id not in nodes:
            raise ValueError(f"start_node_id '{start_node_id}' 不在节点列表中")

        self.nodes = nodes
        self.start_node_id = start_node_id
        self.max_steps = max_steps

    def run(self, context: AgentContext | None = None) -> dict[str, Any]:
        """
        执行工作流。

        Args:
            context: 全局上下文，若不提供则创建空的。

        Returns:
            {"status": "completed"|"max_steps_reached"|"cycle_detected", "steps": int, "context": AgentContext}
        """
        ctx = context or AgentContext()
        current_id = self.start_node_id
        steps = 0
        execution_log: list[dict[str, Any]] = []

        # v0.5.3: 循环检测 — 记录每个节点被访问的次数
        visit_counts: dict[str, int] = {}
        MAX_VISITS_PER_NODE = 5  # 同一节点最多反复进入 5 次

        logger.info(f"=== 工作流启动，起始节点: {current_id} ===")

        while current_id and steps < self.max_steps:
            node = self.nodes.get(current_id)
            if node is None:
                raise RuntimeError(f"节点 '{current_id}' 不存在于工作流中")

            # v0.5.3: 循环检测
            visit_counts[current_id] = visit_counts.get(current_id, 0) + 1
            if visit_counts[current_id] > MAX_VISITS_PER_NODE:
                logger.warning(
                    f"检测到循环: 节点 '{current_id}' 已访问 "
                    f"{visit_counts[current_id]} 次，强制终止"
                )
                execution_log.append({
                    "step": steps + 1,
                    "node": current_id,
                    "status": "cycle_detected",
                    "error": f"节点 '{current_id}' 重复执行 {visit_counts[current_id]} 次，可能存在死循环",
                })
                break

            # 保存每步快照
            ctx.snapshot()
            steps += 1
            logger.info(f"--- 步骤 {steps}: 执行节点 {node} ---")

            try:
                result = node.execute(ctx)
            except Exception as e:
                logger.error(f"节点 {current_id} 执行失败: {e}")
                execution_log.append({
                    "step": steps,
                    "node": current_id,
                    "status": "error",
                    "error": str(e),
                })
                raise

            execution_log.append({
                "step": steps,
                "node": current_id,
                "status": "success",
                "result": result,
            })

            # 决定下一跳
            current_id = self._next_hop(node, result)

        # 判断退出原因
        if steps >= self.max_steps:
            status = "max_steps_reached"
            logger.warning(f"工作流达到最大步数 {self.max_steps}，强制终止")
        elif visit_counts.get(current_id, 0) > MAX_VISITS_PER_NODE if current_id else False:
            status = "cycle_detected"
        else:
            status = "completed"
            logger.info(f"=== 工作流完成，共 {steps} 步 ===")

        return {
            "status": status,
            "steps": steps,
            "context": ctx,
            "log": execution_log,
        }

    @staticmethod
    def _next_hop(node: BaseNode, result: dict[str, Any] | None) -> str | None:
        """
        确定下一跳节点 ID。

        优先级：
        1. RouterNode 的 result["next_node"]（条件路由结果）
        2. 节点返回值中的 result["next"]（动态跳转）
        3. 节点自身的 default_next（静态连接，来自配置中的 next 字段）
        """
        if isinstance(node, RouterNode) and result:
            return result.get("next_node")

        if result and "next" in result:
            return result["next"]

        return node.default_next
