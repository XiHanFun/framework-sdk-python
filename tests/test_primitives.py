"""primitives 的测试：一致性向量（错误码、Baggage 键、生命周期迁移）与编码、消息、工厂的用例。"""

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from fractions import Fraction
from typing import Any
from uuid import UUID

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


class _Level(IntEnum):
    HIGH = 2


class _Tone(StrEnum):
    WARM = "warm"


class _Shade(Enum):
    DARK = 1


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


#: RFC-0001「空白字符」的 25 个码位，照规范逐个写出，作为测试的对照。
SPEC_WHITESPACE = (
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
)
#: 为空或只有空白的自由文本：25 个空白字符各一个、合在一起，以及几种常见写法。
BLANK_TEXTS = ["", " ", " \t　", "".join(map(chr, SPEC_WHITESPACE)), *map(chr, SPEC_WHITESPACE)]


@pytest.mark.parametrize("text", BLANK_TEXTS)
def test_free_text_must_not_be_empty_or_whitespace(text: str) -> None:
    assert len(SPEC_WHITESPACE) == 25
    with pytest.raises(FrameworkError) as raised:
        Error.literal("ORDERS_UNEXPECTED_001", text)
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT
    with pytest.raises(FrameworkError) as raised:
        Error.not_found("ORDERS_NOTFOUND_001", "orders.missing").with_text(text)
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT


@pytest.mark.parametrize("text", ["\u001c", "​", "\u001c\u001d\u001e\u001f", " boom\t"])
def test_characters_outside_the_whitespace_table_are_free_text(text: str) -> None:
    # str.isspace 认 U+001C–U+001F，零宽空格也不在规范的表里：它们都不是空白，文案照常接受、原样保留
    assert Error.literal("ORDERS_UNEXPECTED_001", text).message == text
    assert Error.not_found("ORDERS_NOTFOUND_001", "orders.missing").with_text(text).message == text


def test_error_details_must_be_json_values() -> None:
    with pytest.raises(FrameworkError) as raised:
        Error("ORDERS_VALIDATION_001", "orders.invalid", {"ids": {1, 2}})  # type: ignore[dict-item]
    assert "'ids'" in raised.value.message
    # 非有限的浮点是值模型里的值，写出时是字符串（RFC-0001「Result」，ADR-0236）
    special = Error("ORDERS_VALIDATION_001", "orders.invalid", {"ratio": float("nan"), "top": float("-inf")})
    assert '"details":{"ratio":"NaN","top":"-Infinity"}' in special.to_json()
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
    # printf 风格的 %03d 不是可移植的说明符，按无格式输出（RFC-0010「模板」，ADR-0236）
    assert primitives.format_message("{0} of {count:%03d}", {"0": "a", "count": 7}) == "a of 7"
    assert primitives.format_message("{missing}", None) == "{missing}"
    assert primitives.format_message("{{id}} is {id}", {"id": 1}) == "{id} is 1"
    assert primitives.format_message("{0}-{1}", primitives.positional("x", 2)) == "x-2"
    assert primitives.format_message("{amount:N2}", {"amount": 1.5}) == "1.50"
    assert primitives.format_message("{amount:N2}", {"amount": 1234.5}, locale="fr-FR") == "1" + chr(0x202F) + "234,50"
    assert primitives.try_format_message("{amount:F1}", {"amount": 2.25}, locale="DE-de") == "2,2"
    assert primitives.format_message("{when:yyyy-MM-dd}", {"when": "2026-11-16"}) == "2026-11-16"
    assert primitives.format_message("{n:F2}", {"n": "seven"}) == "seven"
    assert primitives.format_message("{n:X}", {"n": -1}) == "-1"
    assert primitives.format_message("{_x}", {"_x": 1}) == "{_x}"
    # 没有闭合的 { 之后全部原样输出，里面的 {{ 也不再当转义；有闭合但名字不合法的整段原样输出
    assert primitives.format_message("a {b {{c", {"b": 1}) == "a {b {{c"
    assert primitives.format_message("a {b {{c}", {"b": 1}) == "a {b {{c}"
    assert primitives.format_message("{a b {x}}", {"x": 1}) == "{a b {x}}"
    assert primitives.placeholders("{a} {b:%d} {a} {{c}} {0} {bad name}") == ["a", "b", "0"]
    assert primitives.try_format_message("{a} {b}", {"a": 1}) is None
    assert primitives.describe("orders.failed", {"z": 1, "a": "x"}) == "orders.failed (a=x, z=1)"
    assert primitives.describe("orders.failed", {}) == "orders.failed"


def test_arguments_follow_the_text_forms() -> None:
    cases: list[tuple[object, str]] = [
        (True, "true"),
        (None, ""),
        (42, "42"),
        (1.5, "1.5"),
        (2.0, "2"),
        (100000.0, "100000"),
        (1e6, "1000000"),
        (0.0001, "0.0001"),
        (123456789.0, "123456789"),
        (999999999999999.9, "999999999999999.9"),
        (-0.0, "-0"),
        # 1e-4 ≤ |x| < 1e17 不用指数（Python 的 repr 到 1e16 就换成指数），范围外写值模型的指数形式（ADR-0235）
        (1e15, "1000000000000000"),
        (1e16, "10000000000000000"),
        (1e17, "1E+17"),
        (0.00001, "1E-05"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
        (float("nan"), "NaN"),
        # 值模型（RFC-0010「模板」，ADR-0236）：超出 64 位的整数、Decimal 与其他实数是浮点，IntEnum 是整数
        (2**63, "9.223372036854776E+18"),
        (10**400, "Infinity"),
        (Decimal("2.50"), "2.5"),
        (Fraction(1, 4), "0.25"),
        (_Level.HIGH, "2"),
        (_Tone.WARM, "warm"),
        # 数组与对象写成紧凑的 JSON，对象成员按名字的 UTF-16 码元序排列
        ([1, "a", None, 2.0, [True]], '[1,"a",null,2,[true]]'),
        ((1, 2), "[1,2]"),
        (b"\x01\xff", "[1,255]"),
        ({"b": 1, "a": {"d": None}}, '{"a":{"d":null},"b":1}'),
        ({2: "x", 10: "y"}, '{"10":"y","2":"x"}'),
        # 模型之外的值取 str()
        (_Shade.DARK, "_Shade.DARK"),
        (datetime(2026, 11, 16, 8, 30), "2026-11-16 08:30:00"),
        (UUID(int=1), "00000000-0000-0000-0000-000000000001"),
        ([_Shade.DARK], '["_Shade.DARK"]'),
    ]
    for value, text in cases:
        assert primitives.format_argument(value) == text, value
    # 超过 64 层的容器写 null；自引用的列表因此也写得出
    nested: list[object] = []
    nested.append(nested)
    assert primitives.format_argument(nested) == "[" * 64 + "null" + "]" * 64


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
    # 细节的键按字典序写出，输出不随插入顺序变化
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
    assert success().to_json() == '{"ok":true}'
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
    # RFC-0001 里默认值的 Result 是未初始化的失败（PRIMITIVES_VALIDATION_002）；Python 只能经工厂创建，直接构造即编程错误
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


# 写进源码的不可见字符与转义用 chr 拼出
_BACKSLASH = chr(92)
_LINE_SEPARATOR = chr(0x2028)


def test_json_is_written_by_the_value_model() -> None:
    # 整数值的浮点带 .0 或指数，读回来仍是浮点；超出 64 位的整数按浮点写；非有限的浮点写成字符串（RFC-0001「Result」，ADR-0236）
    numbers: list[JsonValue] = [
        2.0,
        0.1,
        1e16,
        1e20,
        1e-6,
        1.5e-10,
        123456789.0,
        -0.0,
        3.14159,
        5e-324,
        1.7976931348623157e308,
        2,
        9007199254740993,
        2**63,
        float("nan"),
        float("inf"),
        float("-inf"),
    ]
    assert _json.dumps(numbers) == (
        "[2.0,0.1,10000000000000000.0,1E+20,1E-06,1.5E-10,123456789.0,-0.0,3.14159,5E-324,1.7976931348623157E+308,"
        '2,9007199254740993,9.223372036854776E+18,"NaN","Infinity","-Infinity"]'
    )
    # 字符串只转义必须转义的字符（RFC-0010「模板」的 JSON 字符串写法）
    text = "<a&b>/" + _LINE_SEPARATOR + chr(0x7F) + chr(0xE9) + "\n\t" + chr(1) + chr(0x1F) + '"' + _BACKSLASH
    assert _json.dumps(text) == (
        '"<a&b>/'
        + _LINE_SEPARATOR
        + chr(0x7F)
        + chr(0xE9)
        + _BACKSLASH
        + "n"
        + _BACKSLASH
        + "t"
        + _BACKSLASH
        + "u0001"
        + _BACKSLASH
        + "u001f"
        + _BACKSLASH
        + '"'
        + _BACKSLASH
        + _BACKSLASH
        + '"'
    )
    assert _json.dumps(chr(0xD800)) == '"' + _BACKSLASH + 'ud800"'
    assert _json.dumps(chr(0xD83D) + chr(0xDE00)) == '"' + chr(0x1F600) + '"'
    assert _json.dumps({"b": [], "a": {}}, indent=True) == '{\n  "b": [],\n  "a": {}\n}'
    assert _json.dumps(ok([_Line("x", 2.0)]).to_json_value()) == '{"ok":true,"value":[{"sku":"x","price":2.0}]}'


def test_json_text_follows_the_vector(vector: VectorLoader) -> None:
    # 共同的 JSON 读取检查（RFC-0001「JSON 文本」，ADR-0236）：语法错误只写 $，重复成员写第一处重复的路径
    for case in vector("primitives", "json-text")["cases"]:
        data: str | bytes = bytes.fromhex(case["hex"]) if "hex" in case else case["text"]
        if case["expect"] == "valid":
            _json.parse(data)
            continue
        with pytest.raises(_json.JsonTextError) as raised:
            _json.parse(data)
        assert raised.value.syntax == (case["expect"] == "syntax"), case["name"]
        assert raised.value.path == case.get("path", "$"), case["name"]
    # 数字按值模型读：64 位范围内的整数写法是整数，其余是浮点，上溢为无穷
    assert _same(
        _json.parse("[9007199254740993,-9223372036854775808,9223372036854775808,-0,-0.0,2.0,1e2,1e400,1e-400]"),
        [9007199254740993, -(2**63), 9.223372036854776e18, 0, -0.0, 2.0, 100.0, float("inf"), 0.0],
    )
    # Python 的 str 里的代理项码点不是合法的 Unicode
    with pytest.raises(_json.JsonTextError):
        _json.parse('"' + chr(0xD800) + '"')


def test_error_encoding_bytes() -> None:
    inner = Error.literal("KERNEL_UNEXPECTED_001", "boom <&>")
    details: dict[str, JsonValue] = {
        "z": 1.5,
        "a": {"y": [1, "x", True, None, 2.0, 1e21, 1e-7], "b": _LINE_SEPARATOR + "\t"},
        "m": 123456789.0,
    }
    error = Error.unexpected("ORDERS_UNEXPECTED_001", "orders.failed", details, inner)
    # message 里的描述按 RFC-0010「模板」：数组与对象写紧凑的 JSON、对象成员按名字排序，数字写法同顶层（2.0 写 2）
    description = (
        'orders.failed (a={"b":"' + _LINE_SEPARATOR + _BACKSLASH + 't","y":[1,"x",true,null,2,1E+21,1E-07]}, '
        "m=123456789, z=1.5)"
    )
    assert error.message == description
    # 线上的细节按值模型写：整数值的浮点带 .0
    assert error.to_json() == (
        '{"code":"ORDERS_UNEXPECTED_001","message":' + _json.quote(description) + ',"messageKey":"orders.failed",'
        '"details":{"a":{"b":"' + _LINE_SEPARATOR + _BACKSLASH + 't","y":[1,"x",true,null,2.0,1E+21,1E-07]},'
        '"m":123456789.0,"z":1.5},"inner":{"code":"KERNEL_UNEXPECTED_001","message":"boom <&>"}}'
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
        'orders.failed (b=true, f=1000000, g=100000, h=1E-05, i=42, l=[1,"a"], m={"a":2,"b":1}, n=, s=x)'
    )
    assert primitives.format_message("{0} of {count:%03d} {x:N2}", {"0": "a", "count": 7, "x": 1.5}) == "a of 7 1.50"


def _same(left: object, right: object) -> bool:
    """按值模型比较两个 JSON 值：布尔、整数与浮点互不相等，整数精确比较，浮点按值（连同负零的符号）比较。"""
    if isinstance(left, bool) or isinstance(right, bool) or left is None or right is None:
        return left is right
    if isinstance(left, int) or isinstance(right, int):
        return type(left) is type(right) and left == right
    if isinstance(left, float) or isinstance(right, float):
        if not (isinstance(left, float) and isinstance(right, float)):
            return False
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    if isinstance(left, str) or isinstance(right, str):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[name], right[name]) for name in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    return False


def _identity(value: JsonValue) -> JsonValue:
    """把 JSON 值原样当作结果的值。"""
    return value


def _read_result(kind: str, value: JsonValue) -> JsonValue:
    """按向量的 kind 读取一条结果，再写成 JSON 值。"""
    if kind == "unit":
        return Result.unit_from_json_value(value).to_json_value()
    if kind == "maybe":
        return Result.maybe_from_json_value(value, _identity).to_json_value()
    assert kind == "value"
    return Result.from_json_value(value, _identity).to_json_value()


def test_result_encoding_follows_the_wire_vectors(vector: VectorLoader) -> None:
    results = vector("primitives", "wire-encoding")["results"]
    for case in results["valid"]:
        # 再写出时按 JSON 值等于规范形式，不比较成员顺序；数字按值模型比较，所以经文本读写一遍（ADR-0236）
        assert _same(_read_result(case["kind"], case["json"]), case.get("canonical", case["json"])), case["name"]
        text = _read_text("result", case["kind"], _json.dumps(case["json"]))
        assert isinstance(text, Result)
        assert _same(_json.parse(text.to_json()), case.get("canonical", case["json"])), case["name"]
    for case in results["invalid"]:
        with pytest.raises(FrameworkError) as raised:
            _read_result(case["kind"], case["json"])
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, case["name"]


def test_error_encoding_follows_the_wire_vectors(vector: VectorLoader) -> None:
    errors = vector("primitives", "wire-encoding")["errors"]
    for case in errors["valid"]:
        decoded = Error.from_json_value(case["json"])
        assert _same(decoded.to_json_value(), case.get("canonical", case["json"])), case["name"]
        text = Error.from_json(_json.dumps(case["json"]))
        assert _same(_json.parse(text.to_json()), case.get("canonical", case["json"])), case["name"]
    for case in errors["invalid"]:
        with pytest.raises(FrameworkError) as raised:
            Error.from_json_value(case["json"])
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, case["name"]


def test_unit_and_maybe_results_write_their_canonical_form() -> None:
    assert success().to_json_value() == {"ok": True}
    assert ok(Maybe.none()).to_json() == '{"ok":true,"value":null}'
    assert ok(Maybe.some(3)).to_json() == '{"ok":true,"value":3}'
    assert Result.unit_from_json('{"ok":true}') == success()
    assert Result.maybe_from_json('{"ok":true,"value":null}', _identity).value == Maybe.none()
    with pytest.raises(FrameworkError):
        Result.unit_from_json('{"ok":true,"value":{}}')


def test_message_templates_follow_the_vectors(vector: VectorLoader) -> None:
    vectors = vector("primitives", "message-templates")
    for key in vectors["keys"]["valid"]:
        assert primitives.is_message_key(key), key
    for key in vectors["keys"]["invalid"]:
        assert not primitives.is_message_key(key), key
    for case in vectors["format"]:
        # 向量里的 JSON 值按原类型交给格式化：整数、浮点、布尔、字符串、null、数组与对象；可带区域（ADR-0236）
        locale = case.get("locale")
        lenient = primitives.format_message(case["template"], case["arguments"], locale=locale)
        assert lenient == case["lenient"], case["name"]
        strict = primitives.try_format_message(case["template"], case["arguments"], locale=locale)
        assert strict == case["strict"], case["name"]
        if locale is not None:
            # 本地化解析按请求的区域取符号
            catalog = Catalog({locale: {"tests.template": case["template"]}})
            assert catalog.resolve("tests.template", locale, case["arguments"]) == case["strict"], case["name"]
    for case in vectors["describe"]:
        assert primitives.describe(case["key"], case["arguments"]) == case["text"], case["key"]


def test_catalogs_resolve_strictly_and_fall_back_to_the_description() -> None:
    catalog = Catalog({"en-US": {"orders.missing": "Order {id} of {tenant}."}})
    assert catalog.resolve("orders.missing", "en-US", {"id": 1}) is None
    assert catalog.resolve("orders.missing", "en-US", {"id": 1, "tenant": None}) == "Order 1 of ."
    long_key = "a." + "b" * (primitives.MAX_MESSAGE_KEY_LENGTH - 2)
    assert primitives.is_message_key(long_key)
    assert not primitives.is_message_key(long_key + "b")


def test_baggage_values_follow_the_vectors(vector: VectorLoader) -> None:
    values = vector("primitives", "baggage-keys")["values"]
    for value in values["acceptable"]:
        assert primitives.is_baggage_value_acceptable(value), value
    for value in values["unacceptable"]:
        assert not primitives.is_baggage_value_acceptable(value), value
    # 长度按 UTF-16 码元计：基本多文种平面以外的字符算两个，按码点计会把 255 个 a 加一个表情算成 256
    assert primitives.is_baggage_value_acceptable("a" * 254 + "\U0001f600")
    assert not primitives.is_baggage_value_acceptable("a" * 255 + "\U0001f600")


def test_describe_orders_arguments_by_utf16_code_units() -> None:
    # U+FF5E 的码点比 U+1F600 小，但 UTF-16 的码元 0xFF5E 比代理项 0xD83D 大：按码元排，表情在前
    assert primitives.describe("a.b", {"\uff5e": 1, "\U0001f600": 2}) == "a.b (\U0001f600=2, \uff5e=1)"
    assert primitives.describe("a.b", {"b": 1, "B": 2, "a": 3}) == "a.b (B=2, a=3, b=1)"


def test_duplicate_members_are_rejected(vector: VectorLoader) -> None:
    # JSON 值表达不了重复的成员名，向量给的是原文；任何一层重复即不合法，含 value、details 与 inner 的内部（ADR-0236）
    for case in vector("primitives", "wire-encoding")["duplicates"]:
        with pytest.raises(FrameworkError) as raised:
            _read_text(case["target"], case.get("kind"), case["text"])
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, case["name"]
    with pytest.raises(_json.JsonTextError) as duplicate:
        _json.parse('{"a":[{"b":1,"b":2}]}')
    assert (duplicate.value.syntax, duplicate.value.path) == (False, "$.a[0].b")


def _read_text(target: str, kind: str | None, text: str) -> object:
    """按向量的 target 与 kind 从原文读取一条 Error 或 Result。"""
    if target == "error":
        return Error.from_json(text)
    if kind == "unit":
        return Result.unit_from_json(text)
    if kind == "maybe":
        return Result.maybe_from_json(text, _identity)
    return Result.from_json(text, _identity)
