"""Set Claude Cam up for Claude without the plugin system, for the one-click Windows installer.

The installer copies the bundled claude-cam program into the user's folder, registers it with
Claude as a user-scope MCP server (with Claude's own `claude mcp add` when a `claude` command can be
found, including the one bundled with the desktop app; otherwise by editing Claude's settings file),
installs the phone-camera skill, and on Windows lets the phone through the firewall and adds an
entry to Settings > Apps so it can be removed again.

Only the standard library is used here, so the setup program stays small.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from . import __version__

MCP_NAME = "claude-cam"
SKILL_NAME = "phone-camera"
FIREWALL_RULE = "Claude Cam"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ClaudeCam"
MARKER = ".installed-by-claude-cam"
APK_URL = "https://github.com/ssjrocks/claude-cam/releases/latest/download/claude-cam.apk"
WINDOWS = sys.platform == "win32"


# --- where things live -----------------------------------------------------------------------------


def claude_dir() -> Path:
    """Claude Code's config folder (~/.claude, or $CLAUDE_CONFIG_DIR)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def claude_json() -> Path:
    """Claude Code's main settings file, where user-scope MCP servers are kept."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() / ".claude.json" if env else Path.home() / ".claude.json"


def install_root() -> Path:
    if env := os.environ.get("CLAUDE_CAM_INSTALL_DIR"):
        return Path(env).expanduser()
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / "ClaudeCam"
    return Path.home() / ".local" / "share" / "claude-cam"


def server_exe(folder: Path) -> Path:
    return folder / ("claude-cam.exe" if WINDOWS else "claude-cam")


def _version_key(path: Path) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", path.parent.name)) or (0,)


def find_claude() -> str | None:
    """A `claude` command: on PATH, the standalone install, or the copy bundled with the desktop app."""
    if found := shutil.which("claude"):
        return found
    home = Path.home()
    candidates = [home / ".local" / "bin" / ("claude.exe" if WINDOWS else "claude")]
    if WINDOWS:
        bundled = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Claude" / "claude-code"
        candidates += sorted(bundled.glob("*/claude.exe"), key=_version_key, reverse=True)
    else:
        for base in (home / ".config" / "Claude" / "claude-code", home / "Library" / "Application Support" / "Claude" / "claude-code"):
            candidates += sorted(base.glob("*/claude"), key=_version_key, reverse=True)
    return next((str(c) for c in candidates if c.is_file()), None)


def plugin_installed() -> bool:
    """True if the claude-cam plugin is installed, which would make this setup a duplicate."""
    index = claude_dir() / "plugins" / "installed_plugins.json"
    try:
        return "claude-cam@" in index.read_text(encoding="utf-8")
    except OSError:
        return False


# --- registering with Claude ----------------------------------------------------------------------


def _edit_claude_json(change) -> None:
    path = claude_json()
    data = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else {}
        backup = path.with_name(path.name + ".claude-cam-backup")
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
    change(data)
    tmp = path.with_name(path.name + ".claude-cam-tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _run(cmd: list[str], timeout: float = 90) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags
    )


def register(exe: Path) -> str:
    """Add Claude Cam as a user-scope MCP server. Returns how it was done."""
    entry = {"type": "stdio", "command": str(exe), "args": ["stdio"], "env": {}}
    cli = find_claude()
    if cli:
        try:
            _run([cli, "mcp", "remove", MCP_NAME, "-s", "user"])
            done = _run([cli, "mcp", "add", "-s", "user", MCP_NAME, "--", str(exe), "stdio"])
            if done.returncode == 0:
                return f"with Claude's own setup command ({cli})"
        except (OSError, subprocess.SubprocessError):
            pass
    _edit_claude_json(lambda data: data.setdefault("mcpServers", {}).__setitem__(MCP_NAME, entry))
    return f"in {claude_json()}"


def unregister() -> None:
    cli = find_claude()
    if cli:
        try:
            _run([cli, "mcp", "remove", MCP_NAME, "-s", "user"])
        except (OSError, subprocess.SubprocessError):
            pass
    if claude_json().exists():
        _edit_claude_json(lambda data: (data.get("mcpServers") or {}).pop(MCP_NAME, None))


def install_skill(source: Path) -> Path:
    folder = claude_dir() / "skills" / SKILL_NAME
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, folder / "SKILL.md")
    (folder / MARKER).write_text(__version__, encoding="utf-8")
    return folder


def remove_skill() -> None:
    folder = claude_dir() / "skills" / SKILL_NAME
    if (folder / MARKER).exists():  # never remove a skill someone else put there
        shutil.rmtree(folder, ignore_errors=True)


# --- Windows: firewall, network type, Apps entry -------------------------------------------------


def _powershell(script: str, elevated: bool = False, timeout: float = 300) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    if not elevated:
        return _run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], timeout)
    # One UAC prompt; waits for the elevated script and passes its exit code back.
    outer = (
        "$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{encoded}'; exit $p.ExitCode"
    )
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", outer], timeout)


def public_networks() -> list[dict]:
    """Active network connections Windows treats as Public (which blocks the phone)."""
    result = _powershell(
        "Get-NetConnectionProfile | Select-Object Name, InterfaceIndex, "
        "@{n='Category'; e={[string]$_.NetworkCategory}} | ConvertTo-Json -Compress"
    )
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    data = data if isinstance(data, list) else [data]
    return [d for d in data if d.get("Category") == "Public"]


def _quote(text: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def allow_through_firewall(exe: Path, make_private: list[dict]) -> bool:
    """Allow the program on private networks, and optionally make networks private. One UAC prompt."""
    lines = [
        f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue",
        f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -Direction Inbound -Action Allow "
        f"-Program {_quote(exe)} -Profile Private,Domain | Out-Null",
    ]
    lines += [f"Set-NetConnectionProfile -InterfaceIndex {int(n['InterfaceIndex'])} -NetworkCategory Private" for n in make_private]
    return _powershell("\n".join(lines), elevated=True).returncode == 0


def remove_firewall_rule() -> bool:
    return _powershell(f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue", elevated=True).returncode == 0


def add_apps_entry(exe: Path, folder: Path) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        values = {
            "DisplayName": "Claude Cam",
            "DisplayVersion": __version__,
            "Publisher": "ssjrocks",
            "URLInfoAbout": "https://github.com/ssjrocks/claude-cam",
            "InstallLocation": str(folder),
            "DisplayIcon": str(exe),
            "UninstallString": f'"{exe}" uninstall',
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def remove_apps_entry() -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def message_box(text: str, title: str = "Claude Cam") -> None:
    if WINDOWS:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION
    else:
        print(text)


# --- the phone page ---------------------------------------------------------------------------------


def write_phone_page(folder: Path, qr_png: Path | None) -> Path:
    qr = ""
    if qr_png and qr_png.exists():
        qr = f'<img src="data:image/png;base64,{base64.b64encode(qr_png.read_bytes()).decode()}" alt="QR code for the app" width="260" height="260">'
    page = folder / "phone-setup.html"
    page.write_text(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Claude Cam: phone setup</title>
<style>body{{font:18px/1.5 system-ui,sans-serif;background:#141413;color:#f0eee6;margin:0}}
main{{max-width:34rem;margin:0 auto;padding:2.5rem 1rem;text-align:center}}h1{{font-size:1.7rem}}
img{{background:#fff;padding:12px;border-radius:12px}}ol{{text-align:left}}li{{margin:.5rem 0}}a{{color:#d97757}}
.ok{{color:#6fbf73;font-weight:600}}</style></head><body><main>
<p class="ok">Claude Cam is installed on this computer.</p>
<h1>Now get the app on your Android phone</h1>
{qr}
<ol>
<li>Scan this code with your phone's camera and download <b>claude-cam.apk</b>.</li>
<li>Open it and tap <b>Install</b>. If Android asks, allow installing apps from your browser.</li>
<li>Open <b>Claude Cam</b>, allow the camera, and keep the phone on the same Wi-Fi as this computer.</li>
</ol>
<p>Then <b>quit Claude completely and open it again</b>, and ask: <i>"Can you see what my phone camera is pointed at?"</i></p>
<p><small>Download link: <a href="{APK_URL}">{APK_URL}</a></small></p>
</main></body></html>""",
        encoding="utf-8",
    )
    return page


def open_file(path: Path) -> None:
    try:
        if WINDOWS:
            os.startfile(str(path))  # noqa: S606 - opens our own local page in the default browser
        else:
            import webbrowser

            webbrowser.open(path.as_uri())
    except OSError:
        pass


# --- install and uninstall -------------------------------------------------------------------------


def _ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{question} [Y/n] ").strip().lower()
    except EOFError:
        return True
    return answer in ("", "y", "yes")


def claude_present() -> bool:
    return claude_json().exists() or claude_dir().exists() or find_claude() is not None


def install(payload: Path, *, assume_yes=False, firewall=True, browser=True, registry=True) -> int:
    say = print
    say(f"Setting up Claude Cam {__version__}\n")
    if not claude_present():
        say("Claude doesn't seem to be installed on this computer yet.")
        say("Install it from https://claude.com/download, open it once, then run this setup again.")
        if not assume_yes:
            try:
                input("\nPress Enter to close.")
            except EOFError:
                pass
        return 1
    root = install_root()
    target = root / __version__
    if target.exists():
        try:
            shutil.rmtree(target)
        except OSError:
            say("Claude Cam is in use. Quit Claude completely (check the system tray too), then run this again.")
            return 1
    say(f"1. Copying Claude Cam to {target}")
    with zipfile.ZipFile(payload) as z:
        z.extractall(target)
    exe = server_exe(target)
    if not WINDOWS:
        exe.chmod(0o755)
    for old in root.iterdir() if root.exists() else []:  # earlier versions, unless still running
        if old.is_dir() and old.name != __version__ and re.fullmatch(r"\d+(\.\d+)*", old.name):
            shutil.rmtree(old, ignore_errors=True)

    if plugin_installed():
        say("2. Claude Cam is already installed as a Claude Code plugin, so it's not added a second time.")
    else:
        say(f"2. Adding it to Claude {register(exe)}")
        skill = install_skill(target / "extras" / "SKILL.md")
        say(f"   and the phone-camera skill in {skill}")

    if WINDOWS and firewall:
        say("3. Letting your phone reach this computer (Windows will ask for permission)")
        make_private = []
        for net in public_networks():
            name = net.get("Name") or "your network"
            say(f"   Windows treats '{name}' as a public network, which blocks your phone.")
            if _ask(f"   Is '{name}' your home or another network you trust? Make it private", assume_yes):
                make_private.append(net)
        if allow_through_firewall(exe, make_private):
            say("   Done.")
        else:
            say("   Skipped. If Windows later asks whether Claude Cam may use the network, tick Private networks and click Allow.")
    if WINDOWS and registry:
        add_apps_entry(exe, root)

    page = write_phone_page(target, target / "extras" / "qr-apk.png")
    say("\nAll set on this computer. Now:")
    say("  - Quit Claude completely (check the system tray) and open it again.")
    if browser:
        say("  - Install the app on your phone: the page that just opened shows how.")
        open_file(page)
    else:
        say(f"  - Install the app on your phone: see {page}")
    if not assume_yes:
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
    return 0


def uninstall(*, assume_yes=False, firewall=True, registry=True) -> int:
    unregister()
    remove_skill()
    if WINDOWS and firewall:
        remove_firewall_rule()
    if WINDOWS and registry:
        remove_apps_entry()
    root = install_root()
    if WINDOWS and getattr(sys, "frozen", False):
        # This program runs from that folder, so delete it once we've exited.
        subprocess.Popen(
            f'cmd /c ping -n 3 127.0.0.1 >nul & rmdir /s /q "{root}"',
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
    else:
        shutil.rmtree(root, ignore_errors=True)
    if not assume_yes:
        message_box("Claude Cam has been removed. Restart Claude to finish.")
    return 0
