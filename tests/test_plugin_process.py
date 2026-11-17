"""插件进程的入口 ``run``：以子进程运行，核对启动前的拒绝与收尾的时限和退出码（handshake.md §1、lifecycle.md §7.7）。"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import grpc
import pytest

from plugin_support import environment, handshake_request, manifest_text, session
from xihan_framework.plugin import parse_handshake_line
from xihan_framework.plugin.v1 import plugin_pb2 as pb
from xihan_framework.plugin.v1 import plugin_pb2_grpc as pb_grpc

# 插件脚本：probes.wait 既不理取消信号也不理任务取消，只有进程退出才能结束它
SCRIPT = """
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from xihan_framework import operation
from xihan_framework.plugin import Plugin, run
from xihan_framework.primitives import Result


@dataclass(frozen=True)
class Empty:
    pass


async def stuck(ctx, request: Empty) -> Result[Empty]:
    while True:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass


def build() -> Plugin:
    registry = operation.Registry()
    registry.add(operation.Contract("probes.wait"), stuck, request_type=Empty, response_type=Empty)
    return Plugin((Path(__file__).parent / "xihan.manifest.json").read_bytes(), operations=registry)


sys.exit(run(build))
"""


def launch(directory: Path, manifest: str) -> subprocess.Popen[bytes]:
    """在插件目录里以子进程启动插件脚本，环境变量与宿主下发的相同。"""
    (directory / "xihan.manifest.json").write_text(manifest, encoding="utf-8")
    (directory / "plugin.py").write_text(SCRIPT, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(directory / "plugin.py")],
        cwd=directory,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **environment()},
    )


def stuck_and_terminated(directory: Path, terminate: str) -> tuple[float, int]:
    """启动插件、让一次调用卡住，再以 ``terminate``（``stdin`` 或 ``sigterm``）要求收尾；返回从请求到退出的秒数与退出码。"""
    with launch(directory, manifest_text()) as process:
        # 读握手行是阻塞的：插件起不来时到时杀掉进程，测试照常失败而不是挂住
        guard = threading.Timer(60, process.kill)
        guard.start()
        try:
            assert process.stdout is not None
            assert process.stdin is not None
            line = process.stdout.readline().decode("utf-8").removesuffix("\n")
            endpoint = parse_handshake_line(line)
            assert endpoint is not None, line
            assert endpoint.grpc_target is not None
            with grpc.insecure_channel(endpoint.grpc_target) as channel:
                stub = pb_grpc.PluginStub(channel)
                stub.Handshake(handshake_request(), metadata=session(), timeout=10)
                configured = stub.Configure(pb.ConfigureRequest(configuration="{}"), metadata=session(), timeout=10)
                assert configured.status.ok
                assert stub.Start(pb.StartRequest(), metadata=session(), timeout=10).status.ok
                call = stub.Invoke.future(pb.InvokeRequest(operation="probes.wait"), metadata=session())
                time.sleep(0.5)
                began = time.monotonic()
                if terminate == "stdin":
                    process.stdin.close()
                else:
                    process.send_signal(signal.SIGTERM)
                code = process.wait(timeout=20)
                elapsed = time.monotonic() - began
                call.cancel()
        finally:
            guard.cancel()
            process.kill()
    return elapsed, code


def test_closing_stdin_exits_with_code_zero_within_three_seconds_even_with_stuck_calls(tmp_path: Path) -> None:
    elapsed, code = stuck_and_terminated(tmp_path, "stdin")
    assert code == 0
    # 3 秒的时限加上进程退出本身
    assert elapsed < 4.5


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no SIGTERM; the host closes stdin instead.")
def test_sigterm_exits_with_code_zero_within_three_seconds_even_with_stuck_calls(tmp_path: Path) -> None:
    elapsed, code = stuck_and_terminated(tmp_path, "sigterm")
    assert code == 0
    assert elapsed < 4.5


def test_a_refused_plugin_logs_an_error_and_writes_no_handshake_line(tmp_path: Path) -> None:
    # 清单的 primitives 不是本 SDK 实现的版本：不监听、不写握手行，stderr 记一条 error（handshake.md §1 末段）
    with launch(tmp_path, manifest_text(primitives="2")) as process:
        stdout, stderr = process.communicate(timeout=60)
    assert process.returncode == 1
    assert stdout == b""
    entries = [json.loads(line) for line in stderr.decode("utf-8").splitlines() if line.startswith("{")]
    assert [entry["level"] for entry in entries] == ["error"]
    assert "primitives 2" in entries[0]["message"]
