"""Small dependency-free inter-process lock for JSON memory transactions."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


class MemoryLockTimeoutError(TimeoutError):
    """Raised when another process keeps a memory transaction lock too long."""


class InterProcessFileLock:
    """An ownership-token lock based on atomic ``O_EXCL`` file creation.

    Xenon keeps transactions short, so a lock file is simpler and more portable
    than adding a database dependency. Dead owners are reclaimed after the stale
    threshold; a live process is never treated as stale merely because it is slow.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.05,
        stale_after: float = 30.0,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.stale_after = stale_after
        self._token: str | None = None

    def __enter__(self) -> "InterProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        token = uuid.uuid4().hex
        payload = json.dumps(
            {"pid": os.getpid(), "created_at": time.time(), "token": token}
        ).encode("utf-8")
        while True:
            try:
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self._token = token
                return
            except FileExistsError:
                self._reclaim_dead_owner()
                if time.monotonic() >= deadline:
                    raise MemoryLockTimeoutError(
                        f"等待记忆锁超时: {self.path}"
                    )
                time.sleep(self.poll_interval)

    def release(self) -> None:
        if self._token is None:
            return
        try:
            owner = self._read_owner()
            if owner.get("token") == self._token:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            # Never remove a lock whose ownership cannot be verified.
            pass
        finally:
            self._token = None

    def _read_owner(self) -> dict[str, object]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _reclaim_dead_owner(self) -> None:
        try:
            owner = self._read_owner()
            created_at = float(owner["created_at"])
            pid = int(owner["pid"])
            token = str(owner["token"])
            if time.time() - created_at < self.stale_after or self._pid_is_alive(pid):
                return
            # Re-read before unlinking to avoid deleting a newly acquired lock.
            if str(self._read_owner().get("token")) == token:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # A partially written lock is reclaimable only after it is old.
            try:
                if time.time() - self.path.stat().st_mtime >= self.stale_after:
                    self.path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
