#!/usr/bin/env python3
"""UV Offline Installer - tkinter version

A Python/tkinter offline installer for uv (the extremely fast Python package installer).
Features:
- User-level or system-wide installation
- Embedded zip extraction using zipfile
- Automatic PATH configuration via Windows Registry
- Admin privilege elevation for system-wide install
- Self-contained executable via PyInstaller

The installer embeds uv-x86_64-pc-windows-msvc.zip and extracts:
  - uv.exe
  - uvx.exe
  - uvw.exe

Install locations (matching official uv installer):
- User: %LOCALAPPDATA%\\uv (or XDG_BIN_HOME, XDG_DATA_HOME\\..\\bin, HOME\\.local\\bin)
- System: %ProgramFiles%\\uv
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

# Windows-specific imports
if sys.platform == "win32":
    import winreg

# Version
CURRENT_VERSION = "0.11.23"
APP_NAME = "uv"


def is_admin() -> bool:
    """Check if current process has administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def run_as_admin(args: list[str]) -> None:
    """Restart current script with admin privileges using UAC elevation."""
    script = sys.argv[0]
    params = " ".join([f'"{a}"' for a in args])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}" {params}', None, 1)


def broadcast_env_change() -> None:
    """Broadcast WM_SETTINGCHANGE to notify Windows of environment variable changes."""
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_long()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST,
        WM_SETTINGCHANGE,
        0,
        "Environment",
        SMTO_ABORTIFHUNG,
        5000,
        ctypes.byref(result),
    )


class InstallationScope:
    USER = "user"
    SYSTEM = "system"


class UVInstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("UV Offline Installer")
        self.root.geometry("600x500")
        self.root.resizable(False, False)
        self.root.configure(bg="white")

        # Installation state
        self.is_installing = False
        self.installation_scope = InstallationScope.USER

        # Paths
        self.temp_dir = Path(tempfile.gettempdir()) / "uv-installer"

        # Build UI
        self._build_ui()

        # Handle --system flag (after UAC elevation)
        if len(sys.argv) > 1 and sys.argv[1] == "--system":
            self.installation_scope = InstallationScope.SYSTEM
            self.log_message("Installation scope: system-wide (elevated)")
            self.status_label.config(text="Ready to install (system-wide)")
        else:
            if not self.show_scope_dialog():
                self.root.after(100, self.root.destroy)
                return

        self.check_admin_privileges()
        self.version_label.config(text=f"UV Offline Installer v{CURRENT_VERSION}")
        self.log_message("Ready to install uv (offline mode)")

    def _build_ui(self):
        """Build the main application UI."""
        # Header panel
        header_frame = tk.Frame(self.root, bg="#2D2D30", height=100)
        header_frame.pack(fill=tk.X)
        header_frame.pack_propagate(False)

        self.title_label = tk.Label(
            header_frame,
            text="UV Offline Installer",
            font=("Segoe UI", 16, "bold"),
            fg="white",
            bg="#2D2D30",
        )
        self.title_label.place(x=20, y=20)

        self.version_label = tk.Label(
            header_frame,
            text=f"Version {CURRENT_VERSION}",
            font=("Segoe UI", 9),
            fg="lightgray",
            bg="#2D2D30",
        )
        self.version_label.place(x=20, y=55)

        # Status label
        self.status_label = tk.Label(
            self.root,
            text="Ready to install",
            font=("Segoe UI", 9),
            bg="white",
            fg="black",
        )
        self.status_label.place(x=20, y=120)

        # Progress bar
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            self.root,
            variable=self.progress_var,
            maximum=100,
            length=540,
            mode="determinate",
        )
        self.progress_bar.place(x=20, y=150)

        # Log text box
        self.log_text = tk.Text(
            self.root,
            width=76,
            height=14,
            font=("Consolas", 9),
            bg="black",
            fg="white",
            state=tk.DISABLED,
            wrap=tk.WORD,
        )
        self.log_text.place(x=20, y=190)

        # Scrollbar for log
        scrollbar = ttk.Scrollbar(self.root, command=self.log_text.yview)
        scrollbar.place(x=560, y=190, height=220)
        self.log_text.config(yscrollcommand=scrollbar.set)

        # Button panel
        button_frame = tk.Frame(self.root, bg="#F0F0F0", height=60)
        button_frame.pack(side=tk.BOTTOM, fill=tk.X)
        button_frame.pack_propagate(False)

        self.install_button = tk.Button(
            button_frame,
            text="Install UV",
            width=14,
            height=1,
            bg="#0078D7",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT,
            command=self.on_install_click,
        )
        self.install_button.place(x=280, y=12)

        self.about_button = tk.Button(
            button_frame,
            text="About",
            width=8,
            height=1,
            bg="#646464",
            fg="white",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            command=self.show_about,
        )
        self.about_button.place(x=410, y=12)

        self.exit_button = tk.Button(
            button_frame,
            text="Exit",
            width=8,
            height=1,
            bg="#A0A0A0",
            fg="white",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            command=self.root.destroy,
        )
        self.exit_button.place(x=500, y=12)

    def show_scope_dialog(self) -> bool:
        """Show installation scope selection dialog. Returns True if user continues."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Installation Scope")
        dialog.geometry("500x330")
        dialog.resizable(False, False)
        dialog.configure(bg="white")
        dialog.transient(self.root)
        dialog.grab_set()

        # Center dialog
        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() // 2) - (500 // 2)
        y = (dialog.winfo_screenheight() // 2) - (330 // 2)
        dialog.geometry(f"+{x}+{y}")

        tk.Label(
            dialog,
            text="Choose Installation Scope",
            font=("Segoe UI", 12, "bold"),
            bg="white",
        ).place(x=20, y=20)

        tk.Label(
            dialog,
            text="How would you like to install uv?",
            font=("Segoe UI", 9),
            fg="gray",
            bg="white",
        ).place(x=20, y=50)

        scope_var = tk.StringVar(value="user")

        tk.Radiobutton(
            dialog,
            text="User Installation (Recommended)",
            font=("Segoe UI", 9, "bold"),
            variable=scope_var,
            value="user",
            bg="white",
        ).place(x=30, y=85)

        tk.Label(
            dialog,
            text="• No administrator privileges required\n"
                 "• Available only for your user account\n"
                 "• Installed in your AppData folder",
            font=("Segoe UI", 8),
            fg="darkgray",
            bg="white",
            justify=tk.LEFT,
        ).place(x=50, y=108)

        tk.Radiobutton(
            dialog,
            text="System-wide Installation",
            font=("Segoe UI", 9, "bold"),
            variable=scope_var,
            value="system",
            bg="white",
        ).place(x=30, y=160)

        tk.Label(
            dialog,
            text="• Requires administrator privileges\n"
                 "• Available for all users on this computer\n"
                 "• Installed in Program Files or similar",
            font=("Segoe UI", 8),
            fg="darkgray",
            bg="white",
            justify=tk.LEFT,
        ).place(x=50, y=183)

        result = {"value": False}

        def on_ok():
            result["value"] = True
            self.installation_scope = scope_var.get()
            dialog.destroy()

        def on_cancel():
            result["value"] = False
            dialog.destroy()

        ok_btn = tk.Button(
            dialog,
            text="Continue",
            width=10,
            height=1,
            bg="#0078D7",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT,
            command=on_ok,
        )
        ok_btn.place(x=280, y=245)

        cancel_btn = tk.Button(
            dialog,
            text="Cancel",
            width=9,
            height=1,
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            command=on_cancel,
        )
        cancel_btn.place(x=390, y=245)

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        dialog.wait_window()

        if result["value"]:
            scope_text = "user-level" if self.installation_scope == InstallationScope.USER else "system-wide"
            self.log_message(f"Installation scope selected: {scope_text}")
            self.status_label.config(text=f"Ready to install ({scope_text})")
        else:
            self.log_message("Installation cancelled by user")

        return result["value"]

    def check_admin_privileges(self):
        """Check and request admin privileges if needed."""
        if self.installation_scope == InstallationScope.SYSTEM:
            if not is_admin():
                try:
                    self.log_message("Requesting administrator privileges...")
                    run_as_admin(["--system"])
                    sys.exit(0)
                except Exception as e:
                    self.log_message(f"Failed to elevate privileges: {e}")
                    messagebox.showwarning(
                        "Administrator Required",
                        "Administrator privileges are required for system-wide installation.\n\n"
                        "Please approve the UAC prompt or choose User Installation instead.",
                    )
                    sys.exit(1)
            else:
                self.log_message("System-wide installation with administrator privileges")
        else:
            self.log_message("User installation selected - administrator privileges not required")

    def on_install_click(self):
        """Handle install button click."""
        if self.is_installing:
            return

        target = self._get_install_dir()

        if not messagebox.askyesno(
            "Confirm Installation",
            f"This will install uv to:\n{target}\n\n"
            "And add it to your system PATH.\n\nProceed?",
        ):
            return

        # Run installation in a separate thread
        thread = threading.Thread(target=self.install_uv, daemon=True)
        thread.start()

    def _get_install_dir(self) -> Path:
        """Get the installation directory based on scope."""
        if self.installation_scope == InstallationScope.SYSTEM:
            return Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "uv"
        
        # User installation: follow official installer logic
        # 1. XDG_BIN_HOME
        xdg_bin = os.environ.get("XDG_BIN_HOME")
        if xdg_bin:
            return Path(xdg_bin)
        
        # 2. XDG_DATA_HOME/../bin
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data).parent / "bin"
        
        # 3. HOME/.local/bin
        home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if home:
            return Path(home) / ".local" / "bin"
        
        # Fallback: LOCALAPPDATA/uv
        return Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "uv"

    def _get_receipt_home(self) -> Path:
        """Get the receipt home directory."""
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "uv"
        return Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "uv"

    def install_uv(self):
        """Main installation workflow."""
        self.is_installing = True
        self.root.after(0, lambda: self.install_button.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.progress_var.set(0))

        try:
            final_dir = self._get_install_dir()
            receipt_home = self._get_receipt_home()

            # Step 1: Clean up previous installation
            self.update_status("Cleaning up previous installation...")
            self.cleanup_previous_installation(final_dir)
            self.root.after(0, lambda: self.progress_var.set(10))

            # Step 2: Extract embedded zip file
            self.update_status("Extracting uv from embedded package...")
            archive_path = self.extract_embedded_archive()
            self.root.after(0, lambda: self.progress_var.set(30))

            # Step 3: Extract zip to temp
            self.update_status("Extracting uv files...")
            extract_dir = self.temp_dir / "extracted"
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            extract_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(archive_path, "r") as z:
                z.extractall(path=extract_dir)
            self.root.after(0, lambda: self.progress_var.set(60))

            # Step 4: Install binaries
            self.update_status("Installing binaries...")
            final_dir.mkdir(parents=True, exist_ok=True)
            
            # Copy binaries from extracted archive
            bins = ["uv.exe", "uvx.exe", "uvw.exe"]
            for bin_name in bins:
                src = extract_dir / bin_name
                if src.exists():
                    dst = final_dir / bin_name
                    shutil.copy2(str(src), str(dst))
                    self.log_message(f"  Installed {bin_name}")
                else:
                    # Try finding in subdirectories
                    found = list(extract_dir.rglob(bin_name))
                    if found:
                        dst = final_dir / bin_name
                        shutil.copy2(str(found[0]), str(dst))
                        self.log_message(f"  Installed {bin_name}")
                    else:
                        raise RuntimeError(f"Binary not found in archive: {bin_name}")

            self.root.after(0, lambda: self.progress_var.set(80))

            # Step 5: Write install receipt
            self.update_status("Writing install receipt...")
            self._write_receipt(receipt_home, final_dir, bins)
            self.root.after(0, lambda: self.progress_var.set(90))

            # Step 6: Add to PATH
            self.update_status("Configuring PATH...")
            self.add_to_system_path(str(final_dir))
            self.root.after(0, lambda: self.progress_var.set(95))

            # Step 7: Test installation
            self.update_status("Testing installation...")
            self.test_installation(final_dir)
            self.root.after(0, lambda: self.progress_var.set(100))

            scope_text = "user-level" if self.installation_scope == InstallationScope.USER else "system-wide"
            self.update_status("Installation completed successfully!")
            self.log_message(f"✓ uv ({scope_text}) installation completed successfully!")

            restart_msg = (
                "Please restart your command prompt or applications to use uv."
                if self.installation_scope == InstallationScope.USER
                else "Please restart your command prompt to use uv."
            )

            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "Installation Complete",
                    f"uv has been installed successfully as a {scope_text} installation!\n\n{restart_msg}",
                ),
            )

        except Exception as e:
            self.update_status(f"Installation failed: {e}")
            self.log_message(f"✗ Installation failed: {e}")
            self.root.after(
                0,
                lambda: messagebox.showerror("Installation Error", f"Installation failed:\n\n{e}"),
            )
        finally:
            # Clean up temp files
            try:
                if self.temp_dir.exists():
                    shutil.rmtree(self.temp_dir)
            except Exception:
                pass

            self.is_installing = False
            self.root.after(0, lambda: self.install_button.config(state=tk.NORMAL))

    def cleanup_previous_installation(self, target_dir: Path):
        """Remove previous installation if it exists."""
        try:
            if target_dir.exists():
                # Only remove the binaries we manage, not the whole directory
                for bin_name in ["uv.exe", "uvx.exe", "uvw.exe"]:
                    bin_path = target_dir / bin_name
                    if bin_path.exists():
                        bin_path.unlink()
                self.log_message("Previous installation cleaned up")
        except Exception as e:
            self.log_message(f"Warning: Could not clean up previous installation: {e}")

    def extract_embedded_archive(self) -> Path:
        """Extract embedded zip archive from PyInstaller bundle or package."""
        try:
            self.log_message("Extracting embedded uv package...")

            # When running as PyInstaller bundle, sys._MEIPASS is the extraction dir
            if hasattr(sys, "_MEIPASS"):
                bundle_dir = Path(sys._MEIPASS)
            else:
                bundle_dir = Path(__file__).parent

            # Search for embedded archive, excluding PyInstaller internal files
            excluded_names = {
                "base_library.zip", "python3.zip", "python38.zip", "python39.zip",
                "python310.zip", "python311.zip", "python312.zip"
            }
            archive_candidates = []
            for pattern in ("*.zip", "*.ZIP"):
                for path in bundle_dir.rglob(pattern):
                    if path.name not in excluded_names:
                        archive_candidates.append(path)

            # Also check common locations
            search_paths = [
                bundle_dir,
                bundle_dir / "assets",
                bundle_dir.parent / "assets",
                Path.cwd(),
                Path.cwd() / "assets",
            ]
            for sp in search_paths:
                if sp.exists() and sp.is_dir():
                    for pattern in ("*.zip", "*.ZIP"):
                        for path in sp.glob(pattern):
                            if path.name not in excluded_names and path not in archive_candidates:
                                archive_candidates.append(path)

            if not archive_candidates:
                raise RuntimeError(
                    "Embedded uv package not found. "
                    "Please ensure the .zip file is placed in the assets/ directory."
                )

            # Prefer files in 'assets' directory, then largest file
            def sort_key(path: Path):
                in_assets = "assets" in path.parts
                return (-int(in_assets), -path.stat().st_size)

            archive_candidates.sort(key=sort_key)
            archive_path = archive_candidates[0]
            self.log_message(f"Found embedded archive: {archive_path.name}")

            # Copy to temp
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            temp_archive = self.temp_dir / "uv_embedded.zip"
            shutil.copy2(archive_path, temp_archive)

            size_mb = temp_archive.stat().st_size / (1024 * 1024)
            self.log_message(f"Archive copied to temp: {size_mb:.2f} MB")
            return temp_archive

        except Exception as e:
            raise RuntimeError(f"Failed to extract embedded archive: {e}")

    def _write_receipt(self, receipt_home: Path, install_dir: Path, bins: list[str]):
        """Write the uv install receipt JSON."""
        try:
            receipt_home.mkdir(parents=True, exist_ok=True)
            
            receipt = {
                "binaries": bins,
                "binary_aliases": {},
                "cdylibs": [],
                "cstaticlibs": [],
                "install_layout": "flat",
                "install_prefix": str(install_dir),
                "modify_path": True,
                "provider": {
                    "source": "cargo-dist",
                    "version": "0.31.0"
                },
                "source": {
                    "app_name": "uv",
                    "name": "uv",
                    "owner": "astral-sh",
                    "release_type": "github"
                },
                "version": CURRENT_VERSION
            }
            
            receipt_path = receipt_home / "uv-receipt.json"
            with open(receipt_path, "w", encoding="utf-8") as f:
                json.dump(receipt, f, indent=2, ensure_ascii=False)
            
            self.log_message(f"Install receipt written to {receipt_path}")
        except Exception as e:
            self.log_message(f"Warning: Could not write receipt: {e}")

    def add_to_system_path(self, path: str):
        """Add directory to user or system PATH via Windows Registry."""
        try:
            if self.installation_scope == InstallationScope.USER:
                key_path = r"Environment"
                root = winreg.HKEY_CURRENT_USER
                path_type = "user PATH"
            else:
                key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
                root = winreg.HKEY_LOCAL_MACHINE
                path_type = "system PATH"

            with winreg.OpenKey(root, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                try:
                    current_path, _ = winreg.QueryValueEx(key, "PATH")
                except FileNotFoundError:
                    current_path = ""

                # Normalize for comparison
                current_path_lower = current_path.lower() if current_path else ""
                path_lower = path.lower()
                
                # Check if path is already in PATH (case-insensitive)
                paths = [p.strip() for p in current_path.split(";") if p.strip()]
                already_exists = any(p.lower() == path_lower for p in paths)
                
                if not already_exists:
                    new_path = f"{path};{current_path}" if current_path else path
                    winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
                    self.log_message(f"Successfully added to {path_type}")
                else:
                    self.log_message(f"Path already exists in {path_type}")

            # Broadcast environment change
            broadcast_env_change()

        except Exception as e:
            raise RuntimeError(f"Failed to update PATH: {e}")

    def test_installation(self, bin_dir: Path):
        """Run uv --version to verify installation."""
        try:
            uv_exe = bin_dir / "uv.exe"
            result = subprocess.run(
                [str(uv_exe), "--version"],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                self.log_message(f"✓ uv is working: {version}")
            else:
                self.log_message("⚠ uv test failed, but installation appears complete")
        except Exception as e:
            self.log_message(f"⚠ Could not test installation: {e}")

    def update_status(self, message: str):
        """Update status label and log."""
        self.root.after(0, lambda: self.status_label.config(text=message))
        self.log_message(message)

    def log_message(self, message: str):
        """Append message to log text box with timestamp."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: self._append_log(f"[{timestamp}] {message}\n"))

    def _append_log(self, text: str):
        """Thread-safe log append."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def show_about(self):
        """Show about dialog."""
        about = tk.Toplevel(self.root)
        about.title("About UV Offline Installer")
        about.geometry("450x280")
        about.resizable(False, False)
        about.configure(bg="white")
        about.transient(self.root)
        about.grab_set()

        about.update_idletasks()
        x = (about.winfo_screenwidth() // 2) - (450 // 2)
        y = (about.winfo_screenheight() // 2) - (280 // 2)
        about.geometry(f"+{x}+{y}")

        tk.Label(
            about,
            text="UV Offline Installer",
            font=("Segoe UI", 16, "bold"),
            fg="#0078D7",
            bg="white",
        ).place(x=80, y=20)

        tk.Label(
            about,
            text=f"Version {CURRENT_VERSION}",
            font=("Segoe UI", 10),
            fg="gray",
            bg="white",
        ).place(x=80, y=50)

        tk.Label(
            about,
            text="A Windows offline installer for uv with automatic PATH configuration.\n"
                 "No internet connection required - uv is embedded in the installer.",
            font=("Segoe UI", 9),
            bg="white",
            fg="black",
            justify=tk.LEFT,
            wraplength=400,
        ).place(x=20, y=90)

        tk.Label(
            about,
            text="uv is an extremely fast Python package installer and resolver,\n"
                 "written in Rust by Astral Software.",
            font=("Segoe UI", 8),
            fg="gray",
            bg="white",
        ).place(x=20, y=145)

        tk.Button(
            about,
            text="Close",
            width=8,
            height=1,
            bg="#0078D7",
            fg="white",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            command=about.destroy,
        ).place(x=340, y=200)


def main():
    root = tk.Tk()
    app = UVInstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
