"""插件协议的数据规则：握手行、回调端点、截止时间与信封的时间、回调失败的映射与消息键的向量，协议消息的转换、运行时的消息
目录、配置节的类型化读取、回调的本地核对、日志格式与遮值、插件声明的核对。"""

import io
import json
import logging
import math
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from importlib.resources.abc import Traversable
from typing import Any, cast

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
    Binding,
    Configuration,
    Endpoint,
    Envelope,
    Host,
    JsonLogHandler,
    Network,
    Plugin,
    Subscription,
    SubscriptionHandler,
    find_host,
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
    _details,
    effective_deadline,
    format_deadline,
    format_time,
    from_proto_envelope,
    from_proto_error,
    from_status,
    parse_timestamp,
    restore_context,
    to_metadata,
    to_proto_envelope,
    to_proto_error,
    to_status,
)
from xihan_framework.plugin._host import (
    REASON_NO_CALLBACK_ENDPOINT,
    callback_phase,
    map_callback_status,
    status_name,
)
from xihan_framework.plugin._lifecycle import TenantLedger
from xihan_framework.plugin._runtime import REASON_CONFIGURATION_NOT_OBJECT
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.primitives import (
    ERROR_CODE_INVALID_ARGUMENT,
    Catalog,
    Error,
    FrameworkError,
    JsonValue,
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
        assert parse_timestamp(case["text"]) == _utc(case["utc"]), case["text"]
    for text in deadlines["invalid"]:
        assert parse_timestamp(text) is None, text
    assert parse_timestamp("") is None
    for case in deadlines["effective"]:
        grpc = None if case["grpc"] is None else _utc(case["grpc"])
        expected = None if case["expected"] is None else _utc(case["expected"])
        assert effective_deadline(case["metadata"], grpc) == expected, case
    for case in deadlines["written"]:
        written = format_deadline(_utc(case["utc"]))
        expected = case["text"]
        # 七位小数里 datetime 只有前六位，第七位总是 0；其余逐字相同
        assert (written[:26], written[26], written[27:]) == (expected[:26], "0", expected[27:]), case


def _envelope(**fields: Any) -> Envelope:
    return Envelope(topic="orders", id="e1", source="urn:test", type="orders.created", **fields)


def _delivered(**fields: Any) -> Envelope | None:
    """宿主写来的信封经 SDK 读出的样子。"""
    return from_proto_envelope(pb.Envelope(topic="orders", id="e1", source="urn:test", type="orders.created", **fields))


def _delivered_time(text: str) -> datetime | None:
    envelope = _delivered(time=text)
    assert envelope is not None, text
    assert envelope.time is None or envelope.time.tzinfo is UTC, text
    return envelope.time


def test_envelope_times_follow_the_vector(vector: VectorLoader) -> None:
    # valid 与 invalid 是宿主读插件写来的时间，SDK 读宿主写来的时间按同一条文法；written 是宿主写给插件的（七位小数、+00:00），
    # SDK 读得出，但不是 SDK 写出的样子（ADR-0310）
    times = vector("plugin", "envelope-times")
    for case in times["valid"]:
        assert _delivered_time(case["text"]) == (None if case["utc"] is None else _utc(case["utc"])), case["text"]
    for text in times["invalid"]:
        assert _delivered(time=text) is None, text
    for case in times["written"]:
        instant = case["instant"]
        assert _delivered_time(case["text"]) == (None if instant is None else _instant(instant)), case


def _instant(text: str) -> datetime:
    """``written`` 的 ``instant``（带偏移、小数最多七位）截断到微秒后的 UTC 时刻。"""
    match = re.fullmatch(r"([^.]*?)(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})", text)
    assert match is not None, text
    offset = "+00:00" if match[3] == "Z" else match[3]
    base = datetime.fromisoformat(match[1] + offset)
    return base.replace(microsecond=int(((match[2] or "") + "000000")[:6])).astimezone(UTC)


#: ``datetime`` 的精度在 ``sdk-envelope-times.json`` 里的名字：微秒，写出时最多六位小数。
PRECISION = "microseconds"


def _authored(text: str) -> datetime:
    """``writing`` 的 ``instant``：按投递时读宿主文本的读法读成 UTC 时刻（截断到微秒），再换回原文的偏移，好核对写出时换算成 UTC。"""
    utc = parse_timestamp(text)
    assert utc is not None, text
    offset = re.search(r"([+-])([0-9]{2}):([0-9]{2})\Z", text)
    if offset is None:
        return utc
    span = timedelta(hours=int(offset[2]), minutes=int(offset[3]))
    return utc.astimezone(timezone(-span if offset[1] == "-" else span))


def test_sdk_envelope_times_follow_the_vector_at_microsecond_precision(vector: VectorLoader) -> None:
    # SDK 一侧的写出与读入（ADR-0310），取微秒那一项；unreadable 经 gRPC 的投递另在 test_plugin_runtime 核对
    times = vector("plugin", "sdk-envelope-times")
    assert times["precisions"][PRECISION] == 6
    for case in times["writing"]:
        authored = _envelope() if case["instant"] is None else _envelope(time=_authored(case["instant"]))
        assert to_proto_envelope(authored).time == case["text"][PRECISION], case["name"]
    for case in times["reading"]:
        assert _delivered_time(case["text"]) == _utc(case["utc"][PRECISION]), case["name"]
    for case in times["unreadable"]:
        assert _delivered(time=case["text"]) is None, case["name"]


#: 六个 SDK 共用的写出用例：作者给的时刻 → SDK 写出的 ``time``（UTC、``Z``、小数去掉末尾的 0）。
WRITTEN_TIMES: list[tuple[datetime, str]] = [
    (datetime(2026, 11, 25, 3, 25, 0, 123000, tzinfo=UTC), "2026-11-25T03:25:00.123Z"),
    (datetime(2026, 11, 25, 3, 25, 0, 500000, tzinfo=UTC), "2026-11-25T03:25:00.5Z"),
    (datetime(2026, 11, 25, 3, 25, 0, 120000, tzinfo=UTC), "2026-11-25T03:25:00.12Z"),
    (datetime(2026, 11, 25, 3, 25, tzinfo=UTC), "2026-11-25T03:25:00Z"),
    (datetime(2026, 11, 25, 11, 25, 0, 250000, tzinfo=timezone(timedelta(hours=8))), "2026-11-25T03:25:00.25Z"),
    (datetime(2026, 11, 24, 22, 25, tzinfo=timezone(timedelta(hours=-5))), "2026-11-25T03:25:00Z"),
    (datetime(2026, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))), "2025-12-31T23:30:00Z"),
    (datetime(1, 1, 1, tzinfo=UTC), "0001-01-01T00:00:00Z"),
    # 微秒是 datetime 的精度
    (datetime(2026, 11, 25, 3, 25, 0, 1, tzinfo=UTC), "2026-11-25T03:25:00.000001Z"),
    (datetime(2026, 11, 25, 3, 25, 0, 123456, tzinfo=UTC), "2026-11-25T03:25:00.123456Z"),
    (datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), "9999-12-31T23:59:59.999999Z"),
]

#: 六个 SDK 共用的读入用例：宿主写来的 ``time`` → 作者收到的 UTC 时刻（比微秒更细的小数截断）。
READ_TIMES: list[tuple[str, datetime | None]] = [
    ("2026-11-25T11:25:00+08:00", datetime(2026, 11, 25, 3, 25, tzinfo=UTC)),
    ("2026-11-24T22:25:00.25-05:00", datetime(2026, 11, 25, 3, 25, 0, 250000, tzinfo=UTC)),
    ("2026-11-25t03:25:00z", datetime(2026, 11, 25, 3, 25, tzinfo=UTC)),
    ("2026-11-25T03:25:00.1234567+00:00", datetime(2026, 11, 25, 3, 25, 0, 123456, tzinfo=UTC)),
    ("2026-11-25T03:25:00.123456789Z", datetime(2026, 11, 25, 3, 25, 0, 123456, tzinfo=UTC)),
    ("9999-12-31T23:59:59.999999999Z", datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
    ("", None),
]


@pytest.mark.parametrize(("time", "text"), WRITTEN_TIMES)
def test_envelope_times_are_written_in_utc_with_the_trailing_zeros_of_the_fraction_dropped(
    time: datetime, text: str
) -> None:
    assert format_time(time) == text
    assert to_proto_envelope(_envelope(time=time)).time == text
    # 往返：写出的文本读回同一个时刻
    assert _delivered_time(text) == time


@pytest.mark.parametrize(("text", "time"), READ_TIMES)
def test_envelope_times_from_the_host_are_read_in_utc_and_truncated_to_microseconds(
    text: str, time: datetime | None
) -> None:
    assert _delivered_time(text) == time


def test_envelopes_without_a_time_write_an_empty_text() -> None:
    assert to_proto_envelope(_envelope()).time == ""
    assert _envelope().time is None


@pytest.mark.parametrize(
    "text",
    ["2026-11-25T03:25:60Z", "2026-11-25T03:25:00", "2026-11-25 03:25:00Z", "2026-11-25T03:25:00.1234567890Z", " "],
)
def test_envelope_times_from_the_host_outside_the_grammar_are_not_read(text: str) -> None:
    assert _delivered(time=text) is None


def test_envelope_times_must_be_timezone_aware_datetimes() -> None:
    with pytest.raises(FrameworkError) as naive:
        _envelope(time=datetime(2026, 11, 25, 3, 25))
    assert naive.value.code == ERROR_CODE_INVALID_ARGUMENT
    assert isinstance(naive.value, ValueError)
    with pytest.raises(TypeError):
        _envelope(time="2026-11-25T03:25:00Z")


@pytest.mark.parametrize(
    ("time", "text"),
    [
        (datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))), "0000-12-31T23:30:00Z"),
        (
            datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=23, minutes=59, microseconds=500))),
            "0000-12-31T00:00:59.9995Z",
        ),
        (datetime(9999, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-1))), "10000-01-01T00:30:00Z"),
        (
            datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone(-timedelta(hours=23, minutes=59))),
            "10000-01-01T23:58:59.999999Z",
        ),
    ],
)
def test_envelope_times_outside_the_four_digit_years_in_utc_are_written_for_the_host_to_reject(
    time: datetime, text: str
) -> None:
    # 不在本地核对年份范围（ADR-0306）：照写，宿主以 messaging.envelope.invalid_time 拒绝那一项
    assert to_proto_envelope(_envelope(time=time)).time == text
    assert parse_timestamp(text) is None


def test_envelopes_carry_the_data_schema_as_is() -> None:
    schema = "https://example.com/schemas/order.json"
    assert to_proto_envelope(_envelope(data_schema=schema)).data_schema == schema
    assert to_proto_envelope(_envelope()).data_schema == ""
    assert to_proto_envelope(_envelope(data_schema="")).data_schema == ""
    # 不裁剪、不核对：只有空白的值由宿主拒绝那一项
    assert to_proto_envelope(_envelope(data_schema=" urn:x ")).data_schema == " urn:x "
    for text, expected in ((schema, schema), ("", None), (" ", " ")):
        delivered = _delivered(data_schema=text)
        assert delivered is not None
        assert delivered.data_schema == expected, text
    # 调试输出、相等比较与哈希都算上它
    assert "data_schema='urn:a'" in repr(_envelope(data_schema="urn:a"))
    assert _envelope(data_schema="urn:a") != _envelope(data_schema="urn:b")
    assert _envelope(data_schema="urn:a") == _envelope(data_schema="urn:a")
    assert hash(_envelope(data_schema="urn:a")) == hash(_envelope(data_schema="urn:a"))
    time = datetime(2026, 11, 25, 3, 25, tzinfo=UTC)
    assert "time=datetime.datetime(2026, 11, 25, 3, 25" in repr(_envelope(time=time))


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


def _decoded(error: Error | None) -> Any:
    """读入后的错误，写成向量 ``expected`` 的形状；数的类型也比较（``1e+20`` 读成双精度，不是整数）。"""
    if error is None:
        return None
    details = None if error.details is None else {name: (type(value), value) for name, value in error.details.items()}
    return {
        "code": error.code,
        "key": error.message_key,
        "text": error.text,
        "details": details,
        "inner": _decoded(error.inner),
    }


def _expected(case: Any) -> Any:
    if case is None:
        return None
    details = case["details"]
    return {
        "code": case["code"],
        "key": case["key"],
        "text": case["text"],
        "details": None if details is None else {name: (type(value), value) for name, value in details.items()},
        "inner": _expected(case["inner"]),
    }


def _proto_error(fields: dict[str, Any]) -> pb.Error:
    inner = fields.get("inner")
    encoded = pb.Error(
        code=fields["code"], message_key=fields["message_key"], text=fields["text"], details=fields["details"]
    )
    if inner is not None:
        encoded.inner.CopyFrom(_proto_error(inner))
    return encoded


def test_errors_written_by_the_host_follow_the_vector(vector: VectorLoader) -> None:
    reading = vector("plugin", "error-reading")
    plugin_id = reading["pluginId"]
    for case in reading["cases"]:
        decoded = from_proto_error(_proto_error(case["error"]), plugin_id)
        assert _decoded(decoded) == _expected(case["expected"]), case["name"]
        # Host.Invoke 与 Host.Publish 的结果同一规则：错误码不合文法的 phase 也是 invoke
        for phase in ("orders.create", "publish"):
            status = pb.Status(ok=False, error=_proto_error(case["error"]))
            result = from_status(status, plugin_id=plugin_id, phase=phase)
            assert _decoded(result.error) == _expected(case["expected"]), (case["name"], phase)
    for case in reading["statusWithoutError"]:
        if case["reader"] != "plugin":
            continue
        phase = callback_phase(case["rpc"], case["operation"] or "")
        # ok 为假却没有 error 与整个 status 缺失相同
        missing: tuple[pb.Status | None, ...] = (pb.Status(ok=False), None)
        for absent in missing:
            result = from_status(absent, plugin_id=plugin_id, phase=phase)
            assert _decoded(result.error) == _expected(case["expected"]), case


def test_error_details_with_lone_surrogates_are_no_details() -> None:
    # 以字符串交来的 JSON 文本严格编码成 UTF-8，代理项码点编码不成 UTF-8，与非法字节一样不合法（RFC-0001「JSON 文本」，
    # ADR-0301）；Python 的 str 里两个代理项码点也不是一个字符。protobuf 的字符串字段本来就收不下它们，线上触发不到
    for text in (
        '{"a":"' + chr(0xD800) + '"}',
        '{"a":"' + chr(0xDC00) + '"}',
        '{"a":"' + chr(0xD83D) + chr(0xDE00) + '"}',
    ):
        assert _details(text) is None, ascii(text)
        with pytest.raises(UnicodeEncodeError):
            pb.Error(code="ORDERS_VALIDATION_001", details=text)
    assert _details('{"a":"' + chr(0x1F600) + '"}') == {"a": chr(0x1F600)}


def _nested(levels: int) -> dict[str, JsonValue]:
    """嵌套 ``levels`` 层的对象：1 层是 ``{}``。"""
    value: dict[str, JsonValue] = {}
    for _ in range(levels - 1):
        value = {"a": value}
    return value


def test_error_details_that_cannot_be_written_are_left_out() -> None:
    # invocation.md §3「写出 Error」（ADR-0320）：细节有代理项码点或写出的文本超过 64 层时这个错误不带细节，其余字段照写；
    # 内部错误的细节各自核对。Python 的 str 里两个代理项码点也不是一个字符
    inner = Error.validation("SAMPLES_VALIDATION_002", "samples.inner", {"id": "i1"})
    unwritable: list[dict[str, JsonValue]] = [
        {"a": chr(0xD800)},
        {chr(0xDC00): 1},
        {"list": ["x", chr(0xDC00) + chr(0xD800)]},
        {"pair": chr(0xD83D) + chr(0xDE00)},
        _nested(65),
    ]
    for details in unwritable:
        written = to_proto_error(Error.unexpected("SAMPLES_UNEXPECTED_001", "samples.failed", details, inner))
        assert written.details == "", ascii(details)
        assert (written.code, written.message_key) == ("SAMPLES_UNEXPECTED_001", "samples.failed")
        assert written.inner.details == '{"id":"i1"}', "the inner details are checked on their own"
        literal = to_proto_error(Error.literal("SAMPLES_CONFLICT_001", "stale", details))
        assert (literal.details, literal.text) == ("", "stale")
        outer = to_proto_error(
            Error.unexpected(
                "SAMPLES_UNEXPECTED_001",
                "samples.failed",
                {"id": "o1"},
                Error.literal("SAMPLES_VALIDATION_002", "x", details),
            )
        )
        assert (outer.details, outer.inner.details, outer.inner.text) == ('{"id":"o1"}', "", "x")
    # 64 层的细节照写；平面以外的字符与非有限的数照值模型写
    deep = to_proto_error(Error.literal("SAMPLES_CONFLICT_001", "stale", _nested(64)))
    assert deep.details == '{"a":' * 63 + "{}" + "}" * 63
    special = to_proto_error(Error.literal("SAMPLES_CONFLICT_001", "stale", {"a": chr(0x1F600), "n": math.nan}))
    assert special.details == '{"a":"' + chr(0x1F600) + '","n":"NaN"}'


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
        # 第 ④ 步排在租户之后：截止时间已过、租户也越界的回调报租户
        expired_tenant = Context(timeout=-1, baggage={"xihan.tenant": "t1"})
        assert (await host.invoke(expired_tenant, "orders.create")).outcome.error == refused
        # 截止时间已过按 DeadlineExceeded、已被取消按「Cancelled，取消信号已触发」那一行，两者都成立时按截止时间；不发请求
        timeout = Error.timeout("KERNEL_TIMEOUT_001", "kernel.invocation.deadline_exceeded")
        cancelled_error = Error.cancelled("KERNEL_CANCELLED_001", "kernel.invocation.cancelled")
        assert (await host.invoke(Context(timeout=-1), "orders.create")).outcome.error == timeout
        cancelled = Context()
        cancelled.cancellation.cancel()
        assert (await host.invoke(cancelled, "orders.create")).outcome.error == cancelled_error
        # 权限排在第 ④ 步之前：这个客户端的清单没有声明发布权限
        assert [result.error for result in await host.publish(cancelled, envelopes)] == [denied(PERMISSION_PUBLISH)] * 2
        publisher = Host(
            Endpoint(Network.TCP, "127.0.0.1:9"), "token", PLUGIN_ID, ledger, permissions=[PERMISSION_PUBLISH]
        )
        publisher._grant([PERMISSION_PUBLISH])
        assert [result.error for result in await publisher.publish(cancelled, envelopes)] == [cancelled_error] * 2
        await publisher.close()
        both = Context(timeout=-1)
        both.cancellation.cancel()
        assert (await host.invoke(both, "orders.create")).outcome.error == timeout
        await host.close()

    run_scenario(scenario)


def test_the_host_client_exists_only_in_runtime_contexts() -> None:
    with pytest.raises(FrameworkError, match="host client"):
        host_of(Context())
    # find_host 是不抛出的取法：在运行时之外（测试里直接调处理器）也要能跑的代码用它
    assert find_host(Context()) is None
    derived = Context().with_baggage("xihan.tenant", "t1")
    assert find_host(derived) is None


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


def test_catalog_files_are_named_after_locales(tmp_path: Any) -> None:
    # 文件名就是区域名（RFC-0010）：大小写不同的写法归到规范形式，不是区域名的 .json 文件使目录不合法
    (tmp_path / "EN-us.json").write_text('{"orders.created": "Created {id}."}', encoding="utf-8")
    (tmp_path / "zh-hant-tw.json").write_text('{"orders.created": "已建立 {id}。"}', encoding="utf-8")
    catalog = load_catalog(tmp_path)
    assert sorted(catalog.locales) == ["en-US", "zh-Hant-TW"]
    assert catalog.resolve("orders.created", "zh-hant-TW", {"id": 1}) == "已建立 1。"
    assert catalog.resolve("orders.created", "ja-JP", {"id": 1}) == "Created 1."
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FrameworkError, match=r"'notes\.json'") as raised:
        load_catalog(tmp_path)
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT
    # 只差大小写的两个文件是同一个区域；压缩包里的名字区分大小写，在任何文件系统上都放得下这两个文件
    archive = tmp_path / "catalog.zip"
    with zipfile.ZipFile(archive, "w") as written:
        written.writestr("en-US.json", '{"orders.created": "Created {id}."}')
        written.writestr("en-us.json", '{"orders.created": "created {id}"}')
    with zipfile.ZipFile(archive) as opened, pytest.raises(FrameworkError, match="more than one file") as raised:
        load_catalog(cast(Traversable, zipfile.Path(opened)))
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT


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


def test_logs_carry_only_the_names_of_binding_properties() -> None:
    binding = Binding("db", "connection:relational", "Server=db;Password=s3cr3t", {"password": "s3cr3t", "User": "sa"})
    configuration = Configuration(section={}, bindings=(binding,))
    stream = io.StringIO()
    logger = logging.getLogger("tests.plugin.redaction")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = JsonLogHandler(stream)
    logger.addHandler(handler)
    try:
        logger.info(
            "configured %s with %s",
            binding,
            binding.properties,
            extra={
                "properties": binding.properties,
                "nested": {"bindings": [binding.properties], "pair": (binding.name, binding.properties)},
                "binding": binding,
                "configuration": configuration,
                "extensions": Envelope(topic="t", id="e", source="s", type="x", extensions={"k": "v"}).extensions,
            },
        )
        # 作者自己取出的值照样写出
        logger.info("copied", extra={"copied": dict(binding.properties)})
    finally:
        logger.removeHandler(handler)
    redacted, copied = (json.loads(line) for line in stream.getvalue().splitlines())
    # 属性名按升序，与绑定写成 JSON 时的 propertyNames 同一口径（ADR-0299）
    assert redacted["properties"] == ["User", "password"]
    assert redacted["nested"] == {"bindings": [["User", "password"]], "pair": ["db", ["User", "password"]]}
    written = json.dumps(redacted)
    assert not any(value in written for value in ("s3cr3t", "Server=db", '"sa"', "'sa'")), written
    assert redacted["message"] == "configured " + repr(binding) + " with {'User': ..., 'password': ...}"
    # 扩展属性不是凭据，照常写出
    assert redacted["extensions"] == {"k": "v"}
    assert copied["copied"] == {"password": "s3cr3t", "User": "sa"}


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
