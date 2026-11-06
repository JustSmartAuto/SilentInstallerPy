# -*- coding: utf-8 -*-
"""把本机(健康实例)的数据目录打包成嵌入资源 mariadb_data_13.0.2.zip。

流程：停本机服务 -> 打 zip(排除 *.err/*.pid/my.ini/ibtmp1/ddl_recovery 日志) -> 重新启动本机服务。
"""
import subprocess
import zipfile
from pathlib import Path

SERVICE = "MariaDB"
DATA = Path(r"D:\JustSmart\MariaDB\data")
OUT = Path(r"D:\JustStupid\silent安装器_2606231854\mariadb_installer\assets")
OUT.mkdir(parents=True, exist_ok=True)
ZIP_PATH = OUT / "mariadb_data_13.0.2.zip"

SUB = subprocess.CREATE_NO_WINDOW


def run(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=SUB)
    return (r.stdout or "") + (r.stderr or "")


print("停止本机服务 ...")
print(run("net", "stop", SERVICE).strip() or "(无输出)")

EXCLUDE = {"*.err", "*.pid", "my.ini", "ibtmp1", "ddl_recovery.log",
           "ddl_recovery-backup.log"}

skip_start = False
try:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    files = [p for p in sorted(DATA.rglob("*")) if p.is_file()]
    kept = []
    for p in files:
        rel = p.relative_to(DATA).as_posix()
        name = p.name.lower()
        if any(Path(name).match(g.lower()) for g in EXCLUDE):
            continue
        kept.append((p, rel))
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for p, rel in kept:
            zf.write(p, rel)
    print(f"打包完成：{ZIP_PATH}（{ZIP_PATH.stat().st_size/1048576:.1f} MB，"
          f"{len(kept)} 个文件）")
finally:
    print("重启本机服务 ...")
    print(run("net", "start", SERVICE).strip() or "(无输出)")
    skip_start = True
