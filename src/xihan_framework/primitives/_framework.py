"""框架信息与配置错误（RFC-0001 FrameworkInfo、FrameworkException）。"""

from typing import TYPE_CHECKING, Final

from xihan_framework import SPEC_VERSION as _SPEC_VERSION
from xihan_framework import __version__ as _version

if TYPE_CHECKING:
    from xihan_framework.primitives._error import Error

#: 框架名。
NAME: Final = "XiHan Framework"
#: 本 SDK 实现的原语层大版本。
SPEC_VERSION: Final = _SPEC_VERSION
#: 握手时上报的 SDK 名。
SDK_NAME: Final = "framework-sdk-python"
#: 本 SDK 的版本。
SDK_VERSION: Final = _version
#: SDK 仓库地址。
REPOSITORY: Final = "https://github.com/XiHanFun/framework-sdk-python"
#: 文档地址。
DOCUMENTATION: Final = "https://framework.docs.xihanfun.com"


class FrameworkError(ValueError):
    """配置或编程错误（RFC-0001 FrameworkException）：带错误码，消息是英文、不本地化。

    可恢复的业务失败用 ``Error`` 与 ``Result``，不用它。它是 ``ValueError`` 的子类：非法参数与配置错误
    既能按 ``ValueError`` 捕获，也能按 ``code`` 区分。底层原因用 ``raise ... from cause`` 链接。
    """

    def __init__(self, code: str, message: str) -> None:
        """用错误码与面向开发者的英文说明创建。"""
        super().__init__(code, message)
        self._code = code
        self._message = message
        self._error: Error | None = None

    @classmethod
    def of(cls, error: "Error") -> "FrameworkError":
        """用一个带消息键与参数的错误创建：码与消息取自它，``to_error`` 原样交回它，键与参数不丢。

        配置期的内核错误（如 ``KERNEL_OPERATION_001``）以异常交给调用方时用它，码、键与参数与 kernel-messages 向量一致。
        """
        raised = cls(error.code, error.message)
        raised._error = error
        return raised

    @property
    def code(self) -> str:
        """错误码。"""
        return self._code

    @property
    def message(self) -> str:
        """面向开发者的英文说明。"""
        return self._message

    def __str__(self) -> str:
        """码 + 说明，有底层原因时附在括号里。"""
        text = f"{self._code}: {self._message}"
        if self.__cause__ is not None:
            text += f" ({self.__cause__})"
        return text

    def to_error(self) -> "Error":
        """转成可上线的 ``Error``：经 ``of`` 创建的交回原来的错误；否则是 ``Error.literal``（带已解析的文案），错误码不合法时取 ``PRIMITIVES_VALIDATION_001``。"""
        if self._error is not None:
            return self._error
        from xihan_framework.primitives._error import Error
        from xihan_framework.primitives._error_code import ERROR_CODE_INVALID_ARGUMENT, is_error_code

        code = self._code if is_error_code(self._code) else ERROR_CODE_INVALID_ARGUMENT
        return Error.literal(code, self._message)
