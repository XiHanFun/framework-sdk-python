"""按名字上线的枚举共用的解析与编码（RFC-0001 的 ``Stability``、``LifecycleState``，RFC-0004 的 ``PipelineStage``）。"""

from enum import Enum
from typing import Final, Self, cast

from xihan_framework._json import JsonValue

# 枚举类 → 线上的名字 → 成员：第一次解析时建。只有继承 NamedEnum 的那几个枚举类，表不会增长
_MEMBERS_BY_NAME: Final[dict[type[Enum], dict[str, Enum]]] = {}


class NamedEnum(Enum):
    """按名字上线的枚举的共同部分；本身没有成员，只在本包里作基类，不是公开类型。

    ``str()`` 给出线上的名字，``to_json_value`` 写出它，``parse`` 与 ``from_json_value`` 按它还原；名字区分大小写、按整串
    比较。名字缺省是成员名的小写（``STABLE`` → ``stable``），写法不同的枚举重写 ``__str__``。
    """

    @classmethod
    def parse(cls, name: str) -> Self | None:
        """按线上的名字解析；不认识时为 ``None``。"""
        members = _MEMBERS_BY_NAME.get(cls)
        if members is None:
            # 并发的第一次解析各建一份内容相同的表，后写的换掉先写的，不需要锁
            members = _MEMBERS_BY_NAME[cls] = {str(member): member for member in cls}
        return cast("Self | None", members.get(name))

    def to_json_value(self) -> JsonValue:
        """线上编码：名字。"""
        return str(self)

    @classmethod
    def from_json_value(cls, value: JsonValue) -> Self:
        """从名字还原；不是字符串或不认识时抛出 ``ValueError``。"""
        member = cls.parse(value) if isinstance(value, str) else None
        if member is None:
            # 消息里的称呼取自类名：PipelineStage → pipeline stage
            kind = "".join(f" {letter.lower()}" if letter.isupper() else letter for letter in cls.__name__).strip()
            raise ValueError(f"'{value}' is not a {kind}.")
        return member

    def __str__(self) -> str:
        """线上的名字：成员名的小写，如 ``stable``。"""
        return self.name.lower()
