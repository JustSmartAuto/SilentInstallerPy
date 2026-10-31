# NtpServerSilent

NTP 服务器一键设置工具（静默安装 / 配置 / 测试），用于在内网环境中快速把一台 Windows PC 搭建为局域网 NTP 授时服务器，供 In-Sight 相机（D900、3D-L4000、IS2800、IS3800 等）进行时间同步。

## 功能特性

- **一键全自动**：双击启动即完成「检测 → 静默安装 → 写配置 → 重启服务 → 时间测试」全流程，无弹框、无需人工干预。
- **静默安装 Meinberg NTP**：安装程序（`ntp-4.2.4p8`）作为内嵌资源打包在单个 exe 中，运行时释放并自动执行（依次尝试 NSIS 的 `/S`、`/SD`、`/VERYSILENT` 等静默参数）。
- **内网专用配置**：以本机时钟为唯一时间源（`server 127.127.1.0`，stratum 10），不依赖外网，允许任意网段（0.0.0.0/0）客户端查询授时。
- **配置幂等**：启动时若检测到配置与目标一致则跳过安装直接测试；不一致则自动重新配置。
- **服务自愈**：安装程序未注册服务时，自动用 `sc create` 手动创建 NTP 服务。
- **内置测试**：自动向 `127.0.0.1:123` 发起 NTP 查询并轮询等待同步完成（最长 5 分钟），显示服务器时间与系统时间的偏差。
- **日志**：界面日志 + `%TEMP%\NtpSetupHelper.log` 文件日志。

## 使用方法

1. 以**管理员权限**运行 `src/NtpSetupHelper/bin/Release/NtpSetupHelper.exe`（或重新构建后运行）。
2. 程序启动后自动完成全部设置并在日志窗口显示测试结果。
3. 防火墙放行 UDP 123 端口，即可让局域网内的相机使用该 PC 的 IP 作为 NTP 服务器。
4. 相机端配置步骤详见 [docs/IS2800-Time-Synchronization.md](docs/IS2800-Time-Synchronization.md)（Vision Suite 时间设置 SOP，含 PDF 版）。

## 项目结构

```
├── src/NtpSetupHelper/      C# WinForms 工具（.NET Framework 4.8）
│   ├── Program.cs           全部逻辑（单文件）
│   ├── NtpSetupHelper.csproj
│   └── app.manifest         请求管理员权限（requiredAdministrator）
├── installer/               Meinberg NTP 4.2.4p8 安装包（内嵌进 exe 的 EmbeddedResource）
├── docs/                    相机时间同步 SOP（Markdown + PDF + 截图）
├── assets/                  图标与示意图
└── tools/                   ResourceHacker、FileZilla Server 等辅助工具
```

## 构建

使用 Visual Studio 或 MSBuild 构建：

```bash
msbuild src/NtpSetupHelper/NtpSetupHelper.csproj /p:Configuration=Release
```

生成的单文件 `bin\Release\NtpSetupHelper.exe` 已包含 NTP 安装包资源，可直接拷贝分发。

## NTP 工作原理简述

NTP（Network Time Protocol，网络时间协议）基于 UDP 123 端口工作，核心过程：

1. **客户端**向服务器发送一个带客户端发送时间戳 `t1` 的查询包。
2. **服务器**记录收到时刻 `t2` 和应答发出时刻 `t3`，连同自身时钟的 `t3` 一起返回。
3. **客户端**记录收到应答的时刻 `t4`，用四个时间戳计算：
   - 网络往返延迟：`delay = (t4 - t1) - (t3 - t2)`
   - 本机时间偏差：`offset = ((t2 - t1) + (t3 - t4)) / 2`
4. 客户端据此校准本地时钟；服务器与上游源之间同理一级级溯源，形成 stratum（层数）层级——本工具把本机时钟作为 stratum 10 的本地时钟源（`127.127.1.0`），相机作为 stratum 11 的客户端。

几个与使用相关的概念：

- **stratum**：时间源的层级数。0 表示未同步/原子钟，1 为直连原子钟的服务器，数字越大离源头越远。工具测试时若收到 stratum=0 的应答，说明 ntpd 刚重启、尚未完成同步，属于临时状态。
- **初始同步延迟**：ntpd 重启后要先与上游源（这里是本机时钟）多次交互、确认时钟可信（reach 寄存器累积）才会对外提供有效时间，通常需要 1~5 分钟，因此工具测试阶段会自动轮询等待。
- **restrict 指令**：控制哪些客户端可以访问。本工具写入的 `restrict 0.0.0.0 mask 0.0.0.0 nomodify notrap` 表示允许任意网段查询时间，但不允许修改服务器配置（nomodify）或发起 trap 监控（notrap）。

## 注意事项

- 必须以管理员身份运行（需要安装软件、创建/重启服务）。
- 授时源为本机系统时钟，适合无互联网的内网产线环境；若需高精度授时请先同步本机时间。
- 测试用例中相机所连网卡需与 PC 同网段，并确保 Windows 防火墙允许 UDP 123。

## License

[MIT](LICENSE) © 2026 JustSmartAuto
