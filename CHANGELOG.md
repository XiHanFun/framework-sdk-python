# 更新日志 · XiHan Framework SDK for Python

本页记录 `xihan-framework` 各版本中使用者可感知的变更，每条标注 **新增 / 修复 / 优化 / 调整 / 移除** 类别；仓库自身的 CI、测试与构建脚本不列入。版本号见 `src/xihan_framework/__init__.py`，详细说明见文档站 [Python SDK](https://framework.docs.xihanfun.com/sdks/python) 的「破坏性改动与迁移」。

## 未发布

### 升级须知

- `Result.to_json` 与 `Error.to_json` 遇到字符串里的代理项码点、从根算起嵌套超过 64 层或重复成员时抛 `ValueError`、不写出文本：写出之前去掉这类值（ADR-0320）
- 已登记 Baggage 键的值有代理项码点时，建上下文丢弃这个成员、`with_baggage` 抛 `FrameworkError`：从外部输入拼值之前先去掉（ADR-0321）
- dataclass 有两个字段写进 JSON 是同一个成员名（含 `json_field` 改名撞上别的字段）时，编码与以它（含嵌套在内的类型）为请求或响应类型注册操作抛 `TypeError`：改掉其中一个名字（ADR-0319、ADR-0340）
- 类型化响应有代理项码点、嵌套超过 64 层或重复成员时以 `KERNEL_UNEXPECTED_001` 结束、没有载荷：响应里不要放这类值（ADR-0318、ADR-0319）
- `Envelope.time` 改为带时区的 `datetime | None`，不带时区抛 `FrameworkError`、给字符串抛 `TypeError`：改传 `datetime`，要文本时自己 `isoformat()`（ADR-0306）
- `Contract` 的 `expose` 在构造时校验并存成 `operation.Expose`，单个字符串与不认识的名字抛 `FrameworkError`：写成元组 `(Expose.HTTP,)`
- 区域名按 RFC-0010 校验并归一，不合文法或只差大小写的两个区域名抛 `FrameworkError`：写成 `zh-CN` 这样的写法
- 操作 ID 超过 128 个字符时 `Contract`、`Manifest` 抛 `FrameworkError`，读清单为 `kernel.manifest.invalid`：把 ID 缩到 128 个字符以内（ADR-0276）
- `Envelope.extensions` 与 `Binding.properties` 不再是 `MappingProxyType`：判断只读映射改用 `collections.abc.Mapping`

### 变更

- **修复** 上下文的幂等键有代理项码点时，`Host.invoke` 与 `Host.publish` 在本地以 `plugins.process.failed` 失败、不发请求，不再抛 `UnicodeEncodeError`（ADR-0347）
- **修复** 插件写给宿主的自由文本错误文案（含内部错误的）有代理项码点时各换成 U+FFFD 写出，不再抛 `UnicodeEncodeError`、整个错误写不出（ADR-0339）
- **新增** `Host.invoke` 的载荷也收 `str`，发出之前严格编码成 UTF-8，有代理项码点时不发请求、以 `KERNEL_VALIDATION_001` + `kernel.operation.invalid_payload` 失败（ADR-0312）
- **修复** 回调客户端关闭之后，`Host.invoke` 与 `Host.publish` 在本地核对与载荷编码之后报 `plugins.host.unavailable`、不发请求，不再抛 grpc 的 `UsageError`（ADR-0328）
- **修复** 插件写给宿主的错误细节有代理项码点或超过 64 层时不带细节，其余字段照写（ADR-0320）
- **修复** `int` 的子类按十进制值写进 JSON，不取它覆写的 `__str__`（ADR-0319）
- **新增** `plugin.Binding.properties`：`Configure` 下发的绑定属性交给配置回调，`repr` 不带主值与属性值（ADR-0285）
- **新增** `Envelope.data_schema`、`Context.with_value` / `find_value`、`Result.match` / `raise_if_failed`、`Maybe.match`、`find_host`、`is_locale_name` / `validate_locale_name`、`operation.Expose` 与 `operation.MAX_ID_LENGTH`
- **修复** 调用状态按调用隔离，处理器里再调另一个操作或并发调用时不再互相覆盖
- **修复** 宿主写来的信封 `time` 读不出时投递以 `InvalidArgument` 拒绝；错误细节里的代理项码点当作没有细节（ADR-0301、ADR-0306）
- **修复** `JsonLogHandler` 写 `extra` 里的 `Binding.properties` 只写属性名（ADR-0299）
- **调整** 区域名的查找不区分大小写，清单 `activation` 里 `onEvent:` 的模式可以带下划线（ADR-0276）

## v0.2.0-rc.1（2026-11-21）

- **新增** 首个对外预发布版，发布到 PyPI（`__version__` 为 `0.2.0rc1`）：原语、清单、调用上下文、管道、操作契约与进程外插件运行时（extra `plugin`），钉规范 `v1.0.0-rc.1`，认领一致性档 `core` 与 `plugin`
