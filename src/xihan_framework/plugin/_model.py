"""插件作者面对的类型：订阅与信封、健康、配置与生命周期回调（``spec/plugin/``）。"""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum, StrEnum
from types import MappingProxyType

from xihan_framework._json import JsonValue
from xihan_framework.invocation import Context
from xihan_framework.plugin._configuration import decode_section
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
    """健康回调报的健康（lifecycle.md §7.4）。

    ``state`` 只有 ``READY``、``DEGRADED``、``FAILED`` 生效：按原语层的迁移表从运行时的当前状态迁移过去（``FAILED`` 之后
    不再回来），``None`` 与其他状态不改变状态；``error`` 是 ``DEGRADED`` / ``FAILED`` 的原因。``items`` 里名字不在清单
    ``contributes.health`` 的健康项丢掉、不发给宿主。
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
    """``Configure`` 下发的内容（lifecycle.md §2、§7.2），原样交给作者。

    ``section`` 是清单 ``contributes.config.section`` 节的 JSON 对象（没有声明配置节、或宿主下发的为空时是空对象）；
    宿主写出的叶子一律是 JSON 字符串，类型化读取用 ``decode``。``granted_permissions`` 是宿主授予的权限，回调前的权限
    核对以它为准（invocation.md §5）；``bindings`` 只含清单声明的绑定；``work_directory`` 是宿主分配的可写目录。
    """

    section: Mapping[str, JsonValue]
    granted_permissions: tuple[str, ...] = ()
    bindings: tuple[Binding, ...] = ()
    work_directory: str = ""

    def decode[T](self, target: type[T]) -> T:
        """按目标类型（通常是 dataclass）读配置节（lifecycle.md §7.2）。

        成员名按 ASCII 不区分大小写匹配字段的 JSON 名（与 ``IConfiguration`` 一致）；目标是 ``int`` / ``float`` 的字符串
        叶子按 JSON 数字的文法读，目标是 ``bool`` 的字符串叶子只认 ``true``、``false``（ASCII 不区分大小写）；其余照操作
        载荷的解码规则。读不出时抛出 ``FrameworkError``（说明里带 JSON 路径），目标类型不受支持时抛出 ``TypeError``。
        """
        return decode_section(self.section, target)


@dataclass(frozen=True, slots=True, kw_only=True)
class Hooks:
    """插件作者接入生命周期的回调，都可以省略。

    异步回调返回失败结果即这一步失败（插件进入 ``Failed``，错误随 ``Status.error`` 交给宿主）；抛出的异常连同文本只写进
    stderr 日志，交给宿主的是 ``PLUGINS_UNEXPECTED_001`` + ``plugins.process.failed``，``reason`` 是异常的简单类型名
    （lifecycle.md §7.6）。``start`` 成功过才会调用 ``stop``，它在在途调用结束（或宽限期用完）之后调用、进程里最多一次，
    参数是停止的原因（``Stop`` 的 ``reason``，stdin 关闭与 SIGTERM 时为 ``shutdown``）与宽限期（lifecycle.md §7.5、§7.7）。
    ``health`` 是同步的，``Start`` 应答之前求一次，之后可服务期间每次 ``Health`` 与每 10 秒的健康推送各求一次，应当只读状态。
    """

    configure: Callable[[Configuration], Awaitable[Result[Unit]]] | None = None
    start: Callable[[], Awaitable[Result[Unit]]] | None = None
    stop: Callable[[str, timedelta], Awaitable[Result[Unit]]] | None = None
    health: Callable[[], Health] | None = None
