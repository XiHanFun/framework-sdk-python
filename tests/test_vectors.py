"""一致性向量的全集：规范里的每个向量文件要么由本 SDK 的测试回放，要么显式豁免并写明理由（ADR-0242）。"""

from pathlib import Path

import pytest

VECTORS = Path(__file__).resolve().parent.parent / "spec" / "conformance" / "vectors"
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
#: 本 SDK 不回放的向量文件 → 理由。
EXEMPTED = {
    "plugin/wasm-exports.json": "Wasm ABI exports are checked by hosts; this SDK has no Wasm plugin runtime.",
    "provider/caching-distributed.json": "This SDK does not provide the caching.distributed@1 remote provider.",
    "security/webauthn.json": "WebAuthn verification is host-side (RFC-0008); this SDK does not verify credentials.",
}


def test_every_spec_vector_is_replayed_or_exempted() -> None:
    # 两个集合不相交、列出的文件都存在、存在的文件都在其一；规范新增或删除向量文件时这里失败，而不是悄悄不回放
    if not VECTORS.is_dir():
        pytest.fail(f"The conformance vectors '{VECTORS}' are missing; run 'git submodule update --init'.")
    existing = {path.relative_to(VECTORS).as_posix() for path in VECTORS.rglob("*.json") if path.is_file()}
    listed = REPLAYED | EXEMPTED.keys()
    problems: list[str] = []
    if both := sorted(REPLAYED & EXEMPTED.keys()):
        problems.append(f"vector files both replayed and exempted: {', '.join(both)}")
    if neither := sorted(existing - listed):
        problems.append(f"spec vector files neither replayed nor exempted: {', '.join(neither)}")
    if missing := sorted(listed - existing):
        problems.append(f"listed vector files that the spec does not have: {', '.join(missing)}")
    assert not problems, "\n".join(problems)
