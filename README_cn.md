<div align="center">

<h1>XiHan Framework SDK for Python</h1>

<h3>与 .NET 宿主相同的原语、清单与管道，由同一套一致性向量验证</h3>

<p>
  <a href="https://github.com/XiHanFun/framework-sdk-python/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/XiHanFun/framework-sdk-python/ci.yml?branch=main&style=flat-square&label=CI" /></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
  <a href="./LICENSE"><img alt="MIT License" src="https://img.shields.io/github/license/XiHanFun/framework-sdk-python?style=flat-square&color=green" /></a>
  <a href="https://github.com/XiHanFun/framework-sdk-python/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/XiHanFun/framework-sdk-python?style=flat-square&logo=github&label=Stars&color=1f6feb" /></a>
</p>

<p>
  <a href="https://framework.docs.xihanfun.com"><b>文档</b></a> ·
  <a href="#快速开始"><b>快速开始</b></a> ·
  <a href="https://github.com/XiHanFun/framework-spec"><b>规范</b></a> ·
  <a href="https://github.com/XiHanFun/XiHan.Framework"><b>参考实现</b></a>
</p>

<p><a href="./README.md">English</a> | <b>简体中文</b></p>

</div>

[XiHan Spec](https://github.com/XiHanFun/framework-spec) 的 Python 实现：原语层、清单、调用上下文、管道、操作契约与进程外插件运行时。语义与线上编码以规范为准，行为与 .NET 参考实现（[XiHan.Framework](https://github.com/XiHanFun/XiHan.Framework)）一致（结果取决于平台内部的地方由 ADR-0236 写死在规范里）；规范以 submodule 放在 `spec/`，一致性由规范仓库的向量验证。

## 模块

| 包 | 内容 | 规范 |
| --- | --- | --- |
| `primitives` | `Error` / 错误码、`Result[T]`、`Maybe[T]`、`TypedId[V]`、`LifecycleState`、`PipelineStage`、`Stability`、Baggage 键、消息键与模板、`Catalog`、`FrameworkError` | RFC-0001、RFC-0010、`conventions/` |
| `manifest` | `xihan.manifest.json` 的模型（冻结的 dataclass，构造时校验各字段，`validate()` 核对字段之间的约束）、按规范顺序的读取与逐字相同的写出，能力声明与依赖文本 | RFC-0002、`manifest/manifest.schema.json` |
| `invocation` | 不可变的 `Context`：截止时间、可跨线程触发的 `Cancellation`、W3C 追踪（`traceparent` 只认版本 `00` 与小写十六进制、不合法的当作没给，采样只看标志的最低位，写出只带采样位 `00` / `01`，没有上游时写 `01`）、已登记的 Baggage（已登记键的值非空、按 UTF-16 码元计最多 256、不含控制字符，由一组 Baggage 构造上下文时丢弃不合的成员，`with_baggage` 给出不合的值则抛 `ValueError`，幂等键两种情形都例外）、类型化键的对象袋；`Context.run(call)` 进入时截止时间已过即返回 `KERNEL_TIMEOUT_001` + `kernel.invocation.deadline_before_start`（不核对取消），之后把一个派生的上下文交给 `call`，它的信号在调用方取消或到达截止时间时触发；取消与截止时间只经信号通知，不取消 `call` 的任务，`call` 抛出 `CancelledError` 或写下取消结果时翻译为 `KERNEL_TIMEOUT_001` / `KERNEL_CANCELLED_001`，照常返回的结果原样交回 | RFC-0003 |
| `pipeline` | 固定阶段的 `Builder`：`use` / `use_at` / `use_named_instance`、`before` / `after`（按类型名或实例名）、阶段可写成清单里的名字、`extend` 接上另一个构建器；命名实例只按实例名引用、默认实例按类型名引用；重复登记、未知阶段与约束主体未登记或有歧义在登记时报，约束目标的问题与成环在排序时报（逐个阶段处理），消息是各语言逐字相同的英文原文，命名实例写成 `类型名[名字]`，成环时列出全部卡住的中间件；`describe` / `dump`、`build`；排序规则与错误码 `KERNEL_PIPELINE_00x` | RFC-0004 |
| `operation` | `Contract`、`Schema`、异步处理器、容量 10000 的 `Registry`、经完整管道执行的 `Invoker`（XGI：JSON 请求与响应，带三个内置步骤）与类型化入口 `invoke_typed` | RFC-0011 |
| `plugin` | 进程外插件运行时（协议 `xihan.plugin.v1`）：`Plugin` 读宿主下发的环境变量，在本机端点上以 `grpc.aio` 提供 `Plugin` 服务，写握手行，stdin 关闭或收到 SIGTERM 即收尾退出；每个 RPC 先按常数时间核对会话令牌；生命周期状态机与健康推送；调用经 SDK 的 `Invoker` 与完整管道执行，事件按（主题，订阅方式，类型）交给处理器；宿主回调客户端 `Host`（`invoke` 与批量的 `publish`）；stderr 上的 JSON 日志；运行时的消息目录（10 种区域）。协议代码在 `plugin.v1`，由 `spec/plugin/plugin.proto` 生成。依赖 grpcio 与 protobuf，装 extra：`pip install "xihan-framework[plugin]"` | RFC-0017、`spec/plugin/` |

## 快速开始

```bash
pip install xihan-framework
```

PyPI 上目前只有占名用的 0.0.1；0.1.0 尚未发布，现在从源码安装（见[开发](#开发)）。导入名是 `xihan_framework`。

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

1. 可恢复失败返回 `fail(Error...)`，不抛异常；错误码按 `<领域大写>_<KIND>_<NNN>`，文案用消息键（最长 128 个字符）+ 参数，`Error.message` 先经 `set_default_messages(Catalog(...), locale)` 安装的目录解析，再找 SDK 自带的内核错误英文模板（`BUILT_IN_CATALOG`，含清单读取的错误，与规范向量 kernel-messages 逐字相同，如 `Operation 'orders.create' is not registered.`），都解析不了时是 `键 (参数)`。配置与编程错误（非法参数、清单字段不合格式、管道约束成环、操作重复登记）抛 `FrameworkError`，它是 `ValueError` 的子类，带错误码；操作重复登记与登记表已满的 `FrameworkError` 经 `to_error()` 交回带消息键与参数的错误（`kernel.operation.duplicate` / `kernel.operation.registry_full`）。
2. 模板按 RFC-0010「模板」：`{name}` / `{0}`，`{{` `}}` 转义。值先归到值模型（空、布尔、字符串、64 位整数、双精度浮点、数组、对象）：浮点在 1e-4 ≤ |x| < 1e17 时写最短往返的定点形式（整数值不带小数点，`2.0` 写 `2`），范围外写 `1E+17`、`9.9E-05` 这样的形式，负零写 `-0`，非数与无穷写 `NaN`、`Infinity`、`-Infinity`；数组与对象写成紧凑的 JSON，对象成员按名字的 UTF-16 码元序排列，超过 64 层的容器写 `null`。说明符只认 `D`、`X` / `x`、`F`、`N` 加至多两位精度（`F02` 即 `F2`），`D` 与 `X` 只用于整数（`X` 只用于非负整数），`F` 与 `N` 用于整数与浮点，按精确值舍入、正好一半取偶（`{n:F2}` 取 0.125 得 `0.12`）；其余说明符（`E2`、`0.00`、`yyyy-MM-dd`、printf 风格的 `%05d`）与不适用的场合一律按无格式写出。`format_message` / `try_format_message` / `format_argument` 可带 `locale`，`Catalog.resolve` 按请求的区域格式化：区域只改变小数点与分组符（de-DE、es-ES、pt-BR 是 `,` 与 `.`，fr-FR 是 `,` 与 U+202F，ru-RU 是 `,` 与 U+00A0，区域名不区分大小写，其余区域与不给区域都是 `.` 与 `,`），小数点用于无格式的浮点与 `F`、`N`，分组符只用于 `N`，`D`、`X` 与数组、对象里的数字不受区域影响。`describe` 不随区域变化，参数按名字的 UTF-16 码元序排列。`format_message` 是宽松格式化（缺值的占位符原样保留），`try_format_message` 与 `Catalog` 是严格格式化（缺值即失败，退回描述）。
3. 请求与响应是 dataclass（也可以是原语、`list` / `tuple` / 键为 `str` 的 `dict`、枚举、`X | None`、`Maybe[T]`、`TypedId` 子类，或自带 `from_json_value` / `to_json_value` 的类型），用标准库编解码；成员名用 `field(metadata=operation.json_field("ttlMs"))` 改写。载荷按 RFC-0011 规则 2 解码，不合法返回 `KERNEL_VALIDATION_001`，`reason` 只写 JSON 路径：先过共同的 JSON 读取检查（语法错误、只有空白、BOM、嵌套超过 64 层、非法 UTF-8、孤立的代理项都写 `$`，任何一层有重复成员时写第一处重复的路径，先于一切类型问题）；再按请求类型解码：成员名区分大小写，未知成员忽略，数字不从字符串读，整数不接受 `1.0` 与 `1e2`，超出范围即不符，浮点接受整数写法、`1e400` 为无穷；按文本顺序报第一处类型不符的路径（`$.lines[1].quantity`，成员名含 `.`、空格、引号、括号、`/`、`\` 或几个空白控制字符时写 `$['unit price']`），必填成员缺少时在所在对象结束时报该对象的路径（`$`、`$.inner`）；`null` 载荷与不是对象的载荷写 `$`。空载荷等价于 `{}`。载荷的文本按 JSON 值比较，空白、转义与数字的写法不是契约；`JsonValue` / `Any` 类型的成员原样保留读到的值，数字读成保留原文的 `primitives.JsonNumber`（`float` 的子类，`text` 是原文），写回时与读到的 JSON 值相等：`1e400` 不变成 `null` 或无穷，超出双精度的整数保持精确，`2.0` 与 `2` 的区别保留。
4. 处理器与中间件都是 `async def`。`Invoker` 带三个内置步骤，位置按 RFC-0011 规则 10：都按归属 `xihan.kernel.core` 放在所在阶段的 `Beginning`，与同位置的其他中间件按归属的序数序排序，`invoker.dump()` 打印实际执行的管道。Observe 阶段的 `ObserveMiddleware` 把排在它之后的中间件与处理器抛出的异常（`Exception`）翻译为 `KERNEL_UNEXPECTED_001` + `kernel.invocation.unhandled_exception`，上线的错误不带异常文本，完整异常经 `logging` 的 `xihan_framework.operation` 记下；排在它之前的（Ingress 阶段，或 Observe 阶段 `Beginning` 上归属排在 `xihan.kernel.core` 之前的，如归属 `app`）抛出的异常原样交给调用方。Resilience 阶段的 `DeadlineMiddleware` 就是 `Context.run`：进入时截止时间已过即 `deadline_before_start`；之后的中间件与处理器拿到的上下文在截止时间到了或 `ctx.cancellation.cancel()`（可以在任何线程调用）时触发信号。取消与截止时间只经信号通知（RFC-0003 规则 1）：调用器不取消处理器的任务，处理器读 `ctx.is_cancelled`、`await ctx.cancellation.wait()` 或调用 `ctx.cancellation.raise_if_cancelled()` 自己停下；抛出取消或写下 `KERNEL_CANCELLED_001` 时，截止时间已到即 `KERNEL_TIMEOUT_001`，否则 `KERNEL_CANCELLED_001`；信号触发后照常返回的结果就是调用的结果；任务被别处取消时 `CancelledError` 照常传播。Validate 阶段的 `ValidationMiddleware` 解码原始调用的载荷（解码好的请求经 `operation.decoded_request_of(ctx)` 取），类型化调用跳过。进入管道之前只核对未登记（`KERNEL_NOTFOUND_001`）与类型化调用的类型不符（`KERNEL_VALIDATION_002`，参数只有 `id`）。中间件以成功短路、`handle` 没有执行时，`invoke` 与 `invoke_typed` 都返回 `KERNEL_UNEXPECTED_001` + `kernel.operation.no_response`（规则 9）。XGI 入口在管道结束之后才把响应编码成载荷，管道里的中间件经 `operation.response_of(ctx)` 看到编码之前的对象；写不出时（类型化的浮点是 NaN 或无穷，或编码不了的值）返回 `KERNEL_UNEXPECTED_001` + `kernel.invocation.unhandled_exception`，框架异常的错误放进 `inner`，不抛给调用方，没有载荷，NaN 不写成 `null` 或字符串。
5. 清单：`manifest.read(data)` / `manifest.read_file(path)` 返回 `Result[Manifest]`，失败都是 `KERNEL_CAPABILITY_006`，按 RFC-0002 解析规则 2 的顺序只报第一处问题：不是 JSON 为 `kernel.manifest.invalid_json`（`reason` 为 `$` 或第一处重复成员的路径，`read` 不跳过 BOM，`read_file` 按 UTF-8 读并跳过开头的 UTF-8 BOM），根是 `null` 为 `kernel.manifest.empty`，不合 Schema 为 `kernel.manifest.invalid`（`reason` 是第一处问题的路径：成员按文本顺序，每个成员依次查 Schema 之外的成员、`null`、类型、取值，对象查完后按 `required` 的顺序报缺少的成员），字段之间的约束保留 `primitives.manifest.*` 的键与 `id` 参数（`Manifest.validate()` 的同一组错误，码换成 `KERNEL_CAPABILITY_006`），文件不存在为 `kernel.manifest.file_missing`（`path`）；`message` 是这些键的英文模板格式化后的句子，与 kernel-messages 向量相同。接口版本是不带前导零的正整数，不超过 2147483647。插件声明了 `contributes.pipeline` 时只有 Wasm 插件的中间件生效，由宿主在发现插件时核对，读取不拒绝。`manifest.write(m)` 按 RFC-0002「写出」输出：两个空格缩进、LF、末尾不换行，成员顺序固定，列表总写（空列表 `[]`），`role` 缺省写 `provider`，`activation` 缺省写 `["onStartup"]`，字符串只转义 `"`、`\`、控制字符与孤立的代理项。

### 写一个插件

```bash
pip install "xihan-framework[plugin]"
```

grpcio 与 protobuf 只在 extra `plugin` 里，核心五块不依赖它们；没有装就导入 `xihan_framework.plugin` 时，错误里给出这条安装命令。

```python
import sys
from dataclasses import dataclass
from pathlib import Path

from xihan_framework import operation
from xihan_framework.invocation import Context
from xihan_framework.plugin import Envelope, Plugin, Subscription, SubscriptionHandler
from xihan_framework.primitives import Result, Unit, ok, success


@dataclass(frozen=True)
class Echo:
    text: str


async def echo(ctx: Context, request: Echo) -> Result[Echo]:
    return ok(request)


async def on_event(ctx: Context, envelope: Envelope, attempt: int) -> Result[Unit]:
    # 至少一次投递：处理器必须幂等，可按信封 id 去重
    return success()


registry = operation.Registry()
registry.add(operation.Contract("echoes.create"), echo, request_type=Echo, response_type=Echo)
plugin = Plugin(
    Path(__file__).with_name("xihan.manifest.json").read_bytes(),
    operations=registry,
    subscriptions=[SubscriptionHandler(Subscription("echo.events"), on_event)],
)
sys.exit(plugin.run())
```

清单 `xihan.manifest.json` 与脚本放在插件目录里：

```json
{
  "$schema": "https://spec.xihanfun.com/manifest/v1.json",
  "id": "xihan.samples.echo-python",
  "version": "0.1.0",
  "primitives": "1",
  "stability": "experimental",
  "kind": "plugin",
  "isolation": "out-of-process",
  "contributes": { "operations": ["echoes.create"] },
  "activation": ["onStartup"],
  "entry": "echo"
}
```

1. 清单要点：`kind` 为 `plugin`、`isolation` 为 `out-of-process`，不写 `contributes.pipeline`（只有 Wasm 插件能贡献中间件，`Plugin` 构造时就拒绝）；登记的每个操作都要列在 `contributes.operations`，健康项列在 `contributes.health`，配置节写在 `contributes.config.section`（宿主还要授予 `config:<配置节>`）；回调宿主要在 `permissions` 里声明 `capability:kernel.pipeline`（`Host.invoke`）与 `capability:messaging.queue`（`Host.publish`）。握手交给宿主的是 `Plugin` 收到的清单文本（省略时读工作目录里的文件），必须与插件目录里的文件一致。
2. 启动脚本：`entry` 是相对插件目录的可执行文件。宿主只继承 `PATH`、用户目录、临时目录等少数环境变量（handshake.md §1），所以脚本里写解释器与插件脚本的绝对路径，不依赖 `VIRTUAL_ENV`。Unix 上是带 shebang 的 sh 脚本，并 `chmod +x`；Windows 上是 `.cmd`，`entry` 写 `echo.cmd`（没有扩展名时宿主补的是 `.exe`）。

   ```sh
   #!/bin/sh
   exec /path/to/.venv/bin/python /path/to/echo/echo.py "$@"
   ```

   ```bat
   @echo off
   "C:\path\to\.venv\Scripts\python.exe" "C:\path\to\echo\echo.py" %*
   ```

3. 端点：默认是 Unix 域套接字，Linux、macOS 与 Windows 都用它（Windows 10 1803 起支持 AF_UNIX，grpcio 在 Windows 上同样能监听 `unix:` 地址）。套接字放在本进程新建的私有目录里（`mkdtemp`：名字随机、权限 `0700`、只新建不复用），套接字文件 `0600`，停止后删除；Windows 上 `chmod` 只管只读位，私有目录在当前用户的临时目录下，靠目录的访问控制。`Plugin(network=Network.TCP)` 改为只绑定 `127.0.0.1` 的随机端口，同机的其他用户连得上，隔离只靠会话令牌。grpcio 不支持 Windows 命名管道：插件不监听 `npipe`；宿主回调端点是 `npipe` 时（如 Windows 上的 .NET 宿主），`Host` 的每次回调都以 `PLUGINS_UNAVAILABLE_001` 失败。
4. 生命周期：每个 RPC 先按常数时间核对元数据 `xihan-plugin-session`，缺失、多于一个或不对时以 `Unauthenticated` 拒绝、不执行。`Handshake` 选宿主列表里最高的共同版本（没有即 `FailedPrecondition`），交出清单、操作声明（契约与 JSON Schema）、订阅与消息目录，进入 `Resolved`；`Configure` 把配置节、授予的权限、绑定与工作目录交给 `Hooks.configure`；`Start` 成功后直接报 `Ready`。`Hooks.health` 报的健康项只留清单声明了的，报的状态按迁移表生效；`WatchHealth` 先推当前状态，之后变化时各推一条（至少每秒重新计算一次）。`Stop` 进入 `Draining`：新调用返回 `PLUGINS_UNAVAILABLE_001`，在宽限期内等在途调用结束，再调用 `Hooks.stop`，应答之后退出；stdin 关闭或收到 SIGTERM 时立即触发在途调用的取消信号，收尾后退出。
5. 调用与事件：`Invoke` 按 invocation.md §2 从元数据还原调用上下文（追踪；截止时间取元数据与 gRPC 调用中较早的那个；只收已登记的 Baggage），经 `Invoker` 与完整管道执行；未登记的操作以 `Unimplemented` 结束。调用方取消或截止时间到了只触发信号，不取消处理器的任务（RFC-0003 规则 1）。本 SDK 的操作都是一元的：`InvokeStream` 的第一帧不是 `start` 时为 `InvalidArgument`，否则为 `Unimplemented`。`HandleEvent` 只交给以这次投递的方式（`mode`，空同 `competing`）订阅了该主题、类型为空或相同的处理器，依次执行，第一个失败即投递结果；没有匹配的订阅时返回 `PLUGINS_UNEXPECTED_001`。
6. 回调：`host_of(ctx)` 取宿主客户端，`await host.invoke(ctx, "orders.create", payload)` 与 `await host.publish(ctx, envelopes)`（批量，逐项结果）带会话令牌，以及上下文的追踪、截止时间与已登记的 Baggage。上下文或信封（扩展属性 `xihantenant`）带的租户必须是某个进行中的调用里宿主交给插件的租户，否则不发出调用，直接返回 `PLUGINS_DENIED_002` + `plugins.callback.tenant_denied`；宿主拒绝回调（`Unauthenticated`、`PermissionDenied`）也是 `PLUGINS_DENIED_002`。
7. 错误与日志：业务失败走 `Status.error`，gRPC 状态只表示传输失败。错误的 `text` 先按进程默认的解析器、再按插件的 `catalogs` 在宿主的默认区域里解析，宿主按消息键用握手时拿到的目录再翻译。运行时自己的错误用消息键 `plugins.*`（`PLUGINS_UNAVAILABLE_001`、`PLUGINS_DENIED_002`、`PLUGINS_UNEXPECTED_001`），10 种区域的目录是 `plugin.CATALOG`（目录名 `plugins.protocol`），要随握手交给宿主时放进 `catalogs`。`Plugin.run()` 在根日志没有处理器时接上 `JsonLogHandler`：stderr 一行一个 JSON 对象，`level` 为 `trace` / `debug` / `information` / `warning` / `error` / `critical`，另有 `message` 与其余作用域属性（invocation.md §6）。

用黑盒运行器验证（需要 Go，版本见 `spec/conformance/go.mod`）：

```bash
cd spec/conformance
go run ./cmd/xihan-conformance plugin --dir <插件目录> --exclude echoes.crash --sample 'echoes.create={"text":"hi"}' --config '{"greeting":"hello"}'
```

完整的示例在 `examples/echo`：操作、故意崩溃的 `echoes.crash`、事件订阅、健康项、配置与宿主回调。`tests/test_plugin_conformance.py` 把它放进临时目录、生成启动脚本，再用运行器跑全部 8 个用例。

## 与宿主共用的行为

`Result`、`Error`、`Maybe` 的 JSON 编码按 RFC-0001「Result」：有值成功 `{"ok":true,"value":…}`，无值成功（`success()`）只写 `{"ok":true}`，失败 `{"ok":false,"error":{…}}`。读取分三种：`Result.from_json_value`（有值，`value` 必填且不能是 `null`）、`Result.unit_from_json_value`（无值，带了 `value` 即拒绝，`{}` 与 `null` 也算）、`Result.maybe_from_json_value`（值是 `Maybe`，`null` 即无值）；`Error` 的 `message` 必填且非空，可选成员写成 `null` 不合法，未知成员忽略；从文本读取时先过共同的 JSON 读取检查，任何一层出现重复的成员名即不合法，含 `value`、`details` 与 `inner` 的内部。数字按值模型往返：64 位范围内的整数原样（9007199254740993 不会变成 9007199254740992），整数值的浮点写成 `2.0`、`-0.0`、`1E+17`，读回来仍是浮点；非有限的浮点写成字符串 `NaN`、`Infinity`、`-Infinity`。这些规则与生命周期迁移表、管道阶段顺序、错误码、Baggage 键与能力依赖的文法都由 `spec/conformance/vectors` 验证。插件协议的握手行文法与版本协商由 `spec/conformance/vectors/plugin/` 验证；握手、会话令牌、生命周期、未知操作、声明的操作、事件投递与 stdin 关闭即退出由规范仓库的黑盒运行器验证（见[写一个插件](#写一个插件)）。

## 语言映射

规范把一部分写法交给各语言自己声明，Python 的写法如下。

- **请求类型的必填与可选**：dataclass 里没有缺省值的字段是必填的（`Maybe` 字段例外，没写时为无值）；有缺省值的字段（`= 0`、`= None`、`field(default_factory=list)`）是可选的，缺少时取缺省值。必填只要求成员出现，值为 `null` 也算出现。
- **接受 `null` 的成员**：只有 `X | None`、`Maybe[X]`、`JsonValue` / `Any` 接受 `null`，`null` 即没有值（`None`）。规范里接受 `null` 的字符串、数组与对象在 Python 里写成 `str | None`、`list[X] | None`、`Inner | None`，数组项要接受 `null` 时写 `list[str | None]`；写成 `str`、`list[str]` 的成员遇到 `null` 按类型不符报出。整数、浮点、布尔不写 `| None`，`null` 即类型不符，与规范一致。
- **数字类型**：`int` 是 64 位有符号整数；Python 没有更窄的整数类型，32 位整数写成 `Annotated[int, operation.Int32]`，其他范围用 `operation.IntRange(最小, 最大)`，超出范围即类型不符。`float` 是双精度浮点，也接受整数写法，解码出普通的 `float`；`bool` 不是数字。`JsonValue` / `Any` 成员里的数字是 `JsonNumber`（64 位范围内的整数仍是 `int`）。
- **模板的值模型**：`None`、`bool`、`str`（含 `StrEnum` 等 `str` 的子类）、`int`（含 `IntEnum`；超出 64 位的是浮点）、`float`；`Decimal`、`Fraction` 等其他实数换成最接近的双精度浮点；`Mapping` 是对象（键取它的文本），`list`、`tuple` 与其他序列、`set` / `frozenset`、`bytes` 是数组。模型之外的值取 `str(value)`：`datetime(2026, 11, 16, 8, 30)` 写 `2026-11-16 08:30:00`，`UUID` 写带连字符的小写形式，普通 `Enum` 写 `Color.RED`；它们不跨语言，插件要跨语言格式化时先转成字符串。
- **线上编码的值**：Result 的值与错误细节里，超出 64 位的 `int` 按值模型写成浮点（精度随之丢失），`float('nan')` 与无穷写成字符串；操作的响应里非有限的浮点写不出（见[快速开始](#快速开始)第 4 条），`JsonNumber` 写回原文。
- **JSON 文本与文法**：读取不用 `json.loads`（它接受 `NaN`、孤立的代理项、重复成员与很深的嵌套），由 SDK 自己按 RFC 8259 读；`str` 里的代理项码点按不合法的 Unicode 处理。规范的正则一律 `re.fullmatch`，结尾的换行不会被放过。

## 设计

面向 Python 应用、脚本型插件与 AI 工具链：Python 3.12+，处理器与中间件基于 asyncio，核心五块运行时零依赖。进程外插件运行时基于 `grpc.aio`，与处理器在同一个事件循环里；grpcio 与 protobuf 只在 extra `plugin` 里。

## 开发

```bash
git submodule update --init          # spec/
python -m venv .venv                 # Windows 上用 py -3.13 -m venv .venv
python -m pip install -e ".[plugin]" --group dev
ruff check && ruff format --check && mypy && pytest
```

`--group` 需要 pip 25.1 以上。测试直接读取 `spec/conformance/vectors/` 下的向量，子模块没有检出时测试失败而不是跳过。一致性测试 `tests/test_plugin_conformance.py` 用 `go run` 运行子模块里的黑盒运行器，需要 Go（版本见 `spec/conformance/go.mod`）；没有 Go 时跳过并给出原因，设置了 `XIHAN_CONFORMANCE_REQUIRED` 时（CI）改为失败。

协议代码 `src/xihan_framework/plugin/v1/`（消息、服务与 mypy-protobuf 生成的类型存根）由 `spec/plugin/plugin.proto` 生成并提交，改协议后重新生成：

```bash
python scripts/generate_protocol.py
```

生成器 grpcio-tools 与 mypy-protobuf 在开发依赖里锁定版本，CI 重新生成并核对没有差异；生成物不经 ruff 格式化与检查，mypy 按它们的类型存根检查。

## 状态

`0.1.0`（未发布）：`primitives`、`manifest`、`invocation`、`pipeline`、`operation` 与进程外插件运行时 `plugin`（`xihan.plugin.v1`，基于 `grpc.aio`，以 extra `xihan-framework[plugin]` 引入 grpcio 与 protobuf）可用，示例插件 echo 通过规范黑盒运行器的全部用例。远程 Provider 的操作集（如 `caching.distributed@1`）仍未提供；Wasm 载体不在本 SDK 范围。

## 生态

| 项目 | 说明 |
| --- | --- |
| [framework-spec](https://github.com/XiHanFun/framework-spec) | XiHan Spec：RFC、术语表、协议映射与一致性向量 |
| [XiHan.Framework](https://github.com/XiHanFun/XiHan.Framework) | .NET 参考实现 |
| [framework-sdk-go](https://github.com/XiHanFun/framework-sdk-go) | Go SDK |
| [framework-sdk-typescript](https://github.com/XiHanFun/framework-sdk-typescript) | TypeScript SDK |
| [framework-sdk-rust](https://github.com/XiHanFun/framework-sdk-rust) | Rust SDK |
| [framework-sdk-java](https://github.com/XiHanFun/framework-sdk-java) | Java SDK |
| [framework-sdk-php](https://github.com/XiHanFun/framework-sdk-php) | PHP SDK |

## 参与

欢迎提交 [Issue](https://github.com/XiHanFun/framework-sdk-python/issues) 与 Pull Request。

1. 先读 framework-spec 的 README 与相关 RFC。
2. 公开契约的变更不在 SDK 仓库里做：走 framework-spec 的 RFC 流程，再同步到各 SDK。

## 许可证

[MIT](./LICENSE) © 2021-Present XiHanFun and contributors。
