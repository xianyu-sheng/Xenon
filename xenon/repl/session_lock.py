"""P1-B SAAR: 会话感知路由的会话锁状态机。

防止 ReAct/Plan-Execute 多步循环中因 per-request 无状态路由导致:
- 上下文漂移(不同模型对上下文理解不一致)
- prompt cache 失效(换模型缓存全废,成本飙升)
- 风格/推理深度跳变

锁生命周期:
  UNLOCKED --(选出模型 + 检测到工具调用流)--> LOCKED
  LOCKED   --(锁定模型 failover/失联)------> UNLOCKED(下次 route 重锁到新模型)
  LOCKED   --(显式 reset / 新会话)---------> UNLOCKED
  LOCKED   --(决策漂移连续超阈值)---------> UNLOCKED

建在 AutoRouter 之上,纯内存状态(不依赖 routing_history 持久化)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class SessionLock:
    """会话粘性锁状态。

    线程安全由 AutoRouter 调用方保证(REPL 主循环单线程主导 route;
    ModelPool 自身线程安全,锁仅持有 model_id 引用)。
    """
    locked_model_id: str | None = None
    locked_tier: int = 0
    drift_count: int = 0
    lock_reason: str = ""
    locked_at: float = 0.0

    def is_locked(self) -> bool:
        return self.locked_model_id is not None

    def lock(self, model_id: str, tier: int, reason: str = "") -> None:
        self.locked_model_id = model_id
        self.locked_tier = tier
        self.lock_reason = reason
        self.drift_count = 0
        self.locked_at = time.time()

    def release(self) -> None:
        self.locked_model_id = None
        self.locked_tier = 0
        self.drift_count = 0
        self.lock_reason = ""
        self.locked_at = 0.0
