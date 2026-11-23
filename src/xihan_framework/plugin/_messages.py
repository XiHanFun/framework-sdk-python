"""插件运行时的错误码、消息键与消息目录（RFC-0017「错误码」、invocation.md §8、RFC-0010）。"""

import os
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

from xihan_framework._json import JsonTextError, parse
from xihan_framework.primitives import DEFAULT_LOCALE, Catalog, is_locale_name, validate_locale_name
from xihan_framework.primitives._error_code import invalid_argument

#: 插件不在可服务状态；回调时连不上宿主或没有可用的回调端点。
ERROR_CODE_UNAVAILABLE: Final = "PLUGINS_UNAVAILABLE_001"
#: 回调被拒：会话令牌无效、权限未声明或未授予、租户不是宿主随调用交给插件的租户。
ERROR_CODE_CALLBACK_DENIED: Final = "PLUGINS_DENIED_002"
#: 生命周期或健康回调抛出、配置节不是 JSON 对象、宿主写来的错误不合法、回调的 gRPC 状态没有对应的错误。
ERROR_CODE_UNEXPECTED: Final = "PLUGINS_UNEXPECTED_001"

# 运行时与宿主共用的消息键（invocation.md §8，向量 plugin/plugin-messages.json）；错误的 details 就是这些参数，不另加成员
#: 插件不在可服务状态的消息键（参数 ``id``、``state``，``state`` 是状态名），码 ``PLUGINS_UNAVAILABLE_001``。
MESSAGE_UNAVAILABLE: Final = "plugins.process.unavailable"
#: 插件一侧失败的消息键（参数 ``id``、``phase``、``reason``，``phase`` 小写），码 ``PLUGINS_UNEXPECTED_001``。
MESSAGE_FAILED: Final = "plugins.process.failed"
#: 投递找不到订阅的消息键（参数 ``id``、``topic``、``type``、``mode``），码 ``KERNEL_NOTFOUND_001``。
MESSAGE_EVENT_UNSUBSCRIBED: Final = "plugins.event.unsubscribed"
#: 回调连不上宿主或没有可用回调端点的消息键（参数 ``id``、``reason``），码 ``PLUGINS_UNAVAILABLE_001``。
MESSAGE_HOST_UNAVAILABLE: Final = "plugins.host.unavailable"
#: 回调所需的权限未在清单声明或未被授予的消息键（参数 ``id``、``permission``），码 ``PLUGINS_DENIED_002``。
MESSAGE_CALLBACK_DENIED: Final = "plugins.callback.denied"
#: 回调带的租户不是宿主随调用交给插件的租户的消息键（参数 ``id``、``tenant``），码 ``PLUGINS_DENIED_002``。
MESSAGE_CALLBACK_TENANT_DENIED: Final = "plugins.callback.tenant_denied"
#: 宿主拒绝了回调的会话令牌的消息键（没有参数），码 ``PLUGINS_DENIED_002``。
MESSAGE_SESSION_INVALID: Final = "plugins.callback.session_invalid"

#: 运行时消息目录的名字（与宿主为同一组键登记的目录同名）；它不随握手发送，作者的目录不能用这个名字。
CATALOG_NAME: Final = "plugins.protocol"
#: 内置的 10 种区域（RFC-0010），``en-US`` 是默认区域。
LOCALES: Final = ("en-US", "zh-CN", "zh-TW", "ja-JP", "ko-KR", "de-DE", "es-ES", "fr-FR", "pt-BR", "ru-RU")


def load_catalog(directory: Traversable | str | os.PathLike[str]) -> Catalog:
    """从目录读消息目录：每种区域一个 ``<区域>.json``，内容是键 → 模板的 JSON 对象。

    文件名（去掉 ``.json``）是区域名（RFC-0010「区域」），不区分大小写，按规范形式归到目录里（``zh-cn.json`` 即 ``zh-CN``）。
    文本按 XiHan 的共同 JSON 读取检查读取（重复的键、BOM 都不合法）；文件名不是区域名、两个文件名只差大小写、不是 JSON
    对象、模板不是字符串、键不合文法或没有 ``en-US.json`` 时抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）。
    不以 ``.json`` 结尾的文件忽略。
    """
    root: Traversable = Path(directory) if isinstance(directory, (str, os.PathLike)) else directory
    locales: dict[str, dict[str, str]] = {}
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        name = entry.name.removesuffix(".json")
        if not is_locale_name(name):
            raise invalid_argument(
                f"The message catalog file '{entry.name}' must be named after a locale such as 'en-US'."
            )
        # 区分大小写的文件系统上 en-US.json 与 en-us.json 可以并存，它们是同一个区域
        locale = validate_locale_name(name)
        if locale in locales:
            raise invalid_argument(
                f"The message catalog directory '{root.name}' has more than one file for the locale '{locale}'."
            )
        try:
            value = parse(entry.read_bytes())
        except JsonTextError as error:
            raise invalid_argument(f"The message catalog file '{entry.name}' is not valid JSON.") from error
        if not isinstance(value, dict) or not all(isinstance(template, str) for template in value.values()):
            raise invalid_argument(f"The message catalog file '{entry.name}' must be a JSON object of strings.")
        locales[locale] = {key: str(template) for key, template in value.items()}
    if DEFAULT_LOCALE not in locales:
        raise invalid_argument(f"The message catalog directory '{root.name}' has no {DEFAULT_LOCALE}.json.")
    return Catalog(locales)


#: 运行时自己的消息目录（参考实现的 10 种区域），供本地渲染（如写日志）。宿主有同样的目录，按消息键把错误翻译成
#: 调用方的语言，所以它不随握手发送（handshake.md §3 第 6 条、invocation.md §8）。
CATALOG: Final = load_catalog(files("xihan_framework.plugin") / "Localization")
