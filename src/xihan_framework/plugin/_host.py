"""宿主回调服务 ``Host`` 的客户端（``spec/plugin/invocation.md`` §5）。"""

# grpc 的类型存根把服务端上下文、方法处理器与多调用对象写成泛型，运行时它们不能下标：注解推迟求值
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
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
    MESSAGE_FAILED,
    MESSAGE_SESSION_INVALID,
)
from xihan_framework.plugin._model import Envelope
from xihan_framework.plugin._protocol import SESSION_METADATA_KEY, Endpoint
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.plugin.v1 import plugin_pb2_grpc as pb_grpc
from xihan_framework.primitives import Error, Result, Unit, fail

#: ``Host.Invoke`` 需要的权限。
PERMISSION_INVOKE: Final = "capability:kernel.pipeline"
#: ``Host.Publish`` 需要的权限。
PERMISSION_PUBLISH: Final = "capability:messaging.queue"
#: 信封里携带租户的 CloudEvents 扩展属性（RFC-0009「信封」）。
EXTENSION_TENANT: Final = "xihantenant"

# 运行时把宿主客户端放进每次调用与投递的上下文；只在本包里写入
HOST_ITEM: Final = ItemKey["Host"]("xihan.plugin.host")


def host_of(ctx: Context) -> Host | None:
    """当前调用的宿主客户端；运行时在每次 ``Invoke`` 与 ``HandleEvent`` 的上下文里放入它，宿主没有回调端点时为 ``None``。"""
    return ctx.find_item(HOST_ITEM)


class Host:
    """宿主回调服务的客户端：每次调用都在元数据 ``xihan-plugin-session`` 里带会话令牌，宿主以插件身份执行。

    回调与发布能带的租户以宿主随调用交给插件的租户为限（invocation.md §5，ADR-0195）：上下文或信封的租户不是某个
    进行中的调用的租户时，不发出调用，直接以 ``PLUGINS_DENIED_002`` + ``plugins.callback.tenant_denied`` 失败。权限、
    令牌与操作授权由宿主判定。grpcio 不能拨号 Windows 命名管道，回调端点是 ``npipe`` 时每次回调都以
    ``PLUGINS_UNAVAILABLE_001`` 失败。
    """

    __slots__ = ("_channel", "_endpoint", "_plugin_id", "_session", "_stub", "_tenants")

    def __init__(self, endpoint: Endpoint, session: str, plugin_id: str, tenants: TenantLedger) -> None:
        """为回调端点创建客户端；连接是惰性的，第一次调用时才建立。"""
        self._endpoint = endpoint
        self._session = session
        self._plugin_id = plugin_id
        self._tenants = tenants
        target = endpoint.grpc_target
        self._channel = None if target is None else grpc.aio.insecure_channel(target)
        self._stub = None if self._channel is None else pb_grpc.HostStub(self._channel)

    @property
    def endpoint(self) -> Endpoint:
        """宿主回调端点。"""
        return self._endpoint

    async def invoke(self, ctx: Context, id: str, payload: bytes = b"") -> Response:
        """以插件身份经宿主的完整管道调用一个操作（需要权限 ``capability:kernel.pipeline``）。

        上下文的追踪、截止时间与已登记的 Baggage 随调用传给宿主，截止时间同时是 gRPC 调用的截止时间；上下文的取消
        信号触发时取消这次 gRPC 调用。传输层失败按 invocation.md §3 的映射表映射为原语层错误。
        """
        refused = self._refuse(ctx, "Host.Invoke")
        if refused is not None:
            return Response(fail(refused))
        assert self._stub is not None
        request = pb.InvokeRequest(operation=id, payload=payload, metadata=to_metadata(ctx))
        try:
            response = await self._call(ctx, self._stub.Invoke, request)
        except grpc.aio.AioRpcError as error:
            return Response(fail(self._from_rpc_error(ctx, error, "Host.Invoke", PERMISSION_INVOKE, id)))
        except asyncio.CancelledError:
            if _being_cancelled():
                raise
            return Response(fail(self._cancelled(ctx, "Host.Invoke")))
        outcome = from_status(
            response.status if response.HasField("status") else None, plugin_id=self._plugin_id, phase="Host.Invoke"
        )
        return Response(outcome, response.payload if outcome.is_ok else None)

    async def publish(self, ctx: Context, envelopes: Sequence[Envelope]) -> list[Result[Unit]]:
        """经宿主的消息代理发布一批消息（需要权限 ``capability:messaging.queue``），逐项结果与 ``envelopes`` 一一对应。

        空集合不调用宿主。信封的 ``xihantenant`` 扩展逐项按租户规则判定，越界的那一项为 ``PLUGINS_DENIED_002``，其余照常
        发布；上下文本身的租户越界、截止时间已过、已取消或宿主不可达时整批失败。
        """
        if not envelopes:
            return []
        refused = self._refuse(ctx, "Host.Publish")
        if refused is not None:
            return [fail(refused) for _ in envelopes]
        assert self._stub is not None
        results: list[Result[Unit] | None] = [None] * len(envelopes)
        sent: list[int] = []
        for index, envelope in enumerate(envelopes):
            tenant = envelope.extensions.get(EXTENSION_TENANT)
            if tenant is not None and not self._tenants.admits(tenant):
                results[index] = fail(self._tenant_denied(tenant))
            else:
                sent.append(index)
        if sent:
            request = pb.PublishRequest(
                envelopes=[to_proto_envelope(envelopes[index]) for index in sent], metadata=to_metadata(ctx)
            )
            try:
                response = await self._call(ctx, self._stub.Publish, request)
            except grpc.aio.AioRpcError as error:
                failure: Result[Unit] = fail(self._from_rpc_error(ctx, error, "Host.Publish", PERMISSION_PUBLISH, None))
                for index in sent:
                    results[index] = failure
            except asyncio.CancelledError:
                if _being_cancelled():
                    raise
                cancelled: Result[Unit] = fail(self._cancelled(ctx, "Host.Publish"))
                for index in sent:
                    results[index] = cancelled
            else:
                for position, index in enumerate(sent):
                    if position < len(response.results):
                        results[index] = from_status(
                            response.results[position], plugin_id=self._plugin_id, phase="Host.Publish"
                        )
                    else:
                        results[index] = fail(
                            failed(self._plugin_id, "Host.Publish", "the host returned fewer results than envelopes")
                        )
        return [result for result in results if result is not None]

    async def close(self) -> None:
        """断开与宿主的连接。"""
        if self._channel is not None:
            await self._channel.close()

    def _refuse(self, ctx: Context, phase: str) -> Error | None:
        """发出调用之前的判定：租户越界、宿主端点不可用、截止时间已过或已取消时给出错误，否则为 ``None``。"""
        tenant = ctx.tenant
        if tenant is not None and not self._tenants.admits(tenant):
            return self._tenant_denied(tenant)
        if self._stub is None:
            return Error.unavailable(
                ERROR_CODE_UNAVAILABLE,
                MESSAGE_FAILED,
                {
                    "id": self._plugin_id,
                    "phase": phase,
                    "reason": f"grpcio cannot dial the {self._endpoint.network} endpoint '{self._endpoint.address}'",
                },
            )
        # 截止时间按时钟核对：不发出注定超时的调用
        return ctx.cancellation_error()

    async def _call[Req, Res](
        self, ctx: Context, method: grpc.aio.UnaryUnaryMultiCallable[Req, Res], request: Req
    ) -> Res:
        """带会话令牌与截止时间发出一元调用；上下文的取消信号触发时取消这次调用。"""
        remaining = ctx.remaining_time()
        call = method(
            request,
            metadata=grpc.aio.Metadata((SESSION_METADATA_KEY, self._session)),
            timeout=None if remaining is None else remaining.total_seconds(),
        )
        loop = asyncio.get_running_loop()

        def cancel() -> None:
            # 信号可能在别的线程触发：经事件循环取消；循环已关闭说明调用已经不在了
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(call.cancel)

        unsubscribe = ctx.cancellation.on_cancel(cancel)
        try:
            return await call
        finally:
            unsubscribe()

    def _cancelled(self, ctx: Context, phase: str) -> Error:
        """调用在本地被取消：上下文的信号触发了即 ``KERNEL_CANCELLED_001``，否则按连接被切断、宿主不可用处理。"""
        if ctx.is_cancelled:
            return cancelled_error()
        return Error.unavailable(
            ERROR_CODE_UNAVAILABLE, MESSAGE_FAILED, {"id": self._plugin_id, "phase": phase, "reason": "cancelled"}
        )

    def _from_rpc_error(
        self, ctx: Context, error: grpc.aio.AioRpcError, phase: str, permission: str, operation: str | None
    ) -> Error:
        """按 invocation.md §3 的映射表把 gRPC 状态映射为原语层错误，从插件这一侧看宿主。

        ``Unauthenticated`` 与 ``PermissionDenied`` 是宿主拒绝回调（handshake.md §4、invocation.md §5），都是
        ``PLUGINS_DENIED_002``；``Unimplemented`` 与 ``InvalidArgument`` 只在 ``Host.Invoke`` 映射为操作未登记与载荷不合法。
        """
        code = error.code()
        reason = error.details() or code.name
        if code is grpc.StatusCode.UNAUTHENTICATED:
            return Error.denied(ERROR_CODE_CALLBACK_DENIED, MESSAGE_SESSION_INVALID)
        if code is grpc.StatusCode.PERMISSION_DENIED:
            return Error.denied(
                ERROR_CODE_CALLBACK_DENIED, MESSAGE_CALLBACK_DENIED, {"id": self._plugin_id, "permission": permission}
            )
        if code is grpc.StatusCode.DEADLINE_EXCEEDED:
            return deadline_exceeded_error()
        if code is grpc.StatusCode.CANCELLED:
            if ctx.is_cancelled:
                return cancelled_error()
            return Error.unavailable(
                ERROR_CODE_UNAVAILABLE, MESSAGE_FAILED, {"id": self._plugin_id, "phase": phase, "reason": reason}
            )
        if code is grpc.StatusCode.UNAVAILABLE:
            return Error.unavailable(
                ERROR_CODE_UNAVAILABLE, MESSAGE_FAILED, {"id": self._plugin_id, "phase": phase, "reason": reason}
            )
        if operation is not None and code is grpc.StatusCode.UNIMPLEMENTED:
            return Error.not_found(ERROR_CODE_NOT_FOUND, MESSAGE_NOT_FOUND, {"id": operation})
        if operation is not None and code is grpc.StatusCode.INVALID_ARGUMENT:
            return Error.validation(
                ERROR_CODE_INVALID_PAYLOAD, MESSAGE_INVALID_PAYLOAD, {"id": operation, "reason": reason}
            )
        return failed(self._plugin_id, phase, f"gRPC {code.name}: {reason}")

    def _tenant_denied(self, tenant: str) -> Error:
        """租户不是宿主随调用交给插件的租户。"""
        return Error.denied(
            ERROR_CODE_CALLBACK_DENIED, MESSAGE_CALLBACK_TENANT_DENIED, {"id": self._plugin_id, "tenant": tenant}
        )


def _being_cancelled() -> bool:
    """当前任务本身是否正被取消；是的话 ``CancelledError`` 照常传播，不变成结果。"""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
