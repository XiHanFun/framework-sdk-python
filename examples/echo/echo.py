"""回声插件：用 ``xihan_framework.plugin`` 写的进程外插件，也是黑盒运行器的被测插件。

- ``echoes.create``：回声文本，附上配置节 ``XiHan:Echo`` 的问候语（类型化读取 ``Configuration.decode``）与上下文里的
  租户、主体、关联 ID；文本为空是业务失败，宿主用握手时拿到的消息目录 ``echo`` 翻译它。
- ``echoes.relay``：经宿主回调（``Host.Invoke``，权限 ``capability:kernel.pipeline``）调用另一个操作，返回宿主的应答；
  回调失败（如宿主没有可用的回调端点时的 ``plugins.host.unavailable``）原样作为结果。
- ``echoes.crash``：故意让进程异常退出而不应答，宿主应记为崩溃并按退避重启；跑黑盒用例时用 ``--exclude`` 排除。
- ``events.list``：列出收到的事件 ID（最多保留最近 1000 个）。
- 订阅 ``echo.events``（竞争消费），健康项 ``echo``。

运行：宿主按清单的 ``entry`` 启动插件目录里的启动脚本，脚本以绝对路径的解释器执行本文件（见文档站 Python SDK 页
「写一个插件」，https://framework.docs.xihanfun.com/sdks/python）；入口是 ``run(build)``，构造时的配置错误也记进 stderr 的 JSON 日志。
"""

import json
import logging
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from xihan_framework import operation
from xihan_framework.invocation import Context
from xihan_framework.plugin import (
    Configuration,
    Envelope,
    Health,
    HealthItem,
    HealthStatus,
    Hooks,
    Plugin,
    Subscription,
    SubscriptionHandler,
    host_of,
    load_catalog,
    run,
)
from xihan_framework.primitives import Error, FrameworkError, JsonValue, Result, Stability, Unit, fail, ok, success

#: 插件目录：清单、消息目录与本文件放在一起。
DIRECTORY = Path(__file__).resolve().parent

#: 文本为空。
ERROR_CODE_TEXT_MISSING = "ECHO_VALIDATION_001"
MESSAGE_TEXT_MISSING = "echo.text.missing"
#: 配置节不合法。
ERROR_CODE_CONFIGURATION_INVALID = "ECHO_VALIDATION_002"
MESSAGE_CONFIGURATION_INVALID = "echo.configuration.invalid"
#: 转发没有给操作 ID。
ERROR_CODE_OPERATION_MISSING = "ECHO_VALIDATION_003"
MESSAGE_OPERATION_MISSING = "echo.relay.operation_missing"

_logger = logging.getLogger("echo")


@dataclass(frozen=True)
class Settings:
    """配置节 ``XiHan:Echo``：成员名不区分大小写（宿主写出的是 ``IConfiguration`` 的键）。"""

    greeting: str | None = None


@dataclass(frozen=True)
class CreateRequest:
    """``echoes.create`` 的请求。"""

    text: str = ""


@dataclass(frozen=True)
class CreateResponse:
    """``echoes.create`` 的响应：没有值的成员不写出。"""

    text: str
    greeting: str | None = field(default=None, metadata=operation.json_field(omit_none=True))
    tenant: str | None = field(default=None, metadata=operation.json_field(omit_none=True))
    principal: str | None = field(default=None, metadata=operation.json_field(omit_none=True))
    correlation: str | None = field(default=None, metadata=operation.json_field(omit_none=True))


@dataclass(frozen=True)
class RelayRequest:
    """``echoes.relay`` 的请求：要调用的操作与它的请求（任意 JSON）。"""

    operation: str = ""
    payload: JsonValue = None


@dataclass(frozen=True)
class RelayResponse:
    """``echoes.relay`` 的响应：宿主应答的载荷。"""

    response: JsonValue


@dataclass(frozen=True)
class Empty:
    """没有成员的请求或响应。"""


@dataclass(frozen=True)
class EventsResponse:
    """``events.list`` 的响应。"""

    ids: list[str]


class Echo:
    """插件的状态：配置的问候语与收到的事件 ID（有界）。"""

    def __init__(self) -> None:
        """创建空状态。"""
        self.greeting: str | None = None
        self.events: deque[str] = deque(maxlen=1000)

    async def configure(self, configuration: Configuration) -> Result[Unit]:
        """读配置节 ``XiHan:Echo`` 的 ``greeting``；读不出是业务失败，宿主按目录翻译。"""
        try:
            settings = configuration.decode(Settings)
        except FrameworkError as error:
            return fail(
                Error.validation(
                    ERROR_CODE_CONFIGURATION_INVALID, MESSAGE_CONFIGURATION_INVALID, {"reason": error.message}
                )
            )
        self.greeting = settings.greeting
        return success()

    def health(self) -> Health:
        """健康项 ``echo`` 总是健康。"""
        return Health(items=[HealthItem("echo", HealthStatus.HEALTHY)])

    async def create(self, ctx: Context, request: CreateRequest) -> Result[CreateResponse]:
        """回声文本并附上下文。"""
        if not request.text:
            return fail(Error.validation(ERROR_CODE_TEXT_MISSING, MESSAGE_TEXT_MISSING))
        return ok(
            CreateResponse(
                text=request.text,
                greeting=self.greeting,
                tenant=ctx.tenant,
                principal=ctx.principal,
                correlation=ctx.correlation,
            )
        )

    async def relay(self, ctx: Context, request: RelayRequest) -> Result[RelayResponse]:
        """经宿主回调调用另一个操作；回调失败时把错误原样作为结果。"""
        if not request.operation:
            return fail(Error.validation(ERROR_CODE_OPERATION_MISSING, MESSAGE_OPERATION_MISSING))
        # 例子用标准库 json 转发任意 JSON；None 即空载荷，宿主按 {} 处理
        payload = b"" if request.payload is None else json.dumps(request.payload).encode("utf-8")
        response = await host_of(ctx).invoke(ctx, request.operation, payload)
        if response.outcome.error is not None:
            return fail(response.outcome.error)
        return ok(RelayResponse(json.loads(response.payload) if response.payload else {}))

    async def crash(self, ctx: Context, request: Empty) -> Result[Empty]:
        """故意异常退出，不应答。"""
        _logger.error("Crashing on request.")
        os._exit(3)

    async def list_events(self, ctx: Context, request: Empty) -> Result[EventsResponse]:
        """列出收到的事件 ID。"""
        return ok(EventsResponse(list(self.events)))

    async def handle_event(self, ctx: Context, envelope: Envelope, attempt: int) -> Result[Unit]:
        """记下投递的事件；按信封 ID 去重，至少一次投递下重复投递也只记一次。"""
        if envelope.id not in self.events:
            self.events.append(envelope.id)
        return success()


def build() -> Plugin:
    """组装插件：操作、订阅、消息目录与生命周期回调。"""
    echo = Echo()
    registry = operation.Registry()
    registry.add(
        operation.Contract(
            "echoes.create",
            stability=Stability.EXPERIMENTAL,
            expose=(operation.EXPOSE_HTTP, operation.EXPOSE_MCP),
        ),
        echo.create,
        request_type=CreateRequest,
        response_type=CreateResponse,
        schema=operation.Schema(
            request='{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}',
            response=(
                '{"type":"object","properties":{"text":{"type":"string"},"greeting":{"type":"string"},'
                '"tenant":{"type":"string"},"principal":{"type":"string"},"correlation":{"type":"string"}},'
                '"required":["text"]}'
            ),
        ),
    )
    registry.add(
        operation.Contract("echoes.relay", stability=Stability.EXPERIMENTAL, expose=(operation.EXPOSE_HTTP,)),
        echo.relay,
        request_type=RelayRequest,
        response_type=RelayResponse,
    )
    registry.add(
        operation.Contract("echoes.crash", stability=Stability.INTERNAL),
        echo.crash,
        request_type=Empty,
        response_type=Empty,
    )
    registry.add(
        operation.Contract("events.list", stability=Stability.EXPERIMENTAL, expose=(operation.EXPOSE_HTTP,)),
        echo.list_events,
        request_type=Empty,
        response_type=EventsResponse,
    )
    return Plugin(
        (DIRECTORY / "xihan.manifest.json").read_bytes(),
        operations=registry,
        subscriptions=[SubscriptionHandler(Subscription("echo.events"), echo.handle_event)],
        catalogs={"echo": load_catalog(DIRECTORY / "Localization")},
        hooks=Hooks(configure=echo.configure, health=echo.health),
    )


if __name__ == "__main__":
    sys.exit(run(build))
