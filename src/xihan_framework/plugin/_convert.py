"""协议消息与 SDK 类型之间的转换：错误与结果（RFC-0001 线上编码）、上下文元数据（invocation.md §2）、信封与健康。"""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from xihan_framework._codec import encode
from xihan_framework._json import JsonTextError, JsonValue, dumps, parse
from xihan_framework.invocation import Cancellation, Context
from xihan_framework.plugin._messages import CATALOG, ERROR_CODE_UNEXPECTED, MESSAGE_FAILED
from xihan_framework.plugin._model import Envelope, HealthItem
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.primitives import (
    DEFAULT_LOCALE,
    Error,
    LifecycleState,
    MessageResolver,
    Result,
    Unit,
    fail,
    is_baggage_key_registered,
    is_error_code,
    is_message_key,
    resolve_message,
    success,
)


def error_text(error: Error, resolvers: Iterable[MessageResolver] = (), locale: str = DEFAULT_LOCALE) -> str:
    """错误随协议上线的文案：已解析的文案；否则依次按进程默认的解析器、``resolvers``（插件的消息目录）与运行时目录在
    ``locale`` 里解析消息键，都解析不了时同 ``Error.message``。

    宿主按 ``message_key`` 用握手时拿到的目录再翻译（invocation.md §3），文案是给没有这个键的一方看的。
    """
    if error.text is not None:
        return error.text
    key = error.message_key
    if key is not None:
        resolved = resolve_message(key, error.details)
        for resolver in (*resolvers, CATALOG):
            if resolved is not None:
                break
            resolved = resolver.resolve(key, locale, error.details)
        if resolved is not None:
            return resolved
    return error.message


def to_proto_error(error: Error, resolvers: Iterable[MessageResolver] = (), locale: str = DEFAULT_LOCALE) -> pb.Error:
    """把原语层错误编码为协议的 ``Error``：``details`` 按值模型写成 JSON 对象文本，``inner`` 递归（RFC-0001「Result」）。

    ``text`` 见 ``error_text``。
    """
    resolvers = tuple(resolvers)
    encoded = pb.Error(code=error.code, message_key=error.message_key or "", text=error_text(error, resolvers, locale))
    if error.details:
        encoded.details = dumps(encode(dict(error.details)))
    if error.inner is not None:
        encoded.inner.CopyFrom(to_proto_error(error.inner, resolvers, locale))
    return encoded


def from_proto_error(error: pb.Error) -> Error:
    """把协议的 ``Error`` 还原为原语层错误；宿主交来的错误原样保留码、键、文案与细节。

    ``details`` 不是 JSON 对象文本时没有细节，键不合文法时没有键（只留文案）；码不合 ``<DOMAIN>_<KIND>_<NNN>`` 时不能
    当作原语层错误，换成 ``PLUGINS_UNEXPECTED_001``，原码放进细节的 ``code``。
    """
    details = _details(error.details)
    inner = from_proto_error(error.inner) if error.HasField("inner") else None
    if not is_error_code(error.code):
        return Error.literal(
            ERROR_CODE_UNEXPECTED, error.text or ERROR_CODE_UNEXPECTED, {**(details or {}), "code": error.code}, inner
        )
    if error.message_key and is_message_key(error.message_key):
        restored = Error(error.code, error.message_key, details, inner)
        return restored.with_text(error.text) if error.text else restored
    # 没有键的错误（Error.Literal）以文案为准；文案也空时与 Error.message 一样是错误码
    return Error.literal(error.code, error.text or error.code, details, inner)


def to_status(
    result: Result[Any], resolvers: Iterable[MessageResolver] = (), locale: str = DEFAULT_LOCALE
) -> pb.Status:
    """把结果编码为协议的 ``Status``；失败的文案见 ``error_text``。"""
    if result.error is None:
        return pb.Status(ok=True)
    return pb.Status(ok=False, error=to_proto_error(result.error, resolvers, locale))


def from_status(status: pb.Status | None, *, plugin_id: str, phase: str) -> Result[Unit]:
    """把协议的 ``Status`` 还原为结果；失败却没有错误时为 ``PLUGINS_UNEXPECTED_001``。"""
    if status is not None and status.ok:
        return success()
    if status is None or not status.HasField("error"):
        return fail(failed(plugin_id, phase, "the status carries no error"))
    return fail(from_proto_error(status.error))


def failed(plugin_id: str, phase: str, reason: str, inner: Error | None = None) -> Error:
    """某一步未预期失败的错误：``PLUGINS_UNEXPECTED_001`` + ``plugins.process.failed``。"""
    return Error.unexpected(
        ERROR_CODE_UNEXPECTED, MESSAGE_FAILED, {"id": plugin_id, "phase": phase, "reason": reason}, inner
    )


def parse_deadline(text: str) -> datetime | None:
    """读 RFC 3339 的截止时间；空、不合法或不带时区时为 ``None``。"""
    if not text:
        return None
    try:
        deadline = datetime.fromisoformat(text)
    except ValueError:
        return None
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        return None
    return deadline.astimezone(UTC)


def format_deadline(deadline: datetime) -> str:
    """写出 RFC 3339 的 UTC 截止时间，带微秒，如 ``2026-11-17T08:30:00.000000Z``。"""
    return deadline.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def restore_context(
    metadata: pb.InvocationMetadata | None, *, cancellation: Cancellation, deadline: datetime | None
) -> Context:
    """按 invocation.md §2 从元数据还原调用上下文。

    追踪取 ``trace_parent``（不合法的当作没有给出）；截止时间取元数据与 gRPC 调用的截止时间（``deadline``）中较早的
    那个，规范要求二者相同；Baggage 只收已登记的键，未登记的剥离（RFC-0003 规则 3），已登记键不合约束的值由
    ``Context`` 丢弃。``cancellation`` 由运行时在调用方取消时触发。
    """
    if metadata is None:
        return Context(cancellation=cancellation, deadline=deadline)
    declared = parse_deadline(metadata.deadline)
    if declared is not None and (deadline is None or declared < deadline):
        deadline = declared
    baggage = {key: value for key, value in metadata.baggage.items() if is_baggage_key_registered(key)}
    return Context(
        cancellation=cancellation,
        deadline=deadline,
        trace_parent=metadata.trace_parent or None,
        baggage=baggage,
    )


def to_metadata(ctx: Context) -> pb.InvocationMetadata:
    """把调用上下文编码为元数据：``traceparent``（版本 ``00``、只带采样位）、截止时间与已登记的 Baggage（RFC-0003 规则 3、7）。"""
    metadata = pb.InvocationMetadata(trace_parent=ctx.trace_parent)
    if ctx.deadline is not None:
        metadata.deadline = format_deadline(ctx.deadline)
    metadata.baggage.update(ctx.without_unregistered_baggage().baggage)
    return metadata


def to_proto_envelope(envelope: Envelope) -> pb.Envelope:
    """把信封编码为协议消息。"""
    return pb.Envelope(
        topic=envelope.topic,
        id=envelope.id,
        source=envelope.source,
        type=envelope.type,
        spec_version=envelope.spec_version,
        subject=envelope.subject or "",
        time=envelope.time or "",
        data_content_type=envelope.data_content_type,
        data=envelope.data,
        extensions=dict(envelope.extensions),
    )


def from_proto_envelope(envelope: pb.Envelope) -> Envelope:
    """把协议消息还原为信封；``specversion`` 固定 ``1.0``，``datacontenttype`` 缺省为 ``application/json``（RFC-0009）。"""
    return Envelope(
        topic=envelope.topic,
        id=envelope.id,
        source=envelope.source,
        type=envelope.type,
        spec_version=envelope.spec_version or "1.0",
        subject=envelope.subject or None,
        time=envelope.time or None,
        data_content_type=envelope.data_content_type or "application/json",
        data=envelope.data,
        extensions=dict(envelope.extensions),
    )


def to_proto_health(
    state: LifecycleState,
    items: Iterable[HealthItem],
    error: Error | None,
    resolvers: Iterable[MessageResolver] = (),
    locale: str = DEFAULT_LOCALE,
) -> pb.HealthResponse:
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
        response.error.CopyFrom(to_proto_error(error, resolvers, locale))
    return response


def _details(text: str) -> Mapping[str, JsonValue] | None:
    """读错误细节的 JSON 对象文本；空或不是 JSON 对象时为 ``None``。"""
    if not text:
        return None
    try:
        value = parse(text)
    except JsonTextError:
        return None
    return value if isinstance(value, dict) else None
