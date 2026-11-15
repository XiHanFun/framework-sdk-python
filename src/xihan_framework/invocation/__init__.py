"""调用上下文（RFC-0003）：取消、截止时间、W3C 追踪、已登记的 Baggage 与请求作用域对象袋。

不绑定任何协议，载体映射由协议适配器与插件运行时实现。``Context`` 不可变、按派生传递；取消信号 ``Cancellation``
可以从任何线程触发；``Context.run`` 在截止时间（``asyncio.timeout``）与取消信号下执行协程，把超时与取消翻译成
``KERNEL_TIMEOUT_001`` / ``KERNEL_CANCELLED_001`` 的失败结果。
"""

from xihan_framework.invocation._context import (
    ERROR_CODE_CANCELLED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    MESSAGE_CANCELLED,
    MESSAGE_DEADLINE_BEFORE_START,
    MESSAGE_DEADLINE_EXCEEDED,
    Cancellation,
    Context,
    ItemKey,
    TraceParent,
    cancelled_error,
    deadline_before_start_error,
    deadline_exceeded_error,
    new_span_id,
    new_trace_id,
    parse_trace_parent,
)

__all__ = [
    "ERROR_CODE_CANCELLED",
    "ERROR_CODE_DEADLINE_EXCEEDED",
    "MESSAGE_CANCELLED",
    "MESSAGE_DEADLINE_BEFORE_START",
    "MESSAGE_DEADLINE_EXCEEDED",
    "Cancellation",
    "Context",
    "ItemKey",
    "TraceParent",
    "cancelled_error",
    "deadline_before_start_error",
    "deadline_exceeded_error",
    "new_span_id",
    "new_trace_id",
    "parse_trace_parent",
]
