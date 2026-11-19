---
name: xihan-sdk-python-version-release
description: 为 Python SDK 准备版本、更新 src/xihan_framework/__init__.py 的版本、核对规范标签、构建并校验分发包或上传 PyPI 时使用；只有用户明确要求时才打标签、推送或上传，且上传用用户自己的凭据。
---

# Python SDK 版本发布

## 前置条件

- 工作区干净，无 merge/rebase、冲突或 detached HEAD；目标版本由用户给出，不替用户选版本级别或通道。
- 版本真源是 `src/xihan_framework/__init__.py` 的 `__version__`：hatchling 从这里读（`[tool.hatch.version]`），`primitives.SDK_VERSION` 与握手里的 SDK 版本也取它，`tests/test_package.py` 核对它与分发元数据一致。
- PyPI 包名 `xihan-framework`，导入名 `xihan_framework`；PyPI 上目前只有占名用的版本。
- 仓库没有发布工作流，也还没有标签。

## 与规范标签的关系（RFC-0024「实现钉规范」）

- 日常分支可以指向规范已推送的任意提交；发预发布或正式版时，`spec/` 子模块必须正好在某个规范标签上。
- 正式版只能钉规范的正式版标签（不带预发布后缀），预发布可以钉 rc 标签。
- 规范没有合适的标签时停下，请用户先在 framework-spec 打标签（同样要用户明确要求），再升指针。
- 发版前核对 README 两份的「状态」一节与文档页的「一致性」写明钉的规范版本、认领的档与缺口。

## 准备版本

每一步都在用户要求之后才做：

1. 在 `dev` 上改 `__version__`；文档页 `docs/sdks/python.md`（XiHan.Framework 仓库）的「破坏性改动与迁移」补这一版（见 changelog-collect）。
2. 跑全部门禁与 `python -m build`，按 testing-release 的 `references/gates.md` 核对包内容与版本。
3. 提交、推送 `dev` 并等 CI 全绿；`main` 只在用户明确说「推送并快进 main」时由 `dev` 快进。

## 发布

只有用户明确要求发布时才执行：打标签与推送标签、上传 PyPI。上传用用户自己的 PyPI 凭据，由用户执行或在用户授权下执行；不替用户输入令牌。PyPI 上同一版本号不能重新上传，发错了只能用下一个号。发布后核对 PyPI 上的版本、项目说明与安装（`pip install xihan-framework`）。
