"""管道的十一个阶段（RFC-0004）。"""

from enum import Enum
from typing import Final

from xihan_framework._json import JsonValue


class PipelineStage(Enum):
    """管道的十一个阶段（RFC-0004）：名称与顺序是规范性内容，数值即执行顺序，按定义顺序迭代。"""

    INGRESS = 0
    OBSERVE = 1
    RESILIENCE = 2
    AUTHENTICATE = 3
    TENANT = 4
    AUTHORIZE = 5
    VALIDATE = 6
    IDEMPOTENCY = 7
    TRANSACTION = 8
    HANDLE = 9
    EGRESS = 10

    @classmethod
    def parse(cls, name: str) -> "PipelineStage | None":
        """按清单里的小写阶段名（``handle``）解析；不认识时为 ``None``。"""
        return _STAGES_BY_NAME.get(name)

    @property
    def manifest_name(self) -> str:
        """清单里的阶段名，如 ``handle``。"""
        return self.name.lower()

    def to_json_value(self) -> JsonValue:
        """线上编码：清单里的阶段名。"""
        return self.manifest_name

    @classmethod
    def from_json_value(cls, value: JsonValue) -> "PipelineStage":
        """从清单里的阶段名还原；不认识时抛出 ``ValueError``。"""
        stage = cls.parse(value) if isinstance(value, str) else None
        if stage is None:
            raise ValueError(f"'{value}' is not a pipeline stage.")
        return stage

    def __str__(self) -> str:
        """清单里的阶段名。"""
        return self.manifest_name


_STAGES_BY_NAME: Final = {stage.manifest_name: stage for stage in PipelineStage}
