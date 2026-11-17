"""插件协议的数据规则：握手行与版本协商的向量、协议消息的转换、运行时的消息目录、日志格式与插件声明的核对。"""

import asyncio
import io
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from plugin_support import manifest_text
from xihan_framework import operation, pipeline
from xihan_framework.invocation import Cancellation, Context
from xihan_framework.plugin import (
    CATALOG,
    LOCALES,
    MESSAGE_CALLBACK_DENIED,
    MESSAGE_CALLBACK_TENANT_DENIED,
    MESSAGE_FAILED,
    MESSAGE_SESSION_INVALID,
    MESSAGE_UNAVAILABLE,
    Endpoint,
    Envelope,
    Host,
    JsonLogHandler,
    Network,
    Plugin,
    Subscription,
    SubscriptionHandler,
    is_topic,
    load_catalog,
    negotiate_version,
    parse_endpoint,
    parse_handshake_line,
)
from xihan_framework.plugin._convert import (
    from_proto_error,
    from_status,
    restore_context,
    to_metadata,
    to_proto_error,
    to_status,
)
from xihan_framework.plugin._lifecycle import TenantLedger
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


def test_handshake_lines_follow_the_vector(vector: VectorLoader) -> None:
    lines = vector("plugin", "handshake-lines")
    for case in lines["valid"]:
        endpoint = parse_handshake_line(case["line"])
        assert endpoint == Endpoint(Network(case["network"]), case["address"]), case["line"]
        assert endpoint.handshake_line == case["line"]
    for line in lines["invalid"]:
        assert parse_handshake_line(line) is None, line
    # 结尾的换行不会被整串匹配放过
    assert parse_handshake_line("xihan-plugin/1 tcp 127.0.0.1:1\n") is None


def test_protocol_versions_follow_the_vector(vector: VectorLoader) -> None:
    for case in vector("plugin", "protocol-versions")["cases"]:
        selected = negotiate_version(case["host"], case["plugin"])
        assert selected == case.get("selected"), case


def test_host_endpoints_are_parsed_like_handshake_lines() -> None:
    assert parse_endpoint("unix /run/xihan/host.sock") == Endpoint(Network.UNIX, "/run/xihan/host.sock")
    assert parse_endpoint("tcp [::1]:5000") == Endpoint(Network.TCP, "[::1]:5000")
    assert parse_endpoint("tcp 10.0.0.1:5000") is None
    assert parse_endpoint("unix") is None
    assert Endpoint(Network.UNIX, "/a").grpc_target == "unix:/a"
    assert Endpoint(Network.TCP, "127.0.0.1:1").grpc_target == "127.0.0.1:1"
    assert Endpoint(Network.NPIPE, r"\\.\pipe\x").grpc_target is None


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


def test_errors_round_trip_through_the_protocol() -> None:
    inner = Error.literal("ORDERS_UNEXPECTED_001", "inner text")
    error = Error.unexpected("ORDERS_UNEXPECTED_002", "orders.failed", {"n": 2, "ratio": 2.0, "tags": ["a"]}, inner)
    encoded = to_proto_error(error)
    assert encoded.code == "ORDERS_UNEXPECTED_002"
    assert encoded.message_key == "orders.failed"
    # 没有目录解析时文案是描述
    assert encoded.text == 'orders.failed (n=2, ratio=2, tags=["a"])'
    assert json.loads(encoded.details) == {"n": 2, "ratio": 2.0, "tags": ["a"]}
    assert encoded.inner.text == "inner text"
    assert encoded.inner.message_key == ""
    decoded = from_proto_error(encoded)
    assert decoded.code == error.code
    assert decoded.message_key == error.message_key
    assert decoded.details == error.details
    assert decoded.text == encoded.text
    assert decoded.inner == inner


def test_error_texts_come_from_the_plugin_catalogs_in_the_host_locale() -> None:
    catalogs = [Catalog({"en-US": {"orders.failed": "Failed {n}."}, "zh-CN": {"orders.failed": "失败 {n}。"}})]
    error = Error.validation("ORDERS_VALIDATION_001", "orders.failed", {"n": 1})
    assert to_proto_error(error, catalogs).text == "Failed 1."
    assert to_proto_error(error, catalogs, "zh-CN").text == "失败 1。"
    # 运行时自己的键用运行时的目录
    unavailable = Error.unavailable(
        "PLUGINS_UNAVAILABLE_001", MESSAGE_UNAVAILABLE, {"id": "xihan.a.b", "state": "Draining"}
    )
    assert to_proto_error(unavailable).text == "Plugin 'xihan.a.b' is not available (state: Draining)."
    assert to_proto_error(unavailable, (), "zh-CN").text == "插件 'xihan.a.b' 不可用（状态：Draining）。"


def test_protocol_errors_that_are_not_primitive_errors_are_kept_as_unexpected() -> None:
    decoded = from_proto_error(pb.Error(code="not-a-code", text="odd", details="[1]"))
    assert decoded.code == "PLUGINS_UNEXPECTED_001"
    assert decoded.text == "odd"
    assert decoded.details == {"code": "not-a-code"}
    keyless = from_proto_error(pb.Error(code="ORDERS_DENIED_001", message_key="Not A Key"))
    assert keyless.message_key is None
    assert keyless.message == "ORDERS_DENIED_001"


def test_statuses_encode_results() -> None:
    assert to_status(success()) == pb.Status(ok=True)
    failure: Result[Unit] = fail(Error.validation("ORDERS_VALIDATION_001", "orders.invalid"))
    status = to_status(failure)
    assert not status.ok
    assert status.error.code == "ORDERS_VALIDATION_001"
    assert from_status(status, plugin_id="xihan.a.b", phase="Test").error == from_proto_error(status.error)
    assert from_status(pb.Status(ok=True), plugin_id="xihan.a.b", phase="Test").is_ok
    empty = from_status(pb.Status(ok=False), plugin_id="xihan.a.b", phase="Test")
    assert empty.error is not None
    assert (empty.error.code, empty.error.message_key) == ("PLUGINS_UNEXPECTED_001", MESSAGE_FAILED)


def test_metadata_restores_the_context() -> None:
    deadline = datetime(2099, 1, 1, 8, 30, tzinfo=UTC)
    metadata = pb.InvocationMetadata(
        trace_parent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00",
        deadline="2099-01-01T08:30:00.1234567Z",
        baggage={"xihan.tenant": "t1", "xihan.custom": "x", "not a key": "y", "xihan.locale": "a\nb"},
    )
    later = deadline + timedelta(seconds=10)
    ctx = restore_context(metadata, cancellation=Cancellation(), deadline=later)
    assert ctx.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert ctx.sampled is False
    # 元数据与 gRPC 的截止时间取较早的那个；七位小数截到微秒
    assert ctx.deadline == deadline + timedelta(microseconds=123456)
    # 只收已登记的键，已登记键不合约束的值丢弃
    assert dict(ctx.baggage) == {"xihan.tenant": "t1"}
    assert restore_context(None, cancellation=Cancellation(), deadline=later).deadline == later
    invalid = pb.InvocationMetadata(trace_parent="not-a-traceparent", deadline="tomorrow")
    fallback = restore_context(invalid, cancellation=Cancellation(), deadline=None)
    assert fallback.deadline is None
    assert fallback.sampled is None


def test_the_context_is_encoded_as_metadata() -> None:
    ctx = Context(
        trace_parent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00",
        deadline=datetime(2099, 1, 1, tzinfo=UTC),
        baggage={"xihan.tenant": "t1", "xihan.custom": "x"},
    )
    metadata = to_metadata(ctx)
    # 上游的「不采样」原样传下去（RFC-0003 规则 7）
    assert metadata.trace_parent == "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"
    assert metadata.deadline == "2099-01-01T00:00:00.000000Z"
    assert dict(metadata.baggage) == {"xihan.tenant": "t1"}


def test_the_tenant_ledger_counts_in_flight_calls() -> None:
    ledger = TenantLedger()
    assert not ledger.admits("t1")
    with ledger.handed("t1"):
        with ledger.handed("t1"), ledger.handed(None):
            assert ledger.admits("t1")
        assert ledger.admits("t1")
    assert not ledger.admits("t1")


def test_callbacks_that_cannot_be_sent_fail_without_reaching_the_host() -> None:
    async def scenario() -> None:
        ledger = TenantLedger()
        # grpcio 拨不了命名管道：每次回调都是宿主不可用
        piped = Host(Endpoint(Network.NPIPE, "pipe-name"), "token", "xihan.a.b", ledger)
        response = await piped.invoke(Context(), "orders.create")
        assert response.outcome.error is not None
        assert response.outcome.error.code == "PLUGINS_UNAVAILABLE_001"
        assert await piped.publish(Context(), []) == []
        await piped.close()
        # 截止时间已过或已取消的上下文不发出调用；端口 9 上没有宿主，发出去就会是别的错误
        host = Host(Endpoint(Network.TCP, "127.0.0.1:9"), "token", "xihan.a.b", ledger)
        expired = await host.invoke(Context(timeout=-1), "orders.create")
        assert expired.outcome.error is not None
        assert expired.outcome.error.code == "KERNEL_TIMEOUT_001"
        cancelled = Context()
        cancelled.cancellation.cancel()
        envelope = Envelope(topic="orders", id="e1", source="urn:test", type="orders.created")
        [published] = await host.publish(cancelled, [envelope])
        assert published.error is not None
        assert published.error.code == "KERNEL_CANCELLED_001"
        await host.close()

    asyncio.run(scenario())


def test_the_runtime_catalog_has_every_locale_with_the_same_keys_and_placeholders() -> None:
    keys = {
        MESSAGE_UNAVAILABLE,
        MESSAGE_FAILED,
        MESSAGE_CALLBACK_DENIED,
        MESSAGE_CALLBACK_TENANT_DENIED,
        MESSAGE_SESSION_INVALID,
    }
    assert sorted(CATALOG.locales) == sorted(LOCALES)
    english = CATALOG.locales["en-US"]
    assert set(english) == keys
    for locale, templates in CATALOG.locales.items():
        assert set(templates) == keys, locale
        for key, template in templates.items():
            assert sorted(placeholders(template)) == sorted(placeholders(english[key])), (locale, key)
            for character in template:
                assert ord(character) not in (0xA0, 0x202F, 0x2028, 0x2029, 0xFEFF), (locale, key)


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
        (manifest_text(), {"network": Network.NPIPE}, "named pipes"),
        (manifest_text(), {"stop_grace": timedelta(0)}, "positive"),
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
