"""invocation 的测试：追踪、Baggage、截止时间与取消，取消与截止时间只经信号通知（RFC-0003 规则 1）。"""

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from xihan_framework.invocation import (
    ERROR_CODE_CANCELLED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    MESSAGE_CANCELLED,
    MESSAGE_DEADLINE_BEFORE_START,
    MESSAGE_DEADLINE_EXCEEDED,
    Cancellation,
    Context,
    ItemKey,
    cancelled_error,
    format_trace_parent,
    parse_trace_parent,
)
from xihan_framework.primitives import (
    BAGGAGE_CORRELATION,
    BAGGAGE_IDEMPOTENCY_KEY,
    BAGGAGE_TENANT,
    ERROR_CODE_INVALID_ARGUMENT,
    UNIT,
    FrameworkError,
    Result,
    Unit,
    fail,
    success,
)


async def sleep_then_succeed(ctx: Context) -> Result[Unit]:
    """不看信号，睡一会再成功；任务被取消时 CancelledError 照常传播。"""
    await asyncio.sleep(5)
    return success()


async def report_on_signal(ctx: Context) -> Result[Unit]:
    """配合信号的调用：等到信号触发，把取消写成结果。"""
    await ctx.cancellation.wait()
    return fail(cancelled_error())


async def run_within_the_loop(make: Callable[[], Context], call: Any) -> tuple[Context, Result[Unit]]:
    """在事件循环里才创建上下文再执行：建事件循环本身可能就超过很短的截止时间。"""
    ctx = make()
    outcome: Result[Unit] = await ctx.run(call)
    return ctx, outcome


async def raise_on_signal(ctx: Context) -> Result[Unit]:
    """配合信号的调用：等到信号触发，抛出取消。"""
    await ctx.cancellation.wait()
    ctx.cancellation.raise_if_cancelled()
    return success()


def test_new_generates_trace_and_keeps_baggage() -> None:
    ctx = Context(baggage={BAGGAGE_TENANT: "t1"})
    assert len(ctx.trace_id) == 32
    assert len(ctx.span_id) == 16
    assert ctx.tenant == "t1"
    assert ctx.principal is None
    derived = ctx.with_baggage(BAGGAGE_CORRELATION, "c1").with_baggage("xihan.feature-flag", "on")
    assert derived.correlation == "c1"
    assert ctx.correlation is None, "derived baggage must not leak back"
    assert "xihan.feature-flag" not in derived.without_unregistered_baggage().baggage
    assert derived.without_unregistered_baggage().correlation == "c1"
    key = ItemKey[int]("key")
    derived.set_item(key, 1)
    assert ctx.find_item(key) == 1, "items are shared between derived contexts"
    assert ctx.find_item(ItemKey[int]("key")) is None, "keys compare by identity"
    parsed = parse_trace_parent(ctx.trace_parent)
    assert parsed is not None
    assert (parsed.trace_id, parsed.span_id, parsed.sampled) == (ctx.trace_id, ctx.span_id, True)
    assert derived.cancellation is ctx.cancellation
    assert derived.trace_id == ctx.trace_id
    # 换取消信号的派生：其余不变，对象袋共享
    other = Cancellation()
    scoped = derived.with_cancellation(other)
    assert scoped.cancellation is other
    assert (scoped.correlation, scoped.trace_id, scoped.find_item(key)) == ("c1", ctx.trace_id, 1)
    assert derived.cancellation is ctx.cancellation


def test_the_deadline_fires_the_signal_and_translates() -> None:
    ctx = Context(timeout=0.02)
    assert ctx.deadline is not None
    remaining = ctx.remaining_time()
    assert remaining is not None
    assert remaining <= timedelta(milliseconds=20)
    # 截止时间只经信号通知（RFC-0003 规则 1，ADR-0236）：下游把取消写成结果或抛出取消，都按截止时间归因为超时
    for call in (report_on_signal, raise_on_signal):
        ctx, outcome = asyncio.run(run_within_the_loop(lambda: Context(timeout=0.02), call))
        assert outcome.error is not None
        assert (outcome.error.code, outcome.error.message_key) == (
            ERROR_CODE_DEADLINE_EXCEEDED,
            MESSAGE_DEADLINE_EXCEEDED,
        )
    assert ctx.is_expired()
    assert not ctx.is_cancelled, "the deadline fires the derived signal, not the caller's"
    error = ctx.cancellation_error()
    assert error is not None
    assert error.code == ERROR_CODE_DEADLINE_EXCEEDED

    for call in (report_on_signal, raise_on_signal):
        signal = Cancellation()
        cancelled = Context(cancellation=signal)

        async def cancel_soon(
            context: Context = cancelled, source: Cancellation = signal, chosen: Any = call
        ) -> Result[Unit]:
            asyncio.get_running_loop().call_later(0.01, source.cancel)
            result: Result[Unit] = await context.run(chosen)
            return result

        outcome = asyncio.run(cancel_soon())
        assert outcome.error is not None
        assert (outcome.error.code, outcome.error.message_key) == (ERROR_CODE_CANCELLED, MESSAGE_CANCELLED)
        error = cancelled.cancellation_error()
        assert error is not None
        assert error.code == ERROR_CODE_CANCELLED
    assert Context().cancellation_error() is None


def test_trace_parent_parsing() -> None:
    assert parse_trace_parent("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01") is not None
    for bad in [
        "",
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331",
        "00-00000000000000000000000000000000-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
        "00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01\n",
    ]:
        assert parse_trace_parent(bad) is None, bad
    ctx = Context(trace_parent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00")
    assert ctx.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert not ctx.sampled
    assert ctx.trace_parent == "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"
    # traceparent 是外部输入：不合法时忽略并生成新的追踪
    assert len(Context(trace_parent="garbage").trace_id) == 32


def test_malformed_arguments_are_programming_errors() -> None:
    with pytest.raises(FrameworkError) as raised:
        Context().with_baggage("tenant", "t1")
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT
    with pytest.raises(FrameworkError):
        Context(baggage={"XiHan.tenant": "t1"})
    with pytest.raises(FrameworkError):
        Context(deadline=datetime.now(UTC), timeout=1)
    with pytest.raises(FrameworkError):
        Context(deadline=datetime(2030, 1, 1))
    with pytest.raises(FrameworkError):
        Context(trace_id="0" * 32)
    with pytest.raises(FrameworkError):
        Context(span_id="ABCDEF0123456789")
    with pytest.raises(FrameworkError):
        Context(trace_parent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", trace_id="1" * 32)


def test_deadlines_convert_to_utc() -> None:
    local = datetime(2030, 1, 1, 8, 0, tzinfo=UTC).astimezone()
    ctx = Context(deadline=local)
    assert ctx.deadline == datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    assert ctx.deadline is not None
    assert ctx.deadline.tzinfo is UTC
    assert ctx.is_expired(datetime(2030, 1, 1, 8, 0, tzinfo=UTC))
    assert not ctx.is_expired(datetime(2030, 1, 1, 7, 59, tzinfo=UTC))
    assert ctx.remaining_time(datetime(2030, 1, 1, 9, 0, tzinfo=UTC)) == timedelta(0)
    assert Context().remaining_time() is None


def test_expired_contexts_do_not_start_the_call_but_cancelled_ones_do() -> None:
    started: list[object] = []

    async def call(ctx: Context) -> Result[Unit]:
        started.append(ctx.is_cancelled)
        return success()

    # 进入时截止时间已过：deadline_before_start，call 不执行
    outcome = asyncio.run(Context(timeout=-1).run(call))
    assert outcome.error is not None
    assert (outcome.error.code, outcome.error.message_key) == (
        ERROR_CODE_DEADLINE_EXCEEDED,
        MESSAGE_DEADLINE_BEFORE_START,
    )
    assert started == []
    # 进入时不核对取消（RFC-0011 规则 10）：call 照常执行，一进来就看到已触发的信号，照常返回的结果就是调用的结果
    signal = Cancellation()
    signal.cancel()
    assert asyncio.run(Context(cancellation=signal).run(call)).is_ok
    assert started == [True]
    outcome = asyncio.run(Context(cancellation=signal).run(report_on_signal))
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_CANCELLED


def test_the_signal_never_interrupts_the_call() -> None:
    # RFC-0003 规则 1（ADR-0236）：不取消 call 所在的任务；信号触发后照常返回结果的，调用以那个结果结束
    seen: list[bool] = []

    async def ignore(ctx: Context) -> Result[Unit]:
        await asyncio.sleep(0.05)
        seen.append(ctx.is_cancelled)
        return success()

    async def main(make: Callable[[], Context]) -> Result[Unit]:
        _, outcome = await run_within_the_loop(make, ignore)
        current = asyncio.current_task()
        assert current is not None
        assert current.cancelling() == 0, "the task is never cancelled"
        return outcome

    assert asyncio.run(main(lambda: Context(timeout=0.01))).value is UNIT
    signal = Cancellation()

    async def cancel_during() -> Result[Unit]:
        asyncio.get_running_loop().call_later(0.01, signal.cancel)
        return await main(lambda: Context(cancellation=signal))

    assert asyncio.run(cancel_during()).value is UNIT
    assert seen == [True, True]


def test_deadline_wins_when_both_fire() -> None:
    # 截止时间到了一律报超时，调用方同时取消也一样（RFC-0003 规则 1）
    for raising in (False, True):
        signal = Cancellation()

        async def cancel_after_the_deadline(
            scoped: Context, should_raise: bool = raising, caller: Cancellation = signal
        ) -> Result[Unit]:
            await scoped.cancellation.wait()
            caller.cancel()
            if should_raise:
                scoped.cancellation.raise_if_cancelled()
            return fail(cancelled_error())

        def make(source: Cancellation = signal) -> Context:
            return Context(cancellation=source, timeout=0.01)

        ctx, outcome = asyncio.run(run_within_the_loop(make, cancel_after_the_deadline))
        assert outcome.error is not None
        assert outcome.error.code == ERROR_CODE_DEADLINE_EXCEEDED
        error = ctx.cancellation_error()
        assert error is not None
        assert error.code == ERROR_CODE_DEADLINE_EXCEEDED


def test_downstream_results_are_kept_unless_the_deadline_fired() -> None:
    async def report_cancelled(ctx: Context) -> Result[Unit]:
        return fail(cancelled_error())

    outcome = asyncio.run(Context().run(report_cancelled))
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_CANCELLED
    signal = Cancellation()

    async def swallow(ctx: Context) -> Result[Unit]:
        await ctx.cancellation.wait()
        return success()

    async def cancel_and_swallow() -> Result[Unit]:
        asyncio.get_running_loop().call_later(0.01, signal.cancel)
        return await Context(cancellation=signal).run(swallow)

    assert asyncio.run(cancel_and_swallow()).value is UNIT


def test_external_task_cancellation_propagates() -> None:
    signal = Cancellation()

    async def main(ctx: Context) -> None:
        task = asyncio.create_task(ctx.run(sleep_then_succeed))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main(Context(timeout=5)))
    # 信号已触发也一样：任务本身被别处取消时不翻译成结果
    signal.cancel()
    asyncio.run(main(Context(cancellation=signal)))


def test_cancellation_can_come_from_another_thread() -> None:
    signal = Cancellation()

    async def main() -> Result[Unit]:
        threading.Timer(0.02, signal.cancel).start()
        return await Context(cancellation=signal).run(report_on_signal)

    outcome = asyncio.run(main())
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_CANCELLED


def test_cancellation_signal_wakes_waiters_and_callbacks() -> None:
    signal = Cancellation()
    calls: list[str] = []
    unsubscribe = signal.on_cancel(lambda: calls.append("removed"))
    unsubscribe()
    signal.on_cancel(lambda: calls.append("first"))
    signal.raise_if_cancelled()

    async def main() -> None:
        asyncio.get_running_loop().call_later(0.01, signal.cancel)
        await signal.wait()

    asyncio.run(main())
    assert signal.is_cancelled
    with pytest.raises(asyncio.CancelledError):
        signal.raise_if_cancelled()
    signal.cancel()
    signal.on_cancel(lambda: calls.append("late"))
    assert calls == ["first", "late"]
    asyncio.run(signal.wait())

    failing = Cancellation()

    def boom() -> None:
        raise ValueError("boom")

    failing.on_cancel(boom)
    failing.on_cancel(lambda: calls.append("still runs"))
    with pytest.raises(ExceptionGroup):
        failing.cancel()
    assert calls[-1] == "still runs"


def test_other_exceptions_propagate() -> None:
    signal = Cancellation()

    async def convert(ctx: Context) -> Result[Unit]:
        await ctx.cancellation.wait()
        raise LookupError("converted")

    async def main() -> int:
        asyncio.get_running_loop().call_later(0.01, signal.cancel)
        with pytest.raises(LookupError):
            await Context(cancellation=signal).run(convert)
        current = asyncio.current_task()
        assert current is not None
        return current.cancelling()

    assert asyncio.run(main()) == 0


def test_contexts_drop_external_baggage_but_reject_derived_values(vector: Callable[[str, str], Any]) -> None:
    values = vector("primitives", "baggage-keys")["values"]
    for value in values["acceptable"]:
        assert Context(baggage={BAGGAGE_TENANT: value}).tenant == value
        assert Context().with_baggage(BAGGAGE_CORRELATION, value).correlation == value
    for value in values["unacceptable"]:
        ctx = Context(baggage={BAGGAGE_TENANT: value, BAGGAGE_IDEMPOTENCY_KEY: value, "xihan.note": value})
        assert ctx.tenant is None, value
        # 幂等键不按这条丢弃，交给 Idempotency 阶段拒绝；未登记的键不出进程，也不受约束
        assert ctx.idempotency_key == value
        assert ctx.baggage["xihan.note"] == value
        # 程序内逐项派生给已登记键一个不合的值是编程错误：抛出，不静默丢弃也不保留原值（ADR-0235）
        with pytest.raises(FrameworkError) as raised:
            Context(baggage={BAGGAGE_CORRELATION: "c1"}).with_baggage(BAGGAGE_CORRELATION, value)
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, value
        assert isinstance(raised.value, ValueError)
        assert Context().with_baggage(BAGGAGE_IDEMPOTENCY_KEY, value).idempotency_key == value
        assert Context().with_baggage("xihan.note", value).baggage["xihan.note"] == value


def test_trace_context_follows_the_vectors(vector: Callable[[str, str], Any]) -> None:
    vectors = vector("primitives", "trace-context")
    for case in vectors["parse"]["valid"]:
        parsed = parse_trace_parent(case["text"])
        assert parsed == (case["traceId"], case["spanId"], case["sampled"]), case["text"]
        ctx = Context(trace_parent=case["text"])
        assert (ctx.trace_id, ctx.span_id, ctx.sampled) == (case["traceId"], case["spanId"], case["sampled"])
    for text in vectors["parse"]["invalid"]:
        assert parse_trace_parent(text) is None, text
        # 不合法的 traceparent 当作没有给出：生成新的追踪，没有上游
        ctx = Context(trace_parent=text)
        assert ctx.sampled is None, text
        assert ctx.trace_parent.endswith("-01")
    for case in vectors["format"]:
        assert format_trace_parent(case["traceId"], case["spanId"], case["sampled"]) == case["text"]
        ctx = Context(trace_id=case["traceId"], span_id=case["spanId"], sampled=case["sampled"])
        assert ctx.trace_parent == case["text"]
        assert ctx.with_baggage(BAGGAGE_TENANT, "t1").trace_parent == case["text"]


def test_trace_flags_write_only_the_sampled_bit() -> None:
    # 上游的标志只取采样位，其余位不进上下文；写出总是 00 或 01（RFC-0003 规则 7，ADR-0235）
    upstream = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-"
    assert Context(trace_parent=upstream + "03").trace_parent == upstream + "01"
    assert Context(trace_parent=upstream + "02").trace_parent == upstream + "00"
    assert str(parse_trace_parent(upstream + "ff")) == upstream + "01"
    assert Context().sampled is None
    with pytest.raises(FrameworkError):
        Context(trace_parent=upstream + "01", sampled=True)
