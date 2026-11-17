"""插件日志：stderr 上一行一个 JSON 对象（``spec/plugin/invocation.md`` §6）。"""

import json
import logging
import sys
from typing import Final, TextIO

# LogRecord 自带的属性；extra 传入的其余属性作为作用域属性写出
_RECORD_ATTRIBUTES: Final = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def level_name(level: int) -> str:
    """把 ``logging`` 的级别换成宿主认的级别名：``trace`` / ``debug`` / ``information`` / ``warning`` / ``error`` / ``critical``。"""
    if level < logging.DEBUG:
        return "trace"
    if level < logging.INFO:
        return "debug"
    if level < logging.WARNING:
        return "information"
    if level < logging.ERROR:
        return "warning"
    if level < logging.CRITICAL:
        return "error"
    return "critical"


class JsonLogHandler(logging.Handler):
    """把日志写成宿主按结构化日志处理的 JSON 行：``level``、``message``，其余字段是作用域属性。

    ``logger`` 是记录器名，``extra`` 传入的属性原样写出（不是 JSON 值的取 ``str``），有异常时 ``exception`` 是完整的
    回溯。只写 ASCII（非 ASCII 字符转义），所以不受 stderr 编码影响；JSON 字符串里的换行被转义，一条日志总是一行。
    """

    def __init__(self, stream: TextIO | None = None, level: int = logging.NOTSET) -> None:
        """写到 ``stream``；省略时写到当时的 ``sys.stderr``。"""
        super().__init__(level)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        """写出一行；写不出时交给 ``handleError``，不让日志打断调用。"""
        try:
            entry: dict[str, object] = {"level": level_name(record.levelno), "message": record.getMessage()}
            entry["logger"] = record.name
            for name, value in vars(record).items():
                if name not in _RECORD_ATTRIBUTES and name not in entry:
                    entry[name] = value
            if record.exc_info:
                entry["exception"] = logging.Formatter().formatException(record.exc_info)
            line = json.dumps(entry, ensure_ascii=True, default=str)
            stream = self._stream if self._stream is not None else sys.stderr
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)
