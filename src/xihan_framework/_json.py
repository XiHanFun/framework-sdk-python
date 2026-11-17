"""JSON 文本的共同读取检查、值模型与写出（内部模块）。

读取（RFC-0001「JSON 文本」，ADR-0236）：Result 与 Error 的线上编码、操作载荷与清单都先过 ``parse``。语法按 RFC 8259，
嵌套不超过 64 层，字符串与成员名必须是合法的 Unicode，任何一层都不能有重复的成员名；语法错误不论出现在哪里都优先，
位置只写 ``$``，重复成员写第一处重复的路径。标准库的 ``json.loads`` 接受 ``NaN``、孤立的代理项、重复成员与很深的嵌套，
所以这里自己读。

数字按值模型（RFC-0010「模板」）：整数写法且在 64 位有符号范围内的是 ``int``，其余是 ``float``（上溢为无穷，下溢为 0）；
``-0`` 是整数 0，``-0.0`` 是负零。写出时整数值的浮点带 ``.0``，非有限的浮点写成字符串 ``NaN``、``Infinity``、
``-Infinity``，超出 64 位的整数按浮点写出，让对方按同一模型读回同一个值。字符串按 RFC-0010 的 JSON 字符串写法转义。
"""

import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

type JsonValue = bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue] | None
"""JSON 可表示的值：``null``、布尔、整数、浮点、字符串、数组与键为字符串的对象。"""

#: 嵌套的最大层数，最外层的对象或数组是第 1 层。
MAX_DEPTH: Final = 64
#: 值模型里整数的范围：64 位有符号整数。
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1

# 浮点在这个范围内写最短往返的定点形式（RFC-0010「值的文本形式」）
_PLAIN_FLOAT_MIN: Final = 1e-4
_PLAIN_FLOAT_MAX: Final = 1e17

# 成员名含这些字符时路径写成 ['名字']；U+0085、U+2028、U+2029 用 chr 写出，源码里不放不可见字符
_BRACKETED_CHARACTERS: Final = frozenset(". '/\"[]()\\\t\n\r\f\b") | {chr(0x85), chr(0x2028), chr(0x2029)}

_WHITESPACE = re.compile(r"[ \t\n\r]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
# 字符串里不必转义、也不是控制字符的一段
_PLAIN = re.compile(r'[^"\\\x00-\x1f]*')
_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")
# Python 的 str 以码点存放，里面的代理项码点都不是合法的 Unicode 标量值；范围用 chr 拼出
_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")
_SHORT_ESCAPES: Final = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

# 写出：要转义的字符与成对的代理项码点（合成一个字符原样写出）
_QUOTED = re.compile(
    f'["\\\\\\x00-\\x1f]|[{chr(0xD800)}-{chr(0xDBFF)}][{chr(0xDC00)}-{chr(0xDFFF)}]|[{chr(0xD800)}-{chr(0xDFFF)}]'
)
_QUOTE_ESCAPES: Final = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class JsonNumber(float):
    """读到的数字原文：任意 JSON 类型的成员原样保留数字（RFC-0011 规则 2，ADR-0236）。

    它是 ``float``，值是原文最接近的双精度浮点（``1e400`` 为无穷），可以照常参与运算；``text`` 是读到的写法，写出时
    原样写回，所以 ``1e400``、超出双精度的整数与 ``2.0`` 都与读到的 JSON 值相等，不变成 ``null``、无穷或近似值。
    载荷按 ``parse(..., exact=True)`` 读时，64 位范围以外的整数与带小数点或指数的数字都是它。
    """

    __slots__ = ("text",)

    text: str

    def __new__(cls, text: str) -> "JsonNumber":
        """用 JSON 数字的原文创建；不是 JSON 数字抛出 ``ValueError``。"""
        if _NUMBER.fullmatch(text) is None:
            raise ValueError(f"'{text}' is not a JSON number.")
        number = super().__new__(cls, text)
        number.text = text
        return number

    def __repr__(self) -> str:
        """``JsonNumber('1e400')``。"""
        return f"JsonNumber({self.text!r})"

    __str__ = float.__repr__


class JsonTextError(ValueError):
    """JSON 文本没有通过共同检查。

    ``syntax`` 为真是语法错误（RFC 8259 之外的写法、BOM、嵌套超过 64 层、非法 UTF-8、孤立的代理项），``path`` 只写
    ``$``；为假是重复成员，``path`` 是第一处重复（第二次出现的成员）的路径。
    """

    def __init__(self, syntax: bool, path: str) -> None:
        """用种类与位置创建。"""
        super().__init__(f"The text is not valid JSON at {path}." if syntax else f"The member {path} is duplicated.")
        self.syntax = syntax
        self.path = path


def parse(text: str | bytes, *, exact: bool = False) -> JsonValue:
    """过共同检查并读出 JSON 值；不通过抛出 ``JsonTextError``。

    字节按 UTF-8 解码，非法的 UTF-8（含过长编码与编码成 UTF-8 的代理项）是语法错误；开头的 BOM 不跳过，是语法错误。
    字符串里的代理项码点同样是语法错误。对象读成按文本顺序的 ``dict``，数组读成 ``list``，数字按值模型；``exact``
    时 64 位范围以外的整数与带小数点或指数的数字读成保留原文的 ``JsonNumber``（操作载荷用）。
    """
    if isinstance(text, (bytes, bytearray, memoryview)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            raise JsonTextError(True, "$") from None
    elif _SURROGATE.search(text) is not None:
        raise JsonTextError(True, "$")
    reader = _Reader(text, exact)
    try:
        value = reader.document()
    except _SyntaxError:
        raise JsonTextError(True, "$") from None
    if reader.duplicate is not None:
        raise JsonTextError(False, reader.duplicate)
    return value


def member_path(path: str, name: str) -> str:
    """对象成员的 JSON 路径（RFC-0011 规则 2，照 System.Text.Json 的实测）。

    名字含 ``.``、空格、``'``、``/``、``"``、``[``、``]``、``(``、``)``、``\\``、制表、换行、回车、换页、退格、U+0085、
    U+2028、U+2029 之一时写成 ``$['名字']``，名字原样、不转义；其余写成 ``$.名字``，空名字写成 ``$.``。
    """
    if any(character in _BRACKETED_CHARACTERS for character in name):
        return f"{path}['{name}']"
    return f"{path}.{name}"


def dumps(value: JsonValue, *, indent: bool = False, strict: bool = False) -> str:
    """写出 JSON：紧凑形式，或两个空格缩进、LF 换行、``": "`` 分隔、空容器写 ``[]`` / ``{}`` 的缩进形式。

    对象成员按给定的顺序；数字与字符串的写法见模块说明，``JsonNumber`` 写回它的原文。``strict`` 用于响应编码
    （RFC-0011 规则 10 的「编码」）：非有限的浮点写不出，抛出 ``ValueError``，不写成字符串或 ``null``。
    """
    parts: list[str] = []
    _write(value, parts, 0 if indent else None, strict)
    return "".join(parts)


def number_text(value: int | float) -> str:
    """数字的文本形式（RFC-0010「值的文本形式」），模板与 ``Describe`` 用它，不随区域变化。

    整数十进制；超出 64 位的整数按值模型是浮点。浮点在 1e-4 ≤ |x| < 1e17 或为 0 时写最短往返的定点形式，整数值不带
    小数点（``2.0`` 写 ``2``，1e16 写 ``10000000000000000``），范围外写最短往返的有效数字加 ``E``、符号与至少两位的指数
    （``1E+17``、``9.9E-05``、``5E-324``）；负零写 ``-0``，非数与无穷写 ``NaN``、``Infinity``、``-Infinity``。
    """
    if isinstance(value, int):
        if INT64_MIN <= value <= INT64_MAX:
            return str(value)
        value = int_to_float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    digits, point = shortest_digits(value)
    sign = "-" if value < 0 else ""
    if _PLAIN_FLOAT_MIN <= abs(value) < _PLAIN_FLOAT_MAX:
        return sign + fixed_notation(digits, point)
    return sign + exponent_notation(digits, point)


def int_to_float(value: int) -> float:
    """超出 64 位的整数按值模型换成最接近的双精度浮点；超出双精度范围时为带符号的无穷。"""
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def shortest_digits(value: float) -> tuple[str, int]:
    """最短往返的十进制数字与小数点位置：``|value| = 0.digits × 10**point``，数字串不含首尾的 0。

    ``repr`` 给出最短往返表示，即值模型写浮点时取的数字（RFC-0010「模板」）。
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
    """把数字串写成值模型的指数形式：``d[.ddd]E±XX``，指数至少两位（RFC-0010「模板」，ADR-0235）。"""
    exponent = point - 1
    mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    return f"{mantissa}E{'-' if exponent < 0 else '+'}{abs(exponent):02d}"


def quote(text: str) -> str:
    """JSON 字符串字面量（RFC-0010「模板」的 JSON 字符串写法，清单写出也用它）。

    ``"`` 与 ``\\`` 加反斜杠；退格、换页、换行、回车、制表符写短转义；其余 U+0000–U+001F 与孤立的代理项写成小写
    十六进制的 ``\\u001f``、``\\ud800``；别的字符（含 ``/``、U+007F–U+009F、U+2028 与非 ASCII）原样。
    """
    return '"' + _QUOTED.sub(_escape, text) + '"'


def _escape(match: re.Match[str]) -> str:
    """一个要转义的字符；成对的代理项码点合成一个字符原样写出。"""
    found = match.group()
    if len(found) == 2:
        high, low = ord(found[0]), ord(found[1])
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))
    short = _QUOTE_ESCAPES.get(found)
    return short if short is not None else f"\\u{ord(found):04x}"


def _write(value: JsonValue, parts: list[str], depth: int | None, strict: bool) -> None:
    """递归写出一个值；``depth`` 为空表示紧凑形式，``strict`` 时非有限的浮点写不出。"""
    if value is None:
        parts.append("null")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif isinstance(value, int):
        if INT64_MIN <= value <= INT64_MAX:
            parts.append(str(value))
        else:
            _write_float(int_to_float(value), parts, strict)
    elif isinstance(value, JsonNumber):
        parts.append(value.text)
    elif isinstance(value, float):
        _write_float(value, parts, strict)
    elif isinstance(value, str):
        parts.append(quote(value))
    elif isinstance(value, Mapping):
        _write_members(list(value.items()), parts, depth, strict)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        _write_elements(list(value), parts, depth, strict)
    else:
        raise TypeError(f"The value of type '{type(value).__name__}' is not a JSON value.")


def _write_float(value: float, parts: list[str], strict: bool) -> None:
    """浮点：整数值的写成带 ``.0`` 的写法，让对方读回浮点；非有限的写成字符串，``strict`` 时写不出。"""
    if not math.isfinite(value):
        if strict:
            raise ValueError(f"The number {number_text(value)} cannot be written as JSON.")
        parts.append(quote(number_text(value)))
        return
    text = number_text(value)
    parts.append(text if "." in text or "E" in text else text + ".0")


def _write_members(members: list[tuple[str, JsonValue]], parts: list[str], depth: int | None, strict: bool) -> None:
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
        _write(member, parts, None if depth is None else depth + 1, strict)
    _newline(parts, depth, 0)
    parts.append("}")


def _write_elements(elements: list[JsonValue], parts: list[str], depth: int | None, strict: bool) -> None:
    """写出数组元素。"""
    if not elements:
        parts.append("[]")
        return
    parts.append("[")
    for index, element in enumerate(elements):
        if index:
            parts.append(",")
        _newline(parts, depth, 1)
        _write(element, parts, None if depth is None else depth + 1, strict)
    _newline(parts, depth, 0)
    parts.append("]")


def _newline(parts: list[str], depth: int | None, extra: int) -> None:
    """缩进形式下换行并缩进到 ``depth + extra`` 层。"""
    if depth is not None:
        parts.append("\n" + "  " * (depth + extra))


class _SyntaxError(Exception):
    """读取途中发现的语法错误；``parse`` 把它换成 ``JsonTextError``。"""


class _Reader:
    """按 RFC 8259 读一段文本，边读边记下第一处重复成员；语法错误读到末尾之前也优先。"""

    __slots__ = ("duplicate", "exact", "text")

    def __init__(self, text: str, exact: bool) -> None:
        """用整段文本创建；``exact`` 时值模型之外的数字保留原文。"""
        self.text = text
        self.exact = exact
        self.duplicate: str | None = None

    def document(self) -> JsonValue:
        """读一个根值，前后只能有空白。"""
        value, index = self._value(self._skip(0), 1, "$")
        if self._skip(index) != len(self.text):
            raise _SyntaxError
        return value

    def _skip(self, index: int) -> int:
        """跳过空白。"""
        match = _WHITESPACE.match(self.text, index)
        assert match is not None
        return match.end()

    def _path(self, path: str, step: str) -> str:
        """已经找到重复成员后路径不再有用，省去拼接。"""
        return path + step if self.duplicate is None else path

    def _value(self, index: int, depth: int, path: str) -> tuple[JsonValue, int]:
        """读一个值；``depth`` 是这里的对象或数组所在的层。"""
        text = self.text
        if index >= len(text):
            raise _SyntaxError
        character = text[index]
        if character == "{":
            return self._object(index + 1, depth, path)
        if character == "[":
            return self._array(index + 1, depth, path)
        if character == '"':
            return self._string(index + 1)
        for literal, constant in (("true", True), ("false", False), ("null", None)):
            if text.startswith(literal, index):
                return constant, index + len(literal)
        match = _NUMBER.match(text, index)
        if match is None:
            raise _SyntaxError
        literal = match.group()
        if match.group(1) is None and match.group(2) is None and len(literal) <= 20:
            # 整数写法：64 位范围内是整数；先看长度，避免 int() 碰上超长数字串的上限
            number = int(literal)
            if INT64_MIN <= number <= INT64_MAX:
                return number, match.end()
        return (JsonNumber(literal) if self.exact else float(literal)), match.end()

    def _object(self, index: int, depth: int, path: str) -> tuple[JsonValue, int]:
        """读对象的成员，成员名重复时记下第一处。"""
        if depth > MAX_DEPTH:
            raise _SyntaxError
        text = self.text
        members: dict[str, JsonValue] = {}
        index = self._skip(index)
        if index < len(text) and text[index] == "}":
            return members, index + 1
        while True:
            if index >= len(text) or text[index] != '"':
                raise _SyntaxError
            name, index = self._string(index + 1)
            path_of_member = member_path(path, name) if self.duplicate is None else path
            index = self._skip(index)
            if index >= len(text) or text[index] != ":":
                raise _SyntaxError
            if name in members and self.duplicate is None:
                self.duplicate = path_of_member
            value, index = self._value(self._skip(index + 1), depth + 1, path_of_member)
            members[name] = value
            index = self._skip(index)
            if index < len(text) and text[index] == ",":
                index = self._skip(index + 1)
                continue
            if index < len(text) and text[index] == "}":
                return members, index + 1
            raise _SyntaxError

    def _array(self, index: int, depth: int, path: str) -> tuple[JsonValue, int]:
        """读数组的项。"""
        if depth > MAX_DEPTH:
            raise _SyntaxError
        text = self.text
        elements: list[JsonValue] = []
        index = self._skip(index)
        if index < len(text) and text[index] == "]":
            return elements, index + 1
        while True:
            value, index = self._value(index, depth + 1, self._path(path, f"[{len(elements)}]"))
            elements.append(value)
            index = self._skip(index)
            if index < len(text) and text[index] == ",":
                index = self._skip(index + 1)
                continue
            if index < len(text) and text[index] == "]":
                return elements, index + 1
            raise _SyntaxError

    def _string(self, index: int) -> tuple[str, int]:
        """读开头的引号之后的字符串：不允许原始的控制字符，转义只用 RFC 8259 的几种，代理项转义必须成对。"""
        text = self.text
        chunks: list[str] = []
        while True:
            match = _PLAIN.match(text, index)
            assert match is not None
            end = match.end()
            if end > index:
                chunks.append(text[index:end])
            if end >= len(text):
                raise _SyntaxError
            if text[end] == '"':
                return "".join(chunks), end + 1
            if text[end] != "\\" or end + 1 >= len(text):
                raise _SyntaxError
            escape = text[end + 1]
            if escape != "u":
                short = _SHORT_ESCAPES.get(escape)
                if short is None:
                    raise _SyntaxError
                chunks.append(short)
                index = end + 2
                continue
            unit = self._hex(end + 2)
            index = end + 6
            if 0xD800 <= unit <= 0xDBFF:
                if not text.startswith("\\u", index):
                    raise _SyntaxError
                low = self._hex(index + 2)
                if not 0xDC00 <= low <= 0xDFFF:
                    raise _SyntaxError
                unit = 0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)
                index += 6
            elif 0xDC00 <= unit <= 0xDFFF:
                raise _SyntaxError
            chunks.append(chr(unit))

    def _hex(self, index: int) -> int:
        """``\\u`` 之后的四位十六进制。"""
        match = _HEX4.match(self.text, index)
        if match is None:
            raise _SyntaxError
        return int(match.group(), 16)
