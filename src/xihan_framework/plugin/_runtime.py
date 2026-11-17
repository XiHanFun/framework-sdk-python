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
    ERROR_CODE_NOT_FOUND,
    MESSAGE_UNHANDLED_EXCEPTION,
    Invoker,
    Registry,
    Request,
    Response,
)
from xihan_framework.operation import ERROR_CODE_UNEXPECTED as ERROR_CODE_INVOCATION_UNEXPECTED
from xihan_framework.pipeline import Builder
from xihan_framework.plugin._convert import (
    failed,
    from_proto_envelope,
    restore_context,
    to_proto_health,
    to_status,
)
from xihan_framework.plugin._host import HOST_ITEM, Host
from xihan_framework.plugin._lifecycle import HEALTH_REPORTED_STATES, SERVING_STATES, Lifecycle, TenantLedger
from xihan_framework.plugin._log import JsonLogHandler
from xihan_framework.plugin._messages import (
    CATALOG,
    CATALOG_NAME,
    ERROR_CODE_UNAVAILABLE,
    MESSAGE_EVENT_UNSUBSCRIBED,
    MESSAGE_UNAVAILABLE,
)
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
    has_whitespace,
    negotiate_version,
    select_host_endpoint,
)
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.plugin.v1 import plugin_pb2_grpc as pb_grpc
from xihan_framework.primitives import (
    DEFAULT_LOCALE,
    ERROR_CODE_INVALID_ARGUMENT,
    SDK_NAME,
    SDK_VERSION,
    SPEC_VERSION,
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

#: 配置节不是 JSON 对象时 ``plugins.process.failed`` 的 ``reason``（lifecycle.md §7.2）。
REASON_CONFIGURATION_NOT_OBJECT: Final = "the configuration is not a JSON object"

# 生命周期与健康回调失败时的 phase，一律小写（lifecycle.md §7）
_PHASE_CONFIGURE: Final = "configure"
_PHASE_START: Final = "start"
_PHASE_STOP: Final = "stop"
_PHASE_HEALTH: Final = "health"
# 可服务期间 WatchHealth 重新求健康回调的间隔：宿主 HealthInterval 的缺省值（lifecycle.md §7.4）
_HEALTH_POLL_SECONDS: Final = 10.0
# Stop 的 grace_seconds 为 0 时等在途调用的时间（lifecycle.md §7.5）
_DEFAULT_STOP_GRACE_SECONDS: Final = 5
# stdin 关闭与 SIGTERM 的收尾时限：短于宿主发出终止信号后再等的 5 秒（lifecycle.md §7.7）
_SHUTDOWN_LIMIT_SECONDS: Final = 3.0
# 收尾时限里留给关闭服务端、断开宿主与删除私有目录的时间
_SHUTDOWN_TEARDOWN_SECONDS: Final = 0.5
# Stop 应答之后关闭 gRPC 服务端时给在途 RPC（含 Stop 本身的应答）的时间
_SERVER_STOP_GRACE_SECONDS: Final = 1.0
# 收尾时关闭服务端之后还要断开宿主与删除私有目录，时限前留出的这一点不给在途 RPC
_TEARDOWN_MARGIN_SECONDS: Final = 0.25
# 握手之前不接受 Stop，停止中与停止后也不接受（lifecycle.md §7.1）
_STOP_REFUSED_STATES: Final = frozenset({LifecycleState.DECLARED, LifecycleState.DRAINING, LifecycleState.STOPPED})
# 投递方式只能是空、competing 或 broadcast，其余以 InvalidArgument 拒绝（invocation.md §4）
_SUBSCRIPTION_MODES: Final = frozenset({"", SubscriptionMode.COMPETING.value, SubscriptionMode.BROADCAST.value})


class Plugin:
    """一个进程外插件：清单、操作登记表、插件内的管道、事件订阅、消息目录与生命周期回调。

    构造时核对清单与声明（handshake.md §1 末段），不合即抛出 ``FrameworkError``（配置错误），不监听、不写握手行：清单读
    不出、不是 ``isolation: out-of-process`` 的插件、写了 ``contributes.pipeline``（只有 Wasm 插件能贡献中间件）、
    ``primitives`` 不是本 SDK 实现的原语层大版本、登记的操作不在清单的 ``contributes.operations`` 里、同一（主题，类型）
    对订阅了两次、消息目录没有 ``en-US`` 或用了运行时目录的名字，或要求的网络类型 grpcio 不能监听。操作经 SDK 的
    ``operation.Invoker`` 与完整管道执行（RFC-0011），调用上下文按 invocation.md §2 从 ``InvocationMetadata`` 还原。
    """

    __slots__ = ("_catalogs", "_hooks", "_invoker", "_manifest", "_manifest_text", "_network", "_subscriptions")

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
    ) -> None:
        """创建插件。

        ``manifest`` 是 ``xihan.manifest.json`` 的文本，握手时原样交给宿主（去掉开头的 BOM），所以必须与插件目录里的
        文件一致；省略时读当前工作目录（宿主把它设为插件目录）里的文件。``catalogs`` 是作者自己的消息目录（目录名到目录），
        随握手交给宿主翻译插件的错误；运行时的目录 ``CATALOG`` 由宿主提供，不随握手发送。``network`` 是监听的网络类型，
        省略时为 ``unix``。
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
        if str(parsed.primitives) != SPEC_VERSION:
            raise _configuration_error(
                f"The plugin '{parsed.id}' declares primitives {parsed.primitives}; this SDK implements primitives {SPEC_VERSION}."
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
            # 运行时的键由宿主提供，SDK 不随握手发送它们（handshake.md §3 第 6 条）
            if name == CATALOG_NAME or catalog is CATALOG:
                raise _configuration_error(
                    f"The message catalog '{name}' of '{parsed.id}' is the runtime catalog '{CATALOG_NAME}': "
                    "the host provides its keys, so it is not sent with the handshake."
                )
            if DEFAULT_LOCALE not in catalog.locales:
                raise _configuration_error(
                    f"The message catalog '{name}' of '{parsed.id}' has no {DEFAULT_LOCALE} templates."
                )
        if network is Network.NPIPE:
            raise _configuration_error("grpcio cannot listen on Windows named pipes; use 'unix' or 'tcp'.")
        self._manifest = parsed
        self._manifest_text = text
        self._invoker = Invoker(registry, pipeline)
        self._subscriptions = tuple(subscriptions)
        self._catalogs = frozen_catalogs
        self._hooks = Hooks() if hooks is None else hooks
        self._network = Network.UNIX if network is None else network

    @property
    def manifest(self) -> manifests.Manifest:
        """读好的清单。"""
        return self._manifest

    @property
    def manifest_text(self) -> str:
        """握手时交给宿主的清单文本（去掉了开头的 BOM）。"""
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
        """随握手交给宿主的消息目录（作者自己的）。"""
        return self._catalogs

    @property
    def hooks(self) -> Hooks:
        """生命周期回调。"""
        return self._hooks

    @property
    def network(self) -> Network:
        """监听的网络类型。"""
        return self._network

    async def serve(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> None:
        """以进程外插件的身份运行，直到 ``Stop`` 应答完、stdin 关闭或收到 SIGTERM 之后的收尾结束。

        读 handshake.md §1 的环境变量（``environ`` 省略时为 ``os.environ``），按 §4 监听本机端点，向 ``stdout``（省略时为
        ``sys.stdout`` 的字节流）写一行握手行，之后不再写它；``stdin``（省略时为 ``sys.stdin`` 的字节流）关闭即收尾。
        缺少会话令牌是配置错误，在监听之前抛出 ``FrameworkError``。stdin 关闭与 SIGTERM 之后最多 3 秒返回（lifecycle.md §7.7）。
        """
        await self._serve(environ, stdin, stdout, None)

    def run(self) -> int:
        """进程入口：把根日志接到 stderr 的 JSON 行（invocation.md §6），运行到停止，返回进程退出码；同 ``run(lambda: plugin)``。

        插件在调用它之前已经构造好，构造时的配置错误不经过这里；要让它们也记进 stderr 的 JSON 日志，用模块函数 ``run``。
        """
        _attach_log()
        return _run_process(self)

    async def _serve(
        self,
        environ: Mapping[str, str] | None,
        stdin: BinaryIO | None,
        stdout: BinaryIO | None,
        on_terminate: Callable[[], None] | None,
    ) -> None:
        """``serve`` 的实现；``on_terminate`` 在收到 stdin 关闭或 SIGTERM 时（在收到的线程里）立即调用。"""
        runtime = _Runtime(self, os.environ if environ is None else environ, on_terminate)
        await runtime.serve(_stdin() if stdin is None else stdin, sys.stdout.buffer if stdout is None else stdout)


def run(build: Callable[[], Plugin]) -> int:
    """进程入口：把根日志接到 stderr 的 JSON 行（invocation.md §6），构造插件并运行到停止，返回进程退出码。

    ``build`` 构造插件；它抛出 ``FrameworkError``（清单或声明不合，handshake.md §1 末段）时不监听、不写握手行，在 stderr
    记一条 ``error`` 日志后返回 1，宿主看到的是进程在握手之前退出。正常停止（``Stop`` 应答之后，或 stdin 关闭、SIGTERM
    之后的收尾）返回 0；收尾从收到起最多 3 秒，到时不论在途调用是否结束都以退出码 0 结束进程（lifecycle.md §7.7）。
    运行中的配置错误同样记 ``error`` 并返回 1，未处理的异常记 ``critical`` 并返回 1。根日志已有处理器时不改动它。
    """
    _attach_log()
    try:
        plugin = build()
    except FrameworkError as error:
        _logger.error("The plugin was refused before listening: %s", error)
        return 1
    except Exception:
        _logger.critical("The plugin could not be built.", exc_info=True)
        return 1
    return _run_process(plugin)


def _attach_log() -> None:
    """根日志没有处理器时接上 stderr 的 JSON 行。"""
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(JsonLogHandler())
        root.setLevel(logging.INFO)


def _run_process(plugin: Plugin) -> int:
    """在新的事件循环里运行插件直到停止，收尾时带上硬时限；返回退出码。"""
    watchdog = _Watchdog(_SHUTDOWN_LIMIT_SECONDS)
    try:
        asyncio.run(plugin._serve(None, None, None, watchdog.start))
    except FrameworkError as error:
        _logger.error("The plugin '%s' could not run: %s", plugin.manifest.id, error)
        return 1
    except Exception:
        _logger.critical("The plugin '%s' stopped on an unhandled exception.", plugin.manifest.id, exc_info=True)
        return 1
    return 0


class _Watchdog:
    """收尾的硬时限：stdin 关闭或 SIGTERM 之后 3 秒仍没退出就以退出码 0 结束进程（lifecycle.md §7.7）。

    ``serve`` 自己在时限里返回；这里兜住它管不到的地方：``asyncio.run`` 收场时取消不理取消的处理器、等执行器线程结束。
    只由进程入口启用，在本进程里调用 ``serve``（如测试）时没有它。
    """

    __slots__ = ("_lock", "_seconds", "_timer")

    def __init__(self, seconds: float) -> None:
        """以时限创建，还没开始计时。"""
        self._seconds = seconds
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def start(self) -> None:
        """开始计时；只有第一次生效，可以从任何线程调用。"""
        with self._lock:
            if self._timer is not None:
                return
            # 守护线程：进程正常退出时不等它
            self._timer = threading.Timer(self._seconds, _exit_now)
            self._timer.daemon = True
            self._timer.start()


def _exit_now() -> None:
    """时限到了仍在收尾：日志逐行刷出过，直接以退出码 0 结束进程。"""
    with contextlib.suppress(Exception):
        sys.stderr.flush()
    os._exit(0)


class _Runtime:
    """一次 ``serve`` 的状态：生命周期、在途调用、健康、宿主客户端与停止的进度。"""

    def __init__(self, plugin: Plugin, environ: Mapping[str, str], on_terminate: Callable[[], None] | None) -> None:
        """读环境变量并选回调端点；缺少会话令牌时抛出 ``FrameworkError``。"""
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
        self.host = Host(
            select_host_endpoint(environ.get(ENV_HOST_ADDRESS)),
            self.session,
            self.id,
            self.tenants,
            permissions=plugin.manifest.permissions,
        )
        self._on_terminate = on_terminate
        self._inflight: dict[asyncio.Task[Any], Cancellation] = {}
        # 启动回调成功过：之后的停止要调停止回调（lifecycle.md §7.5）
        self._started = False
        # Configure 或 Start 正在执行：同时到达的第二次按次序表拒绝
        self._stepping = False
        # 已经接受了 Stop 或收尾请求：再来的 Stop 以 FailedPrecondition 拒绝
        self._stopping = False
        self._stop_task: asyncio.Task[Result[Unit]] | None = None
        self._stop_hook_task: asyncio.Task[Result[Unit]] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_deadline = 0.0
        # Stop 应答之后、或收尾开始：serve 往下走，WatchHealth 结束推送
        self._finished = asyncio.Event()
        # 最近一次健康回调报的健康项（只留声明了的）；回调抛出时为空
        self._items: tuple[HealthItem, ...] = ()
        self._undeclared_items: set[str] = set()
        self._check_environment(environ)

    def _check_environment(self, environ: Mapping[str, str]) -> None:
        """启动时核对其余环境变量：没有可用的回调端点、版本不符都只记告警（handshake.md §4；握手时宿主会拒绝版本不符）。"""
        if self.host.endpoint is None:
            _logger.warning(
                "%s=%r gives no callback endpoint grpcio can dial; host callbacks fail with plugins.host.unavailable.",
                ENV_HOST_ADDRESS,
                environ.get(ENV_HOST_ADDRESS),
                extra={"plugin": self.id},
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
        loop = asyncio.get_running_loop()
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

            def stdin_closed() -> None:
                # 在读 stdin 的线程里：硬时限从这一刻算起，收尾交给事件循环
                self._requested()
                loop.call_soon_threadsafe(self.terminate, "stdin closed")

            def sigterm(reason: str) -> None:
                self._requested()
                self.terminate(reason)

            _watch_stdin(stdin, stdin_closed)
            terminated = _watch_sigterm(loop, sigterm)
            try:
                await self._finished.wait()
            finally:
                if terminated:
                    loop.remove_signal_handler(signal.SIGTERM)
            grace = _SERVER_STOP_GRACE_SECONDS
            if self._shutdown_task is not None:
                grace = await self._await_shutdown(loop)
            await server.stop(grace)
            self.lifecycle.transition(LifecycleState.STOPPED)
            _logger.info("Stopped.", extra={"plugin": self.id})
        finally:
            await server.stop(None)
            await self.host.close()
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)

    def _requested(self) -> None:
        """收到 stdin 关闭或 SIGTERM：交给进程入口开始硬时限的计时。"""
        if self._on_terminate is not None:
            self._on_terminate()

    async def _await_shutdown(self, loop: asyncio.AbstractEventLoop) -> float:
        """等收尾到时限之前，返回关闭服务端还能用的时间；到时不论是否完成都往下走（lifecycle.md §7.7）。"""
        assert self._shutdown_task is not None
        remaining = self._shutdown_deadline - loop.time() - _SHUTDOWN_TEARDOWN_SECONDS
        try:
            await asyncio.wait_for(asyncio.shield(self._shutdown_task), max(remaining, 0.0))
        except TimeoutError:
            _logger.warning(
                "The shutdown did not finish within %s seconds; exiting anyway.",
                _SHUTDOWN_LIMIT_SECONDS,
                extra={"plugin": self.id, "inflight": len(self._inflight)},
            )
        return max(
            0.0, min(_SERVER_STOP_GRACE_SECONDS, self._shutdown_deadline - loop.time() - _TEARDOWN_MARGIN_SECONDS)
        )

    def terminate(self, reason: str) -> None:
        """stdin 关闭或 SIGTERM（lifecycle.md §7.7）：只生效一次，在事件循环里执行。

        可服务的插件进入 ``Draining``（其他状态不迁移），立即触发全部在途调用的取消信号并等它们结束；启动回调成功过而停止
        回调还没调过的，调一次停止回调（``reason`` 为 ``shutdown``）；正在处理的 ``Stop`` 照常进行。``serve`` 最多等到收到
        之后 3 秒。
        """
        if self._shutdown_task is not None:
            return
        loop = asyncio.get_running_loop()
        self._shutdown_deadline = loop.time() + _SHUTDOWN_LIMIT_SECONDS
        self._stopping = True
        _logger.info("Shutting down: %s.", reason, extra={"plugin": self.id})
        if self.lifecycle.state in SERVING_STATES:
            self.lifecycle.transition(LifecycleState.DRAINING)
        # 宿主已经离开或要求立即收尾：在途调用的信号立即触发（只经信号通知，RFC-0003 规则 1），再等它们结束
        for cancellation in list(self._inflight.values()):
            _signal(cancellation)
        self._shutdown_task = asyncio.create_task(self._shutdown())
        self.finish()

    async def _shutdown(self) -> None:
        """收尾的过程：等在途调用，再等进行中的 ``Stop``，或者调一次停止回调。"""
        if self._inflight:
            await asyncio.wait(list(self._inflight))
        if self._stop_task is not None:
            await asyncio.shield(self._stop_task)
        elif self._started:
            remaining = self._shutdown_deadline - asyncio.get_running_loop().time()
            await self._stop_hook("shutdown", timedelta(seconds=max(remaining, 0.0)))

    def finish(self) -> None:
        """``Stop`` 应答已经送出或收尾开始：``serve`` 往下走，``WatchHealth`` 推出最后的状态后结束。"""
        self._finished.set()
        self.lifecycle.notify()

    @property
    def finished(self) -> bool:
        """是否已经应答 ``Stop`` 或开始收尾。"""
        return self._finished.is_set()

    @property
    def serving(self) -> bool:
        """是否可服务：只有 ``Ready`` 与 ``Degraded``（lifecycle.md §7.3）。"""
        return self.lifecycle.state in SERVING_STATES

    def accepts_stop(self) -> bool:
        """``Stop`` 是否在次序表里（lifecycle.md §7.1）：握手之后、停止之前，且没有在停止中。"""
        return self.lifecycle.state not in _STOP_REFUSED_STATES and not self._stopping

    def accepts_step(self, state: LifecycleState) -> bool:
        """``Configure`` / ``Start`` 是否在次序表里：处于 ``state``，没有同一步在执行，也没有在停止中。"""
        return self.lifecycle.state is state and not self._stepping and not self._stopping

    def unavailable(self) -> Error:
        """不在可服务状态时的错误：``PLUGINS_UNAVAILABLE_001`` + ``plugins.process.unavailable``（``state`` 是状态名）。"""
        state = self.lifecycle.state
        return Error.unavailable(ERROR_CODE_UNAVAILABLE, MESSAGE_UNAVAILABLE, {"id": self.id, "state": str(state)})

    def handshake(self, version: int) -> pb.HandshakeResponse:
        """组装握手应答（handshake.md §3）。

        ``primitives`` 写清单的值；清单原文去掉了开头的 BOM；只声明一元操作（契约与 JSON Schema）；订阅按声明顺序，``mode``
        总写 ``competing`` 或 ``broadcast``；只发作者自己的消息目录。
        """
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
                    mode=subscription.effective_mode.value,
                )
            )
        for name, catalog in self.plugin.catalogs.items():
            encoded = pb.MessageCatalog(name=name)
            for locale in sorted(catalog.locales):
                encoded.locales.append(pb.LocaleTemplates(locale=locale, templates=dict(catalog.locales[locale])))
            response.catalogs.append(encoded)
        return response

    async def configure(self, request: pb.ConfigureRequest) -> Result[Unit]:
        """``Configure``（lifecycle.md §7.2）：成功进入 ``Configured``，失败进入 ``Failed``。

        配置节为空等同 ``{}``；不是 JSON 对象（含通不过共同的 JSON 读取检查）时不调配置回调。授予的权限交给宿主客户端，
        回调前的权限核对以它为准。
        """
        section = _section(request.configuration)
        self._stepping = True
        try:
            if section is None:
                result: Result[Unit] = fail(failed(self.id, _PHASE_CONFIGURE, REASON_CONFIGURATION_NOT_OBJECT))
            else:
                self.host._grant(request.granted_permissions)
                configuration = Configuration(
                    section=section,
                    granted_permissions=tuple(request.granted_permissions),
                    bindings=tuple(Binding(binding.name, binding.type, binding.value) for binding in request.bindings),
                    work_directory=request.work_directory,
                )
                hook = self.plugin.hooks.configure
                result = success() if hook is None else await self._hook(_PHASE_CONFIGURE, lambda: hook(configuration))
            self._settle(result, LifecycleState.CONFIGURED, _PHASE_CONFIGURE)
        finally:
            self._stepping = False
        return result

    async def start(self) -> Result[Unit]:
        """``Start``（lifecycle.md §7.3）：启动回调成功后进入 ``Started``，求一次健康再进入 ``Ready``、``Degraded`` 或
        ``Failed``，然后才应答；启动回调失败进入 ``Failed``。"""
        self._stepping = True
        try:
            hook = self.plugin.hooks.start
            result = success() if hook is None else await self._hook(_PHASE_START, hook)
            if self._settle(result, LifecycleState.STARTED, _PHASE_START):
                self._started = True
                if self._stopping:
                    # 启动期间收到了收尾请求：不再进入可服务状态
                    self.lifecycle.transition(LifecycleState.DRAINING)
                else:
                    self._become_ready()
                _logger.info(
                    "Started.",
                    extra={
                        "plugin": self.id,
                        "state": str(self.lifecycle.state),
                        "operations": len(self.plugin.invoker.registry),
                        "subscriptions": len(self.plugin.subscriptions),
                    },
                )
        finally:
            self._stepping = False
        return result

    def _become_ready(self) -> None:
        """``Started`` 之后按健康回调的结果进入 ``Ready``、``Degraded`` 或 ``Failed``（lifecycle.md §7.3）。

        健康回调报了这三种状态之一、或者抛出（按 ``Degraded``）时 ``_probe`` 已经迁移过去；报别的状态与没有健康回调时进入
        ``Ready``。所以 ``Start`` 应答之后的状态是这三种之一。
        """
        self._probe()
        if self.lifecycle.state is LifecycleState.STARTED:
            self.lifecycle.transition(LifecycleState.READY)

    def _settle(self, result: Result[Unit], success_state: LifecycleState, phase: str) -> bool:
        """按一步的结果迁移状态：成功到 ``success_state``，失败到 ``Failed`` 并记错误日志。"""
        if result.error is None:
            self.lifecycle.transition(success_state)
            return True
        self.lifecycle.transition(LifecycleState.FAILED, result.error)
        _logger.error(
            "%s failed: %s", phase, self.describe(result.error), extra={"plugin": self.id, "code": result.error.code}
        )
        return False

    def describe(self, error: Error) -> str:
        """日志里的错误：码与按作者的目录、运行时的目录在 ``en-US`` 里渲染的文案（本地渲染，invocation.md §8）。

        交给宿主的错误不带文案（invocation.md §3），文案只用于 stderr 日志；两份目录都渲染不了时同 ``Error.message``。
        """
        key = error.message_key
        if key is not None:
            for catalog in (*self.plugin.catalogs.values(), CATALOG):
                text = catalog.resolve(key, DEFAULT_LOCALE, error.details)
                if text is not None:
                    return f"{error.code}: {text}"
        return f"{error.code}: {error.message}"

    async def stop(self, grace_seconds: int, reason: str) -> Result[Unit]:
        """``Stop``（lifecycle.md §7.5）；调用方已核对次序。进行中的停止由收尾请求接着等，所以放在单独的任务里。"""
        self._stopping = True
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(grace_seconds, reason))
        return await asyncio.shield(self._stop_task)

    async def _stop(self, grace_seconds: int, reason: str) -> Result[Unit]:
        """``stop`` 的过程。

        启动回调没有成功过：不调停止回调、不迁移状态，应答 ``ok``。成功过：可服务的进入 ``Draining``（之后 ``Failed`` 的
        不迁移），新到的调用与投递以 ``PLUGINS_UNAVAILABLE_001`` 拒绝；在途调用最长等到宽限期（``0`` 按 5 秒），到了仍
        没结束的触发它们的取消信号、不再等；然后调停止回调，``reason`` 原样交给作者。
        """
        grace = timedelta(seconds=grace_seconds or _DEFAULT_STOP_GRACE_SECONDS)
        _logger.info("Stopping.", extra={"plugin": self.id, "reason": reason, "grace": grace.total_seconds()})
        if not self._started:
            return success()
        if self.lifecycle.state in SERVING_STATES or self.lifecycle.state is LifecycleState.STARTED:
            self.lifecycle.transition(LifecycleState.DRAINING)
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
        return await self._stop_hook(reason, grace)

    async def _stop_hook(self, reason: str, grace: timedelta) -> Result[Unit]:
        """调停止回调：进程里最多一次，之后的调用等同一次的结果（lifecycle.md §7.5）。"""
        hook = self.plugin.hooks.stop
        if hook is None:
            return success()
        if self._stop_hook_task is None:
            self._stop_hook_task = asyncio.create_task(self._hook(_PHASE_STOP, lambda: hook(reason, grace)))
        return await asyncio.shield(self._stop_hook_task)

    async def _hook(self, phase: str, call: Callable[[], Awaitable[Result[Unit]]]) -> Result[Unit]:
        """执行一个生命周期回调：抛出的异常连同文本记进 stderr，交给宿主的错误只带异常的简单类型名（lifecycle.md §7.6）。"""
        try:
            return await call()
        except Exception as exception:
            _logger.error(
                "The %s hook raised an unhandled exception.", phase, exc_info=exception, extra={"plugin": self.id}
            )
            return fail(failed(self.id, phase, _reason(exception)))

    def health(self) -> pb.HealthResponse:
        """``Health``（lifecycle.md §7.4）：可服务期间每次都求一次健康回调，其余状态照实报、不调回调。"""
        if self.serving:
            self._probe()
        return self.snapshot()

    def probe(self) -> None:
        """可服务期间重新求一次健康回调（``WatchHealth`` 的定时）。"""
        if self.serving:
            self._probe()

    def _probe(self) -> None:
        """求一次健康回调并照办（lifecycle.md §7.4）。

        报 ``Ready``、``Degraded`` 或 ``Failed`` 的按迁移表迁移（``Failed`` 之后不再回来），报其他状态的不改变状态；名字不在
        清单 ``contributes.health`` 里的健康项丢掉，同一个名字只记一条 ``warning``。抛出时这一次的结果是 ``Degraded``，
        ``error`` 为 ``plugins.process.failed``（``phase`` 为 ``health``），没有健康项；状态与回调报了 ``Degraded`` 一样
        迁过去，之后的结果照常决定状态。
        """
        hook = self.plugin.hooks.health
        if hook is None:
            return
        try:
            reported = hook()
        except Exception as exception:
            _logger.error("The health hook raised an unhandled exception.", exc_info=True, extra={"plugin": self.id})
            self._items = ()
            self._apply(LifecycleState.DEGRADED, failed(self.id, _PHASE_HEALTH, _reason(exception)))
            self.lifecycle.notify()
            return
        declared = self.plugin.manifest.health
        items: list[HealthItem] = []
        for item in reported.items:
            if item.name in declared:
                items.append(item)
            elif item.name not in self._undeclared_items:
                # 宿主只认清单声明了的健康项；每个名字只告警一次，免得每次健康探测各记一条
                self._undeclared_items.add(item.name)
                _logger.warning(
                    "The health item '%s' is not declared in contributes.health.",
                    item.name,
                    extra={"plugin": self.id},
                )
        self._items = tuple(items)
        if reported.state in HEALTH_REPORTED_STATES:
            self._apply(reported.state, reported.error)
        # 健康项可能变了：让 WatchHealth 比较一次
        self.lifecycle.notify()

    def _apply(self, state: LifecycleState, error: Error | None) -> None:
        """照健康的结果迁移状态：与当前状态相同时只换原因，否则按迁移表迁移（``Failed`` 之后不再回来）。"""
        if state is self.lifecycle.state:
            self.lifecycle.report(error)
        else:
            self.lifecycle.transition(state, error)

    def snapshot(self) -> pb.HealthResponse:
        """当前的健康，不调健康回调：``Start`` 之前报本地状态、没有健康项，之后照实报状态、原因与最近的健康项。"""
        state, error = self.lifecycle.current()
        if not self._started:
            return to_proto_health(state, (), error)
        return to_proto_health(state, self._items, error)

    def context(self, metadata: pb.InvocationMetadata | None, remaining: float | None) -> tuple[Context, Cancellation]:
        """为一次调用或投递建上下文（invocation.md §2）：放入宿主客户端；返回的信号由运行时在调用方取消时触发。

        ``remaining`` 是 gRPC 调用自身的剩余时间；生效的截止时间取它与元数据的 ``deadline`` 中较早的一个。
        """
        cancellation = Cancellation()
        deadline = None
        if remaining is not None and math.isfinite(remaining):
            deadline = datetime.now(UTC) + timedelta(seconds=max(remaining, 0.0))
        ctx = restore_context(metadata, cancellation=cancellation, deadline=deadline)
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
        """按（主题，投递方式）找订阅，再看类型（invocation.md §4）：只认以这种方式声明、类型为空或相同的订阅，按声明顺序。"""
        return [
            handler.handler
            for handler in self.plugin.subscriptions
            if handler.subscription.topic == envelope.topic
            and handler.subscription.effective_mode == mode
            and handler.subscription.message_type in (None, envelope.type)
        ]

    async def deliver(
        self, ctx: Context, handlers: Sequence[EventHandler], envelope: Envelope, attempt: int
    ) -> Result[Unit]:
        """依次交给匹配的处理器，第一个失败即投递结果（invocation.md §4、RFC-0009「投递」第 2 条）；在截止时间与取消信号下执行。

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
    """``xihan.plugin.v1.Plugin`` 的实现；会话令牌已由 ``_SessionInterceptor`` 先于一切核对过（handshake.md §4）。"""

    def __init__(self, runtime: _Runtime) -> None:
        """绑定一次运行的状态。"""
        self._runtime = runtime

    async def Handshake(  # noqa: N802
        self, request: pb.HandshakeRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.HandshakeResponse:
        """协商协议版本并交换清单与声明（handshake.md §3）；不在 ``Declared`` 或没有共同版本以 ``FailedPrecondition`` 拒绝。"""
        runtime = self._runtime
        if runtime.lifecycle.state is not LifecycleState.DECLARED:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "The plugin has already completed its handshake.")
        version = negotiate_version(request.protocol_versions)
        if version is None:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"No common protocol version: the host supports {list(request.protocol_versions)}, "
                f"the plugin supports {list(SUPPORTED_PROTOCOL_VERSIONS)}.",
            )
        response = runtime.handshake(version)
        runtime.lifecycle.transition(LifecycleState.RESOLVED)
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
        """下发配置、权限、绑定与工作目录（lifecycle.md §7.2）；只在 ``Resolved`` 接受，其余以 ``FailedPrecondition`` 拒绝。"""
        if not self._runtime.accepts_step(LifecycleState.RESOLVED):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Configure requires a completed handshake.")
        return pb.ConfigureResponse(status=to_status(await self._runtime.configure(request)))

    async def Start(  # noqa: N802
        self, request: pb.StartRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.StartResponse:
        """启动（lifecycle.md §7.3）；只在 ``Configured`` 接受，其余以 ``FailedPrecondition`` 拒绝。"""
        if not self._runtime.accepts_step(LifecycleState.CONFIGURED):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Start requires a successful Configure.")
        return pb.StartResponse(status=to_status(await self._runtime.start()))

    async def Stop(  # noqa: N802
        self, request: pb.StopRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.StopResponse:
        """停止（lifecycle.md §7.5），应答之后退出进程；握手之前、停止中与停止之后以 ``FailedPrecondition`` 拒绝（§7.1）。"""
        runtime = self._runtime
        if not runtime.accepts_stop():
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"Stop is not accepted in the state {runtime.lifecycle.state} or while stopping.",
            )
        result = await runtime.stop(request.grace_seconds, request.reason)
        # 应答送出之后才关闭服务端：serve 在这之后给在途 RPC 留出时间
        asyncio.get_running_loop().call_soon(runtime.finish)
        return pb.StopResponse(status=to_status(result))

    async def Health(  # noqa: N802
        self, request: pb.HealthRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.HealthResponse:
        """当前健康（lifecycle.md §7.4），任何状态都可以调用。"""
        return self._runtime.health()

    async def WatchHealth(  # noqa: N802
        self, request: pb.HealthRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> AsyncIterator[pb.HealthResponse]:
        """先推当前健康，之后状态、健康项或 ``error`` 变化时推送；可服务期间每 10 秒重新求一次健康回调；进入 ``Draining``
        时推一次，应答 ``Stop`` 后结束流（lifecycle.md §7.4）。"""
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
            next_probe = loop.time() + _HEALTH_POLL_SECONDS
            while not runtime.finished:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(changed.wait(), max(next_probe - loop.time(), 0.0))
                changed.clear()
                if runtime.finished:
                    break
                if loop.time() >= next_probe:
                    next_probe = loop.time() + _HEALTH_POLL_SECONDS
                    runtime.probe()
                current = runtime.snapshot()
                if current != last:
                    last = current
                    yield current
            # 结束之前把最后的状态（如 Draining）推出去：迁移的通知可能还没轮到这个流
            current = runtime.snapshot()
            if current != last:
                yield current
        finally:
            unsubscribe()

    async def Invoke(  # noqa: N802
        self, request: pb.InvokeRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.InvokeResponse:
        """调用一个一元操作（invocation.md §1）：未声明的操作以 ``Unimplemented`` 结束，不在可服务状态时
        ``PLUGINS_UNAVAILABLE_001``，然后还原上下文、经调用器与完整管道执行；``payload`` 为空等同 ``{}``。"""
        runtime = self._runtime
        if runtime.plugin.invoker.registry.find(request.operation) is None:
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED, f"The operation '{request.operation}' is not provided by this plugin."
            )
        if not runtime.serving:
            return pb.InvokeResponse(status=to_status(fail(runtime.unavailable())))
        metadata = request.metadata if request.HasField("metadata") else None
        ctx, cancellation = runtime.context(metadata, context.time_remaining())
        response = await runtime.track(
            ctx, cancellation, runtime.invoke(ctx, Request(request.operation, request.payload))
        )
        return pb.InvokeResponse(status=to_status(response.outcome), payload=response.payload or b"")

    async def InvokeStream(  # noqa: N802
        self, request_iterator: AsyncIterator[pb.StreamFrame], context: grpc.aio.ServicerContext[Any, Any]
    ) -> AsyncIterator[pb.StreamFrame]:
        """流式调用（invocation.md §1）：流式操作契约之前插件不实现流式调用。

        读第一帧：流在第一帧之前结束或第一帧不是 ``start`` 时 ``InvalidArgument``，是 ``start`` 时 ``Unimplemented``，不论
        操作是否声明、插件是否就绪。
        """
        first: pb.StreamFrame | None = None
        async for frame in request_iterator:
            first = frame
            break
        if first is None or first.WhichOneof("frame") != "start":
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "The first frame of a stream must be 'start'.")
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            f"The plugin does not implement streaming calls (operation '{first.start.operation}').",
        )
        yield first  # pragma: no cover - abort 之后不会执行，只让它成为异步生成器

    async def HandleEvent(  # noqa: N802
        self, request: pb.HandleEventRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.HandleEventResponse:
        """投递一条消息（invocation.md §4），依次核对：信封与投递方式、订阅、可服务状态，再按声明顺序交给匹配的处理器。"""
        runtime = self._runtime
        if not request.HasField("envelope") or request.mode not in _SUBSCRIPTION_MODES:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "A delivery needs an envelope and a mode of '', 'competing' or 'broadcast'.",
            )
        envelope = from_proto_envelope(request.envelope)
        # 投递时宿主总是填写方式，空同竞争消费
        mode = request.mode or SubscriptionMode.COMPETING.value
        handlers = runtime.handlers_for(envelope, mode)
        if not handlers:
            unsubscribed = Error.not_found(
                ERROR_CODE_NOT_FOUND,
                MESSAGE_EVENT_UNSUBSCRIBED,
                {"id": runtime.id, "topic": envelope.topic, "type": envelope.type, "mode": mode},
            )
            return pb.HandleEventResponse(status=to_status(fail(unsubscribed)))
        if not runtime.serving:
            return pb.HandleEventResponse(status=to_status(fail(runtime.unavailable())))
        metadata = request.metadata if request.HasField("metadata") else None
        ctx, cancellation = runtime.context(metadata, context.time_remaining())
        result = await runtime.track(ctx, cancellation, runtime.deliver(ctx, handlers, envelope, request.attempt))
        return pb.HandleEventResponse(status=to_status(result))


class _SessionInterceptor(grpc.aio.ServerInterceptor):
    """只放行带本次会话令牌的调用（handshake.md §4，ADR-0202）：在进入任何服务实现之前按常数时间比较。

    元数据里必须恰好有一个 ``xihan-plugin-session`` 值：没有、两个及以上（即使其中一个正确）或不匹配都以
    ``Unauthenticated`` 拒绝，服务实现不执行；它先于次序核对，也先于流式调用读第一帧。
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
    目录或指向别处的链接不会被拿来用；套接字文件再收紧到 ``0600``。握手行的地址不能有空白（handshake.md §2），临时目录
    的路径带空白（如 Windows 上带空格的用户名）时改用回环 TCP。TCP 只绑定 ``127.0.0.1`` 的随机端口。
    """
    if network is Network.UNIX:
        if has_whitespace(tempfile.gettempdir()):
            _logger.warning(
                "The temporary directory '%s' contains whitespace, which a handshake line cannot carry; "
                "listening on loopback TCP instead.",
                tempfile.gettempdir(),
            )
            return _listen_tcp(server), None
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
    return _listen_tcp(server), None


def _listen_tcp(server: grpc.aio.Server) -> Endpoint:
    """只绑定回环地址 ``127.0.0.1`` 的随机端口；握手行只写 ``127.0.0.1:<端口>``（handshake.md §2）。"""
    port = server.add_insecure_port("127.0.0.1:0")
    if not port:
        raise OSError("Could not listen on a loopback TCP port.")
    return Endpoint(Network.TCP, f"127.0.0.1:{port}")


def _watch_stdin(stdin: BinaryIO | None, on_closed: Callable[[], None]) -> None:
    """在守护线程里读掉 stdin 直到 EOF 或读出错，然后发收尾请求（handshake.md §1、lifecycle.md §7.7）。

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
    """收到 SIGTERM 即发收尾请求（lifecycle.md §7.7）；登记成功时返回真。

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


def _reason(exception: BaseException) -> str:
    """回调抛出时的 ``reason``：异常的简单类型名，不带模块名与异常文本（lifecycle.md §7.6）。"""
    return type(exception).__name__


def _section(text: str) -> Mapping[str, JsonValue] | None:
    """读 ``Configure`` 的配置节：空为 ``{}``；通不过共同的 JSON 读取检查或不是对象时为 ``None``（lifecycle.md §7.2）。"""
    if not text:
        return {}
    try:
        value = parse(text)
    except JsonTextError:
        return None
    return value if isinstance(value, dict) else None


def _manifest_text(manifest: str | bytes | None) -> str:
    """清单文本：省略时读工作目录里的 ``xihan.manifest.json``；去掉开头的 UTF-8 BOM（handshake.md §3 第 3 条）。"""
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
