"""pipeline 的测试：Go SDK 移植来的排序、约束、命名实例与短路用例，以及打印格式与快照语义。"""

import asyncio

import pytest

from xihan_framework import pipeline
from xihan_framework.invocation import Context
from xihan_framework.pipeline import Builder, Middleware, Placement, Step
from xihan_framework.primitives import Error, FrameworkError, PipelineStage, Result, Unit, fail, success


def recording(name: str, log: list[str]) -> Middleware:
    """进入下游前后各记一笔的中间件。"""

    async def invoke(ctx: Context, next_step: Step) -> Result[Unit]:
        log.append(f"{name}:before")
        outcome = await next_step(ctx)
        log.append(f"{name}:after")
        return outcome

    return pipeline.func(name, invoke)


def passthrough(name: str) -> Middleware:
    """直接进入下游的中间件。"""

    async def invoke(ctx: Context, next_step: Step) -> Result[Unit]:
        return await next_step(ctx)

    return pipeline.func(name, invoke)


def assert_code(builder: Builder, code: str) -> FrameworkError:
    """排序必须以给定错误码失败。"""
    with pytest.raises(FrameworkError) as raised:
        builder.describe()
    assert raised.value.code == code, raised.value
    return raised.value


def run(step: Step) -> Result[Unit]:
    """在新的事件循环里执行管道。"""
    return asyncio.run(step(Context()))


def test_order_follows_stage_placement_owner_and_registration() -> None:
    log: list[str] = []
    builder = (
        Builder()
        .use_at(recording("Egress", log), PipelineStage.EGRESS, Placement.END, "app")
        .use_at(recording("ObserveLate", log), PipelineStage.OBSERVE, Placement.END, "xihan.b")
        .use_at(recording("ObserveEarly", log), PipelineStage.OBSERVE, Placement.BEGINNING, "xihan.z")
        .use_at(recording("ObserveA", log), PipelineStage.OBSERVE, Placement.END, "xihan.a")
        .use(recording("Transaction", log))
    )
    assert builder.dump() == (
        "observe      ObserveEarly\nobserve      ObserveA\nobserve      ObserveLate\ntransaction  Transaction\negress       Egress\n"
    )

    async def terminal(ctx: Context) -> Result[Unit]:
        log.append("terminal")
        return success()

    assert run(builder.build(terminal)).is_ok
    assert log == [
        "ObserveEarly:before",
        "ObserveA:before",
        "ObserveLate:before",
        "Transaction:before",
        "Egress:before",
        "terminal",
        "Egress:after",
        "Transaction:after",
        "ObserveLate:after",
        "ObserveA:after",
        "ObserveEarly:after",
    ]


def test_constraints_reorder_within_a_stage() -> None:
    builder = (
        Builder()
        .use_at(passthrough("A"), PipelineStage.VALIDATE)
        .use_at(passthrough("B"), PipelineStage.VALIDATE)
        .use_at(passthrough("C"), PipelineStage.VALIDATE)
        .before("C", "A")
        .after("B", "C")
    )
    assert [descriptor.middleware for descriptor in builder.describe()] == ["C", "A", "B"]


def test_constraints_only_move_what_they_must() -> None:
    builder = Builder()
    for name in ["A", "B", "C", "D", "E"]:
        builder.use_at(passthrough(name), PipelineStage.AUTHORIZE)
    builder.after("A", "D")
    assert [descriptor.middleware for descriptor in builder.describe()] == ["B", "C", "D", "A", "E"]


def test_cycles_and_bad_constraints_are_rejected() -> None:
    cycle = (
        Builder()
        .use_at(passthrough("A"), PipelineStage.VALIDATE)
        .use_at(passthrough("B"), PipelineStage.VALIDATE)
        .before("A", "B")
        .before("B", "A")
    )
    assert (
        assert_code(cycle, pipeline.ERROR_CODE_CYCLE).message
        == "the ordering constraints in stage 'validate' form a cycle"
    )

    unregistered = Builder().use_at(passthrough("A"), PipelineStage.VALIDATE).before("A", "Missing")
    error = assert_code(unregistered, pipeline.ERROR_CODE_REGISTRATION)
    assert error.message == "the ordering constraint references the unregistered middleware 'Missing'."

    cross_stage = (
        Builder()
        .use_at(passthrough("A"), PipelineStage.VALIDATE)
        .use_at(passthrough("B"), PipelineStage.EGRESS)
        .before("A", "B")
    )
    assert_code(cross_stage, pipeline.ERROR_CODE_REGISTRATION)

    duplicate = (
        Builder().use_at(passthrough("A"), PipelineStage.VALIDATE).use_at(passthrough("A"), PipelineStage.VALIDATE)
    )
    assert_code(duplicate, pipeline.ERROR_CODE_REGISTRATION)

    with pytest.raises(FrameworkError) as raised:
        pipeline.parse_stage("middle")
    assert raised.value.code == pipeline.ERROR_CODE_UNKNOWN_STAGE
    assert pipeline.parse_stage("handle") is PipelineStage.HANDLE


def test_named_instances_are_distinct_but_ambiguous_for_type_constraints() -> None:
    builder = (
        Builder()
        .use_named_instance("guard-a", passthrough("Guard"), PipelineStage.OBSERVE, Placement.END, "xihan.plugins.a")
        .use_named_instance("guard-b", passthrough("Guard"), PipelineStage.OBSERVE, Placement.END, "xihan.plugins.b")
    )
    assert builder.dump() == "observe      Guard[guard-a]\nobserve      Guard[guard-b]\n"
    descriptors = builder.describe()
    assert descriptors[0].instance == "guard-a"
    assert descriptors[0].owner == "xihan.plugins.a"
    builder.use_at(passthrough("Other"), PipelineStage.OBSERVE).before("Guard", "Other")
    assert_code(builder, pipeline.ERROR_CODE_REGISTRATION)
    duplicate = (
        Builder()
        .use_named_instance("x", passthrough("Guard"), PipelineStage.OBSERVE)
        .use_named_instance("x", passthrough("Guard"), PipelineStage.OBSERVE)
    )
    assert "'Guard[x]'" in assert_code(duplicate, pipeline.ERROR_CODE_REGISTRATION).message


def test_short_circuit_skips_downstream() -> None:
    log: list[str] = []
    denied = Error.denied("ORDERS_DENIED_001", "orders.denied")

    async def deny(ctx: Context, next_step: Step) -> Result[Unit]:
        return fail(denied)

    async def terminal(ctx: Context) -> Result[Unit]:
        log.append("terminal")
        return success()

    builder = (
        Builder()
        .use_at(pipeline.func("Deny", deny), PipelineStage.AUTHORIZE)
        .use_at(recording("Handle", log), PipelineStage.HANDLE)
    )
    outcome = run(builder.build(terminal))
    assert outcome.error == denied
    assert log == []


def test_built_pipelines_are_snapshots() -> None:
    log: list[str] = []

    async def terminal(ctx: Context) -> Result[Unit]:
        log.append("terminal")
        return success()

    builder = Builder()
    empty = builder.build(terminal)
    builder.use(recording("Late", log))
    assert run(empty).is_ok
    assert log == ["terminal"]
    log.clear()
    assert run(builder.build(terminal)).is_ok
    assert log == ["Late:before", "terminal", "Late:after"]


def test_dump_pads_stage_names_to_twelve_columns() -> None:
    builder = (
        Builder()
        .use_at(passthrough("Auth"), PipelineStage.AUTHENTICATE)
        .use_at(passthrough("Idempotency"), PipelineStage.IDEMPOTENCY)
    )
    assert builder.dump() == "authenticate Auth\nidempotency  Idempotency\n"
    assert Builder().dump() == ""


def test_registrations_need_names_owners_and_stages() -> None:
    with pytest.raises(FrameworkError) as raised:
        Builder().use(passthrough(""))
    assert raised.value.code == pipeline.ERROR_CODE_REGISTRATION
    with pytest.raises(FrameworkError):
        Builder().use_at(passthrough("A"), PipelineStage.OBSERVE, Placement.END, "")
    with pytest.raises(FrameworkError):
        Builder().use_named_instance("", passthrough("A"), PipelineStage.OBSERVE)
    with pytest.raises(TypeError):
        Builder().use_at(passthrough("A"), "observe")  # type: ignore[arg-type]
    assert str(Placement.BEGINNING) == "Beginning"


def test_middleware_exceptions_propagate() -> None:
    async def explode(ctx: Context, next_step: Step) -> Result[Unit]:
        raise LookupError("boom")

    async def terminal(ctx: Context) -> Result[Unit]:
        return success()

    step = Builder().use_at(pipeline.func("Explode", explode), PipelineStage.OBSERVE).build(terminal)
    with pytest.raises(LookupError):
        run(step)


def test_constraints_may_name_instances() -> None:
    # RFC-0004 排序规则第 7 条：有多个实例的类型按实例名约束，两端都可以是实例名或只登记一次的类型名
    builder = (
        Builder()
        .use_named_instance("guard-a", passthrough("Guard"), PipelineStage.OBSERVE, Placement.END, "xihan.plugins.a")
        .use_named_instance("guard-b", passthrough("Guard"), PipelineStage.OBSERVE, Placement.END, "xihan.plugins.b")
        .use_at(passthrough("Audit"), PipelineStage.OBSERVE, Placement.END, "app")
        .before("guard-b", "guard-a")
        .after("Audit", "guard-b")
    )
    assert [descriptor.display for descriptor in builder.describe()] == ["Guard[guard-b]", "Audit", "Guard[guard-a]"]
    crossing = (
        Builder()
        .use_named_instance("guard-a", passthrough("Guard"), PipelineStage.OBSERVE)
        .use_at(passthrough("Tx"), PipelineStage.TRANSACTION)
        .before("guard-a", "Tx")
    )
    assert_code(crossing, pipeline.ERROR_CODE_REGISTRATION)
    clash = (
        Builder()
        .use_at(passthrough("Audit"), PipelineStage.OBSERVE)
        .use_named_instance("Audit", passthrough("Guard"), PipelineStage.OBSERVE)
        .use_at(passthrough("Other"), PipelineStage.OBSERVE)
        .before("Other", "Audit")
    )
    assert "refers to more than one middleware" in assert_code(clash, pipeline.ERROR_CODE_REGISTRATION).message


def test_constraint_subjects_must_be_registered_when_declared() -> None:
    for declare in (Builder.before, Builder.after):
        with pytest.raises(FrameworkError) as raised:
            declare(Builder().use_at(passthrough("A"), PipelineStage.VALIDATE), "Missing", "A")
        assert raised.value.code == pipeline.ERROR_CODE_REGISTRATION
        assert "'Missing'" in raised.value.message
    # 目标在排序时才解析：可以先声明约束、后登记目标
    builder = Builder().use_at(passthrough("A"), PipelineStage.VALIDATE).before("A", "B")
    builder.use_at(passthrough("B"), PipelineStage.VALIDATE)
    assert [descriptor.middleware for descriptor in builder.describe()] == ["A", "B"]
