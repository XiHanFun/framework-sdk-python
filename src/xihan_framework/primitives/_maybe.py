"""可能没有的值（RFC-0001 Maybe<T>）。"""

from collections.abc import Callable
from typing import Final, Never, final

from xihan_framework._json import JsonValue
from xihan_framework.primitives._error import Error
from xihan_framework.primitives._error_code import ERROR_CODE_INVALID_ARGUMENT
from xihan_framework.primitives._framework import FrameworkError
from xihan_framework.primitives._result import Result, fail, ok

# 只有本模块的工厂持有它：Maybe 只能经 some / none / from_optional 创建
_FACTORY: Final = object()


@final
class Maybe[T]:
    """可能没有的值（RFC-0001 Maybe<T>）：与 ``None`` 的编码等价，但类型层面不可为空。

    有值时值不能是 ``None``，所以 ``value`` 为 ``None`` 当且仅当无值。线上编码：有值为值本身，无值为 ``null``。
    """

    __slots__ = ("_value",)

    def __init__(self, factory: object, value: T | None) -> None:
        """内部构造；外部请用 ``Maybe.some``、``Maybe.none`` 或 ``Maybe.from_optional``。"""
        if factory is not _FACTORY:
            raise TypeError("Create maybes with Maybe.some(), Maybe.none() or Maybe.from_optional().")
        self._value: Final = value

    @staticmethod
    def some[U](value: U) -> "Maybe[U]":
        """创建有值的 Maybe；值为 ``None`` 抛出 ``FrameworkError``。"""
        if value is None:
            raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, "A maybe with a value needs a value other than None.")
        return Maybe(_FACTORY, value)

    @staticmethod
    def none() -> "Maybe[Never]":
        """创建无值的 Maybe。"""
        return _NONE

    @staticmethod
    def from_optional[U](value: U | None) -> "Maybe[U]":
        """把可能为 ``None`` 的值转成 Maybe：``None`` 为无值。"""
        return _NONE if value is None else Maybe(_FACTORY, value)

    @property
    def has_value(self) -> bool:
        """是否有值。"""
        return self._value is not None

    @property
    def value(self) -> T | None:
        """值；无值时为 ``None``。"""
        return self._value

    def value_or[U](self, fallback: U) -> T | U:
        """返回值，无值时返回备选值。"""
        return fallback if self._value is None else self._value

    def value_or_raise(self) -> T:
        """返回值；无值时抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。"""
        if self._value is None:
            raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, "The maybe has no value.")
        return self._value

    def to_result(self, error: Error) -> Result[T]:
        """把无值转成给定错误的失败。"""
        return fail(error) if self._value is None else ok(self._value)

    def map[U](self, transform: Callable[[T], U]) -> "Maybe[U]":
        """变换有值的 Maybe；无值原样传递，变换结果为 ``None`` 时也是无值。"""
        return _NONE if self._value is None else Maybe.from_optional(transform(self._value))

    def bind[U](self, next_step: Callable[[T], "Maybe[U]"]) -> "Maybe[U]":
        """把值交给返回 Maybe 的函数；无值原样传递。"""
        return _NONE if self._value is None else next_step(self._value)

    def to_json_value(self, encode_value: Callable[[T], JsonValue] | None = None) -> JsonValue:
        """线上编码：有值为值本身，无值为 ``null``；``encode_value`` 省略时按 SDK 的默认规则编码。"""
        if self._value is None:
            return None
        if encode_value is None:
            from xihan_framework._codec import encode

            return encode(self._value)
        return encode_value(self._value)

    @staticmethod
    def from_json_value[U](value: JsonValue, decode_value: Callable[[JsonValue], U]) -> "Maybe[U]":
        """从线上编码还原：``null`` 为无值，其余交给 ``decode_value``。"""
        return _NONE if value is None else Maybe.from_optional(decode_value(value))

    def __repr__(self) -> str:
        """``Maybe.some(值)`` 或 ``Maybe.none()``。"""
        return "Maybe.none()" if self._value is None else f"Maybe.some({self._value!r})"

    def __eq__(self, other: object) -> bool:
        """有值与有值按值比较，无值与无值相等。"""
        if not isinstance(other, Maybe):
            return NotImplemented
        return bool(self._value == other._value)

    def __hash__(self) -> int:
        """按值散列。"""
        return hash((Maybe, self._value))


_NONE: Final[Maybe[Never]] = Maybe(_FACTORY, None)
