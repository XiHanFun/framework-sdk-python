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
  <a href="https://framework.docs.xihanfun.com"><b>Docs</b></a> ·
  <a href="#getting-started"><b>Getting Started</b></a> ·
  <a href="https://github.com/XiHanFun/framework-spec"><b>Specification</b></a> ·
  <a href="https://github.com/XiHanFun/XiHan.Framework"><b>Reference Implementation</b></a>
</p>

<p><b>English</b> | <a href="./README_cn.md">简体中文</a></p>

</div>

The Python implementation of [XiHan Spec](https://github.com/XiHanFun/framework-spec): the primitives layer, manifests, the invocation context, the pipeline, operation contracts and the out-of-process plugin runtime. Semantics and wire encoding follow the spec, behavior matches the .NET reference implementation, [XiHan.Framework](https://github.com/XiHanFun/XiHan.Framework) (where a result would depend on platform internals, ADR-0236 pins it down in the spec); the spec lives in `spec/` as a submodule, and conformance is verified by the spec repository's vectors.

## Modules

| Package | Contents | Spec |
| --- | --- | --- |
| `primitives` | `Error` / error codes, `Result[T]`, `Maybe[T]`, `TypedId[V]`, `LifecycleState`, `PipelineStage`, `Stability`, Baggage keys, message keys and templates, `Catalog`, `FrameworkError` | RFC-0001, RFC-0010, `conventions/` |
| `manifest` | The model of `xihan.manifest.json` (frozen dataclasses that validate each field on construction, with `validate()` checking the constraints between fields), reading in spec order and character-for-character identical writing, capability declarations and requirement text | RFC-0002, `manifest/manifest.schema.json` |
| `invocation` | Immutable `Context`: a deadline, a `Cancellation` that can be triggered across threads, W3C tracing (`traceparent` accepts only version `00` and lowercase hex, and an invalid one is treated as absent; sampling looks only at the lowest bit of the flags; output carries only the sampled flag `00` / `01`, and `01` when there is no upstream), registered Baggage (values of registered keys are non-empty, at most 256 UTF-16 code units and free of control characters; building a context from a set of Baggage drops nonconforming members, while `with_baggage` raises `ValueError` for a nonconforming value, with the idempotency key exempt in both cases), and an item bag with typed keys; `Context.run(call)` returns `KERNEL_TIMEOUT_001` + `kernel.invocation.deadline_before_start` if the deadline has already passed on entry (cancellation is not checked), and otherwise hands `call` a derived context whose signal fires when the caller cancels or the deadline is reached; cancellation and deadlines only notify through the signal and do not cancel `call`'s task, a `CancelledError` raised by `call` or a cancellation result written by it is translated to `KERNEL_TIMEOUT_001` / `KERNEL_CANCELLED_001`, and a result returned normally is passed back as is | RFC-0003 |
| `pipeline` | The fixed-stage `Builder`: `use` / `use_at` / `use_named_instance`, `before` / `after` (by type name or instance name), stages given by their manifest names, and `extend` to attach another builder; named instances are referenced only by instance name, default instances by type name; duplicate registration, unknown stages and an unregistered or ambiguous constraint subject are reported at registration, while problems with constraint targets and cycles are reported at sorting (stage by stage), with messages that are English originals, identical across languages character for character, named instances written as `TypeName[name]`, and every stuck middleware listed for a cycle; `describe` / `dump`, `build`; ordering rules and error codes `KERNEL_PIPELINE_00x` | RFC-0004 |
| `operation` | `Contract`, `Schema`, async handlers, a `Registry` with a capacity of 10000, an `Invoker` that runs through the full pipeline (XGI: JSON requests and responses, with three built-in steps), and the typed entry point `invoke_typed` | RFC-0011 |
| `plugin` | The out-of-process plugin runtime (protocol `xihan.plugin.v1`): `Plugin` checks the manifest and the declarations before listening, reads the environment variables the host passes, serves the `Plugin` service with `grpc.aio` on a local endpoint and writes the handshake line; every RPC first checks the session token in constant time; call order, configuration (with reading by type), readiness, health, stopping and the shutdown on stdin closure or SIGTERM (at most 3 seconds) follow lifecycle.md §7; calls run through the SDK's `Invoker` and the full pipeline, and events go to handlers one after another by (topic, subscription mode, type); the host callback client `Host` (`invoke` and the batched `publish`, whose endpoint is the first entry of `XIHAN_PLUGIN_HOST_ADDRESS` grpcio can dial, with permissions and tenants checked locally before sending); JSON logs on stderr; the runtime's message catalog (10 locales, for local rendering, not sent in the handshake). The protocol code is in `plugin.v1`, generated from `spec/plugin/plugin.proto`. It depends on grpcio and protobuf; install the extra: `pip install "xihan-framework[plugin]"` | RFC-0017, `spec/plugin/` |

## Getting Started

```bash
pip install xihan-framework
```

PyPI currently has only a 0.0.1 release that reserves the name; 0.1.0 is not yet published, so install from source for now (see [Development](#development)). The import name is `xihan_framework`.

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

1. Recoverable failures return `fail(Error...)` instead of raising; error codes follow `<DOMAIN>_<KIND>_<NNN>` with the domain in upper case, and text is a message key (at most 128 characters) plus arguments. `Error.message` is resolved first through the catalog installed by `set_default_messages(Catalog(...), locale)`, then through the SDK's built-in English templates for kernel errors (`BUILT_IN_CATALOG`, which includes the manifest reading errors and matches the spec's kernel-messages vectors character for character, e.g. `Operation 'orders.create' is not registered.`), and is `key (arguments)` when neither can resolve it. Configuration and programming errors (invalid arguments, malformed manifest fields, cyclic pipeline constraints, duplicate operation registration) raise `FrameworkError`, a subclass of `ValueError` that carries an error code; the `FrameworkError` for a duplicate operation registration or a full registry returns, through `to_error()`, an error with a message key and arguments (`kernel.operation.duplicate` / `kernel.operation.registry_full`).
2. Templates follow the "Templates" section of RFC-0010: `{name}` / `{0}`, with `{{` `}}` as escapes. Values are first normalized to the value model (null, boolean, string, 64-bit integer, double-precision float, array, object): a float with 1e-4 ≤ |x| < 1e17 is written in the shortest round-trip fixed-point form (integral values without a decimal point, so `2.0` is written `2`), values outside that range in forms such as `1E+17` and `9.9E-05`, negative zero as `-0`, and NaN and infinities as `NaN`, `Infinity`, `-Infinity`; arrays and objects are written as compact JSON, with object members sorted by the UTF-16 code-unit order of their names, and containers nested deeper than 64 levels are written as `null`. Only the specifiers `D`, `X` / `x`, `F` and `N` are recognized, with up to two digits of precision (`F02` means `F2`); `D` and `X` apply only to integers (`X` only to non-negative integers), and `F` and `N` to integers and floats, rounding the exact value with ties to even (`{n:F2}` of 0.125 gives `0.12`); any other specifier (`E2`, `0.00`, `yyyy-MM-dd`, printf-style `%05d`) and any case where a specifier does not apply are written unformatted. `format_message` / `try_format_message` / `format_argument` accept a `locale`, and `Catalog.resolve` formats for the requested locale: a locale changes only the decimal separator and the group separator (`,` and `.` for de-DE, es-ES and pt-BR, `,` and U+202F for fr-FR, `,` and U+00A0 for ru-RU; locale names are case-insensitive, and any other locale, or no locale, uses `.` and `,`); the decimal separator applies to unformatted floats and to `F` and `N`, the group separator only to `N`, and `D`, `X` and numbers inside arrays and objects are not affected by the locale. `describe` does not vary by locale and lists arguments in the UTF-16 code-unit order of their names. `format_message` formats leniently (a placeholder without a value is kept as is), while `try_format_message` and `Catalog` format strictly (a missing value is a failure, falling back to the description).
3. Requests and responses are dataclasses (or primitives, `list` / `tuple` / `dict` with `str` keys, enums, `X | None`, `Maybe[T]`, `TypedId` subclasses, or types with their own `from_json_value` / `to_json_value`), encoded and decoded with the standard library; rename a member with `field(metadata=operation.json_field("ttlMs"))`. Payloads are decoded per RFC-0011 rule 2, and an invalid one returns `KERNEL_VALIDATION_001` with only a JSON path in `reason`. The shared JSON text check comes first (syntax errors, whitespace only, a BOM, nesting deeper than 64 levels, invalid UTF-8 and lone surrogates are all reported as `$`, and a duplicate member at any level is reported at the path of the first duplicate, ahead of any type problem); decoding by request type follows: member names are case-sensitive, unknown members are ignored, numbers are not read from strings, integers do not accept `1.0` or `1e2` and are a mismatch when out of range, and floats accept integer notation, with `1e400` read as infinity. The first type mismatch in text order is reported at its path (`$.lines[1].quantity`, or `$['unit price']` when the member name contains `.`, a space, a quote, a bracket or parenthesis, `/`, `\` or one of several whitespace control characters), and a missing required member is reported when its enclosing object ends, at that object's path (`$`, `$.inner`); a `null` payload and a payload that is not an object are reported as `$`. An empty payload is equivalent to `{}`. Payload text is compared as JSON values; whitespace, escapes and number notation are not part of the contract. Members typed `JsonValue` / `Any` keep the value as read, with numbers read as `primitives.JsonNumber`, which keeps the original text (a subclass of `float` whose `text` is the original text), and are written back equal to the JSON value read: `1e400` does not become `null` or infinity, integers beyond double precision stay exact, and the distinction between `2.0` and `2` is kept.
4. Handlers and middleware are all `async def`. `Invoker` has three built-in steps, placed per RFC-0011 rule 10: each is placed at `Beginning` of its stage under the owner `xihan.kernel.core` and sorted against other middleware at the same position by ordinal order of owner, and `invoker.dump()` prints the pipeline that actually runs. `ObserveMiddleware` in the Observe stage translates exceptions (`Exception`) raised by the middleware and handler after it into `KERNEL_UNEXPECTED_001` + `kernel.invocation.unhandled_exception`; the error on the wire carries no exception text, and the full exception is logged through the `logging` logger `xihan_framework.operation`. Exceptions raised by middleware ahead of it (in the Ingress stage, or at `Beginning` of the Observe stage with an owner that sorts before `xihan.kernel.core`, such as owner `app`) reach the caller unchanged. `DeadlineMiddleware` in the Resilience stage is `Context.run`: if the deadline has passed on entry, the result is `deadline_before_start`; after that, the context given to later middleware and the handler fires its signal when the deadline arrives or on `ctx.cancellation.cancel()` (callable from any thread). Cancellation and deadlines only notify through the signal (RFC-0003 rule 1): the invoker does not cancel the handler's task, and the handler stops on its own by reading `ctx.is_cancelled`, with `await ctx.cancellation.wait()` or by calling `ctx.cancellation.raise_if_cancelled()`. When it raises a cancellation or writes `KERNEL_CANCELLED_001`, the result is `KERNEL_TIMEOUT_001` if the deadline has arrived and `KERNEL_CANCELLED_001` otherwise; a result returned normally after the signal fires is the result of the call; when the task is cancelled from elsewhere, `CancelledError` propagates as usual. `ValidationMiddleware` in the Validate stage decodes the payload of raw invocations (get the decoded request through `operation.decoded_request_of(ctx)`) and is skipped for typed invocations. Before entering the pipeline, only two things are checked: an unregistered operation (`KERNEL_NOTFOUND_001`) and a type mismatch on a typed invocation (`KERNEL_VALIDATION_002`, whose only argument is `id`). When middleware short-circuits with success and `handle` never runs, both `invoke` and `invoke_typed` return `KERNEL_UNEXPECTED_001` + `kernel.operation.no_response` (rule 9). The XGI entry point encodes the response into a payload only after the pipeline ends, so middleware in the pipeline sees the unencoded object through `operation.response_of(ctx)`; when the response cannot be written (a typed float that is NaN or infinite, or a value that cannot be encoded), the result is `KERNEL_UNEXPECTED_001` + `kernel.invocation.unhandled_exception`, with the framework exception's error placed in `inner`; nothing is raised to the caller, there is no payload, and NaN is not written as `null` or as a string.
5. Manifests: `manifest.read(data)` / `manifest.read_file(path)` return `Result[Manifest]`, always failing with `KERNEL_CAPABILITY_006` and reporting only the first problem, in the order of RFC-0002 parsing rule 2: not JSON is `kernel.manifest.invalid_json` (`reason` is `$` or the path of the first duplicate member; `read` does not skip a BOM, while `read_file` reads UTF-8 and skips a leading UTF-8 BOM), a `null` root is `kernel.manifest.empty`, a Schema violation is `kernel.manifest.invalid` (`reason` is the path of the first problem: members are checked in text order, each one for being outside the Schema, then for `null`, type and value, and once an object has been checked, missing members are reported in the order of `required`), constraints between fields keep the `primitives.manifest.*` keys and the `id` argument (the same set of errors as `Manifest.validate()`, with the code changed to `KERNEL_CAPABILITY_006`), and a missing file is `kernel.manifest.file_missing` (`path`); `message` is the sentence formatted from each key's English template, matching the kernel-messages vectors. The interface version is a positive integer without leading zeros, at most 2147483647. When a plugin declares `contributes.pipeline`, only middleware from Wasm plugins takes effect; the host checks this when it discovers the plugin, and reading does not reject it. `manifest.write(m)` writes per the "Writing" section of RFC-0002: two-space indentation, LF, no trailing newline, a fixed member order, lists always written (an empty list as `[]`), `role` written as `provider` by default, `activation` written as `["onStartup"]` by default, and strings escaping only `"`, `\`, control characters and lone surrogates.

### Write a Plugin

```bash
pip install "xihan-framework[plugin]"
```

grpcio and protobuf are only in the extra `plugin`, and the five core modules do not depend on them; importing `xihan_framework.plugin` without them installed raises an error that gives this install command.

```python
import sys
from dataclasses import dataclass
from pathlib import Path

from xihan_framework import operation
from xihan_framework.invocation import Context
from xihan_framework.plugin import Envelope, Plugin, Subscription, SubscriptionHandler, run
from xihan_framework.primitives import Result, Unit, ok, success


@dataclass(frozen=True)
class Echo:
    text: str


async def echo(ctx: Context, request: Echo) -> Result[Echo]:
    return ok(request)


async def on_event(ctx: Context, envelope: Envelope, attempt: int) -> Result[Unit]:
    # At-least-once delivery: handlers must be idempotent, e.g. deduplicate by the envelope id
    return success()


def build() -> Plugin:
    registry = operation.Registry()
    registry.add(operation.Contract("echoes.create"), echo, request_type=Echo, response_type=Echo)
    return Plugin(
        Path(__file__).with_name("xihan.manifest.json").read_bytes(),
        operations=registry,
        subscriptions=[SubscriptionHandler(Subscription("echo.events"), on_event)],
    )


sys.exit(run(build))
```

The manifest `xihan.manifest.json` sits next to the script in the plugin directory:

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

1. The manifest and the checks before starting: `kind` is `plugin` and `isolation` is `out-of-process`, with no `contributes.pipeline` (only Wasm plugins can contribute middleware), and `primitives` is `1`, the primitives major version this SDK implements; every registered operation must be listed in `contributes.operations`, health items in `contributes.health`, and the configuration section in `contributes.config.section` (the host must also grant `config:<section>`); calling back into the host needs `capability:kernel.pipeline` (`Host.invoke`) and `capability:messaging.queue` (`Host.publish`) declared in `permissions`. `Plugin` checks these on construction, together with the subscriptions (each (topic, type) declared once) and the message catalogs (each has `en-US`, and none uses the runtime catalog's name `plugins.protocol`), and raises `FrameworkError` when they do not hold. The process entry point `run(build)` builds the plugin; when building fails, it does not listen or write the handshake line, logs one `error` line on stderr and exits with code 1, so the host sees the process exit before the handshake (handshake.md §1). The handshake hands the host the manifest text that `Plugin` received, without a leading BOM (the file in the working directory when omitted), which must match the file in the plugin directory.
2. Launch script: `entry` is an executable relative to the plugin directory. The host passes on only a few environment variables such as `PATH`, the user directory and the temporary directory (handshake.md §1), so the script writes absolute paths to the interpreter and the plugin script and does not rely on `VIRTUAL_ENV`. On Unix it is an sh script with a shebang, made executable with `chmod +x`; on Windows it is a `.cmd` file, with `entry` written as `echo.cmd` (without an extension the host appends `.exe`).

   ```sh
   #!/bin/sh
   exec /path/to/.venv/bin/python /path/to/echo/echo.py "$@"
   ```

   ```bat
   @echo off
   "C:\path\to\.venv\Scripts\python.exe" "C:\path\to\echo\echo.py" %*
   ```

3. The plugin's endpoint: the default is a Unix domain socket, used on Linux, macOS and Windows alike (Windows supports AF_UNIX since Windows 10 1803, and grpcio can listen on `unix:` addresses there as well). The socket is placed in a private directory newly created by this process (`mkdtemp`: a random name, permissions `0700`, always newly created and never reused), the socket file is `0600`, and both are removed after stopping; on Windows `chmod` only controls the read-only flag, and the private directory lies in the current user's temporary directory, relying on its access control. A handshake line cannot carry whitespace in its address (the whitespace set of handshake.md §2), so when the path of the temporary directory contains whitespace (such as a user name with a space), the plugin listens on loopback TCP instead. `Plugin(network=Network.TCP)` binds a random port on `127.0.0.1` only and writes `127.0.0.1:<port>` in the handshake line; other users on the same machine can reach it, and isolation relies on the session token alone. grpcio does not support Windows named pipes, so the plugin does not listen on `npipe`, and the access control of named pipes does not concern this SDK.
4. Callback endpoints: `XIHAN_PLUGIN_HOST_ADDRESS` is a list of endpoints in the host's order of preference, separated by `;` (handshake.md §4), and the whole value is invalid when any entry is. The SDK takes the first `unix` or `tcp` entry in order (grpcio cannot dial named pipes) and keeps it; the host always puts loopback TCP last, so the .NET host on Windows (`npipe \\.\pipe\…;tcp 127.0.0.1:…`) is called back over loopback TCP, and hosts on other systems over the Unix domain socket. When the variable is missing, invalid or has no entry it can dial, the plugin serves as usual, logs one `warning` on stderr at startup, and every callback fails with `PLUGINS_UNAVAILABLE_001` + `plugins.host.unavailable` (`reason` is `no supported callback endpoint`) without sending a request.
5. Order, configuration and readiness (lifecycle.md §7.1–§7.3): every RPC first checks the metadata `xihan-plugin-session` in constant time; it must carry exactly one value, and a missing token, two or more values (even when one of them is right) or a wrong one are refused with `Unauthenticated`, ahead of the order check and of reading a stream's first frame. `Handshake` is accepted only in `Declared`, `Configure` only in `Resolved` and `Start` only in `Configured`, and `Stop` is not accepted before the handshake, while stopping or after stopping; a call outside this table is refused with `FailedPrecondition` and leaves the state as it was. `Handshake` picks the highest version in the host's list that both support (`FailedPrecondition` when there is none) and hands over the manifest, declarations of unary operations only (contracts and JSON Schemas), the subscriptions in declaration order with `mode` always written as `competing` or `broadcast`, and the author's own message catalogs. An empty configuration section in `Configure` is equivalent to `{}`; one that is not a JSON object (including one that fails the shared JSON text check) skips `Hooks.configure` and enters `Failed` with `PLUGINS_UNEXPECTED_001` + `plugins.process.failed` (`phase` is `configure`, `reason` is `the configuration is not a JSON object`). The section is handed to the author as is (the host writes every leaf as a string), and `Configuration.decode(T)` reads it by type: member names match case-insensitively in ASCII, string leaves are read as `int` / `float` by the JSON number grammar and as `bool` from `true` / `false` (case-insensitive in ASCII), and a section that cannot be read raises `FrameworkError` with the JSON path. `Start` enters `Started` once the start hook succeeds, probes the health hook right away when there is one, and answers only after entering `Ready`, `Degraded` or `Failed` by the result (any other reported state counts as `Ready`, and a raising hook as `Degraded`); only `Ready` and `Degraded` serve, and calls and deliveries in any other state fail with `PLUGINS_UNAVAILABLE_001` + `plugins.process.unavailable` (`state` is the state's name, such as `Configured`).
6. Health (lifecycle.md §7.4): `Health` and `WatchHealth` can be called in any state; before `Start` they report the local state (`Declared`, `Resolved`, `Configured`) without health items and without calling the hook. While serving, every `Health` probes `Hooks.health` once: a reported `Ready`, `Degraded` or `Failed` takes effect (`Failed` does not come back), and any other state leaves the state unchanged; health items whose names are not in `contributes.health` are dropped, with one `warning` per name; when the hook raises, this result is `Degraded` with `error` set to `plugins.process.failed` (`phase` is `health`) and no health items, and the state moves to `Degraded` just as if the hook had reported it, with later results deciding the state as usual. `WatchHealth` pushes the current state first and then whenever the state, the health items or `error` change, probes the hook again every 10 seconds while serving, pushes once on entering `Draining`, and ends after `Stop` has been answered.
7. Stopping and shutdown (lifecycle.md §7.5–§7.7): when the start hook has never succeeded, `Stop` does not call `Hooks.stop`, answers `ok` and exits with code 0. Once it has, the plugin enters `Draining` (a plugin that entered `Failed` afterwards does not transition), new calls and deliveries return `PLUGINS_UNAVAILABLE_001`, and in-flight calls are awaited up to the grace period (`grace_seconds` of 0 means 5 seconds); calls still running then get their cancellation signals and are not awaited further; `Hooks.stop(reason, grace)` runs next (at most once per process), and the plugin exits with code 0 after answering; a failing or raising stop hook goes into `StopResponse.status`, and the plugin exits as usual. When stdin reaches its end or SIGTERM arrives, a serving plugin enters `Draining`, fires the cancellation signals of all in-flight calls at once and waits for them, and a plugin whose start hook succeeded calls `Hooks.stop("shutdown", …)` once if it has not been called yet; this takes at most 3 seconds from the request (shorter than the 5 seconds the host waits after its termination signal), an in-progress `Stop` is bound by the same limit, and `run` ends the process with code 0 when the limit is reached, finished or not. When a lifecycle or health hook raises, the `reason` handed to the host is only the simple type name of the exception (such as `ValueError`); the exception text, which may hold paths or connection strings, goes only to stderr.
8. Calls and events (invocation.md §1, §2 and §4): `Invoke` checks, in order, the session token, whether the operation is declared (`Unimplemented` otherwise) and whether the plugin is serving, then restores the context from the metadata: `trace_parent` is taken by the grammar of RFC-0003, and an invalid one means no parent span; `trace_state` is not kept in the context; only registered Baggage keys are kept (`xihan.principal` is handed to the author as is); `deadline` is read by the spec's strict grammar (forms such as `2026-11-18 03:25:00Z` do not count), an unreadable one is treated as absent, and the effective deadline is the earlier of it and the gRPC deadline. The call then runs through the `Invoker` and the full pipeline, with an empty `payload` equivalent to `{}`. Cancellation by the caller or an expired deadline only fires the signal and does not cancel the handler's task (RFC-0003 rule 1). This SDK offers no streaming operations: `InvokeStream` reads the first frame and ends with `InvalidArgument` when the stream ends before it or it is not `start`, and with `Unimplemented` when it is `start`, whether or not the operation is declared and the plugin is ready. `HandleEvent` checks, in order: no envelope, or a `mode` other than empty, `competing` and `broadcast`, is `InvalidArgument`; when no subscription matches (topic, mode, type), the result is `KERNEL_NOTFOUND_001` + `plugins.event.unsubscribed` (`id`, `topic`, `type`, `mode`, with an empty mode written as `competing`); a plugin that is not serving returns `PLUGINS_UNAVAILABLE_001`; after that, the matching subscriptions run one after another in declaration order, the first failure is the delivery result, and a raising handler gives `KERNEL_UNEXPECTED_001` + `kernel.invocation.unhandled_exception`.
9. Callbacks (invocation.md §5): `host_of(ctx)` gets the host client (present in the context of every call and delivery the runtime builds; any other context raises `FrameworkError`), with `await host.invoke(ctx, "orders.create", payload)` and `await host.publish(ctx, envelopes)` (batched, with per-item results). Before sending, the SDK checks locally in the host's order and sends nothing when a check fails: the required permission must be both in the manifest's `permissions` and among the permissions granted by `Configure`, otherwise `PLUGINS_DENIED_002` + `plugins.callback.denied`; the context's `xihan.tenant` must be the tenant of a call or delivery the plugin is handling, otherwise `plugins.callback.tenant_denied`; for `publish` both are results for the whole batch; last, nothing is sent when the context's deadline has passed or it has been cancelled either, with the result of the matching row of the mapping table (`DeadlineExceeded` for a passed deadline, `Cancelled` after the caller cancelled for a cancellation). The endpoint is checked before all of these. The envelope's `xihantenant` is not checked locally and is left to the host item by item. Requests carry the session token, and `metadata` comes from the context: the current span's traceparent, `deadline` written in UTC with seven fractional digits and `+00:00` (the gRPC deadline is the same), the registered Baggage, and an empty `trace_state`. gRPC failures are mapped only by the spec's table, without rewriting the result by the clock or the token (a call grpcio aborts at its deadline is already reported as `DeadlineExceeded`): `Unauthenticated` is `plugins.callback.session_invalid`, `PermissionDenied` is `plugins.callback.denied`, `DeadlineExceeded` is `KERNEL_TIMEOUT_001`, `Cancelled` after the caller cancelled is `KERNEL_CANCELLED_001`, any other `Cancelled`, `Unavailable` and connection failures are `plugins.host.unavailable` (`reason` is `<status name>: <message>`), `Unimplemented` and `InvalidArgument` of `Host.invoke` are `kernel.operation.unknown` and `kernel.operation.invalid_payload`, and everything else is `plugins.process.failed`; when the RPC of `publish` fails every item gets the same error, and when the host returns a different number of results than envelopes every item is `plugins.process.failed`. A `Status` written by the host is read by invocation.md §3: an error code that does not match the grammar replaces the whole error with `plugins.process.failed` (`phase` is always `invoke`, for the results of `Host.publish` too, and the original `inner` is dropped); an error with a message key does not take `text`, and a message key that does not match the grammar makes a free-text error (with `code` as its text when `text` is empty or only whitespace); `details` that are empty, whitespace only, an empty object, fail the shared JSON text check, are not an object or hold a number beyond double precision at any level mean no details (an underflowing number reads as 0, and an integer beyond 64 bits reads as a double); `ok` being false without an `error`, or a missing `status`, is `plugins.process.failed` (`phase` is the operation ID or `publish`, `reason` is `the host reported a failure without an error`).
10. Errors, message keys and logs (invocation.md §3, §6 and §8): business failures travel in `Status.error`, and gRPC statuses only signal transport failures. An error with a message key is written with an empty `text`, for the host to translate into the caller's language by its catalogs; only free-text errors (`Error.literal`) write `text`; `details` is the JSON object text of the arguments, empty when there are none, and `inner` is written as well. The keys the runtime shares with the host are `plugins.process.unavailable`, `plugins.process.failed`, `plugins.event.unsubscribed`, `plugins.host.unavailable`, `plugins.callback.denied`, `plugins.callback.tenant_denied` and `plugins.callback.session_invalid`, and an error's `details` are exactly their arguments. The host has a catalog for these keys, so the SDK does not send them in the handshake; `plugin.CATALOG` (catalog name `plugins.protocol`, 10 locales, with the reference implementation's texts) serves local rendering, and the runtime uses it for the error texts in its stderr logs. `run` and `Plugin.run()` attach a `JsonLogHandler` when the root logger has no handlers: one JSON object per line on stderr, with `level` being `trace` / `debug` / `information` / `warning` / `error` / `critical`, plus `message` and the other scope properties.

Verify with the black-box runner (requires Go, version per `spec/conformance/go.mod`):

```bash
cd spec/conformance
go run ./cmd/xihan-conformance plugin --dir <plugin directory> --exclude echoes.crash --sample 'echoes.create={"text":"hi"}' --config '{"greeting":"hello"}'
```

The complete example is `examples/echo`: operations, the deliberately crashing `echoes.crash`, an event subscription, a health item, configuration read by type and host callbacks. `tests/test_plugin_conformance.py` copies it into a temporary directory, writes the launch script and runs all 14 cases of the runner against it; `tests/test_plugin_runtime.py` plays the host, with a fake `Host` service, through each point above, and `tests/test_plugin_process.py` checks the exit codes and the shutdown limit of the process entry point in a subprocess.

## Behavior Shared with the Host

The JSON encoding of `Result`, `Error` and `Maybe` follows the "Result" section of RFC-0001: a success with a value is `{"ok":true,"value":…}`, a success without a value (`success()`) writes only `{"ok":true}`, and a failure is `{"ok":false,"error":{…}}`. There are three ways to read: `Result.from_json_value` (with a value; `value` is required and cannot be `null`), `Result.unit_from_json_value` (without a value; a present `value` is rejected, `{}` and `null` included) and `Result.maybe_from_json_value` (the value is a `Maybe`, and `null` means no value). An `Error`'s `message` is required and must be neither empty nor only whitespace, optional members written as `null` are invalid, and unknown members are ignored; reading from text goes through the shared JSON text check first, and a duplicate member name at any level is invalid, including inside `value`, `details` and `inner`. Numbers round-trip by the value model: integers within the 64-bit range are kept as is (9007199254740993 does not become 9007199254740992), integral floats are written as `2.0`, `-0.0`, `1E+17` and read back as floats, and non-finite floats are written as the strings `NaN`, `Infinity`, `-Infinity`. These rules, together with the lifecycle transition table, the pipeline stage order, error codes, Baggage keys and the grammar of capability requirements, are verified by `spec/conformance/vectors`. The handshake line, version negotiation, callback endpoint list, deadlines, the mapping of callback failures and the runtime's message keys of the plugin protocol are verified by `spec/conformance/vectors/plugin/`; the handshake, session token, call order, lifecycle, unknown operations, declared operations, calls and deliveries before readiness, event delivery, unsubscribed deliveries, the first frame of streaming calls, configuration sections that are not objects, stopping before the start and exit on stdin closure are verified by the spec repository's black-box runner (see [Write a Plugin](#write-a-plugin)).

## Language Mapping

The spec leaves some choices for each language to declare; here is how Python declares them.

- **Required and optional in request types**: a dataclass field without a default is required (except a `Maybe` field, which is no value when omitted); a field with a default (`= 0`, `= None`, `field(default_factory=list)`) is optional and takes the default when missing. Required only means the member must be present; a `null` value counts as present.
- **Members that accept `null`**: only `X | None`, `Maybe[X]` and `JsonValue` / `Any` accept `null`, and `null` means no value (`None`). Strings, arrays and objects that accept `null` in the spec are written in Python as `str | None`, `list[X] | None`, `Inner | None`, and as `list[str | None]` when array items must accept `null`; a member written as `str` or `list[str]` reports `null` as a type mismatch. Integers, floats and booleans are not written with `| None`; for them `null` is a type mismatch, as in the spec.
- **Number types**: `int` is a 64-bit signed integer; Python has no narrower integer types, so a 32-bit integer is written `Annotated[int, operation.Int32]` and other ranges use `operation.IntRange(minimum, maximum)`, with values out of range a type mismatch. `float` is a double-precision float that also accepts integer notation and decodes to a plain `float`; `bool` is not a number. Numbers in `JsonValue` / `Any` members are `JsonNumber` (integers within the 64-bit range are still `int`).
- **The value model for templates**: `None`, `bool`, `str` (including `str` subclasses such as `StrEnum`), `int` (including `IntEnum`; beyond 64 bits it is a float) and `float`; other real numbers such as `Decimal` and `Fraction` become the nearest double-precision float; a `Mapping` is an object (keys take their text), and `list`, `tuple` and other sequences, `set` / `frozenset` and `bytes` are arrays. Values outside the model take `str(value)`: `datetime(2026, 11, 16, 8, 30)` is written `2026-11-16 08:30:00`, a `UUID` in lowercase hyphenated form, and a plain `Enum` as `Color.RED`; these are not portable across languages, so a plugin that needs cross-language formatting converts them to strings first.
- **Values in the wire encoding**: in Result values and error details, an `int` beyond 64 bits is written as a float per the value model (losing precision), and `float('nan')` and infinities are written as strings; in operation responses, non-finite floats cannot be written (see item 4 under [Getting Started](#getting-started)), and a `JsonNumber` is written back as its original text.
- **JSON text and grammars**: reading does not use `json.loads` (which accepts `NaN`, lone surrogates, duplicate members and very deep nesting); the SDK reads per RFC 8259 itself, and surrogate code points in a `str` are treated as invalid Unicode. The spec's regular expressions always use `re.fullmatch`, so a trailing newline does not slip through.
- **Whitespace**: whitespace is the table of 25 code points fixed by RFC-0001 "Whitespace" (U+0009–U+000D, U+0020, U+0085, U+00A0, U+1680, U+2000–U+200A, U+2028, U+2029, U+202F, U+205F, U+3000), not `str.isspace`, which also counts U+001C–U+001F; the handshake line and the `message` and free text of an `Error` use the same table (`xihan_framework.plugin.is_whitespace`). `Error.literal` and `with_text` raise `FrameworkError` (`PRIMITIVES_VALIDATION_001`) for an empty or whitespace-only text.

## Design

Aimed at Python applications, script-style plugins and AI toolchains: Python 3.12+, with handlers and middleware based on asyncio and zero runtime dependencies for the five core modules. The out-of-process plugin runtime is built on `grpc.aio` and shares the event loop with the handlers; grpcio and protobuf are only in the extra `plugin`.

## Development

```bash
git submodule update --init          # spec/
python -m venv .venv                 # on Windows, use py -3.13 -m venv .venv
python -m pip install -e ".[plugin]" --group dev
ruff check && ruff format --check && mypy && pytest
```

`--group` requires pip 25.1 or later. The tests read the vectors under `spec/conformance/vectors/` directly; when the submodule is not checked out, they fail rather than skip. The conformance test `tests/test_plugin_conformance.py` runs the black-box runner in the submodule with `go run` and requires Go (version per `spec/conformance/go.mod`); without Go it fails with the reason given, and to skip it deliberately set `XIHAN_CONFORMANCE_REQUIRED=0`. `tests/test_vectors.py` checks the claimed conformance profiles against the profile table `spec/conformance/profiles.json` (RFC-0024): every vector the claimed profiles require is either replayed by a test or listed as a gap with a reason; when the spec adds a vector to a claimed profile, this test fails until the file is replayed or listed as a gap with its reason.

The protocol code in `src/xihan_framework/plugin/v1/` (messages, services and the type stubs generated by mypy-protobuf) is generated from `spec/plugin/plugin.proto` and committed; regenerate it after the protocol changes:

```bash
python scripts/generate_protocol.py
```

The generators grpcio-tools and mypy-protobuf are pinned in the development dependencies, and CI regenerates the code and checks that nothing differs; the generated files are not formatted or linted by ruff, and mypy checks them through their type stubs.

## Status

`0.1.0` (unreleased): `primitives`, `manifest`, `invocation`, `pipeline`, `operation` and the out-of-process plugin runtime `plugin` (`xihan.plugin.v1`, built on `grpc.aio`, with grpcio and protobuf pulled in through the extra `xihan-framework[plugin]`) are usable, and the example plugin echo passes every case of the spec's black-box runner. The operation sets for remote providers (such as `caching.distributed@1`) are not yet provided; the Wasm carrier is out of scope for this SDK.

Conformance (RFC-0024):

- Pinned spec version: commit `386501f` (the `spec/` submodule); the spec has no tag yet, and its first tag is planned as `v1.0.0-rc.1`.
- Claimed profiles: `core` and `plugin`.
- Gaps: none.

Until there is a release pinned to a spec tag, the maturity is Experimental.

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

[Issues](https://github.com/XiHanFun/framework-sdk-python/issues) and pull requests are welcome.

1. Start with the README and the relevant RFCs of framework-spec.
2. Changes to public contracts are not made in the SDK repositories: they go through the RFC process of framework-spec and are then synced to each SDK.

## License

[MIT](./LICENSE) © 2021-Present XiHanFun and contributors.
