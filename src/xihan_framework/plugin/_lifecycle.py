"""插件的生命周期状态机与在途调用的租户（``spec/plugin/lifecycle.md``、``invocation.md`` §5）。"""

import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final

from xihan_framework.primitives import Error, LifecycleState

#: 插件可以自报的状态（lifecycle.md §1）；其余由宿主裁决，``Start`` 之前的 ``Health`` 照实报本地状态（§7.4）。
SELF_REPORTED_STATES: Final = frozenset(
    {
        LifecycleState.STARTED,
        LifecycleState.READY,
        LifecycleState.DEGRADED,
        LifecycleState.DRAINING,
        LifecycleState.FAILED,
    }
)
#: 可服务的状态：只有在这两种状态下调用与投递才执行，其余状态以 ``plugins.process.unavailable`` 失败（lifecycle.md §7.3）。
SERVING_STATES: Final = frozenset({LifecycleState.READY, LifecycleState.DEGRADED})
#: 健康回调报了就照办的状态（lifecycle.md §7.4）；报其他状态的不改变状态。
HEALTH_REPORTED_STATES: Final = frozenset({LifecycleState.READY, LifecycleState.DEGRADED, LifecycleState.FAILED})


class Lifecycle:
    """插件进程自己的状态机：迁移按原语层的迁移表核对，状态或健康项变化时通知订阅者。

    gRPC 服务在事件循环里迁移状态，插件作者可能从别的线程报告健康变化，所以状态与订阅表都在锁里读写；
    通知在锁外执行，订阅者自己把它转回事件循环。
    """

    __slots__ = ("_error", "_listeners", "_lock", "_next_token", "_state")

    def __init__(self) -> None:
        """创建处于 ``Declared`` 的状态机。"""
        self._lock = threading.Lock()
        self._state = LifecycleState.DECLARED
        self._error: Error | None = None
        self._listeners: dict[int, Callable[[], None]] = {}
        self._next_token = 0

    @property
    def state(self) -> LifecycleState:
        """当前状态。"""
        return self._state

    def current(self) -> tuple[LifecycleState, Error | None]:
        """当前状态与它的原因（``Degraded`` / ``Failed`` 时可能有）。"""
        with self._lock:
            return self._state, self._error

    def transition(self, to: LifecycleState, error: Error | None = None) -> bool:
        """迁移到 ``to``；迁移表不允许时什么都不做并返回假。"""
        with self._lock:
            if not self._state.can_transition_to(to):
                return False
            self._state = to
            self._error = error
            listeners = list(self._listeners.values())
        for listener in listeners:
            listener()
        return True

    def report(self, error: Error | None) -> None:
        """状态不变而原因变了（健康回调在 ``Degraded`` 里报了另一个原因）：换掉原因，有变化时通知订阅者。"""
        with self._lock:
            if self._error == error:
                return
            self._error = error
            listeners = list(self._listeners.values())
        for listener in listeners:
            listener()

    def notify(self) -> None:
        """状态没变而健康项可能变了：通知订阅者重新计算健康。"""
        with self._lock:
            listeners = list(self._listeners.values())
        for listener in listeners:
            listener()

    def listen(self, listener: Callable[[], None]) -> Callable[[], None]:
        """订阅变化，返回退订函数；订阅表只含仍在推送的 ``WatchHealth`` 流。"""
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._listeners[token] = listener

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.pop(token, None)

        return unsubscribe


class TenantLedger:
    """宿主此刻交给插件处理的调用的租户（invocation.md §5，ADR-0195）。

    每个进行中的 ``Invoke`` / ``HandleEvent`` 在开始时记下宿主随调用交给插件的租户，结束时撤销；回调与发布只能带
    其中之一，没有进行中的调用时不能带租户。按租户计数，大小不超过同时进行的调用数。
    """

    __slots__ = ("_counts", "_lock")

    def __init__(self) -> None:
        """创建空的账本。"""
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    @contextmanager
    def handed(self, tenant: str | None) -> Iterator[None]:
        """在这段代码执行期间记下宿主交来的一个租户；``None`` 为无租户的调用，不记。"""
        if tenant is None:
            yield
            return
        with self._lock:
            self._counts[tenant] += 1
        try:
            yield
        finally:
            with self._lock:
                self._counts[tenant] -= 1
                if self._counts[tenant] <= 0:
                    del self._counts[tenant]

    def admits(self, tenant: str) -> bool:
        """租户是否是某个进行中的调用的租户。"""
        with self._lock:
            return self._counts[tenant] > 0
