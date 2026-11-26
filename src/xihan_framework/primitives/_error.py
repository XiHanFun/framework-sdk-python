"""可恢复失败的描述（RFC-0001 Error）。"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Self

from xihan_framework._json import JsonValue, dumps, parse
from xihan_framework._whitespace import is_blank
from xihan_framework.primitives._error_code import (
    ERROR_CODE_INVALID_ARGUMENT,
    KIND_CANCELLED,
    KIND_CONFLICT,
    KIND_DENIED,
    KIND_NOT_FOUND,
    KIND_TIMEOUT,
    KIND_UNAVAILABLE,
    KIND_UNEXPECTED,
    KIND_VALIDATION,
    invalid_argument,
    is_error_code,
    validate_error_code,
)
from xihan_framework.primitives._framework import FrameworkError
from xihan_framework.primitives._kernel_messages import BUILT_IN_CATALOG
from xihan_framework.primitives._messages import DEFAULT_LOCALE, describe, is_message_key, resolve_message


class Error:
    """可恢复失败的描述（RFC-0001 Error）：错误码面向程序，消息键与参数面向人。

    不可变，``with_*`` 方法返回新值。``details`` 既是模板参数也是结构化细节，只允许 JSON 可表示的值，构造时
    校验并复制成只读映射。相等按码、消息键、文案、细节与内部错误比较。
    """

    __slots__ = ("_code", "_details", "_inner", "_message_key", "_text")

    def __init__(
        self,
        code: str,
        message_key: str,
        details: Mapping[str, JsonValue] | None = None,
        inner: "Error | None" = None,
    ) -> None:
        """用错误码与消息键创建；码或键不合文法抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。"""
        validate_error_code(code)
        if not is_message_key(message_key):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT,
                f"The message key '{message_key}' must match <domain>.<segment>[.<segment>].",
            )
        self._code = code
        self._message_key: str | None = message_key
        self._text: str | None = None
        self._details = _freeze_details(details)
        self._inner = inner

    @classmethod
    def literal(
        cls,
        code: str,
        text: str,
        details: Mapping[str, JsonValue] | None = None,
        inner: "Error | None" = None,
    ) -> Self:
        """用已解析的文案创建：线上还原与异常转换用，没有消息键；文案为空或只有空白抛出 ``FrameworkError``。

        空白按 RFC-0001「空白字符」的 25 个码位，不用 ``str.isspace``：U+001C–U+001F 与零宽空格不是空白（ADR-0245）。
        """
        validate_error_code(code)
        if is_blank(text):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, "A literal error needs a text that is not empty or whitespace."
            )
        return cls._create(code, None, text, _freeze_details(details), inner)

    @classmethod
    def validation(cls, code: str, message_key: str, details: Mapping[str, JsonValue] | None = None) -> Self:
        """创建 VALIDATION 类错误；码的 KIND 段不符抛出 ``FrameworkError``。"""
        return cls._of_kind(KIND_VALIDATION, code, message_key, details, None)

    @classmethod
    def not_found(cls, code: str, message_key: str, details: Mapping[str, JsonValue] | None = None) -> Self:
        """创建 NOTFOUND 类错误。"""
        return cls._of_kind(KIND_NOT_FOUND, code, message_key, details, None)

    @classmethod
    def conflict(cls, code: str, message_key: str, details: Mapping[str, JsonValue] | None = None) -> Self:
        """创建 CONFLICT 类错误。"""
        return cls._of_kind(KIND_CONFLICT, code, message_key, details, None)

    @classmethod
    def denied(cls, code: str, message_key: str, details: Mapping[str, JsonValue] | None = None) -> Self:
        """创建 DENIED 类错误。"""
        return cls._of_kind(KIND_DENIED, code, message_key, details, None)

    @classmethod
    def unavailable(cls, code: str, message_key: str, details: Mapping[str, JsonValue] | None = None) -> Self:
        """创建 UNAVAILABLE 类错误。"""
        return cls._of_kind(KIND_UNAVAILABLE, code, message_key, details, None)

    @classmethod
    def timeout(cls, code: str, message_key: str, details: Mapping[str, JsonValue] | None = None) -> Self:
        """创建 TIMEOUT 类错误。"""
        return cls._of_kind(KIND_TIMEOUT, code, message_key, details, None)

    @classmethod
    def cancelled(cls, code: str, message_key: str, details: Mapping[str, JsonValue] | None = None) -> Self:
        """创建 CANCELLED 类错误。"""
        return cls._of_kind(KIND_CANCELLED, code, message_key, details, None)

    @classmethod
    def unexpected(
        cls,
        code: str,
        message_key: str,
        details: Mapping[str, JsonValue] | None = None,
        inner: "Error | None" = None,
    ) -> Self:
        """创建 UNEXPECTED 类错误，可带内部错误；参数顺序与构造一致，先细节后内部错误。"""
        return cls._of_kind(KIND_UNEXPECTED, code, message_key, details, inner)

    @property
    def code(self) -> str:
        """错误码 ``<领域大写>_<KIND>_<NNN>``。"""
        return self._code

    @property
    def message_key(self) -> str | None:
        """消息键；``literal`` 创建的为 ``None``。"""
        return self._message_key

    @property
    def text(self) -> str | None:
        """已解析的文案；未解析为 ``None``。"""
        return self._text

    @property
    def details(self) -> Mapping[str, JsonValue] | None:
        """模板参数，也是结构化细节；只读。"""
        return self._details

    @property
    def inner(self) -> "Error | None":
        """内部错误。"""
        return self._inner

    @property
    def kind(self) -> str:
        """错误码的 KIND 段。"""
        return self._code.split("_")[1]

    @property
    def domain(self) -> str:
        """错误码的领域段。"""
        return self._code.split("_")[0]

    @property
    def message(self) -> str:
        """面向人的消息：已解析的文案；否则按进程默认区域解析消息键，再找 SDK 自带的默认英文模板；都解析不了时是 ``键 (参数)``。

        没有键时（``literal`` 之外不会出现）是错误码。解析按严格格式化：模板的占位符缺值时退回描述。
        """
        if self._text is not None:
            return self._text
        if self._message_key is None:
            return self._code
        resolved = resolve_message(self._message_key, self._details)
        if resolved is None:
            resolved = BUILT_IN_CATALOG.resolve(self._message_key, DEFAULT_LOCALE, self._details)
        return resolved if resolved is not None else describe(self._message_key, self._details)

    def with_code(self, code: str) -> "Error":
        """换错误码，保留键、文案、参数与内部错误。"""
        validate_error_code(code)
        return self._create(code, self._message_key, self._text, self._details, self._inner)

    def with_text(self, text: str) -> "Error":
        """填入已解析的文案；文案为空或只有空白（RFC-0001「空白字符」）抛出 ``FrameworkError``。"""
        if is_blank(text):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, "The resolved text of an error must not be empty or whitespace."
            )
        return self._create(self._code, self._message_key, text, self._details, self._inner)

    def to_json_value(self) -> dict[str, JsonValue]:
        """线上编码（RFC-0001）：``message`` 是发送端已解析的文案，``messageKey`` 让接收端可再翻译。

        字段顺序是 ``code``、``message``、``messageKey``、``details``、``inner``；没有的省略，细节的键按字典序。
        """
        from xihan_framework._codec import encode

        wire: dict[str, JsonValue] = {"code": self._code, "message": self.message}
        if self._message_key is not None:
            wire["messageKey"] = self._message_key
        if self._details:
            wire["details"] = encode(self._details)
        if self._inner is not None:
            wire["inner"] = self._inner.to_json_value()
        return wire

    def to_json(self) -> str:
        """线上编码的紧凑 JSON 文本。

        写出是严格的（RFC-0001「写出与转交」，ADR-0320）：文案、细节或内部错误里的字符串有代理项码点，或从错误的根算起嵌套
        超过 64 层时写不出，抛出 ``ValueError``。
        """
        return dumps(self.to_json_value(), wire=True)

    @classmethod
    def from_json_value(cls, value: JsonValue) -> "Error":
        """从线上编码还原（RFC-0001「Result」的 Error 规则）；不合规则抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。

        ``code`` 必填且合文法；``message`` 必填，是字符串，不能为空或只有空白（RFC-0001「空白字符」）；``messageKey``、``details``、``inner`` 可选，写了就必须
        分别是合文法的键、JSON 对象与合法的 Error，写成 ``null`` 不合法；未知成员忽略。
        """
        if not isinstance(value, Mapping):
            raise invalid_argument("An error on the wire must be a JSON object.")
        code = value.get("code")
        if not isinstance(code, str) or not is_error_code(code):
            raise FrameworkError(
                ERROR_CODE_INVALID_ARGUMENT, f"The error code '{code}' must match <DOMAIN>_<KIND>_<NNN>."
            )
        message = value.get("message")
        # 只有空白的 message 与空串一样不合法（ADR-0245）：Error 的文案至少要有一个空白之外的字符
        if not isinstance(message, str) or is_blank(message):
            raise invalid_argument("An error on the wire needs a string 'message' that is not empty or whitespace.")
        message_key: str | None = None
        if "messageKey" in value:
            candidate = value["messageKey"]
            if not isinstance(candidate, str) or not is_message_key(candidate):
                raise invalid_argument(
                    f"The 'messageKey' {candidate!r} of an error on the wire is not a valid message key."
                )
            message_key = candidate
        details: Mapping[str, JsonValue] | None = None
        if "details" in value:
            candidate_details = value["details"]
            if not isinstance(candidate_details, Mapping):
                raise invalid_argument("The 'details' of an error on the wire must be a JSON object.")
            details = candidate_details
        inner: Error | None = None
        if "inner" in value:
            # 可选成员缺失表示没有；写成 null 不合法，交给递归的对象判定拒绝
            inner = cls.from_json_value(value["inner"])
        return cls._create(code, message_key, message, _freeze_details(details), inner)

    @classmethod
    def from_json(cls, text: str | bytes) -> "Error":
        """从线上编码的 JSON 文本还原。"""
        try:
            value = parse(text)
        except ValueError as error:
            raise invalid_argument("The error is not valid JSON.") from error
        return cls.from_json_value(value)

    def __str__(self) -> str:
        """码 + 消息。"""
        return f"{self._code}: {self.message}"

    def __repr__(self) -> str:
        """便于调试的形式。"""
        if self._message_key is None:
            return f"Error.literal({self._code!r}, {self._text!r})"
        return f"Error({self._code!r}, {self._message_key!r}, {dict(self._details or {})!r})"

    def __eq__(self, other: object) -> bool:
        """按码、消息键、文案、细节与内部错误比较。"""
        if not isinstance(other, Error):
            return NotImplemented
        return (
            self._code == other._code
            and self._message_key == other._message_key
            and self._text == other._text
            and self._details == other._details
            and self._inner == other._inner
        )

    def __hash__(self) -> int:
        """按码、消息键与文案散列；细节可能不可散列，不参与。"""
        return hash((self._code, self._message_key, self._text))

    @classmethod
    def _of_kind(
        cls,
        kind: str,
        code: str,
        message_key: str,
        details: Mapping[str, JsonValue] | None,
        inner: "Error | None",
    ) -> Self:
        """创建错误并核对 KIND 段与工厂一致。"""
        error = cls(code, message_key, details, inner)
        if error.kind != kind:
            raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, f"The error code '{code}' must have the kind {kind}.")
        return error

    @classmethod
    def _create(
        cls,
        code: str,
        message_key: str | None,
        text: str | None,
        details: Mapping[str, JsonValue] | None,
        inner: "Error | None",
    ) -> Self:
        """不经校验地组装：调用方已经校验过各部分。"""
        error = cls.__new__(cls)
        error._code = code
        error._message_key = message_key
        error._text = text
        error._details = details
        error._inner = inner
        return error


def _freeze_details(details: Mapping[str, JsonValue] | None) -> Mapping[str, JsonValue] | None:
    """校验细节只含 JSON 可表示的值，复制成只读映射。"""
    if details is None:
        return None
    for name, value in details.items():
        _check_json_value(value, name)
    return MappingProxyType(dict(details))


def _check_json_value(value: object, path: str) -> None:
    """细节值必须是值模型里的值：``None``、布尔、整数、浮点、字符串、数组与键为字符串的对象。

    非有限的浮点可以放进细节，写出时是字符串 ``NaN``、``Infinity``、``-Infinity``，与模板里的文本相同（RFC-0001「Result」，
    ADR-0236）；超出 64 位的整数写出时按浮点。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, Mapping):
        for name, member in value.items():
            if not isinstance(name, str):
                raise FrameworkError(
                    ERROR_CODE_INVALID_ARGUMENT, f"The keys of the error detail '{path}' must be strings, got {name!r}."
                )
            _check_json_value(member, f"{path}.{name}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, element in enumerate(value):
            _check_json_value(element, f"{path}[{index}]")
        return
    raise FrameworkError(
        ERROR_CODE_INVALID_ARGUMENT,
        f"The error detail '{path}' must be a JSON value, got a value of type '{type(value).__name__}'.",
    )
