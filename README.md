<div align="center">

<h1>XiHan Framework SDK for Python</h1>

<h3>The same primitives, manifests and pipeline as the .NET host, verified by shared conformance vectors</h3>

<p>
  <a href="https://github.com/XiHanFun/framework-sdk-python/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/XiHanFun/framework-sdk-python/ci.yml?branch=main&style=flat-square&label=CI" /></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
  <a href="./LICENSE"><img alt="MIT License" src="https://img.shields.io/github/license/XiHanFun/framework-sdk-python?style=flat-square&color=green" /></a>
  <a href="https://github.com/XiHanFun/framework-sdk-python/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/XiHanFun/framework-sdk-python?style=flat-square&logo=github&label=Stars&color=1f6feb" /></a>
</p>

<p>
  <a href="https://framework.docs.xihanfun.com/sdks/python"><b>Docs</b></a> ·
  <a href="#quick-start"><b>Quick Start</b></a> ·
  <a href="https://github.com/XiHanFun/framework-spec"><b>Specification</b></a> ·
  <a href="https://github.com/XiHanFun/XiHan.Framework"><b>Reference Implementation</b></a>
</p>

<p><b>English</b> | <a href="./README_cn.md">简体中文</a></p>

</div>

The Python implementation of [XiHan Spec](https://github.com/XiHanFun/framework-spec): the primitives layer, manifests, the invocation context, the pipeline, operation contracts and the out-of-process plugin runtime. Semantics and wire encoding follow the spec, behavior matches the .NET reference implementation, [XiHan.Framework](https://github.com/XiHanFun/XiHan.Framework) (where a result would depend on platform internals, ADR-0236 pins it down in the spec); the spec lives in `spec/` as a submodule, and conformance is verified by the spec repository's vectors.

## Modules

| Package | Contents | Spec |
| --- | --- | --- |
| `primitives` | `Error` and error codes, `Result[T]`, `Maybe[T]`, `TypedId[V]`, lifecycle states, pipeline stages, Baggage keys, message keys and templates, `Catalog`, `FrameworkError` | RFC-0001, RFC-0010 |
| `manifest` | The model of `xihan.manifest.json`, reading in spec order and character-for-character identical writing | RFC-0002 |
| `invocation` | The immutable `Context`: deadline, cancellation, W3C tracing, registered Baggage and typed items | RFC-0003 |
| `pipeline` | The fixed-stage `Builder` with deterministic, printable ordering | RFC-0004 |
| `operation` | `Contract`, `Registry` and the `Invoker` that runs an operation through the full pipeline (XGI) | RFC-0011 |
| `plugin` | The out-of-process plugin runtime (`xihan.plugin.v1` on `grpc.aio`) and the host callback client `Host`; needs the extra `plugin` | RFC-0017 |

## Installation

```bash
pip install xihan-framework
```

The import name is `xihan_framework`. The five core modules have no runtime dependencies; the plugin runtime needs grpcio and protobuf through the extra `plugin` (`xihan-framework[plugin]`). PyPI currently has only a release that reserves the name, so install from source until the first release is published (see the [Python SDK page](https://framework.docs.xihanfun.com/sdks/python)).

## Quick Start

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

## Documentation

The documentation site is in Chinese. The notes on the example above (errors and message keys, templates, payload decoding, the built-in pipeline steps, manifests) and a complete plugin are on the Python SDK page.

| Where | What |
| --- | --- |
| [Python SDK](https://framework.docs.xihanfun.com/sdks/python) | Modules in detail, the quick start notes, writing a plugin, behavior shared with the host, language mapping, conformance, breaking changes and migration |
| [Out-of-process plugins](https://framework.docs.xihanfun.com/plugins/out-of-process) | The protocol `xihan.plugin.v1` and how the .NET host loads plugins |
| [Spec and languages](https://framework.docs.xihanfun.com/appendix/spec) | How the spec, the conformance profiles and the SDKs of each language fit together |
| [XiHan Spec](https://github.com/XiHanFun/framework-spec) | RFCs, vocabulary, protocol mappings and conformance vectors |

## Status

The five core modules and the plugin runtime are usable, and the example plugin `examples/echo` passes every case of the spec's black-box runner. Conformance (RFC-0024): the pinned spec version is the commit of the `spec/` submodule, the claimed profiles are `core` and `plugin`, and there are no gaps. Until there is a release pinned to a spec tag, the maturity is Experimental; details are on the [Python SDK page](https://framework.docs.xihanfun.com/sdks/python).

## Ecosystem

| Project | Description |
| --- | --- |
| [framework-spec](https://github.com/XiHanFun/framework-spec) | XiHan Spec: RFCs, vocabulary, protocol mappings and conformance vectors |
| [XiHan.Framework](https://github.com/XiHanFun/XiHan.Framework) | The .NET reference implementation |
| [framework-sdk-go](https://github.com/XiHanFun/framework-sdk-go) | Go SDK |
| [framework-sdk-typescript](https://github.com/XiHanFun/framework-sdk-typescript) | TypeScript SDK |
| [framework-sdk-rust](https://github.com/XiHanFun/framework-sdk-rust) | Rust SDK |
| [framework-sdk-java](https://github.com/XiHanFun/framework-sdk-java) | Java SDK |
| [framework-sdk-php](https://github.com/XiHanFun/framework-sdk-php) | PHP SDK |

## Contributing

[Issues](https://github.com/XiHanFun/framework-sdk-python/issues) and pull requests are welcome. Changes to public contracts are not made in the SDK repositories: they go through the RFC process of framework-spec first and are then synced to each SDK. Development conventions, gates and the release process are in [AGENTS.md](./AGENTS.md).

## License

[MIT](./LICENSE) © 2021-Present XiHanFun and contributors.
