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
