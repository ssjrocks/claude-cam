"""Build the one-click desktop installer.

    pip install pyinstaller pillow ./plugin/server
    python installer/build.py

Makes the claude-cam server as a folder (it starts fast every time Claude launches it), zips that
folder together with the skill and the phone QR code, and wraps the zip in a one-file setup
program: dist/ClaudeCamSetup.exe on Windows (dist/ClaudeCamSetup elsewhere, for testing).
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "installer"
DIST = ROOT / "dist"
EXE = ".exe" if sys.platform == "win32" else ""
HEAVY = ["aiohttp", "PIL", "av", "zeroconf", "numpy", "tkinter"]


def pyinstaller(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "WARN",
         "--distpath", str(BUILD / "dist"), "--workpath", str(BUILD / "work"), "--specpath", str(BUILD), *args],
        check=True,
    )


def make_icon() -> Path:
    """The app's launcher icon (a dark eye on Claude Cam orange) as a Windows .ico."""
    from PIL import Image, ImageDraw

    size = 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 8, size - 8, size - 8), radius=56, fill=(217, 119, 87))
    d.ellipse((40, 78, size - 40, size - 78), fill=(20, 20, 19))
    d.ellipse((92, 92, size - 92, size - 92), fill=(240, 238, 230))
    d.ellipse((112, 112, size - 112, size - 112), fill=(20, 20, 19))
    path = BUILD / "claude-cam.ico"
    im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


def main() -> None:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    icon = make_icon()

    # 1. The server, as a folder. Windowed, so Claude launching it never flashes a console window.
    pyinstaller(
        "--name", "claude-cam", "--onedir", "--windowed", "--icon", str(icon),
        "--collect-data", "claude_cam", "--collect-submodules", "claude_cam", "--collect-submodules", "av", "--collect-submodules", "zeroconf",
        str(ROOT / "installer" / "server_entry.py"),
    )
    server = BUILD / "dist" / "claude-cam"
    extras = server / "extras"
    extras.mkdir()
    shutil.copyfile(ROOT / "plugin" / "skills" / "phone-camera" / "SKILL.md", extras / "SKILL.md")
    shutil.copyfile(ROOT / "docs" / "images" / "qr-apk.png", extras / "qr-apk.png")

    # 2. Zip it up.
    payload = BUILD / "payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(server.rglob("*")):
            z.write(f, f.relative_to(server))

    # 3. The one-file setup program that carries the zip. It only needs the standard library.
    excludes = [arg for mod in HEAVY for arg in ("--exclude-module", mod)]
    pyinstaller(
        "--name", "ClaudeCamSetup", "--onefile", "--console", "--icon", str(icon),
        "--add-data", f"{payload}{os.pathsep}.", *excludes,
        str(ROOT / "installer" / "setup_entry.py"),
    )
    DIST.mkdir(exist_ok=True)
    out = DIST / f"ClaudeCamSetup{EXE}"
    shutil.copyfile(BUILD / "dist" / f"ClaudeCamSetup{EXE}", out)
    if not EXE:
        out.chmod(0o755)
    print(f"Built {out} ({out.stat().st_size / 1e6:.1f} MB); payload {payload.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
