"""JSON 的读取与规范化写出（内部模块）。

写出与 Go SDK 的 ``encoding/json`` 逐字节相同：对象成员按给定顺序，紧凑或两空格缩进，浮点数取最短往返表示并按
Go 的规则选定点数或指数形式，字符串转义 ``<``、``>``、``&``、U+2028、U+2029，孤立代理项写成 ``\\ufffd``。
读取拒绝 ``NaN`` / ``Infinity`` 这类非标准常量与带 BOM 的文本。
"""

import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal

type JsonValue = bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue] | None
"""JSON 可表示的值：``null``、布尔、整数、浮点、字符串、数组与键为字符串的对象。"""

# 只出现在字符串里的字符：结构字符不含它们，所以可以在整段文本上替换
_HTML_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", " ": "\\u2028", " ": "\\u2029"}
_HTML_PATTERN = re.compile("[<>&  ]")
# Python 字符串里成对的代理项已合成一个码点，剩下的代理项都是孤立的；Go 把无效 UTF-8 写成 �
_SURROGATE_PATTERN = re.compile("[\ud800-\udfff]")


def loads(text: str | bytes) -> JsonValue:
    """解析 JSON 文本；字节按 UTF-8 解码，非标准常量与 BOM 都是 ``ValueError``。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    value: JsonValue = json.loads(text, parse_constant=_reject_constant)
    return value


def dumps(value: JsonValue, *, indent: bool = False) -> str:
    """写出 JSON：紧凑形式，或与 Go 的 ``MarshalIndent(value, "", "  ")`` 相同的两空格缩进。"""
    parts: list[str] = []
    _write(value, parts, 0 if indent else None)
    return "".join(parts)


def format_float(value: float) -> str:
    """按 Go ``encoding/json`` 的规则写出浮点数：``1e-6 <= |v| < 1e21`` 用定点形式，其余用指数形式。"""
    if not math.isfinite(value):
        raise ValueError(f"The number {value!r} is not representable in JSON.")
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    digits, point = shortest_digits(value)
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if 1e-6 <= magnitude < 1e21:
        return sign + fixed_notation(digits, point)
    text = sign + exponent_notation(digits, point)
    # Go 把 e-07 收成 e-7，正指数与两位以上的负指数不变
    if text[-4] == "e" and text[-3] == "-" and text[-2] == "0":
        text = text[:-2] + text[-1]
    return text


def shortest_digits(value: float) -> tuple[str, int]:
    """最短往返的十进制数字与小数点位置：``|value| = 0.digits × 10**point``，数字串不含首尾的 0。

    ``repr`` 与 Go 的 ``strconv`` 都给出最短往返表示，所以两边的数字串相同。
    """
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    assert isinstance(exponent, int)
    digits = "".join(str(digit) for digit in digit_tuple).lstrip("0")
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    return stripped, len(stripped) + exponent


def fixed_notation(digits: str, point: int) -> str:
    """把数字串按小数点位置写成定点形式，不补多余的 0。"""
    if point <= 0:
        return "0." + "0" * -point + digits
    if point >= len(digits):
        return digits + "0" * (point - len(digits))
    return digits[:point] + "." + digits[point:]


def exponent_notation(digits: str, point: int) -> str:
    """把数字串写成 Go 的指数形式：``d[.ddd]e±XX``，指数至少两位。"""
    exponent = point - 1
    mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    return f"{mantissa}e{'-' if exponent < 0 else '+'}{abs(exponent):02d}"


def quote(text: str) -> str:
    """写出 JSON 字符串字面量，转义规则与 Go 的 ``encoding/json`` 相同。"""
    quoted = json.dumps(text, ensure_ascii=False)
    quoted = _HTML_PATTERN.sub(lambda match: _HTML_ESCAPES[match.group()], quoted)
    return _SURROGATE_PATTERN.sub(lambda _: "\\ufffd", quoted)


def _write(value: JsonValue, parts: list[str], depth: int | None) -> None:
    """递归写出一个值；``depth`` 为空表示紧凑形式。"""
    if value is None:
        parts.append("null")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif isinstance(value, int):
        parts.append(str(value))
    elif isinstance(value, float):
        parts.append(format_float(value))
    elif isinstance(value, str):
        parts.append(quote(value))
    elif isinstance(value, Mapping):
        _write_members(list(value.items()), parts, depth)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        _write_elements(list(value), parts, depth)
    else:
        raise TypeError(f"The value of type '{type(value).__name__}' is not a JSON value.")


def _write_members(members: list[tuple[str, JsonValue]], parts: list[str], depth: int | None) -> None:
    """写出对象成员，顺序按给定的顺序。"""
    if not members:
        parts.append("{}")
        return
    parts.append("{")
    for index, (name, member) in enumerate(members):
        if not isinstance(name, str):
            raise TypeError(f"The JSON object key {name!r} is not a string.")
        if index:
            parts.append(",")
        _newline(parts, depth, 1)
        parts.append(quote(name))
        parts.append(":" if depth is None else ": ")
        _write(member, parts, None if depth is None else depth + 1)
    _newline(parts, depth, 0)
    parts.append("}")


def _write_elements(elements: list[JsonValue], parts: list[str], depth: int | None) -> None:
    """写出数组元素。"""
    if not elements:
        parts.append("[]")
        return
    parts.append("[")
    for index, element in enumerate(elements):
        if index:
            parts.append(",")
        _newline(parts, depth, 1)
        _write(element, parts, None if depth is None else depth + 1)
    _newline(parts, depth, 0)
    parts.append("]")


def _newline(parts: list[str], depth: int | None, extra: int) -> None:
    """缩进形式下换行并缩进到 ``depth + extra`` 层。"""
    if depth is not None:
        parts.append("\n" + "  " * (depth + extra))


def _reject_constant(name: str) -> JsonValue:
    """拒绝 ``NaN``、``Infinity``、``-Infinity``：它们不是 JSON。"""
    raise ValueError(f"'{name}' is not valid JSON.")
