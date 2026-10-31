# -*- coding: utf-8 -*-
"""Git + rsync 一键离线安装器（tkinter 版）

参考 uv_installer 结构：
- 内嵌 Git-2.55.0.2-64-bit.exe（官方安装包，静默安装，自带 UAC 提权）
- 内嵌 MSYS2 rsync（rsync.exe + msys-popt-0.dll + msys-xxhash-0.dll），
  运行时从 Git for Windows 的 usr\\bin 复制 msys 运行时 DLL，使 rsync 独立可用
- 已安装的组件提示"重装 / 跳过"，自动配置用户 PATH
"""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

if sys.platform == "win32":
    import winreg

APP_NAME = "Git + Rsync"
CURRENT_VERSION = "1.0.0"
GIT_VERSION = "2.55.0.2"
RSYNC_VERSION = "3.5.1"

# rsync 依赖的 msys 运行时 DLL（由 Git for Windows 的 usr\\bin 提供）
MSYS_DLLS = [
    "msys-2.0.dll", "msys-iconv-2.dll", "msys-crypto-3.dll",
    "msys-idn2-0.dll", "msys-lz4-1.dll", "msys-zstd-1.dll",
    "msys-intl-8.dll", "msys-unistring-5.dll",
]

RSYNC_FILES = ["rsync.exe", "msys-popt-0.dll", "msys-xxhash-0.dll"]


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def broadcast_env_change() -> None:
    result = ctypes.c_long()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
    )


def bundle_dir() -> Path:
    """内嵌资源所在目录（PyInstaller 打包后为 _MEIPASS/assets，开发时为项目 assets/）。

    依次尝试多个候选位置，找不到时返回最后的候选（由调用方报错）。
    """
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


def find_git_exe() -> Path | None:
    """按 PATH → 注册表 → 默认安装路径 顺序查找 git.exe。"""
    which = shutil.which("git")
    if which:
        return Path(which)
    for candidate in [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe",
    ]:
        if candidate.exists():
            return candidate
    return None


def find_git_usr_bin() -> Path | None:
    """查找 Git for Windows 的 usr\\bin（msys DLL 所在目录）。"""
    git_exe = find_git_exe()
    if git_exe:
        usr_bin = git_exe.parent.parent / "usr" / "bin"
        if (usr_bin / "msys-2.0.dll").exists():
            return usr_bin
    for base in [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git",
    ]:
        if (base / "usr" / "bin" / "msys-2.0.dll").exists():
            return base / "usr" / "bin"
    return None


def find_rsync_exe() -> Path | None:
    which = shutil.which("rsync")
    if which:
        return Path(which)
    for d in [Path.home() / "bin", Path.home() / ".local" / "bin"]:
        if (d / "rsync.exe").exists():
            return d / "rsync.exe"
    return None


def get_version(exe: Path, arg: str = "--version") -> str:
    try:
        r = subprocess.run([str(exe), arg], capture_output=True, text=True,
                           timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or r.stderr).splitlines()[0].strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def add_to_user_path(path_str: str) -> bool:
    """把目录加入用户 PATH（已存在则跳过）。返回是否新增。"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, _ = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            current = ""
        paths = [p.strip() for p in current.split(";") if p.strip()]
        if any(p.lower() == path_str.lower() for p in paths):
            return False
        new_path = f"{path_str};{current}" if current else path_str
        winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
    broadcast_env_change()
    return True


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Git + rsync 一键安装器")
        self.root.geometry("640x560")
        self.root.resizable(False, False)
        self.root.configure(bg="white")

        self.is_installing = False
        self.temp_dir = Path(tempfile.gettempdir()) / "git-rsync-installer"

        self._build_ui()
        self.refresh_status()

    # ---------- UI ----------
    def _build_ui(self):
        header = tk.Frame(self.root, bg="#2D2D30", height=90)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="Git + rsync 一键安装器", font=("Microsoft YaHei UI", 15, "bold"),
                 fg="white", bg="#2D2D30").place(x=20, y=16)
        tk.Label(header, text=f"安装器 v{CURRENT_VERSION}  |  内嵌 Git {GIT_VERSION} + rsync {RSYNC_VERSION} (MSYS2)",
                 font=("Microsoft YaHei UI", 9), fg="lightgray", bg="#2D2D30").place(x=20, y=50)

        # 组件状态区
        comp = tk.LabelFrame(self.root, text=" 组件检测（√ = 已安装，可选中后重装）",
                             font=("Microsoft YaHei UI", 9), bg="white", fg="#333")
        comp.place(x=20, y=105, width=600, height=130)

        self.git_var = tk.BooleanVar(value=False)
        self.rsync_var = tk.BooleanVar(value=False)

        self.git_cb = tk.Checkbutton(comp, text="Git for Windows", variable=self.git_var,
                                     font=("Microsoft YaHei UI", 10, "bold"), bg="white",
                                     command=lambda: self._on_toggle("git"))
        self.git_cb.place(x=20, y=12)
        self.git_status = tk.Label(comp, text="检测中...", font=("Microsoft YaHei UI", 8),
                                   fg="gray", bg="white")
        self.git_status.place(x=40, y=42)

        self.rsync_cb = tk.Checkbutton(comp, text="rsync (MSYS2)", variable=self.rsync_var,
                                       font=("Microsoft YaHei UI", 10, "bold"), bg="white",
                                       command=lambda: self._on_toggle("rsync"))
        self.rsync_cb.place(x=320, y=12)
        self.rsync_status = tk.Label(comp, text="检测中...", font=("Microsoft YaHei UI", 8),
                                     fg="gray", bg="white")
        self.rsync_status.place(x=340, y=42)

        tk.Label(self.root, text="rsync 依赖 Git 提供的 msys 运行时，请确保 Git 已安装（可先装 Git 再装 rsync）",
                 font=("Microsoft YaHei UI", 8), fg="darkorange", bg="white").place(x=20, y=242)

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(self.root, variable=self.progress_var, maximum=100,
                        length=600, mode="determinate").place(x=20, y=270)

        self.status_label = tk.Label(self.root, text="就绪", font=("Microsoft YaHei UI", 9),
                                     bg="white", fg="black")
        self.status_label.place(x=20, y=295)

        # 日志
        self.log_text = tk.Text(self.root, width=88, height=13, font=("Consolas", 9),
                                bg="black", fg="white", state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.place(x=20, y=325)
        sb = ttk.Scrollbar(self.root, command=self.log_text.yview)
        sb.place(x=608, y=325, height=180)
        self.log_text.config(yscrollcommand=sb.set)

        # 按钮
        btn_frame = tk.Frame(self.root, bg="#F0F0F0", height=55)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        btn_frame.pack_propagate(False)
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

    def _on_toggle(self, which: str):
        """已安装组件取消勾选 = 跳过；勾选 = 重装。界面文字即时反馈。"""
        installed = self.git_installed if which == "git" else self.rsync_installed
        var = self.git_var if which == "git" else self.rsync_var
        label = self.git_status if which == "git" else self.rsync_status
        text = getattr(self, f"{which}_version_text", "")
        if installed:
            label.config(text=f"{text}  →  将重装" if var.get() else f"{text}  →  将跳过",
                         fg="#0078D7" if var.get() else "gray")

    # ---------- 检测 ----------
    def refresh_status(self):
        self.git_installed = False
        self.rsync_installed = False

        git_exe = find_git_exe()
        if git_exe:
            self.git_installed = True
            self.git_version_text = f"已安装：{get_version(git_exe)}"
            self.git_status.config(text=self.git_version_text, fg="green")
            self.git_var.set(False)
        else:
            self.git_version_text = ""
            self.git_status.config(text="未安装", fg="red")
            self.git_var.set(True)

        rsync_exe = find_rsync_exe()
        if rsync_exe:
            self.rsync_installed = True
            self.rsync_version_text = f"已安装：{get_version(rsync_exe)}"
            self.rsync_status.config(text=self.rsync_version_text, fg="green")
            self.rsync_var.set(False)
        else:
            self.rsync_version_text = ""
            self.rsync_status.config(text="未安装", fg="red")
            self.rsync_var.set(True)

    # ---------- 安装 ----------
    def on_install(self):
        if self.is_installing:
            return
        selected = []
        if self.git_var.get():
            selected.append("Git")
        if self.rsync_var.get():
            selected.append("rsync")
        if not selected:
            messagebox.showinfo("提示", "没有选中任何组件。\n已安装的组件默认跳过，勾选表示重装。")
            return
        if not messagebox.askyesno("确认安装", "将安装以下组件：\n\n  • " + "\n  • ".join(selected) +
                                   "\n\nGit 安装需要管理员权限（会弹出 UAC 提示）。\n是否继续？"):
            return
        threading.Thread(target=self.install_worker, daemon=True).start()

    def install_worker(self):
        self.is_installing = True
        self.root.after(0, lambda: self.install_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.progress_var.set(0))
        try:
            if self.git_var.get():
                self.install_git()
            self.root.after(0, lambda: self.progress_var.set(60))
            if self.rsync_var.get():
                self.install_rsync()
            self.root.after(0, lambda: self.progress_var.set(100))
            self.update_status("全部完成！请新开终端窗口使用 git / rsync。")
            self.root.after(0, lambda: messagebox.showinfo(
                "安装完成", "安装完成！\n\n请新开终端窗口（cmd / Git Bash / PowerShell）使用 git 和 rsync。"))
        except Exception as e:
            self.update_status(f"安装失败：{e}")
            self.log(f"✗ 安装失败：{e}")
            self.root.after(0, lambda: messagebox.showerror("安装错误", f"安装失败：\n\n{e}"))
        finally:
            self.is_installing = False
            self.root.after(0, lambda: self.install_btn.config(state=tk.NORMAL))
            self.root.after(0, self.refresh_status)

    def install_git(self):
        self.update_status("安装 Git for Windows ...")
        assets = bundle_dir()
        git_installers = list(assets.glob("Git-*-64-bit.exe"))
        if not git_installers:
            raise RuntimeError("内嵌 Git 安装包缺失（assets/Git-*-64-bit.exe）")
        installer = git_installers[0]
        self.log(f"运行内嵌安装包：{installer.name}（静默安装，请批准 UAC 提示）")

        # Git 官方安装包自带 UAC 提权；参数为 Inno Setup 标准静默参数
        cmd = [
            str(installer),
            "/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-",
            "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS",
            "/COMPONENTS=icons,ext\\reg\\shellhere,assoc,assoc_sh,gitlfs",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode not in (0,):
            raise RuntimeError(f"Git 安装包返回码 {r.returncode}：{r.stderr or r.stdout}")

        git_exe = find_git_exe()
        if not git_exe:
            raise RuntimeError("Git 安装后未找到 git.exe")
        self.log(f"✓ Git 安装成功：{get_version(git_exe)}")

    def install_rsync(self):
        self.update_status("安装 rsync ...")
        target = Path.home() / "bin"
        target.mkdir(parents=True, exist_ok=True)

        assets = bundle_dir()
        for name in RSYNC_FILES:
            src = assets / name
            if not src.exists():
                raise RuntimeError(f"内嵌文件缺失：assets/{name}")
            shutil.copy2(str(src), str(target / name))
            self.log(f"  复制 {name}")

        # 从 Git 的 usr\\bin 复制 msys 运行时 DLL，使 rsync 脱离 Git Bash 也能运行
        usr_bin = find_git_usr_bin()
        if usr_bin:
            for dll in MSYS_DLLS:
                src = usr_bin / dll
                if src.exists():
                    shutil.copy2(str(src), str(target / dll))
            self.log(f"  从 {usr_bin} 复制 msys 运行时 DLL ×{len(MSYS_DLLS)}")
        else:
            self.log("⚠ 未找到 Git 的 usr\\bin，rsync 可能缺少 msys DLL，请先安装 Git")

        if add_to_user_path(str(target)):
            self.log(f"  已加入用户 PATH：{target}")
        else:
            self.log(f"  用户 PATH 已包含 {target}，跳过")

        rsync_exe = target / "rsync.exe"
        ver = get_version(rsync_exe)
        if ver:
            self.log(f"✓ rsync 安装成功：{ver}")
        else:
            raise RuntimeError("rsync 安装后无法运行，请检查 Git 是否已安装（msys DLL 依赖）")

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
