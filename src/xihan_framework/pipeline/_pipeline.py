"""固定阶段的执行管道（RFC-0004）：中间件按阶段贡献，排序确定、可打印、可测试。"""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Protocol, Self

from xihan_framework.invocation import Context
from xihan_framework.primitives import FrameworkError, PipelineStage, Result, Unit

#: 中间件顺序约束成环。
ERROR_CODE_CYCLE: Final = "KERNEL_PIPELINE_001"
#: 阶段名未知。
ERROR_CODE_UNKNOWN_STAGE: Final = "KERNEL_PIPELINE_002"
#: 中间件重复注册，或顺序约束引用了未注册、跨阶段或有多个实例的中间件。
ERROR_CODE_REGISTRATION: Final = "KERNEL_PIPELINE_003"

#: 应用自己注册的中间件的归属。
APP_OWNER: Final = "app"

type Step = Callable[[Context], Coroutine[Any, Any, Result[Unit]]]
"""管道里的一步：中间件调用它进入下游。"""


class Middleware(Protocol):
    """一个中间件：``name`` 是类型名（排序约束与打印用），``invoke`` 可以在进入下游前后做事，也可以不调 ``next_step`` 而短路。

    可恢复失败返回失败结果，不抛异常；未处理的异常照常向外传播。
    """

    @property
    def name(self) -> str:
        """类型名。"""
        ...

    async def invoke(self, ctx: Context, next_step: Step) -> Result[Unit]:
        """执行这一步。"""
        ...


class _FunctionMiddleware:
    """让函数成为中间件。"""

    __slots__ = ("_invoke", "_name")

    def __init__(self, name: str, invoke: Callable[[Context, Step], Coroutine[Any, Any, Result[Unit]]]) -> None:
        self._name = name
        self._invoke = invoke

    @property
    def name(self) -> str:
        return self._name

    async def invoke(self, ctx: Context, next_step: Step) -> Result[Unit]:
        return await self._invoke(ctx, next_step)

    def __repr__(self) -> str:
        return f"func({self._name!r})"


def func(name: str, invoke: Callable[[Context, Step], Coroutine[Any, Any, Result[Unit]]]) -> Middleware:
    """用名字与异步函数创建中间件。"""
    return _FunctionMiddleware(name, invoke)


class Placement(Enum):
    """中间件在阶段内的位置：``BEGINNING`` 在前。"""

    BEGINNING = 0
    END = 1

    def __str__(self) -> str:
        """``Beginning`` 或 ``End``。"""
        return self.name.capitalize()


@dataclass(frozen=True, slots=True)
class Descriptor:
    """描述一个已排序的中间件。"""

    stage: PipelineStage
    """所在阶段。"""
    middleware: str
    """类型名。"""
    instance: str | None
    """按名登记的实例名；默认实例为 ``None``。"""
    owner: str
    """归属（清单 ID 或 ``app``）。"""
    placement: Placement
    """阶段内位置。"""

    @property
    def display(self) -> str:
        """打印名：类型名，命名实例为 ``类型名[实例名]``。"""
        return self.middleware if self.instance is None else f"{self.middleware}[{self.instance}]"


@dataclass(frozen=True, slots=True, eq=False)
class _Registration:
    """一次登记；``order`` 是登记序号，同一位置、同一归属时按它排。"""

    middleware: Middleware
    descriptor: Descriptor
    order: int


class Builder:
    """收集中间件与顺序约束，按规范排序后构建管道。

    登记与约束在 ``describe`` / ``dump`` / ``build`` 时才解析，错误以 ``FrameworkError`` 抛出（``KERNEL_PIPELINE_00x``）。
    ``build`` 返回的管道是当时登记内容的快照，之后的登记不影响它。
    """

    __slots__ = ("_constraints", "_registrations")

    def __init__(self) -> None:
        """创建空的构建器。"""
        self._registrations: list[_Registration] = []
        self._constraints: list[tuple[str, str]] = []

    def use(self, middleware: Middleware) -> Self:
        """登记中间件到 ``transaction`` 阶段末尾，归属 ``app``（RFC-0004 规则 1）。"""
        return self.use_at(middleware, PipelineStage.TRANSACTION)

    def use_at(
        self,
        middleware: Middleware,
        stage: PipelineStage,
        placement: Placement = Placement.END,
        owner: str = APP_OWNER,
    ) -> Self:
        """登记中间件到指定阶段。"""
        return self._add(middleware, None, stage, placement, owner)

    def use_named_instance(
        self,
        instance: str,
        middleware: Middleware,
        stage: PipelineStage,
        placement: Placement = Placement.END,
        owner: str = APP_OWNER,
    ) -> Self:
        """按名字登记同一类型的另一个实例（规则 7）；名字在类型内唯一。"""
        if not instance:
            raise FrameworkError(ERROR_CODE_REGISTRATION, "A named middleware instance needs a name.")
        return self._add(middleware, instance, stage, placement, owner)

    def before(self, middleware: str, other: str) -> Self:
        """约束 ``middleware`` 在 ``other`` 之前；都按类型名。"""
        self._constraints.append((middleware, other))
        return self

    def after(self, middleware: str, other: str) -> Self:
        """约束 ``middleware`` 在 ``other`` 之后；都按类型名。"""
        self._constraints.append((other, middleware))
        return self

    def describe(self) -> list[Descriptor]:
        """返回排序后的中间件描述。"""
        return [registration.descriptor for registration in self._sorted()]

    def dump(self) -> str:
        """按 ``xihan pipeline dump`` 的格式打印：一行一个中间件，阶段名左对齐占 12 列，空一格接打印名。"""
        return "".join(f"{descriptor.stage.manifest_name:<12} {descriptor.display}\n" for descriptor in self.describe())

    def build(self, terminal: Step) -> Step:
        """构建管道：``terminal`` 是 ``handle`` 阶段之后的终点（通常是操作分派）。"""
        step = terminal
        for registration in reversed(self._sorted()):
            step = _link(registration.middleware, step)
        return step

    def _add(
        self,
        middleware: Middleware,
        instance: str | None,
        stage: PipelineStage,
        placement: Placement,
        owner: str,
    ) -> Self:
        """登记一个中间件。"""
        if not isinstance(stage, PipelineStage):
            raise TypeError(f"The stage of the middleware '{middleware.name}' must be a PipelineStage, got {stage!r}.")
        if not middleware.name:
            raise FrameworkError(ERROR_CODE_REGISTRATION, "A middleware needs a name.")
        if not owner:
            raise FrameworkError(
                ERROR_CODE_REGISTRATION,
                f"The middleware '{middleware.name}' needs an owner: a manifest ID or '{APP_OWNER}'.",
            )
        descriptor = Descriptor(stage, middleware.name, instance, owner, placement)
        self._registrations.append(_Registration(middleware, descriptor, len(self._registrations)))
        return self

    def _sorted(self) -> list[_Registration]:
        """按规则排序：先阶段；阶段内先位置，再归属，再登记顺序，最后按约束做稳定拓扑排序。"""
        self._check_duplicates()
        by_type: dict[str, list[_Registration]] = {}
        for registration in self._registrations:
            by_type.setdefault(registration.descriptor.middleware, []).append(registration)
        # 约束按类型名引用：未注册、有多个实例、跨阶段都是登记错误
        resolved: list[tuple[_Registration, _Registration]] = []
        for first_name, second_name in self._constraints:
            first, second = _single(by_type, first_name), _single(by_type, second_name)
            if first.descriptor.stage is not second.descriptor.stage:
                raise _registration_error(
                    f"the ordering constraint between '{first_name}' and '{second_name}' crosses stages"
                )
            resolved.append((first, second))
        ordered: list[_Registration] = []
        for stage in PipelineStage:
            members = [registration for registration in self._registrations if registration.descriptor.stage is stage]
            # 基准次序：位置、归属的序数字典序、登记顺序（规则 2–4）；sort 是稳定的
            members.sort(key=lambda item: (item.descriptor.placement.value, item.descriptor.owner, item.order))
            ordered.extend(_topological_sort(members, resolved))
        return ordered

    def _check_duplicates(self) -> None:
        """同一类型的默认实例只登记一次、命名实例的名字在类型内唯一。"""
        seen: set[tuple[str, str | None]] = set()
        for registration in self._registrations:
            key = (registration.descriptor.middleware, registration.descriptor.instance)
            if key in seen:
                raise _registration_error(
                    f"the middleware '{registration.descriptor.display}' is registered more than once"
                )
            seen.add(key)


def parse_stage(name: str) -> PipelineStage:
    """解析清单里的阶段名；未知阶段抛出 ``FrameworkError``（``KERNEL_PIPELINE_002``）。"""
    stage = PipelineStage.parse(name)
    if stage is None:
        raise FrameworkError(ERROR_CODE_UNKNOWN_STAGE, f"'{name}' is not a pipeline stage.")
    return stage


def _link(middleware: Middleware, downstream: Step) -> Step:
    """把中间件接到下游之前。"""

    async def step(ctx: Context) -> Result[Unit]:
        return await middleware.invoke(ctx, downstream)

    return step


def _single(by_type: dict[str, list[_Registration]], name: str) -> _Registration:
    """按类型名取唯一的登记；没有或有多个实例都是登记错误。"""
    registrations = by_type.get(name, [])
    if not registrations:
        raise _registration_error(f"the ordering constraint references the unregistered middleware '{name}'")
    if len(registrations) > 1:
        raise _registration_error(
            f"the middleware '{name}' has several named instances; ordering constraints by type are ambiguous"
        )
    return registrations[0]


def _topological_sort(
    members: list[_Registration], constraints: list[tuple[_Registration, _Registration]]
) -> list[_Registration]:
    """在基准次序上应用约束做稳定拓扑排序（Kahn）：每次取基准次序最靠前的无前驱节点，约束只调整必要的相对位置。"""
    in_stage = {id(member) for member in members}
    incoming: dict[int, int] = {}
    outgoing: dict[int, list[_Registration]] = {}
    for first, second in constraints:
        # 跨阶段的约束已经拒绝，所以前一项在本阶段时后一项也在
        if id(first) not in in_stage:
            continue
        outgoing.setdefault(id(first), []).append(second)
        incoming[id(second)] = incoming.get(id(second), 0) + 1
    ordered: list[_Registration] = []
    remaining = list(members)
    while remaining:
        picked = next((position for position, item in enumerate(remaining) if incoming.get(id(item), 0) == 0), None)
        if picked is None:
            raise FrameworkError(
                ERROR_CODE_CYCLE, f"the ordering constraints in stage '{remaining[0].descriptor.stage}' form a cycle"
            )
        node = remaining.pop(picked)
        ordered.append(node)
        for successor in outgoing.get(id(node), []):
            incoming[id(successor)] -= 1
    return ordered


def _registration_error(message: str) -> FrameworkError:
    """登记错误（``KERNEL_PIPELINE_003``）；消息与 Go SDK 一致，句末加句号。"""
    return FrameworkError(ERROR_CODE_REGISTRATION, message + ".")
