"""primitives 的测试：一致性向量（错误码、Baggage 键、生命周期迁移）与 Go SDK 移植来的编码、消息与工厂用例。"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from xihan_framework import _json, primitives
from xihan_framework.primitives import (
    ERROR_CODE_INVALID_ARGUMENT,
    UNIT,
    Catalog,
    Error,
    FrameworkError,
    JsonValue,
    LifecycleState,
    Maybe,
    PipelineStage,
    Result,
    Stability,
    TypedId,
    fail,
    ok,
    success,
)

type VectorLoader = Callable[[str, str], Any]


def _as_int(value: JsonValue) -> int:
    """把 JSON 值当作整数解码。"""
    assert isinstance(value, int)
    return value


@pytest.fixture
def default_messages() -> Iterator[None]:
    """测试结束后还原进程默认的消息解析器与区域。"""
    yield
    primitives.set_default_messages(None, primitives.DEFAULT_LOCALE)


def test_error_codes_follow_the_vectors(vector: VectorLoader) -> None:
    vectors = vector("primitives", "error-codes")
    for code in vectors["valid"]:
        assert primitives.is_error_code(code), code
    for code in vectors["invalid"]:
        assert not primitives.is_error_code(code), code
    # Python 的 $ 会放过结尾的换行；文法判定必须是整串匹配
    assert not primitives.is_error_code("PERSISTENCE_CONFLICT_001\n")
    assert primitives.error_code_kind("PERSISTENCE_CONFLICT_001") == "CONFLICT"
    assert primitives.error_code_domain("PERSISTENCE_CONFLICT_001") == "PERSISTENCE"
    assert primitives.error_code_kind("bad") is None
    with pytest.raises(FrameworkError) as raised:
        primitives.validate_error_code("bad")
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT


def test_baggage_keys_follow_the_vectors(vector: VectorLoader) -> None:
    vectors = vector("primitives", "baggage-keys")
    for key in vectors["wellFormed"]:
        assert primitives.is_baggage_key_well_formed(key), key
    for key in vectors["illFormed"]:
        assert not primitives.is_baggage_key_well_formed(key), key
    assert list(primitives.REGISTERED_BAGGAGE_KEYS) == vectors["registered"]
    for key in vectors["registered"]:
        assert primitives.is_baggage_key_registered(key), key
    assert not primitives.is_baggage_key_registered("xihan.feature-flag")


def test_lifecycle_transitions_follow_the_vectors(vector: VectorLoader) -> None:
    allowed = {(pair[0], pair[1]) for pair in vector("primitives", "lifecycle-transitions")["allowed"]}
    for source_name, target_name in allowed:
        source, target = LifecycleState.parse(source_name), LifecycleState.parse(target_name)
        assert source is not None, source_name
        assert target is not None, target_name
        assert source.can_transition_to(target), f"{source} -> {target}"
    for source in LifecycleState:
        for target in LifecycleState:
            if source.can_transition_to(target):
                assert (str(source), str(target)) in allowed, f"{source} -> {target} is absent from the vector"
    assert LifecycleState.READY.is_serving
    assert LifecycleState.DEGRADED.is_serving
    assert not LifecycleState.STARTED.is_serving
    assert LifecycleState.STOPPED.is_terminal
    assert LifecycleState.FAILED.value == 8
    assert LifecycleState.DECLARED.value == 0
    assert LifecycleState.parse("ready") is None
    assert _json.dumps(LifecycleState.READY.to_json_value()) == '"Ready"'


def test_pipeline_stages_are_ordered() -> None:
    names = [
        "ingress",
        "observe",
        "resilience",
        "authenticate",
        "tenant",
        "authorize",
        "validate",
        "idempotency",
        "transaction",
        "handle",
        "egress",
    ]
    stages = list(PipelineStage)
    assert [stage.manifest_name for stage in stages] == names
    assert [stage.value for stage in stages] == list(range(len(names)))
    for stage, name in zip(stages, names, strict=True):
        assert PipelineStage.parse(name) is stage
        assert str(stage) == name
    assert PipelineStage.parse("Handle") is None


def test_error_factories_check_the_kind() -> None:
    error = Error.validation("ORDERS_VALIDATION_001", "orders.quantity.invalid", {"min": 1})
    assert error.kind == "VALIDATION"
    assert error.domain == "ORDERS"
    assert error.message == "orders.quantity.invalid (min=1)"
    with pytest.raises(FrameworkError) as raised:
        Error.not_found("ORDERS_VALIDATION_001", "orders.missing")
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT
    assert isinstance(raised.value, ValueError)
    with pytest.raises(FrameworkError):
        Error("ORDERS_VALIDATION_001", "Orders.Missing")
    with pytest.raises(FrameworkError):
        Error.literal("ORDERS_VALIDATION_001", "")
    for factory, code in [
        (Error.conflict, "ORDERS_CONFLICT_001"),
        (Error.denied, "ORDERS_DENIED_001"),
        (Error.unavailable, "ORDERS_UNAVAILABLE_001"),
        (Error.timeout, "ORDERS_TIMEOUT_001"),
        (Error.cancelled, "ORDERS_CANCELLED_001"),
        (Error.unexpected, "ORDERS_UNEXPECTED_001"),
    ]:
        assert factory(code, "orders.failed").code == code


def test_error_details_must_be_json_values() -> None:
    with pytest.raises(FrameworkError) as raised:
        Error("ORDERS_VALIDATION_001", "orders.invalid", {"ids": {1, 2}})  # type: ignore[dict-item]
    assert "'ids'" in raised.value.message
    with pytest.raises(FrameworkError):
        Error("ORDERS_VALIDATION_001", "orders.invalid", {"ratio": float("nan")})
    details: dict[str, JsonValue] = {"items": [1, "a", None, {"b": True}]}
    error = Error("ORDERS_VALIDATION_001", "orders.invalid", details)
    details["items"] = []
    assert error.details == {"items": [1, "a", None, {"b": True}]}


def test_message_keys_follow_the_grammar() -> None:
    for key in ["orders.missing", "tenant_members.list.description", "kernel.operation.unknown", "a1.b_c"]:
        assert primitives.is_message_key(key), key
    for key in [
        "",
        "orders",
        "Orders.missing",
        "orders.Missing",
        "orders..missing",
        ".orders",
        "1a.b",
        "orders.missing\n",
    ]:
        assert not primitives.is_message_key(key), key
    assert primitives.message_key_domain("tenant_members.list.description") == "tenant_members"
    assert primitives.message_key_domain("bad") is None


@pytest.mark.usefixtures("default_messages")
def test_error_messages_resolve_through_the_default_catalog() -> None:
    primitives.set_default_messages(
        Catalog(
            {
                "en-US": {"orders.missing": "Order {id} was not found.", "orders.only_english": "English {id}."},
                "zh-CN": {"orders.missing": "订单 {id} 不存在。"},
            }
        ),
        "zh-CN",
    )
    error = Error.not_found("ORDERS_NOTFOUND_001", "orders.missing", {"id": 42})
    assert error.message == "订单 42 不存在。"
    assert Error.not_found("ORDERS_NOTFOUND_001", "orders.only_english", {"id": 7}).message == "English 7."
    assert error.with_text("literal").message == "literal"
    assert primitives.default_locale() == "zh-CN"
    primitives.set_default_messages(None)
    assert primitives.default_locale() == "zh-CN"
    assert error.message == "orders.missing (id=42)"


def test_templates_format_named_positional_and_escaped_placeholders() -> None:
    assert primitives.format_message("{0} of {count:%03d}", {"0": "a", "count": 7}) == "a of 007"
    assert primitives.format_message("{missing}", None) == "{missing}"
    assert primitives.format_message("{{id}} is {id}", {"id": 1}) == "{id} is 1"
    assert primitives.format_message("{0}-{1}", primitives.positional("x", 2)) == "x-2"
    assert primitives.format_message("{amount:N2}", {"amount": 1.5}) == "1.5"
    assert primitives.format_message("{count:%d}", {"count": "seven"}) == "seven"
    assert primitives.placeholders("{a} {b:%d} {a} {{c}} {0}") == ["a", "b", "0"]
    assert primitives.describe("orders.failed", {"z": 1, "a": "x"}) == "orders.failed (a=x, z=1)"
    assert primitives.describe("orders.failed", {}) == "orders.failed"


def test_arguments_are_written_like_go() -> None:
    cases: list[tuple[object, str]] = [
        (True, "true"),
        (None, "<nil>"),
        (42, "42"),
        (1.5, "1.5"),
        (2.0, "2"),
        (100000.0, "100000"),
        (1e6, "1e+06"),
        (0.0001, "0.0001"),
        (0.00001, "1e-05"),
        (123456789.0, "1.23456789e+08"),
        (-0.0, "-0"),
        ([1, "a"], "[1 a]"),
        ({"b": 1, "a": 2}, "map[a:2 b:1]"),
    ]
    for value, text in cases:
        assert primitives.format_argument(value) == text, value


def test_catalog_checks_keys_and_falls_back_to_english() -> None:
    with pytest.raises(FrameworkError):
        Catalog({"en-US": {"Bad": "x"}})
    catalog = Catalog({"en-US": {"orders.missing": "Order {id}."}})
    assert catalog.resolve("orders.missing", "ja-JP", {"id": 1}) == "Order 1."
    assert catalog.resolve("orders.other", "en-US", None) is None


def test_error_wire_encoding() -> None:
    inner = Error.literal("KERNEL_UNEXPECTED_001", "boom")
    error = Error.unexpected("ORDERS_UNEXPECTED_001", "orders.failed", {"id": "o1"}, inner)
    data = error.to_json()
    assert data == (
        '{"code":"ORDERS_UNEXPECTED_001","message":"orders.failed (id=o1)","messageKey":"orders.failed",'
        '"details":{"id":"o1"},"inner":{"code":"KERNEL_UNEXPECTED_001","message":"boom"}}'
    )
    decoded = Error.from_json(data)
    assert decoded.code == "ORDERS_UNEXPECTED_001"
    assert decoded.text == "orders.failed (id=o1)"
    assert decoded.message_key == "orders.failed"
    assert decoded.inner is not None
    assert decoded.inner.code == "KERNEL_UNEXPECTED_001"
    with pytest.raises(FrameworkError):
        Error.from_json('{"code":"bad"}')
    with pytest.raises(FrameworkError):
        Error.from_json("{")
    # 细节的键按字典序写出，与 Go 编码 map 一致
    sorted_details = Error("ORDERS_VALIDATION_001", "orders.invalid", {"z": 1, "a": {"y": 2, "b": 3}})
    assert '"details":{"a":{"b":3,"y":2},"z":1}' in sorted_details.to_json()


def test_result_wire_encoding() -> None:
    succeeded = ok({"id": "o1"})
    assert succeeded.to_json() == '{"ok":true,"value":{"id":"o1"}}'
    failed: Result[dict[str, str]] = fail(Error.literal("ORDERS_CONFLICT_001", "stale"))
    data = failed.to_json()
    assert data == '{"ok":false,"error":{"code":"ORDERS_CONFLICT_001","message":"stale"}}'
    decoded = Result.from_json(data, lambda value: value)
    assert decoded.is_failed
    assert decoded.error is not None
    assert decoded.error.code == "ORDERS_CONFLICT_001"
    assert Result.from_json('{"ok":true,"value":5}', _as_int).value == 5
    assert success().to_json() == '{"ok":true,"value":{}}'
    assert success().value is UNIT
    with pytest.raises(FrameworkError):
        Result.from_json('{"ok":false}', lambda value: value)
    with pytest.raises(FrameworkError):
        Result.from_json('{"value":1}', lambda value: value)
    assert ok(21).map(lambda value: value * 2).value == 42
    assert failed.bind(lambda _: ok(1)).is_failed
    assert ok(1).bind(lambda value: ok(str(value))).value == "1"
    assert failed.value_or({}) == {}
    assert failed.unit().is_failed
    assert ok(3).unit() == success()


def test_results_cannot_be_uninitialized() -> None:
    # Go 的零值 Result 是未初始化的失败；Python 只能经工厂创建，直接构造即编程错误
    with pytest.raises(TypeError):
        Result(object(), 1, None)
    with pytest.raises(FrameworkError):
        ok(None)
    with pytest.raises(FrameworkError):
        fail("boom")  # type: ignore[arg-type]
    with pytest.raises(FrameworkError) as raised:
        fail(Error.literal("ORDERS_CONFLICT_001", "stale")).value_or_raise()
    assert raised.value.code == "ORDERS_CONFLICT_001"
    assert raised.value.message == "stale"
    assert repr(ok(1)) == "Ok(1)"
    assert repr(fail(Error.literal("ORDERS_CONFLICT_001", "stale"))) == "Fail(ORDERS_CONFLICT_001: stale)"


def test_maybe_wire_encoding() -> None:
    assert _json.dumps(Maybe.some("x").to_json_value()) == '"x"'
    assert _json.dumps(Maybe.none().to_json_value()) == "null"
    assert not Maybe.from_json_value(None, _as_int).has_value
    assert Maybe.from_json_value(5, _as_int).value_or(0) == 5
    assert Maybe.none().to_result(Error.literal("ORDERS_NOTFOUND_001", "missing")).is_failed
    assert Maybe.some(2).to_result(Error.literal("ORDERS_NOTFOUND_001", "missing")).value == 2
    assert Maybe.some(2).map(lambda value: value + 1) == Maybe.some(3)
    assert Maybe.from_optional(None) == Maybe.none()
    with pytest.raises(FrameworkError):
        Maybe.some(None)
    with pytest.raises(FrameworkError):
        Maybe.none().value_or_raise()


class OrderId(TypedId[str]):
    __slots__ = ()


class CustomerId(TypedId[str]):
    __slots__ = ()


def test_typed_ids_encode_as_their_value() -> None:
    order = OrderId("o1")
    assert _json.dumps(order.to_json_value()) == '"o1"'
    assert order == OrderId("o1")
    assert order != CustomerId("o1")
    assert len({order, OrderId("o1"), CustomerId("o1")}) == 2
    assert repr(order) == "OrderId('o1')"


def test_stability_names() -> None:
    for name in ["stable", "preview", "experimental", "internal", "deprecated"]:
        stability = Stability.parse(name)
        assert stability is not None
        assert str(stability) == name
    assert _json.dumps(Stability.EXPERIMENTAL.to_json_value()) == '"experimental"'
    assert Stability.STABLE.value == 0


def test_framework_errors_convert_to_literal_errors() -> None:
    error = FrameworkError("KERNEL_PIPELINE_001", "the ordering constraints form a cycle").to_error()
    assert error.code == "KERNEL_PIPELINE_001"
    assert error.message == "the ordering constraints form a cycle"
    assert error.message_key is None
    assert FrameworkError("bad", "x").to_error().code == ERROR_CODE_INVALID_ARGUMENT
    with pytest.raises(FrameworkError) as raised:
        raise FrameworkError("KERNEL_PIPELINE_001", "failed") from OSError("disk")
    assert str(raised.value) == "KERNEL_PIPELINE_001: failed (disk)"


@dataclass
class _Line:
    sku: str
    price: float


def test_json_is_written_like_go_encoding_json() -> None:
    numbers = [2.0, 0.1, 1e20, 1e21, 1e-6, 1e-7, 1.5e-10, 123456789.0, -0.0, 3.14159, 5e-324, 1.7976931348623157e308]
    assert _json.dumps(numbers) == (
        "[2,0.1,100000000000000000000,1e+21,0.000001,1e-7,1.5e-10,123456789,-0,3.14159,5e-324,1.7976931348623157e+308]"
    )
    assert _json.dumps("<a&b> \n\t\x01") == '"\\u003ca\\u0026b\\u003e\\u2028\\n\\t\\u0001"'
    assert _json.dumps("\ud800") == '"\\ufffd"'
    assert _json.dumps({"b": [], "a": {}}, indent=True) == '{\n  "b": [],\n  "a": {}\n}'
    assert _json.dumps(ok([_Line("x", 2.0)]).to_json_value()) == '{"ok":true,"value":[{"sku":"x","price":2}]}'
    with pytest.raises(ValueError, match="not representable"):
        _json.dumps(float("inf"))
    with pytest.raises(ValueError, match="not valid JSON"):
        _json.loads("NaN")


def test_error_encoding_and_descriptions_match_the_go_sdk_output() -> None:
    # 期望值是 Go SDK（framework-sdk-go）对同样输入的实际输出：json.Marshal(err) 与 primitives.Describe
    inner = Error.literal("KERNEL_UNEXPECTED_001", "boom <&>")
    details: dict[str, JsonValue] = {
        "z": 1.5,
        "a": {"y": [1, "x", True, None, 2.0, 1e21, 1e-7], "b": "\u2028\t"},
        "m": 123456789.0,
    }
    error = Error.unexpected("ORDERS_UNEXPECTED_001", "orders.failed", details, inner)
    # 原始字符串：JSON 里的转义（\u2028、\t、\u003c）都是字面的反斜杠序列
    assert error.to_json() == (
        r'{"code":"ORDERS_UNEXPECTED_001","message":"orders.failed (a=map[b:\u2028\t y:[1 x true \u003cnil\u003e 2 '
        r'1e+21 1e-07]], m=1.23456789e+08, z=1.5)","messageKey":"orders.failed","details":{"a":{"b":"\u2028\t",'
        r'"y":[1,"x",true,null,2,1e+21,1e-7]},"m":123456789,"z":1.5},"inner":{"code":"KERNEL_UNEXPECTED_001",'
        r'"message":"boom \u003c\u0026\u003e"}}'
    )
    arguments: dict[str, object] = {
        "b": True,
        "n": None,
        "f": 1e6,
        "g": 100000.0,
        "h": 0.00001,
        "l": [1, "a"],
        "m": {"b": 1, "a": 2},
        "i": 42,
        "s": "x",
    }
    assert primitives.describe("orders.failed", arguments) == (
        "orders.failed (b=true, f=1e+06, g=100000, h=1e-05, i=42, l=[1 a], m=map[a:2 b:1], n=<nil>, s=x)"
    )
    assert primitives.format_message("{0} of {count:%03d} {x:N2}", {"0": "a", "count": 7, "x": 1.5}) == "a of 007 1.5"
