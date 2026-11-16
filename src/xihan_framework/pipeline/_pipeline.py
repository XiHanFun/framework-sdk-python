"""固定阶段的执行管道（RFC-0004）：中间件按阶段贡献，排序确定、可打印、可测试。"""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
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


@dataclass(slots=True, eq=False)
class _Registration:
    """一次登记；``order`` 是登记序号，同一位置、同一归属时按它排。

    ``before`` / ``after`` 是以它为主体的按名字约束（它排在这些名字之前 / 之后），与 .NET 一样挂在主体上，排序时
    按基准次序逐个主体解析目标。
    """

    middleware: Middleware
    descriptor: Descriptor
    order: int
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


class Builder:
    """收集中间件与顺序约束，按规范排序后构建管道。

    重复登记、未知阶段与约束主体的问题在登记时抛出，约束目标的问题与成环在 ``describe`` / ``dump`` / ``build`` 排序时
    抛出（RFC-0004 排序规则第 8 条），都是 ``FrameworkError``（``KERNEL_PIPELINE_00x``），消息是与 .NET 逐字相同的英文
    原文，按名字登记的实例写成 ``类型名[名字]``。``build`` 返回的管道是当时登记内容的快照，之后的登记不影响它。
    """

    __slots__ = ("_registrations",)

    def __init__(self) -> None:
        """创建空的构建器。"""
        self._registrations: list[_Registration] = []

    def use(self, middleware: Middleware) -> Self:
        """登记中间件到 ``transaction`` 阶段末尾，归属 ``app``（RFC-0004 规则 1）。"""
        return self.use_at(middleware, PipelineStage.TRANSACTION)

    def use_at(
        self,
        middleware: Middleware,
        stage: PipelineStage | str,
        placement: Placement = Placement.END,
        owner: str = APP_OWNER,
    ) -> Self:
        """登记中间件到指定阶段。"""
        return self._add(middleware, None, stage, placement, owner)

    def use_named_instance(
        self,
        instance: str,
        middleware: Middleware,
        stage: PipelineStage | str,
        placement: Placement = Placement.END,
        owner: str = APP_OWNER,
    ) -> Self:
        """按名字登记同一类型的另一个实例（规则 7）；名字在类型内唯一。"""
        if not instance:
            raise FrameworkError(ERROR_CODE_REGISTRATION, "A named middleware instance needs a name.")
        return self._add(middleware, instance, stage, placement, owner)

    def before(self, middleware: str, other: str) -> Self:
        """约束 ``middleware`` 在 ``other`` 之前。

        两端都是中间件名：按名字登记的实例只按实例名引用，默认实例按类型名引用（与 .NET 的按名字约束相同）。约束主体
        ``middleware`` 在声明时必须已登记且没有歧义，否则立即抛出 ``KERNEL_PIPELINE_003``；目标 ``other`` 在排序时解析
        （RFC-0004 排序规则第 7、8 条）。
        """
        self._require(middleware).before.append(other)
        return self

    def after(self, middleware: str, other: str) -> Self:
        """约束 ``middleware`` 在 ``other`` 之后；名字的解析与 ``before`` 相同，约束主体 ``middleware`` 在声明时必须已登记。"""
        self._require(middleware).after.append(other)
        return self

    def extend(self, other: "Builder") -> Self:
        """把另一个构建器的登记与约束接在本构建器的登记之后，登记顺序随之排在后面；``other`` 不变。

        调用器用它把内置步骤放在应用的中间件之前登记（RFC-0011 规则 10）。与已有登记重复时抛出 ``KERNEL_PIPELINE_003``。
        """
        for registration in other._registrations:
            descriptor = registration.descriptor
            self._check_unique(descriptor)
            self._registrations.append(
                _Registration(
                    registration.middleware,
                    descriptor,
                    len(self._registrations),
                    list(registration.before),
                    list(registration.after),
                )
            )
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
        stage: PipelineStage | str,
        placement: Placement,
        owner: str,
    ) -> Self:
        """登记一个中间件；重复登记与未知阶段在这里立即报（RFC-0004 排序规则第 8 条）。

        阶段可以是 ``PipelineStage`` 或清单里的阶段名，名字不认识即 ``KERNEL_PIPELINE_002``。
        """
        if isinstance(stage, str):
            stage = parse_stage(stage)
        elif not isinstance(stage, PipelineStage):
            raise TypeError(f"The stage of the middleware '{middleware.name}' must be a PipelineStage, got {stage!r}.")
        if not middleware.name:
            raise FrameworkError(ERROR_CODE_REGISTRATION, "A middleware needs a name.")
        if not owner:
            raise FrameworkError(
                ERROR_CODE_REGISTRATION,
                f"The middleware '{middleware.name}' needs an owner: a manifest ID or '{APP_OWNER}'.",
            )
        descriptor = Descriptor(stage, middleware.name, instance, owner, placement)
        self._check_unique(descriptor)
        self._registrations.append(_Registration(middleware, descriptor, len(self._registrations)))
        return self

    def _check_unique(self, descriptor: Descriptor) -> None:
        """同一类型的默认实例只登记一次、命名实例的名字在类型内唯一；与 .NET 一样在登记时报，不推迟到排序。"""
        for registration in self._registrations:
            existing = registration.descriptor
            if (existing.middleware, existing.instance) != (descriptor.middleware, descriptor.instance):
                continue
            if descriptor.instance is None:
                raise FrameworkError(
                    ERROR_CODE_REGISTRATION,
                    f"Middleware '{descriptor.middleware}' is already registered; each middleware type may appear only "
                    "once in a pipeline unless its instances are named.",
                )
            raise FrameworkError(
                ERROR_CODE_REGISTRATION,
                f"Middleware '{descriptor.middleware}' named '{descriptor.instance}' is already registered; instance "
                "names must be unique within a type.",
            )

    def _sorted(self) -> list[_Registration]:
        """按规则排序：先阶段；阶段内先位置，再归属的序数序，再登记顺序，最后按约束做稳定拓扑排序。

        与 .NET 一样逐个阶段处理：一个阶段的约束目标都解析完（目标的错误在这里报）再排序（成环在这里报），然后才轮到
        下一个阶段，所以先报哪个错与 .NET 相同。
        """
        ordered: list[_Registration] = []
        for stage in PipelineStage:
            members = [registration for registration in self._registrations if registration.descriptor.stage is stage]
            # 基准次序：位置、归属的序数序（逐个 UTF-16 码元）、登记顺序（规则 2–4）；sort 是稳定的
            members.sort(
                key=lambda item: (item.descriptor.placement.value, _ordinal(item.descriptor.owner), item.order)
            )
            edges: list[tuple[_Registration, _Registration]] = []
            for member in members:
                edges.extend((member, self._resolve(member, name, stage)) for name in member.before)
                edges.extend((self._resolve(member, name, stage), member) for name in member.after)
            ordered.extend(_topological_sort(members, edges, stage))
        return ordered

    def _find(self, name: str) -> _Registration | None:
        """按名字找登记：命名实例按实例名，默认实例按类型名；一个名字对应多个登记是歧义。"""
        found: _Registration | None = None
        for registration in self._registrations:
            descriptor = registration.descriptor
            if (descriptor.middleware if descriptor.instance is None else descriptor.instance) != name:
                continue
            if found is not None:
                raise FrameworkError(
                    ERROR_CODE_REGISTRATION,
                    f"The middleware name '{name}' matches several registrations, so ordering constraints by name are "
                    "ambiguous for it.",
                )
            found = registration
        return found

    def _require(self, name: str) -> _Registration:
        """约束主体在声明时必须已登记、按名字没有歧义（RFC-0004 排序规则第 7、8 条），否则立即报。"""
        found = self._find(name)
        if found is None:
            raise FrameworkError(
                ERROR_CODE_REGISTRATION,
                f"Middleware '{name}' is not registered, so no ordering constraint can be declared for it.",
            )
        return found

    def _resolve(self, source: _Registration, target: str, stage: PipelineStage) -> _Registration:
        """排序时解析约束目标：未登记、有歧义或不在同一阶段都是登记错误。"""
        label = source.descriptor.display
        other = self._find(target)
        if other is None:
            raise FrameworkError(
                ERROR_CODE_REGISTRATION,
                f"The ordering constraint of middleware '{label}' references unregistered middleware '{target}'.",
            )
        if other.descriptor.stage is not stage:
            raise FrameworkError(
                ERROR_CODE_REGISTRATION,
                f"Middleware '{label}' (stage '{stage.manifest_name}') and '{target}' (stage "
                f"'{other.descriptor.stage.manifest_name}') are not in the same stage, so no relative order can be "
                "declared between them.",
            )
        return other


def parse_stage(name: str) -> PipelineStage:
    """解析清单里的阶段名；未知阶段抛出 ``FrameworkError``（``KERNEL_PIPELINE_002``）。"""
    stage = PipelineStage.parse(name)
    if stage is None:
        raise FrameworkError(ERROR_CODE_UNKNOWN_STAGE, f"Unknown pipeline stage '{name}'.")
    return stage


def _link(middleware: Middleware, downstream: Step) -> Step:
    """把中间件接到下游之前。"""

    async def step(ctx: Context) -> Result[Unit]:
        return await middleware.invoke(ctx, downstream)

    return step


def _ordinal(text: str) -> bytes:
    """按 UTF-16 码元比较的排序键，与 .NET 的 ``StringComparer.Ordinal`` 相同。"""
    return text.encode("utf-16-be", "surrogatepass")


def _topological_sort(
    members: list[_Registration], edges: list[tuple[_Registration, _Registration]], stage: PipelineStage
) -> list[_Registration]:
    """在基准次序上应用约束做稳定拓扑排序（Kahn）：每次取基准次序最靠前的无前驱节点，约束只调整必要的相对位置。

    成环时列出全部仍被卡住的中间件，按基准次序，按名字登记的实例带名字（RFC-0004 规则 8）。
    """
    incoming: dict[int, int] = {}
    outgoing: dict[int, list[_Registration]] = {}
    for first, second in edges:
        outgoing.setdefault(id(first), []).append(second)
        incoming[id(second)] = incoming.get(id(second), 0) + 1
    ordered: list[_Registration] = []
    remaining = list(members)
    while remaining:
        picked = next((position for position, item in enumerate(remaining) if incoming.get(id(item), 0) == 0), None)
        if picked is None:
            stuck = ", ".join(f"'{item.descriptor.display}'" for item in remaining)
            raise FrameworkError(
                ERROR_CODE_CYCLE,
                f"Middleware ordering constraints in stage '{stage.manifest_name}' form a cycle: {stuck}.",
            )
        node = remaining.pop(picked)
        ordered.append(node)
        for successor in outgoing.get(id(node), []):
            incoming[id(successor)] -= 1
    return ordered
