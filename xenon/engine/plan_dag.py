"""PlanDAG — 计划步骤依赖图 + 拓扑波次（P2-E2 / §Q4 最有亮点的部分）。

解析 plan 步骤的 ``depends_on`` 字段，构建 DAG，拓扑排序成若干 **wave**：

- 同一 wave 内的步骤互不依赖 → 可并发执行
- wave 之间严格串行（wave N 的步骤依赖 wave <N 的产物）
- 检测循环依赖并拒绝（``PlanDAGCycleError``），调用方据此回退串行

设计取舍（见审核文档 §8.1.1 / §8.1.6 / §4 阶段 2）：

1. **并发实现选 ThreadPoolExecutor 而非 asyncio**。全仓库零异步基础设施，
   且 ``chat_completion`` 是同步阻塞 HTTP；现实并发路径是用线程池包同步
   调用（httpx 阻塞调用天然适合线程化），而非 asyncio——后者需把 LLM 客户端
   整体改 async，工作量巨大且触及每个引擎。
2. **每个并发步骤持有独立的隔离 ctx + tracker**。``ToolExecutionTracker`` 与
   ``AgentContext.messages`` 均无锁（普通 dict/list），同 wave 步骤若共享会
   在 tracker 记录与 messages 追加上竞争。规避方式：并发步骤各自持有独立
   ctx/tracker（镜像 ``combined_engines._isolated_ctx``），波次结束后由主线程
   串行合并结果——合并阶段单线程，无竞争。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PlanDAGCycleError(ValueError):
    """检测到循环依赖（或自环），无法拓扑排序。"""


def _canon(value: Any) -> Any:
    """把步骤 id / 依赖引用规范化：纯数字字符串→int，其余原样。

    使 ``1`` 与 ``"1"`` 指向同一节点。
    """
    if isinstance(value, bool):
        # bool 是 int 子类，单独排除以免 True/False 被当成 1/0
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            try:
                return int(s)
            except ValueError:
                return value
    return value


class PlanDAG:
    """计划步骤的有向无环图。

    Args:
        steps: ``parse_plan`` 返回的 ``steps`` 列表，每个步骤含 ``id`` 与
            可选的 ``depends_on``（步骤 id 列表/标量）。

    Raises:
        ValueError: 步骤 id 重复（规划错误，调用方应回退串行）。
    """

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = list(steps)
        self._ids: list[Any] = []          # 保序的规范化 id 列表
        self._by_id: dict[Any, dict[str, Any]] = {}
        self._deps: dict[Any, list[Any]] = {}  # id -> 规范化依赖 id 列表（仅含已知节点）
        self._has_edges: bool = False
        self._build()

    # ── 构建 ──────────────────────────────────────────────────
    def _build(self) -> None:
        known: set[Any] = set()
        for s in self.steps:
            sid = _canon(s.get("id"))
            if sid in known:
                raise ValueError(f"计划步骤 id 重复: {sid!r}（规划错误，回退串行）")
            known.add(sid)
            self._ids.append(sid)
            self._by_id[sid] = s

        for s in self.steps:
            sid = _canon(s.get("id"))
            raw_deps = s.get("depends_on") or []
            if not isinstance(raw_deps, (list, tuple)):
                raw_deps = [raw_deps]
            norm: list[Any] = []
            seen: set[Any] = set()
            for d in raw_deps:
                cd = _canon(d)
                if cd in seen:
                    continue  # 去重
                seen.add(cd)
                if cd not in self._by_id:
                    # 未知依赖：规划错误，丢弃该边并告警（不连坐整张图）
                    logger.warning("步骤 %r 声明了未知依赖 %r，已忽略", sid, cd)
                    continue
                norm.append(cd)
            self._deps[sid] = norm
            if norm:
                self._has_edges = True

    # ── 查询 ──────────────────────────────────────────────────
    @property
    def has_edges(self) -> bool:
        """是否存在任何依赖边（无边的计划拓扑退化为单一波次）。"""
        return self._has_edges

    @property
    def step_ids(self) -> list[Any]:
        """保序的步骤 id 列表。"""
        return list(self._ids)

    def dependency_map(self) -> dict[Any, list[Any]]:
        """返回 {id: [依赖 id, ...]}（仅含已知节点，已去重）。"""
        return {sid: list(deps) for sid, deps in self._deps.items()}

    def step(self, sid: Any) -> dict[str, Any]:
        """按 id 取步骤原始 dict。"""
        return self._by_id[_canon(sid)]

    def waves(self) -> list[list[Any]]:
        """Kahn 拓扑排序，返回波次列表（每波为步骤 id 列表，保持原顺序）。

        Raises:
            PlanDAGCycleError: 剩余节点均非零入度（存在环/自环）。
        """
        in_deg = {sid: len(self._deps[sid]) for sid in self._ids}
        dependents: dict[Any, list[Any]] = {sid: [] for sid in self._ids}
        for sid in self._ids:
            for d in self._deps[sid]:
                dependents[d].append(sid)

        waves: list[list[Any]] = []
        remaining: set[Any] = set(self._ids)
        # 按原顺序遍历以保证确定性
        order = list(self._ids)

        while remaining:
            ready = [sid for sid in order if sid in remaining and in_deg[sid] == 0]
            if not ready:
                stuck = sorted(remaining, key=lambda x: str(x))
                raise PlanDAGCycleError(
                    f"计划存在循环依赖，无法拓扑排序，涉及步骤: {stuck}"
                )
            waves.append(ready)
            for sid in ready:
                remaining.discard(sid)
                for dep in dependents[sid]:
                    if dep in remaining:
                        in_deg[dep] -= 1
        return waves
