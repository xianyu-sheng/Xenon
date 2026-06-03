"""
RouterNode — 条件路由节点。

根据 context 中的变量进行条件判断（if-else），
返回下一个要执行的节点 ID。调度器据此决定图的走向。
"""

from __future__ import annotations

import logging
import operator
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.base import BaseNode

logger = logging.getLogger(__name__)

# 支持的比较运算符
_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "contains": lambda a, b: str(b) in str(a),
    "not_contains": lambda a, b: str(b) not in str(a),
    "is_truthy": lambda a, _b: bool(a),
    "is_falsy": lambda a, _b: not bool(a),
}


class RouterNode(BaseNode):
    """
    条件路由节点。

    配置示例:
        type: router
        rules:
          - condition:
              key: "plan_complete"      # context 中的 key
              op: "=="                  # 比较运算符
              value: true               # 期望值
            next: "execute_step"        # 条件成立时跳转到的节点
          - condition:
              key: "retry_count"
              op: ">="
              value: 3
            next: "error_handler"
        default_next: "generate_plan"   # 所有规则都不匹配时的默认跳转
    """

    def __init__(
        self,
        node_id: str,
        *,
        rules: list[dict[str, Any]],
        default_next: str | None = None,
        output_slot: str | None = None,
    ) -> None:
        super().__init__(node_id, output_slot=output_slot)
        self.rules = rules
        self.default_next = default_next

    def execute(self, context: AgentContext) -> dict[str, Any]:
        """
        评估所有规则，返回下一个节点 ID。

        返回: {"next_node": str}
        """
        for rule in self.rules:
            condition = rule["condition"]
            key = condition["key"]
            op_name = condition["op"]
            expected = condition["value"]
            actual = context.get(key)

            op_func = _OPS.get(op_name)
            if op_func is None:
                raise ValueError(f"[{self.id}] 不支持的运算符: {op_name}")

            if op_func(actual, expected):
                next_node = rule["next"]
                logger.info(
                    f"[{self.id}] 规则命中: {key} {op_name} {expected} -> {next_node}"
                )
                self._write_output(context, next_node)
                return {"next_node": next_node}

        # 没有规则命中，使用默认跳转
        if self.default_next:
            logger.info(f"[{self.id}] 无规则命中，使用默认跳转: {self.default_next}")
            self._write_output(context, self.default_next)
            return {"next_node": self.default_next}

        raise RuntimeError(f"[{self.id}] 无规则命中且未配置 default_next，流程终止")
