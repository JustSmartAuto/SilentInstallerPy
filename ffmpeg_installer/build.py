#!/usr/bin/env python3
"""Build script for FFmpeg Installer.

Uses PyInstaller to create a single-file executable with embedded assets.
The output filename includes a timestamp.

Usage:
    uv run python build.py
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main():
    project_root = Path(__file__).parent.resolve()
    src_dir = project_root / "src" / "ffmpeg_installer"
    assets_dir = project_root / "assets"
    dist_dir = project_root / "dist"

    # Ensure assets directory exists
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Check for FFmpeg archive (zip or 7z)
    archive_files = list(assets_dir.glob("*.zip")) + list(assets_dir.glob("*.ZIP")) + list(assets_dir.glob("*.7z")) + list(assets_dir.glob("*.7Z"))
    if not archive_files:
        print("[!] Warning: No FFmpeg archive (.zip or .7z) found in assets/ directory.")
        print("    Please place the FFmpeg archive in assets/ before building.")
        print("    The installer will still build but will fail at runtime.")
    else:
        print(f"[OK] Found archive: {archive_files[0].name}")

    # Determine icon path
    icon_path = assets_dir / "app_icon.ico"
    icon_arg = [f'--icon={icon_path}'] if icon_path.exists() else []

    # Build timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exe_name = f"FFmpegInstaller_{timestamp}"

    # PyInstaller command
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
    ]

    cmd.extend(icon_arg)

    # Add hidden imports
    cmd.extend([
        "--hidden-import=zipfile",
        "--hidden-import=winreg",
    ])

    # Main script path
    cmd.append(str(src_dir / "main.py"))

    print(f"\n[BUILD] Building: {exe_name}.exe")
    print(f"Command: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, cwd=project_root)

    if result.returncode != 0:
        print("[FAIL] Build failed!")
        sys.exit(1)

    # Verify output
    exe_path = dist_dir / f"{exe_name}.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"\n[OK] Build successful!")
        print(f"   Output: {exe_path}")
        print(f"   Size:   {size_mb:.2f} MB")
    else:
        print("\n[!] Build completed but output file not found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
