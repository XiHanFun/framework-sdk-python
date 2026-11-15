"""已登记的 Baggage 键（conventions/baggage-keys.md）。"""

import re
from typing import Final

#: 当前租户 ID。
BAGGAGE_TENANT: Final = "xihan.tenant"
#: 当前主体 ID。
BAGGAGE_PRINCIPAL: Final = "xihan.principal"
#: 业务关联 ID。
BAGGAGE_CORRELATION: Final = "xihan.correlation"
#: 幂等键。
BAGGAGE_IDEMPOTENCY_KEY: Final = "xihan.idempotency-key"
#: 请求文化（BCP 47）。
BAGGAGE_LOCALE: Final = "xihan.locale"

#: 已登记的键，按登记顺序。
REGISTERED_BAGGAGE_KEYS: Final = (
    BAGGAGE_TENANT,
    BAGGAGE_PRINCIPAL,
    BAGGAGE_CORRELATION,
    BAGGAGE_IDEMPOTENCY_KEY,
    BAGGAGE_LOCALE,
)

# 键格式 xihan.<name>，小写，连字符分隔
_BAGGAGE_KEY_PATTERN = re.compile(r"xihan\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def is_baggage_key_well_formed(key: str) -> bool:
    """判断键是否符合 ``xihan.<name>``。"""
    return _BAGGAGE_KEY_PATTERN.fullmatch(key) is not None


def is_baggage_key_registered(key: str) -> bool:
    """判断键是否已登记。"""
    return key in REGISTERED_BAGGAGE_KEYS


#: 已登记键的值按 UTF-16 码元计的最大长度（conventions/baggage-keys.md，ADR-0204、ADR-0233）。
MAX_BAGGAGE_VALUE_LENGTH: Final = 256


def is_baggage_value_acceptable(value: str) -> bool:
    """判断已登记键的值能否进入调用上下文：非空、按 UTF-16 码元计不超过 256、不含 U+0000–U+001F 与 U+007F–U+009F。

    长度与 .NET、JavaScript、Java 的字符串长度一致：基本多文种平面以外的字符算两个码元。``xihan.idempotency-key``
    不按这条丢弃，由 Idempotency 阶段以校验错误拒绝。
    """
    if not value:
        return False
    units = 0
    for character in value:
        code = ord(character)
        # 控制字符同 .NET 的 char.IsControl：换行能伪造日志行，这些值会写进日志、审计与消息信封
        if code <= 0x1F or 0x7F <= code <= 0x9F:
            return False
        units += 2 if code > 0xFFFF else 1
    return units <= MAX_BAGGAGE_VALUE_LENGTH
