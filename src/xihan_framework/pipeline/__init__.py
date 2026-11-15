"""固定阶段的执行管道（RFC-0004）：中间件按阶段贡献，排序确定、可打印、可测试。

``Builder`` 收集中间件与 ``before`` / ``after`` 约束，按阶段 → 位置 → 归属 → 登记顺序排出基准次序，再做稳定拓扑排序；
中间件与管道的每一步都是 ``async``，可以直接在 asyncio（含 ``grpc.aio``）里执行。
"""

from xihan_framework.pipeline._pipeline import (
    APP_OWNER,
    ERROR_CODE_CYCLE,
    ERROR_CODE_REGISTRATION,
    ERROR_CODE_UNKNOWN_STAGE,
    Builder,
    Descriptor,
    Middleware,
    Placement,
    Step,
    func,
    parse_stage,
)

__all__ = [
    "APP_OWNER",
    "ERROR_CODE_CYCLE",
    "ERROR_CODE_REGISTRATION",
    "ERROR_CODE_UNKNOWN_STAGE",
    "Builder",
    "Descriptor",
    "Middleware",
    "Placement",
    "Step",
    "func",
    "parse_stage",
]
