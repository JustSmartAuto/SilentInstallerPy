# -*- coding: utf-8 -*-
"""Meinberg NTP Server 一键离线安装器（tkinter 版）

参考 mosquitto_installer 结构：
- 内嵌 Meinberg NTP 4.2.4p8 安装包（NSIS，/S 静默安装，自带 UAC 提权）
- 安装完成后写入内网专用配置（本机时钟为唯一时间源，stratum 10，
  允许任意网段客户端查询授时）
- 安装程序未注册服务时用 sc create 手动创建 NTP 服务并启动
- 安装完成后向 127.0.0.1:123 发起 NTP 查询并轮询等待同步完成（最长 5 分钟，
  ntpd 初始同步 stratum=0 属正常现象）
"""

import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

if sys.platform == "win32":
    import winreg

APP_NAME = "NTP"
CURRENT_VERSION = "1.0.0"
NTP_VERSION = "4.2.4p8"
SERVICE_NAME = "NTP"

NTP_SETUP_EXE = "ntp-4.2.4p8-lennon-o-lpv-win32-setup.exe"
CONF_FILE = "ntp.conf"

TEST_HOST = "127.0.0.1"
TEST_PORT = 123
TEST_QUERY_TIMEOUT = 5.0
TEST_TOTAL_TIMEOUT = 300

SERVICE_NAMES = ["NTP", "ntp"]


def bundle_dir() -> Path:
    """内嵌资源所在目录（PyInstaller 打包后为 _MEIPASS/assets，开发时为项目 assets/）。"""
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "assets")
    candidates += [
        Path(__file__).parent.parent.parent / "assets",
        Path.cwd() / "assets",
        Path(sys.argv[0]).parent / "assets",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def ntp_conf_content(install_dir: Path) -> str:
    """内网专用配置：以本机时钟为唯一时间源（无外网依赖），对外授时。"""
    return (
        "restrict 127.0.0.1\n"
        "restrict -6 ::1\n"
        "restrict 0.0.0.0 mask 0.0.0.0 nomodify notrap\n"
        f'driftfile "{install_dir / "etc" / "ntp.drift"}"\n'
        "server 127.127.1.0\n"
        "fudge 127.127.1.0 stratum 10\n"
    )


def find_install_dir() -> Path | None:
    """按默认安装路径 → PATH → 注册表顺序查找 NTP 安装目录。"""
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "NTP",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NTP",
    ]
    for c in candidates:
        if c.exists():
            return c
    which = shutil.which("ntpd")
    if which:
        return Path(which).parent.parent
    if sys.platform != "win32":
        return None
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for view in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
        try:
            hkey = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0,
                                  winreg.KEY_READ | view)
        except FileNotFoundError:
            continue
        with hkey:
            i = 0
            while i < 2000:
                try:
                    subkey = winreg.EnumKey(hkey, i)
                    i += 1
                except OSError:
                    break
                if "ntp" not in subkey.lower():
                    continue
                try:
                    with winreg.OpenKey(hkey, subkey) as sk:
                        name, _ = winreg.QueryValueEx(sk, "DisplayName")
                    if "ntp" not in str(name).lower():
                        continue
                    try:
                        with winreg.OpenKey(hkey, subkey) as sk:
                            uninstall_str, _ = winreg.QueryValueEx(sk, "UninstallString")
                    except FileNotFoundError:
                        continue
                    location = str(Path(str(uninstall_str).strip().strip('"')).parent)
                    if location and Path(location).exists():
                        return Path(location)
                except (OSError, ValueError):
                    continue
    return None


def find_ntpd_exe(install_dir: Path | None) -> Path | None:
    if install_dir and (install_dir / "bin" / "ntpd.exe").exists():
        return install_dir / "bin" / "ntpd.exe"
    install_dir = find_install_dir()
    if install_dir and (install_dir / "bin" / "ntpd.exe").exists():
        return install_dir / "bin" / "ntpd.exe"
    return None


def _run_cmd(file: str, args: str, timeout: int = 120) -> tuple[int, str, str]:
    """执行控制台命令（sc / net 等），返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run([file] + args.split(),
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:
        return -1, "", str(e)


def service_exists() -> bool:
    """检测 NTP 服务是否已注册（服务名 NTP/ntp，显示名 Network Time Protocol）。"""
    for name in SERVICE_NAMES:
        try:
            code = subprocess.run(["sc", "query", name],
                                  capture_output=True, text=True, timeout=10,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if code.returncode == 0 and "STATE" in (code.stdout or code.stderr or ""):
                return True
        except Exception:
            continue
    try:
        code = subprocess.run(["net", "start"], capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for name in SERVICE_NAMES:
            if name.lower() in (code.stdout or "").lower():
                return True
        if "Network Time Protocol" in (code.stdout or ""):
            return True
    except Exception:
        pass
    return False


def find_service_name() -> str | None:
    for name in SERVICE_NAMES:
        try:
            code = subprocess.run(["sc", "query", name],
                                  capture_output=True, text=True, timeout=10,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if code.returncode == 0 and "STATE" in (code.stdout or code.stderr or ""):
                return name
        except Exception:
            continue
    return None


def service_is_running() -> bool:
    name = find_service_name()
    if not name:
        return False
    try:
        code = subprocess.run(["sc", "query", name],
                              capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = code.stdout or code.stderr or ""
        return "STATE" in out and re.search(r"STATE\s+\S+\s+RUNNING", out) is not None
    except Exception:
        return False


def query_ntp_once(host: str = TEST_HOST, port: int = TEST_PORT) -> tuple[datetime, int]:
    """单次 NTP 查询（UDP），返回 (服务器时间 UTC, stratum)。

    收到 stratum=0 说明服务已应答但尚未完成初始同步，也返回由调用方判断。
    """
    data = bytearray(48)
    data[0] = 0x1B  # LI=0, VN=3, Mode=3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(TEST_QUERY_TIMEOUT)
        sock.sendto(bytes(data), (host, port))
        buf, _ = sock.recvfrom(512)
    if len(buf) < 48:
        raise RuntimeError("NTP 应答包过短")
    mode = buf[0] & 0x07
    stratum = buf[1]
    if mode != 4:
        raise RuntimeError(f"NTP 应答模式异常（mode={mode}，应为 4/server）")
    seconds, fraction = struct.unpack("!II", buf[40:48])
    ntp_epoch = 2208988800
    if seconds < ntp_epoch:
        raise RuntimeError("NTP 返回的时间戳无效")
    utc = datetime(1900, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=seconds - ntp_epoch, milliseconds=fraction * 1000 / (1 << 32))
    return utc, stratum


def ntp_server_test(host: str = TEST_HOST, port: int = TEST_PORT,
                    timeout: float = TEST_TOTAL_TIMEOUT) -> str:
    """向本机 NTP 服务发起查询并轮询等待同步完成，成功返回结果摘要。

    ntpd 刚重启时先与上游源（本机时钟）多次交互确认时钟可信才对外提供有效
    时间，通常需要 1~5 分钟；此期间应答 stratum=0，本函数会自动等待。
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    last_detail = ""
    while time.monotonic() < deadline:
        attempt += 1
        try:
            ntp_time, stratum = query_ntp_once(host, port)
            if stratum == 0:
                last_detail = "服务已应答，尚未完成同步（stratum=0，同步中）"
            else:
                offset = abs((ntp_time - datetime.now(timezone.utc)).total_seconds()) * 1000
                return (f"NTP 服务器时间(UTC)={ntp_time.strftime('%Y-%m-%d %H:%M:%S')}  "
                        f"stratum={stratum}  偏差={offset:.0f}ms")
        except (socket.timeout, OSError) as e:
            last_detail = f"服务无应答（{e.__class__.__name__}）"
        time.sleep(5.0)
    raise RuntimeError(f"NTP 服务器 {timeout / 60:.0f} 分钟内未就绪（{host}:{port}）。"
                       f"最后状态：{last_detail}。可能原因：ntpd 同步异常、"
                       f"UDP 123 端口被防火墙拦截，可执行 ntpq -p 检查")


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("NTP 服务器一键安装器")
        self.root.geometry("640x480")
        self.root.resizable(False, False)
        self.root.configure(bg="white")

        self.is_installing = False
        self.is_busy = False
        self.ntp_installed = False

        try:
            icon = bundle_dir() / "app_icon.ico"
            if icon.exists():
                self.root.iconbitmap(str(icon))
        except Exception:
            pass

        self._build_ui()
        self.refresh_status()

    # ---------- UI ----------
    def _build_ui(self):
        header = tk.Frame(self.root, bg="#1F4E79", height=90)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="NTP 服务器一键安装器", font=("Microsoft YaHei UI", 15, "bold"),
                 fg="white", bg="#1F4E79").place(x=20, y=16)
        tk.Label(header, text=f"安装器 v{CURRENT_VERSION}  |  内嵌 Meinberg NTP {NTP_VERSION} (NSIS)",
                 font=("Microsoft YaHei UI", 9), fg="lightgray", bg="#1F4E79").place(x=20, y=50)

        comp = tk.LabelFrame(self.root, text=" 组件检测（√ = 已安装，可选中后重装）",
                             font=("Microsoft YaHei UI", 9), bg="white", fg="#333")
        comp.place(x=20, y=105, width=600, height=110)

        self.ntp_var = tk.BooleanVar(value=False)
        self.ntp_cb = tk.Checkbutton(comp, text="Meinberg NTP Server", variable=self.ntp_var,
                                     font=("Microsoft YaHei UI", 10, "bold"), bg="white",
                                     command=self._on_toggle)
        self.ntp_cb.place(x=20, y=12)
        self.ntp_status = tk.Label(comp, text="检测中...", font=("Microsoft YaHei UI", 8),
                                   fg="gray", bg="white")
        self.ntp_status.place(x=40, y=42)
        self.svc_status = tk.Label(comp, text="服务：检测中...", font=("Microsoft YaHei UI", 8),
                                   fg="gray", bg="white")
        self.svc_status.place(x=40, y=68)

        tk.Label(self.root,
                 text="安装完成后会自动写入内网专用 ntp.conf（本机时钟 stratum 10 / 允许任意网段查询），"
                      "服务未注册时自动 sc create 创建，随后轮询测试 UDP 123 授时",
                 font=("Microsoft YaHei UI", 8), fg="darkorange", bg="white").place(x=20, y=222)

        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(self.root, variable=self.progress_var, maximum=100,
                        length=600, mode="determinate").place(x=20, y=250)

        self.status_label = tk.Label(self.root, text="就绪", font=("Microsoft YaHei UI", 9),
                                     bg="white", fg="black")
        self.status_label.place(x=20, y=275)

        self.log_text = tk.Text(self.root, width=88, height=8, font=("Consolas", 9),
                                bg="black", fg="white", state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.place(x=20, y=305)
        sb = ttk.Scrollbar(self.root, command=self.log_text.yview)
        sb.place(x=608, y=305, height=115)
        self.log_text.config(yscrollcommand=sb.set)

        btn_frame = tk.Frame(self.root, bg="#F0F0F0", height=55)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        btn_frame.pack_propagate(False)
        self.test_btn = tk.Button(btn_frame, text="NTP 测试", width=12, bg="#107C10",
                                  fg="white", font=("Microsoft YaHei UI", 9, "bold"),
                                  relief=tk.FLAT, command=self.on_ntp_test,
                                  state=tk.DISABLED)
        self.test_btn.place(x=195, y=12)
        self.install_btn = tk.Button(btn_frame, text="安装选中项", width=14, bg="#0078D7",
                                     fg="white", font=("Microsoft YaHei UI", 9, "bold"),
                                     relief=tk.FLAT, command=self.on_install)
        self.install_btn.place(x=330, y=12)
        tk.Button(btn_frame, text="重新检测", width=10, bg="#646464", fg="white",
                  font=("Microsoft YaHei UI", 9), relief=tk.FLAT,
                  command=self.refresh_status).place(x=460, y=12)
        tk.Button(btn_frame, text="退出", width=8, bg="#A0A0A0", fg="white",
                  font=("Microsoft YaHei UI", 9), relief=tk.FLAT,
                  command=self.root.destroy).place(x=545, y=12)

    def _on_toggle(self):
        """已安装组件取消勾选 = 跳过；勾选 = 重装。界面文字即时反馈。"""
        if self.ntp_installed:
            self.ntp_status.config(
                text=(f"{self.ntp_version_text}  →  将重装" if self.ntp_var.get()
                      else f"{self.ntp_version_text}  →  将跳过"),
                fg="#0078D7" if self.ntp_var.get() else "gray")

    # ---------- 检测 ----------
    def refresh_status(self):
        ntp_installed = False

        ntpd = find_ntpd_exe(find_install_dir())
        if ntpd:
            ntp_installed = True
            self.ntp_version_text = f"已安装：(NTP {NTP_VERSION}) ({ntpd.parent.parent})"
            self.ntp_status.config(text=self.ntp_version_text, fg="green")
            self.ntp_var.set(False)
        else:
            self.ntp_version_text = ""
            self.ntp_status.config(text="未安装", fg="red")
            self.ntp_var.set(True)

        try:
            if service_exists():
                running = service_is_running()
                self.svc_status.config(
                    text=f"服务：{SERVICE_NAME} 已安装（{'运行中' if running else '已停止'}）",
                    fg="green" if running else "darkorange")
            else:
                self.svc_status.config(text="服务：未安装", fg="gray")
        except Exception:
            self.svc_status.config(text="服务：检测失败", fg="gray")

        self.ntp_installed = ntp_installed
        self.root.after(0, lambda: self.test_btn.config(
            state=tk.NORMAL if ntp_installed else tk.DISABLED))

    # ---------- 安装 ----------
    def on_ntp_test(self):
        if self.is_busy:
            return
        self.is_busy = True
        self.root.after(0, lambda: self.test_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.install_btn.config(state=tk.DISABLED))

        def worker():
            try:
                result = self.run_ntp_test()
                self.root.after(0, lambda: messagebox.showinfo(
                    "NTP 测试", f"UDP {TEST_PORT} 时间查询测试通过！\n\n{result}"))
            except Exception as e:
                self.log(f"✗ NTP 时间查询测试失败：{e}")
                self.root.after(0, lambda: messagebox.showerror(
                    "NTP 测试", f"时间查询测试失败：\n\n{e}"))
            finally:
                self.is_busy = False
                self.root.after(0, self.refresh_status)

        threading.Thread(target=worker, daemon=True).start()

    def on_install(self):
        if self.is_installing or self.is_busy:
            return
        if not self.ntp_var.get():
            messagebox.showinfo("提示", "未选中任何组件。\n已安装的组件默认跳过，勾选表示重装。")
            return
        extra = "\n\n注意：安装过程中请批准 UAC 提权提示（NSIS 安装包需要管理员权限）。"
        if not messagebox.askyesno("确认安装", "将安装：\n\n  • Meinberg NTP Server "
                                   f"{NTP_VERSION}（静默安装）{extra}\n\n是否继续？"):
            return
        threading.Thread(target=self.install_worker, daemon=True).start()

    def install_worker(self):
        self.is_installing = True
        self.is_busy = True
        self.root.after(0, lambda: self.install_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.test_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.progress_var.set(0))
        try:
            self.install_ntp()
            self.root.after(0, lambda: self.progress_var.set(70))
            try:
                result = self.run_ntp_test()
            except Exception as te:
                self.log(f"✗ NTP 时间查询测试失败：{te}")
                self.update_status("安装完成，但 NTP 测试未通过")
                self.root.after(0, lambda: messagebox.showwarning(
                    "NTP 测试", f"安装完成，但 NTP 测试未通过：\n\n{te}"))
                return
            self.root.after(0, lambda: self.progress_var.set(100))
            self.update_status("全部完成！")
            self.root.after(0, lambda: messagebox.showinfo(
                "安装完成", "Meinberg NTP Server 安装完成！\n\n"
                f"NTP 测试通过：\n{result}\n\n详情请查看日志。"))
        except Exception as e:
            self.update_status(f"安装失败：{e}")
            self.log(f"✗ 安装失败：{e}")
            self.root.after(0, lambda: messagebox.showerror("安装错误", f"安装失败：\n\n{e}"))
        finally:
            self.is_installing = False
            self.is_busy = False
            self.root.after(0, lambda: self.install_btn.config(state=tk.NORMAL))
            self.root.after(0, self.refresh_status)

    def run_ntp_test(self) -> str:
        """轮询测试本机 NTP 授时（ntpd 初始同步需要数分钟，最长 5 分钟）。"""
        self.log(f"开始 NTP 测试：{TEST_HOST}:{TEST_PORT}（最多等待 "
                 f"{TEST_TOTAL_TIMEOUT // 60} 分钟，ntpd 初始同步期间 stratum=0 属正常）...")
        result = ntp_server_test()
        self.log(f"✓ NTP 测试通过：{result}")
        return result

    def install_ntp(self):
        self.update_status("安装 Meinberg NTP Server ...")
        assets = bundle_dir()
        installer = assets / NTP_SETUP_EXE
        if not installer.exists():
            raise RuntimeError(f"内嵌 NTP 安装包缺失（assets/{NTP_SETUP_EXE}）")

        self.log(f"运行内嵌安装包：{installer.name}（NSIS 静默安装，请批准 UAC 提示）")
        arg_sets = [
            ["/S"],
            ["/S", "/SD"],
            ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/SP-"],
        ]
        installed = False
        for args in arg_sets:
            if service_exists() and find_ntpd_exe(find_install_dir()):
                installed = True
                break
            code, out, err = self._run_installer(installer, args)
            self.log(f"  静默参数 [{' '.join(args)}] 退出码：{code}"
                     f"{('  ' + out) if out else ''}{('  ' + err) if err else ''}")
            if code == 0:
                installed = True
                break

        if find_install_dir() is None:
            raise RuntimeError("NTP 安装后未找到安装目录")

        if not service_exists():
            self.log("安装程序未注册服务，尝试手动创建 NTP 服务...")
            self.create_ntp_service()
            if not service_exists():
                raise RuntimeError("无法创建 NTP 服务")

        ntpd = find_ntpd_exe(find_install_dir())
        self.log(f"✓ NTP 安装成功：{NTP_VERSION or '未知版本'}")
        if ntpd:
            self.log(f"  ntpd：{ntpd}")

        # 部署配置文件
        self.deploy_conf()

        # 服务重启使新配置生效
        self.handle_service()

    def _run_installer(self, installer: Path, args: list[str]) -> tuple[int, str, str]:
        try:
            r = subprocess.run([str(installer)] + args, capture_output=True, text=True,
                               timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return -1, "", "安装超时（10 分钟）"
        except Exception as e:
            return -1, "", str(e)

    def deploy_conf(self):
        install_dir = find_install_dir()
        if not install_dir:
            raise RuntimeError("未找到 NTP 安装目录")
        etc_dir = install_dir / "etc"
        try:
            etc_dir.mkdir(parents=True, exist_ok=True)
            target = etc_dir / CONF_FILE
            content = ntp_conf_content(install_dir)
            if target.exists() and target.read_text(encoding="utf-8", errors="ignore").strip() == content.strip():
                self.log(f"  配置已一致，跳过：{target}")
                return
            if target.exists():
                backup = etc_dir / "ntp.conf.bak"
                backup.unlink(missing_ok=True)
                shutil.copy2(target, backup)
                self.log(f"  原配置已备份：{backup.name}")
            target.write_text(content, encoding="utf-8")
            self.log(f"✓ 已部署配置：{target}")
            self.log("  （restrict 0.0.0.0 nomodify notrap：允许任意网段客户端查询授时；"
                     "server 127.127.1.0：以本机时钟为唯一时间源，stratum 10）")
        except PermissionError:
            raise RuntimeError(f"无权限写入 {etc_dir}，请以管理员身份运行本安装器")
        except Exception as e:
            raise RuntimeError(f"写入配置失败：{e}")

    def create_ntp_service(self):
        install_dir = find_install_dir()
        ntpd = find_ntpd_exe(install_dir)
        if not ntpd:
            raise RuntimeError("未找到 ntpd.exe，无法创建服务")
        conf = install_dir / "etc" / CONF_FILE
        conf.parent.mkdir(parents=True, exist_ok=True)
        if not conf.exists():
            conf.write_text(ntp_conf_content(install_dir), encoding="utf-8")
        _run_cmd("sc", "stop ntp")
        _run_cmd("sc", "delete ntp")
        time.sleep(0.5)
        bin_path = f'"{ntpd}" -c "{conf}" -g'
        code, out, err = _run_cmd("sc", f'create ntp binPath= "{bin_path}" start= auto')
        self.log(f"  sc create ntp => {code} {out.strip() or err.strip()}")
        _run_cmd("sc", 'description ntp "Network Time Protocol"')

    def handle_service(self):
        name = find_service_name() or "NTP"
        try:
            if service_exists() and service_is_running():
                self.log(f"\n检测到服务 {name} 正在运行，正在重启以加载新配置...")
                _run_cmd("net", f'stop {name}')
                time.sleep(1)
            code, out, err = _run_cmd("net", f'start {name}')
            msg = (out.strip() or err.strip() or "无输出").splitlines()[-1]
            self.log(f"  net start {name} => {msg}")
            time.sleep(1)
            if service_is_running():
                self.log("✓ 服务已启动，正在使用新配置（UDP 123 对外授时）")
            else:
                self.log("⚠ 服务未确认运行状态，请检查服务管理器（services.msc）")
        except Exception as e:
            self.log(f"⚠ 服务处理异常：{e}")

    # ---------- 工具 ----------
    def update_status(self, msg: str):
        self.root.after(0, lambda: self.status_label.config(text=msg))
        self.log(msg)

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: self._append(f"[{ts}] {msg}\n"))

    def _append(self, text: str):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)


def main():
    root = tk.Tk()
    InstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
