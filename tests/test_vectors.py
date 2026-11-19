"""认领的一致性档：档要求的每个向量要么由本 SDK 的测试回放，要么列为缺口并写明理由（RFC-0024「一致性档」，ADR-0242）。"""

import json
from pathlib import Path

import pytest

CONFORMANCE = Path(__file__).resolve().parent.parent / "spec" / "conformance"
VECTORS = CONFORMANCE / "vectors"
#: 规范的一致性档表，档名 → 规范文件、必须回放的向量与必须通过的运行器子命令。
PROFILES = CONFORMANCE / "profiles.json"
#: 本 SDK 认领的一致性档：``core`` 所有实现都认领，``plugin`` 是写进程外插件的一侧。
CLAIMED_PROFILES = ("core", "plugin")
#: 认领的档要求回放、本 SDK 却没有回放的向量文件 → 理由（RFC-0024「实现钉规范」第 3 条）；目前没有缺口。
GAPS: dict[str, str] = {}
#: 本 SDK 的测试回放的向量文件（相对 ``vectors/``，``/`` 分隔），都经 conftest 的 ``vector`` 读取。
REPLAYED = frozenset(
    {
        "manifest/manifests.json",  # test_manifest
        "operations/payload-reasons.json",  # test_operation
        "pipeline/ordering.json",  # test_pipeline
        "plugin/callback-status.json",  # test_plugin_protocol
        "plugin/deadlines.json",  # test_plugin_protocol
        "plugin/error-reading.json",  # test_plugin_protocol
        "plugin/handshake-lines.json",  # test_plugin_protocol
        "plugin/host-addresses.json",  # test_plugin_protocol
        "plugin/plugin-messages.json",  # test_plugin_protocol
        "plugin/protocol-versions.json",  # test_plugin_protocol
        "primitives/baggage-keys.json",  # test_primitives、test_invocation
        "primitives/capability-requirements.json",  # test_manifest
        "primitives/error-codes.json",  # test_primitives
        "primitives/json-text.json",  # test_primitives
        "primitives/kernel-messages.json",  # test_kernel_messages
        "primitives/lifecycle-transitions.json",  # test_primitives
        "primitives/message-templates.json",  # test_primitives
        "primitives/trace-context.json",  # test_invocation
        "primitives/wire-encoding.json",  # test_primitives
    }
)


def test_every_vector_of_the_claimed_profiles_is_replayed() -> None:
    # 认领的档都有定义，它们要求的向量要么被回放、要么列为缺口，缺口只能是认领的档要求而没有回放的向量，回放的文件都存在；
    # 规范在认领的档里新增向量时这里失败，而不是悄悄不回放。回放认领的档以外的向量是允许的
    if not PROFILES.is_file() or not VECTORS.is_dir():
        pytest.fail(
            f"The conformance profiles or vectors under '{CONFORMANCE}' are missing; run 'git submodule update --init'."
        )
    table = json.loads(PROFILES.read_text(encoding="utf-8"))
    profiles: dict[str, list[str]] = {profile["id"]: profile["vectors"] for profile in table["profiles"]}
    existing = {path.relative_to(VECTORS).as_posix() for path in VECTORS.rglob("*.json") if path.is_file()}
    # 未知的档单独报出，其余的档照常核对，免得一个错的档名盖住别的问题
    required = {vector for profile in CLAIMED_PROFILES if profile in profiles for vector in profiles[profile]}
    problems: list[str] = []
    if undefined := sorted(set(CLAIMED_PROFILES) - profiles.keys()):
        problems.append(f"claimed profiles that the spec does not define: {', '.join(undefined)}")
    if uncovered := sorted(required - REPLAYED - GAPS.keys()):
        problems.append(
            f"vector files of the claimed profiles that are neither replayed nor listed as gaps: {', '.join(uncovered)}"
        )
    if outside := sorted(GAPS.keys() - required):
        problems.append(f"gaps outside the claimed profiles: {', '.join(outside)}")
    if replayed_gaps := sorted(GAPS.keys() & REPLAYED):
        problems.append(f"gaps that are replayed; remove them from gaps: {', '.join(replayed_gaps)}")
    if missing := sorted(REPLAYED - existing):
        problems.append(f"replayed vector files that the spec does not have: {', '.join(missing)}")
    assert not problems, "\n".join(problems)
