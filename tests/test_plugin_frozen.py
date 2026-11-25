"""插件作者拿到的只读映射（绑定的属性、信封的扩展属性）：读法同 MappingProxyType，另外可哈希、可拷贝、可 pickle，
``dataclasses.asdict`` 得到普通 dict，绑定属性的调试输出不带值（lifecycle.md §7.2）。"""

import copy
import dataclasses
import json
import logging
import pickle
import pprint
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, cast

import pytest

from xihan_framework.plugin import Binding, Configuration, Envelope
from xihan_framework.plugin._frozen import FrozenDict, RedactedDict, freeze, freeze_redacted

# 测试用的主值与属性值：够特别，在输出里搜得出来
VALUE = "postgres://app:hunter2@db.internal/app"
PASSWORD = "s3cret-9d1c"
USERNAME = "app-user-7f3a"
SECRETS = (VALUE, PASSWORD, USERNAME)

PROPERTIES = {"username": USERNAME, "password": PASSWORD, "Password": PASSWORD, "empty": ""}
"""带凭据的属性：只差大小写的两项、值为空的一项。"""

FREEZERS: tuple[Callable[[Mapping[str, str]], Mapping[str, str]], ...] = (freeze, freeze_redacted)
"""两种只读映射的建法。"""


def secret_binding() -> Binding:
    """带主值与几个属性的绑定。"""
    return Binding("db", "connection:relational", VALUE, PROPERTIES)


def tenant_envelope() -> Envelope:
    """带两个扩展属性的信封。"""
    return Envelope(
        topic="orders.created",
        id="e-1",
        source="/orders",
        type="orders.created",
        data=b'{"id":1}',
        extensions={"xihantenant": "t1", "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
    )


def full_configuration() -> Configuration:
    """带两个绑定（一个有属性、一个没有）与嵌套配置节的配置。"""
    return Configuration(
        section={"pool": "4", "retry": {"delays": ["1", "2"]}},
        granted_permissions=("binding:db", "binding:cache"),
        bindings=(secret_binding(), Binding("cache", "cache:distributed", "redis.internal:6379")),
        work_directory="/work",
    )


@pytest.mark.parametrize("make", FREEZERS)
def test_reading_is_the_same_as_a_mapping_proxy(make: Callable[[Mapping[str, str]], Mapping[str, str]]) -> None:
    source = dict(PROPERTIES)
    proxy = MappingProxyType(dict(source))
    frozen = make(source)
    assert isinstance(frozen, Mapping)
    assert frozen["username"] == proxy["username"] == USERNAME
    assert frozen.get("missing") is None
    assert frozen.get("missing", "x") == "x"
    with pytest.raises(KeyError):
        frozen["missing"]
    assert list(frozen) == list(proxy)
    assert list(frozen.keys()) == list(proxy.keys())
    assert list(frozen.values()) == list(proxy.values())
    assert list(frozen.items()) == list(proxy.items())
    assert len(frozen) == len(proxy) == 4
    assert "Password" in frozen
    assert "PASSWORD" not in frozen
    assert frozen == source
    assert frozen == proxy
    assert proxy == frozen
    assert frozen != {**source, "empty": "x"}
    assert make({}) == {}
    # 由它另建的映射是普通 dict，与 MappingProxyType 相同
    as_dict = cast("dict[str, str]", frozen)
    for derived in (as_dict | {"extra": "x"}, {"extra": "x"} | as_dict, as_dict.copy(), dict(frozen)):
        assert type(derived) is dict
    # 拷贝：之后改动来源不影响它
    source["username"] = "changed"
    del source["empty"]
    assert frozen["username"] == USERNAME
    assert "empty" in frozen


@pytest.mark.parametrize("make", FREEZERS)
def test_every_write_raises_type_error(make: Callable[[Mapping[str, str]], Mapping[str, str]]) -> None:
    frozen = make(PROPERTIES)
    writable = cast("dict[str, str]", frozen)
    writes: dict[str, Callable[[], object]] = {
        "item assignment": lambda: writable.__setitem__("username", "x"),
        "item deletion": lambda: writable.__delitem__("username"),
        "update": lambda: writable.update({"username": "x"}),
        "update with keywords": lambda: writable.update(username="x"),
        "pop": lambda: writable.pop("username"),
        "pop with a default": lambda: writable.pop("missing", None),
        "popitem": writable.popitem,
        "setdefault": lambda: writable.setdefault("added", "x"),
        "clear": writable.clear,
        "in-place union": lambda: writable.__ior__({"username": "x"}),
        "a second __init__": lambda: writable.__init__({"username": "x"}),  # type: ignore[misc]
    }
    for name, write in writes.items():
        with pytest.raises(TypeError):
            write()
        assert frozen == PROPERTIES, f"{name} must not change the mapping"
    with pytest.raises(TypeError):
        writable["username"] = "x"
    with pytest.raises(TypeError):
        del writable["username"]
    with pytest.raises(TypeError):
        writable |= {"username": "x"}
    # 没有 __dict__：不能另挂属性
    with pytest.raises(AttributeError):
        frozen.extra = 1  # type: ignore[attr-defined]
    assert frozen == PROPERTIES


@pytest.mark.parametrize("make", FREEZERS)
def test_the_hash_follows_the_content(make: Callable[[Mapping[str, str]], Mapping[str, str]]) -> None:
    frozen = make(PROPERTIES)
    assert hash(frozen) == hash(frozenset(PROPERTIES.items()))
    # 相等的映射哈希相同，与键的先后无关
    assert hash(frozen) == hash(make(dict(reversed(PROPERTIES.items()))))
    assert len({frozen, make(dict(PROPERTIES)), make({"username": USERNAME})}) == 2
    assert hash(make({})) == hash(frozenset())


@pytest.mark.parametrize("make", FREEZERS)
def test_copies_and_pickles_keep_the_type(make: Callable[[Mapping[str, str]], Mapping[str, str]]) -> None:
    frozen = make(PROPERTIES)
    copies = [copy.copy(frozen), copy.deepcopy(frozen)]
    copies += [pickle.loads(pickle.dumps(frozen, protocol)) for protocol in range(pickle.HIGHEST_PROTOCOL + 1)]
    for copied in copies:
        assert type(copied) is type(frozen)
        assert copied == frozen
        assert copied is not frozen
        assert hash(copied) == hash(frozen)
        assert repr(copied) == repr(frozen)
        with pytest.raises(TypeError):
            cast("dict[str, str]", copied)["username"] = "x"
    # 深拷贝逐项深拷贝内容：可变的值不共用
    nested = freeze({"items": ["a"]})
    deep = copy.deepcopy(nested)
    assert deep == nested
    assert deep["items"] is not nested["items"]


def test_calling_the_class_builds_a_plain_dict() -> None:
    # dataclasses.asdict 按 type(obj)(键值对) 重建 dict 子类：以类来调用得到普通 dict，只读的实例只经 freeze 建出
    for kind in (FrozenDict, RedactedDict):
        built: dict[str, str] = kind([("a", "b")])
        assert type(built) is dict
        assert built == {"a": "b"}
    assert type(freeze({"a": "b"})) is FrozenDict
    assert type(freeze_redacted({"a": "b"})) is RedactedDict
    # 已经是只读映射的原样用上，另一种类型的照样拷贝
    frozen = freeze({"a": "b"})
    assert freeze(frozen) is frozen
    assert type(freeze_redacted(frozen)) is RedactedDict
    assert freeze(MappingProxyType({"a": "b"})) == {"a": "b"}
    assert freeze([("a", "b")]) == {"a": "b"}


def test_the_properties_never_print_their_values(caplog: pytest.LogCaptureFixture) -> None:
    binding = secret_binding()
    properties = binding.properties
    assert type(properties) is RedactedDict
    expected = "{'Password': ..., 'empty': ..., 'password': ..., 'username': ...}"
    assert repr(properties) == expected
    with caplog.at_level(logging.INFO):
        logging.getLogger("xihan.tests.frozen").info("properties %s %r", properties, properties)
    outputs = {
        "repr": repr(properties),
        "str": str(properties),
        "format": format(properties),
        "f-string": f"{properties} {properties!r}",
        "pprint": pprint.pformat(properties, width=20),
        "pprint of a list": pprint.pformat([properties], width=20),
        "repr of a deep copy": repr(copy.deepcopy(properties)),
        "repr of a pickle": repr(pickle.loads(pickle.dumps(properties))),
        "repr of the binding": repr(binding),
        "repr of the configuration": repr(full_configuration()),
        "log message": caplog.records[0].getMessage(),
    }
    for name, text in outputs.items():
        for secret in SECRETS:
            assert secret not in text, f"{name} must not carry '{secret}': {text}"
    for name in ("repr", "str", "f-string", "pprint", "log message"):
        for part in ("Password", "empty", "password", "username"):
            assert part in outputs[name], f"{name} must carry '{part}': {outputs[name]}"
    assert repr(freeze_redacted({})) == "{}"
    # 信封的扩展属性不是凭据：调试输出与 dict 相同
    envelope = tenant_envelope()
    assert type(envelope.extensions) is FrozenDict
    assert repr(envelope.extensions) == repr(dict(envelope.extensions))
    assert "'xihantenant': 't1'" in repr(envelope)


def test_bindings_envelopes_and_configurations_copy_pickle_and_convert() -> None:
    binding = secret_binding()
    envelope = tenant_envelope()
    configuration = full_configuration()
    for value in (binding, envelope, configuration):
        copies = [copy.copy(value), copy.deepcopy(value)]
        copies += [pickle.loads(pickle.dumps(value, protocol)) for protocol in range(pickle.HIGHEST_PROTOCOL + 1)]
        for copied in copies:
            assert copied == value
            assert repr(copied) == repr(value)
    # 深拷贝与还原出来的绑定仍是只读、调试输出不带值的映射
    copied_configuration = copy.deepcopy(configuration)
    assert copied_configuration is not configuration
    assert copied_configuration.bindings[0] is not configuration.bindings[0]
    assert type(copied_configuration.bindings[0].properties) is RedactedDict
    assert type(pickle.loads(pickle.dumps(envelope)).extensions) is FrozenDict
    # 绑定与信封可以哈希：相等的哈希相同
    assert hash(binding) == hash(secret_binding())
    assert hash(envelope) == hash(tenant_envelope())
    assert binding != Binding("db", "connection:relational", VALUE, {**PROPERTIES, "empty": "x"})
    # dataclasses.replace 沿用同一个只读映射，改别的成员
    replaced = dataclasses.replace(binding, value="")
    assert replaced.properties is binding.properties
    assert replaced.value == ""

    # asdict：映射成员是普通 dict，可以照常改写、写成 JSON
    converted = dataclasses.asdict(configuration)
    assert converted == {
        "section": {"pool": "4", "retry": {"delays": ["1", "2"]}},
        "granted_permissions": ("binding:db", "binding:cache"),
        "bindings": (
            {"name": "db", "type": "connection:relational", "value": VALUE, "properties": PROPERTIES},
            {"name": "cache", "type": "cache:distributed", "value": "redis.internal:6379", "properties": {}},
        ),
        "work_directory": "/work",
    }
    properties = converted["bindings"][0]["properties"]
    assert type(properties) is dict
    assert type(converted["bindings"][1]["properties"]) is dict
    properties["password"] = "redacted"
    assert binding.properties["password"] == PASSWORD
    assert json.loads(json.dumps(converted))["bindings"][0]["properties"]["username"] == USERNAME
    extensions = dataclasses.asdict(envelope)["extensions"]
    assert type(extensions) is dict
    assert extensions == {"xihantenant": "t1", "traceparent": envelope.extensions["traceparent"]}
    assert type(dataclasses.asdict(binding)["properties"]) is dict
    assert dataclasses.astuple(binding) == ("db", "connection:relational", VALUE, PROPERTIES)


def test_bindings_accept_any_mapping_and_keep_their_own_copy() -> None:
    sources: list[Any] = [dict(PROPERTIES), MappingProxyType(dict(PROPERTIES)), freeze(PROPERTIES)]
    for source in sources:
        binding = Binding("db", "connection:relational", VALUE, source)
        assert type(binding.properties) is RedactedDict
        assert binding.properties == PROPERTIES
    envelope = Envelope(topic="t", id="1", source="/s", type="t", extensions=MappingProxyType({"a": "b"}))
    assert type(envelope.extensions) is FrozenDict
    assert type(Envelope(topic="t", id="1", source="/s", type="t").extensions) is FrozenDict
