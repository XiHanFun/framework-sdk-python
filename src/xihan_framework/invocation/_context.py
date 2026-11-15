"""调用上下文（RFC-0003）：取消、截止时间、W3C 追踪、已登记的 Baggage 与请求作用域对象袋。"""

import asyncio
import contextlib
import re
import secrets
import threading
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, NamedTuple, Self, cast

from xihan_framework.primitives import (
    BAGGAGE_CORRELATION,
    BAGGAGE_IDEMPOTENCY_KEY,
    BAGGAGE_LOCALE,
    BAGGAGE_PRINCIPAL,
    BAGGAGE_TENANT,
    ERROR_CODE_INVALID_ARGUMENT,
    Error,
    FrameworkError,
    Result,
    fail,
    is_baggage_key_registered,
    is_baggage_key_well_formed,
)

#: 调用超过截止时间。
ERROR_CODE_DEADLINE_EXCEEDED: Final = "KERNEL_TIMEOUT_001"
#: 调用被取消。
ERROR_CODE_CANCELLED: Final = "KERNEL_CANCELLED_001"
#: 超时的消息键。
MESSAGE_DEADLINE_EXCEEDED: Final = "kernel.invocation.deadline_exceeded"
#: 取消的消息键。
MESSAGE_CANCELLED: Final = "kernel.invocation.cancelled"

# W3C traceparent：version-traceid-spanid-flags，全小写十六进制
_TRACE_PARENT = re.compile(r"([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_TRACE_ID = re.compile(r"[0-9a-f]{32}")
_SPAN_ID = re.compile(r"[0-9a-f]{16}")
_ZERO_TRACE_ID: Final = "0" * 32
_ZERO_SPAN_ID: Final = "0" * 16


class TraceParent(NamedTuple):
    """解析后的 W3C traceparent。"""

    trace_id: str
    span_id: str
    sampled: bool


def parse_trace_parent(text: str) -> TraceParent | None:
    """解析 W3C traceparent；格式不对或 ID 全零时为 ``None``。"""
    match = _TRACE_PARENT.fullmatch(text)
    if match is None or match.group(2) == _ZERO_TRACE_ID or match.group(3) == _ZERO_SPAN_ID:
        return None
    return TraceParent(match.group(2), match.group(3), match.group(4) == "01")


def new_trace_id() -> str:
    """生成 32 位小写十六进制的追踪 ID。"""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """生成 16 位小写十六进制的 Span ID。"""
    return secrets.token_hex(8)


def deadline_exceeded_error() -> Error:
    """超时的原语层错误（``KERNEL_TIMEOUT_001``）。"""
    return Error.timeout(ERROR_CODE_DEADLINE_EXCEEDED, MESSAGE_DEADLINE_EXCEEDED)


def cancelled_error() -> Error:
    """取消的原语层错误（``KERNEL_CANCELLED_001``）。"""
    return Error.cancelled(ERROR_CODE_CANCELLED, MESSAGE_CANCELLED)


def _ignore() -> None:
    """什么都不做的退订函数。"""


class Cancellation:
    """一次调用的取消信号：只触发一次，可以从任何线程触发。

    持有它的一方（协议宿主、插件运行时）调用 ``cancel``；上下文与它派生的上下文共享同一个信号。``Context.run``
    订阅它来取消正在执行的协程，处理器也可以 ``await wait()`` 或读 ``is_cancelled`` 自己配合。订阅在退订或触发后
    即移除，所以回调表的大小不超过同时在等的订阅数。
    """

    __slots__ = ("_callbacks", "_cancelled", "_lock", "_next_token")

    def __init__(self) -> None:
        """创建一个未触发的信号。"""
        self._lock = threading.Lock()
        self._cancelled = False
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_token = 0

    @property
    def is_cancelled(self) -> bool:
        """是否已触发。"""
        return self._cancelled

    def cancel(self) -> None:
        """触发信号；重复调用什么都不做。回调在调用方线程里依次执行，任一抛出时其余照常执行，最后一并抛出。"""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        # 回调在锁外执行：它们可能退订或读状态，在锁里执行会死锁
        failures: list[Exception] = []
        for callback in callbacks:
            try:
                callback()
            except Exception as failure:
                failures.append(failure)
        if failures:
            raise ExceptionGroup("Cancellation callbacks failed.", failures)

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """订阅触发：返回退订函数；已触发时立即执行回调并返回什么都不做的退订函数。"""
        with self._lock:
            if not self._cancelled:
                token = self._next_token
                self._next_token += 1
                self._callbacks[token] = callback

                def unsubscribe() -> None:
                    with self._lock:
                        self._callbacks.pop(token, None)

                return unsubscribe
        callback()
        return _ignore

    async def wait(self) -> None:
        """等到信号触发。"""
        if self._cancelled:
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def resolve() -> None:
            if not future.done():
                future.set_result(None)

        def wake() -> None:
            # 触发方可能在别的线程：经事件循环回到它自己的线程里完成 future；循环已关闭说明等待方已经不在了
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(resolve)

        unsubscribe = self.on_cancel(wake)
        try:
            await future
        finally:
            unsubscribe()


class ItemKey[T]:
    """对象袋的类型化键：按身份比较，``name`` 只用于调试。"""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        """用调试名创建；同名的两个键是不同的键。"""
        self._name = name

    @property
    def name(self) -> str:
        """调试名。"""
        return self._name

    def __repr__(self) -> str:
        """``ItemKey('name')``。"""
        return f"ItemKey({self._name!r})"


class _Items:
    """派生上下文共享的请求作用域对象袋；读写加锁，处理器可能把上下文交给别的线程。"""

    __slots__ = ("_lock", "_values")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[ItemKey[Any], object] = {}

    def set(self, key: ItemKey[Any], value: object) -> None:
        with self._lock:
            self._values[key] = value

    def get(self, key: ItemKey[Any]) -> object | None:
        with self._lock:
            return self._values.get(key)


class _RunState:
    """``Context.run`` 一次执行的状态，只在事件循环线程里读写。"""

    __slots__ = ("finished", "signalled", "withdrawn")

    def __init__(self) -> None:
        self.finished = False
        self.signalled = False
        self.withdrawn = False

    def withdraw(self, task: "asyncio.Task[Any]") -> int:
        """撤回本信号发出的取消请求（至多一次），返回任务剩下的取消请求数。"""
        if not self.signalled or self.withdrawn:
            return task.cancelling()
        self.withdrawn = True
        return task.uncancel()


class Context:
    """一次调用的上下文（RFC-0003）：不可变，``with_*`` 方法派生新的上下文。

    派生的上下文共享取消信号、截止时间、追踪与对象袋，Baggage 按值复制。截止时间是 UTC 的绝对时刻；``run`` 在
    截止时间与取消信号下执行协程，把超时与取消翻译成 ``KERNEL_TIMEOUT_001`` / ``KERNEL_CANCELLED_001``。
    """

    __slots__ = ("_baggage", "_cancellation", "_deadline", "_items", "_sampled", "_span_id", "_trace_id")

    def __init__(
        self,
        *,
        cancellation: Cancellation | None = None,
        deadline: datetime | None = None,
        timeout: timedelta | float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        trace_parent: str | None = None,
        baggage: Mapping[str, str] | None = None,
    ) -> None:
        """创建上下文。

        ``cancellation`` 省略时创建新的信号（经 ``cancellation`` 属性取得）；``deadline`` 是带时区的绝对时刻，
        ``timeout`` 是相对现在的时长（秒或 ``timedelta``），二者至多给一个。``trace_parent`` 是外部输入，
        不合法时忽略；``trace_id`` / ``span_id`` 是调用方给的值，不合法即编程错误。没有追踪时生成新的 Trace ID
        与 Span ID，采样标志缺省为真。Baggage 的键必须符合 ``xihan.<name>``。
        """
        if deadline is not None and timeout is not None:
            raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, "A context takes either a deadline or a timeout.")
        if trace_parent is not None and (trace_id is not None or span_id is not None):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, "A context takes either a traceparent or trace and span IDs."
            )
        if deadline is not None:
            if deadline.tzinfo is None or deadline.utcoffset() is None:
                raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, "The deadline must be a timezone-aware datetime.")
            deadline = deadline.astimezone(UTC)
        elif timeout is not None:
            delta = timeout if isinstance(timeout, timedelta) else timedelta(seconds=timeout)
            deadline = datetime.now(UTC) + delta
        sampled = True
        if trace_parent is not None and (parsed := parse_trace_parent(trace_parent)) is not None:
            trace_id, span_id, sampled = parsed
        if trace_id is not None and (_TRACE_ID.fullmatch(trace_id) is None or trace_id == _ZERO_TRACE_ID):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The trace ID '{trace_id}' must be 32 lowercase hex digits, not all zero."
            )
        if span_id is not None and (_SPAN_ID.fullmatch(span_id) is None or span_id == _ZERO_SPAN_ID):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The span ID '{span_id}' must be 16 lowercase hex digits, not all zero."
            )
        copied: dict[str, str] = {}
        for key, value in (baggage or {}).items():
            _check_baggage_key(key)
            copied[key] = value
        self._cancellation = Cancellation() if cancellation is None else cancellation
        self._deadline = deadline
        self._trace_id = new_trace_id() if trace_id is None else trace_id
        self._span_id = new_span_id() if span_id is None else span_id
        self._sampled = sampled
        self._baggage: Mapping[str, str] = MappingProxyType(copied)
        self._items = _Items()

    @property
    def cancellation(self) -> Cancellation:
        """取消信号。"""
        return self._cancellation

    @property
    def is_cancelled(self) -> bool:
        """取消信号是否已触发。"""
        return self._cancellation.is_cancelled

    @property
    def deadline(self) -> datetime | None:
        """截止时间（UTC）；没有时为 ``None``。"""
        return self._deadline

    def is_expired(self, now: datetime | None = None) -> bool:
        """是否已过截止时间；``now`` 省略时取当前 UTC 时刻。"""
        return self._deadline is not None and (now or datetime.now(UTC)) >= self._deadline

    def remaining_time(self, now: datetime | None = None) -> timedelta | None:
        """到截止时间的剩余时间，已过期为零；没有截止时间时为 ``None``。"""
        if self._deadline is None:
            return None
        return max(self._deadline - (now or datetime.now(UTC)), timedelta(0))

    @property
    def trace_id(self) -> str:
        """追踪 ID（32 位小写十六进制）。"""
        return self._trace_id

    @property
    def span_id(self) -> str:
        """Span ID（16 位小写十六进制）。"""
        return self._span_id

    @property
    def sampled(self) -> bool:
        """采样标志。"""
        return self._sampled

    @property
    def trace_parent(self) -> str:
        """W3C traceparent。"""
        return f"00-{self._trace_id}-{self._span_id}-{'01' if self._sampled else '00'}"

    @property
    def baggage(self) -> Mapping[str, str]:
        """Baggage 的只读视图。"""
        return self._baggage

    @property
    def tenant(self) -> str | None:
        """当前租户 ID。"""
        return self._baggage.get(BAGGAGE_TENANT)

    @property
    def principal(self) -> str | None:
        """当前主体 ID。"""
        return self._baggage.get(BAGGAGE_PRINCIPAL)

    @property
    def correlation(self) -> str | None:
        """业务关联 ID。"""
        return self._baggage.get(BAGGAGE_CORRELATION)

    @property
    def idempotency_key(self) -> str | None:
        """幂等键。"""
        return self._baggage.get(BAGGAGE_IDEMPOTENCY_KEY)

    @property
    def locale(self) -> str | None:
        """请求文化（BCP 47）。"""
        return self._baggage.get(BAGGAGE_LOCALE)

    def with_baggage(self, key: str, value: str) -> Self:
        """派生一个多一个 Baggage 键值的上下文；键必须符合 ``xihan.<name>``。"""
        _check_baggage_key(key)
        return self._derive({**self._baggage, key: value})

    def without_unregistered_baggage(self) -> Self:
        """派生一个只保留已登记键的上下文：载体编码时剥离未登记的键（RFC-0003 规则 3）。"""
        return self._derive({key: value for key, value in self._baggage.items() if is_baggage_key_registered(key)})

    def set_item[T](self, key: ItemKey[T], value: T) -> None:
        """在对象袋里放一个值；派生上下文共享对象袋，值不跨进程传播。"""
        self._items.set(key, value)

    def find_item[T](self, key: ItemKey[T]) -> T | None:
        """从对象袋里取一个值；没有时为 ``None``。"""
        return cast(T | None, self._items.get(key))

    def cancellation_error(self, now: datetime | None = None) -> Error | None:
        """把截止时间与取消翻译成原语层错误：已过截止时间为 ``KERNEL_TIMEOUT_001``，信号已触发为 ``KERNEL_CANCELLED_001``。

        两者同时成立时报超时（RFC-0003 规则 1）；都不成立时为 ``None``。这里按时钟判断截止时间，供进入工作之前与
        ``run`` 之外的代码使用；``run`` 里的归因按定时器是否触发。
        """
        if self.is_expired(now):
            return deadline_exceeded_error()
        if self._cancellation.is_cancelled:
            return cancelled_error()
        return None

    async def run[T](self, call: Coroutine[Any, Any, Result[T]]) -> Result[T]:
        """在截止时间与取消信号下执行协程，超时与取消翻译成失败结果。

        进入前已过期或已取消时不执行（关闭协程）。截止时间的定时器触发时取消协程并返回 ``KERNEL_TIMEOUT_001``；信号
        触发时取消协程并返回 ``KERNEL_CANCELLED_001``；两者都触发时报超时。协程自己接住取消、写下 ``KERNEL_CANCELLED_001``
        结果而定时器已触发时，改写为超时（RFC-0004「行为变化」）；它写下的其他结果原样返回。任务被别处取消（不是这个
        信号）时 ``CancelledError`` 照常向外传播，不翻译成结果。
        """
        error = self.cancellation_error()
        if error is not None:
            call.close()
            return fail(error)
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None:
            call.close()
            raise RuntimeError("Context.run must be awaited inside an asyncio task.")
        loop = asyncio.get_running_loop()
        state = _RunState()

        def cancel_task() -> None:
            # 只在事件循环线程里执行；协程已经结束或已经取消过就不再取消，免得取消请求计数失衡
            if not state.finished and not state.signalled:
                state.signalled = True
                task.cancel()

        def on_signal() -> None:
            # 信号可能在别的线程触发：经事件循环回到任务所在的线程；循环已关闭说明这次执行已经结束
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(cancel_task)

        unsubscribe = self._cancellation.on_cancel(on_signal)
        remaining = self.remaining_time()
        timeout = asyncio.timeout(None if remaining is None else remaining.total_seconds())
        try:
            async with timeout:
                try:
                    result = await call
                finally:
                    state.finished = True
        except asyncio.CancelledError:
            if not state.signalled:
                raise
            # 撤回本信号发出的取消请求；还有别的请求说明任务也被别处取消了，照常传播
            if state.withdraw(task) > 0:
                raise
            # 定时器与信号都触发时 asyncio.timeout 不把取消转成 TimeoutError，这里按定时器归因为超时
            return fail(deadline_exceeded_error() if timeout.expired() else cancelled_error())
        except TimeoutError:
            if not timeout.expired():
                raise
            return fail(deadline_exceeded_error())
        finally:
            unsubscribe()
            # 协程接住了本信号的取消后正常返回，或把它转成了别的异常：同样撤回取消请求，计数保持平衡
            state.withdraw(task)
        outcome = result.error
        if timeout.expired() and outcome is not None and outcome.code == ERROR_CODE_CANCELLED:
            return fail(deadline_exceeded_error())
        return result

    def __repr__(self) -> str:
        """便于调试的形式。"""
        return (
            f"Context(trace_parent={self.trace_parent!r}, deadline={self._deadline!r}, baggage={dict(self._baggage)!r})"
        )

    def _derive(self, baggage: dict[str, str]) -> Self:
        """派生：换 Baggage，其余共享。"""
        derived = object.__new__(type(self))
        derived._cancellation = self._cancellation
        derived._deadline = self._deadline
        derived._trace_id = self._trace_id
        derived._span_id = self._span_id
        derived._sampled = self._sampled
        derived._baggage = MappingProxyType(baggage)
        derived._items = self._items
        return derived


def _check_baggage_key(key: str) -> None:
    """Baggage 键必须符合 ``xihan.<name>``，不合法是编程错误。"""
    if not is_baggage_key_well_formed(key):
        raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, f"The baggage key '{key}' must match xihan.<name>.")
