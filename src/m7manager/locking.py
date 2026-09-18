from __future__ import annotations

import os
from pathlib import Path


class DataDirLock:
    """A process-held, cross-platform exclusive lock for one data directory."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = self.root / "manager.lock"
        self._stream = None

    def acquire(self) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self):
        if self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"数据目录已被占用：{self.root}")
        return self

    def __exit__(self, *_):
        self.release()


__all__ = ["DataDirLock"]
