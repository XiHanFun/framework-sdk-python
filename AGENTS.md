# AGENTS.md

本文件约束在 framework-sdk-python 仓库内工作的 AI Agent。回答、文档与提交说明使用中文；代码标识、公开 API 和标准技术术语保持英文。

## 仓库概览

framework-sdk-python 是 [XiHan Spec](https://github.com/XiHanFun/framework-spec) 的 Python 实现：原语层、清单、调用上下文、管道、操作契约与进程外插件运行时。语义与线上编码以规范为准，行为与 .NET 参考实现 [XiHan.Framework](https://github.com/XiHanFun/XiHan.Framework) 一致（结果取决于平台内部的地方由 ADR-0236 写死在规范里）。PyPI 包名 `xihan-framework`，导入名 `xihan_framework`；核心五块（`primitives`、`manifest`、`invocation`、`pipeline`、`operation`）运行时零依赖，进程外插件运行时 `plugin` 基于 `grpc.aio`，grpcio 与 protobuf 只在 extra `plugin` 里。

### 技术栈

| 技术 | 要求 | 用途 |
| --- | --- | --- |
| Python | 3.12 及以上（`pyproject.toml` 的 `requires-python`），CI 跑 3.12 与 3.13 | 运行与开发 |
| asyncio | 标准库 | 处理器、中间件与插件运行时共用一个事件循环 |
| grpcio、protobuf | extra `plugin`，版本范围见 `pyproject.toml`；protobuf 的大版本与生成协议代码的 protoc 相同 | 进程外插件运行时 |
| hatchling | `pyproject.toml` 的 `build-system` | 构建 sdist 与 wheel，版本从 `src/xihan_framework/__init__.py` 读 |
| pytest | 开发依赖组 `dev` | 单元测试、向量回放、插件运行时与黑盒一致性测试 |
| ruff | 开发依赖组里锁定版本 | lint 与格式 |
| mypy | strict，开发依赖组（含 types-grpcio、types-protobuf 存根） | 类型检查 `src`、`tests`、`examples`、`scripts` |
| grpcio-tools、mypy-protobuf | 开发依赖组里锁定版本 | 从 `spec/plugin/plugin.proto` 生成协议代码与类型存根 |
| pip | 25.1 及以上（`--group` 需要） | 安装开发依赖 |
| Go | 版本见 `spec/conformance/go.mod` | 运行规范仓库的黑盒运行器 |

## 开始前必须做

1. 按任务读取对应技能：原语、清单、调用上下文、管道与操作契约读取 `.agents/skills/sdk-development/SKILL.md`；进程外插件运行时、协议代码与黑盒运行器读取 `.agents/skills/plugin-runtime/SKILL.md`；门禁、向量覆盖与打包验证读取 `.agents/skills/testing-release/SKILL.md`；版本与发布读取 `.agents/skills/version-release/SKILL.md`；整理破坏性改动与迁移读取 `.agents/skills/changelog-collect/SKILL.md`；生成提交信息读取 `.agents/skills/commit-msg/SKILL.md`；准备或创建 PR 读取 `.agents/skills/create-pr/SKILL.md`；调查与回复 Issue 读取 `.agents/skills/issue-reply/SKILL.md`；审查测试读取 `.agents/skills/test-review/SKILL.md`。
2. 同时涉及多个职责时加载对应多个技能，不要一次读取无关资料。
3. 检查当前分支、`git status` 和最近提交；保留用户已有改动。用 `git submodule status` 确认 `spec/` 已检出。
4. 读相关规范章节：`spec/rfcs/` 里对应的 RFC（0001 原语、0002 清单、0003 调用上下文、0004 管道阶段、0010 本地化、0011 操作、0017 插件协议、0024 版本与一致性档）；插件运行时另读 `spec/plugin/handshake.md`、`lifecycle.md`、`invocation.md` 与 `plugin.proto`；错误码、Baggage 键、能力 ID 与术语在 `spec/conventions/`，向量在 `spec/conformance/vectors/`。只读与任务相关的部分。
5. 阅读目标模块 `src/xihan_framework/<模块>/`（公开名在 `__init__.py` 的 `__all__` 里，实现在 `_` 开头的私有模块里）及其测试 `tests/test_<模块>.py`。
6. 先找已有实现：JSON 文本的共同读取检查与值模型在 `_json.py`，值与 JSON 值的编解码在 `_codec.py`，规范的空白表在 `_whitespace.py`；不重复建设相同能力。

## 目录结构

```text
/
├─ AGENTS.md
├─ .agents/skills/                        # 按开发、插件运行时、测试和发布分类的仓库技能
├─ .claude/skills -> ../.agents/skills    # Claude Code 技能兼容入口
├─ CLAUDE.md -> AGENTS.md                 # Claude Code 仓库指令入口
├─ .github/workflows/ci.yml               # 门禁：生成代码一致、ruff、mypy、pytest（Linux 与 Windows）、分发包内容
├─ .github/workflows/release.yml          # 发布：推送 v* 标签触发，核对后发布到 PyPI
├─ pyproject.toml                         # 包元数据、extra plugin、开发依赖组、ruff / mypy / pytest 配置
├─ README.md / README_cn.md               # 概览，两份同步；README.md 也是 PyPI 上的项目说明
├─ spec/                                  # 规范（XiHanFun/framework-spec 的 submodule）：RFC、术语、插件协议、一致性向量与黑盒运行器
├─ src/xihan_framework/
│  ├─ __init__.py                         # __version__（版本真源）与 SPEC_VERSION
│  ├─ _json.py / _codec.py / _whitespace.py  # 内部共用：JSON 文本检查与值模型、编解码、空白表
│  ├─ primitives/                         # Error、Result、Maybe、TypedId、消息键与模板、Catalog、FrameworkError
│  ├─ manifest/                           # xihan.manifest.json 的模型、读取与写出
│  ├─ invocation/                         # Context：取消、截止时间、W3C 追踪、Baggage、对象袋
│  ├─ pipeline/                           # 固定阶段的 Builder
│  ├─ operation/                          # Contract、Registry、Invoker（XGI）
│  └─ plugin/                             # 进程外插件运行时（grpc.aio）
│     ├─ Localization/                    # 运行时消息目录，10 种区域
│     └─ v1/                              # 由 spec/plugin/plugin.proto 生成的协议代码与类型存根，提交进仓库
├─ tests/                                 # pytest：单元、向量回放、插件运行时、进程入口与黑盒一致性
├─ examples/echo/                         # 示例插件，也是黑盒运行器的被测插件
└─ scripts/
   ├─ generate_protocol.py                # 重新生成协议代码
   └─ release-check.sh                    # 发版核对：打标签之前与 release.yml 的 guard 都跑它
```

## 常用命令

在仓库根、虚拟环境里执行（激活 `.venv`，或直接用 `.venv/Scripts/python` / `.venv/bin/python` 调 `-m ruff`、`-m mypy`、`-m pytest`；Windows 上 PATH 里的 `python` 可能是应用商店的别名，不要依赖它）：

| 任务 | 命令 |
| --- | --- |
| 初始化规范 | `git submodule update --init` |
| 建虚拟环境 | `python -m venv .venv`（Windows 上用 `py -3.13 -m venv .venv`） |
| 安装包与开发依赖 | `python -m pip install -e ".[plugin]" --group dev` |
| 生成协议代码 | `python scripts/generate_protocol.py` |
| 核对生成代码 | `git diff --exit-code -- src/xihan_framework/plugin/v1` |
| 静态检查 | `ruff check` |
| 格式检查 | `ruff format --check` |
| 类型检查 | `mypy` |
| 测试 | `pytest` |
| 全部门禁 | `ruff check && ruff format --check && mypy && pytest` |
| 黑盒运行器 | `cd spec/conformance && go run ./cmd/xihan-conformance plugin --dir <插件目录> --exclude echoes.crash --sample 'echoes.create={"text":"hi"}' --config '{"greeting":"hello"}'` |
| 构建分发包 | `python -m build`（需要 `build`：`python -m pip install build`） |
| 发版核对 | `bash scripts/release-check.sh vX.Y.Z main`（打标签之前跑，联网取 `origin/main` 与规范的远端标签） |

`.github/workflows/ci.yml` 是门禁命令的事实源；分发包内容的核对见 `.agents/skills/testing-release/references/gates.md`。

## 规范与一致性

- 规范是事实源：以 `spec/` 子模块钉的 framework-spec 提交为准，语义与线上编码按规范写。
- .NET（XiHan.Framework）是参考实现，行为与它一致；结果取决于平台内部的地方（数字写法、格式说明符、区域符号等）以规范写死的为准，不用 Python 内建行为凑。
- 向量回放：测试经 `tests/conftest.py` 的 `vector` fixture 直接读取 `spec/conformance/vectors/`，不复制向量文件；子模块没有检出时测试失败而不是跳过。
- 一致性档（RFC-0024）：`tests/test_vectors.py` 读档表 `spec/conformance/profiles.json`，认领的档写在 `CLAIMED_PROFILES`（`core`、`plugin`）；认领的档要求的每个向量要么列在 `REPLAYED` 里由测试回放，要么列在 `GAPS` 里写明理由。规范在认领的档里新增向量时这个测试失败，直到回放它或列为缺口。
- 黑盒运行器：`tests/test_plugin_conformance.py` 用子模块里的运行器跑示例插件 echo 的全部用例。
- 规范没写到的行为不自行发明：停下来报告差异（现状、.NET 的做法、各候选的后果），提出规范文字，在 framework-spec 提交。
- 公开契约的变化顺序：framework-spec 的 RFC → XiHan.Framework 的 ADR（`plans/adr`）→ 各 SDK。
- 升规范指针：
  1. `git -C spec fetch origin` 后 `git -C spec checkout <提交或标签>`；只指向 framework-spec 已推送的提交（RFC-0024「实现钉规范」，CI 检出子模块也要拉得到）。
  2. 读这段范围的规范改动（`git -C spec log --oneline <旧>..<新>` 与 `spec/CHANGELOG.md`），补实现；协议变了重新生成协议代码；新向量回放或列为缺口。
  3. 跑全部门禁。
  4. 指针与它带来的实现、README 与文档页的同步放在同一提交，说明写实现的结果并注 ADR；只升指针的提交写 `build(spec): 规范指针升到 <短哈希>（ADR-NNNN）`。

## 代码规范

- 模块、类、函数与方法都写中文 docstring：首行一句话，细节另起段落，代码与标识用 reStructuredText 的双反引号包住，并写明依据的规范位置（如「RFC-0003 规则 1」「invocation.md §5」「ADR-0236」）。常量与模块级变量用 `#:` 中文文档注释。测试函数用英文句子命名、不写 docstring。
- 关键逻辑（状态迁移、核对顺序、取消与截止时间的归因、平台差异、为什么不用内建函数）用中文 `#` 注释说明为什么，不复述代码，句末不加「。」。
- 运行时英文：`FrameworkError` / `ValueError` 的消息、日志、`pytest.fail` 与断言文本用英文，句子以句号结尾，标识符用单引号。
- 结构与命名：每个子包的 `__init__.py` 重新导出公开名并列出 `__all__`，实现放 `_` 开头的私有模块；跨子包共用的内部实现放在包根的 `_json.py`、`_codec.py`、`_whitespace.py`。命名按 PEP 8（ruff 的 `N` 规则执行），常量大写并标 `Final`；值类型不可变（`@dataclass(frozen=True)` 或带 `__slots__` 的类），`with_*` 返回新值。
- 工具：ruff 的规则集与行宽 120 见 `pyproject.toml`（`E501` 与中文全角标点的 `RUF001`–`RUF003` 有意忽略），`ruff format` 统一格式，mypy strict；生成的 `plugin/v1/plugin_pb2*` 不经 ruff，mypy 按它们的 `.pyi` 检查。`# noqa` 与 `# type: ignore` 只在有理由的地方带具体规则码，不整文件关闭检查。
- 错误：可恢复失败返回 `Result`（`ok(...)`、`success()`、`fail(Error...)`），不抛异常；配置与编程错误抛 `FrameworkError`（`ValueError` 的子类，带错误码）。错误码按 `<DOMAIN>_<KIND>_<NNN>`，登记在 `spec/conventions/error-codes.md`。
- 文案只引用消息键：`Error` 带消息键与参数，模板在目录里；SDK 自带的内核英文模板 `BUILT_IN_CATALOG` 与 kernel-messages 向量逐字相同；插件运行时的目录是 `plugin/Localization/<区域>.json`，10 种区域键集与占位符一致（测试核对）。源码里 `Error.literal` 只用于读入线上的自由文本错误与 `FrameworkError.to_error`。
- 异步、取消与截止时间：处理器与中间件都是 `async def`；取消与截止时间只经 `Context` 的信号通知，不取消处理器的任务（RFC-0003 规则 1）；宿主回调把上下文的截止时间带成 gRPC 截止时间。
- JSON 与文法：读线上文本不用 `json.loads`，走 `_json` 的共同读取检查；规范的正则一律 `re.fullmatch`；空白用 `_whitespace` 的 25 码位表，不用 `str.isspace`。
- 依赖：核心五块零运行时依赖，不导入 grpc 与 protobuf；新增依赖要有明确收益，并放进合适的 extra 或依赖组。
- 有界：集合要有上限（如 `operation.Registry` 的容量 10000、Baggage 值最多 256 个 UTF-16 码元、示例插件只保留最近 1000 个事件 ID），不建无界的缓存、队列或注册表。

## 测试

- 改哪个模块，就在对应的 `tests/test_<模块>.py` 添加或更新测试；插件运行时的测试分在 `test_plugin_protocol.py`（协议向量）、`test_plugin_runtime.py`（测试扮演宿主与假的 `Host` 服务）、`test_plugin_process.py`（子进程里的退出码与收尾时限）与 `test_plugin_conformance.py`（黑盒运行器）。
- 向量经 `vector` fixture 回放；新回放的向量文件加进 `tests/test_vectors.py` 的 `REPLAYED`。
- 插件运行时用黑盒运行器在 Windows 与 Linux 上跑全部用例（CI 的矩阵两种系统都有）；没有 Go 时一致性测试失败，只有确实要跳过时才设 `XIHAN_CONFORMANCE_REQUIRED=0`，CI 不设。Windows 没有 SIGTERM，相应用例在 Windows 上跳过。
- pytest 配置为警告即错误（`filterwarnings = ["error"]`）、`--strict-markers`、`xfail_strict`，不放宽。
- 修复缺陷先写能复现问题的测试，再改实现。
- 不删除测试、不放宽门禁来通过 CI。

## 文档

- README 只放概览、安装、一个最小示例与链接；README.md（英文）与 README_cn.md（中文）内容同步。README.md 是 PyPI 的项目说明（`pyproject.toml` 的 `readme`）。
- 详细文档在 XiHan.Framework 仓库的 `docs/sdks/python.md`（文档站 `https://framework.docs.xihanfun.com/sdks/python`）。本地克隆在 `../XiHan.Framework`，是另一个仓库，单独提交；文档构建在那边执行 `pnpm --dir docs build`。
- 文档正文不写版本号：包版本、规范提交哈希、测试数量都指向真源（`src/xihan_framework/__init__.py`、`spec/` 子模块）。
- 改公开 API 或行为时，同步 docstring、README 两份、文档页的相应章节与其中的「破坏性改动与迁移」。

## 发布

- 版本真源是 `src/xihan_framework/__init__.py` 的 `__version__`：hatchling 构建时从这里读（`[tool.hatch.version]`），`primitives.SDK_VERSION` 与握手里的 SDK 版本也取它，`tests/test_package.py` 核对它与分发元数据一致。
- PyPI 包名 `xihan-framework`，导入名 `xihan_framework`；PyPI 上最早的 `0.0.1` 是占名用的，不是本仓库发布的版本；目前只有预发布版，README 与文档页的安装命令带 `==<版本>` 占位，首个正式版发布后去掉。
- 发布由 `.github/workflows/release.yml` 在推送 `v*` 标签时执行：guard 跑 `scripts/release-check.sh`（标签在 `main` 上、标签换成 PEP 440 后等于 `__version__`、规范钉在标签上），通过后构建并以 PyPI 可信发布（OIDC）上传；GitHub Release 与 XiHan.Framework 一样在标签推送后手动建。打标签之前先在本地跑同一份脚本；打标签与推送只在用户明确要求时执行，步骤见 `version-release` 技能。首次发布前要用户在 pypi.org 配可信发布者。
- `__version__` 按 PEP 440 的规范写法（预发布如 `0.1.0rc1`），标签按语义化版本（`v0.1.0-rc.1`），预发布只能用 `-alpha.N`、`-beta.N`、`-rc.N`。
- 按 RFC-0024「实现钉规范」，发预发布或正式版时 `spec/` 子模块必须正好在某个规范标签上，正式版只能钉规范的正式版标签。

## 分支

- 日常改动直接提交到 `dev`。
- `main` 只在用户明确说「推送并快进 main」时由 `dev` 快进（`git fetch . dev:main`，不是快进就停下查）。
- CI（`.github/workflows/ci.yml`）在 `main` 与 `dev` 的 push、指向 `main` 与 `dev` 的 Pull Request 上运行，也可手动触发（`workflow_dispatch`）；同一引用上的新运行会取消旧运行。
- 发版工作流（`.github/workflows/release.yml`）只在推送 `v*` 标签、或手动运行时选中版本标签时运行，见「发布」与 `version-release` 技能。

## Git 与提交

- 使用 Conventional Commits：`<type>(<scope>): <中文说明>`，scope 用本仓库的模块名（`primitives`、`manifest`、`invocation`、`pipeline`、`operation`、`plugin`、`conformance`）或 `spec`、`docs`、`build`、`ci`。
- 提交信息只写一行标题，不写正文。说明一句话讲这次改动的结果，不超过 40 个字（汉字、字母、数字、标点各算一个，末尾的 `（ADR-NNNN）` 不计）；不再用「：」「；」往下展开，不罗列文件、成员、取值与改前改后，不写测试数量与门禁结果，这些细节写进 文档页、ADR 或 PR 描述。细则见 `commit-msg` 技能。
- 保留用户已有和无关改动；不要 reset、checkout 或清理它们，只暂存自己的文件。
- 每项能力或独立修复单独提交；实现、测试、README 与规范指针属于同一闭环时放在同一提交。
- 提交前检查 `git diff --check` 和相关门禁。
- 不推送、不发布、不创建远程 PR，除非用户明确要求。

## 禁止事项

- 不自行发明规范没写到的行为，不为凑结果偏离规范或 .NET 参考实现。
- 不手改生成的协议代码（`src/xihan_framework/plugin/v1/`），改 `.proto` 走规范仓库，再重新生成。
- 不在核心五块引入运行时依赖，不添加静默兜底、无限重试或无界集合。
- 不删除测试、不放宽 ruff / mypy / pytest 配置或 CI 门禁来通过检查。
- 不把 `spec/`、`tests/`、`examples/` 放进分发包。
- 不猜测 API、版本或 CI 命令；以当前检出为准。
- 不推送、不打标签、不上传 PyPI，除非用户明确要求。
