"""调用上下文（RFC-0003）：取消、截止时间、W3C 追踪、已登记的 Baggage 与请求作用域对象袋。"""

import asyncio
import contextlib
import re
import secrets
import threading
from collections.abc import Awaitable, Callable, Mapping
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
    MAX_BAGGAGE_VALUE_LENGTH,
    Error,
    FrameworkError,
    Result,
    fail,
    is_baggage_key_registered,
    is_baggage_key_well_formed,
    is_baggage_value_acceptable,
)

#: 调用超过截止时间。
ERROR_CODE_DEADLINE_EXCEEDED: Final = "KERNEL_TIMEOUT_001"
#: 调用被取消。
ERROR_CODE_CANCELLED: Final = "KERNEL_CANCELLED_001"
#: 超时的消息键。
MESSAGE_DEADLINE_EXCEEDED: Final = "kernel.invocation.deadline_exceeded"
#: 取消的消息键。
MESSAGE_CANCELLED: Final = "kernel.invocation.cancelled"
#: 调用开始时截止时间已过的消息键（码同样是 ``KERNEL_TIMEOUT_001``）。
MESSAGE_DEADLINE_BEFORE_START: Final = "kernel.invocation.deadline_before_start"

# W3C traceparent：version-traceid-spanid-flags，全小写十六进制
# W3C traceparent 只认版本 00 与小写十六进制（RFC-0003 规则 7，ADR-0235）；其他版本、大写、多余的段都不合法
_TRACE_PARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_TRACE_ID = re.compile(r"[0-9a-f]{32}")
_SPAN_ID = re.compile(r"[0-9a-f]{16}")
_ZERO_TRACE_ID: Final = "0" * 32
_ZERO_SPAN_ID: Final = "0" * 16


class TraceParent(NamedTuple):
    """W3C traceparent（RFC-0001 ``TraceParent``）：trace-id、parent-id 与采样位；标志的其余位不进上下文。"""

    trace_id: str
    span_id: str
    sampled: bool

    def __str__(self) -> str:
        """写出的文本：版本 ``00``，标志只写采样位。"""
        return format_trace_parent(self.trace_id, self.span_id, self.sampled)


def parse_trace_parent(text: str) -> TraceParent | None:
    """解析 W3C traceparent：只认 ``^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$``，ID 全零也不合法，不合法时为 ``None``。

    是否采样只看标志的最低位；不合法的 traceparent 当作没有给出，不报错。
    """
    match = _TRACE_PARENT.fullmatch(text)
    if match is None or match.group(1) == _ZERO_TRACE_ID or match.group(2) == _ZERO_SPAN_ID:
        return None
    return TraceParent(match.group(1), match.group(2), bool(int(match.group(3), 16) & 0x01))


def format_trace_parent(trace_id: str, span_id: str, sampled: bool | None) -> str:
    """写出 traceparent：版本 ``00``，标志只写采样位 ``00`` / ``01``；``sampled`` 为 ``None``（没有上游、没有采样器）时写 ``01``。"""
    return f"00-{trace_id}-{span_id}-{'00' if sampled is False else '01'}"


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


def deadline_before_start_error() -> Error:
    """调用开始时截止时间已过的原语层错误（``KERNEL_TIMEOUT_001`` + ``kernel.invocation.deadline_before_start``）。"""
    return Error.timeout(ERROR_CODE_DEADLINE_EXCEEDED, MESSAGE_DEADLINE_BEFORE_START)


def _ignore() -> None:
    """什么都不做的退订函数。"""


class Cancellation:
    """一次调用的取消信号：只触发一次，可以从任何线程触发。

    持有它的一方（协议宿主、插件运行时）调用 ``cancel``；上下文与它派生的上下文共享同一个信号。取消只经信号通知，
    谁也不去取消处理器的任务（RFC-0003 规则 1）：处理器读 ``is_cancelled``、``await wait()`` 或调用
    ``raise_if_cancelled()`` 自己配合。订阅在退订或触发后即移除，所以回调表的大小不超过同时在等的订阅数。
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

    def raise_if_cancelled(self) -> None:
        """已触发时抛出 ``asyncio.CancelledError``；``Context.run`` 按触发的信号把它归因为超时或取消。"""
        if self._cancelled:
            raise asyncio.CancelledError

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


class Context:
    """一次调用的上下文（RFC-0003）：不可变，``with_*`` 方法派生新的上下文。

    派生的上下文共享取消信号、截止时间、追踪与对象袋，Baggage 按值复制。截止时间是 UTC 的绝对时刻；``run`` 在
    截止时间与取消信号下执行一次调用，取消与截止时间只经信号通知，超时与取消翻译成 ``KERNEL_TIMEOUT_001`` /
    ``KERNEL_CANCELLED_001``。
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
        sampled: bool | None = None,
        baggage: Mapping[str, str] | None = None,
    ) -> None:
        """创建上下文。

        ``cancellation`` 省略时创建新的信号（经 ``cancellation`` 属性取得）；``deadline`` 是带时区的绝对时刻，
        ``timeout`` 是相对现在的时长（秒或 ``timedelta``），二者至多给一个。``trace_parent`` 是外部输入，
        不合法时当作没有给出；``trace_id`` / ``span_id`` 是调用方给的值，不合法即编程错误。没有追踪时生成新的 Trace ID
        与 Span ID；``sampled`` 是上游的采样决定，``None`` 表示没有上游（RFC-0003 规则 7），写出时按已采样写 ``01``。Baggage 是外部输入：键必须符合 ``xihan.<name>``，已登记键的值不合约束的成员丢弃
        （``xihan.idempotency-key`` 除外，见 ``is_baggage_value_acceptable``）。
        """
        if deadline is not None and timeout is not None:
            raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, "A context takes either a deadline or a timeout.")
        if trace_parent is not None and (trace_id is not None or span_id is not None or sampled is not None):
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
            if _admits(key, value):
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
    def sampled(self) -> bool | None:
        """上游的采样决定；``None`` 表示没有上游，这次调用是链路的根。"""
        return self._sampled

    @property
    def trace_parent(self) -> str:
        """W3C traceparent：版本 ``00``，标志只写采样位，没有上游时写 ``01``（RFC-0003 规则 7）。"""
        return format_trace_parent(self._trace_id, self._span_id, self._sampled)

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
        """派生一个多一个 Baggage 键值的上下文；键必须符合 ``xihan.<name>``。

        程序内逐项派生给已登记的键一个不合约束的值（见 ``is_baggage_value_acceptable``）是编程错误，抛出
        ``FrameworkError``（``ValueError``，``PRIMITIVES_VALIDATION_001``），与键不合文法时一样，不静默丢弃也不保留原值
        （conventions/baggage-keys.md，ADR-0235）；``xihan.idempotency-key`` 例外，原样带上。外部输入经构造函数进入，
        在那里丢弃不合的成员。
        """
        _check_baggage_key(key)
        if not _admits(key, value):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT,
                f"The value of the baggage key '{key}' must be non-empty, at most {MAX_BAGGAGE_VALUE_LENGTH} UTF-16 code "
                "units long and free of control characters.",
            )
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

    def with_cancellation(self, cancellation: Cancellation) -> Self:
        """派生一个换了取消信号的上下文；截止时间、追踪、Baggage 与对象袋不变。"""
        return self._derive(dict(self._baggage), cancellation)

    async def run[T](self, call: Callable[["Context"], Awaitable[Result[T]]]) -> Result[T]:
        """在截止时间与取消信号下执行 ``call``，超时与取消翻译成失败结果；调用器的 Resilience 阶段内置步骤就是它。

        进入时截止时间已过即返回 ``KERNEL_TIMEOUT_001`` + ``kernel.invocation.deadline_before_start``，``call`` 不执行；
        进入时不核对取消（RFC-0011 规则 10，ADR-0236，与 .NET 的 ``DeadlineMiddleware`` 一致）。

        ``call`` 收到一个派生的上下文，它的取消信号在调用方的信号触发或到达截止时间时触发。取消与截止时间只经这个信号
        通知（RFC-0003 规则 1，ADR-0236）：不取消 ``call`` 所在的任务，``call`` 自己看信号（``is_cancelled``、
        ``cancellation.wait()``、``cancellation.raise_if_cancelled()``）决定何时停下。信号触发之后 ``call`` 照常返回结果的，
        以那个结果结束；``call`` 抛出 ``CancelledError`` 或写下 ``KERNEL_CANCELLED_001`` 结果时按触发的信号归因，截止时间
        已到即 ``KERNEL_TIMEOUT_001``（调用方同时取消也一样），否则 ``KERNEL_CANCELLED_001``。任务本身被别处取消时
        ``CancelledError`` 照常向外传播。
        """
        if self.is_expired():
            return fail(deadline_before_start_error())
        linked = Cancellation()
        expired: list[bool] = []
        loop = asyncio.get_running_loop()
        timer: list[asyncio.TimerHandle] = []

        def on_deadline() -> None:
            # asyncio 把时钟精度之内到期的定时器当作已到期，Windows 上的 Python 3.12 可能早约 15 ms 触发；
            # 按时钟还没到截止时间就再等剩下的时间，信号不早于截止时间，与 is_expired、cancellation_error 一致
            if not self.is_expired():
                remaining = self.remaining_time() or timedelta(0)
                timer[0] = loop.call_later(remaining.total_seconds(), on_deadline)
                return
            expired.append(True)
            linked.cancel()

        # 调用方的信号可能已经触发：on_cancel 立即执行回调，下游一进来就看到已取消
        unsubscribe = self._cancellation.on_cancel(linked.cancel)
        remaining = self.remaining_time()
        if remaining is not None:
            timer.append(loop.call_later(remaining.total_seconds(), on_deadline))
        try:
            result = await call(self.with_cancellation(linked))
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if (task is not None and task.cancelling() > 0) or not linked.is_cancelled:
                raise
            return fail(deadline_exceeded_error() if expired else cancelled_error())
        finally:
            unsubscribe()
            if timer:
                timer[0].cancel()
        outcome = result.error
        if expired and outcome is not None and outcome.code == ERROR_CODE_CANCELLED:
            # 下游把取消写成了结果：截止时间已到即按超时报告（RFC-0004「行为变化」）
            return fail(deadline_exceeded_error())
        return result

    def __repr__(self) -> str:
        """便于调试的形式。"""
        return (
            f"Context(trace_parent={self.trace_parent!r}, deadline={self._deadline!r}, baggage={dict(self._baggage)!r})"
        )

    def _derive(self, baggage: dict[str, str], cancellation: Cancellation | None = None) -> Self:
        """派生：换 Baggage（与取消信号），其余共享。"""
        derived = object.__new__(type(self))
        derived._cancellation = self._cancellation if cancellation is None else cancellation
        derived._deadline = self._deadline
        derived._trace_id = self._trace_id
        derived._span_id = self._span_id
        derived._sampled = self._sampled
        derived._baggage = MappingProxyType(baggage)
        derived._items = self._items
        return derived


def _admits(key: str, value: str) -> bool:
    """Baggage 成员能否进入上下文：已登记键的值要合约束，幂等键与未登记的键不按这条丢弃（conventions/baggage-keys.md）。"""
    if not is_baggage_key_registered(key) or key == BAGGAGE_IDEMPOTENCY_KEY:
        return True
    return is_baggage_value_acceptable(value)


def _check_baggage_key(key: str) -> None:
    """Baggage 键必须符合 ``xihan.<name>``，不合法是编程错误。"""
    if not is_baggage_key_well_formed(key):
        raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, f"The baggage key '{key}' must match xihan.<name>.")
