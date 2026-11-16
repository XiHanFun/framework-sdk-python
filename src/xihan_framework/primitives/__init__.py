"""XiHan Framework 原语层（RFC-0001）的 Python 形态。

错误与错误码、``Result``、``Maybe``、类型化标识、生命周期状态、管道阶段、稳定性、Baggage 键与消息原语。
零行为、零依赖、只增不改；语义与线上编码以规范为准、与 .NET 参考实现一致，由 framework-spec 的一致性向量验证。
"""

from xihan_framework._json import JsonNumber, JsonValue
from xihan_framework.primitives._baggage import (
    BAGGAGE_CORRELATION,
    BAGGAGE_IDEMPOTENCY_KEY,
    BAGGAGE_LOCALE,
    BAGGAGE_PRINCIPAL,
    BAGGAGE_TENANT,
    MAX_BAGGAGE_VALUE_LENGTH,
    REGISTERED_BAGGAGE_KEYS,
    is_baggage_key_registered,
    is_baggage_key_well_formed,
    is_baggage_value_acceptable,
)
from xihan_framework.primitives._error import Error
from xihan_framework.primitives._error_code import (
    ERROR_CODE_INVALID_ARGUMENT,
    ERROR_CODE_UNINITIALIZED_RESULT,
    KIND_CANCELLED,
    KIND_CONFLICT,
    KIND_DENIED,
    KIND_NOT_FOUND,
    KIND_TIMEOUT,
    KIND_UNAVAILABLE,
    KIND_UNEXPECTED,
    KIND_VALIDATION,
    error_code_domain,
    error_code_kind,
    is_error_code,
    validate_error_code,
)
from xihan_framework.primitives._framework import (
    DOCUMENTATION,
    NAME,
    REPOSITORY,
    SDK_NAME,
    SDK_VERSION,
    SPEC_VERSION,
    FrameworkError,
)
from xihan_framework.primitives._id import TypedId
from xihan_framework.primitives._kernel_messages import BUILT_IN_CATALOG, MESSAGE_UNINITIALIZED_RESULT
from xihan_framework.primitives._lifecycle import LifecycleState
from xihan_framework.primitives._maybe import Maybe
from xihan_framework.primitives._messages import (
    DEFAULT_LOCALE,
    MAX_MESSAGE_KEY_LENGTH,
    Catalog,
    MessageResolver,
    default_locale,
    describe,
    format_argument,
    format_message,
    is_message_key,
    message_key_domain,
    placeholders,
    positional,
    resolve_message,
    set_default_messages,
    try_format_message,
)
from xihan_framework.primitives._result import UNIT, Result, Unit, fail, ok, success
from xihan_framework.primitives._stability import Stability
from xihan_framework.primitives._stage import PipelineStage

__all__ = [
    "BAGGAGE_CORRELATION",
    "BAGGAGE_IDEMPOTENCY_KEY",
    "BAGGAGE_LOCALE",
    "BAGGAGE_PRINCIPAL",
    "BAGGAGE_TENANT",
    "BUILT_IN_CATALOG",
    "DEFAULT_LOCALE",
    "DOCUMENTATION",
    "ERROR_CODE_INVALID_ARGUMENT",
    "ERROR_CODE_UNINITIALIZED_RESULT",
    "KIND_CANCELLED",
    "KIND_CONFLICT",
    "KIND_DENIED",
    "KIND_NOT_FOUND",
    "KIND_TIMEOUT",
    "KIND_UNAVAILABLE",
    "KIND_UNEXPECTED",
    "KIND_VALIDATION",
    "MAX_BAGGAGE_VALUE_LENGTH",
    "MAX_MESSAGE_KEY_LENGTH",
    "MESSAGE_UNINITIALIZED_RESULT",
    "NAME",
    "REGISTERED_BAGGAGE_KEYS",
    "REPOSITORY",
    "SDK_NAME",
    "SDK_VERSION",
    "SPEC_VERSION",
    "UNIT",
    "Catalog",
    "Error",
    "FrameworkError",
    "JsonNumber",
    "JsonValue",
    "LifecycleState",
    "Maybe",
    "MessageResolver",
    "PipelineStage",
    "Result",
    "Stability",
    "TypedId",
    "Unit",
    "default_locale",
    "describe",
    "error_code_domain",
    "error_code_kind",
    "fail",
    "format_argument",
    "format_message",
    "is_baggage_key_registered",
    "is_baggage_key_well_formed",
    "is_baggage_value_acceptable",
    "is_error_code",
    "is_message_key",
    "message_key_domain",
    "ok",
    "placeholders",
    "positional",
    "resolve_message",
    "set_default_messages",
    "success",
    "try_format_message",
    "validate_error_code",
]
