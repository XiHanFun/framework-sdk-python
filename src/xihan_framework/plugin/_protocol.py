"""协议常量、端点与握手行（``spec/plugin/handshake.md``）。"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: 本 SDK 实现的协议版本（``xihan.plugin.v1``）。
PROTOCOL_VERSION: Final = 1
#: 本 SDK 支持的全部协议版本，升序。
SUPPORTED_PROTOCOL_VERSIONS: Final = (PROTOCOL_VERSION,)
#: 握手行格式版本，与协议版本无关（handshake.md §2）。
HANDSHAKE_LINE_VERSION: Final = "xihan-plugin/1"
#: 携带会话令牌的 gRPC 元数据键：宿主调用插件、插件回调宿主的每次 RPC 都带它（handshake.md §4）。
SESSION_METADATA_KEY: Final = "xihan-plugin-session"
#: 握手时上报的实现语言。
LANGUAGE: Final = "python"

# 宿主下发的环境变量（handshake.md §1）
#: 宿主支持的协议版本，逗号分隔、升序。
ENV_PROTOCOL_VERSIONS: Final = "XIHAN_PLUGIN_PROTOCOL_VERSIONS"
#: 宿主的原语层大版本。
ENV_PRIMITIVES: Final = "XIHAN_PLUGIN_PRIMITIVES"
#: 本次启动的会话令牌。
ENV_SESSION: Final = "XIHAN_PLUGIN_SESSION"
#: 宿主回调端点 ``<network> <address>``。
ENV_HOST_ADDRESS: Final = "XIHAN_PLUGIN_HOST_ADDRESS"
#: 实例 ID。
ENV_INSTANCE: Final = "XIHAN_PLUGIN_INSTANCE"
#: 宿主默认区域（BCP 47）。
ENV_LOCALE: Final = "XIHAN_PLUGIN_LOCALE"

# 握手行文法 ^xihan-plugin/1 (unix|tcp|npipe) (\S+)$；ASCII 模式让 \S 只排除 ASCII 空白，与一致性运行器的 RE2 相同
_HANDSHAKE_LINE = re.compile(r"xihan-plugin/1 (unix|tcp|npipe) (\S+)", re.ASCII)
_ENDPOINT = re.compile(r"(unix|tcp|npipe) (\S+)", re.ASCII)
# tcp 只接受字面的回环地址 127.0.0.1 与 [::1]，主机名（含 localhost）不算（handshake.md §2）
_LOOPBACK = re.compile(r"(?:127\.0\.0\.1|\[::1\]):[0-9]+")


class Network(StrEnum):
    """端点的网络类型（``Endpoint.network``）。"""

    UNIX = "unix"
    TCP = "tcp"
    NPIPE = "npipe"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """一个本机 gRPC 端点：网络类型与地址（套接字路径、``host:port`` 或管道名）。"""

    network: Network
    address: str

    def __str__(self) -> str:
        """``<network> <address>``，即环境变量 ``XIHAN_PLUGIN_HOST_ADDRESS`` 的格式。"""
        return f"{self.network} {self.address}"

    @property
    def handshake_line(self) -> str:
        """公布这个端点的握手行（不含行尾的 ``\\n``）。"""
        return f"{HANDSHAKE_LINE_VERSION} {self}"

    @property
    def grpc_target(self) -> str | None:
        """grpcio 的拨号目标；grpcio 不支持命名管道，``npipe`` 为 ``None``。"""
        if self.network is Network.UNIX:
            return f"unix:{self.address}"
        if self.network is Network.TCP:
            return self.address
        return None


def is_loopback_address(address: str) -> bool:
    """判断 ``host:port`` 是否是回环地址 ``127.0.0.1`` 或 ``[::1]`` 加端口。"""
    return _LOOPBACK.fullmatch(address) is not None


def parse_handshake_line(line: str) -> Endpoint | None:
    """按 ``^xihan-plugin/1 (unix|tcp|npipe) (\\S+)$`` 解析握手行；不合文法或 tcp 不是回环地址时为 ``None``。"""
    match = _HANDSHAKE_LINE.fullmatch(line)
    return None if match is None else _endpoint(match)


def parse_endpoint(text: str) -> Endpoint | None:
    """解析 ``<network> <address>``（``XIHAN_PLUGIN_HOST_ADDRESS``）；规则与握手行的端点部分相同。"""
    match = _ENDPOINT.fullmatch(text)
    return None if match is None else _endpoint(match)


def negotiate_version(host: Iterable[int], plugin: Iterable[int] = SUPPORTED_PROTOCOL_VERSIONS) -> int | None:
    """选宿主列表里最高的、插件也支持的协议版本（handshake.md §3 第 1 条）；没有交集时为 ``None``。"""
    supported = set(plugin)
    common = [version for version in host if version in supported]
    return max(common) if common else None


def _endpoint(match: re.Match[str]) -> Endpoint | None:
    """由文法匹配结果组装端点，tcp 再核对回环地址。"""
    endpoint = Endpoint(Network(match.group(1)), match.group(2))
    if endpoint.network is Network.TCP and not is_loopback_address(endpoint.address):
        return None
    return endpoint
