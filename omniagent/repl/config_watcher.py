"""P3: Configuration Watchdog -- Linux inotify 监听 models.yaml 变更自动热加载。

设计取舍(见整合方案 §7 P3):
- 纯 stdlib ctypes 调 libc inotify,**零新依赖**(项目刻意轻量,仅 httpx/pyyaml/rich/prompt-toolkit);
- Linux only(ubutnu 分支),非 Linux 或 inotify 不可用时 ``start()`` 返回 False 静默降级;
- 监听**目录**+过滤文件名,以正确处理编辑器的 atomic rename 保存(vim/emacs 写临时文件再 rename);
- debounce 0.5s 防多次写触发抖动;
- 回调在 watcher 线程内执行,异常被捕获不波及主循环。

开关:env ``OMNIAGENT_CONFIG_WATCH``,默认 "1"(开),设 "0" 关闭。
"""
from __future__ import annotations

import ctypes
import logging
import os
import select
import struct
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# inotify 事件掩码
IN_CLOSE_WRITE = 0x00000008   # 文件写关闭(覆盖保存)
IN_MOVED_TO = 0x00000080      # 文件被 rename 进目录(vim atomic save)
IN_CREATE = 0x00000100        # 新建文件
_INOTIFY_MASK = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE

IN_CLOEXEC = 0x00080000

# struct inotify_event { int wd; uint32_t mask; uint32_t cookie; uint32_t len; char name[]; }
_EVENT_FMT = "iIII"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)  # 16 字节

_libc: ctypes.CDLL | None = None
_inotify_unavailable = False


def _get_libc() -> ctypes.CDLL | None:
    """惰性加载 libc 并绑定 inotify 符号。非 Linux/无符号时返回 None。"""
    global _libc, _inotify_unavailable
    if _libc is not None:
        return _libc
    if _inotify_unavailable:
        return None
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.inotify_rm_watch.restype = ctypes.c_int
        _libc = libc
        return libc
    except (OSError, AttributeError):
        _inotify_unavailable = True
        return None


def is_watch_supported() -> bool:
    """当前平台是否支持 inotify 热加载。"""
    return _get_libc() is not None


def is_watch_enabled() -> bool:
    """env 开关:默认开,``OMNIAGENT_CONFIG_WATCH=0`` 关。"""
    return os.environ.get("OMNIAGENT_CONFIG_WATCH", "1") not in ("0", "false", "False")


class ConfigWatcher:
    """监听单个配置文件变更,debounce 后触发回调。

    用法::

        w = ConfigWatcher(path, on_reload=reload_fn)
        if w.start():
            ...
            w.stop()

    ``start()`` 失败(非 Linux / 目录不存在 / inotify 调用失败)时返回 False,
    调用方应视作未启用、静默降级,不影响主流程。
    """

    def __init__(
        self,
        config_path: str | Path,
        on_reload: Callable[[], None],
        debounce_s: float = 0.5,
    ) -> None:
        self._path = Path(config_path)
        self._on_reload = on_reload
        self._debounce_s = debounce_s
        self._target_name = self._path.name
        self._dir = str(self._path.parent)
        self._fd: int = -1
        self._wd: int = -1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """启动监听。成功返回 True;不可用/目录不存在返回 False(静默降级)。"""
        libc = _get_libc()
        if libc is None:
            logger.debug("inotify 不可用,跳过配置热加载")
            return False
        if not self._path.parent.is_dir():
            logger.debug("监听目录不存在,跳过配置热加载: %s", self._dir)
            return False
        fd = libc.inotify_init1(IN_CLOEXEC)
        if fd < 0:
            logger.debug("inotify_init1 失败,跳过配置热加载")
            return False
        wd = libc.inotify_add_watch(fd, self._dir.encode(), _INOTIFY_MASK)
        if wd < 0:
            os.close(fd)
            logger.debug("inotify_add_watch 失败: %s", self._dir)
            return False
        self._fd = fd
        self._wd = wd
        self._thread = threading.Thread(
            target=self._run, name="omniagent-config-watch", daemon=True)
        self._thread.start()
        logger.info("配置热加载已启用: 监听 %s", self._path)
        return True

    def _run(self) -> None:
        """后台线程:阻塞读 inotify 事件,过滤目标文件名后 debounce 触发。"""
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self._fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not r:
                continue
            try:
                data = os.read(self._fd, 65536)
            except OSError:
                break
            offset = 0
            n = len(data)
            while offset + _EVENT_SIZE <= n:
                _wd, mask, _cookie, name_len = struct.unpack_from(_EVENT_FMT, data, offset)
                name_bytes = data[offset + _EVENT_SIZE: offset + _EVENT_SIZE + name_len]
                name = name_bytes.split(b"\0", 1)[0].decode("utf-8", "replace")
                offset += _EVENT_SIZE + name_len
                if name == self._target_name and (mask & _INOTIFY_MASK):
                    self._schedule_reload()

    def _schedule_reload(self) -> None:
        """debounce:收到事件后等 _debounce_s 无新事件才触发,防抖动。"""
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(self._debounce_s, self._fire)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _fire(self) -> None:
        """触发回调;异常被吞掉避免拖垮 watcher 线程。"""
        try:
            self._on_reload()
        except Exception as e:  # noqa: BLE001 -- 回调异常不应波及 watcher
            logger.warning("配置热加载回调失败: %s", e)

    def stop(self) -> None:
        """停止监听并释放 inotify 资源。幂等。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None
        libc = _get_libc()
        if self._wd >= 0 and libc is not None:
            try:
                libc.inotify_rm_watch(self._fd, self._wd)
            except Exception:  # noqa: BLE001
                pass
            self._wd = -1
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
