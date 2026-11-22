import os
import shutil
import subprocess
import sys
import threading
import winreg
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "WSL2 Alpine Linux 一键离线安装器"
CURRENT_VERSION = "1.0.0"
DISTRO_NAME = "Alpine"
FEATURES = ("Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RebootRequired(RuntimeError):
    pass


def bundle_dir() -> Path:
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "assets")
    candidates.extend(
        (
            Path(__file__).parent.parent.parent / "assets",
            Path.cwd() / "assets",
            Path(sys.argv[0]).parent / "assets",
        )
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def install_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "AlpineWSL" / DISTRO_NAME


def decode_output(data: bytes | None) -> str:
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:200]:
        return data.decode("utf-16", errors="replace").strip()
    for encoding in ("utf-8", "mbcs"):
        try:
            return data.decode(encoding).strip()
        except (UnicodeDecodeError, LookupError):
            pass
    return data.decode("utf-8", errors="replace").strip()


def run_command(args: list[str], timeout: int = 120, cwd: Path | None = None):
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
        creationflags=NO_WINDOW,
    )


def command_output(result) -> str:
    return "\n".join(
        part for part in (decode_output(result.stdout), decode_output(result.stderr)) if part
    )


def distro_registered() -> bool:
    try:
        result = run_command(["wsl.exe", "--list", "--quiet"], timeout=20)
        if result.returncode == 0:
            names = [line.strip() for line in command_output(result).splitlines()]
            if DISTRO_NAME.casefold() in (name.casefold() for name in names):
                return True
    except (OSError, subprocess.TimeoutExpired):
        pass

    base = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, index)
                    index += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        name, _ = winreg.QueryValueEx(subkey, "DistributionName")
                    if str(name).casefold() == DISTRO_NAME.casefold():
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def verify_alpine(timeout: int = 60) -> str:
    attempts = [["wsl.exe", "-d", DISTRO_NAME, "uname", "-a"]]
    launcher = install_dir() / "Alpine.exe"
    if launcher.is_file():
        attempts.append([str(launcher), "run", "uname", "-a"])

    errors = []
    for command in attempts:
        try:
            result = run_command(
                command,
                timeout=timeout,
                cwd=launcher.parent if launcher.parent.is_dir() else None,
            )
            output = command_output(result)
            if result.returncode == 0 and "Linux" in output:
                return output
            errors.append(output or f"返回码 {result.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
    raise RuntimeError("uname -a 验证失败：" + "；".join(errors))


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("660x500")
        self.root.resizable(False, False)
        self.root.configure(bg="white")
        self.is_busy = False
        self.alpine_installed = False

        try:
            icon = bundle_dir() / "app_icon.ico"
            if icon.is_file():
                self.root.iconbitmap(str(icon))
        except tk.TclError:
            pass

        self._build_ui()
        self.refresh_status()

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#176B87", height=90)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header,
            text=APP_NAME,
            font=("Microsoft YaHei UI", 15, "bold"),
            fg="white",
            bg="#176B87",
        ).place(x=20, y=16)
        tk.Label(
            header,
            text=f"安装器 v{CURRENT_VERSION}  |  离线启用 WSL2 并安装 Alpine Linux",
            font=("Microsoft YaHei UI", 9),
            fg="#D9F1F7",
            bg="#176B87",
        ).place(x=20, y=52)

        component = tk.LabelFrame(
            self.root,
            text=" 组件状态（已安装组件默认跳过，勾选表示重装）",
            font=("Microsoft YaHei UI", 9),
            bg="white",
            fg="#333333",
        )
        component.place(x=20, y=105, width=620, height=115)
        self.alpine_var = tk.BooleanVar(value=True)
        self.alpine_check = tk.Checkbutton(
            component,
            text="Alpine Linux（WSL2）",
            variable=self.alpine_var,
            font=("Microsoft YaHei UI", 10, "bold"),
            bg="white",
            command=self._on_toggle,
        )
        self.alpine_check.place(x=20, y=12)
        self.alpine_status = tk.Label(
            component,
            text="检测中……",
            font=("Microsoft YaHei UI", 9),
            fg="gray",
            bg="white",
        )
        self.alpine_status.place(x=40, y=44)
        tk.Label(
            component,
            text=f"发行版名称：{DISTRO_NAME}    安装位置：{install_dir()}",
            font=("Microsoft YaHei UI", 8),
            fg="gray",
            bg="white",
        ).place(x=40, y=73)

        tk.Label(
            self.root,
            text="自动启用 WSL 与虚拟机平台、安装内核更新，并在完成后执行 uname -a 验证。",
            font=("Microsoft YaHei UI", 9),
            fg="#A65D00",
            bg="white",
        ).place(x=20, y=230)

        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(
            self.root, variable=self.progress_var, maximum=100, length=620
        ).place(x=20, y=260)
        self.status_label = tk.Label(
            self.root,
            text="就绪",
            font=("Microsoft YaHei UI", 9),
            bg="white",
            fg="black",
        )
        self.status_label.place(x=20, y=285)

        self.log_text = tk.Text(
            self.root,
            width=90,
            height=8,
            font=("Consolas", 9),
            bg="#101010",
            fg="white",
            state=tk.DISABLED,
            wrap=tk.WORD,
        )
        self.log_text.place(x=20, y=315)
        scrollbar = ttk.Scrollbar(self.root, command=self.log_text.yview)
        scrollbar.place(x=628, y=315, height=128)
        self.log_text.config(yscrollcommand=scrollbar.set)

        buttons = tk.Frame(self.root, bg="#F0F0F0", height=55)
        buttons.pack(side=tk.BOTTOM, fill=tk.X)
        buttons.pack_propagate(False)
        self.test_btn = tk.Button(
            buttons,
            text="验证 uname -a",
            width=15,
            bg="#107C10",
            fg="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            relief=tk.FLAT,
            command=self.on_test,
            state=tk.DISABLED,
        )
        self.test_btn.place(x=160, y=12)
        self.install_btn = tk.Button(
            buttons,
            text="开始安装",
            width=14,
            bg="#0078D7",
            fg="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            relief=tk.FLAT,
            command=self.on_install,
        )
        self.install_btn.place(x=305, y=12)
        tk.Button(
            buttons,
            text="重新检测",
            width=10,
            bg="#646464",
            fg="white",
            font=("Microsoft YaHei UI", 9),
            relief=tk.FLAT,
            command=self.refresh_status,
        ).place(x=435, y=12)
        tk.Button(
            buttons,
            text="退出",
            width=8,
            bg="#A0A0A0",
            fg="white",
            font=("Microsoft YaHei UI", 9),
            relief=tk.FLAT,
            command=self.root.destroy,
        ).place(x=550, y=12)

    def _on_toggle(self):
        if self.alpine_installed:
            self.alpine_status.config(
                text="已安装 → 将重装（原 Alpine 数据会被删除）"
                if self.alpine_var.get()
                else "已安装 → 将跳过并重跑验证",
                fg="#C04000" if self.alpine_var.get() else "gray",
            )

    def refresh_status(self):
        if self.is_busy:
            return
        self.alpine_installed = distro_registered()
        self.alpine_var.set(not self.alpine_installed)
        self.alpine_status.config(
            text="已安装 → 将跳过并重跑验证" if self.alpine_installed else "未安装",
            fg="green" if self.alpine_installed else "red",
        )
        self.test_btn.config(state=tk.NORMAL if self.alpine_installed else tk.DISABLED)

    def set_busy(self, busy: bool):
        self.is_busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.install_btn.config(state=state)
        self.test_btn.config(state=state if self.alpine_installed else tk.DISABLED)

    def on_test(self):
        if self.is_busy:
            return
        self.set_busy(True)

        def worker():
            try:
                output = verify_alpine()
                self.log(f"验证成功：{output}")
                self.root.after(
                    0,
                    lambda: messagebox.showinfo("验证成功", f"uname -a 输出：\n\n{output}"),
                )
            except Exception as exc:
                message = str(exc)
                self.log(f"验证失败：{message}")
                self.root.after(
                    0, lambda: messagebox.showerror("验证失败", message)
                )
            finally:
                self.root.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def on_install(self):
        if self.is_busy:
            return
        if self.alpine_installed and not self.alpine_var.get():
            self.on_test()
            return
        warning = (
            "\n\n注意：重装会注销现有 Alpine，原发行版中的数据将被永久删除。"
            if self.alpine_installed
            else ""
        )
        if not messagebox.askyesno(
            "确认安装",
            "将离线启用 WSL2、安装内核更新和 Alpine Linux，最后执行 uname -a 验证。"
            f"{warning}\n\n是否继续？",
        ):
            return
        self.set_busy(True)
        self.progress_var.set(0)
        threading.Thread(target=self.install_worker, daemon=True).start()

    def install_worker(self):
        try:
            self.enable_wsl_features()
            self.root.after(0, lambda: self.progress_var.set(25))
            self.install_kernel()
            self.root.after(0, lambda: self.progress_var.set(45))
            self.set_default_wsl2()
            if self.alpine_var.get() or not self.alpine_installed:
                self.install_alpine()
            self.root.after(0, lambda: self.progress_var.set(85))
            self.update_status("正在执行 uname -a 验证……")
            output = verify_alpine()
            self.log(f"✓ 验证成功：{output}")
            self.root.after(0, lambda: self.progress_var.set(100))
            self.update_status("安装和验证全部完成")
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "安装完成", f"Alpine Linux 已通过 WSL 安装并验证。\n\nuname -a：\n{output}"
                ),
            )
        except RebootRequired as exc:
            message = str(exc)
            self.update_status("需要重启后继续")
            self.log(message)
            self.root.after(
                0,
                lambda: messagebox.showwarning(
                    "需要重启", f"{message}\n\n重启后请重新运行本安装器，以继续安装并验证 Alpine。"
                ),
            )
        except Exception as exc:
            message = str(exc)
            self.update_status(f"安装失败：{message}")
            self.log(f"✗ 安装失败：{message}")
            self.root.after(0, lambda: messagebox.showerror("安装错误", message))
        finally:
            self.root.after(0, lambda: self.set_busy(False))
            self.root.after(0, self.refresh_status)

    def enable_wsl_features(self):
        self.update_status("正在启用 WSL 和虚拟机平台……")
        for feature in FEATURES:
            self.log(f"启用 Windows 功能：{feature}")
            result = run_command(
                [
                    "dism.exe",
                    "/online",
                    "/enable-feature",
                    f"/featurename:{feature}",
                    "/all",
                    "/norestart",
                    "/LimitAccess",
                ],
                timeout=600,
            )
            if result.returncode == 3010:
                raise RebootRequired("Windows 功能已启用，必须重启后才能继续。")
            if result.returncode != 0:
                raise RuntimeError(
                    f"启用 {feature} 失败（返回码 {result.returncode}）：{command_output(result)}\n"
                    "已使用 /LimitAccess 禁止 Windows Update。若系统提示找不到源文件，"
                    "说明本机组件存储缺少该功能负载，需要提供与 Windows 版本和语言完全匹配的离线源。"
                )
        self.log("✓ WSL 和虚拟机平台已启用")

    def install_kernel(self):
        self.update_status("正在静默安装 WSL2 Linux 内核更新……")
        package = bundle_dir() / "wsl_update_x64.msi"
        result = run_command(
            ["msiexec.exe", "/i", str(package), "/qn", "/norestart"], timeout=900
        )
        if result.returncode == 3010:
            raise RebootRequired("WSL2 内核更新已安装，必须重启后才能继续。")
        if result.returncode not in (0, 1638):
            raise RuntimeError(
                f"WSL2 内核安装失败（返回码 {result.returncode}）：{command_output(result)}"
            )
        self.log("✓ WSL2 Linux 内核更新已安装（或已存在更新版本）")

    def set_default_wsl2(self):
        result = run_command(["wsl.exe", "--set-default-version", "2"], timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"设置 WSL2 默认版本失败：{command_output(result)}")
        self.log("✓ 默认 WSL 版本已设置为 2")

    def remove_alpine(self):
        self.log("正在注销现有 Alpine……")
        result = run_command(["wsl.exe", "--unregister", DISTRO_NAME], timeout=120)
        if result.returncode != 0:
            launcher = install_dir() / "Alpine.exe"
            if launcher.is_file():
                result = run_command([str(launcher), "clean"], timeout=120, cwd=launcher.parent)
        if result.returncode != 0 or distro_registered():
            raise RuntimeError(f"注销现有 Alpine 失败：{command_output(result)}")
        self.log("✓ 现有 Alpine 已注销")

    def install_alpine(self):
        if distro_registered():
            self.remove_alpine()
        assets = bundle_dir()
        target = install_dir()
        target.mkdir(parents=True, exist_ok=True)
        rootfs = assets / "rootfs.tar.gz"

        help_result = run_command(["wsl.exe", "--help"], timeout=30)
        supports_import = "--import" in command_output(help_result)
        if supports_import:
            self.update_status("正在通过 wsl --import 安装 Alpine……")
            result = run_command(
                [
                    "wsl.exe",
                    "--import",
                    DISTRO_NAME,
                    str(target),
                    str(rootfs),
                    "--version",
                    "2",
                ],
                timeout=900,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Alpine 导入失败：{command_output(result)}")
        else:
            self.update_status("正在通过 Alpine 启动器注册发行版……")
            launcher = target / "Alpine.exe"
            shutil.copy2(assets / "Alpine.exe", launcher)
            shutil.copy2(rootfs, target / "rootfs.tar.gz")
            result = run_command([str(launcher)], timeout=900, cwd=target)
            if result.returncode != 0:
                raise RuntimeError(f"Alpine 注册失败：{command_output(result)}")
            self.log("正在将 Alpine 转换为 WSL2……")
            result = run_command(
                ["wsl.exe", "--set-version", DISTRO_NAME, "2"], timeout=900
            )
            if result.returncode != 0:
                raise RuntimeError(f"转换 Alpine 为 WSL2 失败：{command_output(result)}")

        if not distro_registered():
            raise RuntimeError("安装命令已结束，但未检测到 Alpine 发行版")
        self.log(f"✓ Alpine 已安装：{target}")

    def update_status(self, message: str):
        self.root.after(0, lambda: self.status_label.config(text=message))
        self.log(message)

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: self._append(f"[{timestamp}] {message}\n"))

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
