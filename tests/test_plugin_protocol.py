"""插件协议的数据规则：握手行、回调端点、截止时间、回调失败的映射与消息键的向量，协议消息的转换、运行时的消息目录、
配置节的类型化读取、回调的本地核对、日志格式与插件声明的核对。"""

import io
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from plugin_support import PIPE, manifest_text, run_scenario
from xihan_framework import operation, pipeline
from xihan_framework.invocation import Cancellation, Context
from xihan_framework.plugin import (
    CATALOG,
    CATALOG_NAME,
    DIALABLE_NETWORKS,
    ERROR_CODE_CALLBACK_DENIED,
    ERROR_CODE_UNAVAILABLE,
    ERROR_CODE_UNEXPECTED,
    LOCALES,
    MESSAGE_CALLBACK_DENIED,
    MESSAGE_CALLBACK_TENANT_DENIED,
    MESSAGE_EVENT_UNSUBSCRIBED,
    MESSAGE_FAILED,
    MESSAGE_HOST_UNAVAILABLE,
    MESSAGE_SESSION_INVALID,
    MESSAGE_UNAVAILABLE,
    PERMISSION_INVOKE,
    PERMISSION_PUBLISH,
    Configuration,
    Endpoint,
    Envelope,
    Host,
    JsonLogHandler,
    Network,
    Plugin,
    Subscription,
    SubscriptionHandler,
    host_of,
    is_topic,
    is_whitespace,
    load_catalog,
    negotiate_version,
    parse_endpoint,
    parse_handshake_line,
    parse_host_addresses,
    select_host_endpoint,
)
from xihan_framework.plugin._convert import (
    REASON_FAILURE_WITHOUT_ERROR,
    effective_deadline,
    format_deadline,
    from_proto_error,
    from_status,
    parse_deadline,
    restore_context,
    to_metadata,
    to_proto_error,
    to_status,
)
from xihan_framework.plugin._host import REASON_NO_CALLBACK_ENDPOINT, map_callback_status, status_name
from xihan_framework.plugin._lifecycle import TenantLedger
from xihan_framework.plugin._runtime import REASON_CONFIGURATION_NOT_OBJECT
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.primitives import (
    Catalog,
    Error,
    FrameworkError,
    PipelineStage,
    Result,
    Unit,
    fail,
    placeholders,
    success,
)

type VectorLoader = Callable[[str, str], Any]

PLUGIN_ID = "xihan.acme.echo"
# handshake.md §2 的空白：与 .NET 正则的 \s 相同
SPEC_WHITESPACE = {
    *range(0x09, 0x0E),
    0x20,
    0x85,
    0xA0,
    0x1680,
    *range(0x2000, 0x200B),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
}


def _utc(text: str) -> datetime:
    """向量里精确到纳秒的 UTC 时刻，截断到 ``datetime`` 的微秒（比时钟精度更细的小数截断后比较）。"""
    seconds, fraction = text.removesuffix("Z").split(".")
    parsed = datetime.fromisoformat(seconds).replace(tzinfo=UTC)
    return parsed.replace(microsecond=int(fraction[:6]))


def test_handshake_lines_follow_the_vector(vector: VectorLoader) -> None:
    lines = vector("plugin", "handshake-lines")
    for case in lines["valid"]:
        endpoint = parse_handshake_line(case["line"])
        assert endpoint == Endpoint(Network(case["network"]), case["address"]), case["line"]
        assert endpoint.handshake_line == case["line"]
    for line in lines["invalid"]:
        assert parse_handshake_line(line) is None, line
    # 行尾不属于行，整串匹配不放过结尾的换行
    assert parse_handshake_line("xihan-plugin/1 tcp 127.0.0.1:1\n") is None


def test_the_whitespace_of_addresses_is_the_spec_set() -> None:
    for code in range(0x3100):
        assert is_whitespace(chr(code)) is (code in SPEC_WHITESPACE), hex(code)
    # str.isspace 认的 U+001C–U+001F 与零宽空格都不算
    assert not any(is_whitespace(chr(code)) for code in (0x1C, 0x1F, 0x200B, 0xFEFF))


def test_host_addresses_follow_the_vector(vector: VectorLoader) -> None:
    addresses = vector("plugin", "host-addresses")
    for case in addresses["valid"]:
        expected = tuple(Endpoint(Network(entry["network"]), entry["address"]) for entry in case["entries"])
        assert parse_host_addresses(case["value"]) == expected, case["value"]
    for value in addresses["invalid"]:
        assert parse_host_addresses(value) is None, value
        assert select_host_endpoint(value) is None, value
    for case in addresses["selection"]:
        supported = {Network(network) for network in case["supported"]}
        selected = select_host_endpoint(case["value"], supported)
        expected = case["selected"]
        assert selected == (None if expected is None else Endpoint(Network(expected["network"]), expected["address"]))


def test_the_runtime_picks_the_first_endpoint_grpcio_can_dial() -> None:
    assert {Network.UNIX, Network.TCP} == DIALABLE_NETWORKS
    # Windows 上的 .NET 宿主：命名管道在前、回环 TCP 在后
    assert select_host_endpoint(f"{PIPE};tcp 127.0.0.1:50123") == Endpoint(Network.TCP, "127.0.0.1:50123")
    assert select_host_endpoint("unix /tmp/h/host.sock;tcp 127.0.0.1:1") == Endpoint(Network.UNIX, "/tmp/h/host.sock")
    assert select_host_endpoint(str(PIPE)) is None
    assert select_host_endpoint(None) is None


def test_protocol_versions_follow_the_vector(vector: VectorLoader) -> None:
    for case in vector("plugin", "protocol-versions")["cases"]:
        selected = negotiate_version(case["host"], case["plugin"])
        assert selected == case.get("selected"), case


def test_endpoints_follow_the_handshake_grammar() -> None:
    assert parse_endpoint("unix /run/xihan/host.sock") == Endpoint(Network.UNIX, "/run/xihan/host.sock")
    assert parse_endpoint("tcp [::1]:5000") == Endpoint(Network.TCP, "[::1]:5000")
    assert parse_endpoint("tcp 127.0.0.1:99999") is None
    assert parse_endpoint("tcp 127.0.0.1:" + chr(0xFF11)) is None
    assert parse_endpoint("unix") is None
    assert Endpoint(Network.UNIX, "/a").grpc_target == "unix:/a"
    assert Endpoint(Network.TCP, "127.0.0.1:1").grpc_target == "127.0.0.1:1"
    assert PIPE.grpc_target is None


def test_deadlines_follow_the_vector(vector: VectorLoader) -> None:
    deadlines = vector("plugin", "deadlines")
    for case in deadlines["valid"]:
        assert parse_deadline(case["text"]) == _utc(case["utc"]), case["text"]
    for text in deadlines["invalid"]:
        assert parse_deadline(text) is None, text
    assert parse_deadline("") is None
    for case in deadlines["effective"]:
        grpc = None if case["grpc"] is None else _utc(case["grpc"])
        expected = None if case["expected"] is None else _utc(case["expected"])
        assert effective_deadline(case["metadata"], grpc) == expected, case
    for case in deadlines["written"]:
        written = format_deadline(_utc(case["utc"]))
        expected = case["text"]
        # 七位小数里 datetime 只有前六位，第七位总是 0；其余逐字相同
        assert (written[:26], written[26], written[27:]) == (expected[:26], "0", expected[27:]), case


def test_callback_failures_follow_the_vector(vector: VectorLoader) -> None:
    statuses = vector("plugin", "callback-status")
    for case in statuses["cases"]:
        assert status_name(case["statusCode"]) == case["status"]
        error = map_callback_status(
            case["rpc"],
            case["statusCode"],
            case["message"],
            case["contextCancelled"],
            statuses["pluginId"],
            statuses["operation"],
        )
        assert (error.code, error.message_key) == (case["code"], case["key"]), case
        assert dict(error.details or {}) == (case["details"] or {}), case
        assert error.text is None
    assert status_name(17) == "17"


def test_plugin_messages_follow_the_vector(vector: VectorLoader) -> None:
    messages = vector("plugin", "plugin-messages")
    codes = {
        MESSAGE_UNAVAILABLE: ERROR_CODE_UNAVAILABLE,
        MESSAGE_FAILED: ERROR_CODE_UNEXPECTED,
        MESSAGE_EVENT_UNSUBSCRIBED: operation.ERROR_CODE_NOT_FOUND,
        MESSAGE_HOST_UNAVAILABLE: ERROR_CODE_UNAVAILABLE,
        MESSAGE_CALLBACK_DENIED: ERROR_CODE_CALLBACK_DENIED,
        MESSAGE_CALLBACK_TENANT_DENIED: ERROR_CODE_CALLBACK_DENIED,
        MESSAGE_SESSION_INVALID: ERROR_CODE_CALLBACK_DENIED,
    }
    english = CATALOG.locales["en-US"]
    # SDK 的目录恰好是规范的键，没有自造的键
    assert {message["key"] for message in messages["messages"]} == set(codes) == set(english)
    for message in messages["messages"]:
        key = message["key"]
        assert codes[key] == message["code"], key
        assert english[key] == message["template"], key
        assert sorted(placeholders(english[key])) == sorted(message["arguments"]), key
    reasons = {(reason["key"], reason["reason"]) for reason in messages["reasons"]}
    assert (MESSAGE_FAILED, REASON_CONFIGURATION_NOT_OBJECT) in reasons
    assert (MESSAGE_FAILED, REASON_FAILURE_WITHOUT_ERROR) in reasons
    assert (MESSAGE_HOST_UNAVAILABLE, REASON_NO_CALLBACK_ENDPOINT) in reasons
    for templated in ("invalid error code '{code}'", "the host returned {results} results for {envelopes} envelopes"):
        assert (MESSAGE_FAILED, templated) in reasons


def test_the_runtime_catalog_has_every_locale_with_the_same_keys_and_placeholders() -> None:
    assert sorted(CATALOG.locales) == sorted(LOCALES)
    english = CATALOG.locales["en-US"]
    for locale, templates in CATALOG.locales.items():
        assert set(templates) == set(english), locale
        for key, template in templates.items():
            assert sorted(placeholders(template)) == sorted(placeholders(english[key])), (locale, key)
            for character in template:
                assert ord(character) not in (0xA0, 0x202F, 0x2028, 0x2029, 0xFEFF), (locale, key)


def test_errors_with_a_message_key_are_written_without_text() -> None:
    inner = Error.literal("ORDERS_UNEXPECTED_001", "inner text")
    error = Error.unexpected("ORDERS_UNEXPECTED_002", "orders.failed", {"n": 2, "ratio": 2.0, "tags": ["a"]}, inner)
    # 已解析的文案也不写：宿主按键翻译成调用方的语言
    encoded = to_proto_error(error.with_text("Resolved in some locale."))
    assert (encoded.code, encoded.message_key, encoded.text) == ("ORDERS_UNEXPECTED_002", "orders.failed", "")
    assert json.loads(encoded.details) == {"n": 2, "ratio": 2.0, "tags": ["a"]}
    assert (encoded.inner.code, encoded.inner.message_key, encoded.inner.text) == (
        "ORDERS_UNEXPECTED_001",
        "",
        "inner text",
    )
    assert encoded.inner.details == ""
    assert to_proto_error(Error.validation("ORDERS_VALIDATION_001", "orders.invalid")).details == ""
    status = to_status(fail(error))
    assert not status.ok
    assert status.error == to_proto_error(error)
    assert to_status(success()) == pb.Status(ok=True)


def test_errors_written_by_the_host_are_read_by_the_same_rules() -> None:
    replaced = from_proto_error(
        pb.Error(
            code="not-a-code", message_key="orders.failed", text="odd", details='{"a":1}', inner=pb.Error(code="X")
        ),
        PLUGIN_ID,
    )
    assert (replaced.code, replaced.message_key, replaced.inner) == (ERROR_CODE_UNEXPECTED, MESSAGE_FAILED, None)
    assert replaced.details == {"id": PLUGIN_ID, "phase": "invoke", "reason": "invalid error code 'not-a-code'"}
    keyed = from_proto_error(
        pb.Error(code="ORDERS_DENIED_001", message_key="orders.denied", text="ignored", details='{"n":1}'), PLUGIN_ID
    )
    assert (keyed.message_key, keyed.text, keyed.details) == ("orders.denied", None, {"n": 1})
    literal = from_proto_error(pb.Error(code="ORDERS_DENIED_001", message_key="Not A Key", text="odd"), PLUGIN_ID)
    assert (literal.message_key, literal.text) == (None, "odd")
    bare = from_proto_error(pb.Error(code="ORDERS_DENIED_001"), PLUGIN_ID)
    assert (bare.message_key, bare.text) == (None, "ORDERS_DENIED_001")
    for details in ("", "[1]", '"x"', "{", '{"a":1,"a":2}', chr(0xFEFF) + "{}"):
        assert from_proto_error(pb.Error(code="ORDERS_DENIED_001", details=details), PLUGIN_ID).details is None, details
    nested = from_proto_error(
        pb.Error(code="ORDERS_DENIED_001", message_key="orders.denied", inner=pb.Error(code="bad")), PLUGIN_ID
    )
    assert nested.inner is not None
    assert nested.inner.details == {"id": PLUGIN_ID, "phase": "invoke", "reason": "invalid error code 'bad'"}
    empty = from_status(pb.Status(ok=False), plugin_id=PLUGIN_ID, phase="orders.create")
    assert empty.error is not None
    assert (empty.error.code, empty.error.message_key) == (ERROR_CODE_UNEXPECTED, MESSAGE_FAILED)
    assert empty.error.details == {"id": PLUGIN_ID, "phase": "orders.create", "reason": REASON_FAILURE_WITHOUT_ERROR}
    assert from_status(pb.Status(ok=True), plugin_id=PLUGIN_ID, phase="publish").is_ok


def test_metadata_restores_the_context() -> None:
    deadline = datetime(2099, 1, 1, 8, 30, tzinfo=UTC)
    metadata = pb.InvocationMetadata(
        trace_parent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00",
        trace_state="vendor=ignored",
        deadline="2099-01-01T08:30:00.1234567Z",
        baggage={
            "xihan.tenant": "t1",
            "xihan.principal": "p1",
            "xihan.custom": "x",
            "not a key": "y",
            "xihan.locale": "a\nb",
        },
    )
    later = deadline + timedelta(seconds=10)
    ctx = restore_context(metadata, cancellation=Cancellation(), deadline=later)
    assert ctx.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert ctx.sampled is False
    # 元数据与 gRPC 的截止时间取较早的那个；七位小数截到微秒
    assert ctx.deadline == deadline + timedelta(microseconds=123456)
    # 只收已登记的键，xihan.principal 原样交给作者；已登记键不合约束的值丢弃
    assert dict(ctx.baggage) == {"xihan.tenant": "t1", "xihan.principal": "p1"}
    assert restore_context(None, cancellation=Cancellation(), deadline=later).deadline == later
    earlier = restore_context(
        pb.InvocationMetadata(deadline="2099-01-01T08:30:00Z"),
        cancellation=Cancellation(),
        deadline=deadline - timedelta(1),
    )
    assert earlier.deadline == deadline - timedelta(1)
    # 读不出的截止时间当作没有：空格分隔的写法 fromisoformat 接受，规范的文法不接受
    for text in ("tomorrow", "2099-01-01 08:30:00Z", "2099-01-01T08:30:00"):
        fallback = restore_context(
            pb.InvocationMetadata(trace_parent="not-a-traceparent", deadline=text),
            cancellation=Cancellation(),
            deadline=None,
        )
        assert fallback.deadline is None, text
        assert fallback.sampled is None


def test_the_context_is_encoded_as_metadata() -> None:
    ctx = Context(
        trace_parent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00",
        deadline=datetime(2099, 1, 1, 8, tzinfo=UTC) + timedelta(microseconds=5),
        baggage={"xihan.tenant": "t1", "xihan.custom": "x"},
    )
    metadata = to_metadata(ctx)
    # 上游的「不采样」原样传下去（RFC-0003 规则 7）
    assert metadata.trace_parent == "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"
    assert metadata.trace_state == ""
    assert metadata.deadline == "2099-01-01T08:00:00.0000050+00:00"
    assert dict(metadata.baggage) == {"xihan.tenant": "t1"}
    assert format_deadline(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00.0000000+00:00"
    assert to_metadata(Context()).deadline == ""


def test_the_tenant_ledger_counts_in_flight_calls() -> None:
    ledger = TenantLedger()
    assert not ledger.admits("t1")
    with ledger.handed("t1"):
        with ledger.handed("t1"), ledger.handed(None):
            assert ledger.admits("t1")
        assert ledger.admits("t1")
    assert not ledger.admits("t1")


def test_callbacks_are_checked_locally_before_anything_is_sent() -> None:
    async def scenario() -> None:
        ledger = TenantLedger()
        envelopes = [
            Envelope(topic="orders", id=f"e{index}", source="urn:test", type="orders.created") for index in range(2)
        ]
        unavailable = {"id": PLUGIN_ID, "reason": REASON_NO_CALLBACK_ENDPOINT}
        # 没有可用端点（缺失、或只有 grpcio 拨不了的命名管道）：不发请求，Host.Publish 的每一项都是同一个错误
        for endpoint in (None, PIPE):
            missing = Host(endpoint, "token", PLUGIN_ID, ledger, permissions=[PERMISSION_INVOKE, PERMISSION_PUBLISH])
            assert missing.endpoint is None
            response = await missing.invoke(Context(), "orders.create")
            assert response.outcome.error is not None
            assert (response.outcome.error.code, response.outcome.error.message_key) == (
                ERROR_CODE_UNAVAILABLE,
                MESSAGE_HOST_UNAVAILABLE,
            )
            assert response.outcome.error.details == unavailable
            published = await missing.publish(Context(), envelopes)
            assert [result.error for result in published] == [response.outcome.error] * 2
            assert await missing.publish(Context(), []) == []
            await missing.close()

        # 端口 9 上没有宿主：本地核对拦下的调用不会走到连接失败
        host = Host(Endpoint(Network.TCP, "127.0.0.1:9"), "token", PLUGIN_ID, ledger, permissions=[PERMISSION_INVOKE])

        def denied(permission: str) -> Error:
            return Error.denied(
                ERROR_CODE_CALLBACK_DENIED, MESSAGE_CALLBACK_DENIED, {"id": PLUGIN_ID, "permission": permission}
            )

        # 声明了但还没有授予（Configure 之前）
        assert (await host.invoke(Context(), "orders.create")).outcome.error == denied(PERMISSION_INVOKE)
        host._grant([PERMISSION_INVOKE, PERMISSION_PUBLISH])
        # 授予了但清单没有声明：宿主不放行，本地同样拒绝，整批每一项同一个错误
        assert [result.error for result in await host.publish(Context(), envelopes)] == [denied(PERMISSION_PUBLISH)] * 2
        tenant = Context(baggage={"xihan.tenant": "t1"})
        refused = (await host.invoke(tenant, "orders.create")).outcome.error
        assert refused == Error.denied(
            ERROR_CODE_CALLBACK_DENIED, MESSAGE_CALLBACK_TENANT_DENIED, {"id": PLUGIN_ID, "tenant": "t1"}
        )
        with ledger.handed("t1"):
            # 租户属于在途调用，核对通过，请求发出去连不上：按 Unavailable 映射
            reached = (await host.invoke(tenant, "orders.create")).outcome.error
            assert reached is not None
            assert (reached.code, reached.message_key) == (ERROR_CODE_UNAVAILABLE, MESSAGE_HOST_UNAVAILABLE)
            assert str(reached.details["reason"]).startswith("Unavailable: ")  # type: ignore[index]
        expired = await host.invoke(Context(timeout=-1), "orders.create")
        assert expired.outcome.error is not None
        assert expired.outcome.error.code == "KERNEL_TIMEOUT_001"
        cancelled = Context()
        cancelled.cancellation.cancel()
        assert (await host.invoke(cancelled, "orders.create")).outcome.error == Error.cancelled(
            "KERNEL_CANCELLED_001", "kernel.invocation.cancelled"
        )
        await host.close()

    run_scenario(scenario)


def test_the_host_client_exists_only_in_runtime_contexts() -> None:
    with pytest.raises(FrameworkError, match="host client"):
        host_of(Context())


@dataclass(frozen=True)
class Nested:
    level: int


@dataclass(frozen=True)
class Settings:
    retries: int
    ratio: float = 1.0
    enabled: bool = False
    kind: str = ""
    names: list[str] = field(default_factory=list)
    limits: dict[str, int] = field(default_factory=dict)
    nested: Nested | None = None
    renamed: int = field(default=0, metadata=operation.json_field("maxItems"))


def _configuration(**section: Any) -> Configuration:
    return Configuration(section=section)


def test_configuration_sections_are_read_like_iconfiguration() -> None:
    # 宿主写出的叶子一律是字符串，成员名的大小写随配置源
    read = _configuration(
        RETRIES="3",
        Ratio="2.5e0",
        ENABLED="TRUE",
        Kind="x",
        names=["a", "b"],
        Limits={"A": "1"},
        NESTED={"Level": "7"},
        MAXITEMS="-4",
    ).decode(Settings)
    assert read == Settings(3, 2.5, True, "x", ["a", "b"], {"A": 1}, Nested(7), -4)
    # 本来就是数字与布尔的照样接受
    assert _configuration(retries=3, enabled=False, ratio=1).decode(Settings) == Settings(3, 1.0, False)
    assert _configuration(retries="0", enabled="fAlSe").decode(Settings).enabled is False
    # 开尔文符号 U+212A 小写后是 k，那不是 ASCII 不区分大小写
    assert _configuration(retries="1", **{chr(0x212A) + "IND": "x"}).decode(Settings).kind == ""


@pytest.mark.parametrize(
    ("section", "path"),
    [
        ({"retries": "3.0"}, "$.retries"),
        ({"retries": " 3"}, "$.retries"),
        ({"retries": "+3"}, "$.retries"),
        ({"retries": "0x10"}, "$.retries"),
        ({"retries": "03"}, "$.retries"),
        ({"retries": chr(0xFF11)}, "$.retries"),
        ({"retries": "1", "ratio": "NaN"}, "$.ratio"),
        ({"retries": "1", "enabled": "yes"}, "$.enabled"),
        ({"retries": "1", "enabled": "1"}, "$.enabled"),
        ({"retries": "1", "nested": {"level": "x"}}, "$.nested.level"),
        ({}, "$"),
    ],
)
def test_configuration_sections_that_do_not_fit_the_type_are_refused(section: dict[str, Any], path: str) -> None:
    with pytest.raises(FrameworkError, match=f"'Settings': {path.replace('$', '[$]')}: "):
        Configuration(section=section).decode(Settings)


def test_catalog_directories_are_read_strictly(tmp_path: Any) -> None:
    (tmp_path / "en-US.json").write_text('{"orders.created": "Created {id}."}', encoding="utf-8")
    (tmp_path / "zh-CN.json").write_text('{"orders.created": "已创建 {id}。"}', encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    catalog = load_catalog(tmp_path)
    assert catalog.resolve("orders.created", "zh-CN", {"id": 1}) == "已创建 1。"
    (tmp_path / "de-DE.json").write_text('{"orders.created": "a", "orders.created": "b"}', encoding="utf-8")
    with pytest.raises(FrameworkError, match="not valid JSON"):
        load_catalog(tmp_path)
    (tmp_path / "de-DE.json").write_text('{"orders.created": 1}', encoding="utf-8")
    with pytest.raises(FrameworkError, match="object of strings"):
        load_catalog(tmp_path)
    (tmp_path / "de-DE.json").unlink()
    (tmp_path / "en-US.json").unlink()
    with pytest.raises(FrameworkError, match="en-US"):
        load_catalog(tmp_path)


def test_logs_are_json_lines_with_the_host_levels() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("tests.plugin.log")
    logger.propagate = False
    logger.setLevel(1)
    handler = JsonLogHandler(stream)
    logger.addHandler(handler)
    try:
        logger.log(5, "trace")
        logger.debug("debug")
        logger.info("line\nbreak %s", "é", extra={"plugin": "xihan.a.b", "attempt": 2})
        logger.warning("warning")
        logger.error("error")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")
        logger.critical("critical")
    finally:
        logger.removeHandler(handler)
    lines = stream.getvalue().splitlines()
    entries = [json.loads(line) for line in lines]
    assert all(line.isascii() and line.startswith("{") for line in lines)
    assert [entry["level"] for entry in entries] == [
        "trace",
        "debug",
        "information",
        "warning",
        "error",
        "error",
        "critical",
    ]
    assert entries[2]["message"] == "line\nbreak é"
    assert (entries[2]["plugin"], entries[2]["attempt"], entries[2]["logger"]) == ("xihan.a.b", 2, "tests.plugin.log")
    assert "ValueError: boom" in entries[5]["exception"]


def test_topics_follow_the_messaging_grammar() -> None:
    assert is_topic("echo.events")
    assert is_topic("0" + "a" * 127)
    assert not is_topic("Echo")
    assert not is_topic("")
    assert not is_topic("a" * 129)
    with pytest.raises(FrameworkError):
        Subscription("Echo")
    with pytest.raises(FrameworkError):
        Subscription("echo.events", "")


def _registry(*ids: str) -> operation.Registry:
    async def handle(ctx: Context, request: Unit) -> Result[Unit]:
        return success()

    registry = operation.Registry()
    for id in ids:
        registry.add(operation.Contract(id), handle, request_type=Unit, response_type=Unit)
    return registry


async def _ignore(ctx: Context, envelope: object, attempt: int) -> Result[Unit]:
    return success()


@pytest.mark.parametrize(
    ("manifest", "options", "message"),
    [
        (manifest_text(kind="capability", isolation=None, permissions=[]), {}, "out-of-process"),
        (manifest_text(isolation="wasm"), {}, "out-of-process"),
        (
            manifest_text(contributes={"pipeline": [{"middleware": "Audit", "stage": "observe"}]}),
            {},
            "contributes.pipeline",
        ),
        (manifest_text(primitives="2"), {}, "primitives 2"),
        (manifest_text(), {"operations": _registry("orders.create")}, "contributes.operations"),
        (
            manifest_text(),
            {
                "subscriptions": [
                    SubscriptionHandler(Subscription("orders"), _ignore),
                    SubscriptionHandler(Subscription("orders", None, None), _ignore),
                ]
            },
            "more than once",
        ),
        (manifest_text(), {"catalogs": {"orders": Catalog({"zh-CN": {}})}}, "en-US"),
        (manifest_text(), {"catalogs": {CATALOG_NAME: Catalog({"en-US": {}})}}, "runtime catalog"),
        (manifest_text(), {"catalogs": {"mine": CATALOG}}, "runtime catalog"),
        (manifest_text(), {"network": Network.NPIPE}, "named pipes"),
        ("{", {}, "not valid JSON"),
    ],
)
def test_plugin_declarations_are_checked_when_the_plugin_is_built(
    manifest: str, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(FrameworkError, match=message):
        Plugin(manifest, **options)


def test_the_plugin_reads_the_manifest_of_the_working_directory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FrameworkError, match="does not exist"):
        Plugin()
    text = manifest_text()
    (tmp_path / "xihan.manifest.json").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    plugin = Plugin(pipeline=pipeline.Builder().use_at(pipeline.func("Audit", _pass), PipelineStage.OBSERVE))
    # 握手交的清单与文件一致，开头的 BOM 不算
    assert plugin.manifest_text == text
    assert plugin.manifest.id == "xihan.tests.probe"
    assert "Audit" in plugin.invoker.dump()


async def _pass(ctx: Context, next_step: pipeline.Step) -> Result[Unit]:
    return await next_step(ctx)


def test_importing_the_runtime_without_grpcio_says_how_to_install_it() -> None:
    code = "import sys; sys.modules['grpc'] = None; import xihan_framework.plugin"
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert completed.returncode != 0
    assert 'pip install "xihan-framework[plugin]"' in completed.stderr
    # 核心五块不依赖可选依赖
    core = "import sys; sys.modules['grpc'] = None; import xihan_framework.operation, xihan_framework.manifest"
    assert subprocess.run([sys.executable, "-c", core], check=False).returncode == 0
