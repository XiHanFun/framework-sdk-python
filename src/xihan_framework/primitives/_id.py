"""类型化标识（RFC-0001 TypedId）。"""

from collections.abc import Hashable

from xihan_framework._json import JsonValue
from xihan_framework.primitives._error_code import ERROR_CODE_INVALID_ARGUMENT
from xihan_framework.primitives._framework import FrameworkError


class TypedId[V: Hashable]:
    """类型化标识（RFC-0001 TypedId）：子类就是标签，只用于区分类型、不承载数据。

    用法是为每种标识派生一个子类，如 ``class OrderId(TypedId[str]): __slots__ = ()``。不同子类的同值标识不相等；
    线上编码为底层值的编码，标签不上线。
    """

    __slots__ = ("_value",)

    def __init__(self, value: V) -> None:
        """用底层值创建；值为 ``None`` 抛出 ``FrameworkError``。"""
        if value is None:
            raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, f"A {type(self).__name__} needs a value other than None.")
        self._value = value

    @property
    def value(self) -> V:
        """底层值（字符串、整数、UUID 字符串等）。"""
        return self._value

    def to_json_value(self) -> JsonValue:
        """线上编码：底层值的编码。"""
        from xihan_framework._codec import encode

        return encode(self._value)

    def __eq__(self, other: object) -> bool:
        """同一标识类型且底层值相等。"""
        if not isinstance(other, TypedId):
            return NotImplemented
        return type(self) is type(other) and bool(self._value == other._value)

    def __hash__(self) -> int:
        """按标识类型与底层值散列。"""
        return hash((type(self), self._value))

    def __repr__(self) -> str:
        """``类型名(值)``。"""
        return f"{type(self).__name__}({self._value!r})"

    def __str__(self) -> str:
        """底层值的文本。"""
        return str(self._value)
