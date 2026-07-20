"""
跨平台剪贴板图片监听器 — 惰性加载，热键触发。

支持平台:
- Linux: xclip / wl-paste + pynput
- macOS: pbpaste + pynput
- Windows: PIL.ImageGrab + pynput

架构::

    热键 Ctrl+Alt+V → 读剪贴板 → 保存临时文件 → 回调(on_image)
                                                    ↓
                                        VisionBridge.describe_image()
                                                    ↓
                                        文字注入到 Xenon 对话
"""

from __future__ import annotations

import logging
import platform
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 热键组合
_HOTKEY_COMBO = "<ctrl>+<alt>+v"

# ── 跨平台剪贴板读取 ───────────────────────────────────────────


def _read_clipboard_image_linux() -> bytes | None:
    """Linux: 尝试 xclip 然后 wl-paste 读剪贴板 PNG。"""
    # Wayland
    try:
        result = subprocess.run(
            ["wl-paste", "-t", "image/png"],
            capture_output=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("wl-paste 失败: %s", e)

    # X11
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("xclip 失败: %s", e)

    return None


def _read_clipboard_image_macos() -> bytes | None:
    """macOS: 使用 pbpaste + osascript 读剪贴板 PNG。"""
    try:
        # 检查剪贴板是否有图片
        check = subprocess.run(
            ["osascript", "-e",
             'get the clipboard as «class PNGf»'],
            capture_output=True, timeout=3,
        )
        if check.returncode == 0 and check.stdout.strip():
            # 转换为 PNG 字节
            result = subprocess.run(
                ["osascript", "-e",
                 'set img to the clipboard as «class PNGf»\n'
                 'return img'],
                capture_output=True, timeout=3,
            )
            if result.returncode == 0:
                raw = result.stdout.strip()
                if raw.startswith("«data PNGf"):
                    hex_str = raw[11:-2].replace(" ", "")
                    return bytes.fromhex(hex_str)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("macOS 剪贴板读取失败: %s", e)

    # 备选: pngpaste
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        subprocess.run(["pngpaste", tmp_path], capture_output=True, timeout=2)
        data = Path(tmp_path).read_bytes()
        Path(tmp_path).unlink()
        if data:
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass

    return None


def _read_clipboard_image_windows() -> bytes | None:
    """Windows: 使用 PIL.ImageGrab 读剪贴板。"""
    try:
        from PIL import ImageGrab, Image
        import io

        img = ImageGrab.grabclipboard()
        if img is None:
            return None
        if isinstance(img, list):
            # 文件列表，取第一个
            if img and Path(str(img[0])).exists():
                return Path(str(img[0])).read_bytes()
            return None

        buf = io.BytesIO()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        logger.debug("PIL 未安装")
    except Exception as e:
        logger.debug("Windows 剪贴板读取失败: %s", e)
    return None


def read_clipboard_image() -> bytes | None:
    """跨平台读剪贴板图片。返回 PNG 字节或 None。"""
    system = platform.system()
    if system == "Linux":
        return _read_clipboard_image_linux()
    elif system == "Darwin":
        return _read_clipboard_image_macos()
    elif system == "Windows":
        return _read_clipboard_image_windows()
    else:
        logger.warning("不支持的操作系统: %s", system)
        return None


# ── 热键监听器（惰性启动） ─────────────────────────────────────


class ClipboardMonitor:
    """
    剪贴板热键监听器 — 惰性加载。

    用法::

        monitor = ClipboardMonitor(on_image=handle_image)
        monitor.start()      # 首次调用才启动后台线程
        ...
        monitor.stop()
    """

    def __init__(self, on_image: Callable[[bytes], None]) -> None:
        """
        Args:
            on_image: 回调函数，接收 PNG 字节
        """
        self._on_image = on_image
        self._running = False
        self._thread: threading.Thread | None = None
        self._hotkey_listener: Any = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """惰性启动：首次调用才创建后台线程。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._listen_loop,
            daemon=True,
            name="ClipboardMonitor",
        )
        self._thread.start()
        logger.info("ClipboardMonitor 已启动 (热键: %s)", _HOTKEY_COMBO)

    def stop(self) -> None:
        """停止监听。"""
        self._running = False
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        logger.info("ClipboardMonitor 已停止")

    def _listen_loop(self) -> None:
        """后台线程：注册热键并阻塞监听。"""
        try:
            from pynput import keyboard

            def on_activate():
                """热键回调（在 pynput 内部线程执行）。"""
                logger.info("热键 %s 触发", _HOTKEY_COMBO)
                try:
                    img_data = read_clipboard_image()
                    if img_data:
                        logger.info("剪贴板图片: %d bytes", len(img_data))
                        self._on_image(img_data)
                    else:
                        logger.info("剪贴板无图片")
                except Exception as e:
                    logger.error("热键回调异常: %s", e)

            self._hotkey_listener = keyboard.GlobalHotKeys({
                _HOTKEY_COMBO: on_activate,
            })
            self._hotkey_listener.start()
            logger.info("热键注册成功: %s", _HOTKEY_COMBO)

            # 阻塞直到 stop() 被调用
            while self._running:
                time.sleep(0.5)

            self._hotkey_listener.stop()

        except ImportError:
            logger.warning(
                "pynput 未安装，剪贴板监听不可用。"
                "安装: pip install pynput"
            )
            self._running = False
        except Exception as e:
            logger.error("热键监听失败: %s", e)
            self._running = False
