"""插件作者拿到的只读映射：绑定的属性与信封的扩展属性（lifecycle.md §7.2、RFC-0009）。

``types.MappingProxyType`` 也是只读的，但它不能 ``copy.deepcopy``、``pickle``，``dataclasses.asdict`` 也转不了它，带着它的
``Binding``、``Configuration`` 与 ``Envelope`` 因此都做不了这几件事。这里的映射是禁写的 ``dict`` 子类，原因见
``FrozenDict``。
"""

from collections.abc import Iterable, Mapping
from typing import Any, Final, NoReturn, Self

_READ_ONLY: Final = "The mapping is read-only."
"""写操作抛出的 ``TypeError`` 的文本。"""


def _rebuild[T: FrozenDict[Any, Any]](kind: type[T], items: Mapping[Any, Any]) -> T:
    """按给定的类型建一个只读映射：``freeze`` 与 ``pickle``、``copy`` 的还原都经过这里。"""
    instance = dict.__new__(kind)
    # 以类来调用得到的是普通 dict（见 FrozenDict.__new__），__init__ 又被禁了：只在这里往新建的实例里放一次内容
    dict.__init__(instance, items)
    return instance


class FrozenDict[K, V](dict[K, V]):
    """只读的 ``dict``：读、迭代、``len``、``in``、相等比较与 ``MappingProxyType`` 相同，另外可哈希、可拷贝、可 ``pickle``。

    选 ``dict`` 子类而不是只实现 ``collections.abc.Mapping``：``dataclasses.asdict`` 只认 ``dict``，遇到别的映射时整个
    ``copy.deepcopy``，得到的仍是这个只读类型，``json.dumps`` 也写不了它；遇到 ``dict`` 子类时按 ``type(obj)(键值对)``
    重建。所以以类来调用（``FrozenDict(...)``）得到的是普通 ``dict``，``asdict`` 的结果里就是可以照常改写、写成 JSON 的
    普通 ``dict``；与 ``dict(m)``、``m.copy()``、``m | other`` 一样，凡是由它另建一个映射的写法得到的都是普通 ``dict``。
    只读的实例只经 ``freeze`` / ``freeze_redacted`` 建出；``copy.copy``、``copy.deepcopy`` 与 ``pickle`` 经 ``__reduce__``
    还原成同一个类型、相等的新实例。

    写方法（``m[k] = v``、``del m[k]``、``update``、``pop``、``popitem``、``setdefault``、``clear``、``|=``，以及再调一次
    ``__init__``）一律抛出 ``TypeError``，与给 ``MappingProxyType`` 赋值时的异常类型相同。直接调 ``dict`` 的方法
    （``dict.__setitem__(m, k, v)``）绕得过去，那是有意的破坏，不在防范之内。哈希按内容，等于
    ``hash(frozenset(m.items()))``，值必须可哈希。
    """

    __slots__ = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> dict[K, V]:  # type: ignore[misc]
        """以类来调用时建普通 ``dict``：``dataclasses.asdict`` 重建 ``dict`` 子类时这样调用，结果就是普通 ``dict``。"""
        return dict(*args, **kwargs)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """禁写：再调一次 ``__init__`` 会改写内容。"""
        raise TypeError(_READ_ONLY)

    def __setitem__(self, key: K, value: V) -> NoReturn:
        """禁写。"""
        raise TypeError(_READ_ONLY)

    def __delitem__(self, key: K) -> NoReturn:
        """禁写。"""
        raise TypeError(_READ_ONLY)

    def __ior__(self, other: Any) -> NoReturn:  # type: ignore[misc]
        """禁写：``|=`` 会原地合并；``|`` 照常得到普通 ``dict``。"""
        raise TypeError(_READ_ONLY)

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        """禁写。"""
        raise TypeError(_READ_ONLY)

    def pop(self, *args: Any) -> NoReturn:
        """禁写。"""
        raise TypeError(_READ_ONLY)

    def popitem(self) -> NoReturn:
        """禁写。"""
        raise TypeError(_READ_ONLY)

    def setdefault(self, *args: Any) -> NoReturn:
        """禁写。"""
        raise TypeError(_READ_ONLY)

    def clear(self) -> NoReturn:
        """禁写。"""
        raise TypeError(_READ_ONLY)

    def __hash__(self) -> int:  # type: ignore[override]
        """按内容哈希：相等的映射哈希相同，与 ``hash(frozenset(self.items()))`` 一致。"""
        return hash(frozenset(self.items()))

    def __reduce__(self) -> tuple[Any, ...]:
        """``pickle`` 与 ``copy`` 的还原方式：同一个类型、同样的内容；深拷贝时内容由 ``copy`` 逐项深拷贝。"""
        return (_rebuild, (type(self), dict(self)))

    def __copy__(self) -> Self:
        """浅拷贝：同一个类型、相等的新实例。"""
        return _rebuild(type(self), self)


class RedactedDict[K, V](FrozenDict[K, V]):
    """值可能是凭据的只读映射（绑定的属性，lifecycle.md §7.2）：``repr`` 与 ``str`` 只带键，值写成 ``...``。

    ``print``、f 字符串、日志的 ``%s`` / ``%r`` 与 ``pprint`` 都经过 ``repr``；按 ``dict`` 读它的写法（``json.dumps``、
    ``dict(m)``、逐项取值，以及 ``asdict`` 的结果）照样拿到值，那是作者自己取用。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """调试输出：按升序排列的键，值一律写成 ``...``，形如 ``{'password': ..., 'user': ...}``。"""
        return "{" + ", ".join(f"{name!r}: ..." for name in sorted(self, key=str)) + "}"


def freeze[K, V](items: Mapping[K, V] | Iterable[tuple[K, V]]) -> Mapping[K, V]:
    """把映射或键值对拷贝成只读映射（``FrozenDict``）；已经是 ``FrozenDict`` 的原样返回，它不可变。"""
    if type(items) is FrozenDict:
        return items
    return _rebuild(FrozenDict, dict(items))


def freeze_redacted[K, V](items: Mapping[K, V] | Iterable[tuple[K, V]]) -> Mapping[K, V]:
    """把映射或键值对拷贝成调试输出不带值的只读映射（``RedactedDict``）；已经是 ``RedactedDict`` 的原样返回。"""
    if type(items) is RedactedDict:
        return items
    return _rebuild(RedactedDict, dict(items))
