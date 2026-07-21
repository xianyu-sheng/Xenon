"""
视觉桥接器 — 利用 Xenon 模型池中的多模态模型为 DeepSeek 提供"眼睛"。

架构::

    剪贴板图片 → 轻量多模态模型(描述) → DeepSeek(推理)

设计原则：
- 零外部依赖：复用模型池现有 API Key，不需要额外配置
- 惰性加载：首次热键触发才初始化，启动 0ms 开销
- 自动降级：无多模态模型时提示配置，不崩溃
- 缓存复用：相同图片 SHA256 缓存，LRU 淘汰避免重复调用
- 纯查找不注册：_ensure_vision_model 只读不写，不污染模型池
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── 已知多模态轻量模型（按优先级） ──────────────────────────────
_VISION_CANDIDATES = [
    # OpenAI 系
    "gpt-4o-mini",
    "gpt-4o",
    # Anthropic
    "claude-3-haiku",
    "claude-3-sonnet",
    "claude-3-opus",
    # Google
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    # 火山引擎 / 豆包
    "doubao-vision",
    "doubao-seed-1-6-vision",
    # 通义千问
    "qwen-vl-max",
    "qwen-vl-plus",
    # 本地 (Ollama)
    "llava",
    "bakllava",
    "minicpm-v",
]

# 视觉转录 system prompt
VISION_SYSTEM_PROMPT = """你是一个精准的图片内容转录助手。

请将图片中的所有内容转录为结构化的文本描述：

规则：
1. 图片包含文字/代码/公式时，完整准确地逐字转录，保留原始格式和缩进
2. 图片是图表/流程图/界面截图时，用文字详细描述结构、关键数据和逻辑关系
3. 图片是照片/场景时，描述关键元素、文字标识和空间关系
4. 不要寒暄，不要加解释，只输出转录结果本身"""


@dataclass
class VisionResult:
    """视觉转录结果。"""
    text: str
    model_used: str
    latency_ms: float
    cached: bool = False


@dataclass
class VisionCache:
    """图片 → 文字缓存（基于 SHA256，LRU 淘汰）。

    使用 OrderedDict 实现 LRU：get / put 时将被访问条目移到末尾，
    淘汰时删除开头最久未访问条目。
    """
    _cache: OrderedDict[str, VisionResult] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    max_size: int = 50

    def get(self, image_hash: str) -> VisionResult | None:
        with self._lock:
            result = self._cache.get(image_hash)
            if result is not None:
                # LRU: 移到末尾（最近访问）
                self._cache.move_to_end(image_hash)
            return result

    def put(self, image_hash: str, result: VisionResult) -> None:
        with self._lock:
            if image_hash in self._cache:
                # 更新已有条目并标记为最近使用
                self._cache.move_to_end(image_hash)
                self._cache[image_hash] = result
                return
            if len(self._cache) >= self.max_size:
                # LRU 淘汰：删除最久未访问条目（开头）
                evicted_key, evicted_val = self._cache.popitem(last=False)
                logger.debug(
                    "VisionCache LRU 淘汰: hash=%s..., age=%s",
                    evicted_key[:12],
                    evicted_val.latency_ms,
                )
            self._cache[image_hash] = result

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)


class VisionBridge:
    """
    视觉桥接器 — 惰性加载，首次调用时初始化。

    用法::

        bridge = VisionBridge()
        bridge.lazy_init(model_pool)          # 注册模型池（不连接任何模型）
        result = bridge.describe_image(
            image_data=b"..." ,               # PNG/JPEG 字节
            mime_type="image/png",
        )
        print(result.text)                    # 模型的文字描述
    """

    def __init__(self) -> None:
        self._initialized = False
        self._model_pool: Any = None
        self._vision_model_id: str | None = None
        # 凭证信息（从 credentials 扫描获取，避免污染模型池）
        self._vision_creds: dict[str, str] | None = None
        self._vision_base_url: str | None = None
        self._cache = VisionCache()

    # ── 惰性初始化 ─────────────────────────────────────────────

    def lazy_init(self, model_pool: Any) -> None:
        """
        注册模型池引用（不发起任何网络请求）。

        真正的初始化在首次 describe_image() 调用时触发。
        """
        self._model_pool = model_pool
        self._initialized = True
        logger.info("VisionBridge 已注册模型池（惰性模式，首次调用时激活）")

    @property
    def is_ready(self) -> bool:
        """是否已找到可用的多模态模型。"""
        return self._vision_model_id is not None

    def _ensure_vision_model(self) -> str:
        """纯查找：在模型池和 credentials 中查找可用的多模态模型。

        只读不写 —— 不会将模型注册进模型池，避免副作用和池震荡。

        Returns:
            model_id: 可用的多模态模型 ID

        Raises:
            RuntimeError: 未找到任何多模态模型
        """
        if self._vision_model_id:
            return self._vision_model_id

        if not self._model_pool:
            raise RuntimeError("VisionBridge 未初始化，请先调用 lazy_init()")

        # ── 第 1 层：模型池（已注册的活跃模型） ──
        entries = self._model_pool.list_all()
        available = [e.model_id for e in entries]

        for candidate in _VISION_CANDIDATES:
            for model_id in available:
                if candidate in model_id.lower():
                    self._vision_model_id = model_id
                    logger.info("VisionBridge 已选择多模态模型（池中）: %s", model_id)
                    return model_id

        for model_id in available:
            if "vision" in model_id.lower() and "embedding" not in model_id.lower():
                self._vision_model_id = model_id
                logger.info("VisionBridge 已选择多模态模型（池中 vision match）: %s", model_id)
                return model_id

        # ── 第 2 层：credentials 全量扫描（纯查找，不注册） ──
        try:
            from xenon.repl.provider_registry import get_configured_providers
            configured = get_configured_providers()
            for p in configured:
                if not p.key or not p.key.strip():
                    continue
                for model_name in (p.models or []):
                    model_id = f"{p.key}/{model_name}"
                    for candidate in _VISION_CANDIDATES:
                        if candidate in model_id.lower():
                            self._vision_model_id = model_id
                            self._vision_creds = {p.key: p.api_key} if p.api_key else None
                            self._vision_base_url = p.base_url
                            logger.info(
                                "VisionBridge 发现多模态模型（credentials 中，未注册池）: %s",
                                model_id,
                            )
                            return model_id
                    if "vision" in model_id.lower() and "embedding" not in model_id.lower():
                        self._vision_model_id = model_id
                        self._vision_creds = {p.key: p.api_key} if p.api_key else None
                        self._vision_base_url = p.base_url
                        logger.info(
                            "VisionBridge 发现多模态模型（credentials 中，未注册池）: %s",
                            model_id,
                        )
                        return model_id
        except Exception as e:
            logger.debug("Credentials 扫描失败: %s", e)

        raise RuntimeError(
            "模型池中未找到多模态模型。请配置一个支持图片输入的模型（如 gpt-4o-mini、"
            "claude-3-haiku、gemini-flash、doubao-vision）。"
        )

    # ── 核心方法 ───────────────────────────────────────────────

    def describe_image(
        self,
        image_data: bytes,
        mime_type: str = "image/png",
        *,
        force_refresh: bool = False,
    ) -> VisionResult:
        """
        将图片转为文字描述。

        Args:
            image_data: 图片原始字节（PNG / JPEG）
            mime_type: MIME 类型
            force_refresh: 跳过缓存

        Returns:
            VisionResult: 文字描述 + 模型名 + 耗时
        """
        if not self._initialized:
            raise RuntimeError("VisionBridge 未初始化")

        # SHA256 缓存（LRU）
        image_hash = hashlib.sha256(image_data).hexdigest()
        if not force_refresh:
            cached = self._cache.get(image_hash)
            if cached:
                cached.cached = True
                logger.info("VisionBridge 缓存命中 (hash=%s...)", image_hash[:12])
                return cached

        # 选择模型
        model_id = self._ensure_vision_model()

        # 编码图片
        b64 = base64.b64encode(image_data).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        # 调用模型
        t0 = time.monotonic()
        try:
            text = self._call_vision_api(model_id, data_url, mime_type)
        except Exception as e:
            logger.error("视觉模型调用失败 (%s): %s", model_id, e)
            raise

        latency = (time.monotonic() - t0) * 1000

        result = VisionResult(
            text=text,
            model_used=model_id,
            latency_ms=round(latency, 1),
        )
        self._cache.put(image_hash, result)
        logger.info(
            "VisionBridge 转录完成: %d 字符, 模型=%s, 耗时=%.0fms",
            len(text), model_id, latency,
        )
        return result

    def _call_vision_api(
        self, model_id: str, data_url: str, mime_type: str
    ) -> str:
        """调用 OpenAI 兼容 vision API（支持 99% 的模型商）。"""
        from xenon.utils.llm_client import chat_completion

        messages = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "请解析这张图片。"},
                ],
            },
        ]

        # 如果模型来自 credentials 扫描（非池中），显式传入凭证
        kwargs: dict[str, Any] = {}
        if self._vision_creds is not None:
            kwargs["credentials"] = self._vision_creds
        if self._vision_base_url is not None:
            kwargs["base_url"] = self._vision_base_url

        response = chat_completion(
            model_id,
            messages=messages,
            max_tokens=4096,
            temperature=0.0,  # 转录任务，无创造性
            **kwargs,
        )

        # 提取文本内容
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return response.get("content", str(response))
        return str(response)

    def clear_cache(self) -> None:
        self._cache.clear()
