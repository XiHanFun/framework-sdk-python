"""值与 JSON 值之间的编解码（内部模块）：标准库实现，规则对齐 Go SDK 的 ``encoding/json``。

编码按值的运行期类型：原语原样；有 ``to_json_value()`` 的对象（SDK 的原语与用户类型）用它；枚举取 ``value``；
dataclass 按字段声明顺序写成对象（名字可由 ``json_field`` 改写，``_`` 开头的字段不写）；映射按键的字典序；
列表与元组写成数组。

解码按目标类型先编译成解码函数（注册时就发现不支持的类型），载荷不符时抛出 ``DecodeError``，只带 JSON 路径
（``$.quantity``）而不带类型名（RFC-0011 规则 2）。支持 ``bool`` / ``int`` / ``float`` / ``str`` / ``None``、
``X | None``、``Literal``、``Annotated``、``list`` / ``Sequence`` / ``tuple``、键为 ``str`` 的 ``dict`` / ``Mapping``、
dataclass、枚举、``TypedId`` 子类、``Maybe``、``Result``、``Unit``、``Error``、``JsonValue`` / ``Any``，以及定义了
``from_json_value(value)`` 类方法的类型。dataclass 的未知成员忽略；没有缺省值的字段是必填的，``Maybe`` 字段缺省为无值。
"""

import dataclasses
import types
from collections.abc import Callable, Mapping, MutableSequence, Sequence
from enum import Enum
from typing import Annotated, Any, Final, Literal, TypeAliasType, Union, get_args, get_origin, get_type_hints

from xihan_framework._json import JsonValue
from xihan_framework.primitives._id import TypedId
from xihan_framework.primitives._maybe import Maybe
from xihan_framework.primitives._result import Result, Unit

#: dataclass 字段元数据里的 JSON 名。
JSON_NAME: Final = "xihan.json.name"
#: dataclass 字段元数据里的“为 None 时省略”。
JSON_OMIT_NONE: Final = "xihan.json.omit_none"

type Decoder = Callable[[JsonValue, str], object]

# 成员名含这些字符时路径写成 ['名字']；U+0085、U+2028、U+2029 用 chr 写出，源码里不放不可见字符
_BRACKETED_CHARACTERS: Final = frozenset(". '/\"[]()\\\t\n\r\f\b") | {chr(0x85), chr(0x2028), chr(0x2029)}


class DecodeError(Exception):
    """载荷与目标类型不符：``path`` 是出错的 JSON 路径，``detail`` 是面向开发者的说明（不上线）。"""

    def __init__(self, path: str, detail: str) -> None:
        """用路径与说明创建。"""
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


def json_field(name: str | None = None, *, omit_none: bool = False) -> dict[str, object]:
    """dataclass 字段在 JSON 里的名字与省略规则，作为 ``dataclasses.field(metadata=...)`` 的值。

    ``name`` 改写成员名（如 ``ttl_ms`` 写成 ``ttlMs``）；``omit_none`` 为真时值为 ``None`` 不写出，相当于 Go 的 ``omitempty``。
    """
    metadata: dict[str, object] = {}
    if name is not None:
        if not name:
            raise ValueError("A JSON member name must not be empty.")
        metadata[JSON_NAME] = name
    if omit_none:
        metadata[JSON_OMIT_NONE] = True
    return metadata


def encode(value: object) -> JsonValue:
    """把值编码成 JSON 值；编码不了的值抛出 ``TypeError``。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    hook = getattr(type(value), "to_json_value", None)
    if callable(hook):
        encoded: JsonValue = hook(value)
        return encoded
    if isinstance(value, Enum):
        return encode(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        members: dict[str, JsonValue] = {}
        for field in dataclasses.fields(value):
            if field.name.startswith("_"):
                continue
            member = getattr(value, field.name)
            if member is None and field.metadata.get(JSON_OMIT_NONE):
                continue
            members[str(field.metadata.get(JSON_NAME, field.name))] = encode(member)
        return members
    if isinstance(value, Mapping):
        # 映射的键按字典序写出，与 Go 编码 map 一致；dataclass 与 SDK 原语保持声明顺序
        items: list[tuple[object, object]] = list(value.items())
        for key, _ in items:
            if not isinstance(key, str):
                raise TypeError(f"The JSON object key {key!r} is not a string.")
        return {str(key): encode(member) for key, member in sorted(items, key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [encode(element) for element in value]
    raise TypeError(f"A value of type '{type(value).__name__}' cannot be encoded as JSON.")


def compile_decoder(target: object) -> Callable[[JsonValue], object]:
    """为目标类型编译解码函数；不支持的类型抛出 ``TypeError``，返回的函数在载荷不符时抛出 ``DecodeError``。"""
    decoder = _compile(target, {})

    def decode(value: JsonValue) -> object:
        return decoder(value, "$")

    return decode


def _compile(target: object, cache: dict[object, Decoder]) -> Decoder:
    """编译一个类型；``cache`` 让自引用的 dataclass 与类型别名指向同一个解码函数。"""
    if target in cache:
        return cache[target]
    if target is Any or target is object:
        return _identity
    if isinstance(target, TypeAliasType):
        if target is JsonValue:
            return _identity
        return _deferred(target, cache, lambda: _compile(target.__value__, cache))
    if target is None or target is type(None):
        return _null
    origin, arguments = get_origin(target), get_args(target)
    if origin is Annotated:
        return _compile(arguments[0], cache)
    if origin is Union or origin is types.UnionType:
        return _compile_optional(target, arguments, cache)
    if origin is Literal:
        return _compile_literal(arguments)
    if origin in (list, Sequence, MutableSequence):
        return _compile_list(arguments[0] if arguments else Any, cache)
    if origin is tuple:
        return _compile_tuple(arguments, cache)
    if origin in (dict, Mapping):
        return _compile_mapping(target, arguments, cache)
    if origin is Maybe:
        return _compile_maybe(arguments[0], cache)
    if origin is Result:
        return _compile_result(arguments[0], cache)
    if isinstance(target, type):
        return _compile_class(target, cache)
    raise TypeError(f"The type '{target}' cannot be decoded from JSON.")


def _compile_class(target: type, cache: dict[object, Decoder]) -> Decoder:
    """编译一个普通类。"""
    if target is bool:
        return _boolean
    if target is int:
        return _integer
    if target is float:
        return _number
    if target is str:
        return _string
    if target is list:
        return _compile_list(Any, cache)
    if target is tuple:
        return _compile_tuple((), cache)
    if target is dict:
        return _compile_mapping(target, (), cache)
    if target is Maybe:
        return _compile_maybe(Any, cache)
    if target is Result:
        return _compile_result(Any, cache)
    if issubclass(target, TypedId):
        return _compile_typed_id(target, cache)
    # SDK 的原语（Error、Unit、三个枚举）与用户类型都可以自带 from_json_value
    hook = getattr(target, "from_json_value", None)
    if callable(hook):
        return _compile_hook(hook)
    if issubclass(target, Enum):
        return _compile_enum(target)
    if dataclasses.is_dataclass(target):
        return _deferred(target, cache, lambda: _compile_dataclass(target, cache))
    raise TypeError(f"The type '{target.__qualname__}' cannot be decoded from JSON.")


def _deferred(target: object, cache: dict[object, Decoder], build: Callable[[], Decoder]) -> Decoder:
    """先登记一个转发函数再编译，自引用的类型编译到自己时拿到的是它。"""
    cell: list[Decoder] = []

    def forward(value: JsonValue, path: str) -> object:
        return cell[0](value, path)

    cache[target] = forward
    decoder = build()
    cell.append(decoder)
    cache[target] = decoder
    return decoder


def _compile_optional(target: object, arguments: tuple[object, ...], cache: dict[object, Decoder]) -> Decoder:
    """``X | None``：只支持一个非空分支，其他联合类型没有确定的解码方式。"""
    present = [argument for argument in arguments if argument is not type(None)]
    if len(present) != 1 or len(present) == len(arguments):
        raise TypeError(f"The union type '{target}' cannot be decoded from JSON; only 'X | None' is supported.")
    inner = _compile(present[0], cache)

    def decode(value: JsonValue, path: str) -> object:
        return None if value is None else inner(value, path)

    return decode


def _compile_literal(arguments: tuple[object, ...]) -> Decoder:
    """``Literal[...]``：值与类型都要与某个字面量相同（``True`` 不等于 ``1``）。"""

    def decode(value: JsonValue, path: str) -> object:
        for allowed in arguments:
            if type(value) is type(allowed) and value == allowed:
                return value
        raise DecodeError(path, "the value is not one of the allowed literals")

    return decode


def _compile_list(element: object, cache: dict[object, Decoder]) -> Decoder:
    """数组解码成 ``list``。"""
    inner = _compile(element, cache)

    def decode(value: JsonValue, path: str) -> object:
        if not isinstance(value, list):
            raise DecodeError(path, "expected an array")
        return [inner(member, f"{path}[{index}]") for index, member in enumerate(value)]

    return decode


def _compile_tuple(arguments: tuple[object, ...], cache: dict[object, Decoder]) -> Decoder:
    """数组解码成 ``tuple``：``tuple[X, ...]`` 任意长度，``tuple[X, Y]`` 长度固定。"""
    if not arguments or (len(arguments) == 2 and arguments[1] is Ellipsis):
        inner = _compile(arguments[0] if arguments else Any, cache)

        def decode_variadic(value: JsonValue, path: str) -> object:
            if not isinstance(value, list):
                raise DecodeError(path, "expected an array")
            return tuple(inner(member, f"{path}[{index}]") for index, member in enumerate(value))

        return decode_variadic
    decoders = [_compile(argument, cache) for argument in arguments]

    def decode_fixed(value: JsonValue, path: str) -> object:
        if not isinstance(value, list) or len(value) != len(decoders):
            raise DecodeError(path, f"expected an array of {len(decoders)} elements")
        return tuple(
            decoder(member, f"{path}[{index}]")
            for index, (decoder, member) in enumerate(zip(decoders, value, strict=True))
        )

    return decode_fixed


def _compile_mapping(target: object, arguments: tuple[object, ...], cache: dict[object, Decoder]) -> Decoder:
    """对象解码成 ``dict``；键必须是 ``str``。"""
    if arguments and arguments[0] is not str:
        raise TypeError(f"The mapping type '{target}' cannot be decoded from JSON; the keys must be 'str'.")
    inner = _compile(arguments[1] if arguments else Any, cache)

    def decode(value: JsonValue, path: str) -> object:
        if not isinstance(value, Mapping):
            raise DecodeError(path, "expected an object")
        return {name: inner(member, _member_path(path, name)) for name, member in value.items()}

    return decode


def _compile_maybe(element: object, cache: dict[object, Decoder]) -> Decoder:
    """``Maybe[X]``：``null`` 为无值。"""
    inner = _compile(element, cache)

    def decode(value: JsonValue, path: str) -> object:
        return Maybe.none() if value is None else Maybe.from_optional(inner(value, path))

    return decode


def _compile_result(element: object, cache: dict[object, Decoder]) -> Decoder:
    """``Result[X]``：线上编码 ``{"ok": …}``，按值类型分三种读法（RFC-0001「Result」）。

    ``Result[Unit]`` 是无值结果，成功不能带 ``value``；``Result[Maybe[X]]`` 的 ``value`` 可以是 ``null``；其余的
    ``value`` 必填且不能是 ``null``。
    """
    if element is Unit:

        def decode_unit(value: JsonValue, path: str) -> object:
            try:
                return Result.unit_from_json_value(value)
            except ValueError as error:
                raise DecodeError(path, str(error)) from error

        return decode_unit
    if element is Maybe or get_origin(element) is Maybe:
        arguments = get_args(element)
        maybe_inner = _compile(arguments[0] if arguments else Any, cache)

        def decode_maybe(value: JsonValue, path: str) -> object:
            try:
                return Result.maybe_from_json_value(value, lambda member: maybe_inner(member, f"{path}.value"))
            except ValueError as error:
                raise DecodeError(path, str(error)) from error

        return decode_maybe
    inner = _compile(element, cache)

    def decode(value: JsonValue, path: str) -> object:
        try:
            return Result.from_json_value(value, lambda member: inner(member, f"{path}.value"))
        except ValueError as error:
            raise DecodeError(path, str(error)) from error

    return decode


def _compile_typed_id(target: type[TypedId[Any]], cache: dict[object, Decoder]) -> Decoder:
    """``TypedId`` 子类：底层值的编码。"""
    inner = _compile(_typed_id_value_type(target), cache)

    def decode(value: JsonValue, path: str) -> object:
        try:
            return target(inner(value, path))
        except (TypeError, ValueError) as error:
            raise DecodeError(path, str(error)) from error

    return decode


def _typed_id_value_type(target: type) -> object:
    """从 ``class OrderId(TypedId[str])`` 里取出底层值类型 ``str``。"""
    for klass in target.__mro__:
        for base in types.get_original_bases(klass):
            if get_origin(base) is TypedId:
                value_type = get_args(base)[0]
                if not isinstance(value_type, type):
                    break
                return value_type
    raise TypeError(f"The typed ID '{target.__qualname__}' must derive from TypedId[V] with a concrete V.")


def _compile_hook(hook: Callable[[JsonValue], object]) -> Decoder:
    """类型自己的 ``from_json_value``：它抛出的 ``ValueError`` / ``TypeError`` 算载荷不符。"""

    def decode(value: JsonValue, path: str) -> object:
        try:
            return hook(value)
        except (TypeError, ValueError) as error:
            raise DecodeError(path, str(error)) from error

    return decode


def _compile_enum(target: type[Enum]) -> Decoder:
    """普通枚举按 ``value`` 解码，类型也要相同（``True`` 不当作 ``1``）。"""
    members = list(target)

    def decode(value: JsonValue, path: str) -> object:
        for member in members:
            if type(member.value) is type(value) and member.value == value:
                return member
        raise DecodeError(path, f"the value is not a member of '{target.__qualname__}'")

    return decode


def _compile_dataclass(target: type, cache: dict[object, Decoder]) -> Decoder:
    """dataclass：按字段（或 ``json_field`` 改写的名字）取成员，未知成员忽略。"""
    hints = get_type_hints(target, include_extras=True)
    specs: list[tuple[str, str, Decoder, bool, bool]] = []
    for field in dataclasses.fields(target):
        if not field.init or field.name.startswith("_"):
            continue
        hint = hints[field.name]
        required = field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING
        is_maybe = hint is Maybe or get_origin(hint) is Maybe
        specs.append(
            (field.name, str(field.metadata.get(JSON_NAME, field.name)), _compile(hint, cache), required, is_maybe)
        )

    def decode(value: JsonValue, path: str) -> object:
        if not isinstance(value, Mapping):
            raise DecodeError(path, "expected an object")
        arguments: dict[str, object] = {}
        for name, member_name, decoder, required, is_maybe in specs:
            member_path = _member_path(path, member_name)
            if member_name in value:
                arguments[name] = decoder(value[member_name], member_path)
            elif is_maybe and required:
                arguments[name] = Maybe.none()
            elif required:
                raise DecodeError(member_path, "the property is required")
        try:
            return target(**arguments)
        except (TypeError, ValueError) as error:
            raise DecodeError(path, str(error)) from error

    return decode


def _member_path(path: str, name: str) -> str:
    """对象成员的 JSON 路径（RFC-0011 规则 2，照 System.Text.Json 的实测）。

    名字含 ``.``、空格、``'``、``/``、``"``、``[``、``]``、``(``、``)``、``\\``、制表、换行、回车、换页、退格、U+0085、
    U+2028、U+2029 之一时写成 ``$['名字']``，名字原样、不转义；其余写成 ``$.名字``，空名字写成 ``$.``。
    """
    if any(character in _BRACKETED_CHARACTERS for character in name):
        return f"{path}['{name}']"
    return f"{path}.{name}"


def _identity(value: JsonValue, path: str) -> object:
    """原样交出 JSON 值。"""
    return value


def _null(value: JsonValue, path: str) -> object:
    """只接受 ``null``。"""
    if value is not None:
        raise DecodeError(path, "expected null")
    return None


def _boolean(value: JsonValue, path: str) -> object:
    """只接受布尔。"""
    if not isinstance(value, bool):
        raise DecodeError(path, "expected a boolean")
    return value


def _integer(value: JsonValue, path: str) -> object:
    """只接受整数；``1.0`` 与布尔都不算，与 Go 解码进 int 一致。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise DecodeError(path, "expected an integer")
    return value


def _number(value: JsonValue, path: str) -> object:
    """接受整数与浮点数，解码成 ``float``；布尔不算。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DecodeError(path, "expected a number")
    return float(value)


def _string(value: JsonValue, path: str) -> object:
    """只接受字符串。"""
    if not isinstance(value, str):
        raise DecodeError(path, "expected a string")
    return value
