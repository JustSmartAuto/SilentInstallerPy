# SilentInstallerPy

Python (tkinter + PyInstaller) 制作的 Windows **静默离线一键安装器**集合。每个安装器内嵌官方安装包或二进制文件，双击即可离线完成检测、安装/重装、PATH 配置与验证，无需命令行操作。

## 目录结构

| 目录 | 安装器 | 说明 |
|---|---|---|
| [`uv_installer/`](uv_installer) | UV Offline Installer | 内嵌 uv 官方 zip，解压安装 `uv/uvx/uvw`，支持用户级/系统级安装，自动写 PATH |
| [`ffmpeg_installer/`](ffmpeg_installer) | ffmpeg Installer | 内嵌 ffmpeg，解压安装并配置 PATH |
| [`git_rsync_installer/`](git_rsync_installer) | Git + rsync 一键安装器 | 内嵌 Git for Windows 官方安装包（静默安装）+ MSYS2 rsync（复制到 `~/bin` 并自动补全 msys 运行时 DLL），已装组件可单独重装/跳过 |
| [`mosquitto_installer/`](mosquitto_installer) | Mosquitto MQTT 一键安装器 | 内嵌 Mosquitto 2.1.2 官方 NSIS 安装包（`/S` 静默安装）+ 自定义 `mosquitto.conf`（listener 8907 / allow_anonymous true），部署后自动备份原配置、重启 Windows 服务，并使用内置 paho-mqtt 进行 `/hello` 主题消息收发回环测试（可随时点「MQTT 测试」按钮重跑） |
| [`ntp_installer/`](ntp_installer) | NTP 安装器 | NTP 对时工具（C# 辅助程序源码 + NTP 原理文档），用于相机/工控机时间同步 |

## 通用特性

- **离线**：所有安装介质内嵌在单个 exe 中（构建时经 PyInstaller `--add-data` 打包）
- **组件检测**：启动自动检测已安装组件，已装默认跳过、勾选即重装
- **PATH 自动配置**：写入用户/系统 PATH（注册表）并广播环境变更
- **安装验证**：完成后自动运行 `--version` 验证（Mosquitto 额外做 MQTT `/hello` 收发回环测试）

## 构建

每个安装器目录独立构建（以 git_rsync_installer 为例）：

```bash
pip install pyinstaller
python build.py
# 产物: dist/GitRsyncInstaller_<时间戳>.exe
```

构建前需将安装介质放入对应 `assets/` 目录（如 `Git-*-64-bit.exe`、rsync 及 DLL 等），详见各子目录说明。

## License

[MIT](LICENSE) © JustSmartAuto
