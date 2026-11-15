"""``xihan.manifest.json`` 的模型（RFC-0002，Schema 见 ``spec/manifest/manifest.schema.json``）。

只做数据与格式校验；收集、解析与求解是宿主的职责。``read`` / ``read_file`` 解析并校验，``write`` 输出与 Go SDK
逐字节相同的规范化 JSON；``Manifest`` 是冻结的 dataclass，构造时即校验，不存在不合法的实例。
"""

from xihan_framework.manifest._manifest import (
    FILE_NAME,
    ON_STARTUP,
    Binding,
    CapabilityDeclaration,
    CapabilityRequirement,
    ConfigContribution,
    Contributions,
    Isolation,
    Kind,
    Manifest,
    PipelineContribution,
    Role,
    domain,
    is_capability_id,
    is_manifest_id,
    read,
    read_file,
    write,
)

__all__ = [
    "FILE_NAME",
    "ON_STARTUP",
    "Binding",
    "CapabilityDeclaration",
    "CapabilityRequirement",
    "ConfigContribution",
    "Contributions",
    "Isolation",
    "Kind",
    "Manifest",
    "PipelineContribution",
    "Role",
    "domain",
    "is_capability_id",
    "is_manifest_id",
    "read",
    "read_file",
    "write",
]
