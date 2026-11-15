"""``xihan.manifest.json`` 的模型、读取、规范化输出与校验（RFC-0002）。"""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from xihan_framework._json import JsonValue, dumps, loads
from xihan_framework.primitives import ERROR_CODE_INVALID_ARGUMENT, FrameworkError, PipelineStage, Stability

#: 清单文件名。
FILE_NAME: Final = "xihan.manifest.json"

# 文法与 spec/manifest/manifest.schema.json 一致；一律 fullmatch，Python 的 $ 会放过结尾的换行
_MANIFEST_ID = re.compile(r"xihan\.[a-z][a-z0-9]*\.[a-z][a-z0-9-]*")
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9]*\.[a-z][a-z0-9-]*")
_SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_VERSION = re.compile(r"[1-9][0-9]*")
_RANGE = re.compile(r"\^?([1-9][0-9]*)")
_REQUIREMENT = re.compile(r"([a-z][a-z0-9]*\.[a-z][a-z0-9-]*)@\^?([1-9][0-9]*)(\??)")
_DECLARATION = re.compile(r"([a-z][a-z0-9]*\.[a-z][a-z0-9-]*)@([1-9][0-9]*)")
_ACTIVATION = re.compile(r"onStartup|onOperation:[a-z0-9.*-]+|onEvent:[a-z0-9.*-]+")
_PERMISSION = re.compile(r"(?:net\.outbound|fs\.read|fs\.write|capability):.+")
_BINDING_NAME = re.compile(r"[a-z][a-z0-9-]*")
_BINDING_TYPE = re.compile(r"[a-z]+(?::[a-z]+)?")
_OPERATION = re.compile(r"[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*")
_SECTION = re.compile(r"XiHan:[A-Z][A-Za-z0-9]*(?::[A-Z][A-Za-z0-9]*)*")
_HEALTH = re.compile(r"[a-z][a-z0-9.-]*")
_PERMISSION_CODE = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+")

#: 缺省的激活事件。
ON_STARTUP: Final = "onStartup"


class Kind(StrEnum):
    """清单种类。"""

    CAPABILITY = "capability"
    PROVIDER = "provider"
    PLUGIN = "plugin"
    HOST = "host"
    PROTOCOL = "protocol"


class Role(StrEnum):
    """Provider 角色：``default`` 在没有显式 Provider 时被选中。"""

    PROVIDER = "provider"
    DEFAULT = "default"


class Isolation(StrEnum):
    """插件隔离级别。"""

    IN_PROCESS = "in-process"
    WASM = "wasm"
    OUT_OF_PROCESS = "out-of-process"


def is_manifest_id(id: str) -> bool:
    """判断清单 ID 是否符合 ``xihan.<领域>.<名称>``。"""
    return _MANIFEST_ID.fullmatch(id) is not None


def is_capability_id(id: str) -> bool:
    """判断能力 ID 是否符合 ``<领域>.<能力>``。"""
    return _CAPABILITY_ID.fullmatch(id) is not None


def domain(id: str) -> str | None:
    """返回清单 ID 或能力 ID 的领域段；都不合法时为 ``None``。"""
    if is_manifest_id(id):
        return id.split(".")[1]
    if is_capability_id(id):
        return id.split(".")[0]
    return None


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    """提供的能力：``id@version``，版本是整数接口版本。"""

    id: str
    version: int

    @classmethod
    def parse(cls, text: str) -> "CapabilityDeclaration | None":
        """解析 ``id@version``；不合文法时为 ``None``。"""
        match = _DECLARATION.fullmatch(text)
        return None if match is None else cls(match.group(1), int(match.group(2)))

    def __str__(self) -> str:
        """``id@version``。"""
        return f"{self.id}@{self.version}"


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """依赖的能力：``id@[^]version[?]``；``^N`` 与 ``N`` 等价，``?`` 是可选依赖。"""

    id: str
    version: int
    optional: bool = False

    @classmethod
    def parse(cls, text: str) -> "CapabilityRequirement | None":
        """解析 ``id@[^]version[?]``；不合文法时为 ``None``。"""
        match = _REQUIREMENT.fullmatch(text)
        return None if match is None else cls(match.group(1), int(match.group(2)), match.group(3) == "?")

    def is_satisfied_by(self, declaration: CapabilityDeclaration) -> bool:
        """判断一个声明是否满足本依赖：同一能力且同一接口版本。"""
        return self.id == declaration.id and self.version == declaration.version

    def __str__(self) -> str:
        """``id@version``，可选依赖带 ``?``。"""
        return f"{self.id}@{self.version}{'?' if self.optional else ''}"


@dataclass(frozen=True, slots=True)
class Binding:
    """需要宿主注入的资源。"""

    name: str
    type: str
    required: bool | None = None
    """是否必需；``None`` 表示清单没写，按必需处理。"""

    @property
    def is_required(self) -> bool:
        """是否必需；缺省为必需。"""
        return self.required is None or self.required


@dataclass(frozen=True, slots=True)
class PipelineContribution:
    """贡献的一个中间件。"""

    middleware: str
    stage: PipelineStage
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """把序列字段固定成元组。"""
        _freeze_sequences(self)


@dataclass(frozen=True, slots=True)
class ConfigContribution:
    """贡献的配置节：领域节 ``XiHan:<领域>``，可再带一层或多层 PascalCase 子节（RFC-0002「配置节」）。"""

    section: str
    schema: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Contributions:
    """贡献点。

    ``permissions`` 是此包定义的权限码（RFC-0008 权限码文法：小写、点分、至少两段，如 ``orders.read``），不重复；
    插件的由宿主在登记它的代理操作时一并登记（plugin/invocation.md §1）。
    """

    pipeline: tuple[PipelineContribution, ...] = ()
    operations: tuple[str, ...] = ()
    config: ConfigContribution | None = None
    permissions: tuple[str, ...] = ()
    health: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """把序列字段固定成元组。"""
        _freeze_sequences(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class Manifest:
    """一份清单；字段顺序与 Schema 一致，构造时校验格式与跨字段约束。

    校验失败抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``），消息与 Go SDK 一致：插件必须声明隔离，
    非插件不得声明隔离或权限，只有 Wasm 插件可以贡献中间件，提供、依赖、绑定与权限码不得重复。
    """

    schema: str | None = None
    """``$schema``。"""
    id: str
    package: str | None = None
    version: str
    primitives: int
    stability: Stability
    kind: Kind
    role: Role | None = None
    isolation: Isolation | None = None
    provides: tuple[CapabilityDeclaration, ...] = ()
    requires: tuple[CapabilityRequirement, ...] = ()
    bindings: tuple[Binding, ...] = ()
    contributes: Contributions | None = None
    activation: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    entry: str | None = None

    def __post_init__(self) -> None:
        """把序列字段固定成元组，再校验。"""
        _freeze_sequences(self)
        _validate(self)

    @property
    def effective_role(self) -> Role:
        """角色，缺省为 ``provider``。"""
        return Role.PROVIDER if self.role is None else self.role

    @property
    def effective_activation(self) -> tuple[str, ...]:
        """激活事件，缺省为 ``onStartup``。"""
        return self.activation or (ON_STARTUP,)

    @property
    def activates_on_startup(self) -> bool:
        """是否在宿主启动时激活。"""
        return ON_STARTUP in self.effective_activation

    @property
    def operations(self) -> tuple[str, ...]:
        """贡献的操作 ID；没有贡献时为空。"""
        return () if self.contributes is None else self.contributes.operations

    @property
    def health(self) -> tuple[str, ...]:
        """贡献的健康项；没有贡献时为空。"""
        return () if self.contributes is None else self.contributes.health

    @property
    def config_section(self) -> str | None:
        """贡献的配置节；没有时为 ``None``。"""
        if self.contributes is None or self.contributes.config is None:
            return None
        return self.contributes.config.section


def read(data: str | bytes) -> Manifest:
    """解析并校验清单 JSON。

    不是 JSON、未知字段与类型不符抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``，“not valid JSON for the
    schema”，具体原因链在 ``__cause__`` 上）；格式与跨字段约束不合同样抛出，消息指出是哪一项。
    """
    try:
        return _manifest(loads(data))
    except (ValueError, UnicodeDecodeError) as error:
        if isinstance(error, FrameworkError):
            raise
        raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, "The manifest is not valid JSON for the schema.") from error


def read_file(path: str | os.PathLike[str]) -> Manifest:
    """读取并校验清单文件。"""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise FrameworkError(ERROR_CODE_INVALID_ARGUMENT, "The manifest file could not be read.") from error
    return read(data)


def write(manifest: Manifest) -> str:
    """输出规范化的清单 JSON：两空格缩进，字段按 Schema 顺序，缺省值省略；与 Go SDK 的 ``Write`` 逐字节相同。"""
    return dumps(_manifest_json(manifest), indent=True)


def _validate(manifest: Manifest) -> None:
    """校验格式与跨字段约束；检查顺序与 Go SDK 相同，所以同一份清单报同一个错。"""
    id = manifest.id
    if not isinstance(id, str) or not is_manifest_id(id):
        raise _invalid(f"the manifest ID '{id}' must match xihan.<domain>.<name>")
    if not isinstance(manifest.version, str) or _SEMVER.fullmatch(manifest.version) is None:
        raise _invalid(f"the version '{manifest.version}' of '{id}' must be a semantic version")
    if not _is_interface_version(manifest.primitives):
        raise _invalid(f"the primitives version of '{id}' must be a positive integer")
    if not isinstance(manifest.stability, Stability):
        raise _invalid(f"the stability of '{id}' is not valid")
    if not isinstance(manifest.kind, Kind):
        raise _invalid(f"the kind '{manifest.kind}' of '{id}' is not valid")
    if manifest.role is not None and not isinstance(manifest.role, Role):
        raise _invalid(f"the role '{manifest.role}' of '{id}' is not valid")
    if manifest.isolation is not None and not isinstance(manifest.isolation, Isolation):
        raise _invalid(f"the isolation '{manifest.isolation}' of '{id}' is not valid")
    if manifest.kind is Kind.PLUGIN and manifest.isolation is None:
        raise _invalid(f"the plugin '{id}' must declare its isolation")
    if manifest.kind is not Kind.PLUGIN and manifest.isolation is not None:
        raise _invalid(f"'{id}' is not a plugin and must not declare an isolation")
    if manifest.kind is not Kind.PLUGIN and manifest.permissions:
        raise _invalid(f"'{id}' is not a plugin and must not declare permissions")

    # 重复按 ID 判定，比 Schema 的 uniqueItems（整项相同）更严：同一能力不能以两个版本各提供或依赖一次
    seen: set[str] = set()
    for declaration in manifest.provides:
        if not is_capability_id(declaration.id) or not _is_interface_version(declaration.version):
            raise _invalid(f"the provided capability '{declaration.id}' of '{id}' is not valid")
        if declaration.id in seen:
            raise _invalid(f"'{id}' provides '{declaration.id}' more than once")
        seen.add(declaration.id)
    seen = set()
    for requirement in manifest.requires:
        if not is_capability_id(requirement.id) or not _is_interface_version(requirement.version):
            raise _invalid(f"the required capability '{requirement.id}' of '{id}' is not valid")
        if requirement.id in seen:
            raise _invalid(f"'{id}' requires '{requirement.id}' more than once")
        seen.add(requirement.id)
    seen = set()
    for binding in manifest.bindings:
        if _BINDING_NAME.fullmatch(binding.name) is None or _BINDING_TYPE.fullmatch(binding.type) is None:
            raise _invalid(f"the binding '{binding.name}' ({binding.type}) of '{id}' is not valid")
        if binding.name in seen:
            raise _invalid(f"'{id}' declares the binding '{binding.name}' more than once")
        seen.add(binding.name)
    for event in manifest.activation:
        if _ACTIVATION.fullmatch(event) is None:
            raise _invalid(f"the activation event '{event}' of '{id}' is not valid")
    for permission in manifest.permissions:
        if _PERMISSION.fullmatch(permission) is None:
            raise _invalid(f"the permission '{permission}' of '{id}' is not valid")
    if manifest.contributes is not None:
        _validate_contributions(manifest.contributes, id)
        # 只有 Wasm 插件能贡献中间件（RFC-0017「范围」）：进程外与进程内插件的清单写了 contributes.pipeline，
        # .NET 宿主在发现或加载时以 PLUGINS_VALIDATION_001 拒绝；这里读清单时同样拒绝，不让作者以为中间件生效了、等到宿主那里才失败
        pipeline = manifest.contributes.pipeline
        if manifest.kind is Kind.PLUGIN and manifest.isolation is not Isolation.WASM and pipeline:
            names = ", ".join(contribution.middleware for contribution in pipeline)
            raise _invalid(
                f"the plugin '{id}' with isolation '{manifest.isolation}' must not declare 'contributes.pipeline' "
                f"({names}): only wasm plugins can contribute middleware"
            )


def _validate_contributions(contributions: Contributions, id: str) -> None:
    """校验贡献点。"""
    for contribution in contributions.pipeline:
        if not contribution.middleware:
            raise _invalid(f"a pipeline contribution of '{id}' has no middleware name")
        if not isinstance(contribution.stage, PipelineStage):
            raise _invalid(f"the pipeline contribution '{contribution.middleware}' of '{id}' has an unknown stage")
    for operation in contributions.operations:
        if _OPERATION.fullmatch(operation) is None:
            raise _invalid(f"the operation '{operation}' of '{id}' must match <resource>.<verb>")
    config = contributions.config
    if config is not None and _SECTION.fullmatch(config.section) is None:
        raise _invalid(
            f"the configuration section '{config.section}' of '{id}' must match XiHan:<Domain> or "
            "XiHan:<Domain>:<Section>..., with PascalCase segments"
        )
    # 插件定义的权限码由宿主在登记代理操作时一并登记（plugin/invocation.md §1）：不合文法或重复的码，
    # 宿主在发现时拒绝整个插件；这里读清单时同样拒绝，不让它等到宿主那里才失败
    seen: set[str] = set()
    for code in contributions.permissions:
        if _PERMISSION_CODE.fullmatch(code) is None:
            raise _invalid(
                f"the permission code '{code}' in 'contributes.permissions' of '{id}' must be a lowercase "
                "dot-separated code with at least two segments, such as 'orders.read'"
            )
        if code in seen:
            raise _invalid(
                f"the permission code '{code}' appears more than once in 'contributes.permissions' of '{id}'"
            )
        seen.add(code)
    for health in contributions.health:
        if _HEALTH.fullmatch(health) is None:
            raise _invalid(f"the health item '{health}' of '{id}' is not valid")


def _is_interface_version(version: object) -> bool:
    """接口版本是正整数（布尔不算）。"""
    return isinstance(version, int) and not isinstance(version, bool) and version >= 1


def _invalid(message: str) -> FrameworkError:
    """清单校验失败的配置错误；消息与 Go SDK 一致，句末加句号。"""
    return FrameworkError(ERROR_CODE_INVALID_ARGUMENT, message + ".")


def _freeze_sequences(instance: object) -> None:
    """把以列表传入的序列字段固定成元组，冻结的 dataclass 才真正不可变。"""
    for field in fields(instance):  # type: ignore[arg-type]
        value = getattr(instance, field.name)
        if isinstance(value, list):
            object.__setattr__(instance, field.name, tuple(value))


# ---- 读取：JSON 值 → 模型；结构与类型不符抛 _SchemaMismatchError，由 read 包成“not valid JSON for the schema” ----


class _SchemaMismatchError(ValueError):
    """JSON 与 Schema 的结构或类型不符；消息带 JSON 路径。"""


_MANIFEST_MEMBERS: Final = frozenset(
    {
        "$schema",
        "id",
        "package",
        "version",
        "primitives",
        "stability",
        "kind",
        "role",
        "isolation",
        "provides",
        "requires",
        "bindings",
        "contributes",
        "activation",
        "permissions",
        "entry",
    }
)


def _manifest(value: JsonValue) -> Manifest:
    """把清单的 JSON 值转成模型；格式与跨字段约束由 ``Manifest`` 构造时校验。"""
    members = _object(value, "$", _MANIFEST_MEMBERS)
    stability_text = _string(members, "stability", "$")
    if stability_text is None:
        raise _SchemaMismatchError("$.stability: the property is required")
    stability = Stability.parse(stability_text)
    if stability is None:
        raise _SchemaMismatchError(f"$.stability: '{stability_text}' is not a stability")
    contributes = members.get("contributes")
    # kind / role / isolation 与 Go 一样按字符串读入，不认识的值交给 Manifest 的校验报出具体是哪一项
    return Manifest(
        schema=_string(members, "$schema", "$"),
        id=_string(members, "id", "$") or "",
        package=_string(members, "package", "$"),
        version=_string(members, "version", "$") or "",
        primitives=_version(members, "primitives", "$", allow_range=False),
        stability=stability,
        kind=cast(Kind, _enum_text(Kind, _string(members, "kind", "$") or "")),
        role=cast(Role | None, _optional_enum_text(Role, _string(members, "role", "$"))),
        isolation=cast(Isolation | None, _optional_enum_text(Isolation, _string(members, "isolation", "$"))),
        provides=tuple(_declaration(item, path) for item, path in _array(members, "provides", "$")),
        requires=tuple(_requirement(item, path) for item, path in _array(members, "requires", "$")),
        bindings=tuple(_binding(item, path) for item, path in _array(members, "bindings", "$")),
        contributes=None if "contributes" not in members else _contributions(contributes, "$.contributes"),
        activation=_strings(members, "activation", "$"),
        permissions=_strings(members, "permissions", "$"),
        entry=_string(members, "entry", "$"),
    )


def _declaration(value: JsonValue, path: str) -> CapabilityDeclaration:
    """``provides`` 的一项：``{"id", "version"}``。"""
    members = _object(value, path, frozenset({"id", "version"}))
    return CapabilityDeclaration(
        _string(members, "id", path) or "", _version(members, "version", path, allow_range=False)
    )


def _requirement(value: JsonValue, path: str) -> CapabilityRequirement:
    """``requires`` 的一项：``{"id", "version", "optional"}``，版本可带 ``^``。"""
    members = _object(value, path, frozenset({"id", "version", "optional"}))
    optional = _boolean(members, "optional", path)
    return CapabilityRequirement(
        _string(members, "id", path) or "", _version(members, "version", path, allow_range=True), bool(optional)
    )


def _binding(value: JsonValue, path: str) -> Binding:
    """``bindings`` 的一项。"""
    members = _object(value, path, frozenset({"name", "type", "required"}))
    return Binding(
        _string(members, "name", path) or "", _string(members, "type", path) or "", _boolean(members, "required", path)
    )


def _contributions(value: JsonValue, path: str) -> Contributions:
    """``contributes``。"""
    members = _object(value, path, frozenset({"pipeline", "operations", "config", "permissions", "health"}))
    config = members.get("config")
    return Contributions(
        pipeline=tuple(
            _pipeline_contribution(item, item_path) for item, item_path in _array(members, "pipeline", path)
        ),
        operations=_strings(members, "operations", path),
        config=None if "config" not in members else _config(config, f"{path}.config"),
        permissions=_strings(members, "permissions", path),
        health=_strings(members, "health", path),
    )


def _pipeline_contribution(value: JsonValue, path: str) -> PipelineContribution:
    """``contributes.pipeline`` 的一项；阶段名不认识即结构不符（与 Go 解码阶段名失败一致）。"""
    members = _object(value, path, frozenset({"middleware", "stage", "before", "after"}))
    stage_text = _string(members, "stage", path)
    if stage_text is None:
        raise _SchemaMismatchError(f"{path}.stage: the property is required")
    stage = PipelineStage.parse(stage_text)
    if stage is None:
        raise _SchemaMismatchError(f"{path}.stage: '{stage_text}' is not a pipeline stage")
    return PipelineContribution(
        _string(members, "middleware", path) or "",
        stage,
        _strings(members, "before", path),
        _strings(members, "after", path),
    )


def _config(value: JsonValue, path: str) -> ConfigContribution:
    """``contributes.config``。"""
    members = _object(value, path, frozenset({"section", "schema"}))
    return ConfigContribution(_string(members, "section", path) or "", _string(members, "schema", path))


def _object(value: JsonValue, path: str, allowed: frozenset[str]) -> Mapping[str, JsonValue]:
    """要求是对象且没有 Schema 之外的成员。"""
    if not isinstance(value, Mapping):
        raise _SchemaMismatchError(f"{path}: expected an object")
    for name in value:
        if name not in allowed:
            raise _SchemaMismatchError(f"{path}: unknown property '{name}'")
    return value


def _string(members: Mapping[str, JsonValue], name: str, path: str) -> str | None:
    """可选的字符串成员；没有时为 ``None``。"""
    if name not in members:
        return None
    value = members[name]
    if not isinstance(value, str):
        raise _SchemaMismatchError(f"{path}.{name}: expected a string")
    return value


def _boolean(members: Mapping[str, JsonValue], name: str, path: str) -> bool | None:
    """可选的布尔成员；没有时为 ``None``。"""
    if name not in members:
        return None
    value = members[name]
    if not isinstance(value, bool):
        raise _SchemaMismatchError(f"{path}.{name}: expected a boolean")
    return value


def _version(members: Mapping[str, JsonValue], name: str, path: str, *, allow_range: bool) -> int:
    """接口版本：数字字符串（``"1"``），依赖可写 ``"^1"``；没有时为 0，交给校验报出。"""
    text = _string(members, name, path)
    if text is None:
        return 0
    match = (_RANGE if allow_range else _VERSION).fullmatch(text)
    if match is None:
        raise _SchemaMismatchError(f"{path}.{name}: the interface version '{text}' must be a positive integer")
    return int(match.group(1) if allow_range else match.group())


def _array(members: Mapping[str, JsonValue], name: str, path: str) -> list[tuple[JsonValue, str]]:
    """可选的数组成员，带每一项的路径。"""
    if name not in members:
        return []
    value = members[name]
    if not isinstance(value, list):
        raise _SchemaMismatchError(f"{path}.{name}: expected an array")
    return [(item, f"{path}.{name}[{index}]") for index, item in enumerate(value)]


def _strings(members: Mapping[str, JsonValue], name: str, path: str) -> tuple[str, ...]:
    """可选的字符串数组成员。"""
    items: list[str] = []
    for item, item_path in _array(members, name, path):
        if not isinstance(item, str):
            raise _SchemaMismatchError(f"{item_path}: expected a string")
        items.append(item)
    return tuple(items)


def _enum_text[E: StrEnum](enum: type[E], text: str) -> E | str:
    """认识的值转成枚举，不认识的原样交给校验。"""
    try:
        return enum(text)
    except ValueError:
        return text


def _optional_enum_text[E: StrEnum](enum: type[E], text: str | None) -> E | str | None:
    """同 ``_enum_text``，``None`` 原样返回。"""
    return None if text is None else _enum_text(enum, text)


# ---- 写出：模型 → 按 Schema 顺序、省略缺省值的 JSON 值 ----


def _manifest_json(manifest: Manifest) -> dict[str, JsonValue]:
    """清单的 JSON 值，成员顺序与 Schema 一致。"""
    members: dict[str, JsonValue] = {}
    _put(members, "$schema", manifest.schema)
    members["id"] = manifest.id
    _put(members, "package", manifest.package)
    members["version"] = manifest.version
    members["primitives"] = str(manifest.primitives)
    members["stability"] = str(manifest.stability)
    members["kind"] = manifest.kind.value
    _put(members, "role", None if manifest.role is None else manifest.role.value)
    _put(members, "isolation", None if manifest.isolation is None else manifest.isolation.value)
    _put_list(members, "provides", [{"id": item.id, "version": str(item.version)} for item in manifest.provides])
    _put_list(members, "requires", [_requirement_json(item) for item in manifest.requires])
    _put_list(members, "bindings", [_binding_json(item) for item in manifest.bindings])
    if manifest.contributes is not None:
        members["contributes"] = _contributions_json(manifest.contributes)
    _put_list(members, "activation", list(manifest.activation))
    _put_list(members, "permissions", list(manifest.permissions))
    _put(members, "entry", manifest.entry)
    return members


def _requirement_json(requirement: CapabilityRequirement) -> dict[str, JsonValue]:
    """依赖：``optional`` 只在为真时写出。"""
    members: dict[str, JsonValue] = {"id": requirement.id, "version": str(requirement.version)}
    if requirement.optional:
        members["optional"] = True
    return members


def _binding_json(binding: Binding) -> dict[str, JsonValue]:
    """绑定：``required`` 照清单写过的值写出，没写的省略。"""
    members: dict[str, JsonValue] = {"name": binding.name, "type": binding.type}
    _put(members, "required", binding.required)
    return members


def _contributions_json(contributions: Contributions) -> dict[str, JsonValue]:
    """贡献点。"""
    members: dict[str, JsonValue] = {}
    pipeline: list[JsonValue] = []
    for contribution in contributions.pipeline:
        item: dict[str, JsonValue] = {"middleware": contribution.middleware, "stage": contribution.stage.manifest_name}
        _put_list(item, "before", list(contribution.before))
        _put_list(item, "after", list(contribution.after))
        pipeline.append(item)
    _put_list(members, "pipeline", pipeline)
    _put_list(members, "operations", list(contributions.operations))
    if contributions.config is not None:
        config: dict[str, JsonValue] = {"section": contributions.config.section}
        _put(config, "schema", contributions.config.schema)
        members["config"] = config
    _put_list(members, "permissions", list(contributions.permissions))
    _put_list(members, "health", list(contributions.health))
    return members


def _put(members: dict[str, JsonValue], name: str, value: JsonValue) -> None:
    """值不为 ``None`` 时写入。"""
    if value is not None:
        members[name] = value


def _put_list(members: dict[str, JsonValue], name: str, items: Sequence[JsonValue]) -> None:
    """非空时写入。"""
    if items:
        members[name] = list(items)
