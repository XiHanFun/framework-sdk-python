"""配置节的类型化读取（``spec/plugin/lifecycle.md`` §7.2）。

宿主的配置源是键值字符串（.NET 的 ``IConfiguration``），写出的配置节里叶子一律是 JSON 字符串、成员名的大小写随配置源
（lifecycle.md §2）。类型化读取先按目标类型把配置节改写成 SDK 解码认的形状，再交给操作载荷的同一套解码：成员名按 ASCII
不区分大小写对上 dataclass 字段的 JSON 名；目标是 ``int`` / ``float`` 的字符串叶子合 JSON 数字文法时读成数字，目标是
``bool`` 的字符串叶子是 ``true`` / ``false``（ASCII 不区分大小写）时读成布尔；其余照解码的常规读法。
"""

import dataclasses
import re
import string
import types
from collections.abc import Mapping, MutableSequence, Sequence
from typing import Annotated, Any, Final, TypeAliasType, Union, cast, get_args, get_origin, get_type_hints

from xihan_framework._codec import JSON_NAME, DecodeError, compile_decoder
from xihan_framework._json import JsonValue, parse
from xihan_framework.primitives import ERROR_CODE_INVALID_ARGUMENT, FrameworkError
from xihan_framework.primitives._maybe import Maybe

# JSON 数字的文法（RFC 8259）；[0-9] 是字面的码点范围，全角数字不算
_NUMBER: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
# 只把 ASCII 大写字母换成小写：str.lower 会把开尔文符号 U+212A 之类也变成 ASCII 字母，那不是 IConfiguration 的比较
_ASCII_LOWER: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def decode_section[T](section: Mapping[str, JsonValue], target: type[T]) -> T:
    """按 ``target`` 读配置节；读不出时抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``），说明里带出错的 JSON 路径。

    ``target`` 是解码支持的类型（通常是 dataclass），不支持时抛出 ``TypeError``（编程错误）。
    """
    decode = compile_decoder(target)
    try:
        return cast(T, decode(_coerce(dict(section), target)))
    except DecodeError as error:
        name = getattr(target, "__qualname__", str(target))
        raise FrameworkError(
            ERROR_CODE_INVALID_ARGUMENT,
            f"The configuration section cannot be read as '{name}': {error.path}: {error.detail}.",
        ) from error


def _coerce(value: JsonValue, target: object) -> JsonValue:
    """按目标类型改写一个 JSON 值：dataclass 的成员名换成字段的 JSON 名，字符串叶子按目标类型换成数字或布尔。

    认不出该怎样改写的值原样留下，交给解码报它自己的错误。
    """
    if isinstance(target, TypeAliasType):
        return value if target is JsonValue else _coerce(value, target.__value__)
    origin, arguments = get_origin(target), get_args(target)
    if origin is Annotated:
        return _coerce(value, arguments[0])
    if origin is Union or origin is types.UnionType:
        # X | None 按 X 改写；其余联合类型看不出该读成哪一种，原样交给解码
        members = [argument for argument in arguments if argument is not type(None)]
        return _coerce(value, members[0]) if len(members) == 1 and value is not None else value
    if origin is Maybe:
        return _coerce(value, arguments[0]) if arguments else value
    if isinstance(value, list):
        return _coerce_array(value, origin, arguments)
    if isinstance(value, dict):
        if origin in (dict, Mapping) and len(arguments) == 2:
            return {name: _coerce(member, arguments[1]) for name, member in value.items()}
        if isinstance(target, type) and dataclasses.is_dataclass(target):
            return _coerce_members(value, target)
        return value
    if isinstance(value, str):
        return _coerce_leaf(value, target)
    return value


def _coerce_array(value: list[JsonValue], origin: object, arguments: tuple[Any, ...]) -> JsonValue:
    """数组按元素类型逐个改写；配置节里从 ``0`` 起连续编号的子项写成数组（lifecycle.md §2）。"""
    if origin in (list, Sequence, MutableSequence):
        element = arguments[0] if arguments else Any
        return [_coerce(item, element) for item in value]
    if origin is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return [_coerce(item, arguments[0]) for item in value]
        if len(arguments) == len(value):
            return [_coerce(item, argument) for item, argument in zip(value, arguments, strict=True)]
    return value


def _coerce_members(value: dict[str, JsonValue], target: type) -> JsonValue:
    """对象的成员名按 ASCII 不区分大小写对上 dataclass 字段的 JSON 名，成员值按字段类型改写；对不上的成员原样留下（解码忽略）。"""
    hints = get_type_hints(target, include_extras=True)
    fields: dict[str, tuple[str, object]] = {}
    for field in dataclasses.fields(target):
        if not field.init or field.name.startswith("_"):
            continue
        name = str(field.metadata.get(JSON_NAME, field.name))
        fields[name.translate(_ASCII_LOWER)] = (name, hints[field.name])
    coerced: dict[str, JsonValue] = {}
    for name, member in value.items():
        spec = fields.get(name.translate(_ASCII_LOWER))
        if spec is None:
            coerced[name] = member
        else:
            coerced[spec[0]] = _coerce(member, spec[1])
    return coerced


def _coerce_leaf(value: str, target: object) -> JsonValue:
    """字符串叶子：目标是 ``bool`` 时只认 ``true`` / ``false``（ASCII 不区分大小写），目标是数字时按 JSON 数字的文法读。"""
    if target is bool:
        lowered = value.translate(_ASCII_LOWER)
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return value
    if (target is int or target is float) and _NUMBER.fullmatch(value) is not None:
        # 合 JSON 数字文法的文本按值模型读：整数写法是 int，带小数点或指数的是 float（目标是 int 时解码报类型不符）
        return parse(value)
    return value
