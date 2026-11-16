"""操作契约与 XGI 消息（RFC-0011）。"""

import logging
import re
import threading
from collections.abc import Callable, Coroutine
from dataclasses import KW_ONLY, dataclass
from typing import Any, Final, cast

from xihan_framework._codec import DecodeError, compile_decoder, encode
from xihan_framework._json import JsonTextError, JsonValue, dumps, parse
from xihan_framework.invocation import Context, ItemKey
from xihan_framework.pipeline import APP_OWNER, Builder, Placement, Step, func
from xihan_framework.pipeline import Descriptor as StepDescriptor
from xihan_framework.primitives import (
    ERROR_CODE_INVALID_ARGUMENT,
    Error,
    FrameworkError,
    PipelineStage,
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
#: 类型不符的消息键（参数只有 ``id``，ADR-0236）。
MESSAGE_TYPE_MISMATCH: Final = "kernel.operation.type_mismatch"
#: 管道成功结束却没有执行操作的消息键（参数 ``id``）。
MESSAGE_NO_RESPONSE: Final = "kernel.operation.no_response"
#: 调用期间发生未处理异常的消息键（码 ``KERNEL_UNEXPECTED_001``，没有参数）。
MESSAGE_UNHANDLED_EXCEPTION: Final = "kernel.invocation.unhandled_exception"
#: 操作 ID 重复登记的消息键（参数 ``id``、``owner``）。
MESSAGE_DUPLICATE: Final = "kernel.operation.duplicate"
#: 操作登记表已满的消息键（参数 ``capacity``）。
MESSAGE_REGISTRY_FULL: Final = "kernel.operation.registry_full"

#: 登记表的容量。
CAPACITY: Final = 10000

#: 调用器内置步骤的归属：内核清单 ID，与 .NET 的内置中间件相同（RFC-0011 规则 10）。
KERNEL_OWNER: Final = "xihan.kernel.core"
#: Observe 阶段的内置步骤：把排在它之后的中间件与处理器抛出的异常翻译成 ``KERNEL_UNEXPECTED_001``。
OBSERVE_MIDDLEWARE: Final = "ObserveMiddleware"
#: Resilience 阶段的内置步骤：进入时核对截止时间，之后在截止时间与取消信号下执行（``Context.run``）。
DEADLINE_MIDDLEWARE: Final = "DeadlineMiddleware"
#: Validate 阶段的内置步骤：原始调用在这里把载荷解码成请求，类型化调用跳过。
VALIDATION_MIDDLEWARE: Final = "ValidationMiddleware"

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
                value = parse(text)
            except JsonTextError as error:
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
        """不经过管道地解码、执行并编码响应；解码失败返回 ``KERNEL_VALIDATION_001``，不执行操作。

        响应写不出时与调用器相同（RFC-0011 规则 10 的「编码」）：``KERNEL_UNEXPECTED_001`` +
        ``kernel.invocation.unhandled_exception``，没有载荷。调用器在 Validate 阶段解码、在管道的终点执行、在管道结束之后编码。
        """
        request, invalid = self.decode(payload)
        if invalid is not None:
            return Response(fail(invalid))
        result = await self._handler(ctx, request)
        if result.error is not None:
            return Response(fail(result.error))
        return self._encode_response(result.value)

    def decode(self, payload: bytes) -> tuple[object, Error | None]:
        """解码请求载荷（RFC-0011 规则 2）：成功时为 ``(请求, None)``，不合法时为 ``(None, KERNEL_VALIDATION_001)``。

        空载荷等价于 ``{}``。先过共同的 JSON 读取检查：语法错误（含只有空白、BOM、嵌套超过 64 层、非法 UTF-8、孤立的
        代理项）的 ``reason`` 写 ``$``，任何一层有重复成员时写第一处重复的路径；再按请求类型解码，``reason`` 是第一处
        不符的路径，缺少必填成员时是所在对象的路径；载荷是 ``null`` 或不是对象时写 ``$``。任意 JSON 类型的成员原样保留
        读到的数字（``JsonNumber``），写回时与读到的 JSON 值相等。
        """
        try:
            value: JsonValue = parse(payload or b"{}", exact=True)
        except JsonTextError as error:
            return None, self._invalid_payload(error.path)
        try:
            return self._decode(value), None
        except DecodeError as error:
            return None, self._invalid_payload(error.path)

    def _encode_response(self, value: object) -> Response:
        """把成功的响应对象编码成载荷；写不出（非有限的浮点、编码不了的值）时以 ``unhandled_exception`` 结束，不抛出。"""
        try:
            payload = dumps(encode(value), strict=True).encode("utf-8")
        except Exception as exception:
            return Response(
                fail(_unhandled(self.id, exception, "The response of the operation '%s' could not be encoded."))
            )
        return Response(success(), payload)

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

        重复抛出 ``FrameworkError``（``KERNEL_OPERATION_001`` + ``kernel.operation.duplicate``），满了抛出
        ``KERNEL_OPERATION_002`` + ``kernel.operation.registry_full``（``to_error()`` 交回带键与参数的错误），归属为空抛出
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
            existing = self._descriptors.get(contract.id)
            if existing is not None:
                # 码、键与参数照 kernel-messages 向量；以异常交给调用方是 Python 的形态，to_error() 还原出同一个错误
                raise FrameworkError.of(
                    Error(ERROR_CODE_DUPLICATE, MESSAGE_DUPLICATE, {"id": contract.id, "owner": existing.owner})
                )
            if len(self._descriptors) >= CAPACITY:
                raise FrameworkError.of(Error(ERROR_CODE_REGISTRY_FULL, MESSAGE_REGISTRY_FULL, {"capacity": CAPACITY}))
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


_logger = logging.getLogger("xihan_framework.operation")


@dataclass(frozen=True, slots=True)
class _Box:
    """对象袋里的请求或响应对象；包一层是为了与“还没有”区分开。"""

    value: object


# 上下文对象袋里的键；只在本模块里写入
_DESCRIPTOR: Final = ItemKey[Descriptor]("xihan.operation.descriptor")
_REQUEST: Final = ItemKey[Request]("xihan.operation.request")
_DECODED: Final = ItemKey[_Box | None]("xihan.operation.decoded")
_PRODUCED: Final = ItemKey[_Box | None]("xihan.operation.produced")
_RESPONSE: Final = ItemKey[bytes | None]("xihan.operation.response")
_TERMINAL: Final = ItemKey[Step | None]("xihan.operation.terminal")


def current(ctx: Context) -> Descriptor | None:
    """当前调用的操作描述（管道里的中间件用）。"""
    return ctx.find_item(_DESCRIPTOR)


def request_of(ctx: Context) -> Request | None:
    """当前调用的 XGI 请求；类型化调用的请求只有 ID、没有载荷。"""
    return ctx.find_item(_REQUEST)


def decoded_request_of(ctx: Context) -> object | None:
    """解码好的请求对象（RFC-0011 规则 3）：原始调用在 Validate 阶段解码之后才有，类型化调用是传入的请求对象。"""
    decoded = ctx.find_item(_DECODED)
    return None if decoded is None else decoded.value


def response_of(ctx: Context) -> object | None:
    """执行成功的响应对象（RFC-0011 规则 3）：处理器返回之后才有，是编码之前的对象，管道里的中间件看到的就是它。"""
    produced = ctx.find_item(_PRODUCED)
    return None if produced is None else produced.value


def response_payload_of(ctx: Context) -> bytes | None:
    """XGI 入口在管道结束之后编码的响应载荷（成功时）；管道里还没有它。"""
    return ctx.find_item(_RESPONSE)


class Invoker:
    """让操作经过完整管道执行：管道的终点在 ``handle`` 阶段之后分派当前操作。

    调用器带三个内置步骤，位置与 .NET 的内置中间件相同（RFC-0011 规则 10，ADR-0236）：都按归属 ``xihan.kernel.core``
    放在所在阶段的 ``Beginning``，与同位置的其他中间件按 RFC-0004 的规则排序（归属的序数序，同归属按登记顺序，内置
    步骤先登记）。

    - Observe 阶段的 ``ObserveMiddleware``：排在它之后的中间件与处理器抛出的异常翻译为 ``KERNEL_UNEXPECTED_001`` +
      ``kernel.invocation.unhandled_exception``（规则 5）；排在它之前的（Ingress 阶段，或 Observe 阶段 ``Beginning``
      上归属排在 ``xihan.kernel.core`` 之前的）抛出的异常原样交给调用方。
    - Resilience 阶段的 ``DeadlineMiddleware``：进入时截止时间已过即 ``KERNEL_TIMEOUT_001`` +
      ``kernel.invocation.deadline_before_start``，不核对取消；之后的中间件与处理器在 ``Context.run`` 里执行，途中的
      超时与取消按 RFC-0003 第 1 条归因。
    - Validate 阶段的 ``ValidationMiddleware``：原始调用在这里解码载荷，失败即 ``KERNEL_VALIDATION_001``；类型化调用跳过。

    进入管道之前只核对未登记（``KERNEL_NOTFOUND_001``）与类型化调用的类型不符（``KERNEL_VALIDATION_002``），按这个顺序。
    XGI 入口在管道结束之后才把响应对象编码成载荷，中间件看到的是编码之前的对象（``response_of``）；写不出时以
    ``kernel.invocation.unhandled_exception`` 结束（规则 10 的「编码」）。操作描述、请求与响应放在上下文的对象袋里，
    派生上下文共享对象袋，所以并发的调用各用自己的上下文。
    """

    __slots__ = ("_pipeline", "_registry", "_step")

    def __init__(self, registry: Registry, builder: Builder | None = None) -> None:
        """用登记表与应用的管道构建调用器：先登记内置步骤，再接上 ``builder`` 的登记与约束（``builder`` 本身不变）。

        应用的中间件与内置步骤重名、排序出错时以 ``FrameworkError`` 抛出（``KERNEL_PIPELINE_00x``）。
        """
        self._registry = registry
        pipeline = Builder()
        pipeline.use_at(func(OBSERVE_MIDDLEWARE, _observe), PipelineStage.OBSERVE, Placement.BEGINNING, KERNEL_OWNER)
        pipeline.use_at(
            func(DEADLINE_MIDDLEWARE, _deadline), PipelineStage.RESILIENCE, Placement.BEGINNING, KERNEL_OWNER
        )
        pipeline.use_at(
            func(VALIDATION_MIDDLEWARE, _validate), PipelineStage.VALIDATE, Placement.BEGINNING, KERNEL_OWNER
        )
        if builder is not None:
            pipeline.extend(builder)
        self._pipeline = pipeline
        self._step = pipeline.build(_dispatch)

    @property
    def registry(self) -> Registry:
        """登记表。"""
        return self._registry

    def describe(self) -> list[StepDescriptor]:
        """调用器实际执行的管道：内置步骤与应用的中间件，排好序。"""
        return self._pipeline.describe()

    def dump(self) -> str:
        """按 ``xihan pipeline dump`` 的格式打印调用器实际执行的管道。"""
        return self._pipeline.dump()

    async def invoke(self, ctx: Context, request: Request) -> Response:
        """执行一个 XGI 请求：未登记返回 ``KERNEL_NOTFOUND_001``（不进管道）；否则经完整管道执行，结束之后编码响应。

        管道成功结束而没有执行操作（中间件短路成功）时返回 ``KERNEL_UNEXPECTED_001`` + ``kernel.operation.no_response``；
        响应写不出（类型化的浮点是 NaN 或无穷、编码不了的值）时返回 ``KERNEL_UNEXPECTED_001`` +
        ``kernel.invocation.unhandled_exception``，框架异常的错误放进 ``inner``，不抛给调用方，没有载荷。
        """
        descriptor = self._registry.find(request.id)
        if descriptor is None:
            return Response(fail(_not_found(request.id)))
        # 同一个上下文上先后的调用共享对象袋：清掉上一次的请求、响应与类型化终点，免得短路的调用读到旧值
        self._enter(ctx, descriptor, request, None, None)
        outcome = await self._step(ctx)
        if outcome.is_failed:
            return Response(outcome)
        produced = ctx.find_item(_PRODUCED)
        if produced is None:
            # 管道成功结束却没有响应：某个中间件以成功短路、handle 没有执行（RFC-0011 规则 9），不返回成功的空载荷
            return Response(fail(_no_response(request.id)))
        response = descriptor._encode_response(produced.value)
        ctx.set_item(_RESPONSE, response.payload)
        return response

    async def invoke_typed[Req, Res](
        self, ctx: Context, id: str, request: Req, response_type: type[Res]
    ) -> Result[Res]:
        """类型化入口：不经 JSON，直接以请求对象经完整管道执行，Validate 阶段不解码，响应也不编码。

        请求不是登记的请求类型的实例、或 ``response_type`` 不是登记的响应类型时返回 ``KERNEL_VALIDATION_002``（参数只有
        ``id``）；管道成功结束却没有执行操作（中间件短路成功）时返回 ``KERNEL_UNEXPECTED_001``。
        """
        descriptor = self._registry.find(id)
        if descriptor is None:
            return fail(_not_found(id))
        if not _is_instance(request, descriptor.request_type) or response_type != descriptor.response_type:
            return fail(Error.validation(ERROR_CODE_TYPE_MISMATCH, MESSAGE_TYPE_MISMATCH, {"id": id}))
        handler = cast(Handler[Req, Res], descriptor._handler)
        results: list[Result[Res]] = []

        async def terminal(ctx: Context) -> Result[Unit]:
            result = await handler(ctx, request)
            results.append(result)
            if result.error is None:
                ctx.set_item(_PRODUCED, _Box(result.value))
            return result.unit()

        self._enter(ctx, descriptor, Request(id), _Box(request), terminal)
        try:
            outcome = await self._step(ctx)
        finally:
            ctx.set_item(_TERMINAL, None)
        if outcome.error is not None:
            return fail(outcome.error)
        if not results:
            return fail(_no_response(id))
        return results[0]

    @staticmethod
    def _enter(
        ctx: Context, descriptor: Descriptor, request: Request, decoded: _Box | None, terminal: Step | None
    ) -> None:
        """把这次调用的描述、请求与终点放进对象袋，清掉上一次的解码结果与响应。"""
        ctx.set_item(_DESCRIPTOR, descriptor)
        ctx.set_item(_REQUEST, request)
        ctx.set_item(_DECODED, decoded)
        ctx.set_item(_PRODUCED, None)
        ctx.set_item(_RESPONSE, None)
        ctx.set_item(_TERMINAL, terminal)


async def _observe(ctx: Context, next_step: Step) -> Result[Unit]:
    """Observe 阶段的内置步骤：下游未处理的异常翻译为 ``KERNEL_UNEXPECTED_001``（RFC-0011 规则 5）。

    ``Exception`` 之外的 ``BaseException``（含 ``CancelledError``）照常传播；完整异常经 ``logging`` 记下，上线的错误不带
    异常文本，``FrameworkError`` 以 ``to_error()`` 作内部错误，与 .NET 的 ``ObserveMiddleware`` 一致。
    """
    try:
        return await next_step(ctx)
    except Exception as exception:
        descriptor = current(ctx)
        id = None if descriptor is None else descriptor.id
        return fail(_unhandled(id, exception, "The operation '%s' raised an unhandled exception."))


async def _deadline(ctx: Context, next_step: Step) -> Result[Unit]:
    """Resilience 阶段的内置步骤：进入时核对截止时间，之后在截止时间与取消信号下执行下游。"""
    return await ctx.run(next_step(ctx))


async def _validate(ctx: Context, next_step: Step) -> Result[Unit]:
    """Validate 阶段的内置步骤：原始调用解码载荷并放进对象袋，失败即结束；类型化调用已经有请求对象，跳过。"""
    descriptor = current(ctx)
    if descriptor is not None and ctx.find_item(_DECODED) is None:
        request = request_of(ctx)
        value, invalid = descriptor.decode(b"" if request is None else request.payload)
        if invalid is not None:
            return fail(invalid)
        ctx.set_item(_DECODED, _Box(value))
    return await next_step(ctx)


async def _dispatch(ctx: Context) -> Result[Unit]:
    """管道的终点：类型化调用执行它放进来的终点，否则以解码好的请求执行当前操作，把响应对象放进对象袋，不编码。"""
    terminal = ctx.find_item(_TERMINAL)
    if terminal is not None:
        return await terminal(ctx)
    descriptor = current(ctx)
    if descriptor is None:
        return fail(Error.not_found(ERROR_CODE_NOT_FOUND, MESSAGE_NOT_FOUND))
    decoded = ctx.find_item(_DECODED)
    if decoded is None:
        # Validate 阶段的内置步骤总会先解码；没有解码结果时同样按规则 2 解码，不让处理器拿到未解码的载荷
        request = request_of(ctx)
        value, invalid = descriptor.decode(b"" if request is None else request.payload)
        if invalid is not None:
            return fail(invalid)
        decoded = _Box(value)
    result = await descriptor._handler(ctx, decoded.value)
    if result.error is not None:
        return fail(result.error)
    ctx.set_item(_PRODUCED, _Box(result.value))
    return success()


def _unhandled(id: str | None, exception: Exception, message: str) -> Error:
    """未处理的异常（RFC-0011 规则 5、10）：完整异常经 ``logging`` 记下，上线的错误不带异常文本，框架异常的错误放进 ``inner``。"""
    _logger.error(message, id, exc_info=exception)
    inner = exception.to_error() if isinstance(exception, FrameworkError) else None
    return Error.unexpected(ERROR_CODE_UNEXPECTED, MESSAGE_UNHANDLED_EXCEPTION, None, inner)


def _no_response(id: str) -> Error:
    """管道以成功结束而没有响应的错误（RFC-0011 规则 9）。"""
    return Error.unexpected(ERROR_CODE_UNEXPECTED, MESSAGE_NO_RESPONSE, {"id": id})


def _not_found(id: str) -> Error:
    """操作未登记的错误。"""
    return Error.not_found(ERROR_CODE_NOT_FOUND, MESSAGE_NOT_FOUND, {"id": id})


def _is_instance(value: object, expected: type[Any]) -> bool:
    """按登记的类型判断实例；参数化的泛型（``list[int]``）按它的原始类型判断。"""
    origin = getattr(expected, "__origin__", expected)
    return isinstance(origin, type) and isinstance(value, origin)
