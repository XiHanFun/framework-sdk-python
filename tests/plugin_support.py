"""插件运行时测试的共用工具：在本进程里运行一个插件、连上它，以及一个记录回调的假宿主。"""

# grpc 存根里的泛型与只在类型检查时存在的 PluginAsyncStub：注解推迟求值
from __future__ import annotations

import asyncio
import contextlib
import gc
import io
import json
import os
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

import grpc

from xihan_framework.plugin import (
    ENV_HOST_ADDRESS,
    ENV_INSTANCE,
    ENV_LOCALE,
    ENV_PRIMITIVES,
    ENV_PROTOCOL_VERSIONS,
    ENV_SESSION,
    SESSION_METADATA_KEY,
    Endpoint,
    Network,
    Plugin,
    parse_handshake_line,
)
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.plugin.v1 import plugin_pb2_grpc as pb_grpc

#: 测试用的会话令牌。
TOKEN = "dGVzdC1zZXNzaW9uLXRva2VuLXdpdGgtMzItYnl0ZXMtb2YtZW50cm9weQ"
#: Windows 上的 .NET 宿主排在回环 TCP 之前的命名管道端点：grpcio 连不了，插件跳过它。
PIPE = Endpoint(Network.NPIPE, r"\\.\pipe\xihan-host-tests")


def manifest_text(**overrides: Any) -> str:
    """测试插件 ``xihan.tests.probe`` 的清单；``overrides`` 覆盖顶层成员，值为 ``None`` 时去掉该成员。"""
    manifest: dict[str, Any] = {
        "id": "xihan.tests.probe",
        "version": "1.0.0",
        "primitives": "1",
        "stability": "experimental",
        "kind": "plugin",
        "isolation": "out-of-process",
        "contributes": {
            "operations": ["probes.echo", "probes.wait", "probes.callback", "probes.explode"],
            "config": {"section": "XiHan:Probe"},
            "health": ["probe"],
        },
        "permissions": ["capability:kernel.pipeline", "capability:messaging.queue"],
        "entry": "probe",
    }
    for name, value in overrides.items():
        if value is None:
            manifest.pop(name, None)
        else:
            manifest[name] = value
    return json.dumps(manifest, indent=2)


class _Capture(io.BytesIO):
    """收下插件写到 stdout 的字节；第一次 ``flush`` 之后握手行就在里面。"""

    def __init__(self) -> None:
        super().__init__()
        self.flushed = asyncio.Event()

    def flush(self) -> None:
        super().flush()
        self.flushed.set()


@dataclass
class Running:
    """在本进程里运行的插件：``serve`` 的任务、握手行给出的端点、带对了令牌的客户端与 stdin 的写端。"""

    task: asyncio.Task[None]
    endpoint: Endpoint
    line: bytes
    channel: grpc.aio.Channel
    stub: pb_grpc.PluginAsyncStub
    stdin: int
    stdin_open: bool = True

    def close_stdin(self) -> None:
        """关闭插件的 stdin（宿主退出时的情形）。"""
        if self.stdin_open:
            self.stdin_open = False
            os.close(self.stdin)

    async def handshake(self) -> pb.HandshakeResponse:
        """以宿主的身份握手。"""
        return await self.stub.Handshake(handshake_request(), metadata=session())

    async def configure(self, configuration: str = "{}", **options: Any) -> pb.ConfigureResponse:
        """配置；默认授予清单声明的两项回调权限。"""
        options.setdefault("granted_permissions", ["capability:kernel.pipeline", "capability:messaging.queue"])
        return await self.stub.Configure(
            pb.ConfigureRequest(configuration=configuration, work_directory=".", **options), metadata=session()
        )

    async def health(self) -> pb.HealthResponse:
        """求一次健康。"""
        return await self.stub.Health(pb.HealthRequest(), metadata=session())

    async def start(self, configuration: str = "{}") -> pb.StartResponse:
        """握手、配置并启动，三步都必须成功。"""
        await self.handshake()
        configured = await self.configure(configuration)
        assert configured.status.ok, configured.status
        started = await self.stub.Start(pb.StartRequest(), metadata=session())
        assert started.status.ok, started.status
        return started

    async def stop(self, grace_seconds: int = 5, reason: str = "unload") -> pb.StopResponse:
        """停止。"""
        return await self.stub.Stop(pb.StopRequest(grace_seconds=grace_seconds, reason=reason), metadata=session())

    async def exited(self) -> None:
        """等 ``serve`` 结束，最多 10 秒。"""
        await asyncio.wait_for(asyncio.shield(self.task), 10)


def run_scenario(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """在新的事件循环里跑一个场景，整体限时；循环关闭之前等 gRPC 的收尾事件送达。

    grpc.aio 的完成队列线程把事件交给创建 gRPC 对象时的事件循环：循环关闭之后才到的事件（如被取消的调用、停止的服务端
    的收尾）会让这个线程退出，之后的循环里 gRPC 调用再也等不到完成。所以场景结束时先让出事件循环、回收没人引用的 gRPC
    对象，让它们在循环还开着时收尾。插件进程只有一个事件循环，没有这个问题。
    """

    async def bounded() -> None:
        try:
            async with asyncio.timeout(60):
                await scenario()
        finally:
            await asyncio.sleep(0.2)
            gc.collect()
            await asyncio.sleep(0.05)

    asyncio.run(bounded())


def session(token: str = TOKEN) -> tuple[tuple[str, str], ...]:
    """带会话令牌的调用元数据。"""
    return ((SESSION_METADATA_KEY, token),)


def handshake_request(*versions: int) -> pb.HandshakeRequest:
    """宿主的握手请求；不给版本时为 ``[1]``。"""
    return pb.HandshakeRequest(
        protocol_versions=list(versions or (1,)),
        primitives=1,
        instance_id="test",
        host=pb.HostInfo(framework_version="test", sdk="tests", sdk_version="0"),
        default_locale="en-US",
    )


def environment(**extra: str) -> dict[str, str]:
    """宿主下发的环境变量（不含回调端点）。"""
    return {
        ENV_PROTOCOL_VERSIONS: "1",
        ENV_PRIMITIVES: "1",
        ENV_SESSION: TOKEN,
        ENV_INSTANCE: "xihan.tests.probe#test",
        ENV_LOCALE: "en-US",
        **extra,
    }


def env_with_host(*endpoints: Endpoint) -> dict[str, str]:
    """带宿主回调端点列表的环境变量：按给出的顺序以 ``;`` 分隔（handshake.md §4）。"""
    return environment(**{ENV_HOST_ADDRESS: ";".join(str(endpoint) for endpoint in endpoints)})


@contextlib.asynccontextmanager
async def running(plugin: Plugin, environ: Mapping[str, str] | None = None) -> AsyncIterator[Running]:
    """在本进程里运行插件，读到握手行后连上它；离开时关闭 stdin 并等 ``serve`` 结束。"""
    read_end, write_end = os.pipe()
    stdin = os.fdopen(read_end, "rb", buffering=0)
    stdout = _Capture()
    task = asyncio.create_task(
        plugin.serve(environ=environment() if environ is None else environ, stdin=stdin, stdout=stdout)
    )
    try:
        async with asyncio.timeout(10):
            await stdout.flushed.wait()
        line = stdout.getvalue()
        endpoint = parse_handshake_line(line.decode("utf-8").removesuffix("\n"))
        assert endpoint is not None, line
        target = endpoint.grpc_target
        assert target is not None, line
        channel = grpc.aio.insecure_channel(target)
        state = Running(task, endpoint, line, channel, pb_grpc.PluginStub(channel), write_end)
        try:
            yield state
        finally:
            await channel.close()
            state.close_stdin()
            async with asyncio.timeout(15):
                await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        with contextlib.suppress(OSError):
            os.close(write_end)
        stdin.close()
        stdout.close()


@dataclass(frozen=True)
class Received:
    """假宿主收到的一次回调：请求、元数据里的文本值与 gRPC 的剩余时间。"""

    request: Any
    metadata: dict[str, str]
    remaining: float | None


@dataclass
class FakeHost(pb_grpc.HostServicer):
    """记录回调的假宿主：``Invoke`` 默认回 ``{"ok":true}``，``Publish`` 默认逐项成功。

    ``status`` 不为空时以这个 gRPC 状态（消息 ``message``）结束调用；``invoke_status`` 与 ``publish_results`` 改写应答；
    ``delay`` 是记下回调之后、应答之前等待的秒数，用来让回调在途中遇到截止时间或取消。
    """

    invocations: list[Received] = field(default_factory=list)
    publications: list[Received] = field(default_factory=list)
    status: grpc.StatusCode | None = None
    message: str = "boom"
    invoke_status: pb.Status | None = None
    publish_results: list[pb.Status] | None = None
    delay: float = 0.0

    async def Invoke(  # noqa: N802
        self, request: pb.InvokeRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.InvokeResponse:
        self.invocations.append(Received(request, _metadata(context), context.time_remaining()))
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.status is not None:
            await context.abort(self.status, self.message)
        if self.invoke_status is not None:
            return pb.InvokeResponse(status=self.invoke_status)
        return pb.InvokeResponse(status=pb.Status(ok=True), payload=b'{"ok":true}')

    async def Publish(  # noqa: N802
        self, request: pb.PublishRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.PublishResponse:
        self.publications.append(Received(request, _metadata(context), context.time_remaining()))
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.status is not None:
            await context.abort(self.status, self.message)
        if self.publish_results is not None:
            return pb.PublishResponse(results=self.publish_results)
        return pb.PublishResponse(results=[pb.Status(ok=True) for _ in request.envelopes])


def _metadata(context: grpc.aio.ServicerContext[Any, Any]) -> dict[str, str]:
    """调用元数据里的文本值。"""
    return {key: value for key, value in context.invocation_metadata() or () if isinstance(value, str)}


@contextlib.asynccontextmanager
async def fake_host() -> AsyncIterator[tuple[FakeHost, Endpoint]]:
    """在回环 TCP 上运行假宿主。"""
    host = FakeHost()
    server = grpc.aio.server()
    pb_grpc.add_HostServicer_to_server(host, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield host, Endpoint(Network.TCP, f"127.0.0.1:{port}")
    finally:
        await server.stop(None)
