"""invocation 的测试：Go SDK 移植来的追踪、Baggage、截止时间与取消用例，以及 asyncio 下的超时与取消归因。"""

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


async def sleep_then_succeed(seconds: float) -> Result[Unit]:
    """睡一会再成功；被取消时 CancelledError 照常传播。"""
    await asyncio.sleep(seconds)
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


def test_deadline_cancels_and_translates() -> None:
    ctx = Context(timeout=0.02)
    assert ctx.deadline is not None
    remaining = ctx.remaining_time()
    assert remaining is not None
    assert remaining <= timedelta(milliseconds=20)
    outcome = asyncio.run(ctx.run(sleep_then_succeed(5)))
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_DEADLINE_EXCEEDED
    assert outcome.error.message_key == MESSAGE_DEADLINE_EXCEEDED
    assert ctx.is_expired()
    error = ctx.cancellation_error()
    assert error is not None
    assert error.code == ERROR_CODE_DEADLINE_EXCEEDED

    signal = Cancellation()
    cancelled = Context(cancellation=signal)

    async def cancel_soon() -> Result[Unit]:
        asyncio.get_running_loop().call_later(0.01, signal.cancel)
        return await cancelled.run(sleep_then_succeed(5))

    outcome = asyncio.run(cancel_soon())
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_CANCELLED
    assert outcome.error.message_key == MESSAGE_CANCELLED
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


def test_expired_or_cancelled_contexts_do_not_start_the_call() -> None:
    started: list[bool] = []

    async def call() -> Result[Unit]:
        started.append(True)
        return success()

    expired = Context(timeout=-1)
    outcome = asyncio.run(expired.run(call()))
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_DEADLINE_EXCEEDED
    signal = Cancellation()
    signal.cancel()
    outcome = asyncio.run(Context(cancellation=signal).run(call()))
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_CANCELLED
    # 协程被关闭而不是丢下：filterwarnings=error 会把“never awaited”变成失败
    assert started == []


def test_deadline_wins_when_both_fire() -> None:
    # 截止时间到了一律报超时，调用方同时取消也一样（RFC-0003 规则 1）
    signal = Cancellation()
    ctx = Context(cancellation=signal, timeout=0.01)

    async def swallow_and_report_cancelled() -> Result[Unit]:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            signal.cancel()
            await asyncio.sleep(0)
            return fail(cancelled_error())
        return success()

    async def main() -> Result[Unit]:
        outcome = await ctx.run(swallow_and_report_cancelled())
        current = asyncio.current_task()
        assert current is not None
        assert current.cancelling() == 0, "the cancel requests must be balanced"
        return outcome

    outcome = asyncio.run(main())
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_DEADLINE_EXCEEDED
    error = ctx.cancellation_error()
    assert error is not None
    assert error.code == ERROR_CODE_DEADLINE_EXCEEDED


def test_downstream_results_are_kept_unless_the_deadline_fired() -> None:
    signal = Cancellation()

    async def report_cancelled() -> Result[Unit]:
        return fail(cancelled_error())

    outcome = asyncio.run(Context(cancellation=signal).run(report_cancelled()))
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_CANCELLED

    async def swallow() -> Result[Unit]:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return success()
        return success()

    async def cancel_and_swallow() -> Result[Unit]:
        asyncio.get_running_loop().call_later(0.01, signal.cancel)
        outcome = await Context(cancellation=signal).run(swallow())
        current = asyncio.current_task()
        assert current is not None
        assert current.cancelling() == 0
        return outcome

    assert asyncio.run(cancel_and_swallow()).value is UNIT


def test_external_task_cancellation_propagates() -> None:
    async def main() -> None:
        task = asyncio.create_task(Context(timeout=5).run(sleep_then_succeed(5)))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_cancellation_can_come_from_another_thread() -> None:
    signal = Cancellation()

    async def main() -> Result[Unit]:
        threading.Timer(0.02, signal.cancel).start()
        return await Context(cancellation=signal).run(sleep_then_succeed(5))

    outcome = asyncio.run(main())
    assert outcome.error is not None
    assert outcome.error.code == ERROR_CODE_CANCELLED


def test_cancellation_signal_wakes_waiters_and_callbacks() -> None:
    signal = Cancellation()
    calls: list[str] = []
    unsubscribe = signal.on_cancel(lambda: calls.append("removed"))
    unsubscribe()
    signal.on_cancel(lambda: calls.append("first"))

    async def main() -> None:
        asyncio.get_running_loop().call_later(0.01, signal.cancel)
        await signal.wait()

    asyncio.run(main())
    assert signal.is_cancelled
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


def test_run_outside_a_task_is_rejected() -> None:
    async def call() -> Result[Unit]:
        return success()

    coroutine = Context().run(call())
    with pytest.raises(RuntimeError, match="asyncio task"):
        coroutine.send(None)
    coroutine.close()


def test_cancellation_converted_to_another_exception_keeps_the_count_balanced() -> None:
    signal = Cancellation()

    async def convert() -> Result[Unit]:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise LookupError("converted") from None
        return success()

    async def main() -> int:
        asyncio.get_running_loop().call_later(0.01, signal.cancel)
        with pytest.raises(LookupError):
            await Context(cancellation=signal).run(convert())
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
