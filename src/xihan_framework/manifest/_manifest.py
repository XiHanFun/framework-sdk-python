"""``xihan.manifest.json`` 的模型、读取、写出与校验（RFC-0002）。"""

import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Final

from xihan_framework._json import JsonTextError, JsonValue, dumps, member_path, parse
from xihan_framework.primitives import (
    ERROR_CODE_INVALID_ARGUMENT,
    Error,
    FrameworkError,
    PipelineStage,
    Result,
    Stability,
    Unit,
    fail,
    ok,
    success,
)

#: 清单文件名。
FILE_NAME: Final = "xihan.manifest.json"
#: 写出的 ``$schema``。
SCHEMA_URI: Final = "https://spec.xihanfun.com/manifest/v1.json"
#: 缺省的激活事件。
ON_STARTUP: Final = "onStartup"
#: 接口版本的上限：32 位有符号整数。
MAX_INTERFACE_VERSION: Final = 2147483647

#: 清单读取失败（不是 JSON、为空、不合 Schema、跨字段约束不满足、文件不存在）。
ERROR_CODE_INVALID_MANIFEST: Final = "KERNEL_CAPABILITY_006"
#: 不是 JSON（参数 ``reason``：``$`` 或第一处重复成员的路径）。
MESSAGE_INVALID_JSON: Final = "kernel.manifest.invalid_json"
#: 根是字面量 ``null``（没有参数）。
MESSAGE_EMPTY: Final = "kernel.manifest.empty"
#: 不合 Schema（参数 ``reason``：第一处问题的路径）。
MESSAGE_INVALID: Final = "kernel.manifest.invalid"
#: 清单文件不存在（参数 ``path``）。
MESSAGE_FILE_MISSING: Final = "kernel.manifest.file_missing"
#: 插件没有声明 ``isolation``（参数 ``id``）。
MESSAGE_ISOLATION_REQUIRED: Final = "primitives.manifest.isolation_required"
#: 非插件声明了 ``isolation`` 或 ``permissions``（参数 ``id``）。
MESSAGE_ISOLATION_NOT_ALLOWED: Final = "primitives.manifest.isolation_not_allowed"
#: ``provides`` 的能力 ID 重复（参数 ``id``）。
MESSAGE_DUPLICATE_PROVIDES: Final = "primitives.manifest.duplicate_provides"
#: ``requires`` 的能力 ID 重复（参数 ``id``）。
MESSAGE_DUPLICATE_REQUIRES: Final = "primitives.manifest.duplicate_requires"
#: ``bindings`` 的名字重复（参数 ``id``）。
MESSAGE_DUPLICATE_BINDINGS: Final = "primitives.manifest.duplicate_bindings"

# 文法与 spec/manifest/manifest.schema.json 一致；一律 fullmatch，Python 的 $ 会放过结尾的换行（RFC-0001「文法的匹配」）
_MANIFEST_ID = re.compile(r"xihan\.[a-z][a-z0-9]*\.[a-z][a-z0-9-]*")
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9]*\.[a-z][a-z0-9-]*")
_SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_VERSION = re.compile(r"[1-9][0-9]*")
_REQUIREMENT = re.compile(r"([a-z][a-z0-9]*\.[a-z][a-z0-9-]*)@\^?([1-9][0-9]*)(\??)")
_DECLARATION = re.compile(r"([a-z][a-z0-9]*\.[a-z][a-z0-9-]*)@([1-9][0-9]*)")
_ACTIVATION = re.compile(r"onStartup|onOperation:[a-z0-9.*-]+|onEvent:[a-z0-9.*-]+")
# .+ 不跨换行：Python 的 . 与 .NET 一样不匹配 \n
_PERMISSION = re.compile(r"(?:net\.outbound|fs\.read|fs\.write|capability):.+")
_BINDING_NAME = re.compile(r"[a-z][a-z0-9-]*")
_BINDING_TYPE = re.compile(r"[a-z]+(?::[a-z]+)?")
# 操作 ID 文法（RFC-0011、conventions/operation-ids.md）：连字符只在字母数字之间，不开头、不结尾、不连写
_OPERATION = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_SECTION = re.compile(r"XiHan:[A-Z][A-Za-z0-9]*(?::[A-Z][A-Za-z0-9]*)*")
_HEALTH = re.compile(r"[a-z][a-z0-9.-]*")
_PERMISSION_CODE = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+")
_UTF8_BOM: Final = b"\xef\xbb\xbf"


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
    """判断清单 ID 是否符合 ``xihan.<领域>.<名称>``，按整串匹配。"""
    return _MANIFEST_ID.fullmatch(id) is not None


def is_capability_id(id: str) -> bool:
    """判断能力 ID 是否符合 ``<领域>.<能力>``，按整串匹配。"""
    return _CAPABILITY_ID.fullmatch(id) is not None


def is_interface_version(text: str) -> bool:
    """判断接口版本文本：不带前导零、符号与空白的十进制正整数，不超过 2147483647（conventions/capability-ids.md）。"""
    return _VERSION.fullmatch(text) is not None and int(text) <= MAX_INTERFACE_VERSION


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
        """解析 ``id@version``；不合文法或版本超过 2147483647 时为 ``None``。"""
        match = _DECLARATION.fullmatch(text)
        if match is None or not is_interface_version(match.group(2)):
            return None
        return cls(match.group(1), int(match.group(2)))

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
        """解析 ``id@[^]version[?]``；不合文法（含前导零、``+``、空白与结尾的换行）或版本超过 2147483647 时为 ``None``。"""
        match = _REQUIREMENT.fullmatch(text)
        if match is None or not is_interface_version(match.group(2)):
            return None
        return cls(match.group(1), int(match.group(2)), match.group(3) == "?")

    def is_satisfied_by(self, declaration: CapabilityDeclaration) -> bool:
        """判断一个声明是否满足本依赖：同一能力且同一接口版本。"""
        return self.id == declaration.id and self.version == declaration.version

    def __str__(self) -> str:
        """``id@version``，可选依赖带 ``?``。"""
        return f"{self.id}@{self.version}{'?' if self.optional else ''}"


@dataclass(frozen=True, slots=True)
class Binding:
    """需要宿主注入的资源；``required`` 缺省为必需。"""

    name: str
    type: str
    required: bool = True


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
    """一份清单；字段顺序与写出的顺序一致。

    构造时校验各字段自己的格式，不合抛出 ``FrameworkError``（``PRIMITIVES_VALIDATION_001``）；字段之间的约束由
    ``validate`` 核对，与 .NET 的 ``Manifest.Validate`` 相同。
    """

    id: str
    package: str | None = None
    version: str
    primitives: int
    stability: Stability
    kind: Kind
    role: Role = Role.PROVIDER
    isolation: Isolation | None = None
    provides: tuple[CapabilityDeclaration, ...] = ()
    requires: tuple[CapabilityRequirement, ...] = ()
    bindings: tuple[Binding, ...] = ()
    contributes: Contributions = field(default_factory=Contributions)
    activation: tuple[str, ...] = (ON_STARTUP,)
    """激活事件，缺省为 ``onStartup``，至少一项。"""
    permissions: tuple[str, ...] = ()
    entry: str | None = None

    def __post_init__(self) -> None:
        """把序列字段固定成元组，再校验各字段的格式。"""
        _freeze_sequences(self)
        _check_format(self)

    def validate(self) -> Result[Unit]:
        """核对字段之间的约束（RFC-0002 解析规则 2 第 4 步），按这个顺序报第一处。

        插件没有 ``isolation`` 为 ``primitives.manifest.isolation_required``；非插件写了 ``isolation`` 或 ``permissions``
        为 ``isolation_not_allowed``；``provides`` 的能力 ID 重复为 ``duplicate_provides``；``requires`` 的能力 ID 重复为
        ``duplicate_requires``；``bindings`` 的名字重复为 ``duplicate_bindings``。错误码是 ``PRIMITIVES_VALIDATION_001``，
        参数只有 ``id``（清单 ID）；``read`` 把码换成 ``KERNEL_CAPABILITY_006``。
        """
        key: str | None = None
        if self.kind is Kind.PLUGIN and self.isolation is None:
            key = MESSAGE_ISOLATION_REQUIRED
        elif self.kind is not Kind.PLUGIN and (self.isolation is not None or self.permissions):
            key = MESSAGE_ISOLATION_NOT_ALLOWED
        elif _has_duplicates(declaration.id for declaration in self.provides):
            key = MESSAGE_DUPLICATE_PROVIDES
        elif _has_duplicates(requirement.id for requirement in self.requires):
            key = MESSAGE_DUPLICATE_REQUIRES
        elif _has_duplicates(binding.name for binding in self.bindings):
            key = MESSAGE_DUPLICATE_BINDINGS
        if key is None:
            return success()
        return fail(Error.validation(ERROR_CODE_INVALID_ARGUMENT, key, {"id": self.id}))

    @property
    def activates_on_startup(self) -> bool:
        """是否在宿主启动时激活。"""
        return ON_STARTUP in self.activation

    @property
    def operations(self) -> tuple[str, ...]:
        """贡献的操作 ID。"""
        return self.contributes.operations

    @property
    def health(self) -> tuple[str, ...]:
        """贡献的健康项。"""
        return self.contributes.health

    @property
    def config_section(self) -> str | None:
        """贡献的配置节；没有时为 ``None``。"""
        return None if self.contributes.config is None else self.contributes.config.section


def read(data: str | bytes) -> Result[Manifest]:
    """读取清单 JSON（RFC-0002 解析规则 2），失败都是 ``KERNEL_CAPABILITY_006``，每一步只报第一处问题。

    1. JSON 文本过共同检查：不是 JSON 为 ``kernel.manifest.invalid_json``，``reason`` 为 ``$``；有重复成员时同一个键，
       ``reason`` 为第一处重复的路径。文本原样检查，开头的 BOM 是语法错误。
    2. 根是字面量 ``null``：``kernel.manifest.empty``。
    3. 按 Schema 逐项核对：``kernel.manifest.invalid``，``reason`` 是第一处问题的路径。
    4. 字段之间的约束（``Manifest.validate``）：保留 ``primitives.manifest.*`` 的键与 ``id`` 参数。
    """
    if isinstance(data, str):
        try:
            data = data.encode("utf-8")
        except UnicodeEncodeError:
            # 文本里的孤立代理项不是合法的 Unicode，按不是 JSON 处理
            return fail(_invalid_json("$"))
    return _read_utf8(data)


def read_file(path: str | os.PathLike[str]) -> Result[Manifest]:
    """读取清单文件：按 UTF-8 读，跳过开头的 UTF-8 BOM，其余同 ``read``；文件不存在为 ``kernel.manifest.file_missing``。"""
    file = Path(path)
    if not file.is_file():
        return fail(Error(ERROR_CODE_INVALID_MANIFEST, MESSAGE_FILE_MISSING, {"path": os.fspath(path)}))
    data = file.read_bytes()
    return _read_utf8(data[len(_UTF8_BOM) :] if data.startswith(_UTF8_BOM) else data)


def write(manifest: Manifest) -> str:
    """写出清单 JSON（RFC-0002「写出」），排版各语言逐字相同。

    两个空格缩进，换行只用 LF，末尾不换行，成员名与值之间是 ``": "``；空数组写 ``[]``。成员顺序固定，``package``、
    ``isolation``、``config``、``config.schema``、``entry`` 没有值时不写，其余总写：``$schema`` 写固定的地址，``role``
    缺省写 ``provider``，``activation`` 缺省写 ``["onStartup"]``，接口版本写成不带 ``^`` 的十进制文本，枚举写小写名。
    字符串只转义 ``"``、``\\``、控制字符与孤立的代理项。
    """
    return dumps(_manifest_json(manifest), indent=True)


def _read_utf8(data: bytes) -> Result[Manifest]:
    """按顺序核对 UTF-8 文本：JSON 文本 → 字面量 null → Schema 形状 → 跨字段约束。"""
    try:
        value = parse(data)
    except JsonTextError as error:
        return fail(_invalid_json(error.path))
    if value is None:
        return fail(Error(ERROR_CODE_INVALID_MANIFEST, MESSAGE_EMPTY))
    problem = _ROOT.find_problem(value, "$")
    if problem is not None:
        return fail(Error(ERROR_CODE_INVALID_MANIFEST, MESSAGE_INVALID, {"reason": problem}))
    # 形状已核对：转换与各字段的格式校验不会再失败，失败即两处规则不一致，按缺陷抛出
    assert isinstance(value, Mapping)
    manifest = _manifest(value)
    validation = manifest.validate()
    if validation.error is not None:
        return fail(validation.error.with_code(ERROR_CODE_INVALID_MANIFEST))
    return ok(manifest)


def _invalid_json(path: str) -> Error:
    """不是 JSON 的错误。"""
    return Error(ERROR_CODE_INVALID_MANIFEST, MESSAGE_INVALID_JSON, {"reason": path})


def _check_format(manifest: Manifest) -> None:
    """校验各字段自己的格式；不合抛出 ``FrameworkError``，消息指出是哪一项。"""
    id = manifest.id
    if not isinstance(id, str) or not is_manifest_id(id):
        raise _invalid(f"the manifest ID '{id}' must match xihan.<domain>.<name>")
    if not isinstance(manifest.version, str) or _SEMVER.fullmatch(manifest.version) is None:
        raise _invalid(f"the version '{manifest.version}' of '{id}' must be a semantic version")
    if not _is_version_number(manifest.primitives):
        raise _invalid(f"the primitives version of '{id}' must be an integer from 1 to {MAX_INTERFACE_VERSION}")
    if not isinstance(manifest.stability, Stability):
        raise _invalid(f"the stability of '{id}' is not valid")
    if not isinstance(manifest.kind, Kind):
        raise _invalid(f"the kind '{manifest.kind}' of '{id}' is not valid")
    if not isinstance(manifest.role, Role):
        raise _invalid(f"the role '{manifest.role}' of '{id}' is not valid")
    if manifest.isolation is not None and not isinstance(manifest.isolation, Isolation):
        raise _invalid(f"the isolation '{manifest.isolation}' of '{id}' is not valid")
    for declaration in manifest.provides:
        if not is_capability_id(declaration.id) or not _is_version_number(declaration.version):
            raise _invalid(f"the provided capability '{declaration.id}' of '{id}' is not valid")
    for requirement in manifest.requires:
        if not is_capability_id(requirement.id) or not _is_version_number(requirement.version):
            raise _invalid(f"the required capability '{requirement.id}' of '{id}' is not valid")
    for binding in manifest.bindings:
        if _BINDING_NAME.fullmatch(binding.name) is None or _BINDING_TYPE.fullmatch(binding.type) is None:
            raise _invalid(f"the binding '{binding.name}' ({binding.type}) of '{id}' is not valid")
    if not manifest.activation:
        raise _invalid(f"the activation of '{id}' must list at least one event; omit it to activate on startup")
    for event in manifest.activation:
        if _ACTIVATION.fullmatch(event) is None:
            raise _invalid(f"the activation event '{event}' of '{id}' is not valid")
    for permission in manifest.permissions:
        if _PERMISSION.fullmatch(permission) is None:
            raise _invalid(f"the permission '{permission}' of '{id}' is not valid")
    _check_contributions(manifest.contributes, id)


def _check_contributions(contributions: Contributions, id: str) -> None:
    """校验贡献点各项的格式。"""
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
    for code in contributions.permissions:
        if _PERMISSION_CODE.fullmatch(code) is None:
            raise _invalid(
                f"the permission code '{code}' in 'contributes.permissions' of '{id}' must be a lowercase "
                "dot-separated code with at least two segments, such as 'orders.read'"
            )
    if _has_duplicates(contributions.permissions):
        raise _invalid(f"'contributes.permissions' of '{id}' lists a permission code more than once")
    for health in contributions.health:
        if _HEALTH.fullmatch(health) is None:
            raise _invalid(f"the health item '{health}' of '{id}' is not valid")


def _is_version_number(version: object) -> bool:
    """模型里的接口版本：1 到 2147483647 的整数（布尔不算）。"""
    return isinstance(version, int) and not isinstance(version, bool) and 1 <= version <= MAX_INTERFACE_VERSION


def _has_duplicates(values: Iterable[str]) -> bool:
    """序列里是否有重复项（按序数比较）。"""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return True
        seen.add(value)
    return False


def _invalid(message: str) -> FrameworkError:
    """清单字段格式不合的编程错误，句末加句号。"""
    return FrameworkError(ERROR_CODE_INVALID_ARGUMENT, message + ".")


def _freeze_sequences(instance: object) -> None:
    """把以列表传入的序列字段固定成元组，冻结的 dataclass 才真正不可变。"""
    for item in fields(instance):  # type: ignore[arg-type]
        value = getattr(instance, item.name)
        if isinstance(value, list):
            object.__setattr__(instance, item.name, tuple(value))


# ---- 形状：照 manifest.schema.json 逐项核对，第一处问题的路径就是 reason（RFC-0002 解析规则 2 第 3 步） ----


@dataclass(frozen=True, slots=True)
class _Value:
    """一个值的形状：字符串（可带取值约束）、布尔、对象或数组。"""

    kind: str
    accepts: Callable[[str], bool] | None = None
    shape: "_Object | None" = None
    item: "_Value | None" = None
    non_empty: bool = False
    unique: bool = False

    def find_problem(self, value: JsonValue, path: str) -> str | None:
        """核对一个值：``null`` → 类型 → 取值，对象与数组进到里面再查；Schema 里没有哪个值能取 ``null``。"""
        if value is None:
            return path
        if self.kind == "string":
            if not isinstance(value, str) or (self.accepts is not None and not self.accepts(value)):
                return path
            return None
        if self.kind == "boolean":
            return None if isinstance(value, bool) else path
        if self.kind == "object":
            assert self.shape is not None
            return self.shape.find_problem(value, path)
        assert self.item is not None
        if not isinstance(value, list) or (self.non_empty and not value):
            return path
        seen: set[str] = set()
        for index, element in enumerate(value):
            item_path = f"{path}[{index}]"
            problem = self.item.find_problem(element, item_path)
            if problem is not None:
                return problem
            # 不得重复的字符串数组：先核对这一项本身，再看它是否与前面的项重复，重复时报第二次出现的位置
            if self.unique:
                assert isinstance(element, str)
                if element in seen:
                    return item_path
                seen.add(element)
        return None


@dataclass(frozen=True, slots=True)
class _Object:
    """一个对象的形状：允许的成员（区分大小写）与按 Schema 顺序的必填成员。"""

    members: Mapping[str, _Value]
    required: tuple[str, ...] = ()

    def find_problem(self, value: JsonValue, path: str) -> str | None:
        """成员按文本顺序核对，每个成员依次是 Schema 之外的成员 → 它的值；查完后按 ``required`` 的顺序查缺少的成员。"""
        if not isinstance(value, Mapping):
            return path
        for name, member in value.items():
            shape = self.members.get(name)
            path_of_member = member_path(path, name)
            if shape is None:
                return path_of_member
            problem = shape.find_problem(member, path_of_member)
            if problem is not None:
                return problem
        for name in self.required:
            if name not in value:
                return member_path(path, name)
        return None


def _text(accepts: Callable[[str], bool] | None = None) -> _Value:
    """字符串，可带取值约束。"""
    return _Value("string", accepts)


def _pattern(pattern: re.Pattern[str]) -> _Value:
    """符合文法的字符串，按整串匹配。"""
    return _Value("string", lambda text: pattern.fullmatch(text) is not None)


def _one_of(*names: str) -> _Value:
    """枚举：只认小写名，区分大小写。"""
    allowed = frozenset(names)
    return _Value("string", lambda text: text in allowed)


def _list(item: _Value, *, non_empty: bool = False, unique: bool = False) -> _Value:
    """数组。"""
    return _Value("array", item=item, non_empty=non_empty, unique=unique)


def _shape(members: Mapping[str, _Value], required: tuple[str, ...] = ()) -> _Value:
    """对象。"""
    return _Value("object", shape=_Object(members, required))


_BOOLEAN: Final = _Value("boolean")
_STRING: Final = _text()
_CAPABILITY: Final = _pattern(_CAPABILITY_ID)
_ROOT: Final = _Object(
    {
        "$schema": _STRING,
        "id": _pattern(_MANIFEST_ID),
        "package": _STRING,
        "version": _pattern(_SEMVER),
        "primitives": _text(is_interface_version),
        "stability": _one_of(*(str(stability) for stability in Stability)),
        "kind": _one_of(*(kind.value for kind in Kind)),
        "role": _one_of(*(role.value for role in Role)),
        "isolation": _one_of(*(isolation.value for isolation in Isolation)),
        "provides": _list(
            _shape({"id": _CAPABILITY, "version": _text(is_interface_version)}, ("id", "version")),
        ),
        "requires": _list(
            _shape(
                {
                    "id": _CAPABILITY,
                    "version": _text(lambda text: is_interface_version(text.removeprefix("^"))),
                    "optional": _BOOLEAN,
                },
                ("id", "version"),
            ),
        ),
        "bindings": _list(
            _shape(
                {"name": _pattern(_BINDING_NAME), "type": _pattern(_BINDING_TYPE), "required": _BOOLEAN},
                ("name", "type"),
            ),
        ),
        "contributes": _shape(
            {
                "pipeline": _list(
                    _shape(
                        {
                            "middleware": _text(lambda text: len(text) > 0),
                            "stage": _one_of(*(stage.manifest_name for stage in PipelineStage)),
                            "before": _list(_STRING),
                            "after": _list(_STRING),
                        },
                        ("middleware", "stage"),
                    ),
                ),
                "operations": _list(_pattern(_OPERATION)),
                "config": _shape({"section": _pattern(_SECTION), "schema": _STRING}, ("section",)),
                "permissions": _list(_pattern(_PERMISSION_CODE), unique=True),
                "health": _list(_pattern(_HEALTH)),
            }
        ),
        "activation": _list(_pattern(_ACTIVATION), non_empty=True),
        "permissions": _list(_pattern(_PERMISSION)),
        "entry": _STRING,
    },
    ("id", "version", "primitives", "stability", "kind"),
)


# ---- 转换：已核对形状的 JSON 值 → 模型 ----


def _manifest(members: Mapping[str, JsonValue]) -> Manifest:
    """把已核对形状的清单 JSON 值转成模型；``$schema`` 不保留，写出时写固定的地址。"""
    contributes = members.get("contributes")
    stability = Stability.parse(_get(members, "stability"))
    assert stability is not None
    return Manifest(
        id=_get(members, "id"),
        package=_optional(members, "package"),
        version=_get(members, "version"),
        primitives=int(_get(members, "primitives")),
        stability=stability,
        kind=Kind(_get(members, "kind")),
        role=Role(_optional(members, "role") or Role.PROVIDER),
        isolation=None if "isolation" not in members else Isolation(_get(members, "isolation")),
        provides=tuple(
            CapabilityDeclaration(_get(item, "id"), int(_get(item, "version")))
            for item in _objects(members, "provides")
        ),
        requires=tuple(
            CapabilityRequirement(
                _get(item, "id"), int(_get(item, "version").removeprefix("^")), item.get("optional") is True
            )
            for item in _objects(members, "requires")
        ),
        bindings=tuple(
            Binding(_get(item, "name"), _get(item, "type"), item.get("required") is not False)
            for item in _objects(members, "bindings")
        ),
        contributes=Contributions() if not isinstance(contributes, Mapping) else _contributions(contributes),
        activation=_strings(members, "activation") if "activation" in members else (ON_STARTUP,),
        permissions=_strings(members, "permissions"),
        entry=_optional(members, "entry"),
    )


def _contributions(members: Mapping[str, JsonValue]) -> Contributions:
    """``contributes``。"""
    config = members.get("config")
    pipeline: list[PipelineContribution] = []
    for item in _objects(members, "pipeline"):
        stage = PipelineStage.parse(_get(item, "stage"))
        assert stage is not None
        pipeline.append(
            PipelineContribution(_get(item, "middleware"), stage, _strings(item, "before"), _strings(item, "after"))
        )
    return Contributions(
        pipeline=tuple(pipeline),
        operations=_strings(members, "operations"),
        config=None
        if not isinstance(config, Mapping)
        else ConfigContribution(_get(config, "section"), _optional(config, "schema")),
        permissions=_strings(members, "permissions"),
        health=_strings(members, "health"),
    )


def _get(members: Mapping[str, JsonValue], name: str) -> str:
    """必有的字符串成员。"""
    value = members[name]
    assert isinstance(value, str)
    return value


def _optional(members: Mapping[str, JsonValue], name: str) -> str | None:
    """可选的字符串成员。"""
    return _get(members, name) if name in members else None


def _objects(members: Mapping[str, JsonValue], name: str) -> list[Mapping[str, JsonValue]]:
    """可选的对象数组成员。"""
    items = members.get(name, [])
    assert isinstance(items, list)
    return [item for item in items if isinstance(item, Mapping)]


def _strings(members: Mapping[str, JsonValue], name: str) -> tuple[str, ...]:
    """可选的字符串数组成员。"""
    items = members.get(name, [])
    assert isinstance(items, list)
    return tuple(item for item in items if isinstance(item, str))


# ---- 写出：模型 → 按 RFC-0002「写出」的成员顺序的 JSON 值 ----


def _manifest_json(manifest: Manifest) -> dict[str, JsonValue]:
    """清单的 JSON 值，成员顺序见 ``write``。"""
    members: dict[str, JsonValue] = {"$schema": SCHEMA_URI, "id": manifest.id}
    _put(members, "package", manifest.package)
    members["version"] = manifest.version
    members["primitives"] = str(manifest.primitives)
    members["stability"] = str(manifest.stability)
    members["kind"] = manifest.kind.value
    members["role"] = manifest.role.value
    _put(members, "isolation", None if manifest.isolation is None else manifest.isolation.value)
    members["provides"] = [{"id": item.id, "version": str(item.version)} for item in manifest.provides]
    members["requires"] = [
        {"id": item.id, "version": str(item.version), "optional": item.optional} for item in manifest.requires
    ]
    members["bindings"] = [
        {"name": item.name, "type": item.type, "required": item.required} for item in manifest.bindings
    ]
    members["contributes"] = _contributions_json(manifest.contributes)
    members["activation"] = list(manifest.activation)
    members["permissions"] = list(manifest.permissions)
    _put(members, "entry", manifest.entry)
    return members


def _contributions_json(contributions: Contributions) -> dict[str, JsonValue]:
    """贡献点：``config`` 没有时不写，其余列表总写。"""
    members: dict[str, JsonValue] = {
        "pipeline": [
            {
                "middleware": item.middleware,
                "stage": item.stage.manifest_name,
                "before": list(item.before),
                "after": list(item.after),
            }
            for item in contributions.pipeline
        ],
        "operations": list(contributions.operations),
    }
    if contributions.config is not None:
        config: dict[str, JsonValue] = {"section": contributions.config.section}
        _put(config, "schema", contributions.config.schema)
        members["config"] = config
    members["permissions"] = list(contributions.permissions)
    members["health"] = list(contributions.health)
    return members


def _put(members: dict[str, JsonValue], name: str, value: JsonValue) -> None:
    """值不为 ``None`` 时写入。"""
    if value is not None:
        members[name] = value
