"""插件作者面对的类型：订阅与信封、健康、配置与生命周期回调（``spec/plugin/``）。"""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum, StrEnum
from types import MappingProxyType

from xihan_framework._json import JsonValue
from xihan_framework.invocation import Context
from xihan_framework.primitives import ERROR_CODE_INVALID_ARGUMENT, Error, FrameworkError, LifecycleState, Result, Unit

# 主题文法（RFC-0009「信封」）
_TOPIC = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")


def is_topic(topic: str) -> bool:
    """判断主题是否符合 RFC-0009 的文法 ``^[a-z0-9][a-z0-9._-]{0,127}$``。"""
    return _TOPIC.fullmatch(topic) is not None


class SubscriptionMode(StrEnum):
    """订阅方式（RFC-0009「订阅方式」）：宿主按它向消息代理订阅主题，投递时随 ``HandleEvent`` 带回来。"""

    COMPETING = "competing"
    """竞争消费：订阅了主题的宿主实例里只有一个把消息交给插件；声明与投递里的空都同它。"""
    BROADCAST = "broadcast"
    """广播：每个在线的宿主实例都把消息交给自己的插件实例，尽力而为、不进死信。"""


@dataclass(frozen=True, slots=True)
class Subscription:
    """一个事件订阅：主题、只投递的 CloudEvents ``type``（``None`` 为主题下全部）与订阅方式（``None`` 同竞争消费）。

    同一（主题，类型）对只能声明一次，不能以两种方式各声明一次（handshake.md §3 第 5 条）。
    """

    topic: str
    message_type: str | None = None
    mode: SubscriptionMode | None = None

    def __post_init__(self) -> None:
        """核对主题文法；类型写成空串即编程错误（不限类型写 ``None``）。"""
        if not is_topic(self.topic):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The topic '{self.topic}' must match ^[a-z0-9][a-z0-9._-]{{0,127}}$."
            )
        if self.message_type == "":
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The message type of the subscription '{self.topic}' must not be empty."
            )

    @property
    def effective_mode(self) -> SubscriptionMode:
        """声明的方式；没有声明时是竞争消费。"""
        return self.mode or SubscriptionMode.COMPETING


@dataclass(frozen=True, slots=True, kw_only=True)
class Envelope:
    """CloudEvents 1.0 信封的平铺（RFC-0009）；``extensions`` 是扩展属性，如 ``xihantenant``。"""

    topic: str
    id: str
    source: str
    type: str
    spec_version: str = "1.0"
    subject: str | None = None
    time: str | None = None
    data_content_type: str = "application/json"
    data: bytes = b""
    extensions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """把扩展属性复制成只读映射。"""
        object.__setattr__(self, "extensions", MappingProxyType(dict(self.extensions)))


type EventHandler = Callable[[Context, Envelope, int], Awaitable[Result[Unit]]]
"""处理一条投递的消息：参数是上下文、信封与投递次数（从 1 起）；失败由宿主的消息代理按重试与死信处理，必须幂等。"""


@dataclass(frozen=True, slots=True)
class SubscriptionHandler:
    """订阅与它的处理器。"""

    subscription: Subscription
    handler: EventHandler


class HealthStatus(Enum):
    """健康项状态；数值与协议的 ``HealthStatus`` 一致。"""

    HEALTHY = 1
    DEGRADED = 2
    UNHEALTHY = 3


@dataclass(frozen=True, slots=True)
class HealthItem:
    """一个健康项；名字须在清单 ``contributes.health`` 里声明，没有声明的不上报。"""

    name: str
    status: HealthStatus
    description: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Health:
    """插件自报的健康。

    ``state`` 为 ``None`` 时沿用运行时的状态。插件能自报的状态只有 ``STARTED``、``READY``、``DEGRADED``、``DRAINING``、
    ``FAILED``（lifecycle.md §1），按原语层的迁移表从运行时的当前状态迁移过去，迁移不合法时运行时的状态不变；
    ``error`` 是 ``DEGRADED`` / ``FAILED`` 的原因。
    """

    state: LifecycleState | None = None
    items: Sequence[HealthItem] = ()
    error: Error | None = None


@dataclass(frozen=True, slots=True)
class Binding:
    """宿主解析好的资源绑定（清单 ``bindings``）。"""

    name: str
    type: str
    value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Configuration:
    """``Configure`` 下发的内容（lifecycle.md §2）。

    ``section`` 是清单 ``contributes.config.section`` 节的 JSON 对象（没有声明配置节时为空对象）；``granted_permissions``
    等于清单 ``permissions``；``bindings`` 只含清单声明的绑定；``work_directory`` 是宿主分配的可写目录。
    """

    section: Mapping[str, JsonValue]
    granted_permissions: tuple[str, ...] = ()
    bindings: tuple[Binding, ...] = ()
    work_directory: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Hooks:
    """插件作者接入生命周期的回调，都可以省略。

    异步回调返回失败结果即这一步失败（插件进入 ``Failed``，错误随 ``Status.error`` 交给宿主），抛出的异常记进日志并以
    ``PLUGINS_UNEXPECTED_001`` 交给宿主。``stop`` 在在途调用结束（或宽限期用完）之后调用，释放资源；``health`` 是同步的，
    每次健康探测与推送都会调用，应当只读状态。
    """

    configure: Callable[[Configuration], Awaitable[Result[Unit]]] | None = None
    start: Callable[[], Awaitable[Result[Unit]]] | None = None
    stop: Callable[[str, timedelta], Awaitable[Result[Unit]]] | None = None
    health: Callable[[], Health] | None = None
