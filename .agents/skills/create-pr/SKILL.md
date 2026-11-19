---
name: xihan-sdk-python-create-pr
description: 为 Python SDK 准备、审查或创建 Pull Request 时使用，适用于生成 PR 标题与正文、比较分支或执行 gh pr create；普通代码审查不使用。
---

# Python SDK 创建 PR

## 前置检查

- 检查分支、上游、工作区、merge/rebase 和冲突；非正常分支状态不创建 PR。
- base 默认 `dev`，不默认改成 `main`；`main` 只由 `dev` 快进。
- 用 `git log <base>..HEAD`、`git diff --stat <base>...HEAD` 和完整 diff 审查整条分支。
- 核对公开名（`__all__`）、docstring、`pyproject.toml` 的依赖与 extra、规范指针、测试、README 两份与文档页是否同步。

## PR 内容

仓库没有 PR 模板，正文按以下几段写：

- **关联 Issue**：`Closes #<编号>`，没有就写「无」。
- **变更说明**：使用者可感知的结果，跟随规范的改动写 RFC 或 ADR 编号。
- **影响范围**：涉及的模块（`primitives`、`manifest`、`invocation`、`pipeline`、`operation`、`plugin`）与是否升了规范指针，不堆文件名。
- **自测**：只写实际执行过的命令（`ruff check`、`ruff format --check`、`mypy`、`pytest`、生成代码核对、`python -m build`）与平台。
- **破坏性变更**：公开 API、线上编码、默认行为或依赖的变化与迁移方式；没有就写「无」。

标题使用 Conventional Commit 形态。

只要草稿时不写远程。明确要求创建 PR 时才允许推送当前分支并执行 `gh pr create`；禁止 force push、合并或改写其他远端分支。创建后返回 PR 链接。
