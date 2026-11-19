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
  <a href="https://framework.docs.xihanfun.com/sdks/python"><b>文档</b></a> ·
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
| `primitives` | `Error` 与错误码、`Result[T]`、`Maybe[T]`、`TypedId[V]`、生命周期状态、管道阶段、Baggage 键、消息键与模板、`Catalog`、`FrameworkError` | RFC-0001、RFC-0010 |
| `manifest` | `xihan.manifest.json` 的模型、按规范顺序的读取与逐字相同的写出 | RFC-0002 |
| `invocation` | 不可变的 `Context`：截止时间、取消、W3C 追踪、已登记的 Baggage 与类型化键的对象袋 | RFC-0003 |
| `pipeline` | 固定阶段的 `Builder`，排序确定、可打印 | RFC-0004 |
| `operation` | `Contract`、`Registry` 与让操作经完整管道执行的 `Invoker`（XGI） | RFC-0011 |
| `plugin` | 进程外插件运行时（`xihan.plugin.v1`，基于 `grpc.aio`）与宿主回调客户端 `Host`；需要 extra `plugin` | RFC-0017 |

## 安装

```bash
pip install xihan-framework
```

导入名是 `xihan_framework`。核心五块运行时零依赖；插件运行时经 extra `plugin`（`xihan-framework[plugin]`）引入 grpcio 与 protobuf。PyPI 上目前只有占名用的版本，首个版本发布之前从源码安装（见 [Python SDK 文档页](https://framework.docs.xihanfun.com/sdks/python)）。

## 快速开始

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

## 文档

上面示例的逐条说明（错误与消息键、模板、载荷解码、内置的管道步骤、清单）与完整的插件示例在 Python SDK 文档页。

| 去处 | 内容 |
| --- | --- |
| [Python SDK](https://framework.docs.xihanfun.com/sdks/python) | 详细的模块说明、快速开始的逐条说明、写一个插件、与宿主共用的行为、语言映射、一致性、破坏性改动与迁移 |
| [进程外插件](https://framework.docs.xihanfun.com/plugins/out-of-process) | 协议 `xihan.plugin.v1` 与 .NET 宿主怎样加载插件 |
| [规范与多语言](https://framework.docs.xihanfun.com/appendix/spec) | 规范、一致性档与各语言 SDK 的关系 |
| [XiHan Spec](https://github.com/XiHanFun/framework-spec) | RFC、术语表、协议映射与一致性向量 |

## 状态

核心五块与插件运行时可用，示例插件 `examples/echo` 通过规范黑盒运行器的全部用例。一致性（RFC-0024）：钉的规范版本是 `spec/` 子模块指向的提交，认领的档是 `core` 与 `plugin`，没有缺口。在有钉在规范标签上的发布版之前，成熟度是 Experimental；细节见 [Python SDK 文档页](https://framework.docs.xihanfun.com/sdks/python)。

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

欢迎提交 [Issue](https://github.com/XiHanFun/framework-sdk-python/issues) 与 Pull Request。公开契约的变更不在 SDK 仓库里做：先走 framework-spec 的 RFC 流程，再同步到各 SDK。开发约定、门禁与发布流程见 [AGENTS.md](./AGENTS.md)。

## 许可证

[MIT](./LICENSE) © 2021-Present XiHanFun and contributors。
