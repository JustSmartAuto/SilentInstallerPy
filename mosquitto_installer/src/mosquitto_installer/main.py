# -*- coding: utf-8 -*-
"""Mosquitto MQTT Broker 一键离线安装器（tkinter 版）

参考 git_rsync_installer 结构：
- 内嵌 mosquitto-2.1.2-install-windows-x64.exe（NSIS 官方安装包，/S 静默安装，
  自带 UAC 提权）
- 安装完成后用内嵌 mosquitto.conf 覆盖安装目录中的默认配置
- 若已注册 Windows 服务则重启服务使新配置生效
- 安装完成后通过内嵌 paho-mqtt 库进行 /hello 主题消息收发回环测试
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import paho.mqtt.client as mqtt

if sys.platform == "win32":
    import winreg

APP_NAME = "Mosquitto"
CURRENT_VERSION = "1.0.0"
MOSQUITTO_VERSION = "2.1.2"
SERVICE_NAME = "mosquitto"

CONF_FILE = "mosquitto.conf"

TEST_TOPIC = "/hello"
TEST_PORT_DEFAULT = 8907
TEST_TIMEOUT = 10.0


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


def find_mosquitto_exe() -> Path | None:
    """按 PATH → 注册表 → 默认安装路径 顺序查找 mosquitto.exe。"""
    which = shutil.which("mosquitto")
    if which:
        return Path(which)
    install_dir = find_install_dir()
    if install_dir and (install_dir / "mosquitto.exe").exists():
        return install_dir / "mosquitto.exe"
    for base in [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "mosquitto",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "mosquitto",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "mosquitto",
    ]:
        if (base / "mosquitto.exe").exists():
            return base / "mosquitto.exe"
    return None


def find_install_dir() -> Path | None:
    """从卸载注册表项读取安装目录（枚举 DisplayName 含 Mosquitto 的条目）。"""
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
                if "mosquitto" not in subkey.lower():
                    continue
                try:
                    with winreg.OpenKey(hkey, subkey) as sk:
                        name, _ = winreg.QueryValueEx(sk, "DisplayName")
                    if not str(name).lower().startswith("eclipse mosquitto"):
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


def get_version(exe: Path) -> str:
    """mosquitto -h 输出首行即版本：mosquitto version x.y.z ..."""
    try:
        r = subprocess.run([str(exe), "-h"], capture_output=True, text=True,
                           timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"mosquitto version ([\d.]+)", out)
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


def parse_listener_port(conf_path: Path, default: int = TEST_PORT_DEFAULT) -> int:
    """从 mosquitto.conf 解析第一个未注释的 listener 端口。"""
    try:
        for line in conf_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split()
            if parts[0] == "listener" and len(parts) >= 2:
                try:
                    p = int(parts[1])
                    if 0 < p < 65536:
                        return p
                except ValueError:
                    continue
    except Exception:
        pass
    return default


def _make_mqtt_client(cid: str):
    """兼容 paho-mqtt 2.x（必须显式 CallbackAPIVersion）与 1.x。"""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid,
                           protocol=mqtt.MQTTv311)
    return mqtt.Client(client_id=cid, protocol=mqtt.MQTTv311)


def mqtt_loopback_test(port: int = TEST_PORT_DEFAULT,
                       topic: str = TEST_TOPIC,
                       host: str = "127.0.0.1",
                       timeout: float = TEST_TIMEOUT) -> str:
    """/hello 主题收发回环测试：订阅端等待，发布端发送，返回收到的摘要。

    成功返回 "topic=... payload=... 延迟=..ms"，失败抛出异常。
    """
    recv_evt = threading.Event()
    sub_connected = threading.Event()
    received = {}

    payload_out = f"hello-{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"

    def on_connect_sub(client, userdata, flags, reason_code, properties=None):
        is_failure = getattr(reason_code, "is_failure", bool(reason_code))
        if not is_failure:
            userdata["connected"].set()

    def on_message_sub(client, userdata, msg):
        received[msg.topic] = msg.payload.decode("utf-8", "replace")
        userdata["recv"].set()

    udata = {"connected": sub_connected, "recv": recv_evt}
    sub = _make_mqtt_client(f"sub-{uuid.uuid4().hex[:8]}")
    pub = _make_mqtt_client(f"pub-{uuid.uuid4().hex[:8]}")
    sub.on_connect = on_connect_sub
    sub.on_message = on_message_sub

    t0 = time.monotonic()
    try:
        try:
            sub.connect(host, port, keepalive=10)
        except OSError as e:
            raise RuntimeError(f"无法连接 {host}:{port}（{e.__class__.__name__}：{e}）"
                               "，请确认 Mosquitto 已启动")
        sub.user_data_set(udata)
        sub.loop_start()
        if not sub_connected.wait(timeout=timeout):
            raise RuntimeError(f"连接 {host}:{port} 超时（{timeout:.0f}s）")
        rc, _ = sub.subscribe(topic, qos=1)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"订阅 {topic} 失败（rc={rc}）")

        pub_code = pub.connect(host, port, keepalive=10)
        if pub_code != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"发布端连接失败（rc={pub_code}）")
        pub.loop_start()
        msg_info = pub.publish(topic, payload_out, qos=1)
        try:
            msg_info.wait_for_publish(timeout=timeout)
        except (ValueError, RuntimeError) as e:
            raise RuntimeError(f"发布超时：{e}")

        if not recv_evt.wait(timeout=timeout):
            raise RuntimeError(f"订阅端 {timeout:.0f}s 内未收到主题 {topic} 的消息")
        got_topic = next(iter(received))
        got_payload = received[got_topic]
        if got_topic != topic:
            raise RuntimeError(f"收到意外主题：{got_topic}")
        if got_payload != payload_out:
            raise RuntimeError(f"收到消息内容不符：{got_payload!r}")
        return (f"topic={got_topic}  payload={got_payload}  "
                f"{(time.monotonic() - t0) * 1000:.0f}ms")
    finally:
        try:
            pub.disconnect()
            sub.disconnect()
        except Exception:
            pass
        try:
            pub.loop_stop()
            sub.loop_stop()
        except Exception:
            pass


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Mosquitto MQTT 一键安装器")
        self.root.geometry("640x480")
        self.root.resizable(False, False)
        self.root.configure(bg="white")

        self.is_installing = False
        self.is_busy = False
        self.temp_dir = Path(tempfile.gettempdir()) / "mosquitto-installer"

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
        header = tk.Frame(self.root, bg="#3E3520", height=90)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="Mosquitto MQTT 一键安装器", font=("Microsoft YaHei UI", 15, "bold"),
                 fg="white", bg="#3E3520").place(x=20, y=16)
        tk.Label(header, text=f"安装器 v{CURRENT_VERSION}  |  内嵌 Mosquitto {MOSQUITTO_VERSION} (NSIS)",
                 font=("Microsoft YaHei UI", 9), fg="lightgray", bg="#3E3520").place(x=20, y=50)

        # 组件状态区
        comp = tk.LabelFrame(self.root, text=" 组件检测（√ = 已安装，可选中后重装）",
                             font=("Microsoft YaHei UI", 9), bg="white", fg="#333")
        comp.place(x=20, y=105, width=600, height=110)

        self.mq_var = tk.BooleanVar(value=False)

        self.mq_cb = tk.Checkbutton(comp, text="Mosquitto MQTT Broker", variable=self.mq_var,
                                    font=("Microsoft YaHei UI", 10, "bold"), bg="white",
                                    command=lambda: self._on_toggle("mq"))
        self.mq_cb.place(x=20, y=12)
        self.mq_status = tk.Label(comp, text="检测中...", font=("Microsoft YaHei UI", 8),
                                  fg="gray", bg="white")
        self.mq_status.place(x=40, y=42)

        self.svc_status = tk.Label(comp, text="服务：检测中...", font=("Microsoft YaHei UI", 8),
                                   fg="gray", bg="white")
        self.svc_status.place(x=40, y=68)

        tk.Label(self.root,
                 text="安装完成后会自动部署内嵌 mosquitto.conf（listener 8907 / 允许匿名），"
                      "已安装服务将自动重启，并通过 paho-mqtt 进行 /hello 消息收发测试",
                 font=("Microsoft YaHei UI", 8), fg="darkorange", bg="white").place(x=20, y=222)

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(self.root, variable=self.progress_var, maximum=100,
                        length=600, mode="determinate").place(x=20, y=250)

        self.status_label = tk.Label(self.root, text="就绪", font=("Microsoft YaHei UI", 9),
                                     bg="white", fg="black")
        self.status_label.place(x=20, y=275)

        # 日志
        self.log_text = tk.Text(self.root, width=88, height=8, font=("Consolas", 9),
                                bg="black", fg="white", state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.place(x=20, y=305)
        sb = ttk.Scrollbar(self.root, command=self.log_text.yview)
        sb.place(x=608, y=305, height=115)
        self.log_text.config(yscrollcommand=sb.set)

        # 按钮
        btn_frame = tk.Frame(self.root, bg="#F0F0F0", height=55)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        btn_frame.pack_propagate(False)
        self.test_btn = tk.Button(btn_frame, text="MQTT 测试", width=12, bg="#107C10",
                                  fg="white", font=("Microsoft YaHei UI", 9, "bold"),
                                  relief=tk.FLAT, command=self.on_mqtt_test,
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
        mq_installed = False
        svc_state = "未检测"

        mq_exe = find_mosquitto_exe()
        if mq_exe:
            mq_installed = True
            ver = get_version(mq_exe)
            self.mq_version_text = f"已安装：Mosquitto {ver or '未知版本'} ({mq_exe.parent})"
            self.mq_status.config(text=self.mq_version_text, fg="green")
            self.mq_var.set(False)
        else:
            self.mq_version_text = ""
            self.mq_status.config(text="未安装", fg="red")
            self.mq_var.set(True)

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

        self.mq_installed = mq_installed
        self.root.after(0, lambda: self.test_btn.config(
            state=tk.NORMAL if mq_installed else tk.DISABLED))

    # ---------- 安装 ----------
    def on_mqtt_test(self):
        if self.is_busy:
            return
        self.is_busy = True
        self.root.after(0, lambda: self.test_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.install_btn.config(state=tk.DISABLED))

        def worker():
            try:
                result = self.run_mqtt_test()
                self.root.after(0, lambda: messagebox.showinfo(
                    "MQTT 测试", f"主题 {TEST_TOPIC} 消息收发测试通过！\n\n{result}"))
            except Exception as e:
                self.log(f"✗ MQTT 收发测试失败：{e}")
                self.root.after(0, lambda: messagebox.showerror(
                    "MQTT 测试", f"消息收发测试失败：\n\n{e}"))
            finally:
                self.is_busy = False
                self.root.after(0, self.refresh_status)

        threading.Thread(target=worker, daemon=True).start()

    def on_install(self):
        if self.is_installing or self.is_busy:
            return
        if not self.mq_var.get():
            messagebox.showinfo("提示", "未选中任何组件。\n已安装的组件默认跳过，勾选表示重装。")
            return
        extra = "\n\n注意：安装过程中请批准 UAC 提权提示（NSIS 安装包需要管理员权限）。" \
                "\n配置文件 mosquitto.conf 也来自内嵌资源。" \
                if self.mq_installed else \
                "\n\n安装过程中请批准 UAC 提权提示（NSIS 安装包需要管理员权限）。"
        if not messagebox.askyesno("确认安装", "将安装：\n\n  • Mosquitto MQTT Broker "
                                   f"{MOSQUITTO_VERSION}（静默安装）{extra}\n\n是否继续？"):
            return
        threading.Thread(target=self.install_worker, daemon=True).start()

    def install_worker(self):
        self.is_installing = True
        self.is_busy = True
        self.root.after(0, lambda: self.install_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.test_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.progress_var.set(0))
        try:
            self.install_mosquitto()
            # /hello 收发回环测试
            try:
                result = self.run_mqtt_test()
                test_text = f"MQTT 测试成功：{result}"
            except Exception as te:
                self.log(f"✗ MQTT 收发测试失败：{te}")
                self.update_status("安装完成，但 MQTT 收发测试失败")
                self.root.after(0, lambda: messagebox.showwarning(
                    "MQTT 测试", f"安装完成，但消息收发测试失败：\n\n{te}"))
                return
            self.root.after(0, lambda: self.progress_var.set(100))
            self.update_status("全部完成！")
            self.root.after(0, lambda: messagebox.showinfo(
                "安装完成", "Mosquitto MQTT Broker 安装完成！\n\n"
                f"主题 {TEST_TOPIC} 消息收发测试通过：\n{result}\n\n详情请查看日志。"))
        except Exception as e:
            self.update_status(f"安装失败：{e}")
            self.log(f"✗ 安装失败：{e}")
            self.root.after(0, lambda: messagebox.showerror("安装错误", f"安装失败：\n\n{e}"))
        finally:
            self.is_installing = False
            self.is_busy = False
            self.root.after(0, lambda: self.install_btn.config(state=tk.NORMAL))
            self.root.after(0, self.refresh_status)

    def run_mqtt_test(self) -> str:
        """运行 /hello 收发回环测试，返回结果摘要（失败抛异常）。"""
        port = TEST_PORT_DEFAULT
        mq_exe = find_mosquitto_exe()
        if mq_exe and (mq_exe.parent / CONF_FILE).exists():
            port = parse_listener_port(mq_exe.parent / CONF_FILE)
        self.log(f"开始 MQTT 回环测试：127.0.0.1:{port} 主题 {TEST_TOPIC} ...")
        result = mqtt_loopback_test(port=port)
        self.log(f"✓ MQTT 收发测试通过：{result}")
        return result

    def install_mosquitto(self):
        self.update_status("安装 Mosquitto MQTT Broker ...")
        assets = bundle_dir()
        installers = list(assets.glob("mosquitto-*-install-windows-x64.exe"))
        if not installers:
            raise RuntimeError("内嵌 Mosquitto 安装包缺失（assets/mosquitto-*-install-windows-x64.exe）")
        installer = installers[0]
        conf_src = assets / CONF_FILE
        if not conf_src.exists():
            raise RuntimeError("内嵌配置缺失（assets/mosquitto.conf）")
        self.log(f"运行内嵌安装包：{installer.name}（NSIS /S 静默安装，请批准 UAC 提示）")

        # NSIS 标准静默参数；安装包自带 UAC 提权清单
        cmd = [str(installer), "/S", "/NCRC", "/NOICONS"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode not in (0,):
            raise RuntimeError(f"Mosquitto 安装包返回码 {r.returncode}：{r.stderr or r.stdout}")

        mq_exe = find_mosquitto_exe()
        if not mq_exe:
            raise RuntimeError("Mosquitto 安装后未找到 mosquitto.exe")
        ver = get_version(mq_exe)
        self.log(f"✓ Mosquitto 安装成功：{ver or '未知版本'}")
        self.log(f"  安装目录：{mq_exe.parent}")

        self.root.after(0, lambda: self.progress_var.set(70))

        # 部署配置文件
        self.deploy_conf(mq_exe.parent, conf_src)

        # 服务处理
        self.handle_service(mq_exe.parent)

    def deploy_conf(self, install_dir: Path, conf_src: Path):
        target = install_dir / CONF_FILE
        try:
            if target.exists():
                if target.read_bytes() == conf_src.read_bytes():
                    self.log(f"  配置已一致，跳过：{target}")
                    return
                backup = install_dir / "mosquitto.conf.bak"
                try:
                    backup.unlink(missing_ok=True)
                except Exception:
                    pass
                shutil.copy2(target, backup)
                self.log(f"  原配置已备份：{backup.name}")
            shutil.copy2(conf_src, target)
            self.log(f"✓ 已部署配置：{target}（listener 8907 / allow_anonymous true）")
        except PermissionError:
            raise RuntimeError(f"无权限写入 {target}，请以管理员身份运行本安装器")
        except Exception as e:
            raise RuntimeError(f"写入配置失败：{e}")

    def handle_service(self, install_dir: Path):
        try:
            if service_exists():
                self.log("\n检测到已注册的 Windows 服务，正在重启以加载新配置...")
                subprocess.run(["net", "stop", SERVICE_NAME], capture_output=True,
                               text=True, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                time.sleep(1)
                r = subprocess.run(["net", "start", SERVICE_NAME],
                                   capture_output=True, text=True, timeout=120,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = (r.stdout or r.stderr or "").strip().splitlines()
                if out:
                    self.log(f"  {out[-1]}")
                if service_is_running():
                    self.log("✓ 服务重启成功，正在使用新配置（端口 8907）")
                else:
                    self.log("⚠ 服务重启后未确认运行状态，请检查服务管理器")
            else:
                self.log("\n未检测到 Windows 服务，尝试注册并启动 ...")
                home = str(install_dir / "mosquitto.exe")
                r = subprocess.run([home, "install"], capture_output=True, text=True, timeout=120,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                detail = (r.stdout or r.stderr or "").strip().splitlines()
                if service_exists():
                    self.log(f"  服务注册成功 {('：' + detail[-1]) if detail else ''}")
                    subprocess.run(["net", "start", SERVICE_NAME], capture_output=True,
                                   text=True, timeout=120,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    time.sleep(1)
                    if service_is_running():
                        self.log("✓ 服务已启动，正在使用新配置（端口 8907）")
                    else:
                        self.log("⚠ 服务已注册但未确认启动，可手动执行 net start mosquitto")
                else:
                    self.log("⚠ 服务未能注册为 Windows 服务（不影响手动运行），"
                             "可后续执行：net start mosquitto 或手动 'mosquitto install'")
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
