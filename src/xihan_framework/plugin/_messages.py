"""插件运行时的错误码、消息键与消息目录（RFC-0017「错误码」、RFC-0010）。"""

import os
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

from xihan_framework._json import JsonTextError, parse
from xihan_framework.primitives import DEFAULT_LOCALE, ERROR_CODE_INVALID_ARGUMENT, Catalog, FrameworkError

#: 插件未就绪、正在停止或不可达；回调时宿主不可达。
ERROR_CODE_UNAVAILABLE: Final = "PLUGINS_UNAVAILABLE_001"
#: 回调被拒：会话令牌无效、权限未声明或未授予、租户不是宿主随调用交给插件的租户。
ERROR_CODE_CALLBACK_DENIED: Final = "PLUGINS_DENIED_002"
#: 配置、启动、停止或回调时发生未预期的失败。
ERROR_CODE_UNEXPECTED: Final = "PLUGINS_UNEXPECTED_001"

#: 插件不可用的消息键（参数 ``id``、``state``）。
MESSAGE_UNAVAILABLE: Final = "plugins.process.unavailable"
#: 某一步失败的消息键（参数 ``id``、``phase``、``reason``）。
MESSAGE_FAILED: Final = "plugins.process.failed"
#: 回调需要的权限未在清单声明或未被授予的消息键（参数 ``id``、``permission``）。
MESSAGE_CALLBACK_DENIED: Final = "plugins.callback.denied"
#: 回调或发布带的租户不是宿主随调用交给插件的租户的消息键（参数 ``id``、``tenant``）。
MESSAGE_CALLBACK_TENANT_DENIED: Final = "plugins.callback.tenant_denied"
#: 回调的会话令牌缺失或无效的消息键（没有参数）。
MESSAGE_SESSION_INVALID: Final = "plugins.callback.session_invalid"

#: 运行时消息目录的名字；与宿主为同一组键登记的目录同名，宿主已有同名目录时用它自己的。
CATALOG_NAME: Final = "plugins.protocol"
#: 内置的 10 种区域（RFC-0010），``en-US`` 是默认区域。
LOCALES: Final = ("en-US", "zh-CN", "zh-TW", "ja-JP", "ko-KR", "de-DE", "es-ES", "fr-FR", "pt-BR", "ru-RU")


def load_catalog(directory: Traversable | str | os.PathLike[str]) -> Catalog:
    """从目录读消息目录：每种区域一个 ``<区域>.json``，内容是键 → 模板的 JSON 对象。

    文本按 XiHan 的共同 JSON 读取检查读取（重复的键、BOM 都不合法）；不是 JSON 对象、模板不是字符串、键不合文法或
    没有 ``en-US.json`` 时抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。其他文件忽略。
    """
    root: Traversable = Path(directory) if isinstance(directory, (str, os.PathLike)) else directory
    locales: dict[str, dict[str, str]] = {}
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        try:
            value = parse(entry.read_bytes())
        except JsonTextError as error:
            raise _invalid(f"The message catalog file '{entry.name}' is not valid JSON.") from error
        if not isinstance(value, dict) or not all(isinstance(template, str) for template in value.values()):
            raise _invalid(f"The message catalog file '{entry.name}' must be a JSON object of strings.")
        locales[entry.name.removesuffix(".json")] = {key: str(template) for key, template in value.items()}
    if DEFAULT_LOCALE not in locales:
        raise _invalid(f"The message catalog directory '{root.name}' has no {DEFAULT_LOCALE}.json.")
    return Catalog(locales)


def _invalid(message: str) -> FrameworkError:
    """消息目录不合法的配置错误。"""
    return FrameworkError(ERROR_CODE_INVALID_ARGUMENT, message)


#: 运行时自己的消息目录（10 种区域）：错误的 ``text`` 在进程默认的解析器解析不了时用它的 ``en-US``；
#: 宿主按消息键用自己的目录翻译，要随握手交给宿主时把它放进 ``Plugin`` 的 ``catalogs``。
CATALOG: Final = load_catalog(files("xihan_framework.plugin") / "Localization")
