# -*- coding: utf-8 -*-
"""MariaDB 一键离线安装器（tkinter 版）

参考 mosquitto_installer 结构：
- 内嵌 mariadb-13.0.2-winx64.msi，通过 msiexec /qn 静默安装（自带 UAC 提权）
- 安装属性：INSTALLDIR / DATADIR 位于 D:/JustSmart/MariaDB（无 D 盘时回退到
  %APPDATA%/MariaDB，目录不存在自动创建），PORT=8306，PASSWORD=root 密码，
  INSTALLSERVICE=1 注册 Windows 服务
- 安装完成后更新 my.ini 使 bind-address=0.0.0.0（允许任意地址连接）
- 创建管理员账号 justsmart / justsmart（'%' + 'localhost'，授予全部权限）
- 通过内嵌的 mysql.exe 客户端以 justsmart 账号执行 SQL 测试数据库连接
"""

import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

if sys.platform == "win32":
    import winreg

APP_NAME = "MariaDB"
CURRENT_VERSION = "1.0.0"
MARIADB_VERSION = "13.0.2"
SERVICE_NAME = "MariaDB"

MSI_GLOB = "mariadb-*-winx64.msi"

DB_PORT = 8306
ADMIN_USER = "justsmart"
ADMIN_PASSWORD = "justsmart"
ROOT_PASSWORD = "justsmart"
BIND_ADDRESS = "0.0.0.0"


def default_db_home() -> Path:
    """优先 D:\\JustSmart\\MariaDB；无 D 盘时回退到 %APPDATA%/MariaDB。"""
    if Path("D:/").exists():
        return Path(r"D:\JustSmart\MariaDB")
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "MariaDB"
    return Path.home() / "AppData" / "Roaming" / "MariaDB"


DB_HOME = default_db_home()
DATA_DIR = DB_HOME / "data"

SERVICE_START_TIMEOUT = 90.0
CLIENT_CMD_TIMEOUT = 60.0


def ensure_dirs() -> None:
    """目录不存在时自动创建（安装完成/回退路径兜底）。

    注意：只能在 MSI 安装完成后或更新配置时调用；MSI 要求 INSTALLDIR 在
    全新安装时不预先存在，否则返回 1603（"directory exists and is not empty"）。
    """
    for d in (DB_HOME, DATA_DIR):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)


def find_my_ini(install_dir: Path | None = None) -> Path | None:
    """查找 MariaDB 的 my.ini：数据目录 → 安装目录 data → 安装目录根。"""
    candidates = [DATA_DIR / "my.ini"]
    if install_dir:
        candidates += [install_dir / "data" / "my.ini", install_dir / "my.ini"]
    for c in candidates:
        if c.exists():
            return c
    return None


def set_bind_address(my_ini: Path) -> bool:
    """强制 my.ini 中 bind-address=BIND_ADDRESS（允许任意地址连接）。

    已是目标值返回 False；有修改返回 True（需重启服务生效）。
    """
    try:
        text = my_ini.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    lines = text.splitlines()
    replaced = False
    out = []
    for line in lines:
        s = line.strip()
        if s.startswith("#") and "bind-address" in s.lower():
            continue
        if s.lower().startswith("bind-address"):
            out.append(f"bind-address={BIND_ADDRESS}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"bind-address={BIND_ADDRESS}")
    new_text = "\n".join(out) + "\n"
    if new_text == text:
        return False
    try:
        my_ini.write_text(new_text, encoding="utf-8")
    except PermissionError:
        raise RuntimeError(f"无权限写入 {my_ini}，请以管理员身份运行本安装器")
    return True


def restart_service() -> bool:
    """重启 MariaDB 服务直至端口就绪，成功返回 True。"""
    subprocess.run(["net", "stop", SERVICE_NAME], capture_output=True, text=True,
                   timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(2)
    subprocess.run(["net", "start", SERVICE_NAME], capture_output=True, text=True,
                   timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return wait_for_service()


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


def find_install_dir() -> Path | None:
    """从卸载注册表项读取安装目录（DisplayName 含 MariaDB 的条目）。"""
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
            while i < 3000:
                try:
                    subkey = winreg.EnumKey(hkey, i)
                    i += 1
                except OSError:
                    break
                if "mariadb" not in subkey.lower():
                    continue
                try:
                    with winreg.OpenKey(hkey, subkey) as sk:
                        name, _ = winreg.QueryValueEx(sk, "DisplayName")
                    if "mariadb" not in str(name).lower():
                        continue
                    for value_name in ["InstallLocation", "DisplayIcon", "ARPINSTALLLOCATION"]:
                        try:
                            loc, _ = winreg.QueryValueEx(sk, value_name)
                        except FileNotFoundError:
                            continue
                        loc = str(loc).strip().strip('"')
                        if loc and Path(loc).exists() and (Path(loc) / "bin").exists():
                            return Path(loc).parent
                except (OSError, ValueError):
                    continue
    return None


def find_client_exe() -> Path | None:
    """按 known 安装目录 → 注册表 → 默认安装路径 顺序查找 mysql.exe 客户端。"""
    candidates = []
    install_dir = find_install_dir()
    if install_dir:
        candidates.append(install_dir / "bin" / "mysql.exe")
    if DB_HOME.exists():
        candidates.append(DB_HOME / "bin" / "mysql.exe")
    prog = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    for base in [prog / "MariaDB 13.0", prog / "MariaDB 11.8", prog / "MariaDB 11.4",
                 prog / "MariaDB 11.3", prog / "MariaDB 11.2", prog / "MariaDB 11.1",
                 prog / "MariaDB 11.0", prog / "MariaDB 10.11", prog / "MariaDB 10.6"]:
        candidates.append(base / "bin" / "mysql.exe")
    for c in candidates:
        if c.exists():
            return c
    return None


def get_version(client_exe: Path) -> str:
    """mysql --version 输出：mysql from Ver ... for Win64 on x86 (MariaDB 13.0.2)。"""
    try:
        r = subprocess.run([str(client_exe), "--version"], capture_output=True, text=True,
                           timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"MariaDB\s+([0-9.]+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def service_exists() -> bool:
    try:
        code = subprocess.run(["sc", "query", SERVICE_NAME],
                              capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if code.returncode == 0 and "STATE" in (code.stdout or code.stderr or ""):
            return True
        code = subprocess.run(["net", "start"], capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return SERVICE_NAME.lower() in (code.stdout or "").lower()
    except Exception:
        return False


def service_is_running() -> bool:
    try:
        code = subprocess.run(["sc", "query", SERVICE_NAME],
                              capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = code.stdout or code.stderr or ""
        return "STATE" in out and re.search(r"STATE\s+\S+\s+RUNNING", out) is not None
    except Exception:
        return False


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_sql(client_exe: Path, user: str, password: str, sql: str,
            port: int = DB_PORT, host: str = "127.0.0.1") -> str:
    """用 mysql.exe 客户端执行 SQL，返回 stdout；失败抛出异常。"""
    cmd = [str(client_exe),
           "--host", host,
           "--port", str(port),
           "--user", user,
           "--password", password,
           "--connect-timeout", "15",
           "--protocol", "TCP",
           "--default-character-set", "utf8mb4",
           "-e", sql]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=CLIENT_CMD_TIMEOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"SQL 执行超时（{CLIENT_CMD_TIMEOUT:.0f}s）：{sql}")
    out = r.stdout or ""
    err = r.stderr or ""
    if r.returncode != 0:
        err_norm = err.strip().splitlines()
        detail = err_norm[-1] if err_norm else f"returncode {r.returncode}"
        raise RuntimeError(f"SQL 执行失败：{detail}")
    return out


def read_text_auto(path: Path) -> str:
    """读取文本并自动识别编码（msiexec /L*v 日志为 UTF-16LE）。"""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore").lstrip("\ufeff")
    return raw.decode("utf-8", errors="ignore")


def extract_msi_log_errors(msi_log: Path, max_lines: int = 8) -> str:
    """从 msiexec /L*v 日志中提取错误相关行（找不到日志返回空串）。"""
    markers = ["error 1", "return value 3", "installation directory", "not empty",
               "failed", "error 2", "mainenginethread"]
    try:
        lines = read_text_auto(msi_log).splitlines()
    except Exception:
        return ""
    hits = []
    seen = set()
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if any(m in low for m in markers):
            if "property change" in low or s in seen:
                continue
            seen.add(s)
            hits.append(s)
        if len(hits) >= max_lines:
            break
    return " / ".join(reversed(hits))


def remove_empty_tree(root: Path) -> int:
    """自底向上删除空目录树；只要树中存在任何文件则不动该分支。

    返回删除的目录数。用于卸载后清掉 MSI 留下的空目录残留
    （MSI 拒绝安装进已存在的非空 INSTALLDIR）。
    """
    if not root.exists():
        return 0
    has_file = False
    removed = 0
    try:
        entries = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    except Exception:
        return 0
    for p in entries:
        try:
            if p.is_dir():
                p.rmdir()
                removed += 1
            else:
                has_file = True
        except OSError:
            has_file = True
    if not has_file:
        try:
            root.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def wait_for_service(timeout: float = SERVICE_START_TIMEOUT) -> bool:
    """等待服务进入 RUNNING 且端口可连接。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if service_is_running():
            while time.monotonic() - t0 < timeout:
                if port_open(DB_PORT):
                    return True
                time.sleep(1)
            return False
        time.sleep(2)
    return False


def create_admin_account(client_exe: Path) -> None:
    """创建管理员账号 justsmart，授予全部权限并可从任意主机连接。"""
    esc = ADMIN_PASSWORD.replace("'", "''")
    sql = (
        f"CREATE USER IF NOT EXISTS '{ADMIN_USER}'@'%' IDENTIFIED BY '{esc}';"
        f"ALTER USER '{ADMIN_USER}'@'%' IDENTIFIED BY '{esc}';"
        f"CREATE USER IF NOT EXISTS '{ADMIN_USER}'@'localhost' IDENTIFIED BY '{esc}';"
        f"ALTER USER '{ADMIN_USER}'@'localhost' IDENTIFIED BY '{esc}';"
        f"GRANT ALL PRIVILEGES ON *.* TO '{ADMIN_USER}'@'%' WITH GRANT OPTION;"
        f"GRANT ALL PRIVILEGES ON *.* TO '{ADMIN_USER}'@'localhost' WITH GRANT OPTION;"
        "FLUSH PRIVILEGES;"
    )
    run_sql(client_exe, "root", ROOT_PASSWORD, sql, host="localhost")


def db_connection_test(client_exe: Path, user: str = ADMIN_USER,
                       password: str = ADMIN_PASSWORD) -> str:
    """以指定账号连接 127.0.0.1:8306 执行 SQL 测试，返回摘要（失败抛异常）。"""
    out = run_sql(client_exe, user, password,
                  "SELECT VERSION() AS version, CURRENT_USER() AS user, "
                  "@@port AS port, @@bind_address AS bind;")
    first = "; ".join(line.strip() for line in out.strip().splitlines() if line.strip())
    if "0.0.0.0" not in first and "*" not in first:
        raise RuntimeError(f"数据库未监听 0.0.0.0（bind_address 查询结果：{first.split('|')[-1].strip()}），"
                           "请检查 my.ini 配置")
    out_list = run_sql(client_exe, user, password, "SHOW DATABASES;")
    dbs = [l.strip() for l in out_list.strip().splitlines()[1:] if l.strip()]
    db_names = ", ".join(dbs[:10]) + (" ..." if len(dbs) > 10 else "")
    return f"{first} | 数据库：{db_names}"


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MariaDB 一键安装器")
        self.root.geometry("660x490")
        self.root.resizable(False, False)
        self.root.configure(bg="white")

        self.is_installing = False
        self.is_busy = False
        self.temp_dir = Path(tempfile.gettempdir()) / "mariadb-installer"

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
        header = tk.Frame(self.root, bg="#00365B", height=90)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="MariaDB 一键安装器", font=("Microsoft YaHei UI", 15, "bold"),
                 fg="white", bg="#00365B").place(x=20, y=16)
        tk.Label(header, text=f"安装器 v{CURRENT_VERSION}  |  内嵌 MariaDB "
                              f"{MARIADB_VERSION} (MSI)",
                 font=("Microsoft YaHei UI", 9), fg="lightgray", bg="#00365B").place(x=20, y=50)

        # 组件状态区
        comp = tk.LabelFrame(self.root, text=" 组件检测（√ = 已安装，可选中后重装）",
                             font=("Microsoft YaHei UI", 9), bg="white", fg="#333")
        comp.place(x=20, y=105, width=620, height=110)

        self.db_var = tk.BooleanVar(value=False)

        self.db_cb = tk.Checkbutton(comp, text="MariaDB Server & Client", variable=self.db_var,
                                    font=("Microsoft YaHei UI", 10, "bold"), bg="white",
                                    command=lambda: self._on_toggle("db"))
        self.db_cb.place(x=20, y=12)
        self.db_status = tk.Label(comp, text="检测中...", font=("Microsoft YaHei UI", 8),
                                  fg="gray", bg="white")
        self.db_status.place(x=40, y=42)

        self.svc_status = tk.Label(comp, text="服务：检测中...", font=("Microsoft YaHei UI", 8),
                                   fg="gray", bg="white")
        self.svc_status.place(x=40, y=68)

        tk.Label(self.root,
                 text=f"数据库目录 {DB_HOME}（无 D 盘时回退 %APPDATA%/MariaDB，自动创建），"
                      f"端口 {DB_PORT}，监听 0.0.0.0。\n"
                      f"完成后自动创建管理员 {ADMIN_USER}（允许远程连接）并测试数据库连接",
                 font=("Microsoft YaHei UI", 8), fg="darkorange", bg="white").place(x=20, y=222)

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(self.root, variable=self.progress_var, maximum=100,
                        length=620, mode="determinate").place(x=20, y=250)

        self.status_label = tk.Label(self.root, text="就绪", font=("Microsoft YaHei UI", 9),
                                     bg="white", fg="black")
        self.status_label.place(x=20, y=275)

        # 日志
        self.log_text = tk.Text(self.root, width=90, height=8, font=("Consolas", 9),
                                bg="black", fg="white", state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.place(x=20, y=305)
        sb = ttk.Scrollbar(self.root, command=self.log_text.yview)
        sb.place(x=628, y=305, height=115)
        self.log_text.config(yscrollcommand=sb.set)

        # 按钮
        btn_frame = tk.Frame(self.root, bg="#F0F0F0", height=55)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        btn_frame.pack_propagate(False)
        self.test_btn = tk.Button(btn_frame, text="数据库测试", width=12, bg="#107C10",
                                  fg="white", font=("Microsoft YaHei UI", 9, "bold"),
                                  relief=tk.FLAT, command=self.on_db_test,
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
                  command=self.root.destroy).place(x=555, y=12)

    def _on_toggle(self, which: str):
        """已安装组件取消勾选 = 跳过；勾选 = 重装。界面文字即时反馈。"""
        var = getattr(self, f"{which}_var")
        label = getattr(self, f"{which}_status")
        text = getattr(self, f"{which}_version_text", "")
        if getattr(self, f"{which}_installed"):
            label.config(text=f"{text}  →  将重装" if var.get() else f"{text}  →  将跳过",
                         fg="#0078D7" if var.get() else "gray")

    # ---------- 检测 ----------
    def refresh_status(self):
        db_installed = False

        client_exe = find_client_exe()
        self.db_existing_client = client_exe
        if client_exe:
            db_installed = True
            ver = get_version(client_exe)
            self.db_version_text = (f"已安装：MariaDB {ver or '未知版本'} "
                                    f"({client_exe.parent.parent})")
            self.db_status.config(text=self.db_version_text, fg="green")
            self.db_var.set(False)
        else:
            self.db_version_text = ""
            self.db_status.config(text="未安装", fg="red")
            self.db_var.set(True)

        try:
            if service_exists():
                running = service_is_running()
                svc_state = "运行中" if running else "已停止"
                self.svc_status.config(text=f"服务：{SERVICE_NAME} 已安装（{svc_state}）",
                                       fg="green" if running else "darkorange")
            else:
                self.svc_status.config(text="服务：未安装", fg="gray")
        except Exception:
            self.svc_status.config(text="服务：检测失败", fg="gray")

        self.db_installed = db_installed
        self.root.after(0, lambda: self.test_btn.config(
            state=tk.NORMAL if db_installed else tk.DISABLED))

    # ---------- 测试 ----------
    def on_db_test(self):
        if self.is_busy:
            return
        self.is_busy = True
        self.root.after(0, lambda: self.test_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.install_btn.config(state=tk.DISABLED))

        def worker():
            try:
                result = self.run_db_test()
                self.root.after(0, lambda: messagebox.showinfo(
                    "数据库测试", f"127.0.0.1:{DB_PORT} 账号 {ADMIN_USER} 连接测试通过！"
                    f"\n\n{result}"))
            except Exception as e:
                self.log(f"✗ 数据库连接测试失败：{e}")
                self.root.after(0, lambda: messagebox.showerror(
                    "数据库测试", f"数据库连接测试失败：\n\n{e}"))
            finally:
                self.is_busy = False
                self.root.after(0, self.refresh_status)

        threading.Thread(target=worker, daemon=True).start()

    def run_db_test(self) -> str:
        client_exe = find_client_exe()
        if not client_exe:
            raise RuntimeError("未找到 mysql.exe 客户端，请确认 MariaDB 已安装")
        if not port_open(DB_PORT):
            raise RuntimeError(f"127.0.0.1:{DB_PORT} 端口不可连接，请确认 MariaDB 服务已启动")
        self.log(f"开始数据库连接测试：127.0.0.1:{DB_PORT} 账号 {ADMIN_USER} ...")
        result = db_connection_test(client_exe)
        self.log(f"✓ 数据库连接测试通过：{result}")
        return result

    # ---------- 安装 ----------
    def on_install(self):
        if self.is_installing or self.is_busy:
            return
        if not self.db_var.get():
            messagebox.showinfo("提示", "未选中任何组件。\n已安装的组件默认跳过，勾选表示重装。")
            return
        extra = "\n\n注意：安装过程中请批准 UAC 提权提示（msiexec 需要管理员权限）。" \
                "\nroot 密码与管理员账号均为 justsmart，端口 8306。" \
                if self.db_installed else \
                "\n\n安装过程中请批准 UAC 提权提示（msiexec 需要管理员权限）。"
        if not messagebox.askyesno("确认安装", "将安装：\n\n  • MariaDB "
                                   f"{MARIADB_VERSION}（MSI 静默安装）\n\n"
                                   f"  • 安装/数据目录：{DB_HOME}\n"
                                   f"  • 端口 {DB_PORT}  服务名 {SERVICE_NAME}\n"
                                   "  • 管理员账号 justsmart / justsmart" + extra + "\n\n是否继续？"):
            return
        threading.Thread(target=self.install_worker, daemon=True).start()

    def install_worker(self):
        self.is_installing = True
        self.is_busy = True
        self.root.after(0, lambda: self.install_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.test_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.progress_var.set(0))
        try:
            self.install_mariadb()
            try:
                result = self.run_db_test()
            except Exception as te:
                self.log(f"✗ 数据库连接测试失败：{te}")
                self.update_status("安装完成，但数据库连接测试失败")
                self.root.after(0, lambda: messagebox.showwarning(
                    "数据库测试", f"安装完成，但数据库连接测试失败：\n\n{te}"))
                return
            self.root.after(0, lambda: self.progress_var.set(100))
            self.update_status("全部完成！")
            self.root.after(0, lambda: messagebox.showinfo(
                "安装完成", "MariaDB 安装完成！\n\n"
                f"管理员 {ADMIN_USER} 数据库连接测试通过：\n{result}\n\n详情请查看日志。"))
        except Exception as e:
            self.update_status(f"安装失败：{e}")
            self.log(f"✗ 安装失败：{e}")
            self.root.after(0, lambda: messagebox.showerror("安装错误", f"安装失败：\n\n{e}"))
        finally:
            self.is_installing = False
            self.is_busy = False
            self.root.after(0, lambda: self.install_btn.config(state=tk.NORMAL))
            self.root.after(0, self.refresh_status)

    def install_mariadb(self):
        self.update_status("安装 MariaDB ...")
        assets = bundle_dir()
        msis = list(assets.glob(MSI_GLOB))
        if not msis:
            raise RuntimeError(f"内嵌 MariaDB 安装包缺失（assets/{MSI_GLOB}）")
        msi_path = msis[0]

        if self.db_installed and self.db_existing_client and \
                service_exists():
            self.log("已存在 MariaDB 服务，跳过重复安装")
            return

        # 不完整安装自愈：有服务端残留但没有 mysql.exe 客户端（如只装了
        # MYSQLSERVER 父 Feature 而未装 Client/DBInstance 子 Feature），
        # 先静默卸载残留，再按完整 Feature 列表重装
        partial_server = (DB_HOME / "bin" / "mariadbd.exe").exists()
        if not self.db_existing_client and partial_server:
            self.log("检测到不完整的 MariaDB 安装（缺少客户端/实例配置），正在清理 ...")
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            r_un = subprocess.run(
                ["msiexec", "/x", str(msi_path), "/qn", "/norestart",
                 "/L*v", str(self.temp_dir / "mariadb-uninstall.log")],
                capture_output=True, text=True, timeout=900,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r_un.returncode == 0:
                self.log("✓ 残留卸载完成")
                n = remove_empty_tree(DB_HOME)
                if DB_HOME.exists() and any(DB_HOME.iterdir()):
                    raise RuntimeError(
                        f"残留清理后 {DB_HOME} 仍存在文件，MSI 无法安装进非空目录；"
                        "请确认无重要数据后手动删除该目录再安装")
                if n:
                    self.log(f"✓ 已清理 {n} 个空目录残留")
                self.log("开始重新安装")
            else:
                self.log(f"⚠ 旧包卸载返回码 {r_un.returncode}（未安装则可忽略），继续安装 ...")

        # 安装前检查：MSI 拒绝安装进已存在的非空 INSTALLDIR；
        # 若仅剩空目录残留（无任何文件）则自动清理
        if not self.db_existing_client and DB_HOME.exists() and any(DB_HOME.iterdir()):
            remove_empty_tree(DB_HOME)
            if DB_HOME.exists() and any(DB_HOME.iterdir()):
                raise RuntimeError(
                    f"安装目录 {DB_HOME} 已存在且有内容，MSI 拒绝安装（1603）；"
                    "请确认无重要数据后手动删除该目录再安装")

        # 无 D 盘时回退 %APPDATA%/MariaDB（目录由 MSI 自动创建，不可预先创建：
        # MSI 要求全新安装时 INSTALLDIR 不存在，否则返回 1603）
        fallback_tag = "（无 D 盘，回退到 %APPDATA%/MariaDB）" if not Path("D:/").exists() else ""
        self.log(f"数据库目录：{DB_HOME}{fallback_tag}")

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        msi_log = self.temp_dir / "mariadb-install.log"
        self.log(f"运行内嵌安装包：{msi_path.name}（msiexec /qn 静默安装，请批准 UAC 提示）")
        cmd = ["msiexec", "/i", str(msi_path), "/qn", "/norestart",
               "/L*v", str(msi_log),
               "ADDLOCAL=MYSQLSERVER,Client,SharedClientServerComponents,DBInstance,Backup",
               f"INSTALLDIR={DB_HOME}",
               f"DATADIR={DATA_DIR}",
               f"PORT={DB_PORT}",
               f"PASSWORD={ROOT_PASSWORD}",
               f"SERVICENAME={SERVICE_NAME}",
               "INSTALLSERVICE=1",
               "MODIFYROOTPASSWORD=1",
               "ENABLENETWORKING=1"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()
            if not detail:
                detail = extract_msi_log_errors(msi_log)
            if r.returncode == 1603:
                detail = detail or "（详见 MSI 日志）"
                detail += " | 常见原因：安装目录已存在且有内容、旧服务未卸载、UAC 被拒绝"
            raise RuntimeError(f"MariaDB MSI 返回码 {r.returncode}：{detail}")
        self.root.after(0, lambda: self.progress_var.set(70))

        # 兜底自动创建目录（MSI 完成后仍缺目录时补齐，含回退路径）
        try:
            ensure_dirs()
            self.log(f"✓ 目录就绪：{DB_HOME} / {DATA_DIR}")
        except Exception as e:
            self.log(f"⚠ 目录兜底创建异常：{e}")

        client_exe = find_client_exe()
        if not client_exe:
            raise RuntimeError("MariaDB 安装后未找到 mysql.exe 客户端")
        ver = get_version(client_exe)
        self.log(f"✓ MariaDB 安装成功：{ver or '未知版本'}")
        self.log(f"  安装目录：{client_exe.parent.parent}")
        self.log(f"  数据目录：{DATA_DIR}")

        self.root.after(0, lambda: self.progress_var.set(80))

        # 服务处理
        self.handle_service()

        self.root.after(0, lambda: self.progress_var.set(85))

        # 强制 bind-address=0.0.0.0，允许任意地址连接
        self.update_status("配置 bind-address=0.0.0.0 ...")
        my_ini = find_my_ini(client_exe.parent.parent)
        if not my_ini:
            self.log("⚠ 未找到 my.ini，无法修改 bind-address")
        else:
            try:
                if set_bind_address(my_ini):
                    self.log(f"✓ 已更新 {my_ini} 中的 bind-address={BIND_ADDRESS}")
                    self.update_status("重启服务使 bind-address 生效 ...")
                    if restart_service():
                        self.log(f"✓ 服务重启成功，端口 {DB_PORT} 已监听 {BIND_ADDRESS}")
                    else:
                        raise RuntimeError("服务重启失败，bind-address 未生效")
                else:
                    self.log(f"✓ bind-address 已是 {BIND_ADDRESS}，无需修改")
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(f"配置 bind-address 失败：{e}")

        self.root.after(0, lambda: self.progress_var.set(90))

        # 创建管理员账号
        self.update_status(f"创建管理员账号 {ADMIN_USER} ...")
        try:
            create_admin_account(client_exe)
            self.log(f"✓ 管理员账号已创建：{ADMIN_USER}@'%' / {ADMIN_USER}@'localhost' "
                     "（全部权限，允许远程连接）")
        except Exception as e:
            raise RuntimeError(f"创建管理员账号失败：{e}")

    def handle_service(self):
        try:
            if service_is_running():
                if port_open(DB_PORT):
                    self.log(f"✓ 服务 {SERVICE_NAME} 正在运行，端口 {DB_PORT} 可连接")
                else:
                    self.log("服务运行中，等待端口就绪 ...")
                    if wait_for_service():
                        self.log(f"✓ 端口 {DB_PORT} 就绪")
                    else:
                        self.log(f"⚠ 服务运行中但端口 {DB_PORT} 未就绪")
                return
            if service_exists():
                self.log("\n服务已注册但未运行，正在启动 ...")
                r = subprocess.run(["net", "start", SERVICE_NAME], capture_output=True,
                                   text=True, timeout=180,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = (r.stdout or r.stderr or "").strip().splitlines()
                if out:
                    self.log(f"  {out[-1]}")
            else:
                self.log("\n未检测到 Windows 服务，静默安装应已注册 ...")
                return
            if wait_for_service():
                self.log(f"✓ 服务 {SERVICE_NAME} 启动成功，端口 {DB_PORT} 可连接")
            else:
                self.log(f"⚠ 服务未确认运行，可手动执行 net start {SERVICE_NAME}")
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
