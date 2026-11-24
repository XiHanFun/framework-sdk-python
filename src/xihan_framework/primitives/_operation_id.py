"""操作 ID 文法（RFC-0011「操作 ID」、conventions/operation-ids.md）。

操作契约（``operation.is_id``）与清单的 ``contributes.operations``（RFC-0002）是同一条规则，放在两边都依赖的原语层，
只写这一处；不是公开名，上限公开为 ``operation.MAX_ID_LENGTH``。
"""

import re
from typing import Final

# <resource>.<verb>：两段都是连字符分隔的小写词，连字符只在字母数字之间，不开头、不结尾、不连写
_OPERATION_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

#: 操作 ID 的最大长度（conventions/operation-ids.md，ADR-0276；与 .NET 的 ``OperationId.MaxLength`` 相同）。
#: 文法只含 ASCII 字符，字符数就是 UTF-8 字节数。
MAX_OPERATION_ID_LENGTH: Final = 128


def is_operation_id(id: str) -> bool:
    """判断操作 ID 是否符合 ``<resource>.<verb>`` 且不超过 128 个字符，按整串匹配（Python 的 ``$`` 会放过结尾的换行）。

    超过上限的 ID 别处都合文法也不合法；向量 ``primitives/operation-ids.json``。
    """
    # 先看长度：超长的输入不必交给正则
    return len(id) <= MAX_OPERATION_ID_LENGTH and _OPERATION_ID_PATTERN.fullmatch(id) is not None
