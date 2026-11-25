"""协议消息与 SDK 类型之间的转换：错误与结果（invocation.md §3）、上下文元数据（invocation.md §2）、信封与健康。"""

import calendar
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from xihan_framework._codec import encode
from xihan_framework._json import JsonTextError, JsonValue, dumps, parse
from xihan_framework._whitespace import is_blank
from xihan_framework.invocation import Cancellation, Context
from xihan_framework.plugin._messages import ERROR_CODE_UNEXPECTED, MESSAGE_FAILED
from xihan_framework.plugin._model import Envelope, HealthItem
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.primitives import (
    Error,
    LifecycleState,
    Result,
    Unit,
    fail,
    is_baggage_key_registered,
    is_error_code,
    is_message_key,
    success,
)

#: 宿主写来的错误码不合文法时，替换它的错误所在的阶段（invocation.md §3，参考实现 ``PluginErrorMapper.FromProto``）。
PHASE_INVOKE: Final = "invoke"
#: 宿主报了失败却没有错误时的 ``reason``（invocation.md §3）。
REASON_FAILURE_WITHOUT_ERROR: Final = "the host reported a failure without an error"

# deadline 的读法（截止时间文本的文法，RFC-0003 规则 8）：日期与时间的各段只认 ASCII 数字，小数 1–9 位，偏移是 Z 或 ±HH:MM；
# 各段的取值范围在匹配之后另行核对。[0-9] 是字面的码点范围，全角数字不会匹配
_DEADLINE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?"
    r"(?:[Zz]|([+-])([0-9]{2}):([0-9]{2}))"
)
_MIN_DEADLINE: Final = datetime(1, 1, 1, tzinfo=UTC)
_MAX_DEADLINE: Final = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def to_proto_error(error: Error) -> pb.Error:
    """把原语层错误编码为协议的 ``Error``（invocation.md §3）。

    ``code`` 与 ``message_key`` 原样；带消息键的错误 ``text`` 留空，由宿主按目录翻译成调用方的语言（RFC-0010），只有自由
    文本错误（``Error.literal``）写 ``text``；``details`` 按值模型写成 JSON 对象文本，没有时为空；``inner`` 递归写出。
    """
    text = "" if error.message_key is not None else error.message
    encoded = pb.Error(code=error.code, message_key=error.message_key or "", text=text)
    if error.details:
        encoded.details = dumps(encode(dict(error.details)))
    if error.inner is not None:
        encoded.inner.CopyFrom(to_proto_error(error.inner))
    return encoded


def from_proto_error(error: pb.Error, plugin_id: str) -> Error:
    """把宿主写来的协议 ``Error`` 读成原语层错误（invocation.md §3，与宿主读插件同一规则）。

    ``code`` 不合错误码文法时整个错误换成 ``PLUGINS_UNEXPECTED_001`` + ``plugins.process.failed``（``phase`` 为
    ``invoke``，``reason`` 为 ``invalid error code '<code>'``）；``message_key`` 合消息键文法的按键处理，否则成为自由文本
    错误，文案取 ``text``，``text`` 为空或只有空白（RFC-0001「空白字符」）时取 ``code``；``details`` 为空、通不过共同的
    JSON 读取检查或不是对象时当作没有细节；``inner`` 同此。
    """
    if not is_error_code(error.code):
        return failed(plugin_id, PHASE_INVOKE, f"invalid error code '{error.code}'")
    details = _details(error.details)
    inner = from_proto_error(error.inner, plugin_id) if error.HasField("inner") else None
    if is_message_key(error.message_key):
        return Error(error.code, error.message_key, details, inner)
    # 只有空白的 text 与空串一样取错误码（ADR-0245）：Error 的文案不能只有空白，原样交给 literal 会被拒
    return Error.literal(error.code, error.code if is_blank(error.text) else error.text, details, inner)


def to_status(result: Result[Any]) -> pb.Status:
    """把结果编码为协议的 ``Status``；失败的错误见 ``to_proto_error``。"""
    if result.error is None:
        return pb.Status(ok=True)
    return pb.Status(ok=False, error=to_proto_error(result.error))


def from_status(status: pb.Status | None, *, plugin_id: str, phase: str) -> Result[Unit]:
    """把宿主写来的 ``Status`` 读成结果；``ok`` 为假却没有 ``error`` 时为 ``PLUGINS_UNEXPECTED_001`` + ``plugins.process.failed``
    （``reason`` 为 ``the host reported a failure without an error``，invocation.md §3）。"""
    if status is not None and status.ok:
        return success()
    if status is None or not status.HasField("error"):
        return fail(failed(plugin_id, phase, REASON_FAILURE_WITHOUT_ERROR))
    return fail(from_proto_error(status.error, plugin_id))


def failed(plugin_id: str, phase: str, reason: str) -> Error:
    """插件一侧失败的错误：``PLUGINS_UNEXPECTED_001`` + ``plugins.process.failed``（参数 ``id``、``phase``、``reason``）。"""
    return Error.unexpected(ERROR_CODE_UNEXPECTED, MESSAGE_FAILED, {"id": plugin_id, "phase": phase, "reason": reason})


def parse_deadline(text: str) -> datetime | None:
    """按截止时间文本的文法（RFC-0003 规则 8）严格读 ``InvocationMetadata.deadline``，返回 UTC 时刻；读不出（含空串）为 ``None``。

    文法 ``^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,9})?([Zz]|[+-][0-9]{2}:[0-9]{2})\\z``，
    年 0001–9999、月日按公历（含闰年）、时 00–23、分 00–59、秒 00–59（不认闰秒）、偏移的时 00–23 与分 00–59，换算成
    UTC 之后（不计小数）在 ``0001-01-01T00:00:00`` 到 ``9999-12-31T23:59:59`` 之间。``datetime`` 精确到微秒，更细的
    小数截断、不进位。
    """
    match = _DEADLINE.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(index)) for index in range(1, 7))
    # datetime 构造时也会拒绝越界的值，这里先按规范逐段核对，不靠它抛出的异常判定
    if year < 1 or not 1 <= month <= 12 or hour > 23 or minute > 59 or second > 59:
        return None
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    offset = 0
    if match.group(8) is not None:
        offset_hours, offset_minutes = int(match.group(9)), int(match.group(10))
        if offset_hours > 23 or offset_minutes > 59:
            return None
        offset = offset_hours * 60 + offset_minutes
        if match.group(8) == "-":
            offset = -offset
    try:
        utc = datetime(year, month, day, hour, minute, second, tzinfo=UTC) - timedelta(minutes=offset)
    except OverflowError:
        # 换算成 UTC 之后落到 0001 年之前或 9999 年之后
        return None
    # 范围按不计小数的时刻判定，所以 9999-12-31T23:59:59.999999999Z 仍然合法
    if not _MIN_DEADLINE <= utc <= _MAX_DEADLINE:
        return None
    fraction = match.group(7) or ""
    return utc.replace(microsecond=int((fraction + "000000")[:6]))


def format_deadline(deadline: datetime) -> str:
    """按 .NET 的 ``O`` 格式写出截止时间（invocation.md §2）：UTC、七位小数、``+00:00``，如 ``2026-11-18T03:25:00.1234560+00:00``。

    ``datetime`` 只到微秒，第七位小数总是 ``0``；年份按四位补零，不经 ``strftime``（各平台的 ``%Y`` 对 1000 年之前不一致）。
    """
    utc = deadline.astimezone(UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
        f".{utc.microsecond:06d}0+00:00"
    )


def effective_deadline(metadata: str, grpc: datetime | None) -> datetime | None:
    """生效的截止时间（invocation.md §2）：``metadata`` 按 ``parse_deadline`` 读、读不出当作没有，与 gRPC 调用自身的截止时间
    （``grpc``，``None`` 为没有）取较早的一个；两者都没有时为 ``None``。"""
    declared = parse_deadline(metadata)
    if grpc is not None:
        grpc = grpc.astimezone(UTC)
    if declared is None:
        return grpc
    if grpc is None:
        return declared
    return min(declared, grpc)


def restore_context(
    metadata: pb.InvocationMetadata | None, *, cancellation: Cancellation, deadline: datetime | None
) -> Context:
    """按 invocation.md §2 从元数据还原调用上下文；``deadline`` 是 gRPC 调用自身的截止时间。

    ``trace_parent`` 按 RFC-0003 的文法取，不合文法即没有父 Span；``trace_state`` 不进上下文；截止时间见
    ``effective_deadline``；Baggage 只保留已登记的键（``xihan.principal`` 原样交给作者），已登记键不合约束的值由
    ``Context`` 丢弃。``cancellation`` 由运行时在调用方取消时触发。
    """
    if metadata is None:
        return Context(cancellation=cancellation, deadline=deadline)
    baggage = {key: value for key, value in metadata.baggage.items() if is_baggage_key_registered(key)}
    return Context(
        cancellation=cancellation,
        deadline=effective_deadline(metadata.deadline, deadline),
        trace_parent=metadata.trace_parent or None,
        baggage=baggage,
    )


def to_metadata(ctx: Context) -> pb.InvocationMetadata:
    """把回调所在的调用上下文编码为元数据（invocation.md §5）：当前 Span 的 ``traceparent``、按 §2 写法的截止时间与已登记的
    Baggage，``trace_state`` 为空。"""
    metadata = pb.InvocationMetadata(trace_parent=ctx.trace_parent)
    if ctx.deadline is not None:
        metadata.deadline = format_deadline(ctx.deadline)
    metadata.baggage.update(ctx.without_unregistered_baggage().baggage)
    return metadata


def to_proto_envelope(envelope: Envelope) -> pb.Envelope:
    """把信封编码为协议消息（invocation.md §4）：``dataschema`` 原样，没有写空串。"""
    return pb.Envelope(
        topic=envelope.topic,
        id=envelope.id,
        source=envelope.source,
        type=envelope.type,
        spec_version=envelope.spec_version,
        subject=envelope.subject or "",
        time=envelope.time or "",
        data_content_type=envelope.data_content_type,
        data_schema=envelope.data_schema or "",
        data=envelope.data,
        extensions=dict(envelope.extensions),
    )


def from_proto_envelope(envelope: pb.Envelope) -> Envelope:
    """把协议消息还原为信封；``specversion`` 固定 ``1.0``，``datacontenttype`` 缺省为 ``application/json``（RFC-0009）。

    ``dataschema`` 的空串是没有，其余原样交出、不裁剪（invocation.md §4）。
    """
    return Envelope(
        topic=envelope.topic,
        id=envelope.id,
        source=envelope.source,
        type=envelope.type,
        spec_version=envelope.spec_version or "1.0",
        subject=envelope.subject or None,
        time=envelope.time or None,
        data_content_type=envelope.data_content_type or "application/json",
        data_schema=envelope.data_schema or None,
        data=envelope.data,
        extensions=dict(envelope.extensions),
    )


def to_proto_health(state: LifecycleState, items: Iterable[HealthItem], error: Error | None) -> pb.HealthResponse:
    """把健康编码为协议消息；状态与健康项状态的数值与协议的枚举一致。"""
    response = pb.HealthResponse(state=pb.LifecycleState.ValueType(state.value))
    for item in items:
        response.items.append(
            pb.HealthItem(
                name=item.name,
                status=pb.HealthStatus.ValueType(item.status.value),
                description=item.description or "",
            )
        )
    if error is not None:
        response.error.CopyFrom(to_proto_error(error))
    return response


def _details(text: str) -> Mapping[str, JsonValue] | None:
    """读错误细节的 JSON 对象文本（invocation.md §3）：空、只有空白、通不过共同的 JSON 读取检查、不是对象、是空对象，或
    任何一层有读成双精度会溢出的数时为 ``None``，即没有细节。

    数按值模型读：64 位范围内的整数是 ``int``，超出的是 ``float``，下溢读成 0。
    """
    if not text:
        return None
    try:
        # 只有空白的文本在共同读取检查里是语法错误
        value = parse(text)
    except JsonTextError:
        return None
    if not isinstance(value, dict) or not value or _overflows(value):
        return None
    return value


def _overflows(value: JsonValue) -> bool:
    """任何一层有没有读成 ±∞ 的数：JSON 没有无穷大的写法，读出 ±∞ 只能是超出双精度的数。"""
    if isinstance(value, float):
        return math.isinf(value)
    if isinstance(value, list):
        return any(_overflows(item) for item in value)
    if isinstance(value, dict):
        return any(_overflows(member) for member in value.values())
    return False
