# -*- coding: utf-8 -*-
"""Кольцевой буфер + файловый лог панели (для GET /api/logs)."""
import threading
import time
from collections import deque
from pathlib import Path

MAX_LINES = 800
MAX_FILE_BYTES = 2 * 1024 * 1024


class LogBus:
    def __init__(self, path=None):
        self._lines = deque(maxlen=MAX_LINES)
        self._lock = threading.Lock()
        self._path = Path(path) if path else None

    def log(self, level, msg):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] [{level}] {msg}"
        with self._lock:
            self._lines.append(line)
            if self._path is not None:
                try:
                    if self._path.exists() and self._path.stat().st_size > MAX_FILE_BYTES:
                        self._path.write_text("", encoding="utf-8")
                    with open(self._path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass
        return line

    def info(self, msg):
        return self.log("INFO", msg)

    def ok(self, msg):
        return self.log("OK", msg)

    def warn(self, msg):
        return self.log("WARN", msg)

    def bad(self, msg):
        return self.log("BAD", msg)

    def tail(self, n=200):
        with self._lock:
            lines = list(self._lines)
        try:
            n = max(1, min(int(n), MAX_LINES))
        except (TypeError, ValueError):
            n = 200
        return lines[-n:]