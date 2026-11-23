"""消息原语（RFC-0001、RFC-0010）：消息键文法、模板格式化、未解析时的描述与进程默认的解析器。"""

import math
import numbers
import re
import threading
from collections.abc import Iterator, Mapping, Sequence, Set
from decimal import ROUND_HALF_EVEN, Context, Decimal
from types import MappingProxyType
from typing import Final, NamedTuple, Protocol

from xihan_framework._json import INT64_MAX, INT64_MIN, MAX_DEPTH, int_to_float, number_text, quote
from xihan_framework.primitives._error_code import ERROR_CODE_INVALID_ARGUMENT
from xihan_framework.primitives._framework import FrameworkError

#: 默认区域，也是消息目录的回退区域。
DEFAULT_LOCALE: Final = "en-US"
#: 消息键的最大长度（RFC-0010「消息键」）。
MAX_MESSAGE_KEY_LENGTH: Final = 128
#: 区域名的最大长度（RFC-0010「区域」，.NET 的 ``LocaleName.MaxLength``）。
MAX_LOCALE_NAME_LENGTH: Final = 35

# 消息键文法 <domain>.<segment>[.<segment>]，首段与后段同形（RFC-0001，2026-10-28 修订后首段可含下划线）
_MESSAGE_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
# 区域名文法（RFC-0010「区域」）：2–3 个字母的语言子标签，之后是 0 到多个 2–8 位字母数字的子标签，以 - 连接
_LOCALE_NAME_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
# 占位符名：字母开头的字母、数字与下划线（按名字），或纯数字（按位置）（RFC-0010「模板」）
_PLACEHOLDER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[0-9]+")
# 可移植的格式说明符：D、X / x、F、N 加至多两位精度，整串匹配（RFC-0010「模板」，ADR-0236）
_PORTABLE_SPECIFIER = re.compile(r"([DdXxFfNn])([0-9]{0,2})")
# 区域的小数点与分组符（RFC-0010「模板」的固定表，ADR-0236）；表外的区域与不给区域一样按不变文化
_INVARIANT_SYMBOLS: Final = (".", ",")
_LOCALE_SYMBOLS: Final = {
    "de-de": (",", "."),
    "es-es": (",", "."),
    "pt-br": (",", "."),
    "fr-fr": (",", chr(0x202F)),
    "ru-ru": (",", chr(0xA0)),
}
_ASCII_LOWER: Final = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


class _Placeholder(NamedTuple):
    """模板里的一个占位符；``raw`` 是它在模板里的原文，缺值时原样输出。"""

    name: str
    specifier: str | None
    raw: str


def is_message_key(key: str) -> bool:
    """判断消息键是否符合 ``^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$`` 且不超过 128 个字符，按整串匹配。"""
    return len(key) <= MAX_MESSAGE_KEY_LENGTH and _MESSAGE_KEY_PATTERN.fullmatch(key) is not None


def message_key_domain(key: str) -> str | None:
    """返回消息键的领域段（第一个点之前）；不合法时为 ``None``。"""
    return key.split(".", 1)[0] if is_message_key(key) else None


def is_locale_name(locale: str) -> bool:
    """判断区域名是否符合 ``^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$`` 且不超过 35 个字符，按整串匹配（RFC-0010「区域」）。"""
    return len(locale) <= MAX_LOCALE_NAME_LENGTH and _LOCALE_NAME_PATTERN.fullmatch(locale) is not None


def validate_locale_name(locale: str) -> str:
    """校验区域名并返回它的规范形式；不合法抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。

    规范形式（RFC-0010「区域」，与 .NET 的 ``LocaleName.Normalize`` 相同）：语言子标签小写，两位的地区子标签大写，四位的
    文字子标签首字母大写，其余子标签小写，如 ``zh-cn`` → ``zh-CN``、``ZH-hant-tw`` → ``zh-Hant-TW``。区域名的比较不区分
    大小写：两个区域名相同，当且仅当规范形式相同。
    """
    if not is_locale_name(locale):
        raise FrameworkError(
            ERROR_CODE_INVALID_ARGUMENT, f"The locale '{locale}' must be a BCP 47 name such as '{DEFAULT_LOCALE}'."
        )
    return _canonical_locale(locale)


def placeholders(template: str) -> list[str]:
    """列出模板里的占位符名，按出现顺序去重；转义的花括号与字面量里的花括号不算。"""
    names: list[str] = []
    for segment in _segments(template):
        if isinstance(segment, _Placeholder) and segment.name not in names:
            names.append(segment.name)
    return names


def format_message(template: str, arguments: Mapping[str, object] | None, *, locale: str | None = None) -> str:
    """宽松格式化（RFC-0010「模板」）：缺值的占位符连同格式说明符原样留在文本里，让遗漏可见。

    ``{name}`` 取命名参数，``{0}`` 取位置参数（键 ``"0"``、``"1"``…，见 ``positional``）；``{{`` 与 ``}}`` 是转义的
    单个花括号；花括号里不是合法名字的与没有闭合的 ``{`` 之后的文本原样输出；参数值里的花括号不再解释。值按
    ``format_argument`` 的规则写出：只认可移植的说明符 ``D``、``X`` / ``x``、``F``、``N``，``locale`` 只改变小数点与分组符。
    参数存在而值为 ``None`` 不算缺值，写空串。
    """
    rendered = _render(template, arguments, locale, strict=False)
    assert rendered is not None
    return rendered


def try_format_message(
    template: str, arguments: Mapping[str, object] | None, *, locale: str | None = None
) -> str | None:
    """严格格式化：任一占位符缺值即为 ``None``，供解析器拒绝半截译文、退回 ``describe``；其余规则同 ``format_message``。"""
    return _render(template, arguments, locale, strict=True)


def positional(*values: object) -> dict[str, object]:
    """把位置参数转成以 ``"0"``、``"1"``… 为键的参数表。"""
    return {str(index): value for index, value in enumerate(values)}


def describe(key: str, arguments: Mapping[str, object] | None) -> str:
    """没有文案时描述一个消息：``键 (k=v, ...)``，值按 ``format_argument`` 的无格式文本（不随区域变化）；没有参数时就是键。

    参数按名字的序数顺序排列，逐个 UTF-16 码元比较（RFC-0010，ADR-0235）：与 .NET 的 ``string.CompareOrdinal`` 相同，
    基本多文种平面以外的字符排在 U+E000–U+FFFF 之前，与按码点比较不同。
    """
    if not arguments:
        return key
    parts = ", ".join(f"{name}={format_argument(arguments[name])}" for name in sorted(arguments, key=_utf16_order))
    return f"{key} ({parts})"


def format_argument(value: object, specifier: str | None = None, *, locale: str | None = None) -> str:
    """一个参数值的文本（RFC-0010「模板」，ADR-0236）。

    值先归到值模型：``None``、布尔、字符串、64 位整数、双精度浮点、数组与对象。超出 64 位的整数、``Decimal`` 与其他实数
    是浮点；``IntEnum`` 是整数，``StrEnum`` 是字符串；键为字符串的映射是对象，其余序列与集合是数组；模型之外的值
    （``datetime``、普通 ``Enum``、``UUID`` 等）取 ``str(value)``。

    - ``None`` 写空串，布尔写 ``true`` / ``false``，字符串原样，数字见 ``_json.number_text``。
    - 数组与对象写成紧凑的 JSON：对象成员按名字的 UTF-16 码元序排列（同名保持原序），嵌套的 ``None`` 写 ``null``，
      数字写法同顶层，超过 64 层的容器写 ``null``；说明符与区域都不影响它们。
    - ``specifier`` 只认 ``^[DdFfNnXx][0-9]{0,2}$``：``D`` 至少 n 位的整数，``X`` / ``x`` 非负整数的大写 / 小写十六进制，
      ``F`` 定点小数、``N`` 带千分位的定点小数（n 位小数，缺省 2，正好一半时取偶）；``D`` 与 ``X`` 只用于整数，``F`` 与
      ``N`` 用于整数与浮点，不认或不适用时按无格式写出。
    - ``locale`` 只改变小数点与分组符（de-DE、es-ES、pt-BR 是 ``,`` 与 ``.``，fr-FR 是 ``,`` 与 U+202F，ru-RU 是 ``,``
      与 U+00A0，区域名不区分大小写，其余按 ``.`` 与 ``,``）：小数点用于无格式的浮点与 ``F``、``N``，分组符只用于 ``N``。
    """
    model = _model(value)
    if model is None:
        return ""
    if isinstance(model, bool):
        return "true" if model else "false"
    if isinstance(model, str):
        return model
    if isinstance(model, (int, float)):
        text = _format_number(model, specifier)
        decimal, group = _symbols(locale)
        if (decimal, group) == _INVARIANT_SYMBOLS:
            return text
        return text.translate({ord("."): decimal, ord(","): group})
    parts: list[str] = []
    _write_json(model, parts, 1)
    return "".join(parts)


def _render(template: str, arguments: Mapping[str, object] | None, locale: str | None, *, strict: bool) -> str | None:
    """逐段渲染模板；严格模式下任一占位符缺值返回 ``None``。"""
    parts: list[str] = []
    for segment in _segments(template):
        if isinstance(segment, str):
            parts.append(segment)
        elif arguments is not None and segment.name in arguments:
            parts.append(format_argument(arguments[segment.name], segment.specifier, locale=locale))
        elif strict:
            return None
        else:
            # 缺值：把占位符连同格式说明符原样留下，让遗漏可见而不是悄悄吞掉
            parts.append(segment.raw)
    return "".join(parts)


def _segments(template: str) -> Iterator[str | _Placeholder]:
    """把模板切成字面量与占位符，规则按 RFC-0010「模板」，由 message-templates 向量核对。

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


def _model(value: object) -> object:
    """把值归到值模型；容器原样交回（写 JSON 时逐项再归），模型之外的值换成 ``str(value)``。

    ``bool`` 是 ``int`` 的子类，所以先判断；``str`` 与数字的子类（``StrEnum``、``IntEnum``）取它们的基本值。
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        number = int(value)
        return number if INT64_MIN <= number <= INT64_MAX else int_to_float(number)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, Decimal):
        return math.nan if value.is_nan() else float(value)
    if isinstance(value, numbers.Integral):
        return _model(int(value))
    if isinstance(value, numbers.Real):
        return float(value)
    if isinstance(value, (Mapping, Sequence, Set)) and not isinstance(value, (bytes, bytearray, memoryview)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return list(bytes(value))
    return str(value)


def _format_number(value: int | float, specifier: str | None) -> str:
    """数字的不变文化文本：认得且适用的说明符按它，否则无格式。"""
    match = None if specifier is None else _PORTABLE_SPECIFIER.fullmatch(specifier)
    if match is None:
        return number_text(value)
    letter, digits = match.group(1), match.group(2)
    precision = int(digits) if digits else None
    if isinstance(value, int):
        if letter in "Dd":
            return ("-" if value < 0 else "") + str(abs(value)).zfill(precision or 0)
        if letter in "Xx" and value >= 0:
            return format(value, letter).zfill(precision or 0)
    if letter in "FfNn" and (isinstance(value, int) or math.isfinite(value)):
        return _fixed(value, 2 if precision is None else precision, grouping=letter in "Nn")
    return number_text(value)


def _fixed(value: int | float, digits: int, *, grouping: bool) -> str:
    """定点小数：按值的精确十进制展开舍入，正好一半时取偶（RFC-0010「模板」）；``grouping`` 时整数部分以 ``,`` 分组。

    用局部的 Decimal 上下文：不受进程里改过的全局舍入方式影响，精度足够容纳整数部分，quantize 不会溢出。
    """
    exact = Decimal(value)
    context = Context(prec=max(28, exact.adjusted() + digits + 2), rounding=ROUND_HALF_EVEN)
    rounded = exact.quantize(Decimal(1).scaleb(-digits), context=context)
    return format(rounded, ",f" if grouping else "f")


def _symbols(locale: str | None) -> tuple[str, str]:
    """区域的小数点与分组符；区域名按 ASCII 不区分大小写。"""
    if locale is None:
        return _INVARIANT_SYMBOLS
    return _LOCALE_SYMBOLS.get(locale.translate(_ASCII_LOWER), _INVARIANT_SYMBOLS)


def _canonical_locale(locale: str) -> str:
    """合法区域名的规范大小写；文法只有 ASCII 字母与数字，``lower`` / ``upper`` 不会碰到别的字符。"""
    language, *rest = locale.split("-")
    parts = [language.lower()]
    for part in rest:
        if len(part) == 2:
            # 两位是地区子标签
            parts.append(part.upper())
        elif len(part) == 4:
            # 四位是文字子标签（Hant、Latn）
            parts.append(part[0].upper() + part[1:].lower())
        else:
            # 三位数字的地区（419）没有大小写，其余子标签一律小写
            parts.append(part.lower())
    return "-".join(parts)


def _write_json(value: object, parts: list[str], depth: int) -> None:
    """把模型里的值写成紧凑的 JSON 文本；``depth`` 是这里的容器所在的层，超过 64 层写 ``null``。"""
    model = _model(value)
    if model is None:
        parts.append("null")
    elif isinstance(model, bool):
        parts.append("true" if model else "false")
    elif isinstance(model, str):
        parts.append(quote(model))
    elif isinstance(model, (int, float)):
        parts.append(number_text(model))
    elif depth > MAX_DEPTH:
        parts.append("null")
    elif isinstance(model, Mapping):
        members = sorted(
            ((format_argument(name), member) for name, member in model.items()),
            key=lambda pair: _utf16_order(pair[0]),
        )
        parts.append("{")
        for index, (name, member) in enumerate(members):
            if index:
                parts.append(",")
            parts.append(quote(name))
            parts.append(":")
            _write_json(member, parts, depth + 1)
        parts.append("}")
    else:
        assert isinstance(model, (Sequence, Set))
        parts.append("[")
        for index, element in enumerate(model):
            if index:
                parts.append(",")
            _write_json(element, parts, depth + 1)
        parts.append("]")


def _utf16_order(name: str) -> bytes:
    """按 UTF-16 码元比较的排序键：UTF-16BE 的字节序就是码元的序；孤立代理项原样计为一个码元。"""
    return name.encode("utf-16-be", "surrogatepass")


class MessageResolver(Protocol):
    """按区域把消息键解析成已格式化的文案（RFC-0010 IMessageResolver）。"""

    def resolve(self, key: str, locale: str, arguments: Mapping[str, object] | None) -> str | None:
        """返回文案；没有该键、该区域或占位符缺值时为 ``None``。"""
        ...


class Catalog:
    """一份内存消息目录：区域 → 键 → 模板。

    实现 ``MessageResolver``：请求的区域没有该键时回退到 ``en-US``。构造时复制内容并校验区域名与键的文法，区域名按规范
    形式保存、查找时不区分大小写（RFC-0010「区域」），之后不可变。
    """

    __slots__ = ("_locales",)

    def __init__(self, locales: Mapping[str, Mapping[str, str]]) -> None:
        """用区域到模板表的映射创建。

        区域名或键不合文法、两个区域名只差大小写（是同一个区域）时抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。
        """
        copied: dict[str, Mapping[str, str]] = {}
        for name, templates in locales.items():
            locale = validate_locale_name(name)
            if locale in copied:
                raise FrameworkError(
                    ERROR_CODE_INVALID_ARGUMENT, f"The catalog lists the locale '{locale}' more than once."
                )
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
        """区域（规范形式）到模板表的只读映射。"""
        return self._locales

    def resolve(self, key: str, locale: str, arguments: Mapping[str, object] | None) -> str | None:
        """解析消息键：先找请求的区域，没有再找 ``en-US``；按请求的区域严格格式化，占位符缺值时为 ``None``（RFC-0010「解析」）。

        请求的区域名不区分大小写。它多半来自外部输入（Baggage ``xihan.locale``），不合文法时不报错，当作目录里没有这个
        区域、退到 ``en-US``（RFC-0010「错误」）。
        """
        # 目录里的键都是规范形式：原样命中就是它，没命中再把请求的区域名归一后找一次
        templates = self._locales.get(locale)
        if templates is None and is_locale_name(locale):
            templates = self._locales.get(_canonical_locale(locale))
        template = None if templates is None else templates.get(key)
        if template is None:
            template = self._locales.get(DEFAULT_LOCALE, {}).get(key)
        return None if template is None else try_format_message(template, arguments, locale=locale)


# 进程默认的解析器与区域（RFC-0001 MessageScope.Default）：读取远多于设置，存成一个元组整体替换，
# 读取方一次取到一致的一对、不必加锁；设置方之间用锁串行，保证“保留原区域”读到的是最新值
_defaults_lock = threading.Lock()
_defaults: tuple[MessageResolver | None, str] = (None, DEFAULT_LOCALE)


def set_default_messages(resolver: MessageResolver | None, locale: str | None = None) -> None:
    """设置进程默认的消息解析器与区域；``locale`` 为 ``None`` 时保留原区域。

    ``Error.message`` 在没有已解析文案时用它解析消息键。区域名不合文法是编程错误，抛出 ``FrameworkError``
    （``PRIMITIVES_VALIDATION_001``），解析器与区域都不变；合法的按规范形式保存（``zh-cn`` 存成 ``zh-CN``）。
    """
    global _defaults
    # 先校验再进锁：不合法的区域名什么都不改
    chosen = None if locale is None else validate_locale_name(locale)
    with _defaults_lock:
        _defaults = (resolver, _defaults[1] if chosen is None else chosen)


def default_locale() -> str:
    """返回进程默认区域。"""
    return _defaults[1]


def resolve_message(key: str, arguments: Mapping[str, object] | None) -> str | None:
    """用进程默认解析器按默认区域解析消息键；没有解析器或没有该键时为 ``None``。"""
    resolver, locale = _defaults
    return None if resolver is None else resolver.resolve(key, locale, arguments)
