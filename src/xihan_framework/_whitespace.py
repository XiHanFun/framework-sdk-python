"""规范所说的空白（RFC-0001「空白字符」，内部模块）：Error 的文案、握手行与回调端点共用这一张表。

集合是 25 个码位：U+0009–U+000D、U+0020、U+0085、U+00A0、U+1680、U+2000–U+200A、U+2028、U+2029、U+202F、U+205F、
U+3000，等于 Unicode 的 White_Space 属性与 .NET 正则的 ``\\s``。``str.isspace`` 另把 U+001C–U+001F 算作空白，``re`` 的
``\\s`` 也随之不同，所以按码位逐个列出，不用内建的判断（ADR-0237、ADR-0245）。
"""

from typing import Final

#: 规范所说的 25 个空白字符。
WHITESPACE: Final = frozenset(
    map(
        chr,
        (
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
        ),
    )
)


def is_whitespace(character: str) -> bool:
    """判断一个字符是不是规范所说的空白（RFC-0001「空白字符」，握手行与回调端点见 handshake.md §2）。

    集合是 U+0009–U+000D、U+0020、U+0085、U+00A0、U+1680、U+2000–U+200A、U+2028、U+2029、U+202F、U+205F、U+3000；
    U+001C–U+001F、U+200B 等其他字符不算，可以出现在地址里，也可以是 Error 的文案。
    """
    return character in WHITESPACE


def has_whitespace(text: str) -> bool:
    """文本里是否有 ``is_whitespace`` 所说的空白；SDK 自己生成的端点地址不能有（handshake.md §2）。"""
    return any(character in WHITESPACE for character in text)


def is_blank(text: str) -> bool:
    """文本是否为空或只有 ``is_whitespace`` 所说的空白；Error 的文案不能是这样（RFC-0001，ADR-0245）。"""
    return all(character in WHITESPACE for character in text)
