"""消息原语（RFC-0001、RFC-0010）：消息键文法、模板格式化、未解析时的描述与进程默认的解析器。"""

import math
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal
from types import MappingProxyType
from typing import Final, NamedTuple, Protocol

from xihan_framework._json import exponent_notation, fixed_notation, shortest_digits
from xihan_framework.primitives._error_code import ERROR_CODE_INVALID_ARGUMENT
from xihan_framework.primitives._framework import FrameworkError

#: 默认区域，也是消息目录的回退区域。
DEFAULT_LOCALE: Final = "en-US"
#: 消息键的最大长度（RFC-0010「消息键」，与 .NET 的 ``MessageTemplate.MaxKeyLength`` 相同）。
MAX_MESSAGE_KEY_LENGTH: Final = 128

# 消息键文法 <domain>.<segment>[.<segment>]，首段与后段同形（RFC-0001，2026-10-28 修订后首段可含下划线）
_MESSAGE_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
# 占位符名：字母开头的字母、数字与下划线（按名字），或纯数字（按位置）（RFC-0010「模板」）
_PLACEHOLDER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[0-9]+")
# 可移植的格式说明符：D、X / x、F、N，可带精度；字母大小写与 .NET 一样不区分（X / x 决定十六进制的大小写）
_PORTABLE_SPECIFIER = re.compile(r"([DdXxFfNn])([0-9]{0,9})")
# 浮点在这个范围内写最短往返的定点形式（RFC-0010「值的文本形式」）
_PLAIN_FLOAT_MIN: Final = 1e-4
_PLAIN_FLOAT_MAX: Final = 1e17


class _Placeholder(NamedTuple):
    """模板里的一个占位符；``raw`` 是它在模板里的原文，缺值时原样输出。"""

    name: str
    specifier: str | None
    raw: str


def is_message_key(key: str) -> bool:
    """判断消息键是否符合 ``^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$`` 且不超过 128 个字符。"""
    return len(key) <= MAX_MESSAGE_KEY_LENGTH and _MESSAGE_KEY_PATTERN.fullmatch(key) is not None


def message_key_domain(key: str) -> str | None:
    """返回消息键的领域段（第一个点之前）；不合法时为 ``None``。"""
    return key.split(".", 1)[0] if is_message_key(key) else None


def placeholders(template: str) -> list[str]:
    """列出模板里的占位符名，按出现顺序去重；转义的花括号与字面量里的花括号不算。"""
    names: list[str] = []
    for segment in _segments(template):
        if isinstance(segment, _Placeholder) and segment.name not in names:
            names.append(segment.name)
    return names


def format_message(template: str, arguments: Mapping[str, object] | None) -> str:
    """宽松格式化（RFC-0010「模板」）：缺值的占位符连同格式说明符原样留在文本里，让遗漏可见。

    ``{name}`` 取命名参数，``{0}`` 取位置参数（键 ``"0"``、``"1"``…，见 ``positional``）；``{{`` 与 ``}}`` 是转义的
    单个花括号；花括号里不是合法名字的与没有闭合的 ``{`` 之后的文本原样输出；参数值里的花括号不再解释。占位符可带
    可移植的格式说明符 ``D``、``X`` / ``x``、``F``、``N``（按不变文化，``F`` 与 ``N`` 正好一半时取偶），其他说明符
    按无格式输出。参数存在而值为 ``None`` 不算缺值，写空串。
    """
    rendered = _render(template, arguments, strict=False)
    assert rendered is not None
    return rendered


def try_format_message(template: str, arguments: Mapping[str, object] | None) -> str | None:
    """严格格式化：任一占位符缺值即为 ``None``，供解析器拒绝半截译文、退回 ``describe``；其余规则同 ``format_message``。"""
    return _render(template, arguments, strict=True)


def positional(*values: object) -> dict[str, object]:
    """把位置参数转成以 ``"0"``、``"1"``… 为键的参数表。"""
    return {str(index): value for index, value in enumerate(values)}


def describe(key: str, arguments: Mapping[str, object] | None) -> str:
    """没有文案时描述一个消息：``键 (k=v, ...)``，值按 ``format_argument`` 的文本形式；没有参数时就是键。

    参数按名字的序数顺序排列，逐个 UTF-16 码元比较（RFC-0010，ADR-0235）：与 .NET 的 ``string.CompareOrdinal`` 相同，
    基本多文种平面以外的字符排在 U+E000–U+FFFF 之前，与按码点比较不同。
    """
    if not arguments:
        return key
    parts = ", ".join(f"{name}={format_argument(arguments[name])}" for name in sorted(arguments, key=_utf16_order))
    return f"{key} ({parts})"


def format_argument(value: object) -> str:
    """一个参数的文本形式（RFC-0010「值的文本形式」），无格式说明符时与 ``describe`` 用它。

    字符串原样，整数十进制，布尔写 ``true`` / ``false``，``None`` 写空串；浮点在 1e-4 ≤ |x| < 1e17 或为 0 时写最短
    往返的定点形式，整数值不带小数点（``2.0`` 写 ``2``，1e16 写 ``10000000000000000``），范围外写最短往返的有效数字
    加 ``E``、符号与至少两位的指数（``1E+17``、``9.9E-05``、``5E-324``）；负零写 ``-0``，非数与无穷写 ``NaN``、
    ``Infinity``、``-Infinity``。数组与对象规范不作规定：数组写 ``[a b]``，对象按键排序写 ``map[k:v]``，其他值用 ``str``。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _float_text(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        members = " ".join(f"{name}:{format_argument(value[name])}" for name in sorted(value, key=str))
        return f"map[{members}]"
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "[" + " ".join(format_argument(element) for element in value) + "]"
    return str(value)


def _render(template: str, arguments: Mapping[str, object] | None, *, strict: bool) -> str | None:
    """逐段渲染模板；严格模式下任一占位符缺值返回 ``None``。"""
    parts: list[str] = []
    for segment in _segments(template):
        if isinstance(segment, str):
            parts.append(segment)
        elif arguments is not None and segment.name in arguments:
            parts.append(_format_value(arguments[segment.name], segment.specifier))
        elif strict:
            return None
        else:
            # 缺值：把占位符连同格式说明符原样留下，让遗漏可见而不是悄悄吞掉
            parts.append(segment.raw)
    return "".join(parts)


def _segments(template: str) -> Iterator[str | _Placeholder]:
    """把模板切成字面量与占位符，规则与 .NET 的 ``MessageTemplate`` 逐字相同。

    每个位置先看 ``{{`` / ``}}`` 转义；``{`` 之后找第一个 ``}``，找不到时其余文本都是字面量；花括号之间第一个 ``:``
    之前是名字，名字不合法时整段（含两个花括号）是字面量，不再在里面找占位符。
    """
    index, literal_start, length = 0, 0, len(template)
    while index < length:
        character = template[index]
        if character in "{}" and index + 1 < length and template[index + 1] == character:
            yield template[literal_start:index] + character
            index += 2
            literal_start = index
            continue
        if character != "{":
            index += 1
            continue
        end = template.find("}", index + 1)
        if end < 0:
            break
        body = template[index + 1 : end]
        name, separator, specifier = body.partition(":")
        if _PLACEHOLDER_NAME.fullmatch(name) is None:
            index = end + 1
            continue
        if literal_start < index:
            yield template[literal_start:index]
        yield _Placeholder(name, specifier if separator else None, template[index : end + 1])
        index = end + 1
        literal_start = index
    if literal_start < length:
        yield template[literal_start:]


def _format_value(value: object, specifier: str | None) -> str:
    """按格式说明符格式化一个值；不是可移植的说明符、或说明符不适用于这个值时按无格式输出。"""
    if specifier is not None and (match := _PORTABLE_SPECIFIER.fullmatch(specifier)) is not None:
        kind, digits = match.group(1), match.group(2)
        is_integer = isinstance(value, int) and not isinstance(value, bool)
        is_number = is_integer or (isinstance(value, float) and math.isfinite(value))
        if kind in "Dd" and is_integer:
            assert isinstance(value, int)
            return ("-" if value < 0 else "") + str(abs(value)).zfill(int(digits or "0"))
        if kind in "Xx" and is_integer:
            assert isinstance(value, int)
            if value >= 0:
                return format(value, kind).zfill(int(digits or "0"))
        if kind in "FfNn" and is_number:
            assert isinstance(value, (int, float))
            return _fixed(value, int(digits) if digits else 2, grouping=kind in "Nn")
    return format_argument(value)


def _fixed(value: int | float, digits: int, *, grouping: bool) -> str:
    """定点小数：按值的精确十进制展开舍入，正好一半时取偶（与 .NET 一致）；``grouping`` 时整数部分以 ``,`` 分组。

    用局部的 Decimal 上下文：不受进程里改过的全局舍入方式影响，精度足够容纳整数部分，quantize 不会溢出。
    """
    exact = Decimal(value)
    context = Context(prec=max(28, exact.adjusted() + digits + 2), rounding=ROUND_HALF_EVEN)
    rounded = exact.quantize(Decimal(1).scaleb(-digits), context=context)
    return format(rounded, ",f" if grouping else "f")


def _utf16_order(name: str) -> bytes:
    """按 UTF-16 码元比较的排序键：UTF-16BE 的字节序就是码元的序；孤立代理项原样计为一个码元。"""
    return name.encode("utf-16-be", "surrogatepass")


def _float_text(value: float) -> str:
    """浮点的文本形式：范围内写最短往返的定点形式（Python 的 ``repr`` 到 1e16 就换成指数，所以自己写），范围外写 .NET 的指数形式。"""
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
    return sign + exponent_notation(digits, point).upper()


class MessageResolver(Protocol):
    """按区域把消息键解析成已格式化的文案（RFC-0010 IMessageResolver）。"""

    def resolve(self, key: str, locale: str, arguments: Mapping[str, object] | None) -> str | None:
        """返回文案；没有该键、该区域或占位符缺值时为 ``None``。"""
        ...


class Catalog:
    """一份内存消息目录：区域 → 键 → 模板。

    实现 ``MessageResolver``：请求的区域没有该键时回退到 ``en-US``。构造时复制内容并校验键的文法，之后不可变。
    """

    __slots__ = ("_locales",)

    def __init__(self, locales: Mapping[str, Mapping[str, str]]) -> None:
        """用区域到模板表的映射创建；键不合文法抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。"""
        copied: dict[str, Mapping[str, str]] = {}
        for locale, templates in locales.items():
            for key in templates:
                if not is_message_key(key):
                    raise FrameworkError(
                        ERROR_CODE_INVALID_ARGUMENT,
                        f"The message key '{key}' of the locale '{locale}' must match <domain>.<segment>[.<segment>].",
                    )
            copied[locale] = MappingProxyType(dict(templates))
        self._locales: Mapping[str, Mapping[str, str]] = MappingProxyType(copied)

    @property
    def locales(self) -> Mapping[str, Mapping[str, str]]:
        """区域到模板表的只读映射。"""
        return self._locales

    def resolve(self, key: str, locale: str, arguments: Mapping[str, object] | None) -> str | None:
        """解析消息键：先找请求的区域，没有再找 ``en-US``；按严格格式化，占位符缺值时为 ``None``（RFC-0010「解析」）。"""
        template = self._locales.get(locale, {}).get(key)
        if template is None and locale != DEFAULT_LOCALE:
            template = self._locales.get(DEFAULT_LOCALE, {}).get(key)
        return None if template is None else try_format_message(template, arguments)


# 进程默认的解析器与区域（RFC-0001 MessageScope.Default）：读取远多于设置，存成一个元组整体替换，
# 读取方一次取到一致的一对、不必加锁；设置方之间用锁串行，保证“保留原区域”读到的是最新值
_defaults_lock = threading.Lock()
_defaults: tuple[MessageResolver | None, str] = (None, DEFAULT_LOCALE)


def set_default_messages(resolver: MessageResolver | None, locale: str | None = None) -> None:
    """设置进程默认的消息解析器与区域；``locale`` 为 ``None`` 时保留原区域。

    ``Error.message`` 在没有已解析文案时用它解析消息键。
    """
    global _defaults
    with _defaults_lock:
        _defaults = (resolver, _defaults[1] if locale is None else locale)


def default_locale() -> str:
    """返回进程默认区域。"""
    return _defaults[1]


def resolve_message(key: str, arguments: Mapping[str, object] | None) -> str | None:
    """用进程默认解析器按默认区域解析消息键；没有解析器或没有该键时为 ``None``。"""
    resolver, locale = _defaults
    return None if resolver is None else resolver.resolve(key, locale, arguments)
