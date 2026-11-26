"""插件运行时的测试：在本进程里运行插件，测试扮演宿主（与假的 Host 服务），走 handshake.md、lifecycle.md §7 与
invocation.md 的各个环节。"""

# grpc 存根里的泛型与只在类型检查时存在的 PluginAsyncStub：注解推迟求值
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pprint
import re
import tempfile
import time
from collections.abc import Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import grpc
import pytest

from plugin_support import (
    PIPE,
    TOKEN,
    env_with_host,
    environment,
    fake_host,
    handshake_request,
    manifest_text,
    run_scenario,
    running,
    session,
)
from xihan_framework import operation
from xihan_framework.invocation import (
    ERROR_CODE_CANCELLED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    MESSAGE_CANCELLED,
    MESSAGE_DEADLINE_EXCEEDED,
    Cancellation,
    Context,
)
from xihan_framework.plugin import (
    ENV_HOST_ADDRESS,
    ERROR_CODE_CALLBACK_DENIED,
    ERROR_CODE_UNAVAILABLE,
    ERROR_CODE_UNEXPECTED,
    EXTENSION_TENANT,
    MESSAGE_CALLBACK_DENIED,
    MESSAGE_CALLBACK_TENANT_DENIED,
    MESSAGE_EVENT_UNSUBSCRIBED,
    MESSAGE_FAILED,
    MESSAGE_HOST_UNAVAILABLE,
    MESSAGE_SESSION_INVALID,
    MESSAGE_UNAVAILABLE,
    SESSION_METADATA_KEY,
    Binding,
    Configuration,
    Envelope,
    EventHandler,
    Health,
    HealthItem,
    HealthStatus,
    Hooks,
    Network,
    Plugin,
    Subscription,
    SubscriptionHandler,
    SubscriptionMode,
    _runtime,
    find_host,
    host_of,
)
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.primitives import (
    BAGGAGE_TENANT,
    Catalog,
    Error,
    JsonValue,
    LifecycleState,
    Result,
    Stability,
    Unit,
    fail,
    ok,
    success,
)

TRACE_PARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
PROBE_ID = "xihan.tests.probe"
DEADLINE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{7}\+00:00")


@dataclass(frozen=True)
class EchoRequest:
    text: str = ""


@dataclass(frozen=True)
class EchoResponse:
    text: str
    tenant: str | None
    principal: str | None
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
    invoke: bool = True
    envelope_tenants: list[str | None] = field(default_factory=list, metadata=operation.json_field("envelopeTenants"))


@dataclass(frozen=True)
class Outcome:
    """一次回调结果的码、键与细节；成功时都为空。"""

    code: str | None = None
    key: str | None = None
    details: JsonValue = None


@dataclass(frozen=True)
class CallbackResponse:
    invoked: Outcome | None
    published: list[Outcome]


def outcome(result: Result[Any]) -> Outcome:
    error = result.error
    if error is None:
        return Outcome()
    return Outcome(error.code, error.message_key, None if error.details is None else dict(error.details))


#: ``probes.callback`` 发布的第一项信封的时间：东八区的时刻，写出时换算成 UTC。
PUBLISHED_TIME = datetime(2026, 11, 25, 11, 25, 0, 500000, tzinfo=timezone(timedelta(hours=8)))


class Probe:
    """测试插件的状态：记录收到的配置、事件、停止与健康探测，控制等待中的调用与各个回调的结果。"""

    def __init__(self) -> None:
        self.configuration: Configuration | None = None
        self.configure_calls = 0
        self.events: list[tuple[str, int, SubscriptionMode]] = []
        self.stopped: list[tuple[str, timedelta]] = []
        self.stop_result: Result[Unit] = success()
        self.stop_error: Exception | None = None
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.ignore_signal = False
        self.health_state: LifecycleState | None = None
        self.health_items: list[HealthItem] = [HealthItem("probe", HealthStatus.HEALTHY)]
        self.health_error: Exception | None = None
        self.health_calls = 0
        self.saved: Context | None = None

    async def configure(self, configuration: Configuration) -> Result[Unit]:
        self.configure_calls += 1
        self.configuration = configuration
        if configuration.section.get("fail"):
            return fail(Error.validation("PROBE_VALIDATION_002", "probe.configuration.invalid"))
        if configuration.section.get("explode"):
            raise RuntimeError("secret detail C:\\secrets")
        return success()

    async def stop(self, reason: str, grace: timedelta) -> Result[Unit]:
        self.stopped.append((reason, grace))
        if self.stop_error is not None:
            raise self.stop_error
        return self.stop_result

    def health(self) -> Health:
        self.health_calls += 1
        if self.health_error is not None:
            raise self.health_error
        return Health(state=self.health_state, items=self.health_items)

    async def echo(self, ctx: Context, request: EchoRequest) -> Result[EchoResponse]:
        if not request.text:
            return fail(Error.validation("PROBE_VALIDATION_001", "probe.text.missing", {"field": "text"}))
        deadline = None if ctx.deadline is None else ctx.deadline.isoformat()
        return ok(EchoResponse(request.text, ctx.tenant, ctx.principal, ctx.correlation, ctx.trace_id, deadline))

    async def wait(self, ctx: Context, request: Empty) -> Result[Waited]:
        # 取消只经信号通知：处理器看信号自己停下，或者等测试放行；ignore_signal 时不理信号
        self.waiting.set()
        waiters: set[asyncio.Future[Any]] = {asyncio.ensure_future(self.release.wait())}
        if not self.ignore_signal:
            waiters.add(asyncio.ensure_future(ctx.cancellation.wait()))
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for waiter in waiters:
            waiter.cancel()
        return ok(Waited(ctx.is_cancelled))

    async def callback(self, ctx: Context, request: CallbackRequest) -> Result[CallbackResponse]:
        host = host_of(ctx)
        assert find_host(ctx) is host
        self.saved = ctx
        scoped = ctx if request.tenant is None else ctx.with_baggage(BAGGAGE_TENANT, request.tenant)
        invoked = await host.invoke(scoped, "orders.create", b'{"n":1}') if request.invoke else None
        envelopes = [
            Envelope(
                topic="orders.created",
                id=f"e{index}",
                source="urn:probe",
                type="orders.created",
                # 第一项带时间与 dataschema，其余没有
                time=PUBLISHED_TIME if index == 0 else None,
                data_schema="urn:schema:orders" if index == 0 else None,
                extensions={} if tenant is None else {EXTENSION_TENANT: tenant},
            )
            for index, tenant in enumerate(request.envelope_tenants)
        ]
        published = await host.publish(scoped, envelopes)
        return ok(
            CallbackResponse(
                invoked=None if invoked is None else outcome(invoked.outcome),
                published=[outcome(result) for result in published],
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


def build(probe: Probe, manifest: str | None = None, **options: Any) -> Plugin:
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
    return Plugin(manifest_text() if manifest is None else manifest, **defaults)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """在新的事件循环里跑一个场景（``plugin_support.run_scenario``）。"""
    run_scenario(scenario)


def invoke(operation_id: str, payload: bytes = b"{}", **baggage: str) -> pb.InvokeRequest:
    return pb.InvokeRequest(
        operation=operation_id,
        payload=payload,
        metadata=pb.InvocationMetadata(trace_parent=TRACE_PARENT, baggage=baggage),
    )


def deliver(
    topic: str, identifier: str, mode: str, message_type: str = "probe.created", **envelope: Any
) -> pb.HandleEventRequest:
    return pb.HandleEventRequest(
        envelope=pb.Envelope(
            topic=topic, id=identifier, source="urn:test", type=message_type, spec_version="1.0", data=b"{}", **envelope
        ),
        metadata=pb.InvocationMetadata(trace_parent=TRACE_PARENT),
        attempt=2,
        mode=mode,
    )


def details(status: pb.Status) -> Any:
    return json.loads(status.error.details) if status.error.details else None


def expect_failure(status: pb.Status, code: str, key: str, expected: dict[str, Any] | None) -> None:
    """``Status`` 是带消息键的失败：码、键与细节相等，``text`` 留空（invocation.md §3）。"""
    assert not status.ok, status
    assert (status.error.code, status.error.message_key, status.error.text) == (code, key, ""), status
    assert details(status) == expected, status


async def settle(*calls: asyncio.Future[Any]) -> None:
    """取走被放弃的调用的结果：服务端停止时它们以 gRPC 错误结束。"""
    for call in calls:
        call.cancel()
        with contextlib.suppress(BaseException):
            await call


async def expect_code(call: Any, code: grpc.StatusCode) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await call
    assert refused.value.code() is code


async def callback(plugin: Any, request: dict[str, Any], **baggage: str) -> dict[str, Any]:
    """经 ``probes.callback`` 回调宿主，返回插件看到的结果。"""
    response = await plugin.stub.Invoke(
        invoke("probes.callback", json.dumps(request).encode(), **baggage), metadata=session(), timeout=30
    )
    assert response.status.ok, response.status
    result: dict[str, Any] = json.loads(response.payload)
    return result


# 握手与会话令牌（handshake.md §2–§4）


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
            assert re.fullmatch(r"127\.0\.0\.1:[1-9][0-9]*", plugin.endpoint.address)
            await plugin.handshake()

    run(scenario)


def test_a_temporary_directory_with_whitespace_falls_back_to_loopback_tcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 握手行的地址不能有空白（handshake.md §2）：带空格的用户名会让 Unix 域套接字的路径带空格
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "John Smith"))

    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            assert plugin.endpoint.network is Network.TCP
            await plugin.handshake()

    run(scenario)


def test_calls_need_exactly_one_session_token_before_anything_else() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            for metadata in (
                (),
                session("wrong"),
                (*session(), *session("wrong")),
                (*session("wrong"), *session()),
                (*session(), *session()),
            ):
                await expect_code(
                    plugin.stub.Handshake(handshake_request(), metadata=metadata), grpc.StatusCode.UNAUTHENTICATED
                )
            # 令牌先于次序核对：握手之前的 Stop 带错令牌是 Unauthenticated，不是 FailedPrecondition
            await expect_code(
                plugin.stub.Stop(pb.StopRequest(), metadata=session("wrong")), grpc.StatusCode.UNAUTHENTICATED
            )
            # 令牌也先于读流的第一帧：不发帧的流带错令牌是 Unauthenticated，不是 InvalidArgument
            stream = plugin.stub.InvokeStream(iter([]), metadata=session("wrong"))
            await expect_code(anext(aiter(stream)), grpc.StatusCode.UNAUTHENTICATED)
            watch = plugin.stub.WatchHealth(pb.HealthRequest(), metadata=session("wrong"))
            await expect_code(anext(aiter(watch)), grpc.StatusCode.UNAUTHENTICATED)
            # 被拒的调用没有执行：握手仍然可以完成
            response = await plugin.handshake()
            assert response.protocol_version == 1

    run(scenario)


def test_the_handshake_declares_the_contributions_as_written() -> None:
    async def scenario() -> None:
        text = manifest_text()
        plugin_definition = build(Probe(), manifest=chr(0xFEFF) + text)
        async with running(plugin_definition) as plugin:
            await expect_code(
                plugin.stub.Handshake(handshake_request(7, 8), metadata=session()), grpc.StatusCode.FAILED_PRECONDITION
            )
            response = await plugin.stub.Handshake(handshake_request(1, 2), metadata=session())
            assert response.protocol_version == 1
            assert response.primitives == 1
            # 清单原文去掉开头的 BOM
            assert response.manifest == text
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
            # 订阅按声明顺序，方式总写 competing 或 broadcast
            subscriptions = [(s.topic, s.message_type, s.mode) for s in response.subscriptions]
            assert subscriptions == [("probe.events", "", "competing"), ("probe.events", "probe.cleared", "broadcast")]
            # 只发作者自己的目录，运行时的目录由宿主提供
            assert [catalog.name for catalog in response.catalogs] == ["probe"]
            assert [locale.locale for locale in response.catalogs[0].locales] == ["en-US", "zh-CN"]

    run(scenario)


# 调用次序（lifecycle.md §7.1）


def test_calls_out_of_order_are_refused_without_changing_anything() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            stub = plugin.stub
            refused = grpc.StatusCode.FAILED_PRECONDITION
            await expect_code(plugin.configure(), refused)
            await expect_code(stub.Start(pb.StartRequest(), metadata=session()), refused)
            await expect_code(plugin.stop(), refused)
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_DECLARED
            await plugin.handshake()
            await expect_code(plugin.handshake(), refused)
            await expect_code(stub.Start(pb.StartRequest(), metadata=session()), refused)
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_RESOLVED
            assert (await plugin.configure()).status.ok
            await expect_code(plugin.configure(), refused)
            assert probe.configure_calls == 1
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_CONFIGURED
            assert (await stub.Start(pb.StartRequest(), metadata=session())).status.ok
            await expect_code(stub.Start(pb.StartRequest(), metadata=session()), refused)
            await expect_code(plugin.configure(), refused)
            await expect_code(plugin.handshake(), refused)
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_READY

            # 停止中再 Stop：第一次还在等在途调用
            call = asyncio.ensure_future(stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            first = asyncio.ensure_future(plugin.stop(grace_seconds=30))
            await asyncio.sleep(0.3)
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_DRAINING
            await expect_code(plugin.stop(), refused)
            probe.release.set()
            assert (await call).status.ok
            assert (await first).status.ok
            await plugin.exited()
            assert probe.stopped == [("unload", timedelta(seconds=30))]

    run(scenario)


# 配置（lifecycle.md §7.2）


@pytest.mark.parametrize("configuration", ["[]", '{"a":', '{"a":1,"a":2}', '"x"', "null", chr(0xFEFF) + "{}"])
def test_configurations_that_are_not_json_objects_fail_without_calling_the_hook(configuration: str) -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.handshake()
            response = await plugin.configure(configuration)
            expect_failure(
                response.status,
                ERROR_CODE_UNEXPECTED,
                MESSAGE_FAILED,
                {"id": PROBE_ID, "phase": "configure", "reason": "the configuration is not a JSON object"},
            )
            assert probe.configure_calls == 0
            health = await plugin.health()
            assert health.state == pb.LIFECYCLE_STATE_FAILED
            assert health.error.message_key == MESSAGE_FAILED
            # 启动回调没有成功过：Stop 应答 ok，不调停止回调
            assert (await plugin.stop()).status.ok
            await plugin.exited()
            assert probe.stopped == []

    run(scenario)


def test_configurations_are_handed_over_and_read_with_types() -> None:
    probe = Probe()

    @dataclass(frozen=True)
    class Settings:
        greeting: str
        retries: int = 0
        enabled: bool = False

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.handshake()
            # 宿主写出的叶子是字符串，成员名大小写随配置源
            response = await plugin.configure(
                '{"Greeting":"hi","RETRIES":"3","Enabled":"True"}',
                granted_permissions=["capability:kernel.pipeline"],
                bindings=[pb.Binding(name="db", type="connection:relational", value="Host=x")],
            )
            assert response.status.ok
            configuration = probe.configuration
            assert configuration is not None
            assert configuration.section == {"Greeting": "hi", "RETRIES": "3", "Enabled": "True"}
            assert configuration.decode(Settings) == Settings("hi", 3, True)
            assert configuration.granted_permissions == ("capability:kernel.pipeline",)
            # 请求里没有 properties（按较早的规范实现的宿主不发）：属性是空映射，不是没有
            assert configuration.bindings == (Binding("db", "connection:relational", "Host=x", {}),)
            assert configuration.work_directory == "."

        async with running(build(probe)) as plugin:
            await plugin.handshake()
            # 空的配置节等同 {}
            assert (await plugin.configure("")).status.ok
            assert probe.configuration is not None
            assert probe.configuration.section == {}

    run(scenario)


def test_bindings_follow_the_reading_vectors(vector: Callable[[str, str], Any]) -> None:
    # binding-properties.json 的 reading：宿主写出的 ConfigureRequest 字节原样发给插件（不经本 SDK 的编码），插件一侧由生成的
    # 消息类型解码、走配置这条路；绑定按先后逐项比较，属性按属性名比较。names、limits、delivery 与 json 是宿主一侧的
    cases = vector("plugin", "binding-properties")["reading"]
    assert cases

    async def scenario() -> None:
        for case in cases:
            data = bytes.fromhex(case["hex"])
            decoded = pb.ConfigureRequest.FromString(data)
            assert [binding.name for binding in decoded.bindings] == [binding["name"] for binding in case["bindings"]]
            probe = Probe()
            async with running(build(probe)) as plugin:
                await plugin.handshake()
                configure: grpc.aio.UnaryUnaryMultiCallable[bytes, pb.ConfigureResponse] = plugin.channel.unary_unary(
                    "/xihan.plugin.v1.Plugin/Configure",
                    request_serializer=None,
                    response_deserializer=pb.ConfigureResponse.FromString,
                )
                response = await configure(data, metadata=session())
                assert response.status.ok, case["name"]
                assert probe.configuration is not None, case["name"]
                delivered = [
                    {
                        "name": binding.name,
                        "type": binding.type,
                        "value": binding.value,
                        "properties": dict(binding.properties),
                    }
                    for binding in probe.configuration.bindings
                ]
                assert delivered == case["bindings"], case["name"]

    run(scenario)


def test_binding_properties_are_handed_over_as_sent() -> None:
    probe = Probe()
    # 只差大小写的两项、空值、纯数字与对象协议里的名字，以及不合下发文法的名字：SDK 不折叠、不改写、不核对
    odd = {
        "accessKey": "lower",
        "AccessKey": "upper",
        "empty": "",
        "8080": "digits",
        "__class__": "dunder",
        ".hidden": "dot",
        "user name": "space",
    }
    # 属性多过宿主一侧的上限（64 个）、名字长过 253 个字符：那是宿主的事
    many = {f"p{index}": str(index) for index in range(100)} | {"n" * 300: "long"}

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.handshake()
            response = await plugin.configure(
                bindings=[
                    # 主值为空不是错误
                    pb.Binding(name="db", type="connection:relational", properties=odd),
                    pb.Binding(name="many", type="queue", value="amqp://mq.internal", properties=many),
                    # 较早的宿主不发 properties：读到的是空映射
                    pb.Binding(name="cache", type="cache:distributed", value="redis.internal:6379"),
                ],
            )
            assert response.status.ok

    run(scenario)
    assert probe.configuration is not None
    db, queue, cache = probe.configuration.bindings
    assert (db.name, db.type, db.value, db.properties) == ("db", "connection:relational", "", odd)
    assert (db.properties["accessKey"], db.properties["AccessKey"]) == ("lower", "upper")
    assert "accesskey" not in db.properties
    assert db.properties["empty"] == ""
    assert (queue.value, queue.properties) == ("amqp://mq.internal", many)
    assert (cache.value, cache.properties, len(cache.properties)) == ("redis.internal:6379", {}, 0)
    # 交出的是只读映射，也是一份拷贝：作者改不动绑定，之后改动来源也不影响它
    with pytest.raises(TypeError):
        cast("dict[str, str]", db.properties)["accessKey"] = "changed"
    assert db.properties["accessKey"] == "lower"
    source = {"username": "app"}
    copied = Binding("db", "connection:relational", "", source)
    source["username"] = "changed"
    assert copied.properties == {"username": "app"}
    # 原有的三个位置参数照常可用，没有属性时是空映射；绑定照旧可哈希，属性参与相等比较
    plain = Binding("cache", "cache:distributed", "redis.internal:6379")
    assert plain == cache
    assert hash(plain) == hash(cache)
    assert plain.properties == {}
    assert plain != Binding("cache", "cache:distributed", "redis.internal:6379", {"password": "c"})


def test_bindings_never_print_their_values(caplog: pytest.LogCaptureFixture) -> None:
    # 主值与属性值可能是凭据：绑定的调试输出只带绑定名、类型与属性名，运行时的日志也不带（lifecycle.md §7.2）
    probe = Probe()
    secrets = ("postgres://app:hunter2@db.internal/app", "app-user-7f3a", "s3cret-9d1c")
    value, username, password = secrets

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.handshake()
            response = await plugin.configure(
                bindings=[
                    pb.Binding(
                        name="db",
                        type="connection:relational",
                        value=value,
                        properties={"username": username, "password": password},
                    )
                ],
            )
            assert response.status.ok
            started = await plugin.stub.Start(pb.StartRequest(), metadata=session())
            assert started.status.ok
            assert (await plugin.stop()).status.ok
            await plugin.exited()

    with caplog.at_level(logging.DEBUG):
        run(scenario)
        assert probe.configuration is not None
        (binding,) = probe.configuration.bindings
        # 作者照常读得到值
        assert (binding.value, binding.properties["password"]) == (value, password)
        logging.getLogger("xihan.tests.probe").info("Configured %s with %r.", binding, probe.configuration)
    assert repr(binding) == "Binding(name='db', type='connection:relational', property_names=['password', 'username'])"
    outputs = {
        "repr(binding)": repr(binding),
        "str(binding)": str(binding),
        "f-string": f"{binding} {binding!r}",
        "repr(bindings)": repr(probe.configuration.bindings),
        "repr(configuration)": repr(probe.configuration),
        "pprint(binding)": pprint.pformat(binding, width=20),
        "pprint(configuration)": pprint.pformat(probe.configuration, width=20),
        "log messages": "\n".join(record.getMessage() for record in caplog.records),
        "log records": "\n".join(repr(record.__dict__) for record in caplog.records),
    }
    assert "Stopped." in outputs["log messages"]
    for name, text in outputs.items():
        for secret in secrets:
            assert secret not in text, f"{name} must not carry '{secret}': {text}"
    for name in ("repr(configuration)", "pprint(configuration)", "log messages"):
        for expected in ("db", "connection:relational", "password", "username"):
            assert expected in outputs[name], f"{name} must carry '{expected}': {outputs[name]}"


def test_configure_failures_and_exceptions_leave_the_plugin_failed() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.handshake()
            response = await plugin.configure('{"fail":true}')
            # 带键的错误不按任何区域渲染，宿主按目录翻译
            expect_failure(response.status, "PROBE_VALIDATION_002", "probe.configuration.invalid", None)
            health = await plugin.health()
            assert health.state == pb.LIFECYCLE_STATE_FAILED
            assert health.error.code == "PROBE_VALIDATION_002"

        async with running(build(Probe())) as plugin:
            await plugin.handshake()
            response = await plugin.configure('{"explode":true}')
            # 回调抛出：reason 是异常的简单类型名，异常文本只进 stderr
            expect_failure(
                response.status,
                ERROR_CODE_UNEXPECTED,
                MESSAGE_FAILED,
                {"id": PROBE_ID, "phase": "configure", "reason": "RuntimeError"},
            )
            assert "secret" not in str(response)
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_FAILED

    run(scenario)


# 启动与就绪（lifecycle.md §7.3）


@pytest.mark.parametrize(
    ("reported", "error", "hook", "expected"),
    [
        (None, None, True, pb.LIFECYCLE_STATE_READY),
        (LifecycleState.STARTED, None, True, pb.LIFECYCLE_STATE_READY),
        (LifecycleState.DEGRADED, None, True, pb.LIFECYCLE_STATE_DEGRADED),
        (LifecycleState.FAILED, None, True, pb.LIFECYCLE_STATE_FAILED),
        (None, ValueError("probe"), True, pb.LIFECYCLE_STATE_DEGRADED),
        (None, None, False, pb.LIFECYCLE_STATE_READY),
    ],
)
def test_start_answers_after_the_first_health_probe(
    reported: LifecycleState | None, error: Exception | None, hook: bool, expected: int
) -> None:
    probe = Probe()
    probe.health_state = reported
    probe.health_error = error
    hooks = Hooks(configure=probe.configure, stop=probe.stop, health=probe.health if hook else None)

    async def scenario() -> None:
        async with running(build(probe, hooks=hooks)) as plugin:
            started = await plugin.start()
            assert started.status.ok
            assert probe.health_calls == (1 if hook else 0)
            health = await plugin.stub.Health(pb.HealthRequest(), metadata=session())
            assert health.state == expected
            if error is not None:
                assert details(pb.Status(error=health.error)) == {
                    "id": PROBE_ID,
                    "phase": "health",
                    "reason": "ValueError",
                }
                assert list(health.items) == []
            if expected == pb.LIFECYCLE_STATE_FAILED:
                # 只有 Ready 与 Degraded 可服务
                response = await plugin.stub.Invoke(invoke("probes.echo", b'{"text":"x"}'), metadata=session())
                expect_failure(
                    response.status, ERROR_CODE_UNAVAILABLE, MESSAGE_UNAVAILABLE, {"id": PROBE_ID, "state": "Failed"}
                )

    run(scenario)


def test_start_failures_leave_the_plugin_failed_and_skip_the_stop_hook() -> None:
    probe = Probe()

    async def explode() -> Result[Unit]:
        raise RuntimeError("secret detail")

    async def scenario() -> None:
        hooks = Hooks(start=explode, stop=probe.stop, health=probe.health)
        async with running(build(probe, hooks=hooks)) as plugin:
            await plugin.handshake()
            await plugin.configure()
            response = await plugin.stub.Start(pb.StartRequest(), metadata=session())
            expect_failure(
                response.status,
                ERROR_CODE_UNEXPECTED,
                MESSAGE_FAILED,
                {"id": PROBE_ID, "phase": "start", "reason": "RuntimeError"},
            )
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_FAILED
            assert probe.health_calls == 0
            assert (await plugin.stop()).status.ok
            await plugin.exited()
        assert probe.stopped == []

    run(scenario)


# 健康（lifecycle.md §7.4）


def test_health_before_start_reports_the_local_state_without_the_hook() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            for step in (None, plugin.handshake, plugin.configure):
                if step is not None:
                    await step()
                health = await plugin.health()
                assert list(health.items) == []
                assert not health.HasField("error")
            watch = plugin.stub.WatchHealth(pb.HealthRequest(), metadata=session())
            first = await anext(aiter(watch))
            assert first.state == pb.LIFECYCLE_STATE_CONFIGURED
            watch.cancel()
            assert probe.health_calls == 0

    run(scenario)


def test_health_while_serving_probes_the_hook_every_time(caplog: pytest.LogCaptureFixture) -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            calls = probe.health_calls
            health = await plugin.health()
            assert probe.health_calls == calls + 1
            assert [(item.name, item.status) for item in health.items] == [("probe", pb.HEALTH_STATUS_HEALTHY)]
            # 未声明的健康项丢掉，同一个名字只告警一次
            probe.health_items = [
                HealthItem("probe", HealthStatus.DEGRADED),
                HealthItem("undeclared", HealthStatus.HEALTHY),
            ]
            for _ in range(3):
                health = await plugin.health()
                assert [item.name for item in health.items] == ["probe"]
            warnings = [record for record in caplog.records if "undeclared" in record.getMessage()]
            assert len(warnings) == 1
            assert warnings[0].levelno == logging.WARNING
            # 回调抛出：这一次是 Degraded，没有健康项，状态也迁到 Degraded；调用照常服务（Degraded 可服务）
            probe.health_error = KeyError("x")
            thrown = await plugin.health()
            assert thrown.state == pb.LIFECYCLE_STATE_DEGRADED
            assert list(thrown.items) == []
            assert details(pb.Status(error=thrown.error)) == {"id": PROBE_ID, "phase": "health", "reason": "KeyError"}
            echoed = await plugin.stub.Invoke(invoke("probes.echo", b'{"text":"x"}'), metadata=session())
            assert echoed.status.ok
            # 之后的结果照常决定状态：不报状态的不改变状态，报 Ready 的回到 Ready
            probe.health_error = None
            kept = await plugin.health()
            assert (kept.state, [item.name for item in kept.items]) == (pb.LIFECYCLE_STATE_DEGRADED, ["probe"])
            probe.health_state = LifecycleState.READY
            recovered = await plugin.health()
            assert recovered.state == pb.LIFECYCLE_STATE_READY
            assert not recovered.HasField("error")
            # 回调报的 Degraded 生效；报其他状态不改变状态
            probe.health_state = LifecycleState.DEGRADED
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_DEGRADED
            probe.health_state = LifecycleState.DRAINING
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_DEGRADED
            # Failed 之后不再回来，也不再调回调
            probe.health_state = LifecycleState.FAILED
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_FAILED
            probe.health_state = LifecycleState.READY
            calls = probe.health_calls
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_FAILED
            assert probe.health_calls == calls

    run(scenario)


def test_watch_health_pushes_changes_and_polls_until_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Probe()
    monkeypatch.setattr(_runtime, "_HEALTH_POLL_SECONDS", 0.2)

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            stream = plugin.stub.WatchHealth(pb.HealthRequest(), metadata=session())
            pushes = aiter(stream)
            first = await anext(pushes)
            assert first.state == pb.LIFECYCLE_STATE_READY
            # 健康项的变化只有求回调才知道：定时重新求一次，变化了才推送
            probe.health_items = [HealthItem("probe", HealthStatus.DEGRADED)]
            second = await asyncio.wait_for(anext(pushes), 10)
            assert (second.state, second.items[0].status) == (pb.LIFECYCLE_STATE_READY, pb.HEALTH_STATUS_DEGRADED)
            probe.health_state = LifecycleState.DEGRADED
            third = await asyncio.wait_for(anext(pushes), 10)
            assert third.state == pb.LIFECYCLE_STATE_DEGRADED
            # 进入 Draining 推一次，应答 Stop 之后流结束
            stopped = asyncio.ensure_future(plugin.stop())
            draining = await asyncio.wait_for(anext(pushes), 10)
            assert draining.state == pb.LIFECYCLE_STATE_DRAINING
            assert (await stopped).status.ok
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(pushes), 10)
            await plugin.exited()

    run(scenario)


# 调用（invocation.md §1–§3）


def test_undeclared_operations_are_unimplemented_before_the_plugin_is_serving() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            for state, step in (("Declared", None), ("Resolved", plugin.handshake), ("Configured", plugin.configure)):
                if step is not None:
                    await step()
                await expect_code(
                    plugin.stub.Invoke(invoke("probes.missing"), metadata=session()), grpc.StatusCode.UNIMPLEMENTED
                )
                early = await plugin.stub.Invoke(invoke("probes.echo"), metadata=session())
                expect_failure(
                    early.status, ERROR_CODE_UNAVAILABLE, MESSAGE_UNAVAILABLE, {"id": PROBE_ID, "state": state}
                )
            await plugin.stub.Start(pb.StartRequest(), metadata=session())
            await expect_code(
                plugin.stub.Invoke(invoke("probes.missing"), metadata=session()), grpc.StatusCode.UNIMPLEMENTED
            )

    run(scenario)


def test_invoke_runs_the_operation_through_the_invoker_with_the_restored_context() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.start()
            deadline = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30)
            request = invoke(
                "probes.echo",
                b'{"text":"hi"}',
                **{
                    "xihan.tenant": "t1",
                    "xihan.principal": "p1",
                    "xihan.correlation": "c1",
                    "xihan.custom": "dropped",
                },
            )
            request.metadata.deadline = deadline.strftime("%Y-%m-%dT%H:%M:%S.0000000+00:00")
            request.metadata.trace_state = "vendor=x"
            response = await plugin.stub.Invoke(request, metadata=session(), timeout=60)
            assert response.status.ok
            echoed = json.loads(response.payload)
            assert (echoed["text"], echoed["tenant"], echoed["principal"], echoed["correlation"]) == (
                "hi",
                "t1",
                "p1",
                "c1",
            )
            assert echoed["traceId"] == "0af7651916cd43dd8448eb211c80319c"
            # 元数据的截止时间早于 gRPC 的截止时间：取较早的那个
            assert datetime.fromisoformat(echoed["deadline"]) == deadline

            # 读不出的截止时间当作没有，生效的是 gRPC 的截止时间
            request.metadata.deadline = "2000-01-01 00:00:00Z"
            later = await plugin.stub.Invoke(request, metadata=session(), timeout=60)
            effective = datetime.fromisoformat(json.loads(later.payload)["deadline"])
            assert timedelta(seconds=50) < effective - datetime.now(UTC) < timedelta(seconds=61)

            # 业务失败走 Status.error：码、键与细节，text 留空
            failed = await plugin.stub.Invoke(invoke("probes.echo"), metadata=session())
            assert failed.payload == b""
            expect_failure(failed.status, "PROBE_VALIDATION_001", "probe.text.missing", {"field": "text"})

            # 载荷不合法与处理器抛出的异常都是调用器的内核错误
            invalid = await plugin.stub.Invoke(invoke("probes.echo", b'{"text":1}'), metadata=session())
            assert invalid.status.error.code == "KERNEL_VALIDATION_001"
            exploded = await plugin.stub.Invoke(invoke("probes.explode"), metadata=session())
            expect_failure(exploded.status, "KERNEL_UNEXPECTED_001", "kernel.invocation.unhandled_exception", None)

    run(scenario)


def test_empty_payloads_are_empty_objects() -> None:
    seen: list[Empty] = []

    async def capture(ctx: Context, request: Empty) -> Result[Empty]:
        seen.append(request)
        return ok(Empty())

    registry = operation.Registry()
    registry.add(operation.Contract("probes.wait"), capture, request_type=Empty, response_type=Empty)

    async def scenario() -> None:
        async with running(build(Probe(), operations=registry)) as plugin:
            await plugin.start()
            response = await plugin.stub.Invoke(pb.InvokeRequest(operation="probes.wait"), metadata=session())
            assert response.status.ok
            assert response.payload == b"{}"
        assert seen == [Empty()]

    run(scenario)


def test_responses_that_are_not_valid_json_text_fail_in_the_plugin() -> None:
    # 类型化的应答写不成合法的 JSON 文本即写不出（invocation.md §1「插件一侧写出应答」，ADR-0318）：值、映射的键与数组元素
    # 里的代理项码点经 Status.error 报 unhandled_exception，没有参数、不带 inner，payload 为空，不写成转义交给宿主
    text = "中文" + chr(0x1F600) + chr(0xFFFD)
    responses: dict[str, object] = {
        "probes.echo": {"text": "a" + chr(0xD800)},
        "probes.wait": {chr(0xDC00): "b"},
        "probes.callback": {"items": ["c", chr(0xD83D) + chr(0xDE00)]},
        "probes.explode": {"text": text},
    }
    registry = operation.Registry()
    for operation_id, value in responses.items():

        async def respond(ctx: Context, request: Empty, value: object = value) -> Result[object]:
            return ok(value)

        registry.add(operation.Contract(operation_id), respond, request_type=Empty, response_type=object)

    async def scenario() -> None:
        async with running(build(Probe(), operations=registry)) as plugin:
            await plugin.start()
            for operation_id in ("probes.echo", "probes.wait", "probes.callback"):
                failed = await plugin.stub.Invoke(invoke(operation_id), metadata=session())
                assert failed.payload == b"", operation_id
                expect_failure(failed.status, "KERNEL_UNEXPECTED_001", "kernel.invocation.unhandled_exception", None)
                assert not failed.status.error.HasField("inner"), operation_id
            # 多字节文本与真正的 U+FFFD 照常写出
            written = await plugin.stub.Invoke(invoke("probes.explode"), metadata=session())
            assert written.status.ok
            assert written.payload == ('{"text":"' + text + '"}').encode("utf-8")

    run(scenario)


class Repeated(Mapping[str, Any]):
    # items() 交出同名的成员：自定义的 to_json_value 可以这样交出 dict 存不下的文本
    def __init__(self, members: list[tuple[str, Any]]) -> None:
        self._members = members

    def __getitem__(self, key: str) -> Any:
        return dict(self._members)[key]

    def __iter__(self) -> Iterator[str]:
        return iter(name for name, _ in self._members)

    def __len__(self) -> int:
        return len(self._members)

    def items(self) -> list[tuple[str, Any]]:  # type: ignore[override]
        return list(self._members)


class Twice:
    def to_json_value(self) -> Any:
        return Repeated([("a", 1), ("a", 2)])


def test_responses_that_do_not_pass_the_json_text_checks_fail_in_the_plugin() -> None:
    # 写出的整段文本要过共同检查（invocation.md §1「插件一侧写出应答」，ADR-0319）：嵌套超过 64 层或有重复的成员名经
    # Status.error 报 unhandled_exception，没有参数、不带 inner，payload 为空，不交给宿主去拒绝
    deep: Any = []
    for _ in range(63):
        deep = [deep]
    responses: dict[str, object] = {
        # 响应对象是第 1 层，里面 64 层的数组在根之下是 65 层
        "probes.echo": {"items": deep},
        "probes.wait": {"twice": Twice()},
        "probes.callback": [Twice()],
        # 恰好 64 层照常写出
        "probes.explode": deep,
    }
    registry = operation.Registry()
    for operation_id, value in responses.items():

        async def respond(ctx: Context, request: Empty, value: object = value) -> Result[object]:
            return ok(value)

        registry.add(operation.Contract(operation_id), respond, request_type=Empty, response_type=object)

    async def scenario() -> None:
        async with running(build(Probe(), operations=registry)) as plugin:
            await plugin.start()
            for operation_id in ("probes.echo", "probes.wait", "probes.callback"):
                failed = await plugin.stub.Invoke(invoke(operation_id), metadata=session())
                assert failed.payload == b"", operation_id
                expect_failure(failed.status, "KERNEL_UNEXPECTED_001", "kernel.invocation.unhandled_exception", None)
                assert not failed.status.error.HasField("inner"), operation_id
            written = await plugin.stub.Invoke(invoke("probes.explode"), metadata=session())
            assert written.status.ok
            assert written.payload == b"[" * 64 + b"]" * 64

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
            follow_up = await plugin.stub.Invoke(invoke("probes.echo", b'{"text":"still serving"}'), metadata=session())
            assert follow_up.status.ok

    run(scenario)


def test_invoke_stream_reads_only_the_first_frame() -> None:
    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.handshake()
            start = pb.StreamFrame(start=pb.InvokeRequest(operation="probes.echo"))
            undeclared = pb.StreamFrame(start=pb.InvokeRequest(operation="probes.missing"))
            # 不论操作是否声明、插件是否就绪，第一帧是 start 都是 Unimplemented
            for frames, code in (
                ([], grpc.StatusCode.INVALID_ARGUMENT),
                ([pb.StreamFrame(chunk=b"{}")], grpc.StatusCode.INVALID_ARGUMENT),
                ([pb.StreamFrame(end=pb.Status(ok=True))], grpc.StatusCode.INVALID_ARGUMENT),
                ([start], grpc.StatusCode.UNIMPLEMENTED),
                ([undeclared, pb.StreamFrame(chunk=b"{}")], grpc.StatusCode.UNIMPLEMENTED),
            ):
                stream = plugin.stub.InvokeStream(iter(frames), metadata=session())
                await expect_code(anext(aiter(stream)), code)
            await plugin.configure()
            await plugin.stub.Start(pb.StartRequest(), metadata=session())
            stream = plugin.stub.InvokeStream(iter([start]), metadata=session())
            await expect_code(anext(aiter(stream)), grpc.StatusCode.UNIMPLEMENTED)

    run(scenario)


# 投递（invocation.md §4）


def test_deliveries_are_checked_in_order(vector: Callable[[str, str], Any]) -> None:
    unreadable = [case["text"] for case in vector("plugin", "sdk-envelope-times")["unreadable"]]

    async def scenario() -> None:
        async with running(build(Probe())) as plugin:
            await plugin.handshake()
            await expect_code(
                plugin.stub.HandleEvent(pb.HandleEventRequest(mode="competing", attempt=1), metadata=session()),
                grpc.StatusCode.INVALID_ARGUMENT,
            )
            await expect_code(
                plugin.stub.HandleEvent(deliver("probe.events", "a", "bogus"), metadata=session()),
                grpc.StatusCode.INVALID_ARGUMENT,
            )
            # 宿主写来的 time 读不出：不交给作者，也不当作没有时间；先于找订阅
            for topic in ("probe.events", "nobody.listens"):
                await expect_code(
                    plugin.stub.HandleEvent(
                        deliver(topic, "a", "competing", time="2026-11-25T03:25:00"), metadata=session()
                    ),
                    grpc.StatusCode.INVALID_ARGUMENT,
                )
            # sdk-envelope-times.json 的 unreadable 每一条都以 InvalidArgument 拒绝（ADR-0310）
            for text in unreadable:
                await expect_code(
                    plugin.stub.HandleEvent(deliver("probe.events", "a", "competing", time=text), metadata=session()),
                    grpc.StatusCode.INVALID_ARGUMENT,
                )
            # 找订阅先于就绪：没人订阅的主题报 unsubscribed，订阅了的报不可用
            for mode in ("competing", ""):
                unsubscribed = await plugin.stub.HandleEvent(deliver("nobody.listens", "a", mode), metadata=session())
                expect_failure(
                    unsubscribed.status,
                    "KERNEL_NOTFOUND_001",
                    MESSAGE_EVENT_UNSUBSCRIBED,
                    {"id": PROBE_ID, "topic": "nobody.listens", "type": "probe.created", "mode": "competing"},
                )
            early = await plugin.stub.HandleEvent(deliver("probe.events", "a", "competing"), metadata=session())
            expect_failure(
                early.status, ERROR_CODE_UNAVAILABLE, MESSAGE_UNAVAILABLE, {"id": PROBE_ID, "state": "Resolved"}
            )

    run(scenario)


def test_events_are_delivered_by_topic_mode_and_type() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            for request in (
                deliver("probe.events", "a", "competing"),
                deliver("probe.events", "b", ""),
                deliver("probe.events", "c", "competing", "probe.cleared"),
            ):
                response = await plugin.stub.HandleEvent(request, metadata=session())
                assert response.status.ok, response.status
            cleared = await plugin.stub.HandleEvent(
                deliver("probe.events", "d", "broadcast", "probe.cleared"), metadata=session()
            )
            assert cleared.status.ok
            # 以广播投递的其他类型没有订阅：返回失败，不改交给竞争消费的处理器
            unmatched = await plugin.stub.HandleEvent(deliver("probe.events", "e", "broadcast"), metadata=session())
            expect_failure(
                unmatched.status,
                "KERNEL_NOTFOUND_001",
                MESSAGE_EVENT_UNSUBSCRIBED,
                {"id": PROBE_ID, "topic": "probe.events", "type": "probe.created", "mode": "broadcast"},
            )
            assert probe.events == [
                ("a", 2, SubscriptionMode.COMPETING),
                ("b", 2, SubscriptionMode.COMPETING),
                ("c", 2, SubscriptionMode.COMPETING),
                ("d", 2, SubscriptionMode.BROADCAST),
            ]

    run(scenario)


def test_delivered_envelopes_carry_the_time_in_utc_and_the_data_schema_as_is() -> None:
    received: list[Envelope] = []

    async def handle(ctx: Context, envelope: Envelope, attempt: int) -> Result[Unit]:
        received.append(envelope)
        return success()

    subscriptions = [SubscriptionHandler(Subscription("probe.events"), handle)]

    async def scenario() -> None:
        async with running(build(Probe(), subscriptions=subscriptions)) as plugin:
            await plugin.start()
            for request in (
                deliver(
                    "probe.events",
                    "a",
                    "competing",
                    time="2026-11-25T11:25:00.1234567+08:00",
                    data_schema=" https://example.com/schemas/order.json ",
                ),
                deliver("probe.events", "b", "competing"),
            ):
                response = await plugin.stub.HandleEvent(request, metadata=session())
                assert response.status.ok, response.status

    run(scenario)
    timed, plain = received
    # 换算成 UTC，比微秒更细的小数截断；dataschema 原样、不裁剪
    assert timed.time == datetime(2026, 11, 25, 3, 25, 0, 123456, tzinfo=UTC)
    assert timed.time.tzinfo is UTC
    assert timed.data_schema == " https://example.com/schemas/order.json "
    assert (plain.time, plain.data_schema) == (None, None)


def test_matching_subscriptions_run_in_declaration_order_until_the_first_failure() -> None:
    calls: list[str] = []
    outcomes: dict[str, Result[Unit] | Exception] = {}

    def handler(name: str) -> EventHandler:
        async def handle(ctx: Context, envelope: Envelope, attempt: int) -> Result[Unit]:
            calls.append(f"{name}:{envelope.id}")
            result = outcomes.get(name, success())
            if isinstance(result, Exception):
                raise result
            return result

        return handle

    subscriptions = [
        SubscriptionHandler(Subscription("probe.events"), handler("any")),
        SubscriptionHandler(Subscription("probe.events", "probe.typed"), handler("typed")),
    ]

    async def scenario() -> None:
        async with running(build(Probe(), subscriptions=subscriptions)) as plugin:
            await plugin.start()
            both = await plugin.stub.HandleEvent(
                deliver("probe.events", "1", "competing", "probe.typed"), metadata=session()
            )
            assert both.status.ok
            outcomes["any"] = fail(Error.unavailable("PROBE_UNAVAILABLE_001", "probe.busy"))
            first = await plugin.stub.HandleEvent(
                deliver("probe.events", "2", "competing", "probe.typed"), metadata=session()
            )
            expect_failure(first.status, "PROBE_UNAVAILABLE_001", "probe.busy", None)
            outcomes["any"] = success()
            outcomes["typed"] = LookupError("boom")
            raised = await plugin.stub.HandleEvent(deliver("probe.events", "3", "", "probe.typed"), metadata=session())
            expect_failure(raised.status, "KERNEL_UNEXPECTED_001", "kernel.invocation.unhandled_exception", None)
        assert calls == ["any:1", "typed:1", "any:2", "any:3", "typed:3"]

    run(scenario)


# 停止（lifecycle.md §7.5）


def test_stop_waits_for_in_flight_calls_then_releases_resources_and_exits() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = asyncio.ensure_future(plugin.stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            stop = asyncio.ensure_future(plugin.stop(grace_seconds=5, reason="unload"))
            await asyncio.sleep(0.2)
            # 停止期间拒绝新调用与投递，在途调用照常完成
            refused = await plugin.stub.Invoke(invoke("probes.echo", b'{"text":"x"}'), metadata=session())
            expect_failure(
                refused.status, ERROR_CODE_UNAVAILABLE, MESSAGE_UNAVAILABLE, {"id": PROBE_ID, "state": "Draining"}
            )
            event = await plugin.stub.HandleEvent(deliver("probe.events", "a", "competing"), metadata=session())
            assert event.status.error.code == ERROR_CODE_UNAVAILABLE
            assert not stop.done()
            probe.release.set()
            waited = await call
            assert json.loads(waited.payload) == {"cancelled": False}
            assert (await stop).status.ok
            await plugin.exited()
        # 之后关闭 stdin 也不再调停止回调：进程里最多一次
        assert probe.stopped == [("unload", timedelta(seconds=5))]

    run(scenario)


def test_calls_that_outlive_the_grace_period_are_signalled_and_not_awaited() -> None:
    probe = Probe()
    probe.ignore_signal = True

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = asyncio.ensure_future(plugin.stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            began = time.monotonic()
            # grace_seconds 为 1：到时触发取消信号、不再等，调停止回调并应答
            assert (await asyncio.wait_for(plugin.stop(grace_seconds=1), 10)).status.ok
            assert 0.9 < time.monotonic() - began < 5
            assert probe.stopped == [("unload", timedelta(seconds=1))]
            assert not call.done()
            probe.release.set()
            await plugin.exited()
            await settle(call)

    run(scenario)


def test_a_zero_grace_period_means_five_seconds() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            assert (await plugin.stop(grace_seconds=0, reason="restart")).status.ok
            await plugin.exited()
        assert probe.stopped == [("restart", timedelta(seconds=5))]

    run(scenario)


def test_stop_before_a_successful_start_answers_ok_without_the_stop_hook() -> None:
    probe = Probe()

    async def scenario() -> None:
        for steps in ((), ("configure",)):
            async with running(build(probe)) as plugin:
                await plugin.handshake()
                if steps:
                    await plugin.configure()
                assert (await plugin.stop(grace_seconds=0)).status.ok
                await plugin.exited()
        assert probe.stopped == []

    run(scenario)


def test_a_plugin_that_failed_after_starting_still_calls_the_stop_hook() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = asyncio.ensure_future(plugin.stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            probe.health_state = LifecycleState.FAILED
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_FAILED
            stop = asyncio.ensure_future(plugin.stop())
            await asyncio.sleep(0.2)
            # Failed 不迁移到 Draining，在途调用照样等到结束
            assert (await plugin.health()).state == pb.LIFECYCLE_STATE_FAILED
            assert not stop.done()
            probe.release.set()
            assert (await call).status.ok
            assert (await stop).status.ok
            await plugin.exited()
        assert probe.stopped == [("unload", timedelta(seconds=5))]

    run(scenario)


def test_stop_hook_failures_are_reported_and_the_plugin_still_exits() -> None:
    async def scenario() -> None:
        probe = Probe()
        probe.stop_result = fail(Error.unavailable("PROBE_UNAVAILABLE_002", "probe.release.failed"))
        async with running(build(probe)) as plugin:
            await plugin.start()
            stopped = await plugin.stop()
            expect_failure(stopped.status, "PROBE_UNAVAILABLE_002", "probe.release.failed", None)
            await plugin.exited()

        probe = Probe()
        probe.stop_error = OSError("disk C:\\private")
        async with running(build(probe)) as plugin:
            await plugin.start()
            stopped = await plugin.stop()
            expect_failure(
                stopped.status,
                ERROR_CODE_UNEXPECTED,
                MESSAGE_FAILED,
                {"id": PROBE_ID, "phase": "stop", "reason": "OSError"},
            )
            await plugin.exited()

    run(scenario)


# stdin 关闭与 SIGTERM（lifecycle.md §7.7）


def test_closing_stdin_signals_in_flight_calls_and_calls_the_stop_hook_once() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = asyncio.ensure_future(plugin.stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            plugin.close_stdin()
            # 宿主已经离开：在途调用的信号立即触发，它收尾后调停止回调，插件退出
            waited = await call
            assert json.loads(waited.payload) == {"cancelled": True}
            await plugin.exited()
        [(reason, grace)] = probe.stopped
        assert reason == "shutdown"
        assert timedelta(0) <= grace <= timedelta(seconds=3)

    run(scenario)


def test_closing_stdin_before_start_exits_without_the_stop_hook() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.handshake()
            await plugin.configure()
            plugin.close_stdin()
            await plugin.exited()
        assert probe.stopped == []

    run(scenario)


def test_the_shutdown_ends_within_three_seconds_even_with_stuck_calls() -> None:
    probe = Probe()
    probe.ignore_signal = True

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = asyncio.ensure_future(plugin.stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            began = time.monotonic()
            plugin.close_stdin()
            await plugin.exited()
            assert time.monotonic() - began < 3.5
            probe.release.set()
            await settle(call)

    run(scenario)


def test_a_stop_in_progress_is_bounded_by_the_shutdown_limit() -> None:
    probe = Probe()
    probe.ignore_signal = True

    async def scenario() -> None:
        async with running(build(probe)) as plugin:
            await plugin.start()
            call = asyncio.ensure_future(plugin.stub.Invoke(invoke("probes.wait"), metadata=session()))
            await asyncio.wait_for(probe.waiting.wait(), 10)
            stop = asyncio.ensure_future(plugin.stop(grace_seconds=60))
            await asyncio.sleep(0.2)
            began = time.monotonic()
            plugin.close_stdin()
            await plugin.exited()
            assert time.monotonic() - began < 3.5
            probe.release.set()
            await settle(call, stop)

    run(scenario)


# 回调（invocation.md §5）


def test_callbacks_pick_the_dialable_endpoint_and_carry_the_call_context() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with fake_host() as (host, endpoint), running(build(probe), env_with_host(PIPE, endpoint)) as plugin:
            await plugin.start()
            deadline = datetime.now(UTC) + timedelta(seconds=20)
            request = invoke(
                "probes.callback",
                json.dumps({"envelopeTenants": [None, "t1", "t2"]}).encode(),
                **{"xihan.tenant": "t1", "xihan.principal": "p1", "xihan.correlation": "c1"},
            )
            request.metadata.deadline = deadline.strftime("%Y-%m-%dT%H:%M:%S.%f0+00:00")
            response = await plugin.stub.Invoke(request, metadata=session(), timeout=60)
            assert response.status.ok, response.status
            assert json.loads(response.payload) == {
                "invoked": {"code": None, "key": None, "details": None},
                "published": [{"code": None, "key": None, "details": None}] * 3,
            }
            [invoked] = host.invocations
            assert invoked.metadata[SESSION_METADATA_KEY] == TOKEN
            assert (invoked.request.operation, invoked.request.payload) == ("orders.create", b'{"n":1}')
            metadata = invoked.request.metadata
            assert metadata.trace_parent.startswith("00-0af7651916cd43dd8448eb211c80319c-")
            assert metadata.trace_state == ""
            assert dict(metadata.baggage) == {"xihan.tenant": "t1", "xihan.principal": "p1", "xihan.correlation": "c1"}
            # deadline 按 .NET 的 O 格式写，gRPC 的截止时间与它相同
            assert DEADLINE_TEXT.fullmatch(metadata.deadline)
            assert abs(datetime.fromisoformat(metadata.deadline) - deadline) < timedelta(milliseconds=1)
            assert invoked.remaining is not None
            assert 15 < invoked.remaining < 21
            # 信封的 xihantenant 不在本地核对，交给宿主逐项判定：三项都发出
            [published] = host.publications
            assert [envelope.id for envelope in published.request.envelopes] == ["e0", "e1", "e2"]
            assert published.request.envelopes[2].extensions[EXTENSION_TENANT] == "t2"
            # time 换算成 UTC、以 Z 结尾，dataschema 原样；没有的写空串
            sent = published.request.envelopes
            assert [(envelope.time, envelope.data_schema) for envelope in sent] == [
                ("2026-11-25T03:25:00.5Z", "urn:schema:orders"),
                ("", ""),
                ("", ""),
            ]
            assert published.request.metadata.baggage["xihan.tenant"] == "t1"
            assert published.metadata[SESSION_METADATA_KEY] == TOKEN

    run(scenario)


def test_callbacks_carry_only_the_tenant_of_an_in_flight_call() -> None:
    probe = Probe()
    denied = {
        "code": ERROR_CODE_CALLBACK_DENIED,
        "key": MESSAGE_CALLBACK_TENANT_DENIED,
        "details": {"id": PROBE_ID, "tenant": "t2"},
    }

    async def scenario() -> None:
        async with fake_host() as (host, endpoint), running(build(probe), env_with_host(endpoint)) as plugin:
            await plugin.start()
            # 回调上下文的租户不属于在途调用：Host.Invoke 不发出，Host.Publish 整批不发出
            result = await callback(plugin, {"tenant": "t2", "envelopeTenants": [None, None]}, **{"xihan.tenant": "t1"})
            assert result == {"invoked": denied, "published": [denied, denied]}
            assert (host.invocations, host.publications) == ([], [])

            # 调用结束之后在后台回调：宿主已经没有交给插件的调用，不能带租户
            await callback(plugin, {"invoke": False}, **{"xihan.tenant": "t1"})
            assert probe.saved is not None
            late = await host_of(probe.saved).invoke(probe.saved, "orders.create")
            assert late.outcome.error is not None
            assert late.outcome.error.message_key == MESSAGE_CALLBACK_TENANT_DENIED
            assert host.invocations == []

    run(scenario)


@pytest.mark.parametrize(
    ("permissions", "granted", "invoked", "published"),
    [
        # 清单没有声明：宿主授予了也不放行
        (
            ["capability:kernel.pipeline"],
            ["capability:kernel.pipeline", "capability:messaging.queue"],
            None,
            "capability:messaging.queue",
        ),
        # 声明了但没有授予
        (
            ["capability:kernel.pipeline", "capability:messaging.queue"],
            ["capability:messaging.queue"],
            "capability:kernel.pipeline",
            None,
        ),
    ],
)
def test_callbacks_need_the_permission_declared_and_granted(
    permissions: list[str], granted: list[str], invoked: str | None, published: str | None
) -> None:
    probe = Probe()

    def expected(permission: str | None) -> dict[str, Any]:
        if permission is None:
            return {"code": None, "key": None, "details": None}
        return {
            "code": ERROR_CODE_CALLBACK_DENIED,
            "key": MESSAGE_CALLBACK_DENIED,
            "details": {"id": PROBE_ID, "permission": permission},
        }

    async def scenario() -> None:
        plugin_definition = build(probe, manifest=manifest_text(permissions=permissions))
        async with fake_host() as (host, endpoint), running(plugin_definition, env_with_host(endpoint)) as plugin:
            await plugin.handshake()
            assert (await plugin.configure(granted_permissions=granted)).status.ok
            await plugin.stub.Start(pb.StartRequest(), metadata=session())
            result = await callback(plugin, {"envelopeTenants": [None, None]})
            assert result == {"invoked": expected(invoked), "published": [expected(published)] * 2}
            assert len(host.invocations) == (0 if invoked else 1)
            assert len(host.publications) == (0 if published else 1)

    run(scenario)


@pytest.mark.parametrize("address", [None, "", "unix", f"{PIPE}", "tcp 10.0.0.1:5000;tcp 127.0.0.1:1"])
def test_without_a_dialable_endpoint_callbacks_fail_without_a_request(
    address: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    unavailable = {
        "code": ERROR_CODE_UNAVAILABLE,
        "key": MESSAGE_HOST_UNAVAILABLE,
        "details": {"id": PROBE_ID, "reason": "no supported callback endpoint"},
    }

    async def scenario() -> None:
        environ = environment() if address is None else environment(**{ENV_HOST_ADDRESS: address})
        async with running(build(Probe()), environ) as plugin:
            await plugin.start()
            # 插件照常服务
            result = await callback(plugin, {"envelopeTenants": [None]})
            assert result == {"invoked": unavailable, "published": [unavailable]}

    run(scenario)
    warnings = [record for record in caplog.records if ENV_HOST_ADDRESS in record.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


def test_callback_statuses_are_mapped_by_the_table() -> None:
    probe = Probe()

    def failure(code: str, key: str, details: dict[str, Any] | None) -> dict[str, Any]:
        return {"code": code, "key": key, "details": details}

    cases = [
        (grpc.StatusCode.UNAUTHENTICATED, failure(ERROR_CODE_CALLBACK_DENIED, MESSAGE_SESSION_INVALID, None), None),
        (
            grpc.StatusCode.PERMISSION_DENIED,
            failure(
                ERROR_CODE_CALLBACK_DENIED,
                MESSAGE_CALLBACK_DENIED,
                {"id": PROBE_ID, "permission": "capability:kernel.pipeline"},
            ),
            failure(
                ERROR_CODE_CALLBACK_DENIED,
                MESSAGE_CALLBACK_DENIED,
                {"id": PROBE_ID, "permission": "capability:messaging.queue"},
            ),
        ),
        (
            grpc.StatusCode.UNAVAILABLE,
            failure(ERROR_CODE_UNAVAILABLE, MESSAGE_HOST_UNAVAILABLE, {"id": PROBE_ID, "reason": "Unavailable: boom"}),
            None,
        ),
        (
            grpc.StatusCode.UNIMPLEMENTED,
            failure("KERNEL_NOTFOUND_001", "kernel.operation.unknown", {"id": "orders.create"}),
            failure(
                ERROR_CODE_UNEXPECTED,
                MESSAGE_FAILED,
                {"id": PROBE_ID, "phase": "publish", "reason": "Unimplemented: boom"},
            ),
        ),
        (
            grpc.StatusCode.INVALID_ARGUMENT,
            failure(
                "KERNEL_VALIDATION_001", "kernel.operation.invalid_payload", {"id": "orders.create", "reason": "boom"}
            ),
            failure(
                ERROR_CODE_UNEXPECTED,
                MESSAGE_FAILED,
                {"id": PROBE_ID, "phase": "publish", "reason": "InvalidArgument: boom"},
            ),
        ),
        (
            grpc.StatusCode.INTERNAL,
            failure(
                ERROR_CODE_UNEXPECTED,
                MESSAGE_FAILED,
                {"id": PROBE_ID, "phase": "orders.create", "reason": "Internal: boom"},
            ),
            failure(
                ERROR_CODE_UNEXPECTED, MESSAGE_FAILED, {"id": PROBE_ID, "phase": "publish", "reason": "Internal: boom"}
            ),
        ),
    ]

    async def scenario() -> None:
        async with fake_host() as (host, endpoint), running(build(probe), env_with_host(endpoint)) as plugin:
            await plugin.start()
            for status, invoked, published in cases:
                host.status = status
                result = await callback(plugin, {"envelopeTenants": [None, None]})
                # Host.Publish 的 RPC 失败时每一项都是同一个错误
                assert result == {"invoked": invoked, "published": [published or invoked] * 2}, status

    run(scenario)


def test_callbacks_in_flight_at_the_deadline_time_out_and_caller_cancellation_is_cancelled() -> None:
    probe = Probe()
    timed_out = {"code": ERROR_CODE_DEADLINE_EXCEEDED, "key": MESSAGE_DEADLINE_EXCEEDED, "details": None}
    cancelled = {"code": ERROR_CODE_CANCELLED, "key": MESSAGE_CANCELLED, "details": None}

    async def soon(plugin: Any, request: dict[str, Any]) -> dict[str, Any]:
        """调用 ``probes.callback``，元数据的截止时间在 0.5 秒后，gRPC 自身的截止时间宽松"""
        message = invoke("probes.callback", json.dumps(request).encode())
        deadline = datetime.now(UTC) + timedelta(milliseconds=500)
        message.metadata.deadline = deadline.strftime("%Y-%m-%dT%H:%M:%S.%f0+00:00")
        response = await plugin.stub.Invoke(message, metadata=session(), timeout=30)
        assert response.status.ok, response.status
        result: dict[str, Any] = json.loads(response.payload)
        return result

    async def scenario() -> None:
        async with fake_host() as (host, endpoint), running(build(probe), env_with_host(endpoint)) as plugin:
            await plugin.start()
            host.delay = 5.0
            # 处理器在管道里回调：Resilience 阶段派生的取消信号到截止时间也会触发，回调不能因此在本地取消、
            # 报成取消，要等 gRPC 自己的截止时间报 DeadlineExceeded（invocation.md §5）
            result = await soon(plugin, {"envelopeTenants": [None]})
            # Host.Invoke 途中到了截止时间；之后 Host.Publish 发出前截止时间已过，本地按同一行拒绝、不发出
            assert result == {"invoked": timed_out, "published": [timed_out]}
            assert (len(host.invocations), host.publications) == (1, [])
            result = await soon(plugin, {"invoke": False, "envelopeTenants": [None, None]})
            assert result == {"invoked": None, "published": [timed_out, timed_out]}
            assert len(host.publications) == 1

            # 调用方在途中取消：回调随之在本地取消，按「Cancelled，取消信号已触发」报取消
            assert probe.saved is not None
            client = host_of(probe.saved)
            source = Cancellation()
            seen: list[Result[Any]] = []

            async def call(ctx: Context) -> Result[Unit]:
                seen.append((await client.invoke(ctx, "orders.create")).outcome)
                envelope = Envelope(topic="orders.created", id="e0", source="urn:probe", type="orders.created")
                seen.extend(await client.publish(ctx, [envelope]))
                return success()

            asyncio.get_running_loop().call_later(0.2, source.cancel)
            await Context(cancellation=source, timeout=30).run(call)
            assert [outcome(result) for result in seen] == [Outcome(**cancelled), Outcome(**cancelled)]

    run(scenario)


def test_statuses_written_by_the_host_are_read_strictly() -> None:
    probe = Probe()

    async def scenario() -> None:
        async with fake_host() as (host, endpoint), running(build(probe), env_with_host(endpoint)) as plugin:
            await plugin.start()
            host.invoke_status = pb.Status(ok=False, error=pb.Error(code="nope", text="odd"))
            host.publish_results = [pb.Status(ok=True)]
            result = await callback(plugin, {"envelopeTenants": [None, None]})
            assert result["invoked"] == {
                "code": ERROR_CODE_UNEXPECTED,
                "key": MESSAGE_FAILED,
                "details": {"id": PROBE_ID, "phase": "invoke", "reason": "invalid error code 'nope'"},
            }
            mismatch = {
                "code": ERROR_CODE_UNEXPECTED,
                "key": MESSAGE_FAILED,
                "details": {
                    "id": PROBE_ID,
                    "phase": "publish",
                    "reason": "the host returned 1 results for 2 envelopes",
                },
            }
            assert result["published"] == [mismatch, mismatch]

            host.invoke_status = pb.Status(ok=False)
            host.publish_results = [
                pb.Status(ok=True),
                pb.Status(
                    ok=False, error=pb.Error(code="MESSAGING_VALIDATION_002", message_key="messaging.topic.invalid")
                ),
            ]
            result = await callback(plugin, {"envelopeTenants": [None, None]})
            assert result["invoked"] == {
                "code": ERROR_CODE_UNEXPECTED,
                "key": MESSAGE_FAILED,
                "details": {
                    "id": PROBE_ID,
                    "phase": "orders.create",
                    "reason": "the host reported a failure without an error",
                },
            }
            assert result["published"] == [
                {"code": None, "key": None, "details": None},
                {"code": "MESSAGING_VALIDATION_002", "key": "messaging.topic.invalid", "details": None},
            ]

    run(scenario)


def test_a_missing_session_token_is_a_configuration_error() -> None:
    async def scenario() -> None:
        from xihan_framework.primitives import FrameworkError

        with pytest.raises(FrameworkError, match="XIHAN_PLUGIN_SESSION"):
            await build(Probe()).serve(environ={}, stdin=None)

    run(scenario)
