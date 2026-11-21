---
name: xihan-sdk-python-version-release
description: 为 Python SDK 准备版本、更新 src/xihan_framework/__init__.py 的版本、核对规范标签、构建并校验分发包或上传 PyPI 时使用；只有用户明确要求时才打标签、推送或上传，且上传用用户自己的凭据。
---

# Python SDK 版本发布

## 前置条件

- 工作区干净，无 merge/rebase、冲突或 detached HEAD；目标版本由用户给出，不替用户选版本级别或通道。
- 版本真源是 `src/xihan_framework/__init__.py` 的 `__version__`：hatchling 从这里读（`[tool.hatch.version]`），`primitives.SDK_VERSION` 与握手里的 SDK 版本也取它，`tests/test_package.py` 核对它与分发元数据一致。
- `__version__` 按 PEP 440 的规范写法（`tests/test_package.py` 要求它等于分发元数据里规范化之后的版本），标签按语义化版本写，与其他 SDK 一致：`v0.1.0` 对 `0.1.0`，`v0.1.0-alpha.N` / `-beta.N` / `-rc.N` 对 `0.1.0aN` / `0.1.0bN` / `0.1.0rcN`；别的预发布后缀写不成 PEP 440 的版本，不能用。
- PyPI 包名 `xihan-framework`，导入名 `xihan_framework`；PyPI 上最早的 `0.0.1` 是占名用的，不是本仓库发布的版本；目前只有预发布版，README 与文档页的安装命令带 `==<版本>` 占位，首个正式版发布后去掉。
- 标签写成 `v<版本>`，只从 `main` 打；推送 `v*` 标签由 `.github/workflows/release.yml` 发布到 PyPI。

## 发版核对与工作流

- `scripts/release-check.sh <标签> [<提交>]` 核对要打标签的那个提交（缺省为标签指向的提交，标签还没打时为 `HEAD`）：① 标签是 `v<语义化版本>`；② 提交在 `origin/main` 的历史里，预发布与正式版都只从 `main` 发；③ 标签换成 PEP 440 之后等于 `__version__`；④ `spec/` 正好在规范标签上。它要联网取 `origin/main` 与规范仓库的远端标签。
- 推送 `v*` 标签触发 `release.yml`：guard 用同一份脚本复核，不通过就不发布；通过后 `python -m build` 并经 `pypa/gh-action-pypi-publish` 上传（已有的文件跳过，重跑幂等）。工作流不建 GitHub Release，与 XiHan.Framework 相同，标签推送后手动建（见「发布」）。
- 认证走 PyPI 可信发布（OIDC），仓库里没有 PyPI 令牌。首次发布前要用户自己在 pypi.org 的项目 `xihan-framework` → Manage → Publishing 里加可信发布者：所有者 `XiHanFun`、仓库 `framework-sdk-python`、工作流 `release.yml`、环境留空。

## 与规范标签的关系（RFC-0024「实现钉规范」）

- 日常分支可以指向规范已推送的任意提交；发预发布或正式版时，`spec/` 子模块必须正好在某个规范标签上。
- 正式版只能钉规范的正式版标签（不带预发布后缀），预发布可以钉 rc 标签。
- 规范没有合适的标签时停下，请用户先在 framework-spec 打标签（同样要用户明确要求），再升指针。
- 发版前核对 README 两份的「状态」一节与文档页的「一致性」写明钉的规范版本、认领的档与缺口。

## 准备版本

每一步都在用户要求之后才做：

1. 在 `dev` 上改 `__version__`（PEP 440 的规范写法）；文档页 `docs/sdks/python.md`（XiHan.Framework 仓库）的「破坏性改动与迁移」补这一版（见 changelog-collect）。
2. 跑全部门禁与 `python -m build`，按 testing-release 的 `references/gates.md` 核对包内容与版本。
3. 提交、推送 `dev` 并等 CI 全绿；`main` 只在用户明确说「推送并快进 main」时由 `dev` 快进。

## 发布

只有用户明确要求才打标签：

1. 跑 `bash scripts/release-check.sh vX.Y.Z main`，全部通过才往下走；不通过就停下报告。
2. `git tag -a vX.Y.Z -m "vX.Y.Z" main`，再 `git push origin vX.Y.Z`，用用户自己的权限。
3. 等 `release.yml` 跑完，核对 PyPI 上的版本、项目说明与安装（`pip install "xihan-framework==<版本>"`）；失败就停下报告。
4. 用户要建 GitHub Release 时：`gh release create vX.Y.Z --verify-tag --title vX.Y.Z --notes-file <正文>`，预发布加 `--prerelease`，正文写钉的规范标签与这一版的变化，与 XiHan.Framework 相同。

PyPI 上同一版本号不能重新上传，发错了只能用下一个号。guard 不通过时没有发布，删掉标签、改正之后重打即可。需要绕过工作流手动上传时，用用户自己的 PyPI 凭据，由用户执行；不替用户输入令牌。
