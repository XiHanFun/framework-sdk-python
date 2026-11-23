"""错误码文法与原语层登记的错误码（conventions/error-codes.md）。"""

import re
from typing import Final

from xihan_framework.primitives._framework import FrameworkError

#: 原语参数非法（空 Code、空消息键、清单跨字段约束等）。
ERROR_CODE_INVALID_ARGUMENT: Final = "PRIMITIVES_VALIDATION_001"
#: 使用了未初始化的 Result。Python 的 ``Result`` 只能经 ``ok`` / ``fail`` / ``success`` 创建，没有零值，所以不会产生它。
ERROR_CODE_UNINITIALIZED_RESULT: Final = "PRIMITIVES_VALIDATION_002"

# 错误码的 KIND 段；领域可以自定义其他 KIND
KIND_VALIDATION: Final = "VALIDATION"
KIND_NOT_FOUND: Final = "NOTFOUND"
KIND_CONFLICT: Final = "CONFLICT"
KIND_DENIED: Final = "DENIED"
KIND_UNAVAILABLE: Final = "UNAVAILABLE"
KIND_TIMEOUT: Final = "TIMEOUT"
KIND_CANCELLED: Final = "CANCELLED"
KIND_UNEXPECTED: Final = "UNEXPECTED"

# <领域大写>_<KIND>_<NNN>；一律 fullmatch，Python 的 $ 会放过结尾的换行
_ERROR_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9]*_[A-Z][A-Z0-9]*_[0-9]{3}")


def is_error_code(code: str) -> bool:
    """判断错误码是否符合 ``<领域大写>_<KIND>_<NNN>``。"""
    return _ERROR_CODE_PATTERN.fullmatch(code) is not None


def validate_error_code(code: str) -> str:
    """校验错误码并原样返回；不合法抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。"""
    if not is_error_code(code):
        raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, f"The error code '{code}' must match <DOMAIN>_<KIND>_<NNN>.")
    return code


def invalid_argument(message: str) -> FrameworkError:
    """参数、配置或线上编码不合法的错误（``PRIMITIVES_VALIDATION_001``），``message`` 是完整的英文句子。

    原语的线上解码、清单的字段校验与插件运行时的声明核对共用这一个工厂；只在本包里用，不是公开名。
    """
    return FrameworkError(ERROR_CODE_INVALID_ARGUMENT, message)


def error_code_kind(code: str) -> str | None:
    """返回错误码的 KIND 段；不合法时为 ``None``。"""
    return code.split("_")[1] if is_error_code(code) else None


def error_code_domain(code: str) -> str | None:
    """返回错误码的领域段；不合法时为 ``None``。"""
    return code.split("_")[0] if is_error_code(code) else None
