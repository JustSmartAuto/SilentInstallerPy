import json
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
CURRENT_VERSION = "1.1.0"
DISTRO_NAME = "Alpine"
FEATURES = ("Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform")
RESUME_TASK_NAME = "WslAlpineInstallerResume"
STATE_VERSION = 1
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


def state_dir() -> Path:
    return install_dir().parent


def state_path() -> Path:
    return state_dir() / "installer-state.json"


def load_resume_state() -> dict | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if data.get("version") == STATE_VERSION and data.get("pending"):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def save_resume_state(reinstall_alpine: bool):
    target = state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": STATE_VERSION,
        "pending": True,
        "reinstall_alpine": bool(reinstall_alpine),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def clear_resume_state():
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass


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


def remove_resume_task():
    run_command(
        ["schtasks.exe", "/Delete", "/TN", RESUME_TASK_NAME, "/F"], timeout=30
    )


def schedule_resume_task():
    identity_result = run_command(["whoami.exe"], timeout=15)
    identity = command_output(identity_result).strip()
    if identity_result.returncode != 0 or "\\" not in identity:
        raise RuntimeError(f"无法确定当前 Windows 用户：{identity}")

    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        arguments = "--resume"
    else:
        executable = Path(sys.executable).resolve()
        arguments = f'"{Path(__file__).resolve()}" --resume'
    task_command = f'"{executable}" {arguments}'

    result = run_command(
        [
            "schtasks.exe",
            "/Create",
            "/TN",
            RESUME_TASK_NAME,
            "/TR",
            task_command,
            "/SC",
            "ONLOGON",
            "/RU",
            identity,
            "/RL",
            "HIGHEST",
            "/IT",
            "/F",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"创建重启续跑任务失败：{command_output(result)}")


def distro_info() -> dict | None:
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
                        if str(name).casefold() != DISTRO_NAME.casefold():
                            continue
                        info = {"DistributionName": str(name)}
                        for value_name in ("Version", "State", "BasePath", "DefaultUid"):
                            try:
                                info[value_name], _ = winreg.QueryValueEx(subkey, value_name)
                            except OSError:
                                pass
                        return info
                except OSError:
                    continue
    except OSError:
        pass
    return None


def distro_registered() -> bool:
    return distro_info() is not None


def usage_text() -> str:
    return (
        "进入 Alpine（在 PowerShell 或 CMD 中执行）：\n"
        "  wsl -d Alpine\n\n"
        "退出 Alpine：\n"
        "  exit"
    )


def verification_text(info: dict) -> str:
    return (
        f"Alpine 版本：{info.get('release', '未知')}\n"
        "WSL 版本：2\n"
        f"默认身份：{info.get('identity', '未知')}\n"
        f"安装位置：{info.get('base_path', install_dir())}\n\n"
        f"Linux 内核：{info.get('kernel', '未知')}\n\n"
        f"{usage_text()}"
    )


def verify_alpine(timeout: int = 90) -> dict:
    registered = distro_info()
    if not registered:
        raise RuntimeError("未在当前 Windows 用户下检测到 Alpine 发行版")
    if int(registered.get("Version", 0) or 0) != 2:
        raise RuntimeError("Alpine 已注册，但不是 WSL2")

    command = [
        "wsl.exe",
        "-d",
        DISTRO_NAME,
        "-e",
        "/bin/sh",
        "-c",
        "echo ALPINE_SHELL_OK; cat /etc/alpine-release; uname -a; id",
    ]
    try:
        result = run_command(command, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Alpine Shell 验证超时") from exc
    output = command_output(result)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    release = next(
        (line for line in lines if line and line[0].isdigit() and "Linux" not in line),
        "未知",
    )
    kernel = next((line for line in lines if line.startswith("Linux")), "未知")
    identity = next((line for line in lines if line.startswith("uid=")), "未知")
    if (
        result.returncode == 0
        and "ALPINE_SHELL_OK" in output
        and kernel.startswith("Linux")
        and "WSL2" in kernel
        and identity.startswith("uid=0(root)")
    ):
        return {
            "output": output,
            "release": release,
            "kernel": kernel,
            "identity": identity,
            "base_path": registered.get("BasePath", str(install_dir())),
        }
    raise RuntimeError(
        "Alpine 完整验证失败，需要同时确认 Alpine 版本、WSL2 内核和 root 身份：\n"
        f"{output or f'返回码 {result.returncode}'}"
    )


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("660x500")
        self.root.resizable(False, False)
        self.root.configure(bg="white")
        self.is_busy = False
        self.alpine_installed = False
        self.resume_state = load_resume_state()
        self.reboot_pending = self.resume_state is not None
        self.reinstall_requested = bool(
            self.resume_state and self.resume_state.get("reinstall_alpine", True)
        )

        try:
            icon = bundle_dir() / "app_icon.ico"
            if icon.is_file():
                self.root.iconbitmap(str(icon))
        except tk.TclError:
            pass

        self._build_ui()
        self.refresh_status()
        if self.resume_state:
            self.alpine_var.set(self.reinstall_requested or not self.alpine_installed)
            self.status_label.config(text="检测到重启续装任务")
        self.set_busy(False)

        if "--resume" in sys.argv:
            remove_resume_task()
            if self.resume_state:
                self.root.after(800, self.resume_install)
            else:
                self.root.after(
                    200,
                    lambda: messagebox.showinfo("没有续装任务", "没有检测到待继续的安装任务。"),
                )

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
            text="自动启用 WSL 与虚拟机平台、安装内核，并验证 Alpine、WSL2 和 root 登录。",
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
            text="验证安装",
            width=14,
            bg="#107C10",
            fg="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            relief=tk.FLAT,
            command=self.on_test,
            state=tk.DISABLED,
        )
        self.test_btn.place(x=20, y=12)
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
        self.install_btn.place(x=150, y=12)
        self.restart_btn = tk.Button(
            buttons,
            text="重启并继续",
            width=14,
            bg="#C0504D",
            fg="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            relief=tk.FLAT,
            command=self.on_reboot,
            state=tk.DISABLED,
        )
        self.restart_btn.place(x=290, y=12)
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
        self.set_busy(self.is_busy)

    def set_busy(self, busy: bool):
        self.is_busy = busy
        self.install_btn.config(
            state=tk.DISABLED if busy or self.reboot_pending else tk.NORMAL
        )
        self.test_btn.config(
            state=tk.NORMAL if not busy and self.alpine_installed else tk.DISABLED
        )
        self.restart_btn.config(
            state=tk.NORMAL if not busy and self.reboot_pending else tk.DISABLED
        )

    def on_test(self):
        if self.is_busy:
            return
        self.set_busy(True)

        def worker():
            try:
                info = verify_alpine()
                self.log(f"验证成功：{info['output']}")
                self.root.after(
                    0,
                    lambda: messagebox.showinfo("验证成功", verification_text(info)),
                )
            except Exception as exc:
                message = str(exc)
                self.log(f"验证失败：{message}")
                self.root.after(0, lambda: messagebox.showerror("验证失败", message))
            finally:
                self.root.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def on_install(self):
        if self.is_busy or self.reboot_pending:
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
            "将离线启用 WSL2、安装内核更新和 Alpine Linux，最后执行完整验证。"
            f"{warning}\n\n是否继续？",
        ):
            return
        self.start_install()

    def start_install(self):
        self.reinstall_requested = self.alpine_var.get()
        self.set_busy(True)
        self.progress_var.set(0)
        threading.Thread(target=self.install_worker, daemon=True).start()

    def resume_install(self):
        if self.is_busy or not self.resume_state:
            return
        self.reinstall_requested = bool(self.resume_state.get("reinstall_alpine", True))
        self.reboot_pending = False
        self.alpine_var.set(self.reinstall_requested or not self.alpine_installed)
        self.log("检测到重启续跑状态，自动继续剩余安装步骤。")
        self.start_install()

    def on_reboot(self):
        if self.is_busy or not self.reboot_pending:
            return
        if not messagebox.askyesno(
            "确认重启",
            "现在重启 Windows 吗？\n\n请先保存其他程序的工作。重新登录后，安装器会自动以管理员权限打开并继续剩余步骤。",
        ):
            return
        try:
            save_resume_state(self.reinstall_requested or not self.alpine_installed)
            schedule_resume_task()
            self.log("重启续跑任务已创建，将在当前用户下次登录时自动运行。")
            result = run_command(["shutdown.exe", "/r", "/t", "5"], timeout=15)
            if result.returncode != 0:
                raise RuntimeError(f"重启命令失败：{command_output(result)}")
            self.root.destroy()
        except Exception as exc:
            message = str(exc)
            self.log(f"无法重启：{message}")
            messagebox.showerror("重启失败", message)

    def install_worker(self):
        try:
            self.enable_wsl_features()
            self.root.after(0, lambda: self.progress_var.set(25))
            self.install_kernel()
            self.root.after(0, lambda: self.progress_var.set(45))
            self.set_default_wsl2()
            if self.reinstall_requested or not self.alpine_installed:
                self.install_alpine()
            self.root.after(0, lambda: self.progress_var.set(85))
            self.update_status("正在验证 Alpine、WSL2 和 root 登录……")
            info = verify_alpine()
            self.log(f"✓ 完整验证成功：{info['output']}")
            clear_resume_state()
            remove_resume_task()
            self.resume_state = None
            self.reboot_pending = False
            self.root.after(0, lambda: self.progress_var.set(100))
            self.update_status("安装和验证全部完成")
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "安装完成",
                    "Alpine Linux 已通过 WSL2 安装并验证。\n\n"
                    + verification_text(info),
                ),
            )
        except RebootRequired as exc:
            message = str(exc)
            self.reboot_pending = True
            save_resume_state(self.reinstall_requested or not self.alpine_installed)
            self.resume_state = load_resume_state()
            self.update_status("需要重启后继续")
            self.log(message)
            self.root.after(
                0,
                lambda: messagebox.showwarning(
                    "需要重启",
                    f"{message}\n\n请点击“重启并继续”。重新登录后安装器会自动打开并完成剩余步骤。",
                ),
            )
        except Exception as exc:
            message = str(exc)
            clear_resume_state()
            self.resume_state = None
            self.reboot_pending = False
            self.update_status(f"安装失败：{message}")
            self.log(f"✗ 安装失败：{message}")
            self.root.after(0, lambda: messagebox.showerror("安装错误", message))
        finally:
            self.root.after(0, lambda: self.set_busy(False))
            self.root.after(0, self.refresh_status)

    def enable_wsl_features(self):
        self.update_status("正在启用 WSL 和虚拟机平台……")
        reboot_needed = False
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
                reboot_needed = True
                self.log(f"{feature} 已启用，等待统一重启。")
                continue
            if result.returncode != 0:
                raise RuntimeError(
                    f"启用 {feature} 失败（返回码 {result.returncode}）：{command_output(result)}\n"
                    "已使用 /LimitAccess 禁止 Windows Update。若系统提示找不到源文件，"
                    "说明本机组件存储缺少该功能负载，需要提供与 Windows 版本和语言完全匹配的离线源。"
                )
        if reboot_needed:
            raise RebootRequired("Windows 功能已启用，必须重启后才能安装 WSL2 内核。")
        self.log("✓ WSL 和虚拟机平台已启用")

    def install_kernel(self):
        self.update_status("正在静默安装 WSL2 Linux 内核更新……")
        package = bundle_dir() / "wsl_update_x64.msi"
        log_dir = state_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        result = run_command(
            [
                "msiexec.exe",
                "/i",
                str(package),
                "/qn",
                "/norestart",
                "/L*v",
                str(log_dir / "wsl_update_install.log"),
            ],
            timeout=900,
        )
        if result.returncode == 3010:
            raise RebootRequired("WSL2 内核更新已安装，必须重启后才能继续。")
        if result.returncode not in (0, 1638):
            raise RuntimeError(
                f"WSL2 内核安装失败（返回码 {result.returncode}）：{command_output(result)}"
            )
        kernel = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "lxss" / "tools" / "kernel"
        if not kernel.is_file():
            raise RuntimeError(f"MSI 已返回成功，但未找到 WSL2 内核：{kernel}")
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
        target = install_dir()
        if target.exists():
            shutil.rmtree(target)
        self.log("✓ 现有 Alpine 已注销")

    def install_alpine(self):
        if distro_registered():
            self.remove_alpine()
        assets = bundle_dir()
        target = install_dir()
        target.mkdir(parents=True, exist_ok=True)
        launcher = target / "Alpine.exe"
        shutil.copy2(assets / "Alpine.exe", launcher)
        shutil.copy2(assets / "rootfs.tar.gz", target / "rootfs.tar.gz")

        self.update_status("正在通过 Alpine 启动器注册 WSL2 发行版……")
        try:
            result = run_command(
                [str(launcher), "install", "--root"], timeout=900, cwd=target
            )
        except subprocess.TimeoutExpired:
            if not distro_registered():
                raise RuntimeError("Alpine 启动器超时，且未检测到发行版")
            self.log("启动器未及时退出，但注册表已检测到 Alpine，继续独立验证。")
        else:
            if result.returncode != 0:
                raise RuntimeError(f"Alpine 注册失败：{command_output(result)}")

        registered = distro_info()
        if not registered:
            raise RuntimeError("安装命令已结束，但未检测到 Alpine 发行版")
        if int(registered.get("Version", 0) or 0) != 2:
            self.log("正在将 Alpine 转换为 WSL2……")
            result = run_command(
                ["wsl.exe", "--set-version", DISTRO_NAME, "2"], timeout=900
            )
            registered = distro_info()
            if result.returncode != 0 and int(registered.get("Version", 0) or 0) != 2:
                raise RuntimeError(f"转换 Alpine 为 WSL2 失败：{command_output(result)}")
        if int(registered.get("Version", 0) or 0) != 2:
            raise RuntimeError("Alpine 已注册，但无法确认其运行于 WSL2")
        self.log(f"✓ Alpine 已安装：{target}")

    def update_status(self, message: str):
        self.root.after(0, lambda: self.status_label.config(text=message))
        self.log(message)

    def log(self, message: str):
        now = datetime.now()
        timestamp = now.strftime("%H:%M:%S")
        try:
            log_dir = state_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "installer.log").open("a", encoding="utf-8") as stream:
                stream.write(f"[{now.isoformat(timespec='seconds')}] {message}\n")
        except OSError:
            pass
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