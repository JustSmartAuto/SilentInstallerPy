# -*- coding: utf-8 -*-
"""Build script for RemoteRipple Installer.

Uses PyInstaller to create a single-file executable with embedded assets
(.NET Desktop Runtime 6.0.0 installer + RemoteRipple setup + icon).
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def prepare_assets(assets_dir: Path, project_root: Path) -> None:
    """确保 assets/ 下有构建所需的资源（已存在于 assets 则直接校验）。"""
    assets_dir.mkdir(parents=True, exist_ok=True)
    required_names = [
        "dotnet-windowsdesktop-runtime-6.0.0-win-x64.exe",
        "RemoteRipple-1.2.0-setup.exe",
        "remoteripple.ico",
    ]
    for name in required_names:
        src = project_root / "assets" / name
        dst = assets_dir / name
        if src.exists() and src != dst:
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
                print(f"[ASSETS] {name} <- {src}")
        elif not src.exists():
            print(f"[!] Warning: 源文件缺失 {src}")


def main():
    project_root = Path(__file__).parent.resolve()
    src_dir = project_root / "src" / "remoteripple_installer"
    assets_dir = project_root / "assets"
    dist_dir = project_root / "dist"

    prepare_assets(assets_dir, project_root)

    required = [
        "dotnet-windowsdesktop-runtime-6.0.0-win-x64.exe",
        "RemoteRipple-1.2.0-setup.exe",
        "remoteripple.ico",
    ]
    missing = [f for f in required if not (assets_dir / f).exists()]
    if missing:
        print("[!] Warning: assets 目录缺少文件：")
        for m in missing:
            print(f"    - {m}")
        print("    构建仍会进行，但运行时会报错。")
    else:
        print("[OK] assets 检查通过")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exe_name = f"RemoteRippleInstaller_{timestamp}"

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

    icon_path = assets_dir / "remoteripple.ico"
    if icon_path.exists():
        cmd.append(f"--icon={icon_path}")
    else:
        print("[!] Warning: 未找到 assets/remoteripple.ico，将使用默认图标")

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
