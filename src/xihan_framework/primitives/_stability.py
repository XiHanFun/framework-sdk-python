"""五级稳定性（RFC-0001 Stability）。"""

from xihan_framework.primitives._named_enum import NamedEnum


class Stability(NamedEnum):
    """五级稳定性（RFC-0001）：数值按 RFC-0001 列出的顺序，清单与协议里写小写名（``stable``）。

    ``str()``、``parse``、``to_json_value`` 与 ``from_json_value`` 都按小写名。
    """

    STABLE = 0
    PREVIEW = 1
    EXPERIMENTAL = 2
    INTERNAL = 3
    DEPRECATED = 4
