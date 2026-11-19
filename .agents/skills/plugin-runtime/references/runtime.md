# 插件运行时的验证与平台差异

## 黑盒运行器

规范仓库的运行器在子模块 `spec/conformance` 里，是 Go 程序，版本见 `spec/conformance/go.mod`：

```bash
cd spec/conformance
go run ./cmd/xihan-conformance plugin --dir <插件目录> --exclude echoes.crash --sample 'echoes.create={"text":"hi"}' --config '{"greeting":"hello"}'
```

常用参数（完整说明见 `spec/conformance/README.md`）：`--dir` 插件目录（含 `xihan.manifest.json` 与 `entry` 指向的可执行文件），`--config` 下发的配置节，`--sample op=json` 请求样例，`--exclude op` 不调用的操作（如故意崩溃的 `echoes.crash`），`--case name` 只跑指定用例，`--json` 输出 JSON，`--quiet` 不转发插件的 stderr。退出码 0 为全部通过，1 为有用例失败，2 为参数或目录错误。

`tests/test_plugin_conformance.py` 把 `examples/echo` 复制进临时目录、按当前系统写启动脚本并改清单的 `entry`，再用运行器跑全部用例，核对用例集合与运行器的用例表（`CASES`）相同、没有失败或跳过。没有 Go 时它失败并给出原因；确实要跳过时设 `XIHAN_CONFORMANCE_REQUIRED=0`，CI 不设。运行器新增用例时同步 `CASES`。

## 测试分工

| 测试 | 覆盖 |
| --- | --- |
| `tests/test_plugin_protocol.py` | 回放 `vectors/plugin/` 的向量（握手行、版本协商、回调端点列表、截止时间、回调状态的映射、错误的读入、运行时的消息键），以及协议消息的转换、运行时目录 10 种区域的键集与占位符、配置节的类型化读取、回调的本地核对、日志格式与插件声明的核对 |
| `tests/test_plugin_runtime.py` | 测试扮演宿主与假的 `Host` 服务，走次序、配置、就绪、健康、停止与收尾、调用与事件、回调、错误与日志的每一条（假宿主可配置应答延迟，用来覆盖回调途中到截止时间） |
| `tests/test_plugin_process.py` | 以子进程核对进程入口 `run(build)` 启动前的拒绝、收尾的时限与退出码 |
| `tests/test_plugin_conformance.py` | 黑盒运行器跑示例插件 echo 的全部用例 |

`tests/plugin_support.py` 是共用工具：在本进程里运行插件、连上它，以及记录回调的假宿主。

## 启动脚本

清单的 `entry` 是相对插件目录的可执行文件。宿主只继承 handshake.md §1 列出的少数环境变量，所以脚本写解释器与插件脚本的绝对路径，不依赖 `VIRTUAL_ENV`：

- Unix：带 shebang 的 sh 脚本，`exec` 解释器（stdin、信号与退出码直接落在插件进程上），并加可执行位。
- Windows：`.cmd` 文件，`entry` 写 `echo.cmd`（没有扩展名时宿主补的是 `.exe`）。

## 平台差异

- 插件端点：默认是 Unix 域套接字，Linux、macOS 与 Windows 都用（Windows 10 1803 起支持 AF_UNIX，grpcio 在 Windows 上同样能监听 `unix:`）。套接字放在 `mkdtemp` 新建的私有目录里（`0700`，只新建不复用），套接字文件 `0600`，停止后删除；Windows 上 `chmod` 只管只读位，靠当前用户临时目录的访问控制。临时目录的路径带空白时改为监听回环 TCP；`Plugin(network=Network.TCP)` 只绑定 `127.0.0.1` 的随机端口。
- 命名管道：grpcio 不支持，插件不监听 `npipe`；回调时取 `XIHAN_PLUGIN_HOST_ADDRESS` 里第一个 `unix` 或 `tcp` 项，所以 Windows 上的 .NET 宿主经回环 TCP 回调。
- SIGTERM：Windows 没有，宿主改为关闭 stdin；`test_plugin_process.py` 里发 SIGTERM 的用例在 Windows 上跳过。stdin 读到结尾与收到 SIGTERM 的收尾相同，最多 3 秒。
- CI 只在 Linux 上核对生成的协议代码；测试矩阵是 Linux 与 Windows、Python 3.12 与 3.13。
