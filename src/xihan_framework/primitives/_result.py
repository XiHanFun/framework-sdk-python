"""成功或失败（RFC-0001 Result / Result<T>）。"""

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, ClassVar, Final, Never, final

from xihan_framework._json import JsonValue, dumps, parse
from xihan_framework.primitives._error import Error
from xihan_framework.primitives._error_code import ERROR_CODE_INVALID_ARGUMENT
from xihan_framework.primitives._framework import FrameworkError

if TYPE_CHECKING:
    from xihan_framework.primitives._maybe import Maybe

# 只有本模块的工厂持有它：Result 不能被直接构造，也就没有“未初始化”的结果
_FACTORY: Final = object()


@final
class Unit:
    """没有值的成功结果的值：只有一个实例 ``UNIT``。

    作为结果的值时不上线（无值成功写成 ``{"ok":true}``）；单独出现在别的值里（如操作的空响应）时编码为 ``{}``。
    """

    __slots__ = ()
    _instance: ClassVar["Unit | None"] = None

    def __new__(cls) -> "Unit":
        """返回唯一的实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        """``UNIT``。"""
        return "UNIT"

    def to_json_value(self) -> JsonValue:
        """线上编码：空对象。"""
        return {}

    @classmethod
    def from_json_value(cls, value: JsonValue) -> "Unit":
        """从线上编码还原：任意 JSON 对象都是 ``UNIT``，没有成员的类型按 RFC-0011 规则 2 忽略未知成员。"""
        if not isinstance(value, Mapping):
            raise ValueError("A unit on the wire must be a JSON object.")
        return UNIT


#: ``Unit`` 的唯一实例。
UNIT: Final = Unit()


@final
class Result[T]:
    """成功或失败（RFC-0001 Result / Result<T>）：成功带值，失败带 ``Error``，二者互斥。

    只能经 ``ok``、``fail``、``success`` 创建，成功值不能是 ``None``（没有值的成功用 ``success()``），所以
    ``value`` 为 ``None`` 当且仅当失败、``error`` 为 ``None`` 当且仅当成功。可恢复失败走 ``Result``，不抛异常。
    """

    __slots__ = ("_error", "_value")

    def __init__(self, factory: object, value: T | None, error: Error | None) -> None:
        """内部构造；外部请用 ``ok``、``fail`` 或 ``success``。"""
        if factory is not _FACTORY:
            raise TypeError("Create results with ok(), fail() or success().")
        self._value: Final = value
        self._error: Final = error

    @property
    def is_ok(self) -> bool:
        """是否成功。"""
        return self._error is None

    @property
    def is_failed(self) -> bool:
        """是否失败。"""
        return self._error is not None

    @property
    def value(self) -> T | None:
        """成功值；失败时为 ``None``。"""
        return self._value

    @property
    def error(self) -> Error | None:
        """失败原因；成功时为 ``None``。"""
        return self._error

    def value_or[U](self, fallback: U) -> T | U:
        """返回成功值，失败时返回备选值。"""
        return fallback if self._value is None else self._value

    def value_or_raise(self) -> T:
        """返回成功值；失败时抛出 ``FrameworkError``，码与消息取自失败原因。"""
        if self._error is not None:
            raise FrameworkError(self._error.code, self._error.message)
        assert self._value is not None
        return self._value

    def unit(self) -> "Result[Unit]":
        """丢掉值，只保留成功与否。"""
        return success() if self._error is None else fail(self._error)

    def map[U](self, transform: Callable[[T], U]) -> "Result[U]":
        """把成功值变换成另一种类型；失败原样传递。"""
        if self._error is not None:
            return fail(self._error)
        assert self._value is not None
        return ok(transform(self._value))

    def bind[U](self, next_step: Callable[[T], "Result[U]"]) -> "Result[U]":
        """把成功值交给返回 ``Result`` 的函数；失败原样传递。"""
        if self._error is not None:
            return fail(self._error)
        assert self._value is not None
        return next_step(self._value)

    def to_json_value(self, encode_value: Callable[[T], JsonValue] | None = None) -> JsonValue:
        """线上编码（RFC-0001「Result」）：``{"ok": true, "value": …}``、无值成功 ``{"ok": true}`` 或 ``{"ok": false, "error": …}``。

        值是 ``UNIT`` 的成功不写 ``value``；值是 ``Maybe`` 时无值写 ``null``。``encode_value`` 把成功值编码成 JSON 值，
        省略时按 SDK 的默认规则编码（dataclass、映射、序列与原语）。
        """
        if self._error is not None:
            return {"ok": False, "error": self._error.to_json_value()}
        if self._value is UNIT:
            return {"ok": True}
        if encode_value is None:
            from xihan_framework._codec import encode

            return {"ok": True, "value": encode(self._value)}
        assert self._value is not None
        return {"ok": True, "value": encode_value(self._value)}

    def to_json(self, encode_value: Callable[[T], JsonValue] | None = None) -> str:
        """线上编码的紧凑 JSON 文本。"""
        return dumps(self.to_json_value(encode_value))

    @staticmethod
    def from_json_value[U](value: JsonValue, decode_value: Callable[[JsonValue], U]) -> "Result[U]":
        """从有值结果的线上编码还原；不合 RFC-0001「Result」的规则抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。

        成功必须带 ``value`` 且不是 ``null``，交给 ``decode_value`` 解码；失败必须带 ``error``、不带 ``value``；
        成功不能带 ``error``；未知成员忽略。
        """
        members, error = _read(value)
        if error is not None:
            return fail(error)
        if "value" not in members:
            raise _wire_error("A successful result on the wire needs a 'value'.")
        if members["value"] is None:
            raise _wire_error("The value of a successful result on the wire must not be null.")
        return ok(decode_value(members["value"]))

    @staticmethod
    def unit_from_json_value(value: JsonValue) -> "Result[Unit]":
        """从无值结果的线上编码还原：成功只能是 ``{"ok":true}``，带了 ``value``（``{}`` 与 ``null`` 也算）即不合法。"""
        members, error = _read(value)
        if error is not None:
            return fail(error)
        if "value" in members:
            raise _wire_error("A successful result without a value must not carry a 'value'.")
        return success()

    @staticmethod
    def maybe_from_json_value[U](value: JsonValue, decode_value: Callable[[JsonValue], U]) -> "Result[Maybe[U]]":
        """从值类型为 ``Maybe`` 的结果的线上编码还原：成功必须带 ``value``，``null`` 即无值，其余交给 ``decode_value``。"""
        from xihan_framework.primitives._maybe import Maybe

        members, error = _read(value)
        if error is not None:
            return fail(error)
        if "value" not in members:
            raise _wire_error("A successful result on the wire needs a 'value'.")
        return ok(Maybe.from_json_value(members["value"], decode_value))

    @staticmethod
    def from_json[U](text: str | bytes, decode_value: Callable[[JsonValue], U]) -> "Result[U]":
        """从有值结果的 JSON 文本还原。"""
        return Result.from_json_value(_parse(text), decode_value)

    @staticmethod
    def unit_from_json(text: str | bytes) -> "Result[Unit]":
        """从无值结果的 JSON 文本还原。"""
        return Result.unit_from_json_value(_parse(text))

    @staticmethod
    def maybe_from_json[U](text: str | bytes, decode_value: Callable[[JsonValue], U]) -> "Result[Maybe[U]]":
        """从值类型为 ``Maybe`` 的结果的 JSON 文本还原。"""
        return Result.maybe_from_json_value(_parse(text), decode_value)

    def __repr__(self) -> str:
        """``Ok(值)`` 或 ``Fail(码: 消息)``，便于日志。"""
        if self._error is not None:
            return f"Fail({self._error})"
        return f"Ok({self._value!r})"

    def __eq__(self, other: object) -> bool:
        """成功与成功按值比较，失败与失败按错误比较。"""
        if not isinstance(other, Result):
            return NotImplemented
        return self._error == other._error and self._value == other._value

    __hash__ = None  # type: ignore[assignment]


def ok[T](value: T) -> Result[T]:
    """创建成功结果；值为 ``None`` 抛出 ``FrameworkError``——没有值的成功用 ``success()``。"""
    if value is None:
        raise FrameworkError(
            ERROR_CODE_INVALID_ARGUMENT, "A successful result needs a value; use success() for a result without one."
        )
    return Result(_FACTORY, value, None)


def fail(error: Error) -> Result[Never]:
    """创建失败结果；``error`` 不是 ``Error`` 抛出 ``FrameworkError``。"""
    if not isinstance(error, Error):
        raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, "A failed result needs an error.")
    return Result(_FACTORY, None, error)


def success() -> Result[Unit]:
    """创建没有值的成功结果，值是 ``UNIT``。"""
    return Result(_FACTORY, UNIT, None)


def _read(value: JsonValue) -> tuple[Mapping[str, JsonValue], Error | None]:
    """三种结果共用的读取：核对 ``ok``，成功不带 ``error``、失败带 ``error`` 不带 ``value``，失败时还原出错误。"""
    if not isinstance(value, Mapping):
        raise _wire_error("A result on the wire must be a JSON object.")
    succeeded = value.get("ok")
    if not isinstance(succeeded, bool):
        raise _wire_error("A result on the wire needs a boolean 'ok'.")
    if succeeded:
        if "error" in value:
            raise _wire_error("A successful result on the wire must not carry an 'error'.")
        return value, None
    if "value" in value:
        raise _wire_error("A failed result on the wire must not carry a 'value'.")
    if value.get("error") is None:
        raise _wire_error("A failed result on the wire needs an error.")
    return value, Error.from_json_value(value["error"])


def _parse(text: str | bytes) -> JsonValue:
    """解析结果的 JSON 文本。"""
    try:
        return parse(text)
    except ValueError as error:
        raise _wire_error("The result is not valid JSON.") from error


def _wire_error(message: str) -> FrameworkError:
    """线上编码不合法的配置错误（``PRIMITIVES_VALIDATION_001``）。"""
    return FrameworkError(ERROR_CODE_INVALID_ARGUMENT, message)
