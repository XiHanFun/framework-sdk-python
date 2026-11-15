"""SDK 与宿主共用的内核错误（RFC-0011「错误码」）：回放 kernel-messages 向量，核对码、键、参数与默认英文模板。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from xihan_framework import invocation, operation, pipeline, primitives
from xihan_framework.invocation import Context
from xihan_framework.operation import Contract, Invoker, Registry, Request
from xihan_framework.pipeline import Builder, Step
from xihan_framework.primitives import BUILT_IN_CATALOG, Error, FrameworkError, PipelineStage, Result, Unit, ok, success

type VectorLoader = Callable[[str, str], Any]

# SDK 里定义的（错误码，消息键）对；必须与向量列出的完全相同
SDK_PAIRS = {
    (primitives.ERROR_CODE_UNINITIALIZED_RESULT, primitives.MESSAGE_UNINITIALIZED_RESULT),
    (invocation.ERROR_CODE_DEADLINE_EXCEEDED, invocation.MESSAGE_DEADLINE_EXCEEDED),
    (invocation.ERROR_CODE_DEADLINE_EXCEEDED, invocation.MESSAGE_DEADLINE_BEFORE_START),
    (invocation.ERROR_CODE_CANCELLED, invocation.MESSAGE_CANCELLED),
    (operation.ERROR_CODE_NOT_FOUND, operation.MESSAGE_NOT_FOUND),
    (operation.ERROR_CODE_INVALID_PAYLOAD, operation.MESSAGE_INVALID_PAYLOAD),
    (operation.ERROR_CODE_TYPE_MISMATCH, operation.MESSAGE_TYPE_MISMATCH),
    (operation.ERROR_CODE_UNEXPECTED, operation.MESSAGE_NO_RESPONSE),
    (operation.ERROR_CODE_DUPLICATE, operation.MESSAGE_DUPLICATE),
    (operation.ERROR_CODE_REGISTRY_FULL, operation.MESSAGE_REGISTRY_FULL),
}


@dataclass(frozen=True)
class Ping:
    count: int = 0


async def ping(ctx: Context, request: Ping) -> Result[Ping]:
    return ok(request)


async def short_circuit(ctx: Context, next_step: Step) -> Result[Unit]:
    return success()


def produced_errors() -> list[Error]:
    """逐个触发 SDK 实际产生的内核错误。"""
    registry = Registry()
    registry.add(Contract("pings.send"), ping, request_type=Ping, response_type=Ping)
    invoker = Invoker(registry)
    cached = Invoker(registry, Builder().use_at(pipeline.func("Cache", short_circuit), PipelineStage.IDEMPOTENCY))

    async def collect() -> list[Error | None]:
        expired = await Context(timeout=-1).run(ping(Context(), Ping()))
        return [
            expired.error,
            (await invoker.invoke(Context(), Request("pings.missing"))).outcome.error,
            (await invoker.invoke(Context(), Request("pings.send", b'{"count":"x"}'))).outcome.error,
            (await invoker.invoke_typed(Context(), "pings.send", Ping(), str)).error,
            (await cached.invoke_typed(Context(), "pings.send", Ping(), Ping)).error,
        ]

    errors = [error for error in asyncio.run(collect()) if error is not None]
    with pytest.raises(FrameworkError) as duplicate:
        registry.add(Contract("pings.send"), ping, request_type=Ping, response_type=Ping)
    full = Registry()
    for index in range(operation.CAPACITY):
        full.add(Contract(f"pings{index}.send"), ping, request_type=Ping, response_type=Ping)
    with pytest.raises(FrameworkError) as overflow:
        full.add(Contract("overflow.send"), ping, request_type=Ping, response_type=Ping)
    return [
        invocation.deadline_exceeded_error(),
        invocation.cancelled_error(),
        *errors,
        duplicate.value.to_error(),
        overflow.value.to_error(),
    ]


def test_built_in_templates_match_the_vector(vector: VectorLoader) -> None:
    messages = vector("primitives", "kernel-messages")["messages"]
    templates = BUILT_IN_CATALOG.locales[primitives.DEFAULT_LOCALE]
    assert set(templates) == {message["key"] for message in messages}
    for message in messages:
        assert templates[message["key"]] == message["template"], message["key"]
        assert primitives.placeholders(message["template"]) == message["arguments"], message["key"]
    assert {(message["code"], message["key"]) for message in messages} == SDK_PAIRS


def test_produced_errors_carry_the_vector_codes_keys_and_arguments(vector: VectorLoader) -> None:
    messages = {message["key"]: message for message in vector("primitives", "kernel-messages")["messages"]}
    produced = produced_errors()
    # 除了 Python 不会产生的未初始化结果，向量里的每个内核错误都由 SDK 实际产生一次
    assert {error.message_key for error in produced} == set(messages) - {primitives.MESSAGE_UNINITIALIZED_RESULT}
    for error in produced:
        assert error.message_key is not None
        message = messages[error.message_key]
        assert error.code == message["code"], error.message_key
        assert sorted(error.details or {}) == sorted(message["arguments"]), error.message_key
        # 没有安装目录时 message 就是默认英文模板格式化的结果，上线的 message 与 .NET 宿主的英文一致
        assert error.message == primitives.format_message(message["template"], error.details), error.message_key
        assert "{" not in error.message, error.message
    duplicate = next(error for error in produced if error.code == operation.ERROR_CODE_DUPLICATE)
    assert duplicate.message == "Operation 'pings.send' is already registered by 'app'."


@pytest.mark.usefixtures("default_messages")
def test_installed_catalogs_take_precedence_over_the_built_in_templates() -> None:
    primitives.set_default_messages(
        primitives.Catalog({"zh-CN": {"kernel.operation.unknown": "操作 '{id}' 未登记。"}}), "zh-CN"
    )
    error = Error.not_found(operation.ERROR_CODE_NOT_FOUND, operation.MESSAGE_NOT_FOUND, {"id": "a.b"})
    assert error.message == "操作 'a.b' 未登记。"
    # 安装的目录没有这个键时退回默认英文模板，而不是描述
    assert invocation.cancelled_error().message == "The invocation was canceled."
    # 默认模板的占位符缺值时按严格格式化退回描述
    assert (
        Error.not_found(operation.ERROR_CODE_NOT_FOUND, "kernel.operation.unknown").message
        == "kernel.operation.unknown"
    )


@pytest.fixture
def default_messages() -> Any:
    """测试结束后还原进程默认的消息解析器与区域。"""
    yield
    primitives.set_default_messages(None, primitives.DEFAULT_LOCALE)
