"""九态生命周期（RFC-0001 LifecycleState）。"""

from enum import Enum
from typing import Final

from xihan_framework._json import JsonValue


class LifecycleState(Enum):
    """九态生命周期（RFC-0001）：数值与插件协议的 ``LifecycleState`` 一致，文本是 ``Declared`` 这样的名字。"""

    DECLARED = 0
    RESOLVED = 1
    CONFIGURED = 2
    STARTED = 3
    READY = 4
    DEGRADED = 5
    DRAINING = 6
    STOPPED = 7
    FAILED = 8

    @classmethod
    def parse(cls, name: str) -> "LifecycleState | None":
        """按名字（``Ready``）解析；不认识时为 ``None``。"""
        return _STATES_BY_NAME.get(name)

    @property
    def is_serving(self) -> bool:
        """是否可服务：``READY`` 或 ``DEGRADED``。"""
        return self in (LifecycleState.READY, LifecycleState.DEGRADED)

    @property
    def is_terminal(self) -> bool:
        """是否终态：``STOPPED``。"""
        return self is LifecycleState.STOPPED

    def can_transition_to(self, to: "LifecycleState") -> bool:
        """判断迁移是否合法；未列在迁移表里的一律非法。"""
        return to in _TRANSITIONS[self]

    def to_json_value(self) -> JsonValue:
        """线上编码：状态名。"""
        return str(self)

    @classmethod
    def from_json_value(cls, value: JsonValue) -> "LifecycleState":
        """从状态名还原；不认识时抛出 ``ValueError``。"""
        state = cls.parse(value) if isinstance(value, str) else None
        if state is None:
            raise ValueError(f"'{value}' is not a lifecycle state.")
        return state

    def __str__(self) -> str:
        """状态名，如 ``Ready``。"""
        return self.name.capitalize()


_STATES_BY_NAME: Final = {str(state): state for state in LifecycleState}

# 合法迁移的完整列表（conformance/vectors/primitives/lifecycle-transitions.json）；未列出的一律非法
_TRANSITIONS: Final[dict[LifecycleState, frozenset[LifecycleState]]] = {
    LifecycleState.DECLARED: frozenset({LifecycleState.RESOLVED, LifecycleState.FAILED}),
    LifecycleState.RESOLVED: frozenset({LifecycleState.CONFIGURED, LifecycleState.FAILED}),
    LifecycleState.CONFIGURED: frozenset({LifecycleState.STARTED, LifecycleState.FAILED}),
    LifecycleState.STARTED: frozenset(
        {LifecycleState.READY, LifecycleState.DEGRADED, LifecycleState.DRAINING, LifecycleState.FAILED}
    ),
    LifecycleState.READY: frozenset({LifecycleState.DEGRADED, LifecycleState.DRAINING, LifecycleState.FAILED}),
    LifecycleState.DEGRADED: frozenset({LifecycleState.READY, LifecycleState.DRAINING, LifecycleState.FAILED}),
    LifecycleState.DRAINING: frozenset({LifecycleState.STOPPED, LifecycleState.FAILED}),
    LifecycleState.STOPPED: frozenset(),
    LifecycleState.FAILED: frozenset({LifecycleState.RESOLVED}),
}
