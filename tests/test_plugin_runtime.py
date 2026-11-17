"""插件运行时的测试：在本进程里运行插件，以宿主的身份走 handshake.md、lifecycle.md 与 invocation.md 的各个环节。"""

# grpc 存根里的泛型与只在类型检查时存在的 PluginAsyncStub：注解推迟求值
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest

from plugin_support import (
    TOKEN,
    env_with_host,
    fake_host,
    handshake_request,
    manifest_text,
    running,
    session,
)
from xihan_framework import operation
from xihan_framework.invocation import Context
from xihan_framework.plugin import (
    ERROR_CODE_CALLBACK_DENIED,
    ERROR_CODE_UNAVAILABLE,
    ERROR_CODE_UNEXPECTED,
    EXTENSION_TENANT,
    MESSAGE_CALLBACK_TENANT_DENIED,
    SESSION_METADATA_KEY,
    Configuration,
    Envelope,
    Health,
    HealthItem,
    HealthStatus,
    Hooks,
    Network,
    Plugin,
    Subscription,
    SubscriptionHandler,
    SubscriptionMode,
    host_of,
)
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.primitives import (
    BAGGAGE_TENANT,
    Catalog,
    Error,
    LifecycleState,
    Result,
    Stability,
    Unit,
    fail,
    ok,
    success,
)

TRACE_PARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@dataclass(frozen=True)
class EchoRequest:
    text: str = ""


@dataclass(frozen=True)
class EchoResponse:
    text: str
    tenant: str | None
    correlation: str | None
    trace_id: str = field(metadata=operation.json_field("traceId"))
    deadline: str | None = None


@dataclass(frozen=True)
class Empty:
    pass


@dataclass(frozen=True)
class Waited:
    cancelled: bool


@dataclass(frozen=True)
class CallbackRequest:
    tenant: str | None = None
    envelope_tenants: list[str | None] = field(default_factory=list, metadata=operation.json_field("envelopeTenants"))


@dataclass(frozen=True)
class CallbackResponse:
    invoked: str
    published: list[str]


class Probe:
    """测试插件的状态：记录收到的配置、事件、停止与等待中的调用。"""

    def __init__(self) -> None:
        self.configuration: Configuration | None = None
        self.events: list[tuple[str, int, SubscriptionMode]] = []
        self.stopped: list[tuple[str, timedelta]] = []
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.health_state: LifecycleState | None = None
        self.health_items: list[HealthItem] = [HealthItem("probe", HealthStatus.HEALTHY)]
        self.saved: Context | None = None

    async def configure(self, configuration: Configuration) -> Result[Unit]:
        self.configuration = configuration
        if configuration.section.get("fail"):
            return fail(Error.validation("PROBE_VALIDATION_002", "probe.configuration.invalid"))
        return success()

    async def stop(self, reason: str, grace: timedelta) -> Result[Unit]:
        self.stopped.append((reason, grace))
        return success()

    def health(self) -> Health:
        return Health(state=self.health_state, items=self.health_items)

    async def echo(self, ctx: Context, request: EchoRequest) -> Result[EchoResponse]:
        if not request.text:
            return fail(Error.validation("PROBE_VALIDATION_001", "probe.text.missing", {"field": "text"}))
        deadline = None if ctx.deadline is None else ctx.deadline.isoformat()
        return ok(EchoResponse(request.text, ctx.tenant, ctx.correlation, ctx.trace_id, deadline))

    async def wait(self, ctx: Context, request: Empty) -> Result[Waited]:
        # 取消只经信号通知：处理器看信号自己停下，或者等测试放行
        self.waiting.set()
        signal = asyncio.ensure_future(ctx.cancellation.wait())
        released = asyncio.ensure_future(self.release.wait())
        await asyncio.wait({signal, released}, return_when=asyncio.FIRST_COMPLETED)
        signal.cancel()
        released.cancel()
        return ok(Waited(ctx.is_cancelled))

    async def callback(self, ctx: Context, request: CallbackRequest) -> Result[CallbackResponse]:
        host = host_of(ctx)
        assert host is not None
        self.saved = ctx
        scoped = ctx if request.tenant is None else ctx.with_baggage(BAGGAGE_TENANT, request.tenant)
        invoked = await host.invoke(scoped, "orders.create", b'{"n":1}')
        envelopes = [
            Envelope(
                topic="orders.created",
                id=f"e{index}",
                source="urn:probe",
                type="orders.created",
                extensions={} if tenant is None else {EXTENSION_TENANT: tenant},
            )
            for index, tenant in enumerate(request.envelope_tenants)
        ]
        published = await host.publish(ctx, envelopes)
        return ok(
            CallbackResponse(
                invoked="ok" if invoked.outcome.error is None else invoked.outcome.error.code,
                published=["ok" if result.error is None else result.error.code for result in published],
            )
        )

    async def explode(self, ctx: Context, request: Empty) -> Result[Empty]:
        raise LookupError("boom")

    async def handle(self, ctx: Context, envelope: Envelope, attempt: int) -> Result[Unit]:
        self.events.append((envelope.id, attempt, SubscriptionMode.COMPETING))
        return success()

    async def handle_broadcast(self, ctx: Context, envelope: Envelope, attempt: int) -> Result[Unit]:
        self.events.append((envelope.id, attempt, SubscriptionMode.BROADCAST))
        return success()


CATALOG = Catalog(
    {
        "en-US": {"probe.text.missing": "The text is missing.", "probe.configuration.invalid": "Bad configuration."},
        "zh-CN": {"probe.text.missing": "缺少文本。"},
    }
)


def build(probe: Probe, **options: Any) -> Plugin:
    registry = operation.Registry()
    registry.add(
        operation.Contract("probes.echo", stability=Stability.EXPERIMENTAL, expose=(operation.EXPOSE_HTTP,)),
        probe.echo,
        request_type=EchoRequest,
        response_type=EchoResponse,
        schema=operation.Schema('{"type":"object"}', '{"type":"object"}'),
    )
    registry.add(operation.Contract("probes.wait"), probe.wait, request_type=Empty, response_type=Waited)
    registry.add(
        operation.Contract("probes.callback", permission="probes.call"),
        probe.callback,
        request_type=CallbackRequest,
        response_type=CallbackResponse,
    )
    registry.add(operation.Contract("probes.explode"), probe.explode, request_type=Empty, response_type=Empty)
    defaults: dict[str, Any] = {
        "operations": registry,
        "subscriptions": [
            SubscriptionHandler(Subscription("probe.events"), probe.handle),
            SubscriptionHandler(
                Subscription("probe.events", "probe.cleared", SubscriptionMode.BROADCAST), probe.handle_broadcast
            ),
        ],
        "catalogs": {"probe": CATALOG},
        "hooks": Hooks(configure=probe.configure, stop=probe.stop, health=probe.health),
    }
    defaults.update(options)
    return Plugin(manifest_text(), **defaults)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """在新的事件循环里跑一个场景，整体限时。"""

    async def bounded() -> None:
        async with asyncio.timeout(60):
            await scenario()

    asyncio.run(bounded())


def invoke(operation_id: str, payload: bytes = b"{}", **baggage: str) -> pb.InvokeRequest:
    return pb.InvokeRequest(
        operation=operation_id,
        payload=payload,
        metadata=pb.InvocationMetadata(trace_parent=TRACE_PARENT, baggage=baggage),
    )


def test_the_handshake_line_is_the_only_stdout_output_and_ends_with_a_line_feed() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.start()
            assert plugin.line.endswith(b"\n")
            assert not plugin.line.endswith(b"\r\n")
            assert plugin.line.count(b"\n") == 1
            assert plugin.line.decode().startswith(f"xihan-plugin/1 {Network.UNIX} ")

    run(scenario)


def test_tcp_listens_on_loopback_only() -> None:
    async def scenario() -> None:
        async with running(build(Probe(), network=Network.TCP)) as plugin:
            assert plugin.endpoint.network is Network.TCP
            assert plugin.endpoint.address.startswith("127.0.0.1:")
            await plugin.handshake()

    run(scenario)


def test_calls_without_the_session_token_are_refused_before_anything_runs() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            for metadata in ((), session("wrong"), (*session(), *session())):
                with pytest.raises(grpc.aio.AioRpcError) as refused:
                    await plugin.stub.Handshake(handshake_request(), metadata=metadata)
                assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED
            with pytest.raises(grpc.aio.AioRpcError) as streamed:
                await anext(aiter(plugin.stub.WatchHealth(pb.HealthRequest(), metadata=session("wrong"))))
            assert streamed.value.code() is grpc.StatusCode.UNAUTHENTICATED
            # 被拒的调用没有执行：握手仍然可以完成
            response = await plugin.handshake()
            assert response.protocol_version == 1

    run(scenario)


def test_the_handshake_negotiates_and_declares_the_contributions() -> None:
    async def scenario() -> None:
        plugin_definition = build(Probe())
        async with running(plugin_definition) as plugin:
            with pytest.raises(grpc.aio.AioRpcError) as refused:
                await plugin.stub.Handshake(handshake_request(7, 8), metadata=session())
            assert refused.value.code() is grpc.StatusCode.FAILED_PRECONDITION
            response = await plugin.stub.Handshake(handshake_request(1, 2), metadata=session())
            assert response.protocol_version == 1
            assert response.primitives == 1
            assert response.manifest == plugin_definition.manifest_text
            assert (response.sdk.name, response.sdk.language) == ("framework-sdk-python", "python")
            declared = {declaration.id: declaration for declaration in response.operations}
            assert sorted(declared) == ["probes.callback", "probes.echo", "probes.explode", "probes.wait"]
            echo = declared["probes.echo"]
            assert (echo.stability, list(echo.expose), echo.request_schema) == (
                "experimental",
                ["http"],
                '{"type":"object"}',
            )
            assert declared["probes.callback"].permission == "probes.call"
            assert not any(declaration.streaming for declaration in response.operations)
            subscriptions = [(s.topic, s.message_type, s.mode) for s in response.subscriptions]
            assert subscriptions == [("probe.events", "", ""), ("probe.events", "probe.cleared", "broadcast")]
            [catalog] = response.catalogs
            assert catalog.name == "probe"
            assert [locale.locale for locale in catalog.locales] == ["en-US", "zh-CN"]
            # 握手只做一次
            with pytest.raises(grpc.aio.AioRpcError) as again:
                await plugin.handshake()
            assert again.value.code() is grpc.StatusCode.FAILED_PRECONDITION

    run(scenario)


def test_configure_start_health_and_watch_health() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            health = await plugin.stub.Health(pb.HealthRequest(), metadata=session())
            assert health.state == pb.LIFECYCLE_STATE_DECLARED
            with pytest.raises(grpc.aio.AioRpcError) as early:
                await plugin.stub.Configure(pb.ConfigureRequest(configuration="{}"), metadata=session())
            assert early.value.code() is grpc.StatusCode.FAILED_PRECONDITION
            await plugin.handshake()
            configured = await plugin.stub.Configure(
                pb.ConfigureRequest(
                    configuration='{"greeting":"hi"}',
                    granted_permissions=["capability:kernel.pipeline"],
                    bindings=[pb.Binding(name="db", type="connection:relational", value="Host=x")],
                    work_directory="work",
                ),
                metadata=session(),
            )
            assert configured.status.ok
            assert probe.configuration is not None
            assert probe.configuration.section == {"greeting": "hi"}
            assert probe.configuration.granted_permissions == ("capability:kernel.pipeline",)
            assert probe.configuration.bindings[0].name == "db"
            assert probe.configuration.work_directory == "work"
            started = await plugin.stub.Start(pb.StartRequest(), metadata=session())
            assert started.status.ok
            health = await plugin.stub.Health(pb.HealthRequest(), metadata=session())
            assert health.state == pb.LIFECYCLE_STATE_READY
            assert [(item.name, item.status) for item in health.items] == [("probe", pb.HEALTH_STATUS_HEALTHY)]

            stream = plugin.stub.WatchHealth(pb.HealthRequest(), metadata=session())
            first = await anext(aiter(stream))
            assert first.state == pb.LIFECYCLE_STATE_READY
            # 健康回调报 Degraded：状态按迁移表变过去，推送一条；没有声明的健康项不上报
            probe.health_state = LifecycleState.DEGRADED
            probe.health_items = [
                HealthItem("probe", HealthStatus.DEGRADED),
                HealthItem("undeclared", HealthStatus.HEALTHY),
            ]
            second = await anext(aiter(stream))
            assert second.state == pb.LIFECYCLE_STATE_DEGRADED
            assert [item.name for item in second.items] == ["probe"]
            stream.cancel()

    run(scenario)


def test_configure_failures_are_statuses_and_leave_the_plugin_failed() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.handshake()
            response = await plugin.stub.Configure(
                pb.ConfigureRequest(configuration='{"fail":true}'), metadata=session()
            )
            assert not response.status.ok
            assert response.status.error.code == "PROBE_VALIDATION_002"
            # 文案按插件的消息目录解析，宿主再按键翻译
            assert response.status.error.text == "Bad configuration."
            health = await plugin.stub.Health(pb.HealthRequest(), metadata=session())
            assert health.state == pb.LIFECYCLE_STATE_FAILED
            assert health.error.code == "PROBE_VALIDATION_002"

        async with running(build(Probe())) as plugin:
            await plugin.handshake()
            response = await plugin.stub.Configure(pb.ConfigureRequest(configuration="[1]"), metadata=session())
            assert response.status.error.code == ERROR_CODE_UNEXPECTED
            assert response.status.error.message_key == "plugins.process.failed"
            assert json.loads(response.status.error.details)["phase"] == "Configure"

    run(scenario)


def test_hook_exceptions_become_unexpected_failures_without_the_exception_text() -> None:
    async def explode() -> Result[Unit]:
        raise RuntimeError("secret detail")

    async def scenario() -> None:
        async with running(build(Probe(), hooks=Hooks(start=explode))) as plugin:
            await plugin.handshake()
            await plugin.stub.Configure(pb.ConfigureRequest(), metadata=session())
            response = await plugin.stub.Start(pb.StartRequest(), metadata=session())
            assert response.status.error.code == ERROR_CODE_UNEXPECTED
            assert "secret detail" not in response.status.error.text
            assert json.loads(response.status.error.details) == {
                "id": "xihan.tests.probe",
                "phase": "Start",
                "reason": "unhandled RuntimeError",
            }

    run(scenario)


def test_calls_before_start_are_unavailable_and_unknown_operations_are_unimplemented() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.handshake()
            early = await plugin.stub.Invoke(invoke("probes.echo"), metadata=session())
            assert early.status.error.code == ERROR_CODE_UNAVAILABLE
            assert json.loads(early.status.error.details) == {"id": "xihan.tests.probe", "state": "Resolved"}
            event = await plugin.stub.HandleEvent(pb.HandleEventRequest(attempt=1), metadata=session())
            assert event.status.error.code == ERROR_CODE_UNAVAILABLE
            await plugin.stub.Configure(pb.ConfigureRequest(), metadata=session())
            await plugin.stub.Start(pb.StartRequest(), metadata=session())
            with pytest.raises(grpc.aio.AioRpcError) as missing:
                await plugin.stub.Invoke(invoke("probes.missing"), metadata=session())
            assert missing.value.code() is grpc.StatusCode.UNIMPLEMENTED

    run(scenario)


def test_invoke_runs_the_operation_through_the_invoker_with_the_restored_context() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.start()
            deadline = datetime.now(UTC) + timedelta(seconds=30)
            request = invoke(
                "probes.echo",
                b'{"text":"hi"}',
                **{"xihan.tenant": "t1", "xihan.correlation": "c1", "xihan.custom": "dropped"},
            )
            request.metadata.deadline = deadline.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            response = await plugin.stub.Invoke(request, metadata=session(), timeout=60)
            assert response.status.ok
            echoed = json.loads(response.payload)
            assert (echoed["text"], echoed["tenant"], echoed["correlation"]) == ("hi", "t1", "c1")
            assert echoed["traceId"] == "0af7651916cd43dd8448eb211c80319c"
            # 元数据的截止时间早于 gRPC 的截止时间：取较早的那个
            assert datetime.fromisoformat(echoed["deadline"]) == deadline

            # 业务失败走 Status.error：码、键、细节与按插件目录解析的文案
            failed = await plugin.stub.Invoke(invoke("probes.echo"), metadata=session())
            assert not failed.status.ok
            assert failed.payload == b""
            error = failed.status.error
            assert (error.code, error.message_key, error.text) == (
                "PROBE_VALIDATION_001",
                "probe.text.missing",
                "The text is missing.",
            )
            assert json.loads(error.details) == {"field": "text"}

            # 载荷不合法与处理器抛出的异常都是调用器的内核错误
            invalid = await plugin.stub.Invoke(invoke("probes.echo", b'{"text":1}'), metadata=session())
            assert invalid.status.error.code == "KERNEL_VALIDATION_001"
            exploded = await plugin.stub.Invoke(invoke("probes.explode"), metadata=session())
            assert exploded.status.error.code == "KERNEL_UNEXPECTED_001"
            assert exploded.status.error.message_key == "kernel.invocation.unhandled_exception"

    run(scenario)


def test_cancelling_a_call_only_signals_the_handler() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = plugin.stub.Invoke(invoke("probes.wait"), metadata=session())
            await asyncio.wait_for(probe.waiting.wait(), 10)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            # 处理器没有被取消：它看到信号后照常返回（结果已经没有人收）
            probe.waiting.clear()
            follow_up = await plugin.stub.Invoke(invoke("probes.echo", b'{"text":"still serving"}'), metadata=session())
            assert follow_up.status.ok

    run(scenario)


def test_events_are_delivered_by_topic_mode_and_type() -> None:
    probe = Probe()

    def deliver(identifier: str, mode: str, message_type: str = "probe.created") -> pb.HandleEventRequest:
        return pb.HandleEventRequest(
            envelope=pb.Envelope(
                topic="probe.events",
                id=identifier,
                source="urn:test",
                type=message_type,
                spec_version="1.0",
                data=b"{}",
            ),
            metadata=pb.InvocationMetadata(trace_parent=TRACE_PARENT),
            attempt=2,
            mode=mode,
        )

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            for request in (deliver("a", "competing"), deliver("b", ""), deliver("c", "competing", "probe.cleared")):
                response = await plugin.stub.HandleEvent(request, metadata=session())
                assert response.status.ok, response.status
            cleared = await plugin.stub.HandleEvent(deliver("d", "broadcast", "probe.cleared"), metadata=session())
            assert cleared.status.ok
            # 以广播投递的其他类型没有订阅：返回失败，不改交给竞争消费的处理器（invocation.md §4）
            unmatched = await plugin.stub.HandleEvent(deliver("e", "broadcast"), metadata=session())
            assert unmatched.status.error.code == ERROR_CODE_UNEXPECTED
            assert probe.events == [
                ("a", 2, SubscriptionMode.COMPETING),
                ("b", 2, SubscriptionMode.COMPETING),
                ("c", 2, SubscriptionMode.COMPETING),
                ("d", 2, SubscriptionMode.BROADCAST),
            ]

    run(scenario)


def test_invoke_stream_checks_the_frame_order() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.start()
            for frame, code in (
                (pb.StreamFrame(chunk=b"{}"), grpc.StatusCode.INVALID_ARGUMENT),
                (pb.StreamFrame(start=pb.InvokeRequest(operation="probes.echo")), grpc.StatusCode.UNIMPLEMENTED),
            ):
                stream = plugin.stub.InvokeStream(iter([frame]), metadata=session())
                with pytest.raises(grpc.aio.AioRpcError) as ended:
                    await anext(aiter(stream))
                assert ended.value.code() is code

    run(scenario)


def test_stop_waits_for_in_flight_calls_then_releases_resources_and_exits() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = asyncio.ensure_future(plugin.stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            stop = asyncio.ensure_future(
                plugin.stub.Stop(pb.StopRequest(grace_seconds=5, reason="unload"), metadata=session())
            )
            await asyncio.sleep(0.2)
            # 停止期间拒绝新调用，在途调用照常完成
            refused = await plugin.stub.Invoke(invoke("probes.echo", b'{"text":"x"}'), metadata=session())
            assert refused.status.error.code == ERROR_CODE_UNAVAILABLE
            assert not stop.done()
            probe.release.set()
            waited = await call
            assert waited.status.ok
            assert json.loads(waited.payload) == {"cancelled": False}
            stopped = await stop
            assert stopped.status.ok
            assert probe.stopped == [("unload", timedelta(seconds=5))]
            await asyncio.wait_for(asyncio.shield(plugin.task), 10)

    run(scenario)


def test_closing_stdin_signals_in_flight_calls_and_stops_the_plugin() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = asyncio.ensure_future(plugin.stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            plugin.close_stdin()
            # 宿主已经离开：在途调用的信号立即触发，它收尾后插件退出
            waited = await call
            assert json.loads(waited.payload) == {"cancelled": True}
            await asyncio.wait_for(asyncio.shield(plugin.task), 10)
            assert probe.stopped == [("shutdown", timedelta(seconds=5))]

    run(scenario)


def test_callbacks_carry_the_session_token_and_only_the_handed_tenant() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with fake_host() as (host, endpoint), running(build(probe), env_with_host(endpoint)) as plugin:
            await plugin.start()
            request = invoke(
                "probes.callback",
                json.dumps({"envelopeTenants": [None, "t1", "t2"]}).encode(),
                **{"xihan.tenant": "t1", "xihan.principal": "p1"},
            )
            response = await plugin.stub.Invoke(request, metadata=session())
            assert response.status.ok, response.status
            assert json.loads(response.payload) == {
                "invoked": "ok",
                "published": ["ok", "ok", ERROR_CODE_CALLBACK_DENIED],
            }
            [(invoked, metadata)] = host.invocations
            assert metadata[SESSION_METADATA_KEY] == TOKEN
            assert invoked.operation == "orders.create"
            assert invoked.payload == b'{"n":1}'
            assert invoked.metadata.baggage["xihan.tenant"] == "t1"
            assert invoked.metadata.trace_parent.startswith("00-0af7651916cd43dd8448eb211c80319c-")
            [(published, _)] = host.publications
            # 越界的那一项没有发出，其余照常发布
            assert [envelope.id for envelope in published.envelopes] == ["e0", "e1"]

            # 回调带别的租户：不发出调用，直接拒绝
            other = invoke("probes.callback", b'{"tenant":"t2"}', **{"xihan.tenant": "t1"})
            denied = await plugin.stub.Invoke(other, metadata=session())
            assert json.loads(denied.payload)["invoked"] == ERROR_CODE_CALLBACK_DENIED
            assert len(host.invocations) == 1

            # 调用结束之后在后台回调：宿主已经没有交给插件的调用，不能带租户
            assert probe.saved is not None
            saved_host = host_of(probe.saved)
            assert saved_host is not None
            late = await saved_host.invoke(probe.saved, "orders.create")
            assert late.outcome.error is not None
            assert late.outcome.error.message_key == MESSAGE_CALLBACK_TENANT_DENIED
            assert len(host.invocations) == 1

    run(scenario)


def test_callback_transport_failures_are_mapped_to_primitive_errors() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with fake_host() as (host, endpoint), running(build(probe), env_with_host(endpoint)) as plugin:
            await plugin.start()
            for status, code in (
                (grpc.StatusCode.UNAUTHENTICATED, ERROR_CODE_CALLBACK_DENIED),
                (grpc.StatusCode.PERMISSION_DENIED, ERROR_CODE_CALLBACK_DENIED),
                (grpc.StatusCode.UNIMPLEMENTED, "KERNEL_NOTFOUND_001"),
                (grpc.StatusCode.UNAVAILABLE, ERROR_CODE_UNAVAILABLE),
                (grpc.StatusCode.INTERNAL, ERROR_CODE_UNEXPECTED),
            ):
                host.status = status
                response = await plugin.stub.Invoke(invoke("probes.callback", b"{}"), metadata=session())
                assert json.loads(response.payload)["invoked"] == code, status

    run(scenario)


def test_without_a_host_endpoint_there_is_no_host_client() -> None:
    seen: list[object] = []

    async def capture(ctx: Context, request: Empty) -> Result[Empty]:
        seen.append(host_of(ctx))
        return ok(Empty())

    registry = operation.Registry()
    registry.add(operation.Contract("probes.wait"), capture, request_type=Empty, response_type=Empty)

    async def scenario() -> None:
        async with running(build(Probe(), operations=registry)) as plugin:
            await plugin.start()
            response = await plugin.stub.Invoke(invoke("probes.wait"), metadata=session())
            assert response.status.ok
        assert seen == [None]

    run(scenario)


def test_a_missing_session_token_is_a_configuration_error() -> None:
    async def scenario() -> None:
        from xihan_framework.primitives import FrameworkError

        with pytest.raises(FrameworkError, match="XIHAN_PLUGIN_SESSION"):
            await build(Probe()).serve(environ={}, stdin=None)

    run(scenario)
