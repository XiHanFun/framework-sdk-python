"""operation 的测试：调用、类型化调用与登记表，以及容量、载荷错误、超时与取消。"""

import asyncio
import json
import math
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any

import pytest

from xihan_framework import operation, pipeline
from xihan_framework._json import JsonNumber, JsonValue
from xihan_framework.invocation import ERROR_CODE_CANCELLED, ERROR_CODE_DEADLINE_EXCEEDED, Cancellation, Context
from xihan_framework.operation import (
    Contract,
    Expose,
    Int32,
    Invoker,
    Registry,
    Request,
    Response,
    Schema,
    json_field,
)
from xihan_framework.pipeline import Builder, Step
from xihan_framework.primitives import (
    BAGGAGE_CORRELATION,
    ERROR_CODE_INVALID_ARGUMENT,
    Error,
    FrameworkError,
    PipelineStage,
    Result,
    Stability,
    Unit,
    fail,
    ok,
    success,
)


@dataclass(frozen=True)
class CreateOrder:
    quantity: int = 0


@dataclass(frozen=True)
class OrderCreated:
    id: str
    quantity: int


@dataclass(frozen=True)
class Empty:
    pass


async def create_order(ctx: Context, request: CreateOrder) -> Result[OrderCreated]:
    if request.quantity <= 0:
        return fail(
            Error.validation("ORDERS_VALIDATION_001", "orders.quantity.invalid", {"quantity": request.quantity})
        )
    return ok(OrderCreated(id=f"o-{ctx.correlation}", quantity=request.quantity))


def new_invoker(builder: Builder | None = None) -> Invoker:
    registry = Registry()
    registry.add(
        Contract("orders.create", stability=Stability.STABLE, expose=(operation.EXPOSE_HTTP,)),
        create_order,
        request_type=CreateOrder,
        response_type=OrderCreated,
        schema=Schema(request="{}", response="{}"),
    )
    return Invoker(registry, builder)


def invoke(invoker: Invoker, request: Request, ctx: Context | None = None) -> Response:
    return asyncio.run(invoker.invoke(Context() if ctx is None else ctx, request))


def invoke_with_timeout(invoker: Invoker, request: Request, seconds: float) -> Response:
    """在事件循环里才创建带截止时间的上下文：建事件循环本身可能就超过很短的截止时间。"""

    async def main() -> Response:
        return await invoker.invoke(Context(timeout=seconds), request)

    return asyncio.run(main())


def error_of(response: Response) -> Error:
    assert response.outcome.error is not None, response
    return response.outcome.error


def test_invoke_runs_the_pipeline_and_encodes_the_response() -> None:
    seen: list[str] = []

    async def observe(ctx: Context, next_step: Step) -> Result[Unit]:
        descriptor = operation.current(ctx)
        assert descriptor is not None
        seen.append(descriptor.contract.id)
        return await next_step(ctx)

    invoker = new_invoker(Builder().use_at(pipeline.func("Observe", observe), PipelineStage.OBSERVE))
    ctx = Context(baggage={BAGGAGE_CORRELATION: "c1"})
    response = invoke(invoker, Request("orders.create", b'{"quantity":2}'), ctx)
    assert response.outcome.is_ok
    assert response.payload == b'{"id":"o-c1","quantity":2}'
    assert seen == ["orders.create"]

    failed = invoke(invoker, Request("orders.create", b'{"quantity":0}'))
    assert error_of(failed).code == "ORDERS_VALIDATION_001"
    assert failed.payload is None

    missing = invoke(invoker, Request("orders.missing"))
    assert error_of(missing).code == operation.ERROR_CODE_NOT_FOUND
    assert error_of(missing).details == {"id": "orders.missing"}

    for payload in [b"null", b"[1]", b"{", b'{"quantity":"2"}', b'{"quantity":2.5}', b"\xff"]:
        invalid = invoke(invoker, Request("orders.create", payload))
        assert error_of(invalid).code == operation.ERROR_CODE_INVALID_PAYLOAD, payload
    empty = invoke(invoker, Request("orders.create"))
    assert error_of(empty).code == "ORDERS_VALIDATION_001", "an empty payload is {}"
    # 只有空载荷等价于 {}；只有空白不是 JSON（RFC-0001「JSON 文本」，ADR-0236）
    assert error_of(invoke(invoker, Request("orders.create", b"  \n"))).code == operation.ERROR_CODE_INVALID_PAYLOAD


def test_invalid_payloads_report_only_the_json_path() -> None:
    invoker = new_invoker()
    cases = [(b"null", "$"), (b"{", "$"), (b"[1]", "$"), (b'{"quantity":"2"}', "$.quantity")]
    for payload, reason in cases:
        error = error_of(invoke(invoker, Request("orders.create", payload)))
        assert error.message_key == operation.MESSAGE_INVALID_PAYLOAD
        assert error.details == {"id": "orders.create", "reason": reason}, payload


def test_typed_invoke_checks_types() -> None:
    invoker = new_invoker()
    ctx = Context(baggage={BAGGAGE_CORRELATION: "c2"})
    created = asyncio.run(invoker.invoke_typed(ctx, "orders.create", CreateOrder(quantity=3), OrderCreated))
    assert created.value == OrderCreated(id="o-c2", quantity=3)
    mismatch = asyncio.run(invoker.invoke_typed(Context(), "orders.create", CreateOrder(quantity=3), str))
    assert mismatch.error is not None
    assert mismatch.error.code == operation.ERROR_CODE_TYPE_MISMATCH
    # 只带 id：类型名是实现语言的，各语言写不出同样的参数（ADR-0236）
    assert mismatch.error.details == {"id": "orders.create"}
    assert mismatch.error.message == (
        "Operation 'orders.create' was invoked with request or response types that do not match its registration."
    )
    wrong_request = asyncio.run(invoker.invoke_typed(Context(), "orders.create", Empty(), OrderCreated))
    assert wrong_request.error is not None
    assert wrong_request.error.code == operation.ERROR_CODE_TYPE_MISMATCH
    missing = asyncio.run(invoker.invoke_typed(Context(), "orders.missing", CreateOrder(), OrderCreated))
    assert missing.error is not None
    assert missing.error.code == operation.ERROR_CODE_NOT_FOUND


def test_registry_rejects_duplicates_and_bad_ids() -> None:
    registry = Registry()

    async def handle(ctx: Context, request: Empty) -> Result[Empty]:
        return ok(Empty())

    with pytest.raises(FrameworkError) as raised:
        Contract("Orders.Create")
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT
    with pytest.raises(FrameworkError):
        Contract("orders.create", permission="p", policy="q")
    with pytest.raises(FrameworkError):
        Contract("orders.create", permission="")
    registry.add(Contract("orders.create"), handle, request_type=Empty, response_type=Empty)
    with pytest.raises(FrameworkError) as raised:
        registry.add(Contract("orders.create"), handle, request_type=Empty, response_type=Empty)
    assert raised.value.code == operation.ERROR_CODE_DUPLICATE
    listed = registry.descriptors()
    assert len(listed) == 1
    assert listed[0].owner == pipeline.APP_OWNER
    assert listed[0].contract.stability is Stability.STABLE
    assert operation.is_id("order-lines.list")
    assert not operation.is_id("orders")
    assert not operation.is_id("orders.list\n")
    with pytest.raises(FrameworkError):
        registry.add(Contract("orders.update"), handle, request_type=Empty, response_type=Empty, owner="")


def test_operation_ids_follow_the_vectors(vector: Callable[[str, str], Any]) -> None:
    # conventions/operation-ids.md、RFC-0011「操作 ID」（ADR-0276）：文法加 128 个字符的上限，判定与契约用同一条规则
    vectors = vector("primitives", "operation-ids")
    limit = vectors["maxLength"]
    assert vectors["valid"]
    assert vectors["invalid"]
    assert limit == operation.MAX_ID_LENGTH
    for id in vectors["valid"]:
        assert operation.is_id(id), id
        assert Contract(id).id == id
    for id in vectors["invalid"]:
        assert not operation.is_id(id), repr(id)
        with pytest.raises(FrameworkError) as raised:
            Contract(id)
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, repr(id)
        assert raised.value.message == (
            f"The operation ID '{id}' must match <resource>.<verb> and be at most {limit} characters long."
        )
    # 向量把上限的两侧都带上：正好在上限上的合法，别处都合文法、只多一个字符的不合法
    assert any(len(id) == limit for id in vectors["valid"])
    assert any(len(id) == limit + 1 for id in vectors["invalid"])
    # 上限按整个 ID 计，与哪一段长无关
    long = "a" * (limit - 2)
    assert operation.is_id(f"{long}.b")
    assert not operation.is_id(f"{long}a.b")
    assert operation.is_id(f"a.{long}")
    assert not operation.is_id(f"a.{long}b")


def test_registry_capacity_is_ten_thousand() -> None:
    registry = Registry()

    async def handle(ctx: Context, request: Empty) -> Result[Empty]:
        return ok(Empty())

    for index in range(operation.CAPACITY):
        registry.add(Contract(f"items{index}.get"), handle, request_type=Empty, response_type=Empty)
    assert len(registry) == operation.CAPACITY == 10000
    with pytest.raises(FrameworkError) as raised:
        registry.add(Contract("overflow.get"), handle, request_type=Empty, response_type=Empty)
    assert raised.value.code == operation.ERROR_CODE_REGISTRY_FULL
    # 登记表满了也先报重复：重复是这个 ID 自己的问题，与容量无关
    with pytest.raises(FrameworkError) as raised:
        registry.add(Contract("items0.get"), handle, request_type=Empty, response_type=Empty)
    assert raised.value.code == operation.ERROR_CODE_DUPLICATE
    descriptors = registry.descriptors()
    assert [descriptor.id for descriptor in descriptors] == sorted(descriptor.id for descriptor in descriptors)


def test_concurrent_registrations_respect_uniqueness() -> None:
    registry = Registry()
    failures: list[str] = []

    async def handle(ctx: Context, request: Empty) -> Result[Empty]:
        return ok(Empty())

    def register(worker: int) -> None:
        for index in range(200):
            try:
                registry.add(Contract(f"items{index}.get"), handle, request_type=Empty, response_type=Empty)
            except FrameworkError as error:
                failures.append(error.code)

    threads = [threading.Thread(target=register, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(registry) == 200
    assert failures == [operation.ERROR_CODE_DUPLICATE] * 600


def test_deadlines_and_cancellation_end_the_invocation() -> None:
    # 取消与截止时间只经信号通知（RFC-0003 规则 1，ADR-0236）：处理器看信号停下，调用器不取消它的任务
    registry = Registry()

    async def cooperative(ctx: Context, request: Empty) -> Result[Empty]:
        await ctx.cancellation.wait()
        ctx.cancellation.raise_if_cancelled()
        return ok(Empty())

    async def reports_cancelled(ctx: Context, request: Empty) -> Result[Empty]:
        await ctx.cancellation.wait()
        cancelled = ctx.cancellation_error()
        assert cancelled is not None
        return fail(cancelled)

    async def ignores(ctx: Context, request: Empty) -> Result[Empty]:
        await asyncio.sleep(0.05)
        assert ctx.is_cancelled
        return ok(Empty())

    registry.add(Contract("reports.export"), cooperative, request_type=Empty, response_type=Empty)
    registry.add(Contract("reports.check"), reports_cancelled, request_type=Empty, response_type=Empty)
    registry.add(Contract("reports.ignore"), ignores, request_type=Empty, response_type=Empty)
    invoker = Invoker(registry)
    for id in ["reports.export", "reports.check"]:
        timed_out = invoke_with_timeout(invoker, Request(id), 0.02)
        assert error_of(timed_out).code == ERROR_CODE_DEADLINE_EXCEEDED, id
        signal = Cancellation()

        async def cancel_soon(source: Cancellation = signal, chosen: str = id) -> Response:
            asyncio.get_running_loop().call_later(0.02, source.cancel)
            return await invoker.invoke(Context(cancellation=source), Request(chosen))

        assert error_of(asyncio.run(cancel_soon())).code == ERROR_CODE_CANCELLED, id

    async def typed_with_timeout() -> Result[Empty]:
        return await invoker.invoke_typed(Context(timeout=0.02), "reports.export", Empty(), Empty)

    typed = asyncio.run(typed_with_timeout())
    assert typed.error is not None
    assert typed.error.code == ERROR_CODE_DEADLINE_EXCEEDED
    # 信号触发之后处理器照常返回结果的，调用以那个结果结束
    finished = invoke_with_timeout(invoker, Request("reports.ignore"), 0.01)
    assert finished.outcome.is_ok
    assert finished.payload == b"{}"


def test_typed_invocations_that_never_reach_the_handler_fail() -> None:
    async def short_circuit(ctx: Context, next_step: Step) -> Result[Unit]:
        return success()

    invoker = new_invoker(Builder().use_at(pipeline.func("Cache", short_circuit), PipelineStage.IDEMPOTENCY))
    result = asyncio.run(invoker.invoke_typed(Context(), "orders.create", CreateOrder(quantity=1), OrderCreated))
    assert result.error is not None
    assert result.error.code == operation.ERROR_CODE_UNEXPECTED
    assert result.error.message_key == operation.MESSAGE_NO_RESPONSE
    # XGI 入口同样报 no_response，不返回成功的空载荷（RFC-0011 规则 9）
    response = invoke(invoker, Request("orders.create", b'{"quantity":1}'))
    assert error_of(response).code == operation.ERROR_CODE_UNEXPECTED
    assert error_of(response).message_key == operation.MESSAGE_NO_RESPONSE
    assert error_of(response).details == {"id": "orders.create"}
    assert response.payload is None


def test_sequential_invocations_on_one_context_do_not_leak() -> None:
    invoker = new_invoker()
    ctx = Context(baggage={BAGGAGE_CORRELATION: "c3"})

    async def both() -> tuple[Result[OrderCreated], Response]:
        typed = await invoker.invoke_typed(ctx, "orders.create", CreateOrder(quantity=1), OrderCreated)
        raw = await invoker.invoke(ctx, Request("orders.create", b'{"quantity":2}'))
        return typed, raw

    typed, raw = asyncio.run(both())
    assert typed.value == OrderCreated(id="o-c3", quantity=1)
    assert raw.payload == b'{"id":"o-c3","quantity":2}'
    request = operation.request_of(ctx)
    assert request is not None
    assert request.payload == b'{"quantity":2}'
    assert operation.response_payload_of(ctx) == raw.payload


@dataclass(frozen=True)
class Wrap:
    quantity: int = 0


@dataclass(frozen=True)
class Wrapped:
    inner: str


@dataclass(frozen=True)
class Named:
    name: str


def state_of(ctx: Context) -> tuple[str | None, str | None, object, object]:
    """中间件此刻从上下文读到的调用状态：操作 ID、请求的 ID、解码好的请求与响应对象。"""
    descriptor = operation.current(ctx)
    request = operation.request_of(ctx)
    return (
        None if descriptor is None else descriptor.id,
        None if request is None else request.id,
        operation.decoded_request_of(ctx),
        operation.response_of(ctx),
    )


def test_nested_invocations_keep_their_own_state() -> None:
    # 处理器在调用里再调一个操作：内层返回之后，外层的中间件读到的仍是外层的操作、请求与响应
    registry = Registry()
    invokers: list[Invoker] = []
    seen: list[tuple[tuple[str | None, str | None, object, object], ...]] = []

    async def wrap(ctx: Context, request: Wrap) -> Result[Wrapped]:
        payload = json.dumps({"quantity": request.quantity}).encode("utf-8")
        inner = await invokers[0].invoke(ctx, Request("orders.create", payload))
        assert inner.payload is not None, inner
        return ok(Wrapped(inner=inner.payload.decode("utf-8")))

    async def record(ctx: Context, next_step: Step) -> Result[Unit]:
        before = state_of(ctx)
        outcome = await next_step(ctx)
        seen.append((before, state_of(ctx)))
        return outcome

    registry.add(Contract("orders.create"), create_order, request_type=CreateOrder, response_type=OrderCreated)
    registry.add(Contract("orders.wrap"), wrap, request_type=Wrap, response_type=Wrapped)
    invokers.append(Invoker(registry, Builder().use_at(pipeline.func("Record", record), PipelineStage.TRANSACTION)))
    ctx = Context(baggage={BAGGAGE_CORRELATION: "c4"})
    response = invoke(invokers[0], Request("orders.wrap", b'{"quantity":2}'), ctx)
    created = OrderCreated(id="o-c4", quantity=2)
    wrapped = Wrapped(inner='{"id":"o-c4","quantity":2}')
    assert response.payload == b'{"inner":"{\\"id\\":\\"o-c4\\",\\"quantity\\":2}"}'
    assert seen == [
        (
            ("orders.create", "orders.create", CreateOrder(quantity=2), None),
            ("orders.create", "orders.create", CreateOrder(quantity=2), created),
        ),
        (
            ("orders.wrap", "orders.wrap", Wrap(quantity=2), None),
            ("orders.wrap", "orders.wrap", Wrap(quantity=2), wrapped),
        ),
    ]
    # 调用方的上下文上读到的是最后结束的那次调用：外层，不是内层
    assert state_of(ctx) == ("orders.wrap", "orders.wrap", Wrap(quantity=2), wrapped)
    assert operation.response_payload_of(ctx) == response.payload
    # 类型化的外层同样如此
    seen.clear()
    typed = asyncio.run(invokers[0].invoke_typed(ctx, "orders.wrap", Wrap(quantity=3), Wrapped))
    assert typed.value == Wrapped(inner='{"id":"o-c4","quantity":3}')
    assert [after[0] for _, after in seen] == ["orders.create", "orders.wrap"]
    assert seen[1][1][3] == typed.value


def test_a_middleware_that_invokes_another_operation_keeps_the_current_one() -> None:
    # 中间件在往下走之前先调了另一个操作（如按另一个操作的结果判定）：这次调用执行的仍是它自己的操作与请求
    registry = Registry()
    invokers: list[Invoker] = []
    checked: list[object] = []

    async def wrap(ctx: Context, request: Wrap) -> Result[Wrapped]:
        return ok(Wrapped(inner=f"wrapped {request.quantity}"))

    async def check(ctx: Context, next_step: Step) -> Result[Unit]:
        descriptor = operation.current(ctx)
        assert descriptor is not None
        if descriptor.id == "orders.wrap":
            typed = await invokers[0].invoke_typed(ctx, "orders.create", CreateOrder(quantity=1), OrderCreated)
            raw = await invokers[0].invoke(ctx, Request("orders.create", b'{"quantity":5}'))
            checked.extend([typed.value, raw.payload, state_of(ctx)])
        return await next_step(ctx)

    registry.add(Contract("orders.create"), create_order, request_type=CreateOrder, response_type=OrderCreated)
    registry.add(Contract("orders.wrap"), wrap, request_type=Wrap, response_type=Wrapped)
    invokers.append(Invoker(registry, Builder().use_at(pipeline.func("Check", check), PipelineStage.AUTHORIZE)))
    ctx = Context(baggage={BAGGAGE_CORRELATION: "c5"})
    response = invoke(invokers[0], Request("orders.wrap", b'{"quantity":2}'), ctx)
    assert response.payload == b'{"inner":"wrapped 2"}'
    typed = asyncio.run(invokers[0].invoke_typed(ctx, "orders.wrap", Wrap(quantity=4), Wrapped))
    assert typed.value == Wrapped(inner="wrapped 4")
    # Authorize 阶段在 Validate 之前：原始调用此时还没有解码好的请求，类型化调用的请求就是传入的对象
    inner = [OrderCreated(id="o-c5", quantity=1), b'{"id":"o-c5","quantity":5}']
    assert checked == [
        *inner,
        ("orders.wrap", "orders.wrap", None, None),
        *inner,
        ("orders.wrap", "orders.wrap", Wrap(quantity=4), None),
    ]


def test_concurrent_invocations_on_one_context_do_not_mix() -> None:
    # 同一个上下文上并发的两次调用各有各的状态：谁也不会执行到另一个操作，或拿到另一个操作的响应
    registry = Registry()
    seen: list[tuple[str | None, str | None, object, object]] = []

    async def first(ctx: Context, request: Wrap) -> Result[Named]:
        return ok(Named(f"first {request.quantity}"))

    async def second(ctx: Context, request: Wrap) -> Result[Named]:
        return ok(Named(f"second {request.quantity}"))

    registry.add(Contract("firsts.get"), first, request_type=Wrap, response_type=Named)
    registry.add(Contract("seconds.get"), second, request_type=Wrap, response_type=Named)

    async def main() -> tuple[tuple[Response, ...], tuple[Result[Named], ...]]:
        barrier = asyncio.Barrier(2)

        async def suspend(ctx: Context, next_step: Step) -> Result[Unit]:
            # 两次调用都走到这里才放行：此时两边都已进入管道、解码了各自的请求
            await barrier.wait()
            outcome = await next_step(ctx)
            seen.append(state_of(ctx))
            return outcome

        invoker = Invoker(registry, Builder().use_at(pipeline.func("Suspend", suspend), PipelineStage.TRANSACTION))
        ctx = Context()
        raw = await asyncio.gather(
            invoker.invoke(ctx, Request("firsts.get", b'{"quantity":1}')),
            invoker.invoke(ctx, Request("seconds.get", b'{"quantity":2}')),
        )
        typed = await asyncio.gather(
            invoker.invoke_typed(ctx, "firsts.get", Wrap(quantity=3), Named),
            invoker.invoke_typed(ctx, "seconds.get", Wrap(quantity=4), Named),
        )
        return raw, typed

    raw, typed = asyncio.run(main())
    assert [response.payload for response in raw] == [b'{"name":"first 1"}', b'{"name":"second 2"}']
    assert [result.value for result in typed] == [Named("first 3"), Named("second 4")]
    assert sorted(seen, key=str) == sorted(
        [
            ("firsts.get", "firsts.get", Wrap(quantity=1), Named("first 1")),
            ("seconds.get", "seconds.get", Wrap(quantity=2), Named("second 2")),
            ("firsts.get", "firsts.get", Wrap(quantity=3), Named("first 3")),
            ("seconds.get", "seconds.get", Wrap(quantity=4), Named("second 4")),
        ],
        key=str,
    )


def test_the_terminal_only_runs_the_invocation_its_context_carries() -> None:
    # 调用的状态随派生的上下文往下传：中间件派生出的上下文照常执行；换成不是由它派生的上下文（哪怕共享对象袋）时
    # 终点不去执行对象袋里最近的那次调用，以未登记结束
    callers: list[Context] = []

    async def derive(ctx: Context, next_step: Step) -> Result[Unit]:
        return await next_step(ctx.with_baggage(BAGGAGE_CORRELATION, "c7").with_cancellation(Cancellation()))

    async def hand_back_the_callers(ctx: Context, next_step: Step) -> Result[Unit]:
        return await next_step(callers[-1])

    async def start_over(ctx: Context, next_step: Step) -> Result[Unit]:
        return await next_step(Context())

    def run(step: Callable[[Context, Step], Any]) -> tuple[Response, Result[OrderCreated]]:
        invoker = new_invoker(Builder().use_at(pipeline.func("Replace", step), PipelineStage.AUTHORIZE))
        callers.append(Context())
        raw = invoke(invoker, Request("orders.create", b'{"quantity":1}'), callers[-1])
        typed = asyncio.run(invoker.invoke_typed(callers[-1], "orders.create", CreateOrder(quantity=2), OrderCreated))
        return raw, typed

    raw, typed = run(derive)
    assert raw.payload == b'{"id":"o-c7","quantity":1}'
    assert typed.value == OrderCreated(id="o-c7", quantity=2)
    for step in (hand_back_the_callers, start_over):
        raw, typed = run(step)
        for error in (raw.outcome.error, typed.error):
            assert error is not None
            assert (error.code, error.message_key) == (operation.ERROR_CODE_NOT_FOUND, operation.MESSAGE_NOT_FOUND)
        assert raw.payload is None


@dataclass(frozen=True)
class Refresh:
    key: str
    ttl_ms: int = field(metadata=json_field("ttlMs"))
    note: str | None = field(default=None, metadata=json_field(omit_none=True))


@dataclass(frozen=True)
class Measure:
    ratio: float


class Misconfigured:
    """自带 to_json_value、写出时抛出框架异常的响应。"""

    def to_json_value(self) -> JsonValue:
        raise FrameworkError("ORDERS_VALIDATION_009", "the response is misconfigured")


def test_renamed_members_and_unencodable_responses() -> None:
    registry = Registry()

    async def refresh(ctx: Context, request: Refresh) -> Result[Refresh]:
        return ok(request)

    async def special(ctx: Context, request: Empty) -> Result[list[float]]:
        return ok([float("nan"), 2.0])

    async def measure(ctx: Context, request: Empty) -> Result[Measure]:
        return ok(Measure(float("inf")))

    async def broken(ctx: Context, request: Empty) -> Result[object]:
        return ok(object())

    async def misconfigured(ctx: Context, request: Empty) -> Result[Misconfigured]:
        return ok(Misconfigured())

    registry.add(Contract("cache-entries.try-refresh"), refresh, request_type=Refresh, response_type=Refresh)
    registry.add(Contract("numbers.list"), special, request_type=Empty, response_type=list)
    registry.add(Contract("numbers.measure"), measure, request_type=Empty, response_type=Measure)
    registry.add(Contract("numbers.get"), broken, request_type=Empty, response_type=object)
    registry.add(Contract("numbers.configure"), misconfigured, request_type=Empty, response_type=Misconfigured)
    invoker = Invoker(registry)
    response = invoke(invoker, Request("cache-entries.try-refresh", b'{"key":"k","ttlMs":500,"unknown":true}'))
    assert response.payload == b'{"key":"k","ttlMs":500}'
    # 缺少必填成员报所在对象的路径（RFC-0011 规则 2，ADR-0236）
    missing = invoke(invoker, Request("cache-entries.try-refresh", b'{"key":"k","ttl_ms":500}'))
    assert error_of(missing).details == {"id": "cache-entries.try-refresh", "reason": "$"}
    # 响应写不出（类型化的浮点是 NaN 或无穷、编码不了的值）时以 unhandled_exception 结束，不抛给调用方，没有载荷；
    # NaN 不写成 null 或字符串（RFC-0011 规则 10 的「编码」）
    for id in ["numbers.list", "numbers.measure", "numbers.get"]:
        unencodable = invoke(invoker, Request(id))
        assert unencodable.payload is None, id
        error = error_of(unencodable)
        assert (error.code, error.message_key, error.inner) == (
            operation.ERROR_CODE_UNEXPECTED,
            operation.MESSAGE_UNHANDLED_EXCEPTION,
            None,
        ), id
        assert error.message == "An unhandled exception occurred during the invocation."
    # 框架异常的错误放进 inner
    wrapped = error_of(invoke(invoker, Request("numbers.configure")))
    assert wrapped.message_key == operation.MESSAGE_UNHANDLED_EXCEPTION
    assert wrapped.inner == Error.literal("ORDERS_VALIDATION_009", "the response is misconfigured")
    # 不经过管道的 execute 同样如此
    descriptor = registry.find("numbers.measure")
    assert descriptor is not None
    direct = asyncio.run(descriptor.execute(Context(), b""))
    assert direct.payload is None
    assert error_of(direct).message_key == operation.MESSAGE_UNHANDLED_EXCEPTION


@dataclass(frozen=True)
class Labelled:
    label: str
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Renamed:
    count: int = field(metadata=json_field("count" + chr(0xD800)))


def returning(value: object) -> operation.Handler[Empty, object]:
    async def handle(ctx: Context, request: Empty) -> Result[object]:
        return ok(value)

    return handle


def test_responses_with_surrogate_code_points_cannot_be_written() -> None:
    # 响应里的字符串写不成合法的 JSON 文本即写不出（RFC-0011 规则 10 的「编码」，ADR-0318）：值、成员名、映射的键与数组元素
    # 里的代理项码点都以 unhandled_exception 结束，不带 inner，没有载荷，不写成 \ud800 这样的转义
    unwritable: dict[str, object] = {
        "labels.value": Labelled("a" + chr(0xD800)),
        "labels.element": Labelled("a", ["b", chr(0xDFFF)]),
        "labels.member": Renamed(1),
        "labels.key": {chr(0xD800): 1},
        "labels.nested-key": {"a": [{"b" + chr(0xDC00): "c"}]},
        # Python 的 str 里两个代理项码点不是一个字符（RFC-0001「JSON 文本」，ADR-0301）
        "labels.pair": Labelled(chr(0xD83D) + chr(0xDE00)),
    }
    text = "中文" + chr(0x1F600) + chr(0xFFFD)
    registry = Registry()
    for id, value in unwritable.items():
        registry.add(Contract(id), returning(value), request_type=Empty, response_type=object)
    registry.add(
        Contract("labels.text"), returning(Labelled(text, [chr(0xE9)])), request_type=Empty, response_type=object
    )
    invoker = Invoker(registry)
    for id in unwritable:
        descriptor = registry.find(id)
        assert descriptor is not None
        for response in (invoke(invoker, Request(id)), asyncio.run(descriptor.execute(Context(), b""))):
            assert response.payload is None, id
            error = error_of(response)
            assert (error.code, error.message_key, error.details, error.inner) == (
                operation.ERROR_CODE_UNEXPECTED,
                operation.MESSAGE_UNHANDLED_EXCEPTION,
                None,
                None,
            ), id
    # 多字节文本与真正的 U+FFFD 照常写出
    written = invoke(invoker, Request("labels.text"))
    assert written.outcome.is_ok
    assert written.payload == ('{"label":"' + text + '","tags":["' + chr(0xE9) + '"]}').encode("utf-8")


class Repeated(Mapping[str, JsonValue]):
    # items() 按给定的顺序交出成员，名字可以重复：dict 存不下同名的成员，自定义的 to_json_value 可以这样交出
    def __init__(self, members: list[tuple[str, JsonValue]]) -> None:
        self._members = members

    def __getitem__(self, key: str) -> JsonValue:
        return dict(self._members)[key]

    def __iter__(self) -> Iterator[str]:
        return iter(name for name, _ in self._members)

    def __len__(self) -> int:
        return len(self._members)

    def items(self) -> list[tuple[str, JsonValue]]:  # type: ignore[override]
        return list(self._members)


@dataclass(frozen=True)
class Twice:
    members: list[tuple[str, JsonValue]]

    def to_json_value(self) -> JsonValue:
        return Repeated(self.members)


@dataclass(frozen=True)
class Boxed:
    value: object


class Shown(int):
    def __str__(self) -> str:
        return f"ORD-{int(self)}"


def nested_arrays(depth: int, innermost: object = None) -> object:
    # depth 层嵌套的数组，最外层是第 1 层；innermost 为空时最里面是空数组
    value: object = [] if innermost is None else [innermost]
    for _ in range(depth - 1):
        value = [value]
    return value


def nested_objects(depth: int) -> object:
    value: object = {}
    for _ in range(depth - 1):
        value = {"a": value}
    return value


def test_responses_that_do_not_pass_the_json_text_checks_cannot_be_written() -> None:
    # 写出的整段文本要过 RFC-0001「JSON 文本」的共同检查（RFC-0011 规则 10 的「编码」，ADR-0319）：嵌套超过 64 层
    # （从响应的根算起）或任何一层有重复的成员名同样写不出，以 unhandled_exception 结束，不带 inner，没有载荷
    unwritable: dict[str, object] = {
        "depths.arrays": nested_arrays(65),
        "depths.objects": nested_objects(65),
        "depths.scalar": nested_arrays(65, 1),
        # 响应对象本身是第 1 层：里面 64 层的数组在根之下是 65 层
        "depths.member": Boxed(nested_arrays(64)),
        "members.repeated": Twice([("a", 1), ("a", 2)]),
        "members.apart": Twice([("a", 1), ("b", 2), ("a", 3)]),
        "members.nested": {"outer": [Boxed(Twice([("b", True), ("b", False)]))]},
        # Python 的 str 里两个代理项码点不是一个字符：映射的键也一样（RFC-0001「写出与转交」，ADR-0319）
        "members.pair": {chr(0xD83D) + chr(0xDE00): 1},
        # 共同检查也查语法：int 的子类按自己的 __str__ 写出，写成数字以外的文本同样写不出
        "numbers.shown": [Shown(1)],
    }
    writable: dict[str, tuple[object, bytes]] = {
        "depths.arrays-at-limit": (nested_arrays(64), b"[" * 64 + b"]" * 64),
        "depths.scalar-at-limit": (nested_arrays(64, 1), b"[" * 64 + b"1" + b"]" * 64),
        "depths.objects-at-limit": (nested_objects(64), b'{"a":' * 63 + b"{}" + b"}" * 63),
        "depths.member-at-limit": (Boxed(nested_arrays(63)), b'{"value":' + b"[" * 63 + b"]" * 63 + b"}"),
        # 不同层、不同对象里的同名成员不算重复
        "members.distinct": (
            Twice([("a", {"a": 1}), ("b", [{"a": 2}, {"a": 3}])]),
            b'{"a":{"a":1},"b":[{"a":2},{"a":3}]}',
        ),
    }
    registry = Registry()
    for id, value in unwritable.items():
        registry.add(Contract(id), returning(value), request_type=Empty, response_type=object)
    for id, (value, _) in writable.items():
        registry.add(Contract(id), returning(value), request_type=Empty, response_type=object)
    invoker = Invoker(registry)
    for id in unwritable:
        descriptor = registry.find(id)
        assert descriptor is not None
        for response in (invoke(invoker, Request(id)), asyncio.run(descriptor.execute(Context(), b""))):
            assert response.payload is None, id
            error = error_of(response)
            assert (error.code, error.message_key, error.details, error.inner) == (
                operation.ERROR_CODE_UNEXPECTED,
                operation.MESSAGE_UNHANDLED_EXCEPTION,
                None,
                None,
            ), id
    # 恰好 64 层照常写出：最里面有值也一样，差一层都不行
    for id, (_, payload) in writable.items():
        written = invoke(invoker, Request(id))
        assert written.outcome.is_ok, id
        assert written.payload == payload, id


def test_contracts_schemas_and_types_are_checked_when_registering() -> None:
    with pytest.raises(FrameworkError):
        Schema(request="[]", response="{}")
    with pytest.raises(FrameworkError):
        Schema(request="{}", response="{")
    assert Contract("orders.list", expose=[operation.EXPOSE_MCP]).expose == ("mcp",)  # type: ignore[arg-type]

    async def handle(ctx: Context, request: set[int]) -> Result[Empty]:
        return ok(Empty())

    with pytest.raises(TypeError):
        Registry().add(Contract("sets.create"), handle, request_type=set, response_type=Empty)


def test_contracts_expose_only_the_five_protocols() -> None:
    # RFC-0011：Expose.All 冻结为五个协议；值是插件协议 OperationDeclaration.expose 里的小写名字
    assert [(protocol.name, protocol.value) for protocol in Expose] == [
        ("HTTP", "http"),
        ("GRPC", "grpc"),
        ("MCP", "mcp"),
        ("COMMAND_LINE", "commandline"),
        ("AI", "ai"),
    ]
    # 早先的常量是枚举成员的别名，照旧等于那些字符串
    aliases = (
        operation.EXPOSE_HTTP,
        operation.EXPOSE_GRPC,
        operation.EXPOSE_MCP,
        operation.EXPOSE_COMMAND_LINE,
        operation.EXPOSE_AI,
    )
    assert aliases == tuple(Expose) == ("http", "grpc", "mcp", "commandline", "ai")
    assert all(alias is member for alias, member in zip(aliases, Expose, strict=True))
    assert (str(Expose.AI), f"{Expose.COMMAND_LINE}", json.dumps([Expose.HTTP])) == ("ai", "commandline", '["http"]')
    # 名字与成员都收，契约里一律是成员
    contract = Contract("orders.list", expose=(Expose.HTTP, "ai", operation.EXPOSE_MCP))
    assert contract.expose == (Expose.HTTP, Expose.AI, Expose.MCP) == ("http", "ai", "mcp")
    assert all(type(protocol) is Expose for protocol in contract.expose)
    assert Contract("orders.list").expose == ()
    assert Contract("orders.list", expose=frozenset({"grpc"})).expose == (Expose.GRPC,)  # type: ignore[arg-type]
    # 五个协议之外的名字（含大小写不同的写法）在构造时拒绝
    for unknown in ["HTTP", "rest", "", "http ", "command-line", 1, None]:
        with pytest.raises(FrameworkError, match=r"'orders\.list'") as raised:
            Contract("orders.list", expose=(unknown,))  # type: ignore[arg-type]
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, unknown
    # 单个字符串不是一组协议：不逐个字符去认
    for not_a_tuple in ["http", Expose.HTTP, 5]:
        with pytest.raises(FrameworkError, match="tuple of protocols") as raised:
            Contract("orders.list", expose=not_a_tuple)  # type: ignore[arg-type]
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, not_a_tuple


def test_unhandled_exceptions_become_unexpected_errors(caplog: pytest.LogCaptureFixture) -> None:
    # RFC-0011 规则 5：操作或中间件抛出的异常翻译为 KERNEL_UNEXPECTED_001，上线的错误不带异常文本
    registry = Registry()

    async def explode(ctx: Context, request: Empty) -> Result[Empty]:
        raise LookupError("secret detail")

    async def misconfigured(ctx: Context, request: Empty) -> Result[Empty]:
        raise FrameworkError("KERNEL_PIPELINE_001", "the ordering constraints form a cycle")

    registry.add(Contract("bombs.explode"), explode, request_type=Empty, response_type=Empty)
    registry.add(Contract("bombs.misconfigure"), misconfigured, request_type=Empty, response_type=Empty)
    invoker = Invoker(registry)
    error = error_of(invoke(invoker, Request("bombs.explode")))
    assert error.code == operation.ERROR_CODE_UNEXPECTED
    assert error.message_key == operation.MESSAGE_UNHANDLED_EXCEPTION
    assert error.message == "An unhandled exception occurred during the invocation."
    assert error.inner is None
    assert "secret detail" not in error.to_json()
    assert any("bombs.explode" in record.getMessage() and record.exc_info for record in caplog.records)
    wrapped = error_of(invoke(invoker, Request("bombs.misconfigure")))
    assert wrapped.inner == Error.literal("KERNEL_PIPELINE_001", "the ordering constraints form a cycle")
    typed = asyncio.run(invoker.invoke_typed(Context(), "bombs.explode", Empty(), Empty))
    assert typed.error is not None
    assert typed.error.code == operation.ERROR_CODE_UNEXPECTED

    async def explode_in_middleware(ctx: Context, next_step: Step) -> Result[Unit]:
        raise RuntimeError("boom")

    guarded = Invoker(
        registry, Builder().use_at(pipeline.func("Explode", explode_in_middleware), PipelineStage.OBSERVE)
    )
    assert error_of(invoke(guarded, Request("bombs.explode"))).message_key == operation.MESSAGE_UNHANDLED_EXCEPTION


@dataclass(frozen=True)
class ReasonLine:
    quantity: Annotated[int, Int32] = 0


@dataclass(frozen=True)
class ReasonOrder:
    lines: list[ReasonLine] | None = None


@dataclass(frozen=True)
class ReasonInner:
    n: int


@dataclass(frozen=True)
class ReasonProfile:
    """向量的 profile：没有缺省值的字段是必填的，要接受 null 的字符串、数组与对象写成 X | None。"""

    name: str | None
    note: str | None = None
    count: int = 0
    ratio: float = 0.0
    flag: bool = False
    tags: list[str | None] | None = None
    scores: list[int] | None = None
    inner: ReasonInner | None = None
    meta: JsonValue = None


def test_payload_reasons_follow_the_vectors(vector: Callable[[str, str], Any]) -> None:
    # 向量的三个请求类型：counts 是 32 位整数的字典，order 是 { lines: [ { quantity: 32 位整数 } ] }，profile 见上；
    # reason 为 null 即解码成功。处理器不回写请求：profile 的 ratio 可以是无穷，类型化的浮点无穷写不出
    vectors = vector("operations", "payload-reasons")
    registry = Registry()
    decoded: list[object] = []

    async def accept(ctx: Context, request: object) -> Result[Empty]:
        decoded.append(request)
        return ok(Empty())

    types: dict[str, Any] = {
        "counts": dict[str, Annotated[int, Int32]],
        "order": ReasonOrder,
        "profile": ReasonProfile,
    }
    for name, request_type in types.items():
        registry.add(Contract(f"{name}.check"), accept, request_type=request_type, response_type=Empty)
    invoker = Invoker(registry)
    for case in vectors["cases"]:
        id = f"{case['request']}.check"
        response = invoke(invoker, Request(id, case["payload"].encode("utf-8")))
        if case["reason"] is None:
            assert response.outcome.is_ok, case["name"]
            continue
        error = error_of(response)
        assert error.code == operation.ERROR_CODE_INVALID_PAYLOAD, case["name"]
        assert error.message_key == operation.MESSAGE_INVALID_PAYLOAD
        assert error.details == {"id": id, "reason": case["reason"]}, case["name"]
    decoded.clear()
    invoke(invoker, Request("profile.check", b'{"name":null,"ratio":5,"tags":["x",null]}'))
    invoke(invoker, Request("counts.check", b'{"a":-2147483648}'))
    assert decoded == [ReasonProfile(name=None, ratio=5.0, tags=["x", None]), {"a": -(2**31)}]
    profile = decoded[0]
    assert isinstance(profile, ReasonProfile)
    assert type(profile.ratio) is float, "typed floats are plain floats"


@dataclass(frozen=True)
class Holder:
    meta: JsonValue = None


def _exact(payload: bytes) -> object:
    """按 JSON 值读，数字保留精确值并区分整数写法与带小数点或指数的写法。"""
    return json.loads(
        payload, parse_int=lambda text: ("integer", int(text)), parse_float=lambda text: ("number", Decimal(text))
    )


def test_arbitrary_json_members_keep_the_numbers_they_read() -> None:
    # RFC-0011 规则 2（ADR-0236）：任意 JSON 类型的成员原样保留读到的值，写回时 JSON 值相等，1e400 不变成 null 或无穷，
    # 超出双精度的整数保持精确，2.0 与 2 的区别保留
    seen: list[JsonValue] = []
    registry = Registry()

    async def echo(ctx: Context, request: Holder) -> Result[Holder]:
        seen.append(request.meta)
        return ok(request)

    registry.add(Contract("holders.echo"), echo, request_type=Holder, response_type=Holder)
    payload = (
        b'{"meta":{"big":1e400,"exact":9007199254740993,"huge":123456789012345678901234567890,'
        b'"float":2.0,"int":2,"tiny":1e-400,"digits":0.123456789012345678901234567890,"list":[1.50,-0.0,1E+2]}}'
    )
    response = invoke(Invoker(registry), Request("holders.echo", payload))
    assert response.payload is not None
    assert _exact(response.payload) == _exact(payload)
    meta = seen[0]
    assert isinstance(meta, dict)
    big = meta["big"]
    assert isinstance(big, JsonNumber)
    assert (big.text, math.isinf(big)) == ("1e400", True)
    assert meta["exact"] == 9007199254740993
    assert type(meta["int"]) is int
    assert isinstance(meta["float"], JsonNumber)
    assert meta["float"] == 2.0


def test_responses_are_encoded_after_the_pipeline() -> None:
    # RFC-0011 规则 10 的「编码」：XGI 入口在管道结束之后编码，中间件看到的是编码之前的响应对象
    seen: list[tuple[object, bytes | None]] = []

    async def egress(ctx: Context, next_step: Step) -> Result[Unit]:
        outcome = await next_step(ctx)
        seen.append((operation.response_of(ctx), operation.response_payload_of(ctx)))
        return outcome

    invoker = new_invoker(Builder().use_at(pipeline.func("Egress", egress), PipelineStage.EGRESS))
    ctx = Context(baggage={BAGGAGE_CORRELATION: "c9"})
    response = invoke(invoker, Request("orders.create", b'{"quantity":2}'), ctx)
    assert seen == [(OrderCreated(id="o-c9", quantity=2), None)]
    assert response.payload == b'{"id":"o-c9","quantity":2}'
    assert operation.response_payload_of(ctx) == response.payload
    assert operation.response_of(ctx) == OrderCreated(id="o-c9", quantity=2)


def test_built_in_steps_sit_where_the_dotnet_middleware_does() -> None:
    # RFC-0011 规则 10（ADR-0236）：三个内置步骤按归属 xihan.kernel.core 放在所在阶段的 Beginning，与同位置的其他中间件按
    # 归属的序数序排序；End 位置的中间件都在它们之后
    async def through(ctx: Context, next_step: Step) -> Result[Unit]:
        return await next_step(ctx)

    builder = (
        Builder()
        .use_at(pipeline.func("Late", through), PipelineStage.OBSERVE, pipeline.Placement.BEGINNING, "zeta.late.owner")
        .use_at(pipeline.func("Early", through), PipelineStage.OBSERVE, pipeline.Placement.BEGINNING)
        .use_at(pipeline.func("Limit", through), PipelineStage.RESILIENCE)
        .use_at(pipeline.func("Check", through), PipelineStage.VALIDATE)
        .use_at(pipeline.func("Gate", through), PipelineStage.INGRESS)
    )
    invoker = new_invoker(builder)
    assert invoker.dump() == (
        "ingress      Gate\n"
        "observe      Early\n"
        "observe      ObserveMiddleware\n"
        "observe      Late\n"
        "resilience   DeadlineMiddleware\n"
        "resilience   Limit\n"
        "validate     ValidationMiddleware\n"
        "validate     Check\n"
    )
    built_ins = [item for item in invoker.describe() if item.owner == operation.KERNEL_OWNER]
    assert [(item.stage, item.middleware) for item in built_ins] == [
        (PipelineStage.OBSERVE, operation.OBSERVE_MIDDLEWARE),
        (PipelineStage.RESILIENCE, operation.DEADLINE_MIDDLEWARE),
        (PipelineStage.VALIDATE, operation.VALIDATION_MIDDLEWARE),
    ]
    assert all(item.placement is pipeline.Placement.BEGINNING for item in built_ins)
    # 应用的构建器不变；与内置步骤重名是登记错误
    assert "ObserveMiddleware" not in builder.dump()
    with pytest.raises(FrameworkError) as raised:
        new_invoker(Builder().use_at(pipeline.func(operation.VALIDATION_MIDDLEWARE, through), PipelineStage.VALIDATE))
    assert raised.value.code == pipeline.ERROR_CODE_REGISTRATION


def test_only_exceptions_after_the_observe_step_are_translated() -> None:
    async def explode(ctx: Context, next_step: Step) -> Result[Unit]:
        raise LookupError("boom")

    translated = [
        Builder().use_at(pipeline.func("Explode", explode), PipelineStage.OBSERVE),
        Builder().use_at(pipeline.func("Explode", explode), PipelineStage.OBSERVE, pipeline.Placement.BEGINNING, "zz"),
        Builder().use_at(pipeline.func("Explode", explode), PipelineStage.HANDLE),
    ]
    for builder in translated:
        error = error_of(invoke(new_invoker(builder), Request("orders.create", b'{"quantity":1}')))
        assert error.message_key == operation.MESSAGE_UNHANDLED_EXCEPTION
    # 排在 ObserveMiddleware 之前的（Ingress 阶段，或 Observe 阶段 Beginning 上归属排在 xihan.kernel.core 之前的）原样交给调用方
    passed_through = [
        Builder().use_at(pipeline.func("Explode", explode), PipelineStage.INGRESS),
        Builder().use_at(pipeline.func("Explode", explode), PipelineStage.OBSERVE, pipeline.Placement.BEGINNING),
    ]
    for builder in passed_through:
        with pytest.raises(LookupError):
            invoke(new_invoker(builder), Request("orders.create", b'{"quantity":1}'))


def test_the_deadline_is_checked_when_entering_resilience() -> None:
    # RFC-0011 规则 10（ADR-0236，撤销 ADR-0235 的“第一个中间件之前”）：Ingress 与 Observe 阶段照常执行，
    # Resilience 阶段的 DeadlineMiddleware 进入时已到即结束，后面的中间件与处理器不执行；构造上下文时不判断
    seen: list[str] = []

    def record(name: str) -> Callable[[Context, Step], Any]:
        async def step(ctx: Context, next_step: Step) -> Result[Unit]:
            seen.append(name)
            return await next_step(ctx)

        return step

    builder = Builder()
    for stage in [PipelineStage.INGRESS, PipelineStage.OBSERVE, PipelineStage.RESILIENCE, PipelineStage.VALIDATE]:
        builder.use_at(pipeline.func(stage.manifest_name, record(stage.manifest_name)), stage)
    invoker = new_invoker(builder)
    expired = Context(timeout=-1)
    assert expired.cancellation_error() is not None, "constructing an expired context does not fail"
    raw = error_of(invoke(invoker, Request("orders.create", b'{"quantity":1}'), expired))
    typed = asyncio.run(
        invoker.invoke_typed(Context(timeout=-1), "orders.create", CreateOrder(quantity=1), OrderCreated)
    )
    for error in [raw, typed.error]:
        assert error is not None
        assert error.code == ERROR_CODE_DEADLINE_EXCEEDED
        assert error.message_key == "kernel.invocation.deadline_before_start"
        assert error.message == "The invocation exceeded its deadline before it started."
    assert seen == ["ingress", "observe"] * 2
    # 进入管道之前只核对未登记与类型不符：已过期的上下文上的这两种失败照常先报
    missing = error_of(invoke(invoker, Request("orders.missing"), Context(timeout=-1)))
    assert missing.code == operation.ERROR_CODE_NOT_FOUND
    mismatch = asyncio.run(invoker.invoke_typed(Context(timeout=-1), "orders.create", Empty(), OrderCreated))
    assert mismatch.error is not None
    assert mismatch.error.code == operation.ERROR_CODE_TYPE_MISMATCH
    # 进入时不核对取消：信号已触发而处理器不看信号时照常完成（RFC-0003 规则 1，RFC-0011 规则 10）
    signal = Cancellation()
    signal.cancel()
    assert invoke(invoker, Request("orders.create", b'{"quantity":1}'), Context(cancellation=signal)).outcome.is_ok


def test_payloads_are_decoded_in_the_validate_stage() -> None:
    # 原始调用在 Validate 阶段的 ValidationMiddleware 解码（RFC-0011 规则 2、3）：之前的中间件看不到请求对象，
    # 解码失败时之后的中间件不执行；类型化调用跳过解码，请求对象就是传入的那个
    seen: list[tuple[str, object]] = []

    def record(name: str) -> Callable[[Context, Step], Any]:
        async def step(ctx: Context, next_step: Step) -> Result[Unit]:
            seen.append((name, operation.decoded_request_of(ctx)))
            return await next_step(ctx)

        return step

    builder = (
        Builder()
        .use_at(pipeline.func("Authorize", record("authorize")), PipelineStage.AUTHORIZE)
        .use_at(pipeline.func("Check", record("validate")), PipelineStage.VALIDATE)
    )
    invoker = new_invoker(builder)
    assert invoke(invoker, Request("orders.create", b'{"quantity":2}')).outcome.is_ok
    assert seen == [("authorize", None), ("validate", CreateOrder(quantity=2))]
    seen.clear()
    assert error_of(invoke(invoker, Request("orders.create", b'{"quantity":"x"}'))).details == {
        "id": "orders.create",
        "reason": "$.quantity",
    }
    assert seen == [("authorize", None)]
    seen.clear()
    request = CreateOrder(quantity=3)
    assert asyncio.run(invoker.invoke_typed(Context(), "orders.create", request, OrderCreated)).is_ok
    assert seen == [("authorize", request), ("validate", request)]
