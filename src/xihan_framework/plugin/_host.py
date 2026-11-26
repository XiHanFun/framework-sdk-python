"""宿主回调服务 ``Host`` 的客户端（``spec/plugin/invocation.md`` §5）。"""

# grpc 的类型存根把服务端上下文、方法处理器与多调用对象写成泛型，运行时它们不能下标：注解推迟求值
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection, Sequence
from typing import Final

import grpc

from xihan_framework.invocation import Context, ItemKey, cancelled_error, deadline_exceeded_error
from xihan_framework.operation import (
    ERROR_CODE_INVALID_PAYLOAD,
    ERROR_CODE_NOT_FOUND,
    MESSAGE_INVALID_PAYLOAD,
    MESSAGE_NOT_FOUND,
    Response,
)
from xihan_framework.plugin._convert import failed, from_status, to_metadata, to_proto_envelope
from xihan_framework.plugin._lifecycle import TenantLedger
from xihan_framework.plugin._messages import (
    ERROR_CODE_CALLBACK_DENIED,
    ERROR_CODE_UNAVAILABLE,
    MESSAGE_CALLBACK_DENIED,
    MESSAGE_CALLBACK_TENANT_DENIED,
    MESSAGE_HOST_UNAVAILABLE,
    MESSAGE_SESSION_INVALID,
)
from xihan_framework.plugin._model import Envelope
from xihan_framework.plugin._protocol import SESSION_METADATA_KEY, Endpoint
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.plugin.v1 import plugin_pb2_grpc as pb_grpc
from xihan_framework.primitives import ERROR_CODE_INVALID_ARGUMENT, Error, FrameworkError, Result, Unit, fail

#: ``Host.Invoke`` 需要的权限。
PERMISSION_INVOKE: Final = "capability:kernel.pipeline"
#: ``Host.Publish`` 需要的权限。
PERMISSION_PUBLISH: Final = "capability:messaging.queue"
#: 信封里携带租户的 CloudEvents 扩展属性（RFC-0009「信封」）。
EXTENSION_TENANT: Final = "xihantenant"

#: 回调 RPC ``Host.Invoke``（向量 ``plugin/callback-status.json`` 的 ``rpc``）。
CALLBACK_INVOKE: Final = "invoke"
#: 回调 RPC ``Host.Publish``。
CALLBACK_PUBLISH: Final = "publish"
#: 没有可用回调端点时 ``plugins.host.unavailable`` 的 ``reason``（handshake.md §4）。
REASON_NO_CALLBACK_ENDPOINT: Final = "no supported callback endpoint"
#: ``Host.Publish`` 的错误所在的阶段（invocation.md §5）。
PHASE_PUBLISH: Final = "publish"
#: 客户端已关闭之后的回调在 ``reason`` 里写的说明（invocation.md §5，ADR-0328）：状态名按 ``Unavailable``。
REASON_CLIENT_CLOSED: Final = "the host client is closed"

# gRPC 状态码 0–16 的 PascalCase 名字（invocation.md §3），下标即状态码；各语言的 gRPC 库数值相同
_STATUS_NAMES: Final = (
    "OK",
    "Cancelled",
    "Unknown",
    "InvalidArgument",
    "DeadlineExceeded",
    "NotFound",
    "AlreadyExists",
    "PermissionDenied",
    "ResourceExhausted",
    "FailedPrecondition",
    "Aborted",
    "OutOfRange",
    "Unimplemented",
    "Internal",
    "Unavailable",
    "DataLoss",
    "Unauthenticated",
)
# 本地取消的调用没有宿主给的状态消息，reason 的说明写这句
_LOCALLY_CANCELLED: Final = "the call was cancelled"
# 映射表里单独用到的三个状态码
_CANCELLED: Final[int] = grpc.StatusCode.CANCELLED.value[0]
_DEADLINE_EXCEEDED: Final[int] = grpc.StatusCode.DEADLINE_EXCEEDED.value[0]
_UNAVAILABLE: Final[int] = grpc.StatusCode.UNAVAILABLE.value[0]

# 运行时把宿主客户端放进每次调用与投递的上下文；只在本包里写入
HOST_ITEM: Final = ItemKey["Host"]("xihan.plugin.host")


def find_host(ctx: Context) -> Host | None:
    """当前调用的宿主客户端；上下文不是插件运行时建的（例如测试里直接调用处理器）时为 ``None``。

    运行时在每次 ``Invoke`` 与 ``HandleEvent`` 的上下文里都放入它：宿主没有给出可用的回调端点时它照样在，每次回调都以
    ``plugins.host.unavailable`` 失败（handshake.md §4）。在运行时之外也要能跑的处理器用它，自己决定没有宿主时怎么办；
    一定在运行时里的用 ``host_of``。
    """
    return ctx.find_item(HOST_ITEM)


def host_of(ctx: Context) -> Host:
    """当前调用的宿主客户端，与 ``find_host`` 取的是同一个；上下文不是插件运行时建的时抛出 ``FrameworkError``。"""
    host = find_host(ctx)
    if host is None:
        raise FrameworkError(
            ERROR_CODE_INVALID_ARGUMENT,
            "The context carries no host client: only calls and deliveries of the plugin runtime have one.",
        )
    return host


class Host:
    """宿主回调服务的客户端（invocation.md §5）。

    回调端点按 handshake.md §4 选（``XIHAN_PLUGIN_HOST_ADDRESS`` 里第一个 grpcio 能连的项），之后不换；没有可用端点时不发
    请求，每次回调都以 ``PLUGINS_UNAVAILABLE_001`` + ``plugins.host.unavailable`` 失败。发出之前按宿主的顺序在本地核对，
    与宿主给出同样的错误：所需权限既在清单 ``permissions`` 里、也在 ``Configure`` 授予的权限里，否则
    ``plugins.callback.denied``；上下文的租户必须是插件此刻正在处理的某个调用或投递的租户，否则
    ``plugins.callback.tenant_denied``。每次调用都在元数据 ``xihan-plugin-session`` 里带会话令牌，宿主以插件身份执行。
    运行时收尾时关闭客户端（``close``），之后的回调不发请求、不重新连接，结果是 ``plugins.host.unavailable``。
    """

    __slots__ = (
        "_channel",
        "_closed",
        "_declared",
        "_endpoint",
        "_granted",
        "_plugin_id",
        "_session",
        "_stub",
        "_tenants",
    )

    def __init__(
        self,
        endpoint: Endpoint | None,
        session: str,
        plugin_id: str,
        tenants: TenantLedger,
        *,
        permissions: Collection[str] = (),
    ) -> None:
        """为回调端点创建客户端；``endpoint`` 为 ``None`` 或 grpcio 拨不了时没有可用端点。连接是惰性的，第一次调用时才建立。

        ``permissions`` 是清单声明的权限；授予的权限由运行时在 ``Configure`` 时交给它，之前一律未授予。
        """
        target = None if endpoint is None else endpoint.grpc_target
        self._endpoint = None if target is None else endpoint
        self._session = session
        self._plugin_id = plugin_id
        self._tenants = tenants
        self._declared = frozenset(permissions)
        self._granted: frozenset[str] = frozenset()
        self._channel = None if target is None else grpc.aio.insecure_channel(target)
        self._stub = None if self._channel is None else pb_grpc.HostStub(self._channel)
        self._closed = False

    @property
    def endpoint(self) -> Endpoint | None:
        """选中的回调端点；没有可用端点时为 ``None``。"""
        return self._endpoint

    async def invoke(self, ctx: Context, id: str, payload: bytes | str = b"") -> Response:
        """以插件身份经宿主的完整管道调用一个操作（需要权限 ``capability:kernel.pipeline``）。

        ``payload`` 是请求的 JSON 文本，空等价于 ``{}``。``bytes`` 原样发出，由宿主核对；``str`` 在发出之前严格编码成 UTF-8
        （invocation.md §5，ADR-0312）：Python 的 ``str`` 以码点存放，里面的代理项码点（成对的两个也一样）编码不成，即不发
        请求，结果为 ``KERNEL_VALIDATION_001`` + ``kernel.operation.invalid_payload``（``id`` 为操作 ID，``reason`` 为 ``$``），
        与宿主读到不是 JSON 的载荷相同；这一项排在端点、权限、租户、截止时间与取消的核对之后。

        元数据取自 ``ctx``：当前 Span 的 traceparent、截止时间与已登记的 Baggage（``to_metadata``），截止时间同时是 gRPC
        调用的截止时间；``ctx`` 的取消信号触发时取消这次 gRPC 调用。gRPC 失败按 invocation.md §5 的表映射为原语层错误，
        宿主写来的 ``Status`` 按 §3 的读入规则处理。客户端已关闭时不发请求，结果是 ``plugins.host.unavailable``，排在载荷的
        编码之后（ADR-0328）。
        """
        refused = self._refuse(ctx, CALLBACK_INVOKE)
        if refused is not None:
            return Response(fail(refused))
        if isinstance(payload, str):
            # 宿主在管道的 Validate 阶段才解码载荷：权限、租户与截止时间的错误先于它，所以排在本地核对之后
            try:
                payload = payload.encode("utf-8")
            except UnicodeEncodeError:
                return Response(
                    fail(
                        Error.validation(ERROR_CODE_INVALID_PAYLOAD, MESSAGE_INVALID_PAYLOAD, {"id": id, "reason": "$"})
                    )
                )
        if self._closed:
            return Response(fail(self._closed_error(CALLBACK_INVOKE, id)))
        assert self._stub is not None
        request = pb.InvokeRequest(operation=id, payload=payload, metadata=to_metadata(ctx))
        try:
            response = await self._call(ctx, self._stub.Invoke, request)
        except grpc.aio.AioRpcError as error:
            return Response(fail(self._rpc_failure(ctx, CALLBACK_INVOKE, error, id)))
        except asyncio.CancelledError:
            if _being_cancelled():
                raise
            return Response(fail(self._cancelled(ctx, CALLBACK_INVOKE, id)))
        outcome = from_status(
            response.status if response.HasField("status") else None, plugin_id=self._plugin_id, phase=id
        )
        return Response(outcome, response.payload if outcome.is_ok else None)

    async def publish(self, ctx: Context, envelopes: Sequence[Envelope]) -> list[Result[Unit]]:
        """经宿主的消息代理发布一批消息（需要权限 ``capability:messaging.queue``），逐项结果与 ``envelopes`` 一一对应。

        空集合不调用宿主、返回空列表。本地核对（端点、权限、上下文的租户）与 RPC 失败都是整批的结果，每一项同一个错误；
        信封的 ``xihantenant`` 不在本地核对，由宿主逐项判定（invocation.md §5）。宿主返回的结果数与信封数不同时每一项都是
        ``plugins.process.failed``。客户端已关闭时不发请求，每一项都是 ``plugins.host.unavailable``（ADR-0328）；信封的
        ``data`` 是字节，没有要编码的一步。
        """
        if not envelopes:
            return []
        refused = self._refuse(ctx, CALLBACK_PUBLISH)
        if refused is not None:
            return [fail(refused) for _ in envelopes]
        if self._closed:
            closed = self._closed_error(CALLBACK_PUBLISH, PHASE_PUBLISH)
            return [fail(closed) for _ in envelopes]
        assert self._stub is not None
        request = pb.PublishRequest(
            envelopes=[to_proto_envelope(envelope) for envelope in envelopes], metadata=to_metadata(ctx)
        )
        try:
            response = await self._call(ctx, self._stub.Publish, request)
        except grpc.aio.AioRpcError as error:
            failure = self._rpc_failure(ctx, CALLBACK_PUBLISH, error, PHASE_PUBLISH)
            return [fail(failure) for _ in envelopes]
        except asyncio.CancelledError:
            if _being_cancelled():
                raise
            cancelled = self._cancelled(ctx, CALLBACK_PUBLISH, PHASE_PUBLISH)
            return [fail(cancelled) for _ in envelopes]
        if len(response.results) != len(envelopes):
            mismatch = failed(
                self._plugin_id,
                PHASE_PUBLISH,
                f"the host returned {len(response.results)} results for {len(envelopes)} envelopes",
            )
            return [fail(mismatch) for _ in envelopes]
        return [from_status(status, plugin_id=self._plugin_id, phase=PHASE_PUBLISH) for status in response.results]

    async def close(self) -> None:
        """断开与宿主的连接；之后的回调不发请求、不重新连接，以 ``plugins.host.unavailable`` 失败（invocation.md §5）。

        可以调用多次。进行中的回调被切断，结果按映射表的 ``Cancelled`` 一行。
        """
        # 先记下已关闭再关通道：关闭途中开始的回调看到标记，不会在关掉的通道上发出（grpc.aio 那时抛 UsageError）
        self._closed = True
        if self._channel is not None:
            await self._channel.close()

    def _grant(self, permissions: Collection[str]) -> None:
        """记下 ``Configure`` 授予的权限（运行时调用）。"""
        self._granted = frozenset(permissions)

    def _refuse(self, ctx: Context, rpc: str) -> Error | None:
        """发出之前的本地核对，不合时给出错误、不发请求；全部通过时为 ``None``。

        依次是（invocation.md §5）：① 有没有可用端点（handshake.md §4）；② 权限；③ 上下文的租户；④ 上下文的截止时间已过
        或已被取消，结果与映射表相应的一行相同。
        """
        if self._stub is None:
            return Error.unavailable(
                ERROR_CODE_UNAVAILABLE,
                MESSAGE_HOST_UNAVAILABLE,
                {"id": self._plugin_id, "reason": REASON_NO_CALLBACK_ENDPOINT},
            )
        permission = callback_permission(rpc)
        if permission not in self._declared or permission not in self._granted:
            return Error.denied(
                ERROR_CODE_CALLBACK_DENIED, MESSAGE_CALLBACK_DENIED, {"id": self._plugin_id, "permission": permission}
            )
        tenant = ctx.tenant
        if tenant is not None and not self._tenants.admits(tenant):
            return Error.denied(
                ERROR_CODE_CALLBACK_DENIED, MESSAGE_CALLBACK_TENANT_DENIED, {"id": self._plugin_id, "tenant": tenant}
            )
        # 第 ④ 步：截止时间已过或已被取消的调用也不发出，结果取映射表里相应的一行（截止时间已过按 DeadlineExceeded，
        # 已被取消按「Cancelled，取消信号已触发」），与发出去之后 gRPC 报回来的相同；两者都成立时按截止时间
        if ctx.is_expired():
            return map_callback_status(
                rpc, _DEADLINE_EXCEEDED, "", ctx.is_cancelled, self._plugin_id, callback_phase(rpc, "")
            )
        if ctx.is_cancelled:
            return map_callback_status(
                rpc, _CANCELLED, _LOCALLY_CANCELLED, True, self._plugin_id, callback_phase(rpc, "")
            )
        return None

    async def _call[Req, Res](
        self, ctx: Context, method: grpc.aio.UnaryUnaryMultiCallable[Req, Res], request: Req
    ) -> Res:
        """带会话令牌与截止时间发出一元调用；调用方取消时取消这次调用，截止时间到了交给 gRPC 自己的截止时间。"""
        remaining = ctx.remaining_time()
        call = method(
            request,
            metadata=grpc.aio.Metadata((SESSION_METADATA_KEY, self._session)),
            timeout=None if remaining is None else remaining.total_seconds(),
        )
        loop = asyncio.get_running_loop()

        def cancel() -> None:
            # 管道里（Context.run）派生的取消信号到截止时间也会触发：这时不在本地取消，gRPC 带着同一个截止时间，由 grpcio
            # 以 DEADLINE_EXCEEDED 结束；抢先取消会按「Cancelled，取消信号已触发」报成取消（invocation.md §5）。
            # run 只在按时钟确认已到期后才因截止时间触发信号，这里按同一个时钟判断，两边一致
            if ctx.is_expired():
                return
            # 信号可能在别的线程触发：经事件循环取消；循环已关闭说明调用已经不在了
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(call.cancel)

        unsubscribe = ctx.cancellation.on_cancel(cancel)
        try:
            return await call
        finally:
            unsubscribe()

    def _closed_error(self, rpc: str, operation: str) -> Error:
        """客户端已关闭之后的回调：与映射表「``Unavailable``；连接失败」那一行相同（invocation.md §5，ADR-0328）。"""
        return map_callback_status(rpc, _UNAVAILABLE, REASON_CLIENT_CLOSED, False, self._plugin_id, operation)

    def _rpc_failure(self, ctx: Context, rpc: str, error: grpc.aio.AioRpcError, operation: str) -> Error:
        """把一次失败的 RPC 按 invocation.md §5 的表映射为原语层错误；连接失败 grpcio 报的就是 ``Unavailable``。"""
        code: int = error.code().value[0]
        return map_callback_status(rpc, code, error.details() or "", ctx.is_cancelled, self._plugin_id, operation)

    def _cancelled(self, ctx: Context, rpc: str, operation: str) -> Error:
        """调用在本地被取消（``call.cancel``），按 ``Cancelled`` 映射：上下文的信号触发了即取消，否则是连接被切断。"""
        return map_callback_status(rpc, _CANCELLED, _LOCALLY_CANCELLED, ctx.is_cancelled, self._plugin_id, operation)


def callback_permission(rpc: str) -> str:
    """回调 RPC 所需的权限（invocation.md §5）：``Host.Invoke`` 为 ``capability:kernel.pipeline``，``Host.Publish`` 为
    ``capability:messaging.queue``。"""
    return PERMISSION_INVOKE if rpc == CALLBACK_INVOKE else PERMISSION_PUBLISH


def callback_phase(rpc: str, operation: str) -> str:
    """回调失败时 ``plugins.process.failed`` 的 ``phase``（invocation.md §3、§5）：``Host.Invoke`` 是操作 ID，``Host.Publish``
    是 ``publish``。"""
    return PHASE_PUBLISH if rpc == CALLBACK_PUBLISH else operation


def status_name(code: int) -> str:
    """gRPC 状态码的 PascalCase 名字（invocation.md §3，如 ``InvalidArgument``）；0–16 之外的写成十进制数。"""
    return _STATUS_NAMES[code] if 0 <= code < len(_STATUS_NAMES) else str(code)


def status_reason(code: int, message: str) -> str:
    """``reason`` 里的 ``<状态名>: <说明>``（invocation.md §3）：说明原样取状态的消息，可以为空。"""
    return f"{status_name(code)}: {message}"


def map_callback_status(
    rpc: str, code: int, message: str, context_cancelled: bool, plugin_id: str, operation: str
) -> Error:
    """按 invocation.md §5 的表把插件一侧回调的 gRPC 失败映射成错误（向量 ``plugin/callback-status.json``）。

    ``rpc`` 是 ``invoke`` 或 ``publish``，``code`` 与 ``message`` 是 gRPC 状态码的数值与消息，``context_cancelled`` 是回调
    所在的调用上下文的取消信号是否已触发，``operation`` 是 ``Host.Invoke`` 的操作 ID（``Host.Publish`` 不用）。
    """
    if code == grpc.StatusCode.UNAUTHENTICATED.value[0]:
        return Error.denied(ERROR_CODE_CALLBACK_DENIED, MESSAGE_SESSION_INVALID)
    if code == grpc.StatusCode.PERMISSION_DENIED.value[0]:
        return Error.denied(
            ERROR_CODE_CALLBACK_DENIED,
            MESSAGE_CALLBACK_DENIED,
            {"id": plugin_id, "permission": callback_permission(rpc)},
        )
    if code == grpc.StatusCode.DEADLINE_EXCEEDED.value[0]:
        return deadline_exceeded_error()
    cancelled = code == grpc.StatusCode.CANCELLED.value[0]
    # Cancelled 只在调用方确实取消时算取消，超时的归因交给插件自己管道里的截止时间步骤（RFC-0003）；
    # 调用方没有取消的 Cancelled 是连接被宿主或网络切断，与 Unavailable 同样处理
    if cancelled and context_cancelled:
        return cancelled_error()
    if cancelled or code == grpc.StatusCode.UNAVAILABLE.value[0]:
        return Error.unavailable(
            ERROR_CODE_UNAVAILABLE, MESSAGE_HOST_UNAVAILABLE, {"id": plugin_id, "reason": status_reason(code, message)}
        )
    # Unimplemented 与 InvalidArgument 只对 Host.Invoke 有操作层面的含义；Host.Publish 的这两种落到最后一项
    if rpc == CALLBACK_INVOKE and code == grpc.StatusCode.UNIMPLEMENTED.value[0]:
        return Error.not_found(ERROR_CODE_NOT_FOUND, MESSAGE_NOT_FOUND, {"id": operation})
    if rpc == CALLBACK_INVOKE and code == grpc.StatusCode.INVALID_ARGUMENT.value[0]:
        return Error.validation(
            ERROR_CODE_INVALID_PAYLOAD, MESSAGE_INVALID_PAYLOAD, {"id": operation, "reason": message}
        )
    return failed(plugin_id, callback_phase(rpc, operation), status_reason(code, message))


def _being_cancelled() -> bool:
    """当前任务本身是否正被取消；是的话 ``CancelledError`` 照常传播，不变成结果。"""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
