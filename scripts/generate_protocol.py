"""从 ``spec/plugin/plugin.proto`` 重新生成 ``xihan_framework.plugin.v1`` 的协议代码（``xihan.plugin.v1``，RFC-0017）。

在仓库根、装好开发依赖（``pip install -e ".[plugin]" --group dev``）之后执行::

    python scripts/generate_protocol.py

grpcio-tools 生成消息（``plugin_pb2.py``）与服务（``plugin_pb2_grpc.py``），mypy-protobuf 生成两者的类型存根（``.pyi``）。
``.proto`` 以虚拟路径 ``xihan_framework/plugin/v1/plugin.proto`` 编译，生成代码里的导入因此是包内的绝对导入。
生成物提交进仓库；两个工具的版本在 ``pyproject.toml`` 里锁定，CI 重新生成并核对没有差异。
"""

import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO = ROOT / "spec" / "plugin" / "plugin.proto"
PACKAGE = "xihan_framework/plugin/v1"
OUTPUT = ROOT / "src"


def _plugin(name: str) -> str:
    """protoc 插件的可执行文件：与当前解释器装在同一个脚本目录里，不依赖 PATH 或激活的虚拟环境。"""
    found = shutil.which(name, path=sysconfig.get_path("scripts")) or shutil.which(name)
    if found is None:
        raise SystemExit(f"{name} was not found; install the development dependencies first.")
    return found


def main() -> int:
    """调用 grpcio-tools 自带的 protoc 生成代码与类型存根。"""
    if not PROTO.is_file():
        raise SystemExit(f"{PROTO} is missing; run 'git submodule update --init'.")
    command = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--proto_path={PACKAGE}={PROTO.parent}",
        f"--plugin=protoc-gen-mypy={_plugin('protoc-gen-mypy')}",
        f"--plugin=protoc-gen-mypy_grpc={_plugin('protoc-gen-mypy_grpc')}",
        f"--python_out={OUTPUT}",
        f"--grpc_python_out={OUTPUT}",
        f"--mypy_out={OUTPUT}",
        f"--mypy_grpc_out={OUTPUT}",
        str(PROTO),
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
