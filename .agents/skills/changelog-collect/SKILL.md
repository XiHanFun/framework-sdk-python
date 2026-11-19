---
name: xihan-sdk-python-changelog-collect
description: 收集 Python SDK 两个提交或标签之间的用户可感知变化，写进文档站 Python SDK 页「破坏性改动与迁移」时使用；它不修改版本、不打标签，也不上传 PyPI。
---

# Python SDK 变更整理

## 事实源

- 用户指定的起止引用；未指定时先列出最近的标签（仓库还没有标签时用首个提交）和当前 HEAD。
- 范围内的提交（`git log --oneline <起>..<止>`）、实际 diff、公开名（各子包 `__init__.py` 的 `__all__`）与 `pyproject.toml` 的依赖和 extra。
- 范围内规范指针的变化（`git diff <起>..<止> -- spec`）与对应的规范 `CHANGELOG.md`。
- 去处：XiHan.Framework 仓库（本地 `../XiHan.Framework`）的 `docs/sdks/python.md`「破坏性改动与迁移」一节。

## 收录规则

- 只收录 SDK 使用者可感知的变化：公开 API、默认行为、错误码与消息键、线上编码、插件协议行为、依赖与 extra、支持的 Python 版本。
- 纯测试、CI、内部重构与生成代码的重新生成不单独收录，除非改变了包内容或使用方式。
- 每条以使用者结果表达，写明模块与 API 名，不罗列内部文件；破坏性变化给出迁移步骤。
- 跟随规范的变化写明 RFC 或 ADR 编号。
- 这一节可以按版本分条；文档页的其余正文不写版本号，任何地方都不写提交哈希与测试数量。

文档页是另一个仓库，单独提交；构建在那边执行 `pnpm --dir docs build`。本技能不改 `__version__`、不构建、不发布。
