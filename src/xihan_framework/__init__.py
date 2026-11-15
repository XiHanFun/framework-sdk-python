"""XiHan Framework SDK for Python。

XiHan Framework Spec 的 Python 实现：原语层 ``primitives``、清单 ``manifest``、调用上下文 ``invocation``、
管道 ``pipeline`` 与操作 ``operation``。规范见 https://github.com/XiHanFun/framework-spec，
参考实现是 .NET（https://github.com/XiHanFun/XiHan.Framework）。
"""

__all__ = ["SPEC_VERSION", "__version__"]

#: 本包的版本；构建后端从这里读取，``primitives.SDK_VERSION`` 也取它。
__version__ = "0.1.0"

#: 本 SDK 实现的原语层大版本（RFC-0001）。
SPEC_VERSION = "1"
