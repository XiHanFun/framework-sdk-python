"""管道的十一个阶段（RFC-0004）。"""

from xihan_framework.primitives._named_enum import NamedEnum


class PipelineStage(NamedEnum):
    """管道的十一个阶段（RFC-0004）：名称与顺序是规范性内容，数值即执行顺序，按定义顺序迭代。

    ``str()``、``parse``、``to_json_value`` 与 ``from_json_value`` 都按清单里的小写阶段名（``handle``）。
    """

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

    @property
    def manifest_name(self) -> str:
        """清单里的阶段名，如 ``handle``。"""
        return self.name.lower()
