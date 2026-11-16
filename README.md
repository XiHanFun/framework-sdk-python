# XiHan Framework SDK for Python

[XiHan Framework Spec](https://github.com/XiHanFun/framework-spec) 的 Python 实现：原语层、清单、调用上下文、管道与操作契约。语义与线上编码以规范为准（各语言 SDK 共用的线上细节、模板与内核错误由 ADR-0233 定下），与 .NET 参考实现（[XiHan.Framework](https://github.com/XiHanFun/XiHan.Framework)）一致，形状参照 Go SDK（[framework-sdk-go](https://github.com/XiHanFun/framework-sdk-go)）；规范以 submodule 放在 `spec/`，一致性由规范仓库的向量验证。面向 Python 应用、脚本型插件与 AI 工具链，Python 3.12+，运行时零依赖，处理器与中间件基于 asyncio。

```bash
pip install xihan-framework
```

PyPI 上目前只有占名用的 0.0.1；0.1.0 尚未发布，现在从源码安装（见「开发」）。导入名是 `xihan_framework`。

| 包 | 内容 | 规范 |
| --- | --- | --- |
| `primitives` | `Error` / 错误码、`Result[T]`、`Maybe[T]`、`TypedId[V]`、`LifecycleState`、`PipelineStage`、`Stability`、Baggage 键、消息键与模板、`Catalog`、`FrameworkError` | RFC-0001、RFC-0010、`conventions/` |
| `manifest` | `xihan.manifest.json` 的模型（冻结的 dataclass，构造即校验）、读取、规范化输出与校验，能力声明与依赖文本 | RFC-0002、`manifest/manifest.schema.json` |
| `invocation` | 不可变的 `Context`：截止时间、可跨线程触发的 `Cancellation`、W3C 追踪（上游的 trace-flags 原样保留、采样只看最低位，没有上游时写出 `01`）、已登记的 Baggage（已登记键的值非空、按 UTF-16 码元计最多 256、不含控制字符，不合的成员在构造或派生上下文时丢弃，幂等键除外）、类型化键的对象袋；`Context.run` 以 `asyncio.timeout` 与取消信号执行协程，超时与取消翻译为 `KERNEL_TIMEOUT_001` / `KERNEL_CANCELLED_001` | RFC-0003 |
| `pipeline` | 十一阶段的 `Builder`：`use` / `use_at` / `use_named_instance`、`before` / `after`（按类型名或实例名，约束主体在声明时必须已登记）、`describe` / `dump`、`build`；排序规则与错误码 `KERNEL_PIPELINE_00x` | RFC-0004 |
| `operation` | `Contract`、`Schema`、异步处理器、容量 10000 的 `Registry`、经完整管道执行的 `Invoker`（XGI：JSON 请求与响应）与类型化入口 `invoke_typed` | RFC-0011 |

## 用法

```python
import asyncio
from dataclasses import dataclass

from xihan_framework import operation, pipeline
from xihan_framework.invocation import Context
from xihan_framework.primitives import BAGGAGE_TENANT, Error, PipelineStage, Result, Stability, Unit, fail, ok


@dataclass(frozen=True)
class CreateOrder:
    quantity: int


@dataclass(frozen=True)
class OrderCreated:
    id: str
    tenant: str | None


async def create_order(ctx: Context, request: CreateOrder) -> Result[OrderCreated]:
    if request.quantity <= 0:
        return fail(
            Error.validation("ORDERS_VALIDATION_001", "orders.quantity.invalid", {"quantity": request.quantity})
        )
    return ok(OrderCreated(id="o-1", tenant=ctx.tenant))


async def observe(ctx: Context, next_step: pipeline.Step) -> Result[Unit]:
    outcome = await next_step(ctx)
    print(operation.current(ctx), outcome)
    return outcome


async def main() -> None:
    registry = operation.Registry()
    registry.add(
        operation.Contract("orders.create", stability=Stability.EXPERIMENTAL, expose=(operation.EXPOSE_HTTP,)),
        create_order,
        request_type=CreateOrder,
        response_type=OrderCreated,
    )
    builder = pipeline.Builder().use_at(pipeline.func("Observe", observe), PipelineStage.OBSERVE)
    invoker = operation.Invoker(registry, builder)

    ctx = Context(timeout=2, baggage={BAGGAGE_TENANT: "t1"})
    response = await invoker.invoke(ctx, operation.Request("orders.create", b'{"quantity":2}'))
    print(response.outcome.is_ok, response.payload)  # True b'{"id":"o-1","tenant":"t1"}'


asyncio.run(main())
```

1. 可恢复失败返回 `fail(Error...)`，不抛异常；错误码按 `<领域大写>_<KIND>_<NNN>`，文案用消息键（最长 128 个字符）+ 参数，`Error.message` 先经 `set_default_messages(Catalog(...), locale)` 安装的目录解析，再找 SDK 自带的内核错误英文模板（`BUILT_IN_CATALOG`，与规范向量 kernel-messages 逐字相同，如 `Operation 'orders.create' is not registered.`），都解析不了时是 `键 (参数)`。模板按 RFC-0010「模板」：`{name}` / `{0}`，`{{` `}}` 转义，可移植的格式说明符 `D`、`X` / `x`、`F`、`N` 按不变文化格式化（`F` 与 `N` 按精确值舍入、正好一半取偶，`{n:F2}` 取 0.125 得 `0.12`），其他说明符（含 printf 风格的 `%05d`）按无格式输出；值的文本形式是布尔 `true` / `false`、`None` 空串、浮点在 1e-4 ≤ |x| < 1e17 时写最短往返的定点形式（整数值不带小数点，1e16 写 `10000000000000000`），范围外写 `1E+17`、`9.9E-05` 这样的形式，负零写 `-0`；`describe` 的参数按名字的 UTF-16 码元序排列。`format_message` 是宽松格式化（缺值的占位符原样保留），`try_format_message` 与 `Catalog` 是严格格式化（缺值即失败，退回描述）。配置与编程错误（非法参数、清单不合法、管道约束成环、操作重复登记）抛 `FrameworkError`，它是 `ValueError` 的子类，带错误码；操作重复登记与登记表已满的 `FrameworkError` 经 `to_error()` 交回带消息键与参数的错误（`kernel.operation.duplicate` / `kernel.operation.registry_full`）。
2. 请求与响应是 dataclass（也可以是原语、`list` / `tuple` / 键为 `str` 的 `dict`、枚举、`X | None`、`Maybe[T]`、`TypedId` 子类，或自带 `from_json_value` / `to_json_value` 的类型），用标准库编解码；成员名用 `field(metadata=operation.json_field("ttlMs"))` 改写。载荷不合法返回 `KERNEL_VALIDATION_001`，`reason` 只写 JSON 路径（`$.quantity`）。
3. 处理器与中间件都是 `async def`；`Invoker` 在 `Context.run` 里执行整条管道：截止时间到了取消协程并返回 `KERNEL_TIMEOUT_001`，`ctx.cancellation.cancel()`（可以在任何线程调用）取消协程并返回 `KERNEL_CANCELLED_001`，两者同时触发时报超时；任务被别处取消时 `CancelledError` 照常传播。中间件以成功短路、`handle` 没有执行时，`invoke` 与 `invoke_typed` 都返回 `KERNEL_UNEXPECTED_001` + `kernel.operation.no_response`，不返回成功的空载荷（RFC-0011 规则 9）。操作或中间件抛出的异常（`Exception`）翻译为 `KERNEL_UNEXPECTED_001` + `kernel.invocation.unhandled_exception`，上线的错误不带异常文本，完整异常经 `logging` 的 `xihan_framework.operation` 记下（规则 5）。
4. 清单：`manifest.read(data)` / `manifest.read_file(path)` 解析并校验（未知字段、类型不符、格式与跨字段约束都抛 `FrameworkError`），`manifest.write(m)` 输出两空格缩进、字段按 Schema 顺序、缺省值省略的规范化 JSON。

`Result`、`Error`、`Maybe` 的 JSON 编码按 RFC-0001「Result」：有值成功 `{"ok":true,"value":…}`，无值成功（`success()`）只写 `{"ok":true}`，失败 `{"ok":false,"error":{…}}`。读取分三种：`Result.from_json_value`（有值，`value` 必填且不能是 `null`）、`Result.unit_from_json_value`（无值，带了 `value` 即拒绝，`{}` 与 `null` 也算）、`Result.maybe_from_json_value`（值是 `Maybe`，`null` 即无值）；`Error` 的 `message` 必填且非空，可选成员写成 `null` 不合法，未知成员忽略。写出的字节形式（浮点数的写法、`<` `>` `&` 的转义、映射键的排序）与 Go SDK 的 `encoding/json` 相同；这些规则与生命周期迁移表、管道阶段顺序、错误码、Baggage 键与能力依赖的文法都由 `spec/conformance/vectors` 验证。

## 开发

```bash
git submodule update --init          # spec/
python -m venv .venv                 # Windows 上用 py -3.13 -m venv .venv
python -m pip install -e . --group dev
ruff check && ruff format --check && mypy && pytest
```

`--group` 需要 pip 25.1 以上。测试直接读取 `spec/conformance/vectors/` 下的向量，子模块没有检出时测试失败而不是跳过。

## 状态

`0.1.0`（未发布）：`primitives`、`manifest`、`invocation`、`pipeline`、`operation` 五个包可用。进程外插件运行时 `plugin`（`xihan.plugin.v1`，基于 `grpc.aio`，以 extra `xihan-framework[plugin]` 引入 grpcio）与远程 Provider 的操作集（首个是 `caching.distributed@1`）规划中；Wasm 载体不在本 SDK 范围。

## 许可

[MIT](LICENSE)
