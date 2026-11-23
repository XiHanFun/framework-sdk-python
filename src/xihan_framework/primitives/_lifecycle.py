"""九态生命周期（RFC-0001 LifecycleState）。"""

from typing import Final

from xihan_framework.primitives._named_enum import NamedEnum


class LifecycleState(NamedEnum):
    """九态生命周期（RFC-0001）：数值与插件协议的 ``LifecycleState`` 一致，文本是 ``Declared`` 这样的名字。

    ``str()``、``parse``、``to_json_value`` 与 ``from_json_value`` 都按状态名（``Ready``）。
    """

    DECLARED = 0
    RESOLVED = 1
    CONFIGURED = 2
    STARTED = 3
    READY = 4
    DEGRADED = 5
    DRAINING = 6
    STOPPED = 7
    FAILED = 8

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

    def __str__(self) -> str:
        """状态名，如 ``Ready``。"""
        return self.name.capitalize()


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
