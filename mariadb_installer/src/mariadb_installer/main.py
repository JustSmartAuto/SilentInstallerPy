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
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

WEB_PORT_BASE = 18306  # 日志网页起始端口（冲突自动 +1 尝试）
WEB_LOG_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>MariaDB 一键安装器 · 详细日志</title>
<style>
  body { margin:0; background:#111; color:#e8e8e8;
         font-family: Consolas, "Microsoft YaHei UI", monospace; }
  header { padding:10px 16px; background:#00365B; color:#fff;
           font-family:"Microsoft YaHei UI"; position:sticky; top:0; }
  header .sub { font-size:12px; color:#9fd0ff; }
  #ctrl { padding:6px 16px; font-size:12px; font-family:"Microsoft YaHei UI"; color:#bbb;}
  #log { padding:8px 16px; font-size:13px; line-height:1.5; white-space:pre-wrap;
         word-break:break-all; }
  #log .t { color:#7ec8ff; }
  #log .err { color:#ff7a7a; }
  #log .ok { color:#7be07b; }
</style>
</head>
<body>
<header><b>MariaDB 一键安装器 · 详细日志</b>
<div class="sub">每秒自动追加刷新，本页只读 —— url：__URL__</div></header>
<div id="ctrl"><label><input type="checkbox" id="auto" checked> 自动滚动到底部</label></div>
<div id="log"></div>
<script>
let after = 0;
const logEl = document.getElementById('log');
const auto = document.getElementById('auto');
function cls(t){ if(t.includes('✗')||t.includes('失败')||t.includes('错误')) return 'err';
                 if(t.includes('✓')||t.includes('成功')) return 'ok'; return ''; }
function esc(s){ const d=document.createElement('div'); d.textContent=s; return d; }
async function poll(){
  try {
    const r = await fetch('/logs?after=' + after);
    if (r.ok) {
      const arr = await r.json();
      for (const e of arr) {
        const div = document.createElement('div');
        div.className = cls(e.text);
        div.appendChild(esc(e.text));
        logEl.appendChild(div);
        after = e.seq;
      }
      if (arr.length && auto.checked) window.scrollTo(0, document.body.scrollHeight);
    }
  } catch (e) {}
  setTimeout(poll, 1000);
}
poll();
</script>
</body>
</html>
"""


class WebLogStore:
    """线程安全的日志存储（按序号递增，供网页按 seq 增量拉取）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []

    def append(self, text: str) -> int:
        with self._lock:
            seq = len(self._entries) + 1
            self._entries.append({"seq": seq, "text": text})
            return seq

    def after(self, seq: int) -> list:
        with self._lock:
            return [e for e in self._entries if e["seq"] > seq]


class LogRequestHandler(BaseHTTPRequestHandler):
    store: WebLogStore = None  # type: ignore[assignment]

    def do_GET(self):
        try:
            parts = urllib.parse.urlsplit(self.path)
            if parts.path == "/" or parts.path == "/index.html":
                body = WEB_LOG_PAGE.replace("__URL__", self.headers.get("Host", "")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parts.path == "/logs":
                qs = urllib.parse.parse_qs(parts.query)
                try:
                    after = int((qs.get("after") or ["0"])[0])
                except ValueError:
                    after = 0
                entries = self.store.after(after)
                body = json.dumps(entries, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            pass

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


def lan_ip() -> str:
    """取局域网 IP（UDP connect 法，不发数据）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.255.255", 65535))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def start_web_log_server(store: WebLogStore) -> tuple[str, int] | None:
    """在高端口启动日志网页服务（0.0.0.0，可远程查看）；失败返回 None。"""
    for port in range(WEB_PORT_BASE, WEB_PORT_BASE + 40):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), LogRequestHandler)
            httpd.daemon_threads = True
            LogRequestHandler.store = store
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            return lan_ip(), port
        except OSError:
            continue
    return None


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

SERVICE_START_TIMEOUT = 60.0
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
    """强制 my.ini 的 [mysqld] 段中 bind-address=BIND_ADDRESS（允许任意地址连接）。

    已是目标值返回 False；有修改返回 True（需重启服务生效）。
    """
    try:
        text = my_ini.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    lines = text.splitlines()
    out = []
    saw_mysqld = False
    insert_at = None  # bind-address 应插入的 out 索引
    for line in lines:
        s = line.strip()
        head = s.lower().replace(" ", "")
        if head == "[mysqld]":
            saw_mysqld = True
            out.append(line)
            continue
        if not s.startswith(("#", ";")) and s.lower().startswith("bind-address"):
            continue  # 丢弃任意段中的 bind-address（末段残留会让 mysql.exe 报 unknown variable）
        if saw_mysqld and head.startswith("[") and head != "[mysqld]" and insert_at is None:
            insert_at = len(out)  # 即将离开 [mysqld] 进入下一段，插在它前面
        out.append(line)
    if insert_at is None and saw_mysqld:
        insert_at = len(out)  # [mysqld] 是最后一段，插件末尾
    if not saw_mysqld:
        out = ["[mysqld]", f"bind-address={BIND_ADDRESS}"] + out
        insert_at = None
    else:
        out.insert(insert_at, f"bind-address={BIND_ADDRESS}")

    new_text = "\n".join(out) + "\n"
    if new_text == text:
        return False
    try:
        my_ini.write_text(new_text, encoding="utf-8")
    except PermissionError:
        raise RuntimeError(f"无权限写入 {my_ini}，请以管理员身份运行本安装器")
    return True


def start_service_unit() -> tuple[bool, str]:
    """单次启动服务并等待端口就绪；net start 返回「已启动」也视为成功。"""
    r = subprocess.run(["net", "start", SERVICE_NAME], capture_output=True, text=True,
                       timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or r.stderr or "")
    already_started = "2182" in out or "已经启动" in out or "already started" in out.lower()
    ok = r.returncode == 0 or already_started
    if not ok:
        tail = [l.strip() for l in out.strip().splitlines() if l.strip()]
        detail = tail[-1] if tail else f"returncode {r.returncode}"
        return False, detail
    return (True, "") if wait_for_service() else (False, "进程启动但端口未就绪")


def restart_service() -> bool:
    """重启 MariaDB 服务直至端口就绪；最多重试 3 次（首次启动含初始化可能很慢）。"""
    subprocess.run(["net", "stop", SERVICE_NAME], capture_output=True, text=True,
                   timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    last_detail = ""
    for attempt in range(3):
        if attempt:
            time.sleep(5)
        ok, detail = start_service_unit()
        if ok:
            return True
        last_detail = detail
    raise RuntimeError(f"服务启动未就绪：{last_detail}")


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


GHOST_ZIP_NAME = "mariadb_data_13.0.2.zip"


def ghost_zip_path() -> Path | None:
    """内嵌的现成数据目录包（thinkbook 健康实例，含 mysql 系统表/justsmart 账号）。"""
    p = bundle_dir() / GHOST_ZIP_NAME
    return p if p.exists() and p.stat().st_size > 1024 else None


def ghost_extract() -> int:
    """解包 ghost 数据目录到 DATA_DIR，返回解出的文件数（失败抛异常）。"""
    src = ghost_zip_path()
    if not src:
        return 0
    with zipfile.ZipFile(src) as zf:
        zf.extractall(DATA_DIR)
    return sum(1 for p in DATA_DIR.rglob("*") if p.is_file())


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
    """mysql --version 输出：mysql Ver 15.2 Distrib 13.0.2-MariaDB, for Win64 (AMD64)。"""
    try:
        r = subprocess.run([str(client_exe), "--version"], capture_output=True, text=True,
                           timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"MariaDB\s+([0-9.]+)", out)
        if not m:
            m = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)\s*-?MariaDB", out)
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


def all_local_ips() -> list:
    """列出本机全部 IPv4 地址（lan_ip 可能选错多网卡机器的网卡）。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       family=socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and ip != "127.0.0.1":
                ips.append(ip)
    except OSError:
        pass
    return ips


def port_open(port: int, host: str = "", timeout: float = 2.0) -> bool:
    """探测端口；host 为空时依次尝试 127.0.0.1 与本机全部 IPv4。

    兜底：全部连不上但 netstat 上"我们的 mysqld"正在 LISTEN，
    也算开着（loopback 被杀软/代理拦截的机器仍可远程访问）。
    """
    hosts = [host] if host else ["127.0.0.1"] + all_local_ips()
    for h in hosts:
        try:
            with socket.create_connection((h, port), timeout=timeout):
                return True
        except OSError:
            continue
    if host:
        return False
    listener = describe_port_listener(port)
    if listener and listener[2]:
        path = str(listener[2]).lower()
        if path.startswith(str(DB_HOME).lower() + "\\"):
            return True
    return False


def run_sql(client_exe: Path, user: str, password: str, sql: str,
            port: int = DB_PORT, host: str = "127.0.0.1",
            timeout: float | None = None) -> str:
    """用 mysql.exe 客户端执行 SQL，返回 stdout；失败抛出异常。"""
    cmd = [str(client_exe),
           "--host", host,
           "--port", str(port),
           "--user", user,
           f"--password={password}",  # 空格分隔会让 mysql.exe 挂死等待控制台输入
           "--connect-timeout", "15",
           "--protocol", "TCP",
           "--default-character-set", "utf8mb4",
           "-e", sql]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout or CLIENT_CMD_TIMEOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"SQL 执行超时（{timeout or CLIENT_CMD_TIMEOUT:.0f}s）：{sql}")
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


def kill_zombie_processes_home() -> int:
    """强杀可执行路径位于 DB_HOME 之下的进程（僵尸 mysqld 等），返回强杀数。

    仅按路径匹配，不影响系统里其他位置的 MySQL/MariaDB 实例。
    """
    if sys.platform != "win32" or not DB_HOME.exists():
        return 0
    pattern = str(DB_HOME).rstrip("\\") + "\\*"
    ps_script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "
        f"'{pattern}' }} | ForEach-Object {{ Write-Output "
        "($_.ProcessId.ToString() + '|' + $_.Name) }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script],
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return 0
    killed = 0
    for line in (r.stdout or "").splitlines():
        pid_s, _, name = line.strip().partition("|")
        if not pid_s.strip().isdigit():
            continue
        try:
            rk = subprocess.run(["taskkill", "/F", "/PID", pid_s.strip()],
                                capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if rk.returncode == 0:
                killed += 1
        except Exception:
            continue
    return killed


def home_processes_alive() -> list:
    """列出可执行路径位于 DB_HOME 之下的存活进程 [(pid, name)]，用于杀后验证。"""
    if sys.platform != "win32" or not DB_HOME.exists():
        return []
    pattern = str(DB_HOME).rstrip("\\") + "\\*"
    ps_script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "
        f"'{pattern}' }} | ForEach-Object {{ Write-Output "
        "($_.ProcessId.ToString() + '|' + $_.Name) }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script],
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    alives = []
    for line in (r.stdout or "").splitlines():
        pid_s, _, name = line.strip().partition("|")
        if pid_s.strip().isdigit():
            alives.append((int(pid_s), name))
    return alives


def dump_latest_err_log(limit: int = 30) -> list:
    """转储数据目录最新 *.err 末 limit 行（mysqld 启动失败的官方报错在错误日志里）。"""
    if not DATA_DIR.exists():
        return ["(数据目录不存在，无 *.err)"]
    err_files = sorted(DATA_DIR.glob("*.err"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not err_files:
        return ["(数据目录没有 *.err 文件)"]
    f = err_files[0]
    try:
        st = f.stat()
        head = [f"--- {f.name}（mtime {time.strftime('%H:%M:%S', time.localtime(st.st_mtime))}，"
                f"{st.st_size} 字节）末 {limit} 行 ---"]
        lines = (read_text_auto(f).splitlines() or [""])
        return head + lines[-limit:]
    except Exception as e:
        return [f"--- {f.name}（读取失败：{e}）"]


def probe_file_writable() -> list:
    """核对 ibdata1 等核心文件可写性（杀软/受控文件夹访问拦截时在此现形）。"""
    out = []
    for name in ("ibdata1", "aria_log_control", "aria_log.00000001"):
        p = DATA_DIR / name
        if not p.exists():
            out.append(f"  {name}：不存在")
            continue
        try:
            ro = bool(p.stat().st_file_attributes & 0x1)  # FILE_ATTRIBUTE_READONLY
        except OSError:
            ro = False
        try:
            with open(p, "r+b") as fh:
                fh.read(4)
            status = "OK"
        except OSError as e:
            status = f"失败（{e}）"
        out.append(f"  {name}：只读属性={'是' if ro else '否'}，写测试={status}")
    return out


def force_wipe_home() -> bool:
    """强杀占用进程后强制清空 DB_HOME 整个目录。成功（不存在/已清空）返回 True。"""
    if not DB_HOME.exists():
        return True
    killed = kill_zombie_processes_home()
    time.sleep(1)
    try:
        shutil.rmtree(DB_HOME, ignore_errors=False)
        return True
    except Exception:
        shutil.rmtree(DB_HOME, ignore_errors=True)
        return not DB_HOME.exists()


def describe_port_listener(port: int) -> tuple[str, str, str, str] | None:
    """找出监听指定端口的进程，返回 (pid, 进程名, 可执行路径, 所属服务名)。查不到返回 None。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    pid = None
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and \
                parts[3].upper().startswith("LISTEN"):
            pid = parts[4]
            break
    if not pid or not pid.isdigit():
        return None
    name = path = svc = ""
    ps_script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\";"
        "if ($p) { Write-Output (\"P|\" + $p.Name + \"|\" + $p.ExecutablePath); };"
        f"$s = Get-CimInstance Win32_Service -Filter \"ProcessId={pid}\" | "
        "Select-Object -First 1;"
        "if ($s) { Write-Output (\"S|\" + $s.Name); }"
    )
    try:
        rc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (rc.stdout or "").splitlines():
            if line.startswith("P|"):
                _, name, path = line.split("|", 2)
            elif line.startswith("S|"):
                svc = line.split("|", 1)[1].strip()
    except Exception:
        pass
    return pid, name, path, svc


def stop_pid(pid: str) -> str:
    """强制结束进程，返回空串（成功）或错误摘要。"""
    try:
        r = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "" if r.returncode == 0 else ((r.stderr or r.stdout or "").strip().splitlines()[-1])
    except Exception as e:
        return str(e)


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


def ensure_my_ini() -> Path:
    """确保 my.ini 存在（MSI 维护模式重装可能不生成实例配置）。

    找不到时在数据目录生成一份最小可用配置（datadir/port/bind-address）。
    """
    my = find_my_ini()
    if my:
        return my
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / "my.ini"
    body = (
        "[mysqld]\n"
        f"datadir={DATA_DIR.as_posix()}\n"
        f"port={DB_PORT}\n"
        f"bind-address={BIND_ADDRESS}\n"
        "skip-name-resolve\n"
        "\n[client]\n"
        f"port={DB_PORT}\n"
    )
    target.write_text(body, encoding="utf-8")
    return target


def system_tables_present() -> bool:
    """数据目录是否已初始化（存在 mysql 系统库且非空）。"""
    sysdb = DATA_DIR / "mysql"
    return sysdb.exists() and any(sysdb.iterdir())


def clear_data_dir_keep(my_ini: Path) -> int:
    """清空数据目录内容但保留 my.ini（仅当不存在 mysql 系统库时调用，此时无用户数据）。"""
    if not DATA_DIR.exists():
        return 0
    removed = 0
    for p in DATA_DIR.iterdir():
        if p == my_ini:
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=False)
            else:
                p.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def grant_service_account_dbhome() -> str:
    """授予 NT SERVICE\\MariaDB 对安装/数据目录的完全控制（全新 datadir 必需）。"""
    err = ""
    for target in (DB_HOME, DATA_DIR):
        try:
            r = subprocess.run(["icacls", str(target), "/grant",
                                f"NT SERVICE\\{SERVICE_NAME}:(OI)(CI)F", "/T", "/C", "/Q"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                tail = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1]
                err = err or tail
        except Exception as e:
            err = err or str(e)
    return err


def bootstrap_system_tables(install_dir: Path, my_ini: Path) -> list[str]:
    """用内置 mariadb-install-db.exe 引导系统表，返回输出行列表。

    Windows 版 mariadb-install-db 不支持 --defaults-file；
    先带 --password/--port，遇到未知选项再回退最小参数。
    """
    dbi = install_dir / "bin" / "mariadb-install-db.exe"
    if not dbi.exists():
        return ["(mariadb-install-db.exe 不存在)"]
    attempts = [
        [str(dbi), f"--datadir={DATA_DIR}", f"--port={DB_PORT}",
         f"--password={ROOT_PASSWORD}"],
        [str(dbi), f"--datadir={DATA_DIR}"],
    ]
    out = subprocess_output = None
    last_out = []
    for cmd in attempts:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=900,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            last_out = [f"(执行 mariadb-install-db 异常：{e})"]
            continue
        last_out = []
        for src in ((r.stdout or "").splitlines(), (r.stderr or "").splitlines()):
            for l in src:
                l = l.strip()
                if l and l not in last_out:
                    last_out.append(l)
        if r.returncode == 0:
            return last_out
        joined = "\n".join(last_out).lower()
        if "unknown" not in joined and "unrecognized" not in joined:
            break
    return last_out


def create_admin_account(client_exe: Path, log_fn=None) -> str:
    """创建管理员账号 justsmart，授予全部权限并可从任意主机连接。

    root 密码先试 ROOT_PASSWORD，失败回退空密码（install-db 引导默认），
    成功后统一把 root@localhost 密码设为 ROOT_PASSWORD。
    连接宿主依序尝试（loopback 被拦的机器换 localhost/本机 IP 再试）。
    全部失败则启用 init-file 兜底：把建号 SQL 交给 mysqld 启动时特权执行。
    返回实际使用的 root 密码。
    """
    log = log_fn or (lambda *a, **k: None)

    def msg(text):
        try:
            log(text)
        except Exception:
            pass

    esc = ADMIN_PASSWORD.replace("'", "''")
    root_esc = ROOT_PASSWORD.replace("'", "''")
    sql = (
        f"CREATE USER IF NOT EXISTS '{ADMIN_USER}'@'%' IDENTIFIED BY '{esc}';"
        f"ALTER USER '{ADMIN_USER}'@'%' IDENTIFIED BY '{esc}';"
        f"CREATE USER IF NOT EXISTS '{ADMIN_USER}'@'localhost' IDENTIFIED BY '{esc}';"
        f"ALTER USER '{ADMIN_USER}'@'localhost' IDENTIFIED BY '{esc}';"
        f"GRANT ALL PRIVILEGES ON *.* TO '{ADMIN_USER}'@'%' WITH GRANT OPTION;"
        f"GRANT ALL PRIVILEGES ON *.* TO '{ADMIN_USER}'@'localhost' WITH GRANT OPTION;"
        "FLUSH PRIVILEGES;"
    )
    hosts = ["127.0.0.1", "localhost"] + all_local_ips()
    last_err = None
    used_pw = None
    for pw in (ROOT_PASSWORD, "", "root"):
        for host in hosts:
            try:
                run_sql(client_exe, "root", pw, sql, host=host)
                used_pw = pw
                msg(f"    ✓ root 连接成功：host={host} "
                    f"密码={'justsmart' if pw == ROOT_PASSWORD else ('<空>' if pw == '' else pw)}")
                break
            except RuntimeError as e:
                last_err = e
        if used_pw is not None:
            break
    if used_pw is None:
        msg("    ⚠ 本地 mysql.exe 将无法连接，改走 init-file 兜底（由 mysqld 特权执行建号 SQL）...")
        _bootstrap_accounts_via_init_file(client_exe, log_fn, sql,
                                          f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{root_esc}';")
        try:
            run_sql(client_exe, ADMIN_USER, ADMIN_PASSWORD, "SELECT 1;", host="127.0.0.1")
        except RuntimeError:
            # 本地连不上不代表没建成，远端 '%' 账号交给最后的连接测试验证
            msg("    （本机客户端仍不能回连，建号结果交给最终连接测试）")
        return ROOT_PASSWORD
    if used_pw != ROOT_PASSWORD:
        for host in hosts:
            try:
                run_sql(client_exe, "root", used_pw,
                        f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{root_esc}';",
                        host=host)
                break
            except RuntimeError:
                continue  # root 密码设置失败不阻塞安装
    return used_pw


def _bootstrap_accounts_via_init_file(client_exe: Path, log_fn, admin_sql: str,
                                      root_sql: str):
    """init-file 兜底：把建号 SQL 写成文件并挂到 my.ini 的 init-file，重启服务执行。

    mysqld 启动时以 root@localhost 身份执行 init-file 中的脚本，不依赖客户端连接。
    执行成功后立即撤销 init-file 配置并删除 SQL 文件。
    """
    log = log_fn or (lambda *a, **k: None)

    def msg(text):
        try:
            log(text)
        except Exception:
            pass

    sql_path = DATA_DIR / "bootstrap_accounts.sql"
    my = find_my_ini()
    if not my:
        msg("    ✗ init-file 兜底失败：找不到 my.ini")
        return
    sql_path.write_text(admin_sql + root_sql, encoding="utf-8")
    # 幂等：摘掉旧的 init-file 行
    text = read_text_auto(my)
    lines = [l for l in text.splitlines() if "init-file" not in l.lower()]
    marker_idx = next((i for i, l in enumerate(lines)
                       if l.strip().lower() == "[mysqld]"), None)
    ini_line = f"init-file={sql_path}"
    lines.insert(marker_idx + 1 if marker_idx is not None else 0,
                 ini_line if marker_idx is not None else "[mysqld]")
    my.write_text("\n".join(lines) + "\n", encoding="utf-8")
    msg(f"    init-file 已挂载：{ini_line}（重启服务执行）")
    if not restart_service():
        msg("    ✗ init-file 兜底失败：服务重启未就绪")
        return
    # 撤销 init-file 并删掉 SQL 文件
    lines = [l for l in lines if l != ini_line
             and not l.strip().lower().startswith("init-file=")]
    if marker_idx is None:
        lines = [l for l in lines if l.strip().lower() != "[mysqld]"]
    my.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        sql_path.unlink()
    except OSError:
        pass
    msg("    ✓ init-file 兜底完成，配置已撤除")


def db_connection_test(client_exe: Path, user: str = ADMIN_USER,
                       password: str = ADMIN_PASSWORD) -> str:
    """以指定账号依次尝试本机多个宿主执行 SQL 测试，返回摘要（失败抛异常）。"""
    last = None
    out = ""
    for host in ["127.0.0.1", "localhost"] + all_local_ips():
        try:
            out = run_sql(client_exe, user, password,
                          "SELECT VERSION() AS version, CURRENT_USER() AS user, "
                          "@@port AS port, @@bind_address AS bind;", host=host)
            break
        except RuntimeError as e:
            last = e
    else:
        raise last or RuntimeError("数据库连接测试失败：无可用连接宿主")
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

        # 详细日志网页服务（高端口，可从其他机器远程查看）
        self.web_log = WebLogStore()
        self.web_addr = start_web_log_server(self.web_log)
        self._web_browser = None

        try:
            icon = bundle_dir() / "app_icon.ico"
            if icon.exists():
                self.root.iconbitmap(str(icon))
        except Exception:
            pass

        self._build_ui()
        self.refresh_status()
        self._announce_web_log()

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
        self.web_label = tk.Label(header, text="详细日志网页：启动中...",
                                  font=("Microsoft YaHei UI", 9, "bold"),
                                  fg="#7ec8ff", bg="#00365B", cursor="hand2")
        self.web_label.place(x=20, y=68)
        self.web_label.bind("<Button-1>", lambda _e: self._open_web_url())

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

        client_exe = find_client_exe()
        already_installed = bool(self.db_installed and client_exe and service_exists())
        if already_installed:
            self.log("已安装 MariaDB，跳过重复安装，执行服务/配置/账号自检 ...")
        else:
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
                        self.log("目录仍非空（可能被僵尸进程占用），强制结束进程并清空 ...")
                        if force_wipe_home():
                            self.log(f"✓ 已强制清空 {DB_HOME}")
                        else:
                            raise RuntimeError(
                                f"{DB_HOME} 中仍有文件被占用无法删除，请重启机器后重新安装")
                    if n:
                        self.log(f"✓ 已清理 {n} 个空目录残留")
                    self.log("开始重新安装")
                else:
                    self.log(f"⚠ 旧包卸载返回码 {r_un.returncode}（未安装则可忽略），继续安装 ...")

            # 安装前检查：MSI 拒绝安装进已存在的非空 INSTALLDIR；
            # 若仅剩空目录残留（无任何文件）则自动清理；有文件则强杀占用后强制清空
            if not self.db_existing_client and DB_HOME.exists() and any(DB_HOME.iterdir()):
                remove_empty_tree(DB_HOME)
                if DB_HOME.exists() and any(DB_HOME.iterdir()):
                    self.log(f"安装目录 {DB_HOME} 非空（可能被僵尸进程占用），强制清空 ...")
                    if force_wipe_home():
                        self.log(f"✓ 已强制清空 {DB_HOME}")
                    else:
                        raise RuntimeError(
                            f"{DB_HOME} 中仍有文件被占用无法删除，请重启机器后重新安装")

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

            # 兜底自动创建目录（MSI 完成后仍缺目录时补齐，含回退路径）
            try:
                ensure_dirs()
                self.log(f"✓ 目录就绪：{DB_HOME} / {DATA_DIR}")
            except Exception as e:
                self.log(f"⚠ 目录兜底创建异常：{e}")

            client_exe = find_client_exe()
            if not client_exe:
                raise RuntimeError("MariaDB 安装后未找到 mysql.exe 客户端")

        self.root.after(0, lambda: self.progress_var.set(70))

        ver = get_version(client_exe)
        if already_installed:
            self.log(f"✓ MariaDB 已就绪：{ver or '未知版本'}")
        else:
            self.log(f"✓ MariaDB 安装成功：{ver or '未知版本'}")
        self.log(f"  安装目录：{client_exe.parent.parent}")
        self.log(f"  数据目录：{DATA_DIR}")

        self.root.after(0, lambda: self.progress_var.set(80))

        # 实例配置自愈：MSI 维护模式重装可能不生成 my.ini / 不初始化数据目录
        self.update_status("检查实例配置 my.ini 与数据目录 ...")
        install_dir = client_exe.parent.parent
        my_ini = ensure_my_ini()
        self.log(f"  my.ini：{my_ini}")
        if not system_tables_present():
            # install-db/解包都要求 datadir 完全为空：my.ini 先挪出数据目录暂存，
            # 完成后再放回（服务的 --defaults-file 指向数据目录内）
            self.log("  数据目录未初始化，清理残留后重建系统表 ...")
            stash = DB_HOME / "my.ini.stash"
            try:
                my_ini.replace(stash)
            except OSError:
                stash = None
            n = clear_data_dir_keep(my_ini)
            if not DATA_DIR.exists():
                DATA_DIR.mkdir(parents=True, exist_ok=True)
            if n:
                self.log(f"    已清理 {n} 个未完成的初始化残留（无 mysql 系统库，无用户数据）")
            ghost = ghost_zip_path()
            if ghost:
                self.log(f"    优先解包内嵌现成数据目录 {GHOST_ZIP_NAME}"
                         f"（含 mysql 系统表与 {ADMIN_USER} 账号，来自健康实例）...")
                try:
                    n_files = ghost_extract()
                    self.log(f"    ✓ 解包 {n_files} 个文件完成")
                except Exception as e:
                    self.log(f"    ⚠ ghost 解包失败（{e}），回退 mariadb-install-db ...")
            if not system_tables_present():
                self.log("    用 mariadb-install-db 引导系统表（可能需要 1~2 分钟）...")
                lines = bootstrap_system_tables(install_dir, my_ini)
                for line in lines[-8:]:
                    if line:
                        self.log(f"    {line}")
            if stash and stash.exists():
                try:
                    shutil.copy2(stash, DATA_DIR / "my.ini")
                    stash.unlink()
                    self.log("    已把 my.ini 放回数据目录")
                except Exception as e:
                    self.log(f"    ⚠ my.ini 放回失败：{e}")
            if system_tables_present():
                self.log("  ✓ 系统表初始化完成")
                err = grant_service_account_dbhome()
                if err:
                    self.log(f"  ⚠ 授予 NT SERVICE\\{SERVICE_NAME} 目录权限失败：{err}")
                else:
                    self.log(f"  ✓ 已授予服务账号 NT SERVICE\\{SERVICE_NAME} 目录权限")
            else:
                self.log("  ⚠ 系统表可能未完整初始化，继续尝试启动")
        else:
            self.log("  ✓ 数据目录已初始化")

        # 服务处理（停止状态将启动；运行状态下直接验证登录，端口探测只作兜底）
        self.handle_service(client_exe)
        if not (self.verify_login_brief(client_exe) or port_open(DB_PORT)):
            # 先尝试终极自愈（重建数据目录再引导）
            if not self.reset_data_and_bootstrap(client_exe):
                self.update_status("前台启动 mysqld，捕获启动报错 ...")
                self.run_mysqld_console_diag()
            time.sleep(2)
            subprocess.run(["net", "start", SERVICE_NAME], capture_output=True,
                           text=True, timeout=180,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if not (self.verify_login_brief(client_exe) or port_open(DB_PORT)):
                self.dump_failure_diagnostics("服务未能启动")
                err_hint = (DATA_DIR / "*.err") if DATA_DIR.exists() else "<数据目录>/*.err"
                raise RuntimeError(
                    f"服务 {SERVICE_NAME} 未能启动且端口 {DB_PORT} 不可连接，无法继续配置；"
                    f"请检查数据目录错误日志（{err_hint}），"
                    f"或在本机 CMD 执行 net start {SERVICE_NAME} 查看具体报错")

        self.root.after(0, lambda: self.progress_var.set(85))

        # 强制 bind-address=0.0.0.0，允许任意地址连接
        self.update_status("配置 bind-address=0.0.0.0 ...")
        if set_bind_address(my_ini):
            self.log(f"✓ 已更新 {my_ini} 中的 bind-address={BIND_ADDRESS}")
            self.update_status("重启服务使 bind-address 生效 ...")
            if restart_service():
                self.log(f"✓ 服务重启成功，端口 {DB_PORT} 已监听 {BIND_ADDRESS}")
            else:
                raise RuntimeError("服务重启失败，bind-address 未生效")
        else:
            self.log(f"✓ bind-address 已是 {BIND_ADDRESS}，无需修改")

        self.root.after(0, lambda: self.progress_var.set(90))

        # 创建管理员账号（ghost 装载的账号通常已存在，此步幂等校验）
        if not (self.verify_login_brief(client_exe) or port_open(DB_PORT)):
            raise RuntimeError(
                f"数据不可登录且端口 {DB_PORT} 不可连接，跳过建号；请重跑安装器触发终极自愈")
        self.update_status(f"创建管理员账号 {ADMIN_USER} ...")
        try:
            used_pw = create_admin_account(client_exe, self.log)
            pw_note = f"（root 初始密码为空，已自动补设为 {ROOT_PASSWORD}）" if used_pw != ROOT_PASSWORD else ""
            self.log(f"✓ 管理员账号已创建：{ADMIN_USER}@'%' / {ADMIN_USER}@'localhost' "
                     f"（全部权限，允许远程连接）{pw_note}")
        except Exception as e:
            self.dump_failure_diagnostics("创建管理员账号失败")
            raise RuntimeError(f"创建管理员账号失败：{e}")

    def run_mysqld_console_diag(self):
        """前台启动 mysqld（--console）抓取启动报错并写日志，45s 后强制结束。"""
        mysqld = DB_HOME / "bin" / "mysqld.exe"
        if not mysqld.exists():
            self.log("  （未找到 mysqld.exe，跳过前台诊断）")
            return
        self.log("  前台启动 mysqld（--console）抓取报错 ...")
        out_lines: list[str] = []
        try:
            p = subprocess.Popen(
                [str(mysqld), f"--defaults-file={DATA_DIR / 'my.ini'}", "--console"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            self.log(f"  ⚠ 前台启动 mysqld 失败：{e}")
            return

        def _reader():
            try:
                for line in p.stdout:
                    out_lines.append(line.rstrip())
            except Exception:
                pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=45)
        if t.is_alive():
            subprocess.run(["taskkill", "/F", "/PID", str(p.pid)],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out_lines.append("(前台 mysqld 启动超过 45s 未自己退出，已强制结束)")
        try:
            p.wait(timeout=15)
        except Exception:
            pass
        shown = [l for l in out_lines if l.strip()]
        if shown:
            self.log(f"  mysqld 前台启动输出（末 {min(len(shown), 20)} 行）：")
            for line in shown[-20:]:
                self.log(f"    {line.strip()}")
        else:
            self.log("  （mysqld 前台无任何输出即退出——建议检查 .err 日志）")

    def handle_service(self, client_exe=None):
        """服务处置：先启动/确认健康，随后若能直接登录数据库即判定成功。

        登录验证是最高判据（本机端口探测在部分机器上被杀软/代理拦截失真）；
        只有登录、端口双重不合时才进入杀进程/重构等激进手段。
        """
        try:
            if service_is_running():
                if client_exe and self.verify_login_brief(client_exe):
                    self.log(f"✓ 服务 {SERVICE_NAME} 正在运行，数据库可登录")
                    return
                if port_open(DB_PORT):
                    self.log(f"✓ 服务 {SERVICE_NAME} 正在运行，端口 {DB_PORT} 可连接")
                    return
                self.log("服务运行中，等待端口就绪 ...")
                if wait_for_service():
                    self.log(f"✓ 端口 {DB_PORT} 就绪")
                    return
                if client_exe and self.verify_login_brief(client_exe):
                    self.log("✓ 端口探测失真但数据库可登录，按健康处理")
                    return
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
            if client_exe and self.verify_login_brief(client_exe):
                self.log(f"✓ 服务 {SERVICE_NAME} 启动成功，数据库可登录")
                return
            if wait_for_service():
                self.log(f"✓ 服务 {SERVICE_NAME} 启动成功，端口 {DB_PORT} 可连接")
                return
            # 端口打不开：先把 SCM 僵尸状态清干净（net stop 忽略 1062），再清本安装目录下的僵尸进程后重试
            subprocess.run(["net", "stop", SERVICE_NAME], capture_output=True,
                           text=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            n_kill = kill_zombie_processes_home()
            alives = home_processes_alive()
            if n_kill or alives:
                self.log(f"  杀掉 {n_kill} 个本目录进程；"
                         + (f"⚠ 仍有 {len(alives)} 个杀不掉存活："
                            + ", ".join(f"pid={p} {n}" for p, n in alives) if alives
                            else "已验证死透"))
            if n_kill:
                self.log(f"  强制结束了 {n_kill} 个本目录占用进程，重试启动服务 ...")
                time.sleep(2)
                subprocess.run(["net", "start", SERVICE_NAME], capture_output=True,
                               text=True, timeout=180,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if wait_for_service():
                    self.log(f"✓ 服务 {SERVICE_NAME} 启动成功，端口 {DB_PORT} 可连接")
                    return
            # 仍打不开 → 端口被外来进程占用，报出占用者并强制结束
            listener = describe_port_listener(DB_PORT)
            if listener:
                pid, name, path, svc = listener
                self.log(f"⚠ 端口 {DB_PORT} 被外来进程占用：pid={pid} {name} "
                         f"path={path or '未知'} service={svc or '无'}")
                self.log(f"  为使本安装器的 MariaDB 使用端口 {DB_PORT}，强制结束该进程 ...")
                err = stop_pid(pid)
                if err:
                    self.log(f"  ⚠ 强制结束失败：{err}")
                else:
                    self.log("  ✓ 已结束占用进程")
                    time.sleep(2)
                    subprocess.run(["net", "start", SERVICE_NAME], capture_output=True,
                                   text=True, timeout=180,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if wait_for_service():
                        self.log(f"✓ 服务 {SERVICE_NAME} 启动成功，端口 {DB_PORT} 可连接")
                        return
            self.log(f"⚠ 服务未确认运行，可手动执行 net start {SERVICE_NAME}")
        except Exception as e:
            self.log(f"⚠ 服务处理异常：{e}")

    def verify_login_brief(self, client_exe) -> bool:
        """直接用数据库登录验证（多宿主原始方式，每个宿主最多等 5s）。

        本机端口探测在部分机器上被杀软/代理拦截而失真，
        以"MySQL 客户端能否登录"作为服务健康的最高判据。
        成功返回 True；失败返回 False（不抛异常）。
        """
        for host in ["127.0.0.1", "localhost"] + all_local_ips():
            try:
                run_sql(client_exe, ADMIN_USER, ADMIN_PASSWORD,
                        "SELECT 1;", host=host, timeout=5.0)
                self.log(f"  ✓ 数据库登录验证通过：host={host} "
                         f"账号={ADMIN_USER} 端口={DB_PORT}")
                return True
            except RuntimeError as e:
                self.log(f"    宿主 {host} 登录失败：{str(e).splitlines()[-1][:100]}")
            except Exception as e:
                self.log(f"    宿主 {host} 登录失败：{e}")
        return False

    def dump_failure_diagnostics(self, stage: str):
        try:
            self.log(f"\n【{stage}】失败诊断 ...")
            alives = home_processes_alive()
            if alives:
                self.log("  存活进程（本安装目录下）：")
                for pid, name in alives:
                    self.log(f"    pid={pid} {name}")
            else:
                self.log("  存活进程（本安装目录下）：无")
            for line in dump_latest_err_log():
                self.log(f"  {line}")
            self.log("  核心文件可写性（杀软拦截在此现形）：")
            for line in probe_file_writable():
                self.log(line)
        except Exception as e:
            self.log(f"  ⚠ 诊断过程异常：{e}")

    def reset_data_and_bootstrap(self, client_exe) -> bool:
        """终极自愈：停服/杀僵尸 + 重建数据目录 + 引导系统表 + 授权 + 起服务。"""
        try:
            self.log("\n  终极自愈：重建数据目录并重新引导系统表 ...")
            subprocess.run(["net", "stop", SERVICE_NAME], capture_output=True,
                           text=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            kill_zombie_processes_home()
            listener = describe_port_listener(DB_PORT)
            if listener:
                err = stop_pid(listener[0])
                self.log(f"    已结束端口占用进程 pid={listener[0]}"
                         + (f"（{err}）" if err else ""))
                time.sleep(1)
            install_dir = client_exe.parent.parent
            my_ini = DATA_DIR / "my.ini"
            if not my_ini.exists():
                my_ini = ensure_my_ini()
            stash = DB_HOME / "my.ini.stash"
            try:
                my_ini.replace(stash)
            except OSError:
                stash = None
            self.log(f"    清空数据目录 {DATA_DIR}（引导失败状态下无用户数据）...")
            clear_data_dir_keep(my_ini)
            if not DATA_DIR.exists():
                DATA_DIR.mkdir(parents=True, exist_ok=True)
            ghost = ghost_zip_path()
            if ghost:
                self.log(f"    优先解包内嵌现成数据目录 {GHOST_ZIP_NAME} ...")
                try:
                    n_files = ghost_extract()
                    self.log(f"    ✓ 解包 {n_files} 个文件完成")
                except Exception as e:
                    self.log(f"    ⚠ ghost 解包失败（{e}），回退 mariadb-install-db ...")
            if not system_tables_present():
                lines = bootstrap_system_tables(install_dir, my_ini)
                for line in lines[-5:]:
                    if line:
                        self.log(f"    {line}")
            if stash and stash.exists():
                try:
                    shutil.copy2(stash, DATA_DIR / "my.ini")
                    stash.unlink()
                except Exception:
                    pass
            if not system_tables_present():
                self.log("    ⚠ 重建引导仍未成功")
                return False
            self.log("    ✓ 系统表重建完成")
            err = grant_service_account_dbhome()
            if err:
                self.log(f"    ⚠ 授予服务账号目录权限失败：{err}")
            subprocess.run(["net", "start", SERVICE_NAME], capture_output=True,
                           text=True, timeout=180,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if wait_for_service():
                self.log(f"    ✓ 重建后服务启动成功，端口 {DB_PORT} 可连接")
                return True
            self.log("    ✗ 重建后服务仍未能启动")
            self.dump_failure_diagnostics("重建后服务仍未能启动")
            return False
        except Exception as e:
            self.log(f"    ⚠ 终极自愈异常：{e}")
            return False

    # ---------- 工具 ----------
    def update_status(self, msg: str):
        self.root.after(0, lambda: self.status_label.config(text=msg))
        self.log(msg)

    def _announce_web_log(self):
        if self.web_addr:
            ip, port = self.web_addr
            self.web_label.config(text=f"详细日志网页：http://127.0.0.1:{port}  "
                                       f"（局域网 http://{ip}:{port}）")
            self._web_browser = f"http://127.0.0.1:{port}"
            self.log(f"详细日志网页已开启：http://127.0.0.1:{port}（同网段可访问 "
                     f"http://{ip}:{port}，每秒追加刷新）")
        else:
            self.web_label.config(text="详细日志网页：端口占用失败（仅界面日志）")
            self._web_browser = None

    def _open_web_url(self):
        if not self._web_browser:
            return
        try:
            subprocess.Popen(["cmd", "/c", "start", "", self._web_browser],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            self.log(f"⚠ 打开网页失败：{e}")

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        text = f"[{ts}] {msg}"
        self.root.after(0, lambda: self._append(text + "\n"))
        if self.web_log:
            self.web_log.append(text)

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
