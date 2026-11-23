"""操作 ID 文法（RFC-0011「操作 ID」、conventions/operation-ids.md）。

操作契约（``operation.is_id``）与清单的 ``contributes.operations``（RFC-0002）是同一个文法，放在两边都依赖的原语层，
只写这一处；不是公开名。
"""

import re

# <resource>.<verb>：两段都是连字符分隔的小写词，连字符只在字母数字之间，不开头、不结尾、不连写
_OPERATION_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def is_operation_id(id: str) -> bool:
    """判断操作 ID 是否符合 ``<resource>.<verb>``，按整串匹配（Python 的 ``$`` 会放过结尾的换行）。"""
    return _OPERATION_ID_PATTERN.fullmatch(id) is not None
