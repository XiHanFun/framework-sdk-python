"""进程外插件运行时：启动与握手、生命周期、调用与事件投递（RFC-0017，``spec/plugin/``）。"""

# grpc 的类型存根把服务端上下文、方法处理器与多调用对象写成泛型，运行时它们不能下标：注解推迟求值
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import math
import os
import shutil
import signal
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Final

import grpc

from xihan_framework import manifest as manifests
from xihan_framework._json import JsonTextError, JsonValue, parse
from xihan_framework.invocation import Cancellation, Context
from xihan_framework.operation import (
    ERROR_CODE_UNEXPECTED as ERROR_CODE_INVOCATION_UNEXPECTED,
)
from xihan_framework.operation import (
    MESSAGE_UNHANDLED_EXCEPTION,
    Invoker,
    Registry,
    Request,
    Response,
)
from xihan_framework.pipeline import Builder
from xihan_framework.plugin._convert import (
    failed,
    from_proto_envelope,
    restore_context,
    to_proto_health,
    to_status,
)
from xihan_framework.plugin._host import HOST_ITEM, Host
from xihan_framework.plugin._lifecycle import SELF_REPORTED_STATES, SERVING_STATES, Lifecycle, TenantLedger
from xihan_framework.plugin._log import JsonLogHandler
from xihan_framework.plugin._messages import ERROR_CODE_UNAVAILABLE, MESSAGE_UNAVAILABLE
from xihan_framework.plugin._model import (
    Binding,
    Configuration,
    Envelope,
    EventHandler,
    HealthItem,
    Hooks,
    SubscriptionHandler,
    SubscriptionMode,
)
from xihan_framework.plugin._protocol import (
    ENV_HOST_ADDRESS,
    ENV_INSTANCE,
    ENV_LOCALE,
    ENV_PRIMITIVES,
    ENV_PROTOCOL_VERSIONS,
    ENV_SESSION,
    LANGUAGE,
    SESSION_METADATA_KEY,
    SUPPORTED_PROTOCOL_VERSIONS,
    Endpoint,
    Network,
    negotiate_version,
    parse_endpoint,
)
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.plugin.v1 import plugin_pb2_grpc as pb_grpc
from xihan_framework.primitives import (
    DEFAULT_LOCALE,
    ERROR_CODE_INVALID_ARGUMENT,
    SDK_NAME,
    SDK_VERSION,
    Catalog,
    Error,
    FrameworkError,
    LifecycleState,
    Result,
    Unit,
    fail,
    success,
)

_logger = logging.getLogger("xihan_framework.plugin")

# WatchHealth 在没有变化通知时也按这个间隔重新计算一次健康：健康回调的结果可能在没有通知的情况下变化
_HEALTH_POLL_SECONDS: Final = 1.0
# Stop 应答之后关闭 gRPC 服务端时给在途 RPC（含 Stop 本身的应答）的时间
_SERVER_STOP_GRACE_SECONDS: Final = 1.0
# 宿主没有给宽限（Stop 的 grace_seconds 为 0，或 stdin 关闭、SIGTERM）时等待在途调用的时间
_DEFAULT_STOP_GRACE: Final = timedelta(seconds=5)


class Plugin:
    """一个进程外插件：清单、操作登记表、插件内的管道、事件订阅、消息目录与生命周期回调。

    构造时核对清单与声明，不合即抛出 ``FrameworkError``（配置错误）：清单读不出、不是 ``isolation: out-of-process``
    的插件、写了 ``contributes.pipeline``（只有 Wasm 插件能贡献中间件，RFC-0017「范围」）、登记的操作不在清单的
    ``contributes.operations`` 里、同一（主题，类型）对订阅了两次、消息目录没有 ``en-US``，或要求的网络类型 grpcio
    不能监听。操作经 SDK 的 ``operation.Invoker`` 与完整管道执行（RFC-0011），调用上下文按 invocation.md §2 从
    ``InvocationMetadata`` 还原。
    """

    __slots__ = (
        "_catalogs",
        "_hooks",
        "_invoker",
        "_manifest",
        "_manifest_text",
        "_network",
        "_stop_grace",
        "_subscriptions",
    )

    def __init__(
        self,
        manifest: str | bytes | None = None,
        *,
        operations: Registry | None = None,
        pipeline: Builder | None = None,
        subscriptions: Sequence[SubscriptionHandler] = (),
        catalogs: Mapping[str, Catalog] | None = None,
        hooks: Hooks | None = None,
        network: Network | None = None,
        stop_grace: timedelta = _DEFAULT_STOP_GRACE,
    ) -> None:
        """创建插件。

        ``manifest`` 是 ``xihan.manifest.json`` 的文本，握手时原样交给宿主，所以必须与插件目录里的文件一致；省略时读
        当前工作目录（宿主把它设为插件目录）里的文件。``catalogs`` 是目录名到消息目录的映射，随握手交给宿主翻译插件的
        错误。``network`` 是监听的网络类型，省略时为 ``unix``；``stop_grace`` 是宿主没有给宽限时等待在途调用的时间。
        """
        text = _manifest_text(manifest)
        read = manifests.read(text)
        if read.error is not None:
            raise FrameworkError.of(read.error)
        parsed = read.value_or_raise()
        if parsed.kind is not manifests.Kind.PLUGIN or parsed.isolation is not manifests.Isolation.OUT_OF_PROCESS:
            raise _configuration_error(
                f"The manifest '{parsed.id}' must describe a plugin with isolation out-of-process."
            )
        if parsed.contributes.pipeline:
            raise _configuration_error(
                f"The plugin '{parsed.id}' must not declare 'contributes.pipeline': only wasm plugins can contribute middleware."
            )
        registry = Registry() if operations is None else operations
        for descriptor in registry.descriptors():
            if descriptor.id not in parsed.operations:
                raise _configuration_error(
                    f"The operation '{descriptor.id}' is not declared in contributes.operations of '{parsed.id}'."
                )
        # 宿主拒绝同一（主题，类型）对以两种方式各声明一次（handshake.md §3 第 5 条），在监听之前就拒绝
        declared: set[tuple[str, str | None]] = set()
        for handler in subscriptions:
            key = (handler.subscription.topic, handler.subscription.message_type)
            if key in declared:
                raise _configuration_error(
                    f"The subscription '{key[0]}' with the message type '{key[1]}' is declared more than once by '{parsed.id}'."
                )
            declared.add(key)
        frozen_catalogs = dict(catalogs or {})
        for name, catalog in frozen_catalogs.items():
            if not name:
                raise _configuration_error(f"The message catalogs of '{parsed.id}' need non-empty names.")
            if DEFAULT_LOCALE not in catalog.locales:
                raise _configuration_error(
                    f"The message catalog '{name}' of '{parsed.id}' has no {DEFAULT_LOCALE} templates."
                )
        if network is Network.NPIPE:
            raise _configuration_error("grpcio cannot listen on Windows named pipes; use 'unix' or 'tcp'.")
        if stop_grace <= timedelta(0):
            raise _configuration_error("The stop grace period must be positive.")
        self._manifest = parsed
        self._manifest_text = text
        self._invoker = Invoker(registry, pipeline)
        self._subscriptions = tuple(subscriptions)
        self._catalogs = frozen_catalogs
        self._hooks = Hooks() if hooks is None else hooks
        self._network = Network.UNIX if network is None else network
        self._stop_grace = stop_grace

    @property
    def manifest(self) -> manifests.Manifest:
        """读好的清单。"""
        return self._manifest

    @property
    def manifest_text(self) -> str:
        """握手时交给宿主的清单文本。"""
        return self._manifest_text

    @property
    def invoker(self) -> Invoker:
        """执行插件操作的调用器（带内置步骤与插件内的管道）。"""
        return self._invoker

    @property
    def subscriptions(self) -> tuple[SubscriptionHandler, ...]:
        """事件订阅与处理器，按声明顺序。"""
        return self._subscriptions

    @property
    def catalogs(self) -> Mapping[str, Catalog]:
        """随握手交给宿主的消息目录。"""
        return self._catalogs

    @property
    def hooks(self) -> Hooks:
        """生命周期回调。"""
        return self._hooks

    @property
    def network(self) -> Network:
        """监听的网络类型。"""
        return self._network

    @property
    def stop_grace(self) -> timedelta:
        """宿主没有给宽限时等待在途调用的时间。"""
        return self._stop_grace

    async def serve(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> None:
        """以进程外插件的身份运行，直到 ``Stop`` 完成、stdin 关闭或收到 SIGTERM。

        读 handshake.md §1 的环境变量（``environ`` 省略时为 ``os.environ``），按 §4 监听本机端点，向 ``stdout``（省略时为
        ``sys.stdout`` 的字节流）写一行握手行，之后不再写它；``stdin``（省略时为 ``sys.stdin`` 的字节流）关闭即收尾退出。
        缺少会话令牌是配置错误，抛出 ``FrameworkError``。
        """
        runtime = _Runtime(self, os.environ if environ is None else environ)
        await runtime.serve(_stdin() if stdin is None else stdin, sys.stdout.buffer if stdout is None else stdout)

    def run(self) -> int:
        """进程入口：把根日志接到 stderr 的 JSON 行（invocation.md §6），运行到停止，返回进程退出码。

        正常停止返回 0；配置错误与未处理的异常记一条 ``critical`` 日志后返回 1。根日志已有处理器时不改动它。
        """
        root = logging.getLogger()
        if not root.handlers:
            root.addHandler(JsonLogHandler())
            root.setLevel(logging.INFO)
        try:
            asyncio.run(self.serve())
        except FrameworkError as error:
            _logger.critical("The plugin '%s' could not run: %s", self._manifest.id, error)
            return 1
        except Exception:
            _logger.critical("The plugin '%s' stopped on an unhandled exception.", self._manifest.id, exc_info=True)
            return 1
        return 0


class _Runtime:
    """一次 ``serve`` 的状态：生命周期、在途调用、宿主客户端与停止信号。"""

    def __init__(self, plugin: Plugin, environ: Mapping[str, str]) -> None:
        """读环境变量；缺少会话令牌时抛出 ``FrameworkError``。"""
        self.plugin = plugin
        self.id = plugin.manifest.id
        self.lifecycle = Lifecycle()
        self.tenants = TenantLedger()
        self.session = environ.get(ENV_SESSION, "")
        if not self.session:
            raise _configuration_error(
                f"The environment variable '{ENV_SESSION}' is required: the host starts plugins with it."
            )
        self.instance = environ.get(ENV_INSTANCE, "")
        self.locale = environ.get(ENV_LOCALE, "")
        self.host: Host | None = None
        self._inflight: dict[asyncio.Task[Any], Cancellation] = {}
        self._configured = False
        self._drain_task: asyncio.Task[Result[Unit]] | None = None
        self._draining = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._undeclared_items: set[str] = set()
        self._check_environment(environ)

    def _check_environment(self, environ: Mapping[str, str]) -> None:
        """核对其余环境变量：宿主回调端点建成客户端，版本不符只记告警（握手时宿主会拒绝）。"""
        address = environ.get(ENV_HOST_ADDRESS, "")
        if address:
            endpoint = parse_endpoint(address)
            if endpoint is None:
                _logger.warning(
                    "%s is not '<network> <address>': %r; host callbacks are unavailable.", ENV_HOST_ADDRESS, address
                )
            else:
                self.host = Host(endpoint, self.session, self.id, self.tenants)
                if endpoint.grpc_target is None:
                    _logger.warning(
                        "grpcio cannot dial the host endpoint '%s'; host callbacks are unavailable.", endpoint
                    )
        versions = environ.get(ENV_PROTOCOL_VERSIONS, "")
        try:
            offered = [int(part) for part in versions.split(",")] if versions else []
        except ValueError:
            offered = []
        if negotiate_version(offered) is None:
            _logger.warning(
                "The host offers protocol versions %r; this SDK supports %s.",
                versions,
                list(SUPPORTED_PROTOCOL_VERSIONS),
            )
        primitives = environ.get(ENV_PRIMITIVES, "")
        if primitives and primitives != str(self.plugin.manifest.primitives):
            _logger.warning(
                "The host's primitives version %s differs from the manifest's %s; the host will reject the handshake.",
                primitives,
                self.plugin.manifest.primitives,
            )

    async def serve(self, stdin: BinaryIO | None, stdout: BinaryIO) -> None:
        """监听、写握手行、服务到停止，最后关闭服务端并删除私有目录。"""
        server = grpc.aio.server(interceptors=(_SessionInterceptor(self.session),))
        pb_grpc.add_PluginServicer_to_server(_Service(self), server)
        endpoint, directory = _listen(server, self.plugin.network)
        try:
            await server.start()
            # 握手行是 stdout 的第一行也是唯一一行（handshake.md §2）；写字节流，Windows 上也只有 \n
            stdout.write((endpoint.handshake_line + "\n").encode("utf-8"))
            stdout.flush()
            _logger.info(
                "Listening on %s.",
                endpoint,
                extra={"plugin": self.id, "instance": self.instance, "locale": self.locale},
            )
            loop = asyncio.get_running_loop()

            def stdin_closed() -> None:
                loop.call_soon_threadsafe(self.request_shutdown, "stdin closed")

            _watch_stdin(stdin, stdin_closed)
            terminated = _watch_sigterm(loop, self.request_shutdown)
            try:
                await self._shutdown.wait()
            finally:
                if terminated:
                    loop.remove_signal_handler(signal.SIGTERM)
            await self.drain(self.plugin.stop_grace, "shutdown")
            await server.stop(_SERVER_STOP_GRACE_SECONDS)
            self.lifecycle.transition(LifecycleState.STOPPED)
            _logger.info("Stopped.", extra={"plugin": self.id})
        finally:
            await server.stop(None)
            if self.host is not None:
                await self.host.close()
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)

    def request_shutdown(self, reason: str) -> None:
        """停止信号（``Stop`` 应答之后、stdin 关闭、SIGTERM）：只生效一次；后两种立即通知在途调用收尾（lifecycle.md §5）。"""
        if self._shutdown.is_set():
            return
        _logger.info("Stopping: %s.", reason, extra={"plugin": self.id})
        if self._drain_task is None:
            # 宿主没有发 Stop：没有人等应答，在途调用的信号立即触发，再在宽限期内等它们收尾
            for cancellation in list(self._inflight.values()):
                _signal(cancellation)
        self._shutdown.set()

    @property
    def serving(self) -> bool:
        """是否接受调用与事件投递：``Start`` 成功之后、开始停止之前。"""
        return self.lifecycle.state in SERVING_STATES and not self._draining.is_set()

    @property
    def draining(self) -> bool:
        """是否已经开始停止。"""
        return self._draining.is_set()

    def status(self, result: Result[Any]) -> pb.Status:
        """把结果编码为协议的 ``Status``：失败的文案按插件的消息目录在宿主的默认区域里解析。"""
        return to_status(result, self.plugin.catalogs.values(), self.locale or DEFAULT_LOCALE)

    def unavailable(self) -> Error:
        """插件不接受调用时的错误：``PLUGINS_UNAVAILABLE_001``（lifecycle.md §2、§5）。"""
        state = self.lifecycle.state
        return Error.unavailable(ERROR_CODE_UNAVAILABLE, MESSAGE_UNAVAILABLE, {"id": self.id, "state": str(state)})

    def handshake(self, version: int) -> pb.HandshakeResponse:
        """组装握手应答：清单原文、操作声明（契约与 JSON Schema）、订阅与消息目录（handshake.md §3）。"""
        response = pb.HandshakeResponse(
            protocol_version=version,
            primitives=self.plugin.manifest.primitives,
            manifest=self.plugin.manifest_text,
            sdk=pb.SdkInfo(name=SDK_NAME, version=SDK_VERSION, language=LANGUAGE),
        )
        for descriptor in self.plugin.invoker.registry.descriptors():
            contract = descriptor.contract
            declaration = pb.OperationDeclaration(
                id=contract.id,
                stability=str(contract.stability),
                expose=list(contract.expose),
                permission=contract.permission or "",
                policy=contract.policy or "",
            )
            if descriptor.schema is not None:
                declaration.request_schema = descriptor.schema.request
                declaration.response_schema = descriptor.schema.response
            response.operations.append(declaration)
        for handler in self.plugin.subscriptions:
            subscription = handler.subscription
            response.subscriptions.append(
                pb.Subscription(
                    topic=subscription.topic,
                    message_type=subscription.message_type or "",
                    mode="" if subscription.mode is None else subscription.mode.value,
                )
            )
        for name, catalog in self.plugin.catalogs.items():
            encoded = pb.MessageCatalog(name=name)
            for locale in sorted(catalog.locales):
                encoded.locales.append(pb.LocaleTemplates(locale=locale, templates=dict(catalog.locales[locale])))
            response.catalogs.append(encoded)
        return response

    async def configure(self, request: pb.ConfigureRequest) -> Result[Unit]:
        """``Configure``：读配置节，交给配置回调；成功进入 ``Configured``，失败进入 ``Failed``（lifecycle.md §2）。"""
        section, problem = _section(request.configuration)
        if section is None:
            result: Result[Unit] = fail(failed(self.id, "Configure", problem))
        else:
            configuration = Configuration(
                section=section,
                granted_permissions=tuple(request.granted_permissions),
                bindings=tuple(Binding(binding.name, binding.type, binding.value) for binding in request.bindings),
                work_directory=request.work_directory,
            )
            hook = self.plugin.hooks.configure
            result = success()
            if hook is not None:
                # 配置回调可能已经占用了资源：从这里起停止时都要调用停止回调
                self._configured = True
                result = await self._hook("Configure", lambda: hook(configuration))
        self._settle(result, LifecycleState.CONFIGURED, "Configure")
        return result

    async def start(self) -> Result[Unit]:
        """``Start``：交给启动回调；成功后直接报 ``Ready``（lifecycle.md §1），失败进入 ``Failed``。"""
        hook = self.plugin.hooks.start
        result = success() if hook is None else await self._hook("Start", hook)
        if self._settle(result, LifecycleState.STARTED, "Start"):
            self.lifecycle.transition(LifecycleState.READY)
            _logger.info(
                "Started.",
                extra={
                    "plugin": self.id,
                    "operations": len(self.plugin.invoker.registry),
                    "subscriptions": len(self.plugin.subscriptions),
                },
            )
        return result

    def _settle(self, result: Result[Unit], success_state: LifecycleState, phase: str) -> bool:
        """按一步的结果迁移状态：成功到 ``success_state``，失败到 ``Failed`` 并记错误日志。"""
        if result.error is None:
            self.lifecycle.transition(success_state)
            return True
        self.lifecycle.transition(LifecycleState.FAILED, result.error)
        _logger.error("%s failed: %s", phase, result.error, extra={"plugin": self.id, "code": result.error.code})
        return False

    async def drain(self, grace: timedelta, reason: str) -> Result[Unit]:
        """进入 ``Draining``：拒绝新调用，在宽限期内等在途调用结束，再调用停止回调释放资源（lifecycle.md §5）。

        只执行一次；再次调用等同一次停止的结果。
        """
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain(grace, reason))
        return await asyncio.shield(self._drain_task)

    async def _drain(self, grace: timedelta, reason: str) -> Result[Unit]:
        """``drain`` 的实际过程。"""
        self.lifecycle.transition(LifecycleState.DRAINING)
        self._draining.set()
        # 状态可能迁移不了（还没 Start）：照样通知 WatchHealth 结束推送
        self.lifecycle.notify()
        _logger.info("Draining.", extra={"plugin": self.id, "reason": reason, "grace": grace.total_seconds()})
        if self._inflight:
            _, pending = await asyncio.wait(list(self._inflight), timeout=grace.total_seconds())
            if pending:
                _logger.warning(
                    "%d in-flight calls did not finish within the grace period.",
                    len(pending),
                    extra={"plugin": self.id},
                )
                for task in pending:
                    cancellation = self._inflight.get(task)
                    if cancellation is not None:
                        _signal(cancellation)
        hook = self.plugin.hooks.stop
        if hook is None or not self._configured:
            return success()
        return await self._hook("Stop", lambda: hook(reason, grace))

    async def _hook(self, phase: str, call: Callable[[], Awaitable[Result[Unit]]]) -> Result[Unit]:
        """执行一个生命周期回调：抛出的异常记进日志，以 ``PLUGINS_UNEXPECTED_001`` 交给宿主，不带异常文本。"""
        try:
            return await call()
        except Exception as exception:
            _logger.error(
                "The %s hook raised an unhandled exception.", phase, exc_info=exception, extra={"plugin": self.id}
            )
            inner = exception.to_error() if isinstance(exception, FrameworkError) else None
            return fail(failed(self.id, phase, f"unhandled {type(exception).__name__}", inner))

    def health(self) -> pb.HealthResponse:
        """当前健康：运行时的状态与健康回调报的健康项（只留清单声明了的）；回调报的状态按迁移表生效（lifecycle.md §1、§4）。"""
        hook = self.plugin.hooks.health
        items: list[HealthItem] = []
        if hook is not None:
            try:
                reported = hook()
            except Exception:
                _logger.error(
                    "The health hook raised an unhandled exception.", exc_info=True, extra={"plugin": self.id}
                )
                reported = None
            if reported is not None:
                declared = self.plugin.manifest.health
                for item in reported.items:
                    if item.name in declared:
                        items.append(item)
                    elif item.name not in self._undeclared_items:
                        # 宿主只认清单声明了的健康项；每个名字只告警一次，免得健康推送每秒一条
                        self._undeclared_items.add(item.name)
                        _logger.warning(
                            "The health item '%s' is not declared in contributes.health.",
                            item.name,
                            extra={"plugin": self.id},
                        )
                state = reported.state
                if state is not None and state in SELF_REPORTED_STATES and state is not self.lifecycle.state:
                    self.lifecycle.transition(state, reported.error)
        state, error = self.lifecycle.current()
        return to_proto_health(state, items, error, self.plugin.catalogs.values(), self.locale or DEFAULT_LOCALE)

    def context(self, metadata: pb.InvocationMetadata | None, remaining: float | None) -> tuple[Context, Cancellation]:
        """为一次调用或投递建上下文（invocation.md §2）：放入宿主客户端；返回的信号由运行时在调用方取消时触发。"""
        cancellation = Cancellation()
        deadline = None
        if remaining is not None and math.isfinite(remaining):
            deadline = datetime.now(UTC) + timedelta(seconds=max(remaining, 0.0))
        ctx = restore_context(metadata, cancellation=cancellation, deadline=deadline)
        if self.host is not None:
            ctx.set_item(HOST_ITEM, self.host)
        return ctx, cancellation

    async def track[T](self, ctx: Context, cancellation: Cancellation, call: Coroutine[Any, Any, T]) -> T:
        """作为在途调用执行 ``call``：期间记下宿主交来的租户，停止时等它结束。

        gRPC 在调用方取消或截止时间到了时取消处理 RPC 的任务；这里只触发信号，不取消执行调用的任务（RFC-0003 规则 1），
        处理器看信号自己停下，结果已经没有人收。
        """

        async def guarded() -> T:
            with self.tenants.handed(ctx.tenant):
                return await call

        task = asyncio.create_task(guarded())
        self._inflight[task] = cancellation
        task.add_done_callback(self._forget)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            _signal(cancellation)
            raise

    def _forget(self, task: asyncio.Task[Any]) -> None:
        """在途调用结束：移出在途表；调用方已经离开时取走结果，免得事件循环报告没人取的异常。"""
        self._inflight.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def invoke(self, ctx: Context, request: Request) -> Response:
        """经调用器与完整管道执行操作；管道外抛出的异常（排在 Observe 之前的中间件）记进日志并以内核错误返回。"""
        try:
            return await self.plugin.invoker.invoke(ctx, request)
        except Exception:
            _logger.exception(
                "The operation '%s' raised an unhandled exception.", request.id, extra={"plugin": self.id}
            )
            return Response(fail(Error.unexpected(ERROR_CODE_INVOCATION_UNEXPECTED, MESSAGE_UNHANDLED_EXCEPTION)))

    def handlers_for(self, envelope: Envelope, mode: str) -> list[EventHandler]:
        """按（主题，投递方式）找订阅，再看类型（invocation.md §4）：只认以这种方式声明、类型为空或相同的订阅。"""
        # 投递时宿主总是填写方式，空同竞争消费；不认识的方式匹配不到任何订阅
        delivered = mode or SubscriptionMode.COMPETING
        return [
            handler.handler
            for handler in self.plugin.subscriptions
            if handler.subscription.topic == envelope.topic
            and handler.subscription.effective_mode == delivered
            and handler.subscription.message_type in (None, envelope.type)
        ]

    async def deliver(
        self, ctx: Context, handlers: Sequence[EventHandler], envelope: Envelope, attempt: int
    ) -> Result[Unit]:
        """依次交给匹配的处理器，第一个失败即投递结果（RFC-0009「投递」第 2 条）；在截止时间与取消信号下执行。

        处理器抛出的异常与调用器的 Observe 阶段一样翻译为 ``KERNEL_UNEXPECTED_001`` +
        ``kernel.invocation.unhandled_exception``（RFC-0011 规则 5），完整异常记进日志。
        """

        async def dispatch(scoped: Context) -> Result[Unit]:
            for handler in handlers:
                try:
                    result = await handler(scoped, envelope, attempt)
                except Exception:
                    _logger.exception(
                        "The handler of the topic '%s' raised an unhandled exception.",
                        envelope.topic,
                        extra={"plugin": self.id},
                    )
                    return fail(Error.unexpected(ERROR_CODE_INVOCATION_UNEXPECTED, MESSAGE_UNHANDLED_EXCEPTION))
                if result.error is not None:
                    return result
            return success()

        return await ctx.run(dispatch)


class _Service(pb_grpc.PluginServicer):
    """``xihan.plugin.v1.Plugin`` 的实现；会话令牌已由 ``_SessionInterceptor`` 核对过。"""

    def __init__(self, runtime: _Runtime) -> None:
        """绑定一次运行的状态。"""
        self._runtime = runtime

    async def Handshake(  # noqa: N802
        self, request: pb.HandshakeRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.HandshakeResponse:
        """协商协议版本并交换清单与声明（handshake.md §3）；没有共同版本以 ``FailedPrecondition`` 拒绝。"""
        runtime = self._runtime
        version = negotiate_version(request.protocol_versions)
        if version is None:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"No common protocol version: the host supports {list(request.protocol_versions)}, "
                f"the plugin supports {list(SUPPORTED_PROTOCOL_VERSIONS)}.",
            )
        if runtime.lifecycle.state is not LifecycleState.DECLARED:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "The plugin has already completed its handshake.")
        response = runtime.handshake(version)
        runtime.lifecycle.transition(LifecycleState.RESOLVED)
        if request.default_locale:
            # 错误文案按宿主的默认区域解析；握手带来的比环境变量更准
            runtime.locale = request.default_locale
        _logger.info(
            "Handshake completed.",
            extra={
                "plugin": runtime.id,
                "protocolVersion": version,
                "host": request.host.sdk,
                "hostVersion": request.host.sdk_version,
                "instance": request.instance_id,
            },
        )
        return response

    async def Configure(  # noqa: N802
        self, request: pb.ConfigureRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.ConfigureResponse:
        """下发配置、权限、绑定与工作目录（lifecycle.md §2）；握手之前或重复调用以 ``FailedPrecondition`` 拒绝。"""
        if self._runtime.lifecycle.state is not LifecycleState.RESOLVED:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Configure requires a completed handshake.")
        return pb.ConfigureResponse(status=self._runtime.status(await self._runtime.configure(request)))

    async def Start(  # noqa: N802
        self, request: pb.StartRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.StartResponse:
        """启动；返回 ``ok`` 后接受调用（lifecycle.md §2）。``Configure`` 成功之前以 ``FailedPrecondition`` 拒绝。"""
        if self._runtime.lifecycle.state is not LifecycleState.CONFIGURED:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Start requires a successful Configure.")
        return pb.StartResponse(status=self._runtime.status(await self._runtime.start()))

    async def Stop(  # noqa: N802
        self, request: pb.StopRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.StopResponse:
        """停止：在宽限期内完成在途调用、释放资源、应答，然后退出进程（lifecycle.md §5）。"""
        runtime = self._runtime
        grace = timedelta(seconds=request.grace_seconds) if request.grace_seconds else runtime.plugin.stop_grace
        result = await runtime.drain(grace, request.reason or "shutdown")
        # 应答送出之后才关闭服务端：serve 在停止信号之后给在途 RPC 留出时间
        asyncio.get_running_loop().call_soon(runtime.request_shutdown, f"Stop ({request.reason or 'shutdown'})")
        return pb.StopResponse(status=runtime.status(result))

    async def Health(  # noqa: N802
        self, request: pb.HealthRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.HealthResponse:
        """当前健康。"""
        return self._runtime.health()

    async def WatchHealth(  # noqa: N802
        self, request: pb.HealthRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> AsyncIterator[pb.HealthResponse]:
        """先推当前健康，之后状态或健康项变化时各推一条；开始停止时推最后一条并结束流，不拖住停止（lifecycle.md §4）。"""
        runtime = self._runtime
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()

        def wake() -> None:
            # 迁移可能发生在别的线程：经事件循环置位；循环已关闭说明流已经不在了
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(changed.set)

        unsubscribe = runtime.lifecycle.listen(wake)
        try:
            last = runtime.health()
            yield last
            while not runtime.draining:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(changed.wait(), _HEALTH_POLL_SECONDS)
                changed.clear()
                current = runtime.health()
                if current != last:
                    last = current
                    yield current
        finally:
            unsubscribe()

    async def Invoke(  # noqa: N802
        self, request: pb.InvokeRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.InvokeResponse:
        """调用一个一元操作：不接受调用时 ``PLUGINS_UNAVAILABLE_001``，未登记的操作以 ``Unimplemented`` 结束（invocation.md §1、§3）。"""
        runtime = self._runtime
        if not runtime.serving:
            return pb.InvokeResponse(status=runtime.status(fail(runtime.unavailable())))
        if runtime.plugin.invoker.registry.find(request.operation) is None:
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED, f"The operation '{request.operation}' is not provided by this plugin."
            )
        metadata = request.metadata if request.HasField("metadata") else None
        ctx, cancellation = runtime.context(metadata, context.time_remaining())
        response = await runtime.track(
            ctx, cancellation, runtime.invoke(ctx, Request(request.operation, request.payload))
        )
        return pb.InvokeResponse(status=runtime.status(response.outcome), payload=response.payload or b"")

    async def InvokeStream(  # noqa: N802
        self, request_iterator: AsyncIterator[pb.StreamFrame], context: grpc.aio.ServicerContext[Any, Any]
    ) -> AsyncIterator[pb.StreamFrame]:
        """流式调用：第一帧必须是 ``start``，否则 ``InvalidArgument``；本 SDK 的操作都是一元的，``start`` 之后 ``Unimplemented``。"""
        first: pb.StreamFrame | None = None
        async for frame in request_iterator:
            first = frame
            break
        if first is None or first.WhichOneof("frame") != "start":
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "The first frame of a stream must be 'start'.")
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            f"The operation '{first.start.operation}' is not a streaming operation of this plugin.",
        )
        yield first  # pragma: no cover - abort 之后不会执行，只让它成为异步生成器

    async def HandleEvent(  # noqa: N802
        self, request: pb.HandleEventRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.HandleEventResponse:
        """把消息交给以投递的方式订阅了该主题、类型也匹配的处理器（invocation.md §4）；没有匹配的订阅时返回失败。"""
        runtime = self._runtime
        if not runtime.serving:
            return pb.HandleEventResponse(status=runtime.status(fail(runtime.unavailable())))
        envelope = from_proto_envelope(request.envelope)
        handlers = runtime.handlers_for(envelope, request.mode)
        if not handlers:
            mode = request.mode or SubscriptionMode.COMPETING
            reason = f"no subscription matches the topic '{envelope.topic}', the type '{envelope.type}' and the mode '{mode}'"
            return pb.HandleEventResponse(status=runtime.status(fail(failed(runtime.id, "HandleEvent", reason))))
        metadata = request.metadata if request.HasField("metadata") else None
        ctx, cancellation = runtime.context(metadata, context.time_remaining())
        result = await runtime.track(ctx, cancellation, runtime.deliver(ctx, handlers, envelope, request.attempt))
        return pb.HandleEventResponse(status=runtime.status(result))


class _SessionInterceptor(grpc.aio.ServerInterceptor):
    """只放行带本次会话令牌的调用（handshake.md §4，ADR-0202）：在进入任何服务实现之前按常数时间比较。

    令牌缺失、多于一个或不匹配时以 ``Unauthenticated`` 拒绝，服务实现不执行；本机别的进程连上插件端点也冒充不了宿主。
    """

    def __init__(self, token: str) -> None:
        """以握手前经环境变量下发的令牌创建。"""
        self._token = token.encode("utf-8")

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        """核对令牌；不对时换成一个只拒绝的处理器，处理器的种类与原方法相同，宿主那边按原方法读应答。"""
        presented = [
            value for key, value in handler_call_details.invocation_metadata or () if key == SESSION_METADATA_KEY
        ]
        if len(presented) == 1:
            value = presented[0]
            candidate = value if isinstance(value, bytes) else value.encode("utf-8")
            # 常数时间比较：逐字节短路的比较会从耗时里泄露令牌前缀
            if hmac.compare_digest(candidate, self._token):
                return await continuation(handler_call_details)
        handler = await continuation(handler_call_details)
        return _refusing(handler)


async def _refuse(request: object, context: grpc.aio.ServicerContext[Any, Any]) -> None:
    """拒绝一个没有带对会话令牌的调用。"""
    await context.abort(grpc.StatusCode.UNAUTHENTICATED, "The plugin session token is missing or invalid.")


def _refusing(handler: grpc.RpcMethodHandler[Any, Any] | None) -> grpc.RpcMethodHandler[Any, Any]:
    """与 ``handler`` 同种类的拒绝处理器；方法不存在时同样以 ``Unauthenticated`` 拒绝，先核对令牌再看方法。"""
    if handler is None or not (handler.request_streaming or handler.response_streaming):
        return grpc.unary_unary_rpc_method_handler(_refuse)
    if handler.request_streaming and handler.response_streaming:
        return grpc.stream_stream_rpc_method_handler(_refuse)
    if handler.request_streaming:
        return grpc.stream_unary_rpc_method_handler(_refuse)
    return grpc.unary_stream_rpc_method_handler(_refuse)


def _listen(server: grpc.aio.Server, network: Network) -> tuple[Endpoint, str | None]:
    """按 handshake.md §4 监听本机端点，返回端点与要在停止后删除的私有目录。

    Unix 域套接字放进本进程新建的私有目录：``mkdtemp`` 的名字随机、权限 ``0700``、只新建不复用，别人抢先建好的同名
    目录或指向别处的链接不会被拿来用；套接字文件再收紧到 ``0600``。TCP 只绑定回环地址 ``127.0.0.1`` 的随机端口。
    """
    if network is Network.UNIX:
        directory = tempfile.mkdtemp(prefix="xihan-plugin-")
        path = os.path.join(directory, "plugin.sock")
        try:
            bound = server.add_insecure_port(f"unix:{path}")
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        if not bound:
            shutil.rmtree(directory, ignore_errors=True)
            raise OSError(f"Could not listen on the unix socket '{path}'.")
        # Windows 上 chmod 只管只读位；那里的私有目录在当前用户的临时目录下，靠目录的访问控制
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return Endpoint(Network.UNIX, path), directory
    port = server.add_insecure_port("127.0.0.1:0")
    if not port:
        raise OSError("Could not listen on a loopback TCP port.")
    return Endpoint(Network.TCP, f"127.0.0.1:{port}"), None


def _watch_stdin(stdin: BinaryIO | None, on_closed: Callable[[], None]) -> None:
    """在守护线程里读掉 stdin 直到 EOF，然后发停止信号（handshake.md §1：stdin 关闭即自行退出，不留孤儿进程）。

    用线程而不是事件循环：Windows 的事件循环读不了管道形式的 stdin；守护线程不拖住进程退出。
    """
    if stdin is None:
        return

    def drain() -> None:
        try:
            fd = stdin.fileno()
        except (OSError, ValueError):
            fd = None
        try:
            if fd is None:
                while stdin.read(65536):
                    pass
            else:
                while os.read(fd, 65536):
                    pass
        except (OSError, ValueError):
            # stdin 已经关闭或不可读：同样当作宿主已经离开
            pass
        with contextlib.suppress(RuntimeError):
            on_closed()

    threading.Thread(target=drain, name="xihan-plugin-stdin", daemon=True).start()


def _watch_sigterm(loop: asyncio.AbstractEventLoop, on_signal: Callable[[str], None]) -> bool:
    """收到 SIGTERM 即发停止信号（lifecycle.md §5）；登记成功时返回真。

    Windows 没有 SIGTERM，宿主以关闭 stdin 作等价的终止信号；不在主线程的事件循环也登记不了，同样只靠 stdin。
    """
    try:
        loop.add_signal_handler(signal.SIGTERM, on_signal, "SIGTERM")
    except (NotImplementedError, RuntimeError, ValueError):
        return False
    return True


def _stdin() -> BinaryIO | None:
    """进程的 stdin 字节流；没有 stdin（如 ``pythonw``）时为 ``None``。"""
    stream = sys.stdin
    return None if stream is None else stream.buffer


def _signal(cancellation: Cancellation) -> None:
    """触发一次调用的取消信号；订阅者抛出的异常记进日志，不打断停止。"""
    try:
        cancellation.cancel()
    except Exception:
        _logger.exception("A cancellation callback failed.")


def _section(text: str) -> tuple[Mapping[str, JsonValue] | None, str]:
    """读 ``Configure`` 的配置节：空为 ``{}``；不是 JSON 对象时返回 ``None`` 与原因。"""
    if not text:
        return {}, ""
    try:
        value = parse(text)
    except JsonTextError:
        return None, "the configuration is not valid JSON"
    if not isinstance(value, dict):
        return None, "the configuration is not a JSON object"
    return value, ""


def _manifest_text(manifest: str | bytes | None) -> str:
    """清单文本：省略时读工作目录里的 ``xihan.manifest.json``；去掉开头的 UTF-8 BOM。"""
    if manifest is None:
        path = Path(manifests.FILE_NAME)
        if not path.is_file():
            raise FrameworkError.of(
                Error(
                    manifests.ERROR_CODE_INVALID_MANIFEST, manifests.MESSAGE_FILE_MISSING, {"path": str(path.resolve())}
                )
            )
        manifest = path.read_bytes()
    if isinstance(manifest, bytes):
        try:
            manifest = manifest.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FrameworkError.of(
                Error(manifests.ERROR_CODE_INVALID_MANIFEST, manifests.MESSAGE_INVALID_JSON, {"reason": "$"})
            ) from error
    return manifest.removeprefix(chr(0xFEFF))


def _configuration_error(message: str) -> FrameworkError:
    """插件声明不合法的配置错误。"""
    return FrameworkError(ERROR_CODE_INVALID_ARGUMENT, message)
