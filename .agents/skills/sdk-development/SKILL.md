---
name: xihan-sdk-python-development
description: 设计、实现、重构或对齐 Python SDK 的原语、清单、调用上下文、管道与操作契约时使用，适用于按规范补齐行为、回放新向量和与 .NET 参考实现对齐；进程外插件运行时用 plugin-runtime，门禁与打包用 testing-release，发版用 version-release。
---

# Python SDK 开发

代码约定的细节读 `references/conventions.md`。

## 先查事实

```bash
git status --short --branch
git log -5 --oneline
git submodule status
```

按顺序读：相关 RFC（`spec/rfcs/`）→ `spec/conventions/` 的登记（错误码、Baggage 键、能力 ID、术语）→ `spec/conformance/vectors/` 里对应的向量 → 目标模块与测试。只读与任务相关的部分。

| 模块 | 规范 | 测试 |
| --- | --- | --- |
| `primitives` | RFC-0001、RFC-0010、`conventions/` | `test_primitives.py`、`test_kernel_messages.py` |
| `manifest` | RFC-0002、`manifest/manifest.schema.json` | `test_manifest.py` |
| `invocation` | RFC-0003 | `test_invocation.py` |
| `pipeline` | RFC-0004 | `test_pipeline.py` |
| `operation` | RFC-0011、`conventions/operation-ids.md` | `test_operation.py`、`test_codec.py` |

## 与 .NET 一致

- 规范是事实源，行为与 XiHan.Framework 一致；结果取决于平台内部的地方（数字写法、格式说明符、区域符号、JSON 读法）以规范写死的为准（ADR-0236），不用 Python 内建行为凑。
- 线上可见的文本与向量逐字相同：错误码、消息键、内核英文模板、JSON 路径、写出的清单。
- 可恢复失败返回 `Result`，配置与编程错误抛 `FrameworkError`；取消与截止时间只经信号通知（RFC-0003 规则 1）。
- 核心五块零运行时依赖，共用的内部实现（`_json.py`、`_codec.py`、`_whitespace.py`）先复用再新增。

## 规范没写到时

停下来，不自行发明：写清现状、.NET 的做法与各候选的后果，提出规范文字，在 framework-spec 提交。公开契约的顺序是 framework-spec 的 RFC → XiHan.Framework 的 ADR（`plans/adr`）→ 各 SDK；规范落地、指针升上来之后再改实现并回放向量。

## 完成条件

1. 行为与规范和向量一致；新回放的向量文件登记在 `tests/test_vectors.py` 的 `REPLAYED`，认领的档里没回放的列进 `GAPS` 并写理由。
2. `ruff check`、`ruff format --check`、`mypy`、`pytest` 全部通过。
3. 公开名经子包 `__init__.py` 的 `__all__` 导出，全部模块、类与函数有中文 docstring。
4. 公开 API 或行为变化同步 README 两份（只限概览）与文档站 `docs/sdks/python.md`（含「破坏性改动与迁移」）。
5. 一项能力一个提交：`<type>(<scope>): <中文说明>`；规范指针的升级与它带来的实现在同一提交。
