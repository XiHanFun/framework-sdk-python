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
