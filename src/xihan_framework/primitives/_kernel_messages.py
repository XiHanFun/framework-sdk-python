"""SDK 自带的默认英文模板：SDK 与宿主共用的内核错误的消息键与 en-US 模板（RFC-0011「错误码」，ADR-0233）。

模板与 ``spec/conformance/vectors/primitives/kernel-messages.json`` 逐字相同（取自 .NET 的 ``Localization/en-US.json``）；
宿主收到插件的错误后按键用自己的目录再翻译。
"""

from typing import Final

from xihan_framework.primitives._messages import DEFAULT_LOCALE, Catalog

#: 读取了未初始化的结果（``PRIMITIVES_VALIDATION_002``）；Python 的 ``Result`` 没有零值，不会产生它。
MESSAGE_UNINITIALIZED_RESULT: Final = "primitives.result.uninitialized"

#: SDK 自带的默认英文模板；``Error.message`` 在进程默认的解析器解析不了时用它，再解析不了才退回描述。
BUILT_IN_CATALOG: Final = Catalog(
    {
        DEFAULT_LOCALE: {
            MESSAGE_UNINITIALIZED_RESULT: "The result is uninitialized: a default value is not a valid result.",
            "kernel.invocation.deadline_exceeded": "The invocation exceeded its deadline.",
            "kernel.invocation.deadline_before_start": "The invocation exceeded its deadline before it started.",
            "kernel.invocation.cancelled": "The invocation was canceled.",
            # 规范向量还没有列出这个键（RFC-0011 规则 5 要求 SDK 翻译未处理的异常）；模板取自 .NET 的 en-US.json
            "kernel.invocation.unhandled_exception": "An unhandled exception occurred during the invocation.",
            "kernel.operation.unknown": "Operation '{id}' is not registered.",
            "kernel.operation.invalid_payload": "The payload of operation '{id}' is not valid: {reason}",
            "kernel.operation.type_mismatch": "Operation '{id}' handles '{expected}', but '{actual}' was supplied.",
            "kernel.operation.no_response": "Operation '{id}' completed without producing a response.",
            "kernel.operation.duplicate": "Operation '{id}' is already registered by '{owner}'.",
            "kernel.operation.registry_full": "The operation registry has reached its capacity of {capacity}.",
        }
    }
)
