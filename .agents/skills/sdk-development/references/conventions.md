# 代码约定

以 `pyproject.toml` 的 ruff / mypy / pytest 配置和现有代码为准。

## 结构

- 每个子包（`primitives`、`manifest`、`invocation`、`pipeline`、`operation`、`plugin`）的 `__init__.py` 重新导出公开名并列出 `__all__`；实现放在 `_` 开头的私有模块（`_error.py`、`_context.py`、`_operation.py`）。
- 跨子包共用的内部实现放在包根：`_json.py`（JSON 文本的共同读取检查、值模型与写出）、`_codec.py`（值与 JSON 值之间的编解码）、`_whitespace.py`（RFC-0001「空白字符」的 25 码位表）。
- `__init__.py` 的 `__version__` 是版本真源；`SPEC_VERSION` 是实现的原语层大版本（RFC-0001），清单的 `primitives` 与它比较。
- `py.typed` 随 wheel 发布；公开 API 都有完整的类型标注，mypy strict 覆盖 `src`、`tests`、`examples`、`scripts`。

## 文档字符串与注释

- 模块 docstring 首行写这个模块对应的规范（如「调用上下文（RFC-0003）：……」）；类、函数与方法的 docstring 首行一句话，细节另起段落；代码与标识用 reStructuredText 的双反引号包住。
- 常量与模块级变量用 `#:` 文档注释。
- `#` 注释写为什么：核对顺序、状态迁移、取消与截止时间的归因、为什么不用 `json.loads` 或 `str.isspace` 这类内建函数、平台差异；句末不加「。」。
- 注释与 docstring 引用规范位置的写法：`RFC-0011 规则 9`、`invocation.md §5`、`lifecycle.md §7.3`、`ADR-0236`。
- 测试函数用英文句子命名（`test_the_runtime_catalog_has_every_locale_with_the_same_keys_and_placeholders`），不写 docstring；测试辅助函数与 fixture 写中文 docstring。

## 运行时文本

- `FrameworkError` / `ValueError` 的消息、日志、`pytest.fail` 与断言文本用英文；句子以句号结尾，标识符与路径用单引号（`The message key '{key}' must match <domain>.<segment>[.<segment>].`）。
- 面向人的错误文案不拼在代码里：`Error` 只带消息键与参数，按 RFC-0010 的模板经目录解析；`Error.message` 先找 `set_default_messages` 安装的目录，再找 `BUILT_IN_CATALOG`，都没有时是 `键 (参数)`。
- 插件运行时的消息目录 `plugin/Localization/<区域>.json` 有 10 种区域（`en-US`、`zh-CN`、`zh-TW`、`ja-JP`、`ko-KR`、`de-DE`、`es-ES`、`fr-FR`、`pt-BR`、`ru-RU`），新增键要补齐 10 个文件，键集与占位符与 `en-US` 一致（`test_plugin_protocol.py` 核对）。
- 源码里 `Error.literal` 只用于读入线上的自由文本错误（`plugin/_convert.py`）与 `FrameworkError.to_error`。

## 错误

- 可恢复失败返回 `Result[T]`：`ok(value)`、`success()`、`fail(Error...)`；不抛异常。
- 配置与编程错误（非法参数、清单字段不合格式、管道约束成环、操作重复登记）抛 `FrameworkError`，它是 `ValueError` 的子类，带错误码；需要上线时经 `to_error()` 转成 `Error`。
- 错误码文法 `<DOMAIN>_<KIND>_<NNN>`，领域大写；新码先登记到规范的 `conventions/error-codes.md`。

## 值类型与集合

- 值类型不可变：`@dataclass(frozen=True)`，或带 `__slots__`、`with_*` 返回新值的类（`Error`、`Context`）；清单模型构造时校验各字段，`validate()` 核对字段之间的约束。
- 常量大写并标 `Final`；正则预编译并一律用 `fullmatch`。
- 集合要有上限：`operation.Registry` 容量 10000，Baggage 值最多 256 个 UTF-16 码元，JSON 文本嵌套超过 64 层即不合法；不建无界的缓存、队列或注册表。

## 异步、取消与截止时间

- 处理器与中间件都是 `async def`。
- 取消与截止时间只经 `Context` 的信号通知（RFC-0003 规则 1）：调用器不取消处理器的任务；处理器读 `ctx.is_cancelled`、`await ctx.cancellation.wait()` 或调 `ctx.cancellation.raise_if_cancelled()` 自己停下。
- 抛出取消或写下 `KERNEL_CANCELLED_001` 时，截止时间已到即 `KERNEL_TIMEOUT_001`，否则 `KERNEL_CANCELLED_001`；照常返回的结果就是调用的结果。

## JSON 与文法

- 读线上文本不用 `json.loads`（它接受 `NaN`、孤立的代理项、重复成员与很深的嵌套），走 `_json` 按 RFC 8259 的读法；数字按值模型往返（`JsonNumber` 保留原文）。
- 空白用 `_whitespace` 的表，不用 `str.isspace`（它另把 U+001C–U+001F 算作空白）。

## 工具

- ruff：规则集 `E W F I N B UP C4 SIM PIE RET PT ASYNC RUF`，行宽 120；`E501`（中文 docstring 与注释不强行折行）与 `RUF001`–`RUF003`（中文全角标点）有意忽略；生成的 `plugin/v1/plugin_pb2*` 排除在外。
- `# noqa` 与 `# type: ignore` 只在有理由的地方带具体规则码（如 `plugin/__init__.py` 先核对可选依赖再导入的 `E402`），不整文件关闭检查。
- 编辑器配置见 `.editorconfig`：UTF-8、LF、末尾换行；Python 缩进 4，其余 2。
