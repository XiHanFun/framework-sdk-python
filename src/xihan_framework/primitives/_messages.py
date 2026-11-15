"""消息原语（RFC-0001、RFC-0010）：消息键文法、模板格式化、未解析时的描述与进程默认的解析器。"""

import math
import re
import threading
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, Protocol

from xihan_framework._json import exponent_notation, fixed_notation, shortest_digits
from xihan_framework.primitives._error_code import ERROR_CODE_INVALID_ARGUMENT
from xihan_framework.primitives._framework import FrameworkError

#: 默认区域，也是消息目录的回退区域。
DEFAULT_LOCALE: Final = "en-US"

# 消息键文法 <domain>.<segment>[.<segment>]，首段与后段同形（RFC-0001，2026-10-28 修订后首段可含下划线）
_MESSAGE_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
# 模板里的一段：转义的 {{ 与 }}，或占位符 {name} / {0} / {name:format}
_TEMPLATE_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*|[0-9]+)(?::([^{}]*))?\}")


def is_message_key(key: str) -> bool:
    """判断消息键是否符合 ``^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$``。"""
    return _MESSAGE_KEY_PATTERN.fullmatch(key) is not None


def message_key_domain(key: str) -> str | None:
    """返回消息键的领域段（第一个点之前）；不合法时为 ``None``。"""
    return key.split(".", 1)[0] if is_message_key(key) else None


def placeholders(template: str) -> list[str]:
    """列出模板里的占位符名，按出现顺序去重；转义的花括号不算。"""
    names: list[str] = []
    for match in _TEMPLATE_TOKEN.finditer(template):
        name = match.group(1)
        if name is not None and name not in names:
            names.append(name)
    return names


def format_message(template: str, arguments: Mapping[str, object] | None) -> str:
    """用参数填充模板。

    ``{name}`` 取命名参数，``{0}`` 取位置参数（键 ``"0"``、``"1"``…，见 ``positional``）；``{{`` 与 ``}}`` 是转义的
    单个花括号（RFC-0010）；缺参数的占位符原样保留。占位符可带 ``:format``：以 ``%`` 开头的按 printf 风格解释
    （如 ``{count:%05d}``，常用写法与 Go 的 fmt 动词一致），其他写法按值的默认文本输出。
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group()
        if token in ("{{", "}}"):
            return token[0]
        name, spec = match.group(1), match.group(2)
        if arguments is None or name not in arguments:
            return token
        value = arguments[name]
        if spec and spec.startswith("%"):
            try:
                return spec % (value,)
            except (TypeError, ValueError):
                # 动词与值的类型不符是目录的缺陷；渲染消息不能失败，按默认文本输出，与非 % 写法一样
                return format_argument(value)
        return format_argument(value)

    return _TEMPLATE_TOKEN.sub(replace, template)


def positional(*values: object) -> dict[str, object]:
    """把位置参数转成以 ``"0"``、``"1"``… 为键的参数表。"""
    return {str(index): value for index, value in enumerate(values)}


def describe(key: str, arguments: Mapping[str, object] | None) -> str:
    """没有文案时描述一个消息：``键 (k=v, ...)``，参数按键排序；没有参数时就是键。"""
    if not arguments:
        return key
    parts = ", ".join(f"{name}={format_argument(arguments[name])}" for name in sorted(arguments))
    return f"{key} ({parts})"


def format_argument(value: object) -> str:
    """把一个参数写成文本，与 Go SDK 的 ``fmt.Sprint`` 逐字一致。

    布尔写 ``true`` / ``false``，``None`` 写 ``<nil>``，浮点数取最短表示（指数小于 -4 或不小于 6 时用
    ``1e+06`` 形式），数组写 ``[a b]``，对象按键排序写 ``map[k:v]``，其他值用 ``str``。
    """
    if value is None:
        return "<nil>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        members = " ".join(f"{name}:{format_argument(value[name])}" for name in sorted(value, key=str))
        return f"map[{members}]"
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "[" + " ".join(format_argument(element) for element in value) + "]"
    return str(value)


def _format_float(value: float) -> str:
    """Go ``%v`` 的浮点格式：最短往返的数字，指数小于 -4 或不小于 6 时用指数形式。"""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    digits, point = shortest_digits(value)
    sign = "-" if value < 0 else ""
    exponent = point - 1
    if exponent < -4 or exponent >= 6:
        return sign + exponent_notation(digits, point)
    return sign + fixed_notation(digits, point)


class MessageResolver(Protocol):
    """按区域把消息键解析成已格式化的文案（RFC-0010 IMessageResolver）。"""

    def resolve(self, key: str, locale: str, arguments: Mapping[str, object] | None) -> str | None:
        """返回文案；没有该键或该区域时为 ``None``。"""
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
        """解析消息键：先找请求的区域，没有再找 ``en-US``。"""
        template = self._locales.get(locale, {}).get(key)
        if template is None and locale != DEFAULT_LOCALE:
            template = self._locales.get(DEFAULT_LOCALE, {}).get(key)
        return None if template is None else format_message(template, arguments)


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
