# -*- coding: utf-8 -*-
"""RemoteRipple 一键离线静默安装器

功能：
- 内嵌 dotnet-windowsdesktop-runtime-6.0.0-win-x64.exe（.NET Desktop Runtime 6.0.0）
- 若系统已存在更高版本的 .NET 6 Desktop Runtime，先静默卸载高版本，
  再强制安装 6.0.0（RemoteRipple 依赖 .NET 6.0.0）
- 内嵌 RemoteRipple-1.2.0-setup.exe（WiX Burn /quiet 静默安装）
"""

import os
import re
import shutil
import subprocess
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

if sys.platform == "win32":
    import winreg

APP_NAME = "RemoteRipple"
CURRENT_VERSION = "1.0.0"
REMOTE_RIPPLE_VERSION = "1.2.0"
DOTNET_TARGET_VERSION = "6.0.0"
DOTNET_DESKTOP_FX = "Microsoft.WindowsDesktop.App"

DOTNET_INSTALLER = "dotnet-windowsdesktop-runtime-6.0.0-win-x64.exe"
REMOTE_RIPPLE_INSTALLER = "RemoteRipple-1.2.0-setup.exe"
ICON_FILE = "remoteripple.ico"


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


# ---------------------------------------------------------------------------
# .NET Desktop Runtime 检测 / 卸载 / 强制安装
# ---------------------------------------------------------------------------

def _dotnet_reg_path() -> str:
    return r"SOFTWARE\dotnet\Setup\InstalledVersions\x64\sharedfx\\" + DOTNET_DESKTOP_FX


def get_installed_dotnet_desktop_versions() -> list[str]:
    """返回已安装的 .NET Desktop Runtime (x64) 版本列表（例如 ['6.0.0','6.0.25']）。"""
    if sys.platform != "win32":
        return []
    versions = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            hkey = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _dotnet_reg_path(), 0,
                                  winreg.KEY_READ | view)
        except FileNotFoundError:
            continue
        with hkey:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(hkey, i)
                except OSError:
                    break
                i += 1
                if sub and sub not in versions:
                    versions.append(sub)
    return versions


def parse_version(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def version_gt(a: str, b: str) -> bool:
    return parse_version(a) > parse_version(b)


def find_dotnet_uninstall_product_code(version: str) -> str | None:
    """从卸载注册表找到指定版本 .NET Desktop Runtime 的 ProductCode。"""
    if sys.platform != "win32":
        return None
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    needle = f"Microsoft Windows Desktop Runtime - {version} (x64)"
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            hkey = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0,
                                  winreg.KEY_READ | view)
        except FileNotFoundError:
            continue
        with hkey:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(hkey, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(hkey, sub) as sk:
                        name, _ = winreg.QueryValueEx(sk, "DisplayName")
                except (OSError, FileNotFoundError):
                    continue
                if str(name).strip() != needle:
                    continue
                # key 本身可能就是 GUID；也可能在 UninstallString 里
                if re.match(r"^\{[0-9A-Fa-f\-]{36}\}$", sub):
                    return sub
                try:
                    with winreg.OpenKey(hkey, sub) as sk:
                        us, _ = winreg.QueryValueEx(sk, "UninstallString")
                    m = re.search(r"\{[0-9A-Fa-f\-]{36}\}", str(us))
                    if m:
                        return m.group(0)
                except (OSError, FileNotFoundError):
                    pass
                return None
    return None


def uninstall_dotnet_desktop(version: str, log) -> bool:
    """静默卸载指定版本的 .NET Desktop Runtime。成功返回 True。"""
    code = find_dotnet_uninstall_product_code(version)
    if not code:
        log(f"  ⚠ 未找到 {version} 的卸载 ProductCode，跳过卸载")
        return False
    log(f"  正在静默卸载 .NET Desktop Runtime {version}（{code}）...")
    r = subprocess.run(
        ["msiexec.exe", "/x", code, "/qn", "/norestart",
         f"/REBOOT=ReallySuppress"],
        capture_output=True, text=True, timeout=600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode == 0 or r.returncode == 1605:
        log(f"  ✓ 已卸载 .NET Desktop Runtime {version}")
        return True
    log(f"  ✗ 卸载 {version} 返回码 {r.returncode}：{r.stderr or r.stdout}")
    return False


def install_dotnet_desktop_6_0_0(log) -> None:
    """强制安装 .NET Desktop Runtime 6.0.0。

    若已存在更高版本的 6.x，先卸载高版本再安装；若已存在 6.0.0 则跳过。
    """
    target = DOTNET_TARGET_VERSION
    installed = get_installed_dotnet_desktop_versions()
    log(f"已安装的 .NET Desktop Runtime (x64)：{installed or '无'}")

    has_target = target in installed
    newer = [v for v in installed
             if parse_version(v)[:2] == (6, 0) and version_gt(v, target)]

    if has_target and not newer:
        log(f"✓ .NET Desktop Runtime {target} 已安装，跳过")
        return

    installer = bundle_dir() / DOTNET_INSTALLER
    if not installer.exists():
        raise RuntimeError(f"内嵌 .NET 安装包缺失：{DOTNET_INSTALLER}")

    # 先卸载所有高于 6.0.0 的 6.0.x 版本，否则 6.0.0 安装包会拒绝
    if newer:
        log(f"检测到更高版本的 .NET 6 运行时：{newer}，将先卸载以强制安装 {target}")
        for v in sorted(newer, key=parse_version, reverse=True):
            uninstall_dotnet_desktop(v, log)
        # 等待注册表刷新
        time.sleep(2)

    log(f"运行 {installer.name} /quiet /norestart ...")
    r = subprocess.run(
        [str(installer), "/quiet", "/norestart"],
        capture_output=True, text=True, timeout=1200,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # 1638 (0x666) = 已有更高版本；0x80070666 = HRESULT 包装的 1638
    if r.returncode in (1638, 0x00000666, 0x80070666, -2147023258):
        log("  安装包提示已有更高版本，再次枚举并卸载所有 6.0.x（非目标版本）...")
        for v in list(get_installed_dotnet_desktop_versions()):
            if parse_version(v)[:2] == (6, 0) and v != target:
                uninstall_dotnet_desktop(v, log)
        time.sleep(2)
        log(f"重试安装 .NET Desktop Runtime {target} ...")
        r = subprocess.run(
            [str(installer), "/quiet", "/norestart"],
            capture_output=True, text=True, timeout=1200,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    if r.returncode != 0:
        raise RuntimeError(
            f".NET Desktop Runtime 安装返回码 {r.returncode}（0x{r.returncode & 0xFFFFFFFF:08X}）："
            f"{(r.stderr or r.stdout or '').strip()[:500]}")

    # 验证目标版本已安装
    final = get_installed_dotnet_desktop_versions()
    if target not in final:
        raise RuntimeError(
            f".NET Desktop Runtime {target} 安装后未检测到，当前已安装：{final}")
    log(f"✓ .NET Desktop Runtime {target} 安装成功")


# ---------------------------------------------------------------------------
# RemoteRipple 检测与安装
# ---------------------------------------------------------------------------

def find_remoteripple_uninstall() -> str | None:
    """查找 RemoteRipple 的卸载注册表项 DisplayName，用于判断是否已安装。"""
    if sys.platform != "win32":
        return None
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            hkey = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0,
                                  winreg.KEY_READ | view)
        except FileNotFoundError:
            continue
        with hkey:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(hkey, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(hkey, sub) as sk:
                        name, _ = winreg.QueryValueEx(sk, "DisplayName")
                except (OSError, FileNotFoundError):
                    continue
                if "remoteripple" in str(name).lower():
                    return str(name)
    return None


def install_remoteripple(log) -> None:
    installer = bundle_dir() / REMOTE_RIPPLE_INSTALLER
    if not installer.exists():
        raise RuntimeError(f"内嵌 RemoteRipple 安装包缺失：{REMOTE_RIPPLE_INSTALLER}")
    log(f"运行 {installer.name} /quiet /norestart ...")
    r = subprocess.run(
        [str(installer), "/quiet", "/norestart"],
        capture_output=True, text=True, timeout=1200,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(
            f"RemoteRipple 安装返回码 {r.returncode}："
            f"{(r.stderr or r.stdout or '').strip()[:500]}")
    # WiX Burn /quiet 成功一般返回 0；3010 表示需要重启也算成功
    if r.returncode == 3010:
        log("  ⚠ 安装成功，但需要重启电脑后生效")
    log(f"✓ RemoteRipple {REMOTE_RIPPLE_VERSION} 安装成功")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("RemoteRipple 一键安装器")
        self.root.geometry("640x480")
        self.root.resizable(False, False)
        self.root.configure(bg="white")

        self.is_installing = False
        self.is_busy = False

        try:
            icon = bundle_dir() / ICON_FILE
            if icon.exists():
                self.root.iconbitmap(str(icon))
        except Exception:
            pass

        self._build_ui()
        self.refresh_status()

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#1E3A5F", height=90)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="RemoteRipple 一键安装器",
                 font=("Microsoft YaHei UI", 15, "bold"),
                 fg="white", bg="#1E3A5F").place(x=20, y=16)
        tk.Label(header,
                 text=f"安装器 v{CURRENT_VERSION}  |  内嵌 RemoteRipple {REMOTE_RIPPLE_VERSION} "
                      f"+ .NET Desktop Runtime {DOTNET_TARGET_VERSION}",
                 font=("Microsoft YaHei UI", 9), fg="lightgray",
                 bg="#1E3A5F").place(x=20, y=50)

        comp = tk.LabelFrame(self.root, text=" 组件检测",
                             font=("Microsoft YaHei UI", 9), bg="white", fg="#333")
        comp.place(x=20, y=105, width=600, height=130)

        self.dotnet_var = tk.BooleanVar(value=False)
        self.rr_var = tk.BooleanVar(value=False)

        self.dotnet_cb = tk.Checkbutton(
            comp, text=f".NET Desktop Runtime {DOTNET_TARGET_VERSION}",
            variable=self.dotnet_var, font=("Microsoft YaHei UI", 10, "bold"),
            bg="white", command=lambda: self._on_toggle("dotnet"))
        self.dotnet_cb.place(x=20, y=12)
        self.dotnet_status = tk.Label(comp, text="检测中...",
                                      font=("Microsoft YaHei UI", 8), fg="gray", bg="white")
        self.dotnet_status.place(x=40, y=42)

        self.rr_cb = tk.Checkbutton(
            comp, text=f"RemoteRipple {REMOTE_RIPPLE_VERSION}",
            variable=self.rr_var, font=("Microsoft YaHei UI", 10, "bold"),
            bg="white", command=lambda: self._on_toggle("rr"))
        self.rr_cb.place(x=20, y=68)
        self.rr_status = tk.Label(comp, text="检测中...",
                                  font=("Microsoft YaHei UI", 8), fg="gray", bg="white")
        self.rr_status.place(x=40, y=98)

        tk.Label(self.root,
                 text="若系统已存在更高版本 .NET 6 运行时，将先静默卸载高版本，"
                      "再强制安装 6.0.0（RemoteRipple 依赖该版本）。\n"
                      "安装过程中请批准 UAC 提权提示。",
                 font=("Microsoft YaHei UI", 8), fg="darkorange", bg="white",
                 justify=tk.LEFT).place(x=20, y=245)

        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(self.root, variable=self.progress_var, maximum=100,
                        length=600, mode="determinate").place(x=20, y=285)

        self.status_label = tk.Label(self.root, text="就绪",
                                     font=("Microsoft YaHei UI", 9), bg="white", fg="black")
        self.status_label.place(x=20, y=310)

        self.log_text = tk.Text(self.root, width=88, height=7, font=("Consolas", 9),
                                bg="black", fg="white", state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.place(x=20, y=335)
        sb = ttk.Scrollbar(self.root, command=self.log_text.yview)
        sb.place(x=608, y=335, height=100)
        self.log_text.config(yscrollcommand=sb.set)

        btn_frame = tk.Frame(self.root, bg="#F0F0F0", height=55)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        btn_frame.pack_propagate(False)
        self.install_btn = tk.Button(btn_frame, text="安装选中项", width=14, bg="#0078D7",
                                     fg="white", font=("Microsoft YaHei UI", 9, "bold"),
                                     relief=tk.FLAT, command=self.on_install)
        self.install_btn.place(x=250, y=12)
        tk.Button(btn_frame, text="重新检测", width=10, bg="#646464", fg="white",
                  font=("Microsoft YaHei UI", 9), relief=tk.FLAT,
                  command=self.refresh_status).place(x=380, y=12)
        tk.Button(btn_frame, text="退出", width=8, bg="#A0A0A0", fg="white",
                  font=("Microsoft YaHei UI", 9), relief=tk.FLAT,
                  command=self.root.destroy).place(x=485, y=12)

    def _on_toggle(self, which: str):
        var = getattr(self, f"{which}_var")
        label = getattr(self, f"{which}_status")
        text = getattr(self, f"{which}_version_text", "")
        if getattr(self, f"{which}_installed"):
            label.config(text=f"{text}  →  将重装" if var.get() else f"{text}  →  将跳过",
                         fg="#0078D7" if var.get() else "gray")

    def refresh_status(self):
        # .NET
        dotnet_installed = False
        vers = get_installed_dotnet_desktop_versions()
        if DOTNET_TARGET_VERSION in vers:
            dotnet_installed = True
            self.dotnet_version_text = f"已安装：.NET Desktop Runtime {DOTNET_TARGET_VERSION}"
            self.dotnet_status.config(text=self.dotnet_version_text, fg="green")
            self.dotnet_var.set(False)
        else:
            self.dotnet_version_text = ""
            extra = f"（当前：{', '.join(vers)}）" if vers else ""
            self.dotnet_status.config(text=f"未安装 {DOTNET_TARGET_VERSION}{extra}", fg="red")
            self.dotnet_var.set(True)
        self.dotnet_installed = dotnet_installed

        # RemoteRipple
        rr_name = find_remoteripple_uninstall()
        if rr_name:
            self.rr_installed = True
            self.rr_version_text = f"已安装：{rr_name}"
            self.rr_status.config(text=self.rr_version_text, fg="green")
            self.rr_var.set(False)
        else:
            self.rr_installed = False
            self.rr_version_text = ""
            self.rr_status.config(text="未安装", fg="red")
            self.rr_var.set(True)

    def on_install(self):
        if self.is_installing or self.is_busy:
            return
        if not (self.dotnet_var.get() or self.rr_var.get()):
            messagebox.showinfo("提示", "未选中任何组件。\n已安装的组件默认跳过，勾选表示重装。")
            return
        items = []
        if self.dotnet_var.get():
            items.append(f".NET Desktop Runtime {DOTNET_TARGET_VERSION}（强制安装）")
        if self.rr_var.get():
            items.append(f"RemoteRipple {REMOTE_RIPPLE_VERSION}（静默安装）")
        if not messagebox.askyesno(
                "确认安装", "将安装：\n\n  • " + "\n  • ".join(items) +
                "\n\n安装过程中请批准 UAC 提权提示。\n若系统存在更高版本 .NET 6，将先卸载高版本。\n\n是否继续？"):
            return
        threading.Thread(target=self.install_worker, daemon=True).start()

    def install_worker(self):
        self.is_installing = True
        self.is_busy = True
        self.root.after(0, lambda: self.install_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.progress_var.set(0))
        try:
            steps = []
            if self.dotnet_var.get():
                steps.append(("dotnet", self._install_dotnet))
            if self.rr_var.get():
                steps.append(("rr", self._install_remoteripple))

            n = len(steps)
            for idx, (name, fn) in enumerate(steps, 1):
                self.update_status(f"[{idx}/{n}] 正在安装 ...")
                fn()
                self.root.after(0, lambda i=idx: self.progress_var.set(int(i / n * 100)))

            self.root.after(0, lambda: self.progress_var.set(100))
            self.update_status("全部完成！")
            self.root.after(0, lambda: messagebox.showinfo(
                "安装完成", "所选组件安装完成！\n\n详情请查看日志。"))
        except Exception as e:
            self.update_status(f"安装失败：{e}")
            self.log(f"✗ 安装失败：{e}")
            self.root.after(0, lambda: messagebox.showerror("安装错误", f"安装失败：\n\n{e}"))
        finally:
            self.is_installing = False
            self.is_busy = False
            self.root.after(0, lambda: self.install_btn.config(state=tk.NORMAL))
            self.root.after(0, self.refresh_status)

    def _install_dotnet(self):
        self.update_status("安装/强制安装 .NET Desktop Runtime 6.0.0 ...")
        install_dotnet_desktop_6_0_0(self.log)

    def _install_remoteripple(self):
        self.update_status("安装 RemoteRipple ...")
        install_remoteripple(self.log)

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
