"""操作契约与 XGI 消息（RFC-0011）。"""

import logging
import threading
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import KW_ONLY, dataclass
from enum import StrEnum
from typing import Any, Final, cast

from xihan_framework._codec import DecodeError, check_json_names, compile_decoder, encode
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
from xihan_framework.primitives._operation_id import MAX_OPERATION_ID_LENGTH, is_operation_id

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
#: 载荷不合法的消息键（参数 ``id``、``reason``）；与 kernel-messages 向量同键，宿主按它再翻译。
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
#: 操作 ID 的最大长度（conventions/operation-ids.md、RFC-0011「操作 ID」；与 .NET 的 ``OperationId.MaxLength`` 相同）。
MAX_ID_LENGTH: Final = MAX_OPERATION_ID_LENGTH

#: 调用器内置步骤的归属：内核清单 ID（RFC-0011 规则 10）。
KERNEL_OWNER: Final = "xihan.kernel.core"
#: Observe 阶段的内置步骤：把排在它之后的中间件与处理器抛出的异常翻译成 ``KERNEL_UNEXPECTED_001``。
OBSERVE_MIDDLEWARE: Final = "ObserveMiddleware"
#: Resilience 阶段的内置步骤：进入时核对截止时间，之后在截止时间与取消信号下执行（``Context.run``）。
DEADLINE_MIDDLEWARE: Final = "DeadlineMiddleware"
#: Validate 阶段的内置步骤：原始调用在这里把载荷解码成请求，类型化调用跳过。
VALIDATION_MIDDLEWARE: Final = "ValidationMiddleware"


class Expose(StrEnum):
    """操作暴露给哪个协议（RFC-0011「契约」「协议映射」）：五个协议，值是插件协议 ``OperationDeclaration.expose`` 里的名字。

    协议适配器按它决定是否发布一个操作；成员是字符串，等于各自的名字（``Expose.HTTP == "http"``）。
    """

    #: HTTP 路由。
    HTTP = "http"
    #: gRPC 方法。
    GRPC = "grpc"
    #: MCP tool。
    MCP = "mcp"
    #: 命令行。
    COMMAND_LINE = "commandline"
    #: ``ai.tools@1`` 目录里的模型工具（RFC-0022）。
    AI = "ai"


# 早于 Expose 就有的协议名常量：各是对应成员的别名，保留给已有的代码
EXPOSE_HTTP: Final = Expose.HTTP
EXPOSE_GRPC: Final = Expose.GRPC
EXPOSE_MCP: Final = Expose.MCP
EXPOSE_COMMAND_LINE: Final = Expose.COMMAND_LINE
EXPOSE_AI: Final = Expose.AI

type Handler[Req, Res] = Callable[[Context, Req], Coroutine[Any, Any, Result[Res]]]
"""处理一个操作的异步函数：可恢复失败返回失败结果，不抛异常。"""


def is_id(id: str) -> bool:
    """判断操作 ID 是否符合 ``<resource>.<verb>`` 且不超过 ``MAX_ID_LENGTH`` 个字符（conventions/operation-ids.md）。

    清单的 ``contributes.operations`` 用同一条规则；向量 ``primitives/operation-ids.json``。
    """
    return is_operation_id(id)


@dataclass(frozen=True, slots=True)
class Contract:
    """操作契约：ID、稳定性、暴露的协议与授权要求（``permission`` 与 ``policy`` 二选一）。

    构造只收 ``id``，其余字段以关键字给出；ID 不合文法、``expose`` 里有五个协议之外的名字、授权要求冲突或为空在构造时即
    抛出 ``FrameworkError``。``expose`` 收 ``Expose`` 成员或它们的名字（``"http"``），契约里一律存成成员；缺省为空，即不
    对外发布。
    """

    id: str
    _: KW_ONLY
    stability: Stability = Stability.STABLE
    expose: tuple[str, ...] = ()
    permission: str | None = None
    policy: str | None = None

    def __post_init__(self) -> None:
        """校验并把 ``expose`` 固定成 ``Expose`` 成员的元组。"""
        if not is_id(self.id):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT,
                f"The operation ID '{self.id}' must match <resource>.<verb> and be at most {MAX_ID_LENGTH} "
                "characters long.",
            )
        if self.permission is not None and self.policy is not None:
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The operation '{self.id}' must declare either a permission or a policy."
            )
        if self.permission == "" or self.policy == "":
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The permission or policy of the operation '{self.id}' must not be empty."
            )
        object.__setattr__(self, "expose", _protocols(self.id, self.expose))


def _protocols(id: str, expose: object) -> tuple[Expose, ...]:
    """把契约的 ``expose`` 固定成 ``Expose`` 成员的元组；不是一组协议、或有五个协议之外的名字时抛出 ``FrameworkError``。"""
    # 字符串也能迭代：逐个字符去认只会报出一个莫名其妙的名字，所以单个字符串（含单个成员）直接拒绝
    if isinstance(expose, str) or not isinstance(expose, Iterable):
        raise FrameworkError(
            ERROR_CODE_INVALID_ARGUMENT,
            f"The 'expose' of the operation '{id}' must be a tuple of protocols, such as (Expose.HTTP,).",
        )
    protocols: list[Expose] = []
    for protocol in expose:
        try:
            protocols.append(Expose(protocol))
        except ValueError as error:
            known = ", ".join(f"'{member}'" for member in Expose)
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT,
                f"The operation '{id}' cannot be exposed to '{protocol}': the protocols are {known}.",
            ) from error
    return tuple(protocols)


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
        """由 ``Registry.add`` 创建；请求类型在这里编译成解码函数，不支持的类型抛出 ``TypeError``。

        响应类型（含嵌套的类型）里写进 JSON 的 dataclass 有两个字段同名时同样抛出 ``TypeError``（ADR-0319、ADR-0340），
        与请求类型同一个时机，不等到调用时才以 ``KERNEL_UNEXPECTED_001`` 结束。
        """
        self._contract = contract
        self._handler = handler
        self._request_type = request_type
        self._response_type = response_type
        self._schema = schema
        self._owner = owner
        self._decode = compile_decoder(request_type)
        check_json_names(response_type)

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
        """把成功的响应对象编码成载荷；写不出（非有限的浮点、编码不了的值）时以 ``unhandled_exception`` 结束，不抛出。

        写不成合法的 JSON 文本同样是写不出：字符串（值、成员名与映射的键）里有代理项码点时不写成转义（ADR-0318），写出的
        整段文本还要过 RFC-0001「JSON 文本」的共同检查（``parse``），嵌套超过 64 层（从响应的根算起）或任何一层有重复的
        成员名（如 ``to_json_value`` 交出 ``items()`` 带同名成员的映射）也写不出（RFC-0011 规则 10 的「编码」，
        ADR-0319）；这些错误都不带 ``inner``，只有 ``FrameworkError`` 的错误才放进 ``inner``。
        """
        try:
            text = dumps(encode(value), strict=True)
            # 写完之后用读取一侧同一份检查核对整段文本，而不是在 _write 里另记深度与成员名：to_json_value 交回的值不再
            # 经过 encode，int、float 的子类与 JsonNumber 的原文都可能写出写法以外的文本，只有读回去才与对方的判定一致；
            # 代价是多读一遍整段文本
            parse(text)
            payload = text.encode("utf-8")
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
        ``PRIMITIVES_VALIDATION_001``；请求类型不能从 JSON 解码，或者请求、响应类型里的 dataclass 有两个字段写成同一个
        JSON 名时抛出 ``TypeError``。
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
    """调用状态里的请求或响应对象；包一层是为了与“还没有”区分开。"""

    value: object


class _Invocation:
    """一次操作调用的状态：描述、请求与类型化调用的终点，以及途中写下的解码结果、响应对象与编码后的载荷。

    每次调用新建一个，随派生的上下文交给中间件与处理器（``Context.with_value``），不逐项放进共享的对象袋：同一个上下文上
    嵌套或并发的调用各有各的状态，互不覆盖。途中的字段只由这次调用自己的管道写入，每次都是整体换一个引用，处理器把
    上下文交给别的线程去读也不需要锁。
    """

    __slots__ = ("decoded", "descriptor", "payload", "produced", "request", "terminal")

    def __init__(self, descriptor: Descriptor, request: Request, decoded: _Box | None) -> None:
        """用描述与请求创建；``decoded`` 是类型化调用传入的请求对象，原始调用为 ``None``、到 Validate 阶段才解码。"""
        self.descriptor = descriptor
        self.request = request
        self.decoded = decoded
        # 处理器成功返回的响应对象，编码之前
        self.produced: _Box | None = None
        # XGI 入口在管道结束之后编码出的载荷
        self.payload: bytes | None = None
        # 类型化调用的终点；原始调用没有，由管道的终点按描述执行
        self.terminal: Step | None = None


# 调用状态的键，只在本模块里写入：管道里的上下文按派生携带它（with_value）；对象袋里另记最近进入或结束的那次调用，
# 给没有携带它的上下文读，也就是调用方传给调用器的那个（调用返回之后读响应载荷）
_INVOCATION: Final = ItemKey[_Invocation]("xihan.operation.invocation")


def _invocation_of(ctx: Context) -> _Invocation | None:
    """上下文所在的调用的状态：先取它按派生携带的；没有携带时取对象袋里记的最近一次，都没有为 ``None``。"""
    invocation = ctx.find_value(_INVOCATION)
    return ctx.find_item(_INVOCATION) if invocation is None else invocation


def current(ctx: Context) -> Descriptor | None:
    """当前调用的操作描述（管道里的中间件用）。

    中间件与处理器拿到的上下文携带所在调用的状态，同一个上下文上嵌套或并发的调用互不影响：处理器里再调另一个操作，
    返回之后外层读到的仍是外层的操作。没有携带状态的上下文（调用方传给调用器的那个）读到的是它的对象袋上最近进入或
    结束的那次调用，供调用返回之后读取；没有调用过为 ``None``。``request_of``、``decoded_request_of``、``response_of``
    与 ``response_payload_of`` 按同样的规则取。
    """
    invocation = _invocation_of(ctx)
    return None if invocation is None else invocation.descriptor


def request_of(ctx: Context) -> Request | None:
    """当前调用的 XGI 请求；类型化调用的请求只有 ID、没有载荷。"""
    invocation = _invocation_of(ctx)
    return None if invocation is None else invocation.request


def decoded_request_of(ctx: Context) -> object | None:
    """解码好的请求对象（RFC-0011 规则 3）：原始调用在 Validate 阶段解码之后才有，类型化调用是传入的请求对象。"""
    invocation = _invocation_of(ctx)
    return None if invocation is None or invocation.decoded is None else invocation.decoded.value


def response_of(ctx: Context) -> object | None:
    """执行成功的响应对象（RFC-0011 规则 3）：处理器返回之后才有，是编码之前的对象，管道里的中间件看到的就是它。"""
    invocation = _invocation_of(ctx)
    return None if invocation is None or invocation.produced is None else invocation.produced.value


def response_payload_of(ctx: Context) -> bytes | None:
    """XGI 入口在管道结束之后编码的响应载荷（成功时）；管道里还没有它，调用返回之后在调用方的上下文上读。"""
    invocation = _invocation_of(ctx)
    return None if invocation is None else invocation.payload


class Invoker:
    """让操作经过完整管道执行：管道的终点在 ``handle`` 阶段之后分派当前操作。

    调用器带三个内置步骤，位置按 RFC-0011 规则 10（ADR-0236）：都按归属 ``xihan.kernel.core``
    放在所在阶段的 ``Beginning``，与同位置的其他中间件按 RFC-0004 的规则排序（归属的序数序，同归属按登记顺序，内置
    步骤先登记）。

    - Observe 阶段的 ``ObserveMiddleware``：排在它之后的中间件与处理器抛出的异常翻译为 ``KERNEL_UNEXPECTED_001`` +
      ``kernel.invocation.unhandled_exception``（规则 5）；排在它之前的（Ingress 阶段，或 Observe 阶段 ``Beginning``
      上归属排在 ``xihan.kernel.core`` 之前的）抛出的异常原样交给调用方。
    - Resilience 阶段的 ``DeadlineMiddleware``：进入时截止时间已过即 ``KERNEL_TIMEOUT_001`` +
      ``kernel.invocation.deadline_before_start``，不核对取消；之后的中间件与处理器在 ``Context.run`` 里执行，拿到的
      上下文在调用方取消或到达截止时间时触发信号。取消与截止时间只经信号通知，不中止处理器（RFC-0003 规则 1）。
    - Validate 阶段的 ``ValidationMiddleware``：原始调用在这里解码载荷，失败即 ``KERNEL_VALIDATION_001``；类型化调用跳过。

    进入管道之前只核对未登记（``KERNEL_NOTFOUND_001``）与类型化调用的类型不符（``KERNEL_VALIDATION_002``），按这个顺序。
    XGI 入口在管道结束之后才把响应对象编码成载荷，中间件看到的是编码之前的对象（``response_of``）；写不出时以
    ``kernel.invocation.unhandled_exception`` 结束（规则 10 的「编码」）。

    每次调用的状态（操作描述、请求与响应）随派生的上下文交给中间件与处理器，不逐项放进共享的对象袋：同一个上下文上
    嵌套的调用（处理器或中间件里再调另一个操作）与并发的调用（``asyncio.gather``）各有各的状态。管道的终点只执行上下文
    携带的那次调用，所以中间件往下传的必须是它拿到的上下文或由它派生的上下文。
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
        响应写不出（类型化的浮点是 NaN 或无穷、字符串里有代理项码点、编码不了的值、写出的文本嵌套超过 64 层或有重复的
        成员名）时返回 ``KERNEL_UNEXPECTED_001`` + ``kernel.invocation.unhandled_exception``，框架异常的错误放进
        ``inner``，不抛给调用方，没有载荷。
        """
        descriptor = self._registry.find(request.id)
        if descriptor is None:
            return Response(fail(_not_found(request.id)))
        invocation = _Invocation(descriptor, request, None)
        outcome = await self._run(ctx, invocation)
        if outcome.is_failed:
            return Response(outcome)
        if invocation.produced is None:
            # 管道成功结束却没有响应：某个中间件以成功短路、handle 没有执行（RFC-0011 规则 9），不返回成功的空载荷
            return Response(fail(_no_response(request.id)))
        response = descriptor._encode_response(invocation.produced.value)
        invocation.payload = response.payload
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
        invocation = _Invocation(descriptor, Request(id), _Box(request))

        async def terminal(ctx: Context) -> Result[Unit]:
            result = await handler(ctx, request)
            results.append(result)
            if result.error is None:
                invocation.produced = _Box(result.value)
            return result.unit()

        invocation.terminal = terminal
        outcome = await self._run(ctx, invocation)
        if outcome.error is not None:
            return fail(outcome.error)
        if not results:
            return fail(_no_response(id))
        return results[0]

    async def _run(self, ctx: Context, invocation: _Invocation) -> Result[Unit]:
        """让一次调用经过管道：它的状态随派生的上下文往下传，同一个上下文上嵌套或并发的调用互不覆盖。"""
        scoped = ctx.with_value(_INVOCATION, invocation)
        # 对象袋里的这一份只给没有携带状态的上下文读（调用方手里的那个）：进入时记下，途中读到的是这次调用
        ctx.set_item(_INVOCATION, invocation)
        try:
            return await self._step(scoped)
        finally:
            # 结束时再记一次：嵌套在里面的调用先结束，调用方在返回之后读到的是自己这一次，而不是里面那次
            ctx.set_item(_INVOCATION, invocation)
            # 终点只在管道里用：放掉它，连同它闭包里的请求对象与处理器
            invocation.terminal = None


async def _observe(ctx: Context, next_step: Step) -> Result[Unit]:
    """Observe 阶段的内置步骤：下游未处理的异常翻译为 ``KERNEL_UNEXPECTED_001``（RFC-0011 规则 5）。

    ``Exception`` 之外的 ``BaseException``（含 ``CancelledError``）照常传播；完整异常经 ``logging`` 记下，上线的错误不带
    异常文本，``FrameworkError`` 以 ``to_error()`` 作内部错误。
    """
    try:
        return await next_step(ctx)
    except Exception as exception:
        descriptor = current(ctx)
        id = None if descriptor is None else descriptor.id
        return fail(_unhandled(id, exception, "The operation '%s' raised an unhandled exception."))


async def _deadline(ctx: Context, next_step: Step) -> Result[Unit]:
    """Resilience 阶段的内置步骤：进入时核对截止时间，之后在截止时间与取消信号下执行下游（``Context.run``）。"""
    return await ctx.run(next_step)


async def _validate(ctx: Context, next_step: Step) -> Result[Unit]:
    """Validate 阶段的内置步骤：原始调用解码载荷并记进调用状态，失败即结束；类型化调用已经有请求对象，跳过。"""
    # 只认上下文携带的状态，不看对象袋里最近的那次：那可能是同一个上下文上的另一次调用
    invocation = ctx.find_value(_INVOCATION)
    if invocation is not None and invocation.decoded is None:
        value, invalid = invocation.descriptor.decode(invocation.request.payload)
        if invalid is not None:
            return fail(invalid)
        invocation.decoded = _Box(value)
    return await next_step(ctx)


async def _dispatch(ctx: Context) -> Result[Unit]:
    """管道的终点：类型化调用执行它带来的终点，否则以解码好的请求执行当前操作，把响应对象记进调用状态，不编码。"""
    # 与 Validate 阶段一样只认上下文携带的状态：对象袋里最近的那次可能是别的调用，执行它就串了操作
    invocation = ctx.find_value(_INVOCATION)
    if invocation is None:
        # 中间件往下传的不是它拿到的上下文或由它派生的上下文：没有可执行的操作
        return fail(Error.not_found(ERROR_CODE_NOT_FOUND, MESSAGE_NOT_FOUND))
    if invocation.terminal is not None:
        return await invocation.terminal(ctx)
    descriptor = invocation.descriptor
    decoded = invocation.decoded
    if decoded is None:
        # Validate 阶段的内置步骤总会先解码；没有解码结果时同样按规则 2 解码，不让处理器拿到未解码的载荷
        value, invalid = descriptor.decode(invocation.request.payload)
        if invalid is not None:
            return fail(invalid)
        decoded = _Box(value)
    result = await descriptor._handler(ctx, decoded.value)
    if result.error is not None:
        return fail(result.error)
    invocation.produced = _Box(result.value)
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
