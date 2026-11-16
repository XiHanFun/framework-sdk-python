"""manifest 的测试：能力依赖向量、Go SDK 移植来的读取 / 校验用例，以及与 Go 实际输出逐字节相同的规范化写出。"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from xihan_framework import manifest
from xihan_framework.manifest import (
    CapabilityDeclaration,
    CapabilityRequirement,
    Contributions,
    Isolation,
    Kind,
    Manifest,
    PipelineContribution,
    Role,
)
from xihan_framework.primitives import ERROR_CODE_INVALID_ARGUMENT, FrameworkError, PipelineStage, Stability

type VectorLoader = Callable[[str, str], Any]

ECHO_MANIFEST = """{
  "$schema": "https://spec.xihanfun.com/manifest/v1.json",
  "id": "xihan.samples.echo",
  "package": "EchoPlugin",
  "version": "5.0.0-preview.1",
  "primitives": "1",
  "stability": "experimental",
  "kind": "plugin",
  "isolation": "out-of-process",
  "requires": [ { "id": "kernel.plugins", "version": "1" } ],
  "contributes": {
    "operations": [ "echoes.create", "echoes.relay", "echoes.crash", "events.list" ],
    "config": { "section": "XiHan:Echo" },
    "health": [ "echo" ]
  },
  "permissions": [ "capability:kernel.pipeline" ],
  "activation": [ "onStartup" ],
  "entry": "EchoPlugin"
}"""

MINIMAL = {"id": "xihan.a.b", "version": "1.0.0", "primitives": "1", "stability": "stable", "kind": "capability"}


def manifest_json(**members: object) -> str:
    """在最小合法清单上增改成员；值为 None 的成员删掉。"""
    data: dict[str, object] = {**MINIMAL, **members}
    return json.dumps({name: value for name, value in data.items() if value is not None})


def assert_rejected(data: str | bytes) -> FrameworkError:
    """读取必须以 PRIMITIVES_VALIDATION_001 失败。"""
    with pytest.raises(FrameworkError) as raised:
        manifest.read(data)
    assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT
    return raised.value


def test_read_parses_a_plugin_manifest() -> None:
    parsed = manifest.read(ECHO_MANIFEST)
    assert parsed.id == "xihan.samples.echo"
    assert parsed.kind is Kind.PLUGIN
    assert parsed.isolation is Isolation.OUT_OF_PROCESS
    assert parsed.primitives == 1
    assert parsed.stability is Stability.EXPERIMENTAL
    assert [str(requirement) for requirement in parsed.requires] == ["kernel.plugins@1"]
    assert len(parsed.operations) == 4
    assert parsed.config_section == "XiHan:Echo"
    assert parsed.health == ("echo",)
    assert parsed.activates_on_startup
    assert parsed.effective_role is Role.PROVIDER
    assert manifest.domain(parsed.id) == "samples"
    assert manifest.domain("caching.distributed") == "caching"
    assert manifest.domain("Bad") is None
    written = manifest.write(parsed)
    again = manifest.read(written)
    assert again == parsed
    assert '"primitives": "1"' in written
    assert '"stability": "experimental"' in written


def test_write_matches_the_go_sdk_output() -> None:
    # 期望值是 Go SDK 的 manifest.Write 对同一份清单的实际输出：字段按 Schema 顺序、缺省值省略、^2 规范化为 2、HTML 字符转义
    source = """{
      "$schema": "https://spec.xihanfun.com/manifest/v1.json",
      "id": "xihan.samples.echo", "package": "EchoPlugin", "version": "5.0.0-preview.1", "primitives": "1",
      "stability": "experimental", "kind": "plugin", "isolation": "out-of-process",
      "requires": [ { "id": "kernel.plugins", "version": "1" }, { "id": "caching.distributed", "version": "^2", "optional": true } ],
      "provides": [ { "id": "samples.echo", "version": "3" } ],
      "bindings": [ { "name": "db", "type": "connection:relational", "required": false }, { "name": "cache", "type": "cache:distributed" } ],
      "contributes": {
        "operations": [ "echoes.create", "echoes.relay", "echoes.crash", "events.list" ],
        "config": { "section": "XiHan:Echo", "schema": "a<b>&c" },
        "permissions": [ "echo.read" ],
        "health": [ "echo" ]
      },
      "permissions": [ "capability:kernel.pipeline" ],
      "activation": [ "onStartup", "onEvent:orders.*" ],
      "entry": "EchoPlugin"
    }"""
    expected = (Path(__file__).parent / "data" / "echo.manifest.go.json").read_text(encoding="utf-8")
    assert manifest.write(manifest.read(source)) == expected.removesuffix("\n")


def test_config_section_may_have_subsections() -> None:
    for section in [
        "XiHan:Caching",
        "XiHan:Caching:Redis",
        "XiHan:Protocols:CloudEvents",
        "XiHan:ObjectStorage:SignedUrls",
        "XiHan:A:B2:C",
    ]:
        assert manifest.read(manifest_json(contributes={"config": {"section": section}})).config_section == section
    for section in [
        "Orders",
        "XiHan",
        "XiHan:",
        "XiHan:caching",
        "XiHan:Caching:",
        "XiHan:Caching::Redis",
        "XiHan:Caching:redis",
        "XiHan:Caching:Re-dis",
        "xihan:Caching",
        "XiHan:Caching:Redis:",
        "XiHan:Caching\n",
    ]:
        assert_rejected(manifest_json(contributes={"config": {"section": section}}))


def test_contributed_permission_codes_follow_the_host_grammar() -> None:
    plugin = {"kind": "plugin", "isolation": "out-of-process"}
    for codes in [[], ["orders.read"], ["sales.orders.read", "sales.orders.write"], ["echo2.items-v2.read"]]:
        parsed = manifest.read(manifest_json(**plugin, contributes={"permissions": codes}))
        assert parsed.contributes is not None
        assert list(parsed.contributes.permissions) == codes
    for codes in [
        ["orders"],
        ["Orders.read"],
        ["orders.Read"],
        ["orders..read"],
        ["orders.read."],
        [".orders.read"],
        ["1orders.read"],
        ["orders.1read"],
        ["orders_x.read"],
        ["orders:read"],
        ["orders.read", "orders.read"],
        ["orders.read", "orders.write", "orders.read"],
    ]:
        assert_rejected(manifest_json(**plugin, contributes={"permissions": codes}))


def test_read_rejects_invalid_manifests() -> None:
    wasm = {"kind": "plugin", "isolation": "wasm"}
    cases: dict[str, str] = {
        "unknown field": manifest_json(extra=1),
        "bad id": manifest_json(id="a.b"),
        "bad version": manifest_json(version="1.0"),
        "bad primitives": manifest_json(primitives="0"),
        "bad stability": manifest_json(stability="solid"),
        "bad kind": manifest_json(kind="thing"),
        "plugin needs isolation": manifest_json(kind="plugin"),
        "non-plugin isolation": manifest_json(isolation="wasm"),
        "non-plugin permissions": manifest_json(permissions=["capability:x"]),
        "duplicate provides": manifest_json(provides=[{"id": "a.b", "version": "1"}, {"id": "a.b", "version": "2"}]),
        "duplicate requires": manifest_json(requires=[{"id": "a.b", "version": "1"}, {"id": "a.b", "version": "^1"}]),
        "bad activation": manifest_json(**wasm, activation=["onBoot"]),
        "bad permission": manifest_json(**wasm, permissions=["sudo"]),
        "bad operation": manifest_json(contributes={"operations": ["Orders.Create"]}),
        "bad section": manifest_json(contributes={"config": {"section": "Orders"}}),
        "bad stage": manifest_json(contributes={"pipeline": [{"middleware": "x", "stage": "middle"}]}),
        "bad binding": manifest_json(bindings=[{"name": "Db", "type": "connection:relational"}]),
        "duplicate binding": manifest_json(bindings=[{"name": "db", "type": "queue"}, {"name": "db", "type": "blob"}]),
        "bad role": manifest_json(role="primary"),
        "bad health": manifest_json(contributes={"health": ["Echo"]}),
        "nameless middleware": manifest_json(contributes={"pipeline": [{"middleware": "", "stage": "observe"}]}),
    }
    for name, data in cases.items():
        with pytest.raises(FrameworkError) as raised:
            manifest.read(data)
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, name
    assert assert_rejected(manifest_json(kind="thing")).message == "the kind 'thing' of 'xihan.a.b' is not valid."
    assert assert_rejected(manifest_json(id="a.b")).message == "the manifest ID 'a.b' must match xihan.<domain>.<name>."


def test_read_is_strict_about_the_schema() -> None:
    # Schema 规定接口版本是字符串、只有依赖可带 ^，成员不能是 null，stability 必填；Go 的解码在这些地方更宽松
    cases: list[str | bytes] = [
        manifest_json(primitives=1),
        manifest_json(primitives="^1"),
        manifest_json(provides=[{"id": "a.b", "version": "^1"}]),
        manifest_json(provides=[{"id": "a.b", "version": 1}]),
        manifest_json(stability=None),
        json.dumps({**MINIMAL, "package": None}),
        manifest_json(contributes={"pipeline": [{"middleware": "x"}]}),
        manifest_json(bindings=[{"name": "db", "type": "queue", "required": "yes"}]),
        manifest_json(requires=[{"id": "a.b", "version": "1", "extra": True}]),
        "[]",
        "{",
        "\ufeff" + manifest_json(),
        b"\xff",
    ]
    for data in cases:
        error = assert_rejected(data)
        assert error.message == "The manifest is not valid JSON for the schema."
        assert error.__cause__ is not None
    assert assert_rejected(manifest_json(id="xihan.a.b\n")).message.startswith("the manifest ID")


def test_requirements_follow_the_vectors(vector: VectorLoader) -> None:
    vectors = vector("primitives", "capability-requirements")
    for case in vectors["valid"]:
        requirement = CapabilityRequirement.parse(case["input"])
        assert requirement == CapabilityRequirement(case["id"], case["version"], case["optional"]), case["input"]
    for text in vectors["invalid"]:
        assert CapabilityRequirement.parse(text) is None, text
    declaration = CapabilityDeclaration.parse("caching.distributed@2")
    assert declaration is not None
    assert str(declaration) == "caching.distributed@2"
    requirement = CapabilityRequirement.parse("caching.distributed@^2?")
    assert requirement is not None
    assert requirement.is_satisfied_by(declaration)
    assert str(requirement) == "caching.distributed@2?"
    other = CapabilityDeclaration.parse("caching.distributed@1")
    assert other is not None
    assert not requirement.is_satisfied_by(other)
    assert CapabilityDeclaration.parse("caching.distributed@^2") is None


def test_only_wasm_plugins_may_contribute_middleware() -> None:
    audit = [{"middleware": "audit", "stage": "observe"}, {"middleware": "tenant-guard", "stage": "tenant"}]
    for isolation in [Isolation.OUT_OF_PROCESS, Isolation.IN_PROCESS]:
        error = assert_rejected(
            manifest_json(kind="plugin", isolation=isolation.value, contributes={"pipeline": audit})
        )
        for fragment in ["'xihan.a.b'", "'contributes.pipeline'", "audit, tenant-guard", isolation.value]:
            assert fragment in error.message, error.message
    accepted = {
        "wasm plugin": manifest_json(kind="plugin", isolation="wasm", contributes={"pipeline": audit}),
        "capability": manifest_json(contributes={"pipeline": audit}),
        "provider": manifest_json(kind="provider", contributes={"pipeline": audit}),
        "out-of-process, empty list": manifest_json(
            kind="plugin", isolation="out-of-process", contributes={"pipeline": []}
        ),
        "in-process, empty list": manifest_json(kind="plugin", isolation="in-process", contributes={"pipeline": []}),
    }
    for name, data in accepted.items():
        assert manifest.read(data).id == "xihan.a.b", name
    wasm = manifest.read(accepted["wasm plugin"])
    assert wasm.contributes is not None
    assert wasm.contributes.pipeline[1] == PipelineContribution("tenant-guard", PipelineStage.TENANT)

    # 构造同样核对：不存在宿主会拒绝的清单实例，也就写不出它
    with pytest.raises(FrameworkError):
        Manifest(
            id="xihan.a.b",
            version="1.0.0",
            primitives=1,
            stability=Stability.STABLE,
            kind=Kind.PLUGIN,
            isolation=Isolation.OUT_OF_PROCESS,
            contributes=Contributions(pipeline=(PipelineContribution("audit", PipelineStage.OBSERVE),)),
        )


def test_manifests_are_immutable_and_freeze_lists() -> None:
    built = Manifest(
        id="xihan.a.b",
        version="1.0.0",
        primitives=1,
        stability=Stability.STABLE,
        kind=Kind.CAPABILITY,
        activation=["onOperation:orders.*"],  # type: ignore[arg-type]
    )
    assert built.activation == ("onOperation:orders.*",)
    assert not built.activates_on_startup
    assert built.effective_activation == ("onOperation:orders.*",)
    assert manifest.read(manifest_json()).effective_activation == ("onStartup",)
    with pytest.raises(AttributeError):
        built.id = "xihan.c.d"  # type: ignore[misc]


def test_read_file_reads_and_reports_missing_files(tmp_path: Path) -> None:
    path = tmp_path / manifest.FILE_NAME
    path.write_text(ECHO_MANIFEST, encoding="utf-8")
    assert manifest.read_file(path).id == "xihan.samples.echo"
    with pytest.raises(FrameworkError) as raised:
        manifest.read_file(tmp_path / "missing.json")
    assert raised.value.message == "The manifest file could not be read."
    assert isinstance(raised.value.__cause__, OSError)


def test_contributed_operations_follow_the_operation_id_grammar() -> None:
    # RFC-0002 解析规则 2（ADR-0233）：contributes.operations 的每一项符合操作 ID 文法，与 operation.is_id 同一条
    for operation_id in ["orders.create", "order-lines.list", "a1.b2", "items-v2.get-many"]:
        assert manifest.read(manifest_json(contributes={"operations": [operation_id]})).operations == (operation_id,)
    for operation_id in [
        "orders-.create",
        "-orders.create",
        "orders.create-",
        "or--ders.create",
        "orders.cre--ate",
        "orders",
        "Orders.create",
        "orders.create\n",
    ]:
        assert_rejected(manifest_json(contributes={"operations": [operation_id]}))


def test_duplicate_members_make_a_manifest_invalid() -> None:
    error = assert_rejected(
        '{"id":"xihan.a.b","version":"1.0.0","primitives":"1","stability":"stable","kind":"capability","kind":"provider"}'
    )
    assert isinstance(error.__cause__, ValueError)
    assert "more than once" in str(error.__cause__)


def test_manifests_follow_the_vectors(vector: VectorLoader) -> None:
    vectors = vector("manifest", "manifests")
    for case in vectors["valid"]:
        parsed = manifest.read(json.dumps(case["manifest"]))
        # 写出的清单再读一遍得到同一份清单（RFC-0002 解析规则 2）
        assert manifest.read(manifest.write(parsed)) == parsed, case["name"]
    for case in vectors["invalid"]:
        text = case["text"] if "text" in case else json.dumps(case["manifest"])
        with pytest.raises(FrameworkError) as raised:
            manifest.read(text)
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, case["name"]


def test_an_empty_activation_is_not_the_default() -> None:
    assert_rejected(manifest_json(activation=[]))
    with pytest.raises(FrameworkError):
        Manifest(
            id="xihan.a.b",
            version="1.0.0",
            primitives=1,
            stability=Stability.STABLE,
            kind=Kind.CAPABILITY,
            activation=(),
        )
    absent = manifest.read(manifest_json())
    assert absent.activation is None
    assert absent.effective_activation == ("onStartup",)
    assert '"activation"' not in manifest.write(absent)
