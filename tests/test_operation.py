"""operation 的测试：Go SDK 移植来的调用、类型化调用与登记表用例，以及容量、载荷错误、超时与取消。"""

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from xihan_framework import operation, pipeline
from xihan_framework._json import JsonValue
from xihan_framework.invocation import ERROR_CODE_CANCELLED, ERROR_CODE_DEADLINE_EXCEEDED, Cancellation, Context
from xihan_framework.operation import Contract, Invoker, Registry, Request, Response, Schema, json_field
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
    # 重复先于容量判定，与 Go 一致
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
    registry = Registry()

    async def slow(ctx: Context, request: Empty) -> Result[Empty]:
        await asyncio.sleep(5)
        return ok(Empty())

    registry.add(Contract("reports.export"), slow, request_type=Empty, response_type=Empty)
    invoker = Invoker(registry)
    timed_out = invoke(invoker, Request("reports.export"), Context(timeout=0.02))
    assert error_of(timed_out).code == ERROR_CODE_DEADLINE_EXCEEDED
    signal = Cancellation()

    async def cancel_soon() -> Response:
        asyncio.get_running_loop().call_later(0.02, signal.cancel)
        return await invoker.invoke(Context(cancellation=signal), Request("reports.export"))

    assert error_of(asyncio.run(cancel_soon())).code == ERROR_CODE_CANCELLED
    typed = asyncio.run(invoker.invoke_typed(Context(timeout=0.02), "reports.export", Empty(), Empty))
    assert typed.error is not None
    assert typed.error.code == ERROR_CODE_DEADLINE_EXCEEDED


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
class Refresh:
    key: str
    ttl_ms: int = field(metadata=json_field("ttlMs"))
    note: str | None = field(default=None, metadata=json_field(omit_none=True))


def test_renamed_members_and_unencodable_responses() -> None:
    registry = Registry()

    async def refresh(ctx: Context, request: Refresh) -> Result[Refresh]:
        return ok(request)

    async def special(ctx: Context, request: Empty) -> Result[list[float]]:
        return ok([float("nan"), 2.0])

    async def broken(ctx: Context, request: Empty) -> Result[object]:
        return ok(object())

    registry.add(Contract("cache-entries.try-refresh"), refresh, request_type=Refresh, response_type=Refresh)
    registry.add(Contract("numbers.list"), special, request_type=Empty, response_type=list)
    registry.add(Contract("numbers.get"), broken, request_type=Empty, response_type=object)
    invoker = Invoker(registry)
    response = invoke(invoker, Request("cache-entries.try-refresh", b'{"key":"k","ttlMs":500,"unknown":true}'))
    assert response.payload == b'{"key":"k","ttlMs":500}'
    # 缺少必填成员报所在对象的路径（RFC-0011 规则 2，ADR-0236）
    missing = invoke(invoker, Request("cache-entries.try-refresh", b'{"key":"k","ttl_ms":500}'))
    assert error_of(missing).details == {"id": "cache-entries.try-refresh", "reason": "$"}
    # 响应按值模型写：非有限的浮点写成字符串，整数值的浮点带 .0
    assert invoke(invoker, Request("numbers.list")).payload == b'["NaN",2.0]'
    unencodable = error_of(invoke(invoker, Request("numbers.get")))
    assert unencodable.code == operation.ERROR_CODE_UNEXPECTED
    assert unencodable.message.startswith("The response of 'numbers.get' could not be encoded:")


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
    quantity: int = 0


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
    # 向量的三个请求类型：counts 是整数字典，order 是 { lines: [ { quantity: 整数 } ] }，profile 见上；reason 为 null 即解码成功
    vectors = vector("operations", "payload-reasons")
    registry = Registry()

    async def echo(ctx: Context, request: object) -> Result[object]:
        return ok(request)

    types: dict[str, Any] = {"counts": dict[str, int], "order": ReasonOrder, "profile": ReasonProfile}
    for name, request_type in types.items():
        registry.add(Contract(f"{name}.check"), echo, request_type=request_type, response_type=request_type)
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
    assert (
        invoke(invoker, Request("order.check", b'{"lines":[{"quantity":2}]}')).payload == b'{"lines":[{"quantity":2}]}'
    )
    profile = invoke(invoker, Request("profile.check", b'{"name":null,"ratio":5,"tags":["x",null]}')).payload
    assert profile == (
        b'{"name":null,"note":null,"count":0,"ratio":5.0,"flag":false,"tags":["x",null],"scores":null,'
        b'"inner":null,"meta":null}'
    )


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
    # 进入时不核对取消：信号已触发而处理器不让出时照常完成，与 .NET 处理器不看令牌时一样
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
