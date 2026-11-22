import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


REQUIRED_ASSETS = (
    "Alpine.exe",
    "rootfs.tar.gz",
    "wsl_update_x64.msi",
    "app_icon.ico",
)


def main():
    project_root = Path(__file__).parent.resolve()
    src_dir = project_root / "src" / "wsl_alpine_installer"
    assets_dir = project_root / "assets"
    dist_dir = project_root / "dist"

    missing = [name for name in REQUIRED_ASSETS if not (assets_dir / name).is_file()]
    if missing:
        print("[FAIL] assets 目录缺少文件：")
        for name in missing:
            print(f"    - {name}")
        sys.exit(1)
    print("[OK] assets 检查通过")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exe_name = f"WslAlpineInstaller_{timestamp}"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed",
        "--uac-admin",
        "--noconfirm",
        "--clean",
        f"--name={exe_name}",
        f"--distpath={dist_dir}",
        f"--workpath={project_root / 'build'}",
        f"--specpath={project_root / 'build'}",
        f"--add-data={assets_dir}{os.pathsep}assets",
        "--hidden-import=winreg",
        "--hidden-import=tkinter",
        f"--icon={assets_dir / 'app_icon.ico'}",
        str(src_dir / "main.py"),
    ]

    print(f"\n[BUILD] Building: {exe_name}.exe\n")
    result = subprocess.run(cmd, cwd=project_root)
    if result.returncode != 0:
        print("[FAIL] Build failed!")
        sys.exit(result.returncode)

    exe_path = dist_dir / f"{exe_name}.exe"
    if not exe_path.is_file():
        print("\n[FAIL] 构建结束但未找到输出文件")
        sys.exit(1)
    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"\n[OK] Build successful!\n   Output: {exe_path}\n   Size:   {size_mb:.2f} MB")


if __name__ == "__main__":
    main()
