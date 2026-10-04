"""End-to-end test of the built setup program, in a throwaway home. CI runs it on Windows.

    python installer/smoke_test.py dist/ClaudeCamSetup.exe

Installs, checks the Claude registration and the skill, talks MCP to the installed server over
stdio, asks Claude Code itself to health-check the server (if a `claude` command is available),
then uninstalls and checks that the registration is gone.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def check(ok: bool, what: str) -> None:
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        sys.exit(1)


def mcp_session(exe: str, env: dict) -> None:
    p = subprocess.Popen(
        [exe, "stdio", "--port", "18777", "--no-mdns"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )

    def rpc(i: int, method: str, params: dict) -> dict:
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n").encode())
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}})
    check(init["result"]["serverInfo"]["name"] == "claude-cam", f"MCP initialize ({init['result']['serverInfo']['version']})")
    tools = [t["name"] for t in rpc(2, "tools/list", {})["result"]["tools"]]
    check({"camera_status", "camera_record_video", "camera_video_frames"} <= set(tools), f"{len(tools)} tools listed")
    status = rpc(3, "tools/call", {"name": "camera_status", "arguments": {}})["result"]
    check("No phone is connected" in status["content"][0]["text"], "camera_status answers (no phone)")
    p.stdin.close()
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        p.kill()


def main() -> None:
    setup = str(Path(sys.argv[1]).resolve())
    tmp = Path(tempfile.mkdtemp(prefix="claude-cam-smoke-"))
    env = dict(os.environ)
    env.update(
        CLAUDE_CONFIG_DIR=str(tmp / "claude"),
        CLAUDE_CAM_INSTALL_DIR=str(tmp / "app"),
        CLAUDE_CAM_RECORDINGS=str(tmp / "videos"),
    )
    (tmp / "claude").mkdir()  # as if Claude were installed and had been opened once
    cli = shutil.which("claude")
    print(f"claude command: {cli or 'not found (the installer will edit the settings file)'}")

    r = subprocess.run([setup, "--yes", "--no-firewall", "--no-browser", "--no-registry"], env=env, capture_output=True, text=True, timeout=900)
    print(r.stdout, r.stderr)
    check(r.returncode == 0, "setup finished")

    config = json.loads((tmp / "claude" / ".claude.json").read_text(encoding="utf-8"))
    entry = config.get("mcpServers", {}).get("claude-cam")
    check(entry is not None and entry.get("args") == ["stdio"], "registered as a user MCP server")
    exe = entry["command"]
    check(Path(exe).is_file(), f"server installed at {exe}")
    check((tmp / "claude" / "skills" / "phone-camera" / "SKILL.md").is_file(), "skill installed")

    mcp_session(exe, env)

    if cli:
        listing = subprocess.run([cli, "mcp", "list"], env=env, capture_output=True, text=True, timeout=300)
        line = next((ln for ln in listing.stdout.splitlines() if ln.startswith("claude-cam")), "")
        print(f"claude mcp list: {line}")
        check("Connected" in line, "Claude Code connects to it")

    r = subprocess.run([exe, "uninstall", "--yes", "--no-firewall", "--no-registry"], env=env, capture_output=True, text=True, timeout=300)
    check(r.returncode == 0, "uninstall finished")
    config = json.loads((tmp / "claude" / ".claude.json").read_text(encoding="utf-8"))
    check("claude-cam" not in config.get("mcpServers", {}), "registration removed")
    check(not (tmp / "claude" / "skills" / "phone-camera").exists(), "skill removed")
    for _ in range(20):  # on Windows the folder is deleted just after the program exits
        if not (tmp / "app").exists():
            break
        time.sleep(0.5)
    check(not (tmp / "app").exists(), "program folder removed")
    print("All checks passed.")


if __name__ == "__main__":
    main()
