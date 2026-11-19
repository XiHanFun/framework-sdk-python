---
name: xihan-sdk-python-testing-release
description: 运行或排查 Python SDK 的门禁、向量覆盖测试、黑盒一致性测试与分发包验证时使用，适用于 ruff、mypy、pytest、生成代码核对与 python -m build；不负责实现功能，版本与发布见 version-release。
---

# Python SDK 测试与打包验证

先读 `references/gates.md`；`.github/workflows/ci.yml` 与 `pyproject.toml` 是事实源。

## 准备

```bash
git submodule update --init
python -m venv .venv                 # Windows 上用 py -3.13 -m venv .venv
python -m pip install -e ".[plugin]" --group dev
```

`--group` 需要 pip 25.1 以上。之后的命令都用虚拟环境里的解释器（Windows 上 `.venv/Scripts/python`，Unix 上 `.venv/bin/python`）。

## 标准验证

```bash
python scripts/generate_protocol.py
git diff --exit-code -- src/xihan_framework/plugin/v1
ruff check && ruff format --check && mypy && pytest
python -m build
```

- 先跑受影响的测试文件（`pytest tests/test_<模块>.py`），再跑全部。
- 测试读取 `spec/conformance/vectors/`，子模块没有检出时失败而不是跳过。
- 黑盒一致性测试需要 Go（版本见 `spec/conformance/go.mod`）；没有 Go 时失败，确实要跳过时设 `XIHAN_CONFORMANCE_REQUIRED=0`，并在结果里说明。
- Windows 上发 SIGTERM 的用例跳过，属于预期；Linux 上的 CI 会跑它。
- 不删除断言、不放宽 ruff / mypy / pytest 配置或 CI 门禁来制造通过。

## 打包验证

`python -m build` 生成 sdist 与 wheel（hatchling），然后按 `references/gates.md` 核对包内容：`spec/`、`tests/`、`examples/` 不进分发包，`py.typed`、协议代码的类型存根与消息目录在 wheel 里。README.md 是 PyPI 的项目说明，改它之后也要构建一次。仓库没有配置 `twine check`。

上传 PyPI 属于外部状态变更，只在用户明确要求时由用户的凭据执行，见 version-release。
