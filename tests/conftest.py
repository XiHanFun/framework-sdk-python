"""测试的共用工具：读取 spec/ 子模块里的一致性向量。"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SPEC = Path(__file__).resolve().parent.parent / "spec"


def _load_vector(group: str, name: str) -> Any:
    """读取 ``spec/conformance/vectors/<group>/<name>.json``；子模块没有检出时让测试失败而不是跳过。"""
    path = SPEC / "conformance" / "vectors" / group / f"{name}.json"
    if not path.is_file():
        pytest.fail(f"The conformance vector '{path}' is missing; run 'git submodule update --init'.")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def vector() -> Callable[[str, str], Any]:
    """按组名与向量名读取一致性向量，不复制向量文件。"""
    return _load_vector


def _utf16_text(units: list[str]) -> str:
    """按 UTF-16 码元组字符串（RFC-0012「一致性」）：成对的代理项合成一个码点，孤立的留作代理项码点。"""
    return b"".join(bytes.fromhex(unit) for unit in units).decode("utf-16-be", "surrogatepass")


@pytest.fixture
def utf16() -> Callable[[list[str]], str]:
    """向量里以 ``utf16`` 列出码元的样例组成字符串：JSON 字符串写不出孤立的代理项。"""
    return _utf16_text
