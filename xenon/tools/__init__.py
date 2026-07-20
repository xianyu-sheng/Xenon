"""
Xenon 原生工具集 — 惰性加载，热键触发。

包含:
- VisionBridge: 利用模型池多模态模型为 DeepSeek 提供"眼睛"
- ClipboardMonitor: 跨平台剪贴板图片热键监听 (Ctrl+Alt+V)

用法::

    from xenon.tools import VisionBridge, ClipboardMonitor

    bridge = VisionBridge()
    bridge.lazy_init(model_pool)

    def on_image(image_data: bytes):
        result = bridge.describe_image(image_data)
        print(result.text)

    monitor = ClipboardMonitor(on_image=on_image)
    monitor.start()  # 首次热键才激活
"""

from xenon.tools.vision_bridge import VisionBridge
from xenon.tools.clipboard_monitor import ClipboardMonitor, read_clipboard_image

__all__ = ["VisionBridge", "ClipboardMonitor", "read_clipboard_image"]
