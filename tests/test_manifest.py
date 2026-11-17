"""manifest 的测试：清单与能力依赖向量（读取的错误与写出的全文逐字相等），以及模型的格式校验与跨字段约束。"""

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


def compact(value: object) -> str:
    """向量里的清单按紧凑写法交给读取，成员顺序照写。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_ok(data: str | bytes) -> Manifest:
    """读取必须成功。"""
    result = manifest.read(data)
    assert result.error is None, result.error
    return result.value_or_raise()


def read_error(data: str | bytes) -> tuple[str, str | None, dict[str, object]]:
    """读取必须失败，返回错误码、消息键与参数。"""
    result = manifest.read(data)
    assert result.error is not None, data
    return result.error.code, result.error.message_key, dict(result.error.details or {})


def invalid(reason: str) -> tuple[str, str, dict[str, object]]:
    """不合 Schema 的期望错误。"""
    return manifest.ERROR_CODE_INVALID_MANIFEST, manifest.MESSAGE_INVALID, {"reason": reason}


def test_read_parses_a_plugin_manifest() -> None:
    parsed = read_ok(ECHO_MANIFEST)
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
    assert parsed.role is Role.PROVIDER
    assert manifest.domain(parsed.id) == "samples"
    assert manifest.domain("caching.distributed") == "caching"
    assert manifest.domain("Bad") is None
    assert read_ok(manifest.write(parsed)) == parsed


def test_manifests_follow_the_vectors(vector: VectorLoader) -> None:
    vectors = vector("manifest", "manifests")
    for case in vectors["valid"]:
        parsed = read_ok(compact(case["manifest"]))
        # 写出的全文逐字相等：两空格缩进、LF、末尾不换行、成员顺序固定、字符串只转义必须转义的字符（RFC-0002「写出」）
        written = manifest.write(parsed)
        assert written == case["written"], case["name"]
        assert read_ok(written) == parsed, case["name"]
    for case in vectors["invalid"]:
        # text 可能是空串，要与没有 text 区分
        text = case["text"] if "text" in case else compact(case["manifest"])
        error = case["error"]
        assert read_error(text) == (error["code"], error["messageKey"], error["arguments"]), case["reason"]


def test_write_lists_every_collection_and_omits_only_the_optional_members() -> None:
    source = manifest_json(
        kind="plugin",
        isolation="wasm",
        requires=[{"id": "caching.distributed", "version": "^2", "optional": True}],
        bindings=[{"name": "cache", "type": "cache:distributed"}],
        contributes={"pipeline": [{"middleware": "audit", "stage": "observe", "after": ["trace"]}]},
    )
    assert manifest.write(read_ok(source)) == (
        "{\n"
        '  "$schema": "https://spec.xihanfun.com/manifest/v1.json",\n'
        '  "id": "xihan.a.b",\n'
        '  "version": "1.0.0",\n'
        '  "primitives": "1",\n'
        '  "stability": "stable",\n'
        '  "kind": "plugin",\n'
        '  "role": "provider",\n'
        '  "isolation": "wasm",\n'
        '  "provides": [],\n'
        '  "requires": [\n'
        "    {\n"
        '      "id": "caching.distributed",\n'
        '      "version": "2",\n'
        '      "optional": true\n'
        "    }\n"
        "  ],\n"
        '  "bindings": [\n'
        "    {\n"
        '      "name": "cache",\n'
        '      "type": "cache:distributed",\n'
        '      "required": true\n'
        "    }\n"
        "  ],\n"
        '  "contributes": {\n'
        '    "pipeline": [\n'
        "      {\n"
        '        "middleware": "audit",\n'
        '        "stage": "observe",\n'
        '        "before": [],\n'
        '        "after": [\n'
        '          "trace"\n'
        "        ]\n"
        "      }\n"
        "    ],\n"
        '    "operations": [],\n'
        '    "permissions": [],\n'
        '    "health": []\n'
        "  },\n"
        '  "activation": [\n'
        '    "onStartup"\n'
        "  ],\n"
        '  "permissions": []\n'
        "}"
    )


def test_read_follows_the_check_order() -> None:
    # JSON 文本 → 字面量 null → Schema 形状 → 跨字段约束（RFC-0002 解析规则 2）
    assert read_error("[1,]") == (manifest.ERROR_CODE_INVALID_MANIFEST, manifest.MESSAGE_INVALID_JSON, {"reason": "$"})
    assert read_error(b"\xff") == (manifest.ERROR_CODE_INVALID_MANIFEST, manifest.MESSAGE_INVALID_JSON, {"reason": "$"})
    assert read_error('"' + chr(0xD800) + '"') == (
        manifest.ERROR_CODE_INVALID_MANIFEST,
        manifest.MESSAGE_INVALID_JSON,
        {"reason": "$"},
    )
    assert read_error(" null ") == (manifest.ERROR_CODE_INVALID_MANIFEST, manifest.MESSAGE_EMPTY, {})
    assert read_error(manifest_json(kind="plugin", extra=1)) == invalid("$.extra")
    assert read_error(manifest_json(kind="plugin")) == (
        manifest.ERROR_CODE_INVALID_MANIFEST,
        manifest.MESSAGE_ISOLATION_REQUIRED,
        {"id": "xihan.a.b"},
    )
    # 一个对象的成员都查完后才按 required 的顺序报缺少的成员
    assert read_error('{"kind":"capability","extra":1}') == invalid("$.extra")
    assert read_error('{"kind":"capability"}') == invalid("$.id")
    assert read_error(manifest_json(provides=[{"version": "1"}])) == invalid("$.provides[0].id")


def test_read_checks_each_member_against_the_schema() -> None:
    cases: dict[str, str] = {
        "$.id": manifest_json(id="a.b"),
        "$.version": manifest_json(version="1.0"),
        "$.primitives": manifest_json(primitives="0"),
        "$.stability": manifest_json(stability="solid"),
        "$.kind": manifest_json(kind="thing"),
        "$.role": manifest_json(role="primary"),
        "$.activation[0]": manifest_json(kind="plugin", isolation="wasm", activation=["onBoot"]),
        "$.permissions[0]": manifest_json(kind="plugin", isolation="wasm", permissions=["sudo"]),
        "$.contributes.operations[0]": manifest_json(contributes={"operations": ["Orders.Create"]}),
        "$.contributes.config.section": manifest_json(contributes={"config": {"section": "Orders"}}),
        "$.contributes.pipeline[0].stage": manifest_json(contributes={"pipeline": [{"middleware": "x", "stage": "m"}]}),
        "$.bindings[0].name": manifest_json(bindings=[{"name": "Db", "type": "connection:relational"}]),
        "$.bindings[0].required": manifest_json(bindings=[{"name": "db", "type": "queue", "required": "yes"}]),
        "$.requires[0].extra": manifest_json(requires=[{"id": "a.b", "version": "1", "extra": True}]),
        "$.provides[0].version": manifest_json(provides=[{"id": "a.b", "version": 1}]),
        "$.package": json.dumps({**MINIMAL, "package": None}),
        "$": "[]",
    }
    for reason, data in cases.items():
        assert read_error(data) == invalid(reason), reason
    # 文法按整串匹配，结尾的换行不例外（RFC-0001「文法的匹配」）
    assert read_error(manifest_json(id="xihan.a.b\n")) == invalid("$.id")
    # 接口版本不带前导零，不超过 2147483647
    assert read_error(manifest_json(primitives="01")) == invalid("$.primitives")
    assert read_error(manifest_json(requires=[{"id": "a.b", "version": "^2147483648"}])) == invalid(
        "$.requires[0].version"
    )
    assert read_ok(manifest_json(primitives="2147483647")).primitives == manifest.MAX_INTERFACE_VERSION


def test_config_section_may_have_subsections() -> None:
    for section in [
        "XiHan:Caching",
        "XiHan:Caching:Redis",
        "XiHan:Protocols:CloudEvents",
        "XiHan:ObjectStorage:SignedUrls",
        "XiHan:A:B2:C",
    ]:
        assert read_ok(manifest_json(contributes={"config": {"section": section}})).config_section == section
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
        assert read_error(manifest_json(contributes={"config": {"section": section}})) == invalid(
            "$.contributes.config.section"
        ), section


def test_contributed_permission_codes_follow_the_host_grammar() -> None:
    plugin = {"kind": "plugin", "isolation": "out-of-process"}
    for codes in [[], ["orders.read"], ["sales.orders.read", "sales.orders.write"], ["echo2.items-v2.read"]]:
        assert (
            list(read_ok(manifest_json(**plugin, contributes={"permissions": codes})).contributes.permissions) == codes
        )
    cases: list[tuple[list[str], str]] = [
        (["orders"], "$.contributes.permissions[0]"),
        (["Orders.read"], "$.contributes.permissions[0]"),
        (["orders.Read"], "$.contributes.permissions[0]"),
        (["orders..read"], "$.contributes.permissions[0]"),
        (["orders.read."], "$.contributes.permissions[0]"),
        ([".orders.read"], "$.contributes.permissions[0]"),
        (["1orders.read"], "$.contributes.permissions[0]"),
        (["orders.1read"], "$.contributes.permissions[0]"),
        (["orders_x.read"], "$.contributes.permissions[0]"),
        (["orders:read"], "$.contributes.permissions[0]"),
        # 重复时报第二次出现的项
        (["orders.read", "orders.read"], "$.contributes.permissions[1]"),
        (["orders.read", "orders.write", "orders.read"], "$.contributes.permissions[2]"),
    ]
    for codes, reason in cases:
        assert read_error(manifest_json(**plugin, contributes={"permissions": codes})) == invalid(reason), codes


def test_contributed_operations_follow_the_operation_id_grammar() -> None:
    # RFC-0002 解析规则 2（ADR-0233）：contributes.operations 的每一项符合操作 ID 文法，与 operation.is_id 同一条
    for operation_id in ["orders.create", "order-lines.list", "a1.b2", "items-v2.get-many"]:
        assert read_ok(manifest_json(contributes={"operations": [operation_id]})).operations == (operation_id,)
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
        assert read_error(manifest_json(contributes={"operations": [operation_id]})) == invalid(
            "$.contributes.operations[0]"
        ), operation_id


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
    for text in ["caching.distributed@^2", "caching.distributed@02", "caching.distributed@2147483648"]:
        assert CapabilityDeclaration.parse(text) is None, text
    assert manifest.is_interface_version("2147483647")
    for text in ["0", "01", "+1", " 1", "1\n", "2147483648", "^1"]:
        assert not manifest.is_interface_version(text), text


def test_cross_field_constraints_follow_the_validate_order() -> None:
    def build(**members: Any) -> Manifest:
        return Manifest(
            **{"id": "xihan.a.b", "version": "1.0.0", "primitives": 1, "stability": Stability.STABLE, **members}
        )

    twice = (CapabilityDeclaration("a.b", 1), CapabilityDeclaration("a.b", 2))
    cases: list[tuple[Manifest, str | None]] = [
        (build(kind=Kind.CAPABILITY), None),
        (build(kind=Kind.PLUGIN, provides=twice), manifest.MESSAGE_ISOLATION_REQUIRED),
        (build(kind=Kind.CAPABILITY, isolation=Isolation.WASM), manifest.MESSAGE_ISOLATION_NOT_ALLOWED),
        (build(kind=Kind.HOST, permissions=("capability:x",)), manifest.MESSAGE_ISOLATION_NOT_ALLOWED),
        (build(kind=Kind.PROVIDER, provides=twice), manifest.MESSAGE_DUPLICATE_PROVIDES),
        (
            build(
                kind=Kind.CAPABILITY,
                requires=(CapabilityRequirement("a.b", 1), CapabilityRequirement("a.b", 1, optional=True)),
            ),
            manifest.MESSAGE_DUPLICATE_REQUIRES,
        ),
        (
            build(kind=Kind.CAPABILITY, bindings=(manifest.Binding("db", "queue"), manifest.Binding("db", "blob"))),
            manifest.MESSAGE_DUPLICATE_BINDINGS,
        ),
    ]
    for built, key in cases:
        error = built.validate().error
        assert (None if error is None else error.message_key) == key, key
        if error is not None:
            assert error.code == ERROR_CODE_INVALID_ARGUMENT
            assert error.details == {"id": "xihan.a.b"}
    # 读取时把码换成 KERNEL_CAPABILITY_006，键与参数不变
    assert read_error(manifest_json(isolation="wasm")) == (
        manifest.ERROR_CODE_INVALID_MANIFEST,
        manifest.MESSAGE_ISOLATION_NOT_ALLOWED,
        {"id": "xihan.a.b"},
    )


def test_plugins_of_any_isolation_may_declare_pipeline_contributions() -> None:
    # 只有 Wasm 插件的中间件生效，由宿主在发现插件时核对（PLUGINS_VALIDATION_001）；读取不拒绝（RFC-0002，ADR-0236）
    audit = [{"middleware": "audit", "stage": "observe"}, {"middleware": "tenant-guard", "stage": "tenant"}]
    for isolation in Isolation:
        parsed = read_ok(manifest_json(kind="plugin", isolation=isolation.value, contributes={"pipeline": audit}))
        assert parsed.contributes.pipeline[1] == PipelineContribution("tenant-guard", PipelineStage.TENANT)


def test_the_model_checks_each_field() -> None:
    def build(**members: Any) -> Manifest:
        return Manifest(
            **{
                "id": "xihan.a.b",
                "version": "1.0.0",
                "primitives": 1,
                "stability": Stability.STABLE,
                "kind": Kind.CAPABILITY,
                **members,
            }
        )

    built = build(activation=["onOperation:orders.*"])
    assert built.activation == ("onOperation:orders.*",)
    assert not built.activates_on_startup
    assert build().activation == (manifest.ON_STARTUP,)
    assert build().contributes == Contributions()
    with pytest.raises(AttributeError):
        built.id = "xihan.c.d"  # type: ignore[misc]
    invalid_fields: list[dict[str, Any]] = [
        {"id": "a.b"},
        {"version": "1.0"},
        {"primitives": 0},
        {"primitives": manifest.MAX_INTERFACE_VERSION + 1},
        {"activation": ()},
        {"activation": ("onBoot",)},
        {"permissions": ("sudo",)},
        {"provides": (CapabilityDeclaration("a.b", 0),)},
        {"contributes": Contributions(pipeline=(PipelineContribution("", PipelineStage.OBSERVE),))},
        {"contributes": Contributions(permissions=("orders.read", "orders.read"))},
        {"contributes": Contributions(health=("Echo",))},
    ]
    for members in invalid_fields:
        with pytest.raises(FrameworkError) as raised:
            build(**members)
        assert raised.value.code == ERROR_CODE_INVALID_ARGUMENT, members


def test_read_file_skips_the_bom_and_reports_missing_files(tmp_path: Path) -> None:
    path = tmp_path / manifest.FILE_NAME
    path.write_bytes(b"\xef\xbb\xbf" + ECHO_MANIFEST.encode("utf-8"))
    assert manifest.read_file(path).value_or_raise().id == "xihan.samples.echo"
    # read 按原样检查文本，开头的 BOM 是语法错误；read_file 才跳过
    assert read_error(path.read_bytes())[1] == manifest.MESSAGE_INVALID_JSON
    path.write_bytes(b"\xef\xbb\xbf\xff")
    assert manifest.read_file(path).error == manifest.read("\xff".encode("latin-1")).error
    missing = tmp_path / "missing.json"
    error = manifest.read_file(missing).error
    assert error is not None
    assert (error.code, error.message_key, dict(error.details or {})) == (
        manifest.ERROR_CODE_INVALID_MANIFEST,
        manifest.MESSAGE_FILE_MISSING,
        {"path": str(missing)},
    )
