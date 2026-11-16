"""值与 JSON 值之间编解码的测试：操作的请求与响应、Result / Maybe 的值都经过它。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal

import pytest

from xihan_framework import _json
from xihan_framework._codec import DecodeError, Int32, IntRange, compile_decoder, encode, json_field
from xihan_framework._json import JsonNumber
from xihan_framework.primitives import (
    Error,
    JsonValue,
    LifecycleState,
    Maybe,
    Result,
    Stability,
    TypedId,
    Unit,
)


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class Level(Enum):
    LOW = 1
    HIGH = 2


class SkuId(TypedId[str]):
    __slots__ = ()


@dataclass(frozen=True)
class Line:
    sku: SkuId
    quantity: int
    unit_price: float = field(default=0.0, metadata=json_field("unitPrice"))


@dataclass(frozen=True)
class Order:
    lines: tuple[Line, ...]
    color: Color
    stability: Stability
    note: str | None = field(default=None, metadata=json_field(omit_none=True))
    tags: dict[str, int] = field(default_factory=dict)
    coupon: Maybe[str] = field(default_factory=Maybe.none)
    _cache: int = 0


@dataclass(frozen=True)
class Node:
    name: str
    children: list["Node"] = field(default_factory=list)


@dataclass(frozen=True)
class Positive:
    value: int

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError("the value must be positive")


def decode(target: object, value: JsonValue) -> object:
    """编译并解码。"""
    return compile_decoder(target)(value)


def test_dataclasses_round_trip_with_renamed_members() -> None:
    order = Order(
        lines=(Line(SkuId("s1"), 2, 9.5),), color=Color.RED, stability=Stability.PREVIEW, tags={"b": 2, "a": 1}
    )
    encoded = encode(order)
    assert _json.dumps(encoded) == (
        '{"lines":[{"sku":"s1","quantity":2,"unitPrice":9.5}],"color":"red","stability":"preview",'
        '"tags":{"a":1,"b":2},"coupon":null}'
    )
    assert decode(Order, encoded) == order
    with_note = decode(Order, {"lines": [], "color": "blue", "stability": "stable", "note": "n", "coupon": "c"})
    assert with_note == Order(lines=(), color=Color.BLUE, stability=Stability.STABLE, note="n", coupon=Maybe.some("c"))


def test_decoding_reports_only_the_json_path() -> None:
    cases: list[tuple[JsonValue, str]] = [
        ([1], "$"),
        # 缺少必填成员报所在对象的路径（RFC-0011 规则 2，ADR-0236）
        ({"color": "red", "stability": "stable"}, "$"),
        ({"lines": [{"sku": "s1"}], "color": "red", "stability": "stable"}, "$.lines[0]"),
        ({"lines": [{"sku": "s1", "quantity": "2"}], "color": "red", "stability": "stable"}, "$.lines[0].quantity"),
        ({"lines": [{"sku": "s1", "quantity": 1.0}], "color": "red", "stability": "stable"}, "$.lines[0].quantity"),
        ({"lines": [], "color": "green", "stability": "stable"}, "$.color"),
        ({"lines": [], "color": "red", "stability": "solid"}, "$.stability"),
        ({"lines": [], "color": "red", "stability": "stable", "tags": {"a.b": True}}, "$.tags['a.b']"),
        # 按文本顺序：写在前面的类型不符先于缺少的成员，嵌套对象里缺少的成员先于外层后面的类型不符
        ({"tags": {"a": "x"}}, "$.tags.a"),
        ({"lines": [{"sku": "s1"}], "color": 1}, "$.lines[0]"),
        # 只有 X | None、Maybe 与 JsonValue 接受 null
        ({"lines": None, "color": "red", "stability": "stable"}, "$.lines"),
        ({"lines": [], "color": "red", "stability": "stable", "tags": None}, "$.tags"),
    ]
    for value, path in cases:
        with pytest.raises(DecodeError) as raised:
            decode(Order, value)
        assert raised.value.path == path, value
    # null 也算出现：可选的 note 写 null 就是没有值，必填的成员写 null 仍要过它自己的类型
    assert decode(Order, {"lines": [], "color": "red", "stability": "stable", "note": None}) == Order(
        lines=(), color=Color.RED, stability=Stability.STABLE
    )


def test_scalars_are_strict() -> None:
    assert decode(int, 3) == 3
    assert decode(int, -(2**63)) == -(2**63)
    assert decode(float, 3) == 3.0
    for target, value in [
        (int, True),
        (int, 1.5),
        (int, 2**63),
        (float, False),
        (str, 1),
        (str, None),
        (bool, 0),
        (type(None), 0),
    ]:
        with pytest.raises(DecodeError):
            decode(target, value)
    assert decode(int | None, None) is None
    assert decode(Literal["a", 1], 1) == 1
    with pytest.raises(DecodeError):
        decode(Literal[1], True)
    assert decode(Level, 2) is Level.HIGH
    with pytest.raises(DecodeError):
        decode(Level, "2")
    assert decode(Annotated[int, "meta"], 5) == 5
    assert decode(Any, {"x": [1]}) == {"x": [1]}
    assert decode(JsonValue, [None]) == [None]
    assert decode(tuple[int, str], [1, "a"]) == (1, "a")
    with pytest.raises(DecodeError):
        decode(tuple[int, str], [1])


def test_sdk_primitives_decode_from_their_wire_form() -> None:
    assert decode(SkuId, "s1") == SkuId("s1")
    assert decode(LifecycleState, "Ready") is LifecycleState.READY
    assert decode(Maybe[int], None) == Maybe.none()
    assert decode(Result[int], {"ok": True, "value": 4}) == Result.from_json_value(
        {"ok": True, "value": 4}, lambda v: v
    )
    failed = decode(Result[int], {"ok": False, "error": {"code": "ORDERS_CONFLICT_001", "message": "stale"}})
    assert isinstance(failed, Result)
    assert failed.error == Error.literal("ORDERS_CONFLICT_001", "stale")
    with pytest.raises(DecodeError):
        decode(Result[int], {"ok": False})
    assert isinstance(decode(Unit, {}), Unit)
    assert decode(Error, {"code": "ORDERS_CONFLICT_001", "message": "stale"}) == Error.literal(
        "ORDERS_CONFLICT_001", "stale"
    )


def test_constructor_failures_are_payload_failures() -> None:
    with pytest.raises(DecodeError) as raised:
        decode(Positive, {"value": 0})
    assert raised.value.path == "$"
    assert "positive" in raised.value.detail


def test_self_referencing_dataclasses_decode() -> None:
    tree = decode(Node, {"name": "a", "children": [{"name": "b", "children": [{"name": "c"}]}]})
    assert tree == Node("a", [Node("b", [Node("c")])])


def test_unsupported_types_fail_when_compiling() -> None:
    for target in [int | str, dict[int, str], set[int], bytes]:
        with pytest.raises(TypeError):
            compile_decoder(target)


def test_encoding_rejects_values_without_a_json_form() -> None:
    with pytest.raises(TypeError):
        encode({1, 2})
    with pytest.raises(TypeError):
        encode({1: "a"})
    with pytest.raises(ValueError, match="empty"):
        json_field("")


def test_int_ranges_narrow_integers() -> None:
    # Python 只有一种整数：更窄的整数类型用 Annotated[int, IntRange] 声明（RFC-0011 规则 2，ADR-0236）
    assert decode(Annotated[int, Int32], 2**31 - 1) == 2**31 - 1
    assert decode(Annotated[int, Int32], -(2**31)) == -(2**31)
    values: list[JsonValue] = [2**31, -(2**31) - 1, 1.5, True, "1"]
    for value in values:
        with pytest.raises(DecodeError) as raised:
            decode(dict[str, Annotated[int, Int32]], {"a": value})
        assert raised.value.path == "$.a", value
    assert decode(Annotated[int, IntRange(0, 255)], 255) == 255
    for target in [Annotated[str, Int32], Annotated[int, Int32, Int32]]:
        with pytest.raises(TypeError):
            compile_decoder(target)
    for bounds in [(1, 0), (0, 2**63)]:
        with pytest.raises(ValueError, match="64-bit"):
            IntRange(*bounds)


def test_exact_payloads_keep_number_texts() -> None:
    # 任意 JSON 的数字保留原文：值是最接近的双精度浮点，写出时原样写回（RFC-0011 规则 2）
    value = _json.parse("[1e400,2.0,9223372036854775808,3,-0.0]", exact=True)
    assert isinstance(value, list)
    big, two, huge, three, _zero = value
    assert isinstance(big, JsonNumber)
    assert big.text == "1e400"
    assert big == float("inf")
    assert isinstance(two, JsonNumber)
    assert two == 2.0
    assert isinstance(huge, JsonNumber)
    assert huge.text == "9223372036854775808"
    assert type(three) is int
    assert repr(big) == "JsonNumber('1e400')"
    assert str(two) == "2.0"
    assert _json.dumps(value) == "[1e400,2.0,9223372036854775808,3,-0.0]"
    with pytest.raises(ValueError, match="not a JSON number"):
        JsonNumber("1.")
    # 按类型解码时取它的浮点值，Literal 与枚举按值比较
    assert type(decode(float, two)) is float
    assert decode(Literal[2.0], two) == 2.0
    with pytest.raises(DecodeError):
        decode(int, huge)
