"""协议常量、端点、握手行与宿主回调端点列表（``spec/plugin/handshake.md``）。"""

from collections.abc import Collection, Iterable
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
#: 宿主回调端点：一个或多个 ``<network> <address>``，按宿主的偏好排序、以 ``;`` 分隔（handshake.md §4）。
ENV_HOST_ADDRESS: Final = "XIHAN_PLUGIN_HOST_ADDRESS"
#: 实例 ID。
ENV_INSTANCE: Final = "XIHAN_PLUGIN_INSTANCE"
#: 宿主默认区域（BCP 47）。
ENV_LOCALE: Final = "XIHAN_PLUGIN_LOCALE"

# 握手行与端点里的空白（handshake.md §2，ADR-0237）：与 .NET 正则的 \s 相同。str.isspace 另把 U+001C–U+001F 算作空白，
# re 的 \s 也随之不同，所以按码点逐个列出
_WHITESPACE: Final = frozenset(
    map(
        chr,
        (
            *range(0x09, 0x0E),
            0x20,
            0x85,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
        ),
    )
)
# 握手行的前缀含其后的一个空格；之后的部分与宿主回调端点的一项同一文法
_HANDSHAKE_LINE_PREFIX: Final = HANDSHAKE_LINE_VERSION + " "


class Network(StrEnum):
    """端点的网络类型（``Endpoint.network``）。"""

    UNIX = "unix"
    TCP = "tcp"
    NPIPE = "npipe"


#: grpcio 能拨号的网络类型：它不支持 Windows 命名管道，插件按顺序取宿主回调端点里第一个这些类型的项（handshake.md §4）。
DIALABLE_NETWORKS: Final = frozenset({Network.UNIX, Network.TCP})

_NETWORKS: Final = frozenset(network.value for network in Network)


@dataclass(frozen=True, slots=True)
class Endpoint:
    """一个本机 gRPC 端点：网络类型与地址（套接字路径、``host:port`` 或管道名）。"""

    network: Network
    address: str

    def __str__(self) -> str:
        """``<network> <address>``，即 ``XIHAN_PLUGIN_HOST_ADDRESS`` 里一项的写法。"""
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


def is_whitespace(character: str) -> bool:
    """判断一个字符是不是握手行与回调端点所说的空白（handshake.md §2）。

    集合是 U+0009–U+000D、U+0020、U+0085、U+00A0、U+1680、U+2000–U+200A、U+2028、U+2029、U+202F、U+205F、U+3000；
    U+200B 等其他字符不算，可以出现在地址里。
    """
    return character in _WHITESPACE


def has_whitespace(text: str) -> bool:
    """文本里是否有 ``is_whitespace`` 所说的空白；SDK 自己生成的端点地址不能有（handshake.md §2）。"""
    return any(character in _WHITESPACE for character in text)


def is_loopback_address(address: str) -> bool:
    """判断 ``tcp`` 端点的地址是不是回环地址加端口（handshake.md §2）。

    IPv4 是 ``127.0.0.0/8`` 的点分十进制，四段、每段 0–255、不带前导零；IPv6 只认 ``[::1]``；端口是 1–65535 的十进制，
    不带正负号与前导零。主机名（含 ``localhost``）不算。
    """
    host, separator, port = address.rpartition(":")
    if not separator or not _decimal(port, 65535):
        return False
    if host == "[::1]":
        return True
    # 只认四段写法：127.1、0177.0.0.1、2130706433、0x7f.0.0.1 这些操作系统也接受的写法一律不合法
    segments = host.split(".")
    return (
        len(segments) == 4
        and segments[0] == "127"
        and all(_decimal(segment, 255) is not None for segment in segments[1:])
    )


def parse_endpoint(text: str) -> Endpoint | None:
    """解析 ``<network> <address>``；不合文法时为 ``None``（handshake.md §2）。

    ``network`` 是 ``unix``、``tcp`` 或 ``npipe``，与地址之间恰好一个空格；地址是一个或多个 ``is_whitespace`` 之外的字符
    （含非 ASCII 字母与 ``;``），``tcp`` 的地址另须通过 ``is_loopback_address``。
    """
    network, separator, address = text.partition(" ")
    if not separator or network not in _NETWORKS or not address or has_whitespace(address):
        return None
    if network == Network.TCP and not is_loopback_address(address):
        return None
    return Endpoint(Network(network), address)


def parse_handshake_line(line: str) -> Endpoint | None:
    """按 ``^xihan-plugin/1 (unix|tcp|npipe) (<地址>)\\z`` 解析去掉行尾之后的握手行；不合文法时为 ``None``。"""
    if not line.startswith(_HANDSHAKE_LINE_PREFIX):
        return None
    return parse_endpoint(line[len(_HANDSHAKE_LINE_PREFIX) :])


def parse_host_addresses(value: str) -> tuple[Endpoint, ...] | None:
    """解析 ``XIHAN_PLUGIN_HOST_ADDRESS``（handshake.md §4）：一个或多个以 ``;`` 分隔的端点，每项同 ``parse_endpoint``。

    任何一项不合法整个值就不合法，返回 ``None``；空串，以及开头、结尾或连续的 ``;`` 留下的空项都不合法。
    """
    endpoints: list[Endpoint] = []
    # 按 ; 切开后地址里自然没有 ;
    for part in value.split(";"):
        endpoint = parse_endpoint(part)
        if endpoint is None:
            return None
        endpoints.append(endpoint)
    return tuple(endpoints)


def select_host_endpoint(value: str | None, supported: Collection[Network] = DIALABLE_NETWORKS) -> Endpoint | None:
    """按 handshake.md §4 选回调端点：值合法时按顺序取第一个 ``network`` 在 ``supported`` 里的项，之后不换。

    变量缺失（``None``）、不合法或没有可用的项时为 ``None``：插件照常服务，回调以 ``plugins.host.unavailable`` 失败。
    """
    endpoints = None if value is None else parse_host_addresses(value)
    if endpoints is None:
        return None
    return next((endpoint for endpoint in endpoints if endpoint.network in supported), None)


def negotiate_version(host: Iterable[int], plugin: Iterable[int] = SUPPORTED_PROTOCOL_VERSIONS) -> int | None:
    """选宿主列表里最高的、插件也支持的协议版本（handshake.md §3 第 1 条）；没有交集时为 ``None``。"""
    supported = set(plugin)
    common = [version for version in host if version in supported]
    return max(common) if common else None


def _decimal(text: str, limit: int) -> int | None:
    """读不带正负号与前导零的 ASCII 十进制数（``0`` 本身可以）；不合写法或超过 ``limit`` 时为 ``None``。"""
    # 先按位数挡掉过长的数字，再看前导零
    if not text or len(text) > len(str(limit)) or (len(text) > 1 and text[0] == "0"):
        return None
    # str.isdigit 认全角与上标数字，这里只认 ASCII
    if not all("0" <= character <= "9" for character in text):
        return None
    value = int(text)
    return value if value <= limit else None
