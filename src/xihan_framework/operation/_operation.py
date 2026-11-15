"""操作契约与 XGI 消息（RFC-0011）。"""

import re
import threading
from collections.abc import Callable, Coroutine
from dataclasses import KW_ONLY, dataclass
from typing import Any, Final, cast

from xihan_framework._codec import DecodeError, compile_decoder, encode
from xihan_framework._json import JsonValue, dumps, loads
from xihan_framework.invocation import Context, ItemKey
from xihan_framework.pipeline import APP_OWNER, Builder, Step
from xihan_framework.primitives import (
    ERROR_CODE_INVALID_ARGUMENT,
    Error,
    FrameworkError,
    Result,
    Stability,
    Unit,
    fail,
    success,
)

#: 操作未登记。
ERROR_CODE_NOT_FOUND: Final = "KERNEL_NOTFOUND_001"
#: 操作载荷不合法（非 JSON、``null`` 或类型不匹配）。
ERROR_CODE_INVALID_PAYLOAD: Final = "KERNEL_VALIDATION_001"
#: 类型化调用的请求或响应类型与登记不符。
ERROR_CODE_TYPE_MISMATCH: Final = "KERNEL_VALIDATION_002"
#: 操作 ID 重复登记（配置期）。
ERROR_CODE_DUPLICATE: Final = "KERNEL_OPERATION_001"
#: 操作登记表已满（配置期）。
ERROR_CODE_REGISTRY_FULL: Final = "KERNEL_OPERATION_002"
#: 调用期间的意外失败：响应编码不了、管道成功却没有响应。
ERROR_CODE_UNEXPECTED: Final = "KERNEL_UNEXPECTED_001"

#: 操作未登记的消息键（参数 ``id``）。
MESSAGE_NOT_FOUND: Final = "kernel.operation.unknown"
#: 载荷不合法的消息键（参数 ``id``、``reason``）；与 .NET 宿主的目录同键。
MESSAGE_INVALID_PAYLOAD: Final = "kernel.operation.invalid_payload"
#: 类型不符的消息键（参数 ``id``、``expected``、``actual``）。
MESSAGE_TYPE_MISMATCH: Final = "kernel.operation.type_mismatch"
#: 管道成功结束却没有执行操作的消息键（参数 ``id``）。
MESSAGE_NO_RESPONSE: Final = "kernel.operation.no_response"

#: 登记表的容量。
CAPACITY: Final = 10000

# 暴露的协议名
EXPOSE_HTTP: Final = "http"
EXPOSE_GRPC: Final = "grpc"
EXPOSE_MCP: Final = "mcp"
EXPOSE_COMMAND_LINE: Final = "commandline"

# 操作 ID 文法 <resource>.<verb>（conventions/operation-ids.md）
_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

type Handler[Req, Res] = Callable[[Context, Req], Coroutine[Any, Any, Result[Res]]]
"""处理一个操作的异步函数：可恢复失败返回失败结果，不抛异常。"""


def is_id(id: str) -> bool:
    """判断操作 ID 是否符合 ``<resource>.<verb>``。"""
    return _ID.fullmatch(id) is not None


@dataclass(frozen=True, slots=True)
class Contract:
    """操作契约：ID、稳定性、暴露的协议与授权要求（``permission`` 与 ``policy`` 二选一）。

    构造只收 ``id``，其余字段以关键字给出；ID 不合文法、授权要求冲突或为空在构造时即抛出 ``FrameworkError``。
    """

    id: str
    _: KW_ONLY
    stability: Stability = Stability.STABLE
    expose: tuple[str, ...] = ()
    permission: str | None = None
    policy: str | None = None

    def __post_init__(self) -> None:
        """校验并把 ``expose`` 固定成元组。"""
        if not is_id(self.id):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The operation ID '{self.id}' must match <resource>.<verb>."
            )
        if self.permission is not None and self.policy is not None:
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The operation '{self.id}' must declare either a permission or a policy."
            )
        if self.permission == "" or self.policy == "":
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The permission or policy of the operation '{self.id}' must not be empty."
            )
        if isinstance(self.expose, list):
            object.__setattr__(self, "expose", tuple(self.expose))


@dataclass(frozen=True, slots=True)
class Schema:
    """请求与响应的 JSON Schema（draft 2020-12）文本；两段都必须是 JSON 对象，构造时核对。"""

    request: str
    response: str

    def __post_init__(self) -> None:
        """核对两段都是 JSON 对象。"""
        for name, text in (("request", self.request), ("response", self.response)):
            try:
                value = loads(text)
            except ValueError as error:
                raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, f"The {name} schema is not valid JSON.") from error
            if not isinstance(value, dict):
                raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, f"The {name} schema must be a JSON object.")


@dataclass(frozen=True, slots=True)
class Request:
    """XGI 请求：操作 ID 与请求类型的 UTF-8 JSON（空等价于 ``{}``）。"""

    id: str
    payload: bytes = b""


@dataclass(frozen=True, slots=True)
class Response:
    """XGI 响应：结果与成功时响应类型的 UTF-8 JSON；失败时载荷为 ``None``。"""

    outcome: Result[Unit]
    payload: bytes | None = None


class Descriptor:
    """登记表里的一个操作：契约、Schema、归属、请求与响应类型，以及解码、执行、编码的入口。"""

    __slots__ = ("_contract", "_decode", "_handler", "_owner", "_request_type", "_response_type", "_schema")

    def __init__(
        self,
        contract: Contract,
        handler: Handler[Any, Any],
        request_type: type[Any],
        response_type: type[Any],
        schema: Schema | None,
        owner: str,
    ) -> None:
        """由 ``Registry.add`` 创建；请求类型在这里编译成解码函数，不支持的类型抛出 ``TypeError``。"""
        self._contract = contract
        self._handler = handler
        self._request_type = request_type
        self._response_type = response_type
        self._schema = schema
        self._owner = owner
        self._decode = compile_decoder(request_type)

    @property
    def contract(self) -> Contract:
        """契约。"""
        return self._contract

    @property
    def id(self) -> str:
        """操作 ID。"""
        return self._contract.id

    @property
    def schema(self) -> Schema | None:
        """可选的 Schema。"""
        return self._schema

    @property
    def owner(self) -> str:
        """归属（清单 ID 或 ``app``）。"""
        return self._owner

    @property
    def request_type(self) -> type[Any]:
        """请求类型。"""
        return self._request_type

    @property
    def response_type(self) -> type[Any]:
        """响应类型。"""
        return self._response_type

    async def execute(self, ctx: Context, payload: bytes) -> Response:
        """解码载荷、执行、编码响应；解码失败返回 ``KERNEL_VALIDATION_001``，不执行操作。"""
        request, invalid = self._decode_payload(payload)
        if invalid is not None:
            return Response(fail(invalid))
        result = await self._handler(ctx, request)
        if result.error is not None:
            return Response(fail(result.error))
        try:
            encoded = dumps(encode(result.value)).encode("utf-8")
        except (TypeError, ValueError) as error:
            return Response(
                fail(Error.literal(ERROR_CODE_UNEXPECTED, f"The response of '{self.id}' could not be encoded: {error}"))
            )
        return Response(success(), encoded)

    def _decode_payload(self, payload: bytes) -> tuple[object, Error | None]:
        """解码请求载荷：空等价于 ``{}``；非 JSON、``null`` 或类型不匹配为 ``KERNEL_VALIDATION_001``，``reason`` 只写 JSON 路径。"""
        text = payload.strip() or b"{}"
        if text == b"null":
            return None, self._invalid_payload("the payload is null")
        try:
            value: JsonValue = loads(text)
        except ValueError:
            return None, self._invalid_payload("$")
        try:
            return self._decode(value), None
        except DecodeError as error:
            return None, self._invalid_payload(error.path)

    def _invalid_payload(self, reason: str) -> Error:
        """载荷不合法的错误。"""
        return Error.validation(ERROR_CODE_INVALID_PAYLOAD, MESSAGE_INVALID_PAYLOAD, {"id": self.id, "reason": reason})

    def __repr__(self) -> str:
        """便于调试的形式。"""
        return f"Descriptor({self.id!r}, owner={self._owner!r})"


class Registry:
    """操作登记表：ID 唯一，容量 ``CAPACITY``；登记与读取都线程安全。"""

    __slots__ = ("_descriptors", "_lock")

    def __init__(self) -> None:
        """创建空的登记表。"""
        self._lock = threading.Lock()
        self._descriptors: dict[str, Descriptor] = {}

    def add[Req, Res](
        self,
        contract: Contract,
        handler: Handler[Req, Res],
        *,
        request_type: type[Req],
        response_type: type[Res],
        schema: Schema | None = None,
        owner: str = APP_OWNER,
    ) -> Descriptor:
        """登记一个操作并返回它的描述。

        重复抛出 ``FrameworkError``（``KERNEL_OPERATION_001``），满了抛出 ``KERNEL_OPERATION_002``，归属为空抛出
        ``PRIMITIVES_VALIDATION_001``；请求类型不能从 JSON 解码时抛出 ``TypeError``。
        """
        if not owner:
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT,
                f"The operation '{contract.id}' needs an owner: a manifest ID or '{APP_OWNER}'.",
            )
        descriptor = Descriptor(contract, handler, request_type, response_type, schema, owner)
        # 重复与容量在同一把锁里判定和写入，并发登记不会越过容量或写进两个同 ID 的操作
        with self._lock:
            if contract.id in self._descriptors:
                raise FrameworkError(ERROR_CODE_DUPLICATE, f"The operation '{contract.id}' is already registered.")
            if len(self._descriptors) >= CAPACITY:
                raise FrameworkError(
                    ERROR_CODE_REGISTRY_FULL, f"The operation registry is full ({CAPACITY} operations)."
                )
            self._descriptors[contract.id] = descriptor
        return descriptor

    def find(self, id: str) -> Descriptor | None:
        """按 ID 取描述；没有时为 ``None``。"""
        with self._lock:
            return self._descriptors.get(id)

    def descriptors(self) -> list[Descriptor]:
        """按 ID 升序列出全部描述。"""
        with self._lock:
            found = list(self._descriptors.values())
        return sorted(found, key=lambda descriptor: descriptor.id)

    def __len__(self) -> int:
        """已登记的操作数。"""
        with self._lock:
            return len(self._descriptors)


# 上下文对象袋里的键；只在本模块里写入
_DESCRIPTOR: Final = ItemKey[Descriptor]("xihan.operation.descriptor")
_REQUEST: Final = ItemKey[Request]("xihan.operation.request")
_RESPONSE: Final = ItemKey[bytes | None]("xihan.operation.response")
_TERMINAL: Final = ItemKey[Step | None]("xihan.operation.terminal")


def current(ctx: Context) -> Descriptor | None:
    """当前调用的操作描述（管道里的中间件用）。"""
    return ctx.find_item(_DESCRIPTOR)


def request_of(ctx: Context) -> Request | None:
    """当前调用的 XGI 请求；类型化调用的请求只有 ID、没有载荷。"""
    return ctx.find_item(_REQUEST)


def response_payload_of(ctx: Context) -> bytes | None:
    """``handle`` 阶段写回的响应载荷（成功时）。"""
    return ctx.find_item(_RESPONSE)


class Invoker:
    """让操作经过完整管道执行：管道的终点在 ``handle`` 阶段之后分派当前操作。

    每次调用都在 ``Context.run`` 里执行，截止时间与取消信号翻译成 ``KERNEL_TIMEOUT_001`` / ``KERNEL_CANCELLED_001``。
    操作描述、请求与响应放在上下文的对象袋里，派生上下文共享对象袋，所以并发的调用各用自己的上下文。
    """

    __slots__ = ("_registry", "_step")

    def __init__(self, registry: Registry, builder: Builder | None = None) -> None:
        """用登记表与管道构建调用器；管道的排序错误在这里以 ``FrameworkError`` 抛出。"""
        self._registry = registry
        self._step = (Builder() if builder is None else builder).build(_dispatch)

    @property
    def registry(self) -> Registry:
        """登记表。"""
        return self._registry

    async def invoke(self, ctx: Context, request: Request) -> Response:
        """执行一个 XGI 请求：未登记返回 ``KERNEL_NOTFOUND_001``（不进管道）；否则经完整管道执行。"""
        descriptor = self._registry.find(request.id)
        if descriptor is None:
            return Response(fail(_not_found(request.id)))
        ctx.set_item(_DESCRIPTOR, descriptor)
        ctx.set_item(_REQUEST, request)
        # 同一个上下文上先后的调用共享对象袋：清掉上一次的响应与类型化终点，免得短路的调用读到旧值
        ctx.set_item(_RESPONSE, None)
        ctx.set_item(_TERMINAL, None)
        outcome = await ctx.run(self._step(ctx))
        if outcome.is_failed:
            return Response(outcome)
        return Response(outcome, ctx.find_item(_RESPONSE))

    async def invoke_typed[Req, Res](
        self, ctx: Context, id: str, request: Req, response_type: type[Res]
    ) -> Result[Res]:
        """类型化入口：不经 JSON，直接以请求对象经完整管道执行。

        请求不是登记的请求类型的实例、或 ``response_type`` 不是登记的响应类型时返回 ``KERNEL_VALIDATION_002``；管道
        成功结束却没有执行操作（中间件短路成功）时返回 ``KERNEL_UNEXPECTED_001``。
        """
        descriptor = self._registry.find(id)
        if descriptor is None:
            return fail(_not_found(id))
        if not _is_instance(request, descriptor.request_type) or response_type != descriptor.response_type:
            return fail(
                Error.validation(
                    ERROR_CODE_TYPE_MISMATCH,
                    MESSAGE_TYPE_MISMATCH,
                    {
                        "id": id,
                        "expected": f"{_type_name(descriptor.request_type)} -> {_type_name(descriptor.response_type)}",
                        "actual": f"{type(request).__name__} -> {_type_name(response_type)}",
                    },
                )
            )
        handler = cast(Handler[Req, Res], descriptor._handler)
        results: list[Result[Res]] = []

        async def terminal(ctx: Context) -> Result[Unit]:
            result = await handler(ctx, request)
            results.append(result)
            return result.unit()

        ctx.set_item(_DESCRIPTOR, descriptor)
        ctx.set_item(_REQUEST, Request(id))
        ctx.set_item(_RESPONSE, None)
        ctx.set_item(_TERMINAL, terminal)
        try:
            outcome = await ctx.run(self._step(ctx))
        finally:
            ctx.set_item(_TERMINAL, None)
        if outcome.error is not None:
            return fail(outcome.error)
        if not results:
            return fail(Error.unexpected(ERROR_CODE_UNEXPECTED, MESSAGE_NO_RESPONSE, {"id": id}))
        return results[0]


async def _dispatch(ctx: Context) -> Result[Unit]:
    """管道的终点：类型化调用执行它放进来的终点，否则执行当前操作并把响应载荷写回上下文。"""
    terminal = ctx.find_item(_TERMINAL)
    if terminal is not None:
        return await terminal(ctx)
    descriptor = current(ctx)
    if descriptor is None:
        return fail(Error.not_found(ERROR_CODE_NOT_FOUND, MESSAGE_NOT_FOUND))
    request = request_of(ctx)
    response = await descriptor.execute(ctx, b"" if request is None else request.payload)
    if response.outcome.is_ok:
        ctx.set_item(_RESPONSE, response.payload)
    return response.outcome


def _not_found(id: str) -> Error:
    """操作未登记的错误。"""
    return Error.not_found(ERROR_CODE_NOT_FOUND, MESSAGE_NOT_FOUND, {"id": id})


def _is_instance(value: object, expected: type[Any]) -> bool:
    """按登记的类型判断实例；参数化的泛型（``list[int]``）按它的原始类型判断。"""
    origin = getattr(expected, "__origin__", expected)
    return isinstance(origin, type) and isinstance(value, origin)


def _type_name(value: type[Any]) -> str:
    """类型名，用于错误细节。"""
    return getattr(value, "__name__", repr(value))
