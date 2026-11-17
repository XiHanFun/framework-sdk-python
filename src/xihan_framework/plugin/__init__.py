"""进程外插件运行时（RFC-0017，``spec/plugin/``）：协议 ``xihan.plugin.v1`` 的插件一侧。

读宿主下发的环境变量、在本机端点上以 gRPC 提供 ``Plugin`` 服务、向 stdout 写握手行、维护生命周期状态机与健康推送、
把调用交给 SDK 的调用器经完整管道执行、把投递交给订阅的处理器，并以 ``Host`` 客户端回调宿主；stdin 关闭即退出。
协议代码在 ``xihan_framework.plugin.v1``，由 ``spec/plugin/plugin.proto`` 生成。

需要可选依赖 ``grpcio`` 与 ``protobuf``：``pip install "xihan-framework[plugin]"``；核心五块不依赖它们。
"""

import importlib.util


def _missing(name: str) -> bool:
    """可选依赖是否没有安装；``google`` 是命名空间包，按子包 ``google.protobuf`` 查。"""
    try:
        return importlib.util.find_spec(name) is None
    except ModuleNotFoundError:
        return True


# 缺可选依赖时给出能照着做的错误，而不是生成代码里一句 No module named 'grpc'
_MISSING = [name for name in ("grpc", "google.protobuf") if _missing(name)]
if _MISSING:
    raise ModuleNotFoundError(
        "The plugin runtime needs the optional dependencies 'grpcio' and 'protobuf' "
        f'(missing: {", ".join(_MISSING)}); install them with: pip install "xihan-framework[plugin]"',
        name=_MISSING[0],
    )

from xihan_framework.plugin._host import EXTENSION_TENANT, PERMISSION_INVOKE, PERMISSION_PUBLISH, Host, host_of  # noqa: E402
from xihan_framework.plugin._lifecycle import SELF_REPORTED_STATES, SERVING_STATES  # noqa: E402
from xihan_framework.plugin._log import JsonLogHandler, level_name  # noqa: E402
from xihan_framework.plugin._messages import (  # noqa: E402
    CATALOG,
    CATALOG_NAME,
    ERROR_CODE_CALLBACK_DENIED,
    ERROR_CODE_UNAVAILABLE,
    ERROR_CODE_UNEXPECTED,
    LOCALES,
    MESSAGE_CALLBACK_DENIED,
    MESSAGE_CALLBACK_TENANT_DENIED,
    MESSAGE_FAILED,
    MESSAGE_SESSION_INVALID,
    MESSAGE_UNAVAILABLE,
    load_catalog,
)
from xihan_framework.plugin._model import (  # noqa: E402
    Binding,
    Configuration,
    Envelope,
    EventHandler,
    Health,
    HealthItem,
    HealthStatus,
    Hooks,
    Subscription,
    SubscriptionHandler,
    SubscriptionMode,
    is_topic,
)
from xihan_framework.plugin._protocol import (  # noqa: E402
    ENV_HOST_ADDRESS,
    ENV_INSTANCE,
    ENV_LOCALE,
    ENV_PRIMITIVES,
    ENV_PROTOCOL_VERSIONS,
    ENV_SESSION,
    HANDSHAKE_LINE_VERSION,
    LANGUAGE,
    PROTOCOL_VERSION,
    SESSION_METADATA_KEY,
    SUPPORTED_PROTOCOL_VERSIONS,
    Endpoint,
    Network,
    is_loopback_address,
    negotiate_version,
    parse_endpoint,
    parse_handshake_line,
)
from xihan_framework.plugin._runtime import Plugin  # noqa: E402

__all__ = [
    "CATALOG",
    "CATALOG_NAME",
    "ENV_HOST_ADDRESS",
    "ENV_INSTANCE",
    "ENV_LOCALE",
    "ENV_PRIMITIVES",
    "ENV_PROTOCOL_VERSIONS",
    "ENV_SESSION",
    "ERROR_CODE_CALLBACK_DENIED",
    "ERROR_CODE_UNAVAILABLE",
    "ERROR_CODE_UNEXPECTED",
    "EXTENSION_TENANT",
    "HANDSHAKE_LINE_VERSION",
    "LANGUAGE",
    "LOCALES",
    "MESSAGE_CALLBACK_DENIED",
    "MESSAGE_CALLBACK_TENANT_DENIED",
    "MESSAGE_FAILED",
    "MESSAGE_SESSION_INVALID",
    "MESSAGE_UNAVAILABLE",
    "PERMISSION_INVOKE",
    "PERMISSION_PUBLISH",
    "PROTOCOL_VERSION",
    "SELF_REPORTED_STATES",
    "SERVING_STATES",
    "SESSION_METADATA_KEY",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "Binding",
    "Configuration",
    "Endpoint",
    "Envelope",
    "EventHandler",
    "Health",
    "HealthItem",
    "HealthStatus",
    "Hooks",
    "Host",
    "JsonLogHandler",
    "Network",
    "Plugin",
    "Subscription",
    "SubscriptionHandler",
    "SubscriptionMode",
    "host_of",
    "is_loopback_address",
    "is_topic",
    "level_name",
    "load_catalog",
    "negotiate_version",
    "parse_endpoint",
    "parse_handshake_line",
]
