"""
WeatherTool — 天气查询工具。

功能：
1. 查询指定城市的实时天气信息
2. 获取当日温度范围
3. 根据温度给出穿衣建议

使用 wttr.in API，配合城市拼音映射确保中文城市查询准确。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── 常见城市中文名 → 拼音/英文映射 ──────────────────────────
# 确保 wttr.in 能正确识别中国城市
_CITY_PINYIN: dict[str, str] = {
    # 直辖市
    "北京": "Beijing", "上海": "Shanghai", "天津": "Tianjin", "重庆": "Chongqing",
    # 省会/主要城市
    "广州": "Guangzhou", "深圳": "Shenzhen", "成都": "Chengdu", "杭州": "Hangzhou",
    "武汉": "Wuhan", "南京": "Nanjing", "西安": "Xian", "长沙": "Changsha",
    "沈阳": "Shenyang", "哈尔滨": "Harbin", "济南": "Jinan", "郑州": "Zhengzhou",
    "昆明": "Kunming", "福州": "Fuzhou", "厦门": "Xiamen", "合肥": "Hefei",
    "南昌": "Nanchang", "贵阳": "Guiyang", "兰州": "Lanzhou", "太原": "Taiyuan",
    "石家庄": "Shijiazhuang", "南宁": "Nanning", "海口": "Haikou", "呼和浩特": "Hohhot",
    "乌鲁木齐": "Urumqi", "拉萨": "Lhasa", "银川": "Yinchuan", "西宁": "Xining",
    "大连": "Dalian", "青岛": "Qingdao", "苏州": "Suzhou", "无锡": "Wuxi",
    "宁波": "Ningbo", "温州": "Wenzhou", "东莞": "Dongguan", "佛山": "Foshan",
    "珠海": "Zhuhai", "桂林": "Guilin", "三亚": "Sanya", "香港": "Hong Kong",
    "澳门": "Macau", "台北": "Taipei",
}

# ── 天气描述英文 → 中文映射 ──────────────────────────────────
_WEATHER_DESC_ZH: dict[str, str] = {
    "sunny": "晴天",
    "clear": "晴朗",
    "partly cloudy": "多云",
    "cloudy": "阴天",
    "overcast": "阴天",
    "mist": "薄雾",
    "fog": "雾",
    "light rain": "小雨",
    "moderate rain": "中雨",
    "heavy rain": "大雨",
    "light drizzle": "毛毛雨",
    "drizzle": "小雨",
    "patchy rain possible": "可能有零星小雨",
    "patchy rain nearby": "附近有零星小雨",
    "thundery outbreaks possible": "可能有雷阵雨",
    "light rain shower": "小阵雨",
    "moderate or heavy rain shower": "中到大阵雨",
    "torrential rain shower": "暴雨",
    "light snow": "小雪",
    "moderate snow": "中雪",
    "heavy snow": "大雪",
    "blizzard": "暴风雪",
    "light sleet": "雨夹雪",
    "moderate or heavy sleet": "大雨夹雪",
    "patchy light drizzle": "零星小雨",
    "patchy light rain": "零星小雨",
    "patchy moderate rain": "零星中雨",
    "patchy heavy rain": "零星大雨",
    "patchy snow possible": "可能有零星小雪",
    "patchy sleet possible": "可能有雨夹雪",
    "patchy freezing drizzle possible": "可能有冻毛毛雨",
    "freezing fog": "冻雾",
    "ice pellets": "冰粒",
    "light rain with thunder": "雷阵雨",
    "moderate or heavy rain with thunder": "强雷阵雨",
    "haze": "霾",
    "smoke": "烟雾",
}


# ── 穿衣建议配置 ──────────────────────────────────────────
_CLOTHING_RULES: list[tuple[int, str, str]] = [
    (35, "酷暑天气", "非常炎热！建议穿轻薄透气的短袖、短裤、裙子，注意防晒防暑，多补充水分。"),
    (30, "高温天气", "炎热天气，建议穿短袖、短裤、薄裙子等清凉透气的衣服，做好防晒。"),
    (25, "温暖天气", "温度适宜偏热，建议穿短袖、薄T恤、短裤或裙子。"),
    (20, "舒适天气", "温度舒适，建议穿长袖衬衫、薄外套、针织衫或轻薄卫衣。"),
    (15, "微凉天气", "天气微凉，建议穿薄毛衣、卫衣、夹克外套或薄风衣。"),
    (10, "凉爽天气", "天气凉爽，建议穿毛衣、厚外套、风衣或薄羽绒服。"),
    (5,  "寒冷天气", "天气寒冷，建议穿厚毛衣、棉衣、羽绒服或大衣，注意保暖。"),
    (0,  "严寒天气", "天气严寒，建议穿厚羽绒服、棉服，搭配帽子围巾手套等保暖装备。"),
    (-999, "极寒天气", "极寒天气！建议穿加厚羽绒服、防寒服，做好全身保暖，尽量减少外出。"),
]


def _resolve_city(city: str) -> str:
    """将中文城市名转换为英文/拼音，确保 API 识别。"""
    city = city.strip()
    # 先查映射表
    if city in _CITY_PINYIN:
        return _CITY_PINYIN[city]
    # 如果已经是英文，直接返回
    if re.match(r"^[a-zA-Z\s\-]+$", city):
        return city
    # 尝试用原名（wttr.in 对部分中文名也能识别）
    return city


def _get_clothing_advice(temp_c: int) -> str:
    """根据温度获取穿衣建议。"""
    for threshold, label, advice in _CLOTHING_RULES:
        if temp_c >= threshold:
            return f"【{label}】{advice}"
    return "【极寒天气】请穿最厚的保暖衣物，注意防寒！"


def _get_clothing_items(temp_c: int) -> list[str]:
    """根据温度推荐具体衣物清单。"""
    items: list[str] = []
    if temp_c >= 30:
        items.extend(["短袖T恤", "短裤/裙子", "凉鞋/拖鞋", "防晒霜/遮阳帽"])
    elif temp_c >= 25:
        items.extend(["短袖T恤", "薄长裤/短裤", "运动鞋"])
    elif temp_c >= 20:
        items.extend(["长袖衬衫", "薄外套/针织衫", "长裤"])
    elif temp_c >= 15:
        items.extend(["薄毛衣/卫衣", "夹克/薄风衣", "长裤"])
    elif temp_c >= 10:
        items.extend(["毛衣", "厚外套/风衣", "厚长裤"])
    elif temp_c >= 5:
        items.extend(["厚毛衣", "羽绒服/棉服", "保暖裤"])
    elif temp_c >= 0:
        items.extend(["保暖内衣", "厚羽绒服", "加绒裤", "帽子/围巾/手套"])
    else:
        items.extend(["加厚保暖内衣", "超厚羽绒服", "防寒裤", "帽子/围巾/手套/耳罩", "保暖靴", "暖宝宝"])
    return items


def get_weather(city: str = "Beijing", lang: str = "zh") -> dict[str, Any]:
    """
    获取指定城市的天气信息。

    Args:
        city: 城市名称（中文或英文，如 "北京"、"Beijing"、"Chongqing"）
        lang: 语言（zh 中文，en 英文）

    Returns:
        包含天气信息的字典
    """
    resolved = _resolve_city(city)
    logger.info(f"天气查询: {city} -> {resolved}")

    try:
        url = f"https://wttr.in/{resolved}?format=j1&lang={lang}"
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers={
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": "Mozilla/5.0",
            })
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current_condition", [{}])[0]
        forecast = data.get("weather", [{}])[0]

        # 天气描述（优先中文翻译）
        description = "N/A"
        lang_key = f"lang_{lang}"
        if lang_key in current and current[lang_key]:
            description = current[lang_key][0].get("value", "N/A")
        elif "weatherDesc" in current and current["weatherDesc"]:
            description = current["weatherDesc"][0].get("value", "N/A")

        # 英文描述转中文
        desc_lower = description.strip().lower()
        if desc_lower in _WEATHER_DESC_ZH:
            description = _WEATHER_DESC_ZH[desc_lower]

        temp_c = int(current.get("temp_C", 0))
        feels_like = int(current.get("FeelsLikeC", 0))

        # 日出日落
        astronomy = forecast.get("astronomy", [{}])[0] if forecast else {}

        weather_info = {
            "city": city,
            "resolved_city": resolved,
            "temperature": f"{current.get('temp_C', 'N/A')}°C",
            "temperature_value": temp_c,
            "feels_like": f"{feels_like}°C",
            "description": description,
            "humidity": f"{current.get('humidity', 'N/A')}%",
            "wind_speed": f"{current.get('windspeedKmph', 'N/A')} km/h",
            "wind_dir": current.get("winddir16Point", "N/A"),
            "visibility": f"{current.get('visibility', 'N/A')} km",
            "uv_index": current.get("uvIndex", "N/A"),
            "precipitation": f"{current.get('precipMM', 'N/A')} mm",
            "pressure": f"{current.get('pressure', 'N/A')} hPa",
            "max_temp": f"{forecast.get('maxtempC', 'N/A')}°C" if forecast else "N/A",
            "min_temp": f"{forecast.get('mintempC', 'N/A')}°C" if forecast else "N/A",
            "sunrise": astronomy.get("sunrise", "N/A"),
            "sunset": astronomy.get("sunset", "N/A"),
            "query_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "clothing_advice": _get_clothing_advice(temp_c),
            "clothing_items": _get_clothing_items(temp_c),
        }
        return weather_info

    except httpx.TimeoutException:
        return {"error": "请求超时，请检查网络连接后重试。", "city": city}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP 错误: {e.response.status_code}", "city": city}
    except Exception as e:
        logger.error(f"天气查询失败: {e}")
        return {"error": f"获取天气信息失败: {str(e)}", "city": city}


def format_weather_report(info: dict[str, Any]) -> str:
    """格式化天气报告为 Markdown 文本。"""
    if "error" in info:
        return f"❌ 查询失败: {info['error']}"

    lines = [
        f"## 🏙️ {info['city']} 今日天气",
        "",
        "| 项目 | 数据 |",
        "|------|------|",
        f"| 🌡️ 当前温度 | **{info['temperature']}** |",
        f"| 🤔 体感温度 | **{info['feels_like']}** |",
        f"| 🌤️ 天气状况 | **{info['description']}** |",
        f"| 📊 今日温度 | {info['min_temp']} ~ {info['max_temp']} |",
        f"| 💧 湿度 | {info['humidity']} |",
        f"| 💨 风速 | {info['wind_speed']} ({info['wind_dir']}) |",
        f"| 👁️ 能见度 | {info['visibility']} |",
        f"| ☀️ 紫外线指数 | {info['uv_index']} |",
        f"| 🌧️ 降水量 | {info['precipitation']} |",
        f"| 🌅 日出 | {info['sunrise']} |",
        f"| 🌇 日落 | {info['sunset']} |",
        "",
        "---",
        "",
        "## 👔 穿衣建议",
        "",
        f"**{info['clothing_advice']}**",
        "",
        "### 👕 推荐穿搭清单",
        "",
    ]
    for item in info.get("clothing_items", []):
        lines.append(f"- {item}")

    lines.extend([
        "",
        f"_查询时间: {info.get('query_time', 'N/A')}_",
    ])

    return "\n".join(lines)


class WeatherTool:
    """天气查询工具类（供 ToolNode 集成使用）。"""

    def execute(self, city: str = "Beijing", lang: str = "zh") -> dict[str, Any]:
        """执行天气查询，返回格式化的结果。"""
        info = get_weather(city, lang)
        report = format_weather_report(info)
        return {
            "weather_info": info,
            "report": report,
        }


# 全局实例
weather_tool = WeatherTool()
