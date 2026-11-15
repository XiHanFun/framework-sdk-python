"""五级稳定性（RFC-0001 Stability）。"""

from enum import Enum
from typing import Final

from xihan_framework._json import JsonValue


class Stability(Enum):
    """五级稳定性（RFC-0001）：数值与 .NET 枚举一致，清单与协议里写小写名。"""

    STABLE = 0
    PREVIEW = 1
    EXPERIMENTAL = 2
    INTERNAL = 3
    DEPRECATED = 4

    @classmethod
    def parse(cls, name: str) -> "Stability | None":
        """按小写名（``stable``）解析；不认识时为 ``None``。"""
        return _STABILITIES_BY_NAME.get(name)

    def to_json_value(self) -> JsonValue:
        """线上编码：小写名。"""
        return str(self)

    @classmethod
    def from_json_value(cls, value: JsonValue) -> "Stability":
        """从小写名还原；不认识时抛出 ``ValueError``。"""
        stability = cls.parse(value) if isinstance(value, str) else None
        if stability is None:
            raise ValueError(f"'{value}' is not a stability.")
        return stability

    def __str__(self) -> str:
        """小写名，如 ``stable``。"""
        return self.name.lower()


_STABILITIES_BY_NAME: Final = {str(stability): stability for stability in Stability}
