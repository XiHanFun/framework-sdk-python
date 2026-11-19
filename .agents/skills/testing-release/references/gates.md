# 门禁

## CI（`.github/workflows/ci.yml`）

触发：`main` 与 `dev` 的 push，指向 `main` 与 `dev` 的 Pull Request，手动 `workflow_dispatch`；同一引用上的新运行取消旧运行。

### 检查与测试（矩阵：ubuntu-latest、windows-latest × Python 3.12、3.13）

| 步骤 | 命令 | 失败时 |
| --- | --- | --- |
| 检出含规范子模块 | `actions/checkout`，`submodules: true` | 子模块指针要指向 framework-spec 已推送的提交 |
| 安装 Go | 版本取 `spec/conformance/go.mod` | 与锁定的规范一致，不另指定 |
| 安装 | `python -m pip install --upgrade pip`，`python -m pip install -e ".[plugin]" --group dev` | `--group` 需要 pip 25.1 以上 |
| 生成的协议代码与 `.proto` 一致（只在 Linux） | `python scripts/generate_protocol.py`，`git diff --exit-code -- src/xihan_framework/plugin/v1`，且 `git status --porcelain` 在该目录下为空 | 重新生成并提交生成物，不手改 |
| 格式与静态检查 | `ruff check`，`ruff format --check` | 修代码或 `ruff format`；不改规则集 |
| 类型检查 | `mypy` | strict；补类型，不加无规则码的 `# type: ignore` |
| 测试 | `pytest` | 见下 |

### 分发包（ubuntu-latest，Python 3.13）

```bash
python -m pip install --upgrade pip build
python -m build
```

然后核对：

- sdist（`dist/*.tar.gz`）与 wheel（`dist/*.whl`）里没有 `spec/`、`tests/`、`examples/`。
- wheel 里有 `xihan_framework/py.typed`、`xihan_framework/plugin/v1/plugin_pb2_grpc.pyi` 与 `xihan_framework/plugin/Localization/zh-CN.json`。

本地复现：

```bash
tar -tzf dist/*.tar.gz | grep -E '/(spec|tests|examples)/'          # 应当没有输出
python -m zipfile -l dist/*.whl | grep -E '(^| )(spec|tests|examples)/'   # 应当没有输出
python -m zipfile -l dist/*.whl | grep -E 'py.typed|plugin_pb2_grpc.pyi|Localization/zh-CN.json'
```

`dist/` 在 `.gitignore` 里；本地验证可用 `python -m build --outdir <临时目录>`，免得覆盖已有产物。sdist 的内容由 `pyproject.toml` 的 `[tool.hatch.build.targets.sdist]` 决定（`include` 的路径以 `/` 开头锚定在仓库根，否则会匹配到 `spec/` 里的同名文件）。

## pytest 配置

`pyproject.toml` 的 `[tool.pytest.ini_options]`：`testpaths = ["tests"]`，`-ra --strict-markers --strict-config`，`xfail_strict = true`，`filterwarnings = ["error"]`（警告即失败）。不放宽这些设置。

## 向量覆盖（RFC-0024）

`tests/test_vectors.py` 读 `spec/conformance/profiles.json`：

- `CLAIMED_PROFILES`：认领的档（`core`、`plugin`）。
- `REPLAYED`：本 SDK 回放的向量文件（相对 `vectors/`），每行注释写回放它的测试文件。
- `GAPS`：认领的档要求、却没有回放的向量 → 理由；目前为空。

测试失败的几种情形与处理：

| 报错 | 处理 |
| --- | --- |
| 认领的档里有既没回放也没列为缺口的向量 | 写测试回放它（经 `vector` fixture）并加进 `REPLAYED`；确实做不到时列进 `GAPS` 写理由，同步 README 与文档页的缺口 |
| 缺口在认领的档之外 | 从 `GAPS` 删掉 |
| 缺口已被回放 | 从 `GAPS` 删掉 |
| 回放的文件规范里没有 | 规范改了名或删了文件，跟着改测试 |
| 认领的档规范里没定义 | 核对档名与规范版本 |

## 黑盒一致性

`tests/test_plugin_conformance.py` 用 `go run` 运行子模块里的运行器跑示例插件 echo 的全部用例；没有 Go 时失败并给出原因，`XIHAN_CONFORMANCE_REQUIRED=0` 才跳过，CI 不设这个变量。用例表 `CASES` 与 `spec/conformance/README.md` 的运行器用例一致。
