---
name: xihan-sdk-python-plugin-runtime
description: 实现、修复或对齐 Python SDK 的进程外插件运行时（xihan.plugin.v1，grpc.aio）时使用，适用于握手、生命周期、调用与投递、宿主回调、协议代码生成和黑盒运行器；原语与操作契约本身用 sdk-development，发版用 version-release。
---

# Python SDK 插件运行时

黑盒运行器、平台差异与测试分工的细节读 `references/runtime.md`。

## 规范与实现

| 主题 | 规范 | 实现（`src/xihan_framework/plugin/`） |
| --- | --- | --- |
| 协议总述、错误码 | `spec/rfcs/0017-plugin-protocol.md` | `_messages.py` |
| 环境变量、端点、握手行、回调端点列表 | `spec/plugin/handshake.md` | `_protocol.py` |
| 生命周期、配置、健康、停止与收尾 | `spec/plugin/lifecycle.md` | `_lifecycle.py`、`_runtime.py`、`_configuration.py` |
| 调用、投递、元数据、错误读写、回调、日志、消息键 | `spec/plugin/invocation.md`；截止时间文本的文法在 `spec/rfcs/0003-invocation-context.md` 规则 8 | `_runtime.py`、`_convert.py`、`_host.py`、`_log.py`、`_messages.py` |
| 协议消息与服务 | `spec/plugin/plugin.proto` | `v1/`（生成，不手改） |
| 作者面对的类型 | `spec/plugin/` | `_model.py` |

向量在 `spec/conformance/vectors/plugin/`，由 `tests/test_plugin_protocol.py` 回放。

## 协议代码

`v1/` 里的消息、服务与 mypy-protobuf 生成的类型存根由 `spec/plugin/plugin.proto` 生成并提交。协议变了（通常随规范指针升级）就重新生成：

```bash
python scripts/generate_protocol.py
git diff --exit-code -- src/xihan_framework/plugin/v1
```

生成器 grpcio-tools 与 mypy-protobuf 在开发依赖组里锁定版本；CI 在 Linux 上重新生成并核对没有差异。生成物不经 ruff 格式化与检查，mypy 按它们的 `.pyi` 检查。

## 依赖边界

grpcio 与 protobuf 只在 extra `plugin` 里，核心五块不依赖它们；`plugin/__init__.py` 先核对可选依赖，没装时抛出给出安装命令（`pip install "xihan-framework[plugin]"`）的 `ModuleNotFoundError`。

## 完成条件

1. 行为与 `spec/plugin/` 的条文和插件向量一致；规范没写到的停下来报告，提出规范文字。
2. `tests/test_plugin_runtime.py` 覆盖改到的每一条；涉及进程入口、退出码或收尾时限时改 `tests/test_plugin_process.py`。
3. 黑盒运行器的全部用例通过（`tests/test_plugin_conformance.py`，CI 在 Linux 与 Windows 上都跑）。
4. 全部门禁通过；文档站 `docs/sdks/python.md` 的「写一个插件」与 README 的模块表同步。
