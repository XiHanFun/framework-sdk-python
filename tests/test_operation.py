"""operation 的测试：Go SDK 移植来的调用、类型化调用与登记表用例，以及容量、载荷错误、超时与取消。"""

import asyncio
import threading
from dataclasses import dataclass, field

import pytest

from xihan_framework import operation, pipeline
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
    assert error_of(invoke(invoker, Request("orders.create", b"  \n"))).code == "ORDERS_VALIDATION_001"


def test_invalid_payloads_report_only_the_json_path() -> None:
    invoker = new_invoker()
    cases = [(b"null", "the payload is null"), (b"{", "$"), (b"[1]", "$"), (b'{"quantity":"2"}', "$.quantity")]
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
    assert mismatch.error.details == {
        "id": "orders.create",
        "expected": "CreateOrder -> OrderCreated",
        "actual": "CreateOrder -> str",
    }
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

    async def broken(ctx: Context, request: Empty) -> Result[float]:
        return ok(float("nan"))

    registry.add(Contract("cache-entries.try-refresh"), refresh, request_type=Refresh, response_type=Refresh)
    registry.add(Contract("numbers.get"), broken, request_type=Empty, response_type=float)
    invoker = Invoker(registry)
    response = invoke(invoker, Request("cache-entries.try-refresh", b'{"key":"k","ttlMs":500,"unknown":true}'))
    assert response.payload == b'{"key":"k","ttlMs":500}'
    missing = invoke(invoker, Request("cache-entries.try-refresh", b'{"key":"k","ttl_ms":500}'))
    assert error_of(missing).details == {"id": "cache-entries.try-refresh", "reason": "$.ttlMs"}
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
