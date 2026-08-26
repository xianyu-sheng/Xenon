"""CircuitBreaker — 每工具一个断路器（F1 / Q3 Stage 4）。

状态机：closed（正常）→ open（连败达阈值，熔断）→ half_open（冷却后试探）。
- closed：请求放行；失败累计，达 ``failure_threshold`` 转 open。
- open：``allow()`` 返回 False，直接拒绝；冷却 ``cooldown`` 秒后转 half_open。
- half_open：放行一次试探；成功转 closed，失败转 open 且 ``cooldown`` 翻倍（上限 600s）。

时钟可注入（``clock``）以支持单测；默认 ``time.monotonic``。
"""

from __future__ import annotations

import time
from typing import Callable


class CircuitBreaker:
    """单工具断路器。线程不安全（引擎单线程内调用）。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown: float = 30.0,
        max_cooldown: float = 600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._base_cooldown = cooldown
        self.max_cooldown = max_cooldown
        self._clock = clock or time.monotonic
        self.failures = 0
        self.state = self.CLOSED
        self.opened_at: float = 0.0

    def allow(self) -> bool:
        """是否放行本次调用。open 且未冷却 → False；否则 True 并按需转 half_open。"""
        if self.state == self.OPEN:
            if self._clock() - self.opened_at >= self.cooldown:
                self.state = self.HALF_OPEN
                self._probing = True
                return True  # 放行一次试探
            return False
        return True

    def record_success(self) -> None:
        """成功：重置失败计数，回到 closed。"""
        self.failures = 0
        self.state = self.CLOSED
        self.cooldown = self._base_cooldown  # 恢复基础冷却
        self._probing = False

    def record_failure(self) -> None:
        """失败：累计；达阈值转 open（half_open 下失败立即转 open 且冷却翻倍）。"""
        self.failures += 1
        if self.state == self.HALF_OPEN:
            # 试探失败 → 重新打开，冷却翻倍
            self._open(doubled=True)
            return
        if self.failures >= self.failure_threshold:
            self._open(doubled=False)

    def _open(self, *, doubled: bool) -> None:
        self.state = self.OPEN
        self.opened_at = self._clock()
        self._probing = False
        if doubled:
            self.cooldown = min(self.cooldown * 2, self.max_cooldown)


class BreakerRegistry:
    """按工具名维护独立 CircuitBreaker 的注册表。"""

    def __init__(self, **breaker_kwargs) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._breaker_kwargs = breaker_kwargs
        self._probing = False

    def get(self, tool_name: str) -> CircuitBreaker:
        b = self._breakers.get(tool_name)
        if b is None:
            b = CircuitBreaker(**self._breaker_kwargs)
            self._breakers[tool_name] = b
        return b

    def reset(self, tool_name: str | None = None) -> None:
        """重置断路器状态。

        Args:
            tool_name: 若指定，仅重置该工具的 breaker；
                      若为 None，重置所有 breaker（保留对象，只清状态）。
        """
        if tool_name:
            b = self._breakers.get(tool_name)
            if b:
                b.failures = 0
                b.state = CircuitBreaker.CLOSED
                b.cooldown = b._base_cooldown
                b._probing = False
        else:
            for b in self._breakers.values():
                b.failures = 0
                b.state = CircuitBreaker.CLOSED
                b.cooldown = b._base_cooldown
                b._probing = False


# 进程级共享注册表（四引擎默认共用，使断路状态跨 run 累积）
GLOBAL_BREAKERS = BreakerRegistry()
