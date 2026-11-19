---
name: xihan-sdk-python-commit-msg
description: 根据 Python SDK 当前暂存区生成一行提交信息时使用，适用于“提交信息”“commit msg”“msg”或提交前概括 staged changes；只生成消息，不负责暂存、解决冲突或执行提交。
---

# Python SDK 提交信息

1. 检查 `git status --short --branch`；存在冲突、merge 或 rebase 时先报告，不生成完成态消息。
2. 只读取 `git diff --cached --stat`、完整 staged diff 和最近 20 条提交。暂存区为空时不从未暂存内容猜测。
3. 将全部暂存变化归纳成一个意图，输出一行 Conventional Commit。

格式：`<type>(<scope>): <中文说明>`。type 使用 `feat`、`fix`、`refactor`、`perf`、`docs`、`style`、`test`、`build`、`ci`、`chore`、`revert`。

scope 用本仓库的模块名：`primitives`、`manifest`、`invocation`、`pipeline`、`operation`、`plugin`，一致性门禁用 `conformance`，或 `spec`（只升规范指针）、`docs`、`build`、`ci`；跨多个模块的基础能力可省略 scope，不逐项罗列。

说明写使用者可感知的结果，跟随规范的改动写明 RFC 或 ADR 编号，升了规范指针时写「规范指针升到 <短哈希>」。删除、改名或改变公开 API、线上编码与默认行为时使用 `!`。只要求消息时最终只输出一行标题，不附解释或候选列表。
