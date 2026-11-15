"""包级信息的测试。"""

import importlib.metadata

import xihan_framework


def test_version_matches_the_distribution_metadata() -> None:
    assert xihan_framework.__version__ == importlib.metadata.version("xihan-framework")
    assert xihan_framework.SPEC_VERSION == "1"
