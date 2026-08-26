"""Low-dependency utility tools: weather and date/time."""

from __future__ import annotations

import logging
from typing import Any

from xenon.engine.context import AgentContext

logger = logging.getLogger(__name__)


class UtilityToolsMixin:
    """Implement utility tools against the legacy ToolNode host contract."""

    def _weather(self, context: AgentContext) -> dict[str, Any]:
        """查询指定城市的天气信息。"""
        city = self._resolve_template(getattr(self, "city", ""), context) or "Beijing"
        lang = self._resolve_template(getattr(self, "lang", ""), context) or "zh"

        logger.info("[%s] 查询天气: %s", self.id, city)

        try:
            from xenon.utils.weather import format_weather_report, get_weather

            info = get_weather(city, lang)
            report = format_weather_report(info)
            result = {
                "action_type": "weather",
                "city": city,
                "success": "error" not in info,
                "weather_info": info,
                "content": report,
            }
            self._write_output(context, report[:5000])
            return result
        except ImportError:
            return {
                "action_type": "weather",
                "city": city,
                "success": False,
                "content": "",
                "error": "weather 工具需要 httpx 库。请 pip install httpx",
            }
        except Exception as exc:  # noqa: BLE001 - preserve ToolNode result contract
            logger.error("[%s] 天气查询失败: %s", self.id, exc)
            result = {
                "action_type": "weather",
                "city": city,
                "success": False,
                "content": "",
                "error": str(exc),
            }
            self._write_output(context, f"天气查询失败: {exc}")
            return result

    def _datetime(self, context: AgentContext) -> dict[str, Any]:
        """获取当前日期和时间信息。"""
        from datetime import datetime

        now = datetime.now()
        weekdays_cn = [
            "星期一",
            "星期二",
            "星期三",
            "星期四",
            "星期五",
            "星期六",
            "星期日",
        ]
        date_str = f"{now.year}年{now.month}月{now.day}日"
        time_str = now.strftime("%H:%M:%S")
        weekday = weekdays_cn[now.weekday()]
        content = (
            f"📅 当前日期: {date_str} {weekday}\n"
            f"🕐 当前时间: {time_str}\n"
            f"📊 详细信息:\n"
            f"  - 年: {now.year}\n"
            f"  - 月: {now.month}\n"
            f"  - 日: {now.day}\n"
            f"  - 星期: {weekday}\n"
            f"  - 时: {now.hour}\n"
            f"  - 分: {now.minute}\n"
            f"  - 秒: {now.second}"
        )
        result = {
            "action_type": "datetime",
            "success": True,
            "content": content,
            "date": date_str,
            "time": time_str,
            "weekday": weekday,
            "year": now.year,
            "month": now.month,
            "day": now.day,
        }
        self._write_output(context, content)
        return result
