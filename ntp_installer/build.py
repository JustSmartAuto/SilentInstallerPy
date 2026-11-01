# -*- coding: utf-8 -*-
"""Build script for NTP Installer.

Uses PyInstaller to create a single-file executable with embedded assets
(Meinberg NTP NSIS installer).
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

NTP_SETUP_EXE = "ntp-4.2.4p8-lennon-o-lpv-win32-setup.exe"


def prepare_assets(assets_dir: Path, project_root: Path) -> None:
    """把根目录 / installer/ 的 NTP 资源同步到 assets/（构建输入）。"""
    assets_dir.mkdir(parents=True, exist_ok=True)
    pairs = [
        project_root / "installer" / NTP_SETUP_EXE,
        project_root / NTP_SETUP_EXE,
        project_root / "assets" / "ntp.ico",
    ]
    setup_src = next((p for p in pairs[:2] if p.exists()), None)
    if setup_src is None:
        print(f"[!] Warning: 源文件缺失 {NTP_SETUP_EXE}")
    else:
        dst = assets_dir / NTP_SETUP_EXE
        if not dst.exists() or dst.stat().st_size != setup_src.stat().st_size:
            shutil.copy2(setup_src, dst)
            print(f"[ASSETS] {NTP_SETUP_EXE} <- {setup_src}")

    icon_src = pairs[2]
    icon_dst = assets_dir / "app_icon.ico"
    if icon_src.exists():
        if not icon_dst.exists() or icon_dst.stat().st_size != icon_src.stat().st_size:
            shutil.copy2(icon_src, icon_dst)
            print("[ASSETS] app_icon.ico <- ntp.ico")
    else:
        print("[!] Warning: 源文件缺失 ntp.ico")


def main():
    project_root = Path(__file__).parent.resolve()
    src_dir = project_root / "src" / "ntp_installer"
    assets_dir = project_root / "assets"
    dist_dir = project_root / "dist"

    prepare_assets(assets_dir, project_root)

    required = [NTP_SETUP_EXE, "app_icon.ico"]
    missing = [f for f in required if not (assets_dir / f).exists()]
    if missing:
        print("[!] Warning: assets 目录缺少文件：")
        for m in missing:
            print(f"    - {m}")
        print("    构建仍会进行，但运行时会报错。")
    else:
        print("[OK] assets 检查通过")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exe_name = f"NtpInstaller_{timestamp}"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        f"--name={exe_name}",
        f"--distpath={dist_dir}",
        f"--workpath={project_root / 'build'}",
        f"--specpath={project_root / 'build'}",
        f"--add-data={assets_dir}{os.pathsep}assets",
        "--hidden-import=winreg",
        "--hidden-import=tkinter",
    ]

    icon_path = assets_dir / "app_icon.ico"
    if icon_path.exists():
        cmd.append(f"--icon={icon_path}")
    else:
        print("[!] Warning: 未找到 assets/app_icon.ico，将使用默认图标")

    cmd.append(str(src_dir / "main.py"))

    print(f"\n[BUILD] Building: {exe_name}.exe\n")
    result = subprocess.run(cmd, cwd=project_root)
    if result.returncode != 0:
        print("[FAIL] Build failed!")
        sys.exit(1)

    exe_path = dist_dir / f"{exe_name}.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"\n[OK] Build successful!\n   Output: {exe_path}\n   Size:   {size_mb:.2f} MB")
    else:
        print("\n[!] Build completed but output file not found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
