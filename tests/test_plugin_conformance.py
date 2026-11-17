"""黑盒一致性：把示例插件 echo 放进临时目录，用规范仓库的运行器跑全部用例（``spec/plugin/invocation.md`` §7）。

运行器在子模块的 ``spec/conformance`` 里以 ``go run`` 运行，版本与锁定的规范一致；没有 Go 时跳过并给出原因，设置了
``XIHAN_CONFORMANCE_REQUIRED`` 时（CI）改为失败。
宿主只继承 handshake.md §1 列出的环境变量，所以启动脚本里写解释器与插件脚本的绝对路径，不依赖 ``VIRTUAL_ENV``。
"""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "echo"
RUNNER = ROOT / "spec" / "conformance"
#: 黑盒运行器的全部用例（spec/conformance/README.md）。
CASES = {
    "handshake",
    "refusal",
    "session",
    "lifecycle",
    "order",
    "unknown-operation",
    "declared-operations",
    "unavailable",
    "event",
    "unsubscribed",
    "stream",
    "configuration",
    "stop-early",
    "stdin",
}


def write_launcher(directory: Path) -> str:
    """在插件目录里写启动脚本，返回清单 ``entry`` 该写的文件名：Unix 上是带 shebang 的 sh，Windows 上是 ``.cmd``。"""
    script = directory / "echo.py"
    if sys.platform == "win32":
        launcher = directory / "echo.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return launcher.name
    launcher = directory / "echo"
    # exec 让解释器替换 sh：stdin、信号与退出码都直接落在插件进程上
    launcher.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n', encoding="utf-8"
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher.name


def test_the_echo_plugin_passes_every_black_box_case(tmp_path: Path) -> None:
    go = shutil.which("go")
    if go is None:
        reason = "Go is not installed: the black-box runner of the spec (spec/conformance) is a Go program."
        # CI 装了 Go 并设置 XIHAN_CONFORMANCE_REQUIRED，那里跳过即失败，免得一致性测试悄悄不跑
        if os.environ.get("XIHAN_CONFORMANCE_REQUIRED"):
            pytest.fail(reason)
        pytest.skip(reason)
    directory = tmp_path / "echo"
    shutil.copytree(EXAMPLE, directory, ignore=shutil.ignore_patterns("__pycache__"))
    manifest_path = directory / "xihan.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entry"] = write_launcher(directory)
    # 插件在握手里交的就是这个文件，所以只改 entry 也照样逐字段一致
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    command = [
        go,
        "run",
        "./cmd/xihan-conformance",
        "plugin",
        "--dir",
        str(directory),
        "--json",
        "--quiet",
        "--config",
        '{"greeting":"hello"}',
        "--sample",
        'echoes.create={"text":"hi"}',
        "--exclude",
        "echoes.crash",
    ]
    completed = subprocess.run(
        command,
        cwd=RUNNER,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
        env=os.environ.copy(),
        check=False,
    )
    # 退出码 0 为全部通过，1 为有用例失败（结果里有原因），2 为参数或目录错误
    assert completed.returncode in (0, 1), completed.stderr
    results = json.loads(completed.stdout)
    assert {result["case"] for result in results} == CASES
    failures = [result for result in results if not result["passed"] or result["skipped"]]
    assert not failures, failures
    assert completed.returncode == 0
