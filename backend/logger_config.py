"""全链路日志基础设施。

提供线程安全的内存环形缓冲区 + stdout + 文件（按天滚动）三路输出，支持 task_id 注入和敏感数据脱敏。
默认将日志写入 <项目根>/data/studio.log，按天滚动并保留 7 天；可通过 STUDIO_LOG_FILE 覆盖路径。
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    _BUFFER_SIZE = int(os.environ.get("STUDIO_LOG_BUFFER_SIZE", "2000"))
except (TypeError, ValueError):
    _BUFFER_SIZE = 2000
_LOG_LEVEL = os.environ.get("STUDIO_LOG_LEVEL", "INFO").upper()
# N2 日志持久化：默认启用文件日志，写入 <项目根>/data/studio.log；
# 仅当显式设置 STUDIO_LOG_FILE 环境变量时才覆盖默认路径。
_DEFAULT_LOG_FILE = str(Path(__file__).resolve().parent.parent / "data" / "studio.log")
_LOG_FILE = os.environ.get("STUDIO_LOG_FILE", "").strip() or _DEFAULT_LOG_FILE

_REDACT_PATTERNS = [
    # Authorization 允许「Bearer <token>」中间存在空格，需先于通用字段处理。
    re.compile(
        r"(?i)([\"']?authorization[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?([^\"'\s,}\]]+)"
    ),
    # JSON、表单和 key=value 形式的敏感字段。字段名和分隔符保留，值统一替换。
    re.compile(
        r"(?i)([\"']?(?:confirmpassword|newpassword|oldpassword|password|api[_-]?key|apikey|"
        r"app[_-]?secret|appsecret|access_token|csrf_token|csrftoken|session_token)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}\]]+)"
    ),
    re.compile(r"(?i)(bearer\s+)(sk-[\w-]+|[\w\-]{20,})", re.ASCII),
    re.compile(r"(?i)(sk-[a-zA-Z0-9]{16,})", re.ASCII),
    re.compile(r"(?i)(api[_-]?key[\s:=]+)([\w\-]{8,})", re.ASCII),
    re.compile(r"(?i)(access_token[\"\s:=]+)([\w\-]+)", re.ASCII),
    re.compile(r"(?i)(app[_-]?secret[\s:=]+)([\w\-]{4})[\w\-]*", re.ASCII),
]


def redact_sensitive(text: str) -> str:
    """脱敏日志中的 API Key、Token、密码等敏感信息。"""
    result = text
    for pattern in _REDACT_PATTERNS:
        if pattern.groups == 2:
            result = pattern.sub(lambda m: m.group(1) + "***REDACTED***", result)
        else:
            result = pattern.sub("***REDACTED***", result)
    return result


class RedactingFormatter(logging.Formatter):
    """在任何日志离开进程前统一脱敏。

    脱敏放在 Formatter，而不是仅放在内存 Handler 中，确保 stdout、滚动文件
    和日志 API 三条输出链路执行完全相同的安全策略。
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive(super().format(record))


class _LogEntry:
    """单条日志记录的不可变结构。"""

    __slots__ = ("timestamp", "level", "module", "task_id", "message", "stack")

    def __init__(self, timestamp: str, level: str, module: str, task_id: str, message: str, stack: str):
        self.timestamp = timestamp
        self.level = level
        self.module = module
        self.task_id = task_id
        self.message = message
        self.stack = stack

    def to_dict(self) -> dict:
        d = {
            "timestamp": self.timestamp,
            "level": self.level,
            "module": self.module,
            "task_id": self.task_id,
            "message": self.message,
        }
        if self.stack:
            d["stack"] = self.stack
        return d


class RingBufferHandler(logging.Handler):
    """线程安全的内存环形缓冲区日志处理器。

    写入端使用 collections.deque(maxlen=N) 的 append()（CPython 原子操作），
    读取端使用 threading.Lock 保护快照遍历。
    """

    def __init__(self, maxlen: int = _BUFFER_SIZE):
        super().__init__()
        self._buffer: collections.deque[_LogEntry] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            task_id = getattr(record, "task_id", "-") or "-"
            stack = ""
            if record.exc_info and record.exc_info[1] is not None:
                import traceback
                stack = redact_sensitive("".join(traceback.format_exception(*record.exc_info)))
            message = self.format(record)
            message = redact_sensitive(message)
            entry = _LogEntry(
                timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                level=record.levelname,
                module=record.name,
                task_id=str(task_id),
                message=message,
                stack=stack,
            )
            self._buffer.append(entry)
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def query(
        self,
        *,
        level: str = "ALL",
        keyword: str = "",
        task_id: str = "",
        since: str = "",
        limit: int = 100,
    ) -> list[dict]:
        """查询日志，返回过滤后的列表。"""
        with self._lock:
            snapshot = list(self._buffer)
        level_upper = level.upper()
        keyword_lower = keyword.lower()
        results: list[dict] = []
        for entry in snapshot:
            if level_upper != "ALL" and entry.level != level_upper:
                continue
            if task_id and entry.task_id != task_id:
                continue
            if since and entry.timestamp < since:
                continue
            if keyword_lower:
                searchable = f"{entry.message} {entry.stack} {entry.module}".lower()
                if keyword_lower not in searchable:
                    continue
            results.append(entry.to_dict())
        return results[:limit]

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

class _TaskIdFilter(logging.Filter):
    """确保所有 LogRecord 都有 task_id 属性，避免 Formatter 报 KeyError。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "task_id") or not record.task_id:
            record.task_id = "-"  # type: ignore[attr-defined]
        return True


_task_id_filter = _TaskIdFilter()

_ring_handler = RingBufferHandler(maxlen=_BUFFER_SIZE)

_formatter = RedactingFormatter(
    fmt="[%(asctime)s] [%(levelname)s] [%(name)s] [task:%(task_id)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_ring_handler.setFormatter(_formatter)
_ring_handler.addFilter(_task_id_filter)

# 配置 root logger
_root = logging.getLogger()
_root.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_formatter)
_stream_handler.addFilter(_task_id_filter)
_root.addHandler(_stream_handler)
_root.addHandler(_ring_handler)

# 文件日志（默认启用，按天滚动，保留 7 天）
# 用 try/except 包裹以优雅处理权限/路径错误：失败时降级为仅 stdout + 环形缓冲区。
try:
    _log_file_path = Path(_LOG_FILE)
    _log_file_path.parent.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.handlers.TimedRotatingFileHandler(
        _LOG_FILE,
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    _file_handler.setFormatter(_formatter)
    _file_handler.addFilter(_task_id_filter)
    _root.addHandler(_file_handler)
except PermissionError:
    # 无写入权限时降级，仅保留 stdout + 环形缓冲区
    import logging
    logging.getLogger("logger_config").warning("日志文件无写入权限，降级为仅 stdout + 环形缓冲区")
except OSError:
    # 路径不可用等错误时降级
    import logging
    logging.getLogger("logger_config").warning("日志文件路径不可用，降级为仅 stdout + 环形缓冲区")
except Exception:  # noqa: BLE001
    import logging
    logging.getLogger("logger_config").warning("日志文件初始化失败，降级为仅 stdout + 环形缓冲区", exc_info=True)


class TaskLoggerAdapter(logging.LoggerAdapter):
    """注入 task_id 的 LoggerAdapter。"""

    def process(self, msg, kwargs):
        extra = self.extra or {}
        kwargs.setdefault("extra", {}).setdefault("task_id", extra.get("task_id", "-"))
        return msg, kwargs


def get_logger(name: str, task_id: str = "") -> logging.LoggerAdapter:
    """获取带 task_id 的 logger。"""
    logger = logging.getLogger(name)
    return TaskLoggerAdapter(logger, {"task_id": task_id or "-"})


def query_logs(
    *,
    level: str = "ALL",
    q: str = "",
    task_id: str = "",
    since: str = "",
    limit: int = 100,
) -> list[dict]:
    """查询日志的全局入口。"""
    return _ring_handler.query(level=level, keyword=q, task_id=task_id, since=since, limit=limit)


def clear_logs() -> None:
    """清空日志缓冲区。"""
    _ring_handler.clear()


def get_buffer_size() -> int:
    """返回当前缓冲区中的日志条数。"""
    return len(_ring_handler._buffer)
