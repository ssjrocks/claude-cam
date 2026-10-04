"""Entry point of ClaudeCamSetup.exe: installs the bundled Claude Cam for the current user."""

import argparse
import sys
from pathlib import Path

from claude_cam import installer


def main() -> None:
    p = argparse.ArgumentParser(prog="ClaudeCamSetup", description="Set up Claude Cam for Claude on this computer.")
    p.add_argument("--yes", action="store_true", help="don't ask questions or wait for Enter")
    p.add_argument("--no-firewall", action="store_true", help="leave the firewall alone")
    p.add_argument("--no-browser", action="store_true", help="don't open the phone setup page")
    p.add_argument("--no-registry", action="store_true", help="don't add an entry to Settings > Apps")
    args = p.parse_args()
    payload = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "payload.zip"
    try:
        code = installer.install(
            payload,
            assume_yes=args.yes,
            firewall=not args.no_firewall,
            browser=not args.no_browser,
            registry=not args.no_registry,
        )
    except Exception as e:  # noqa: BLE001 - show the reason instead of a window that vanishes
        print(f"\nSetup failed: {e}\nPlease report it at https://github.com/ssjrocks/claude-cam/issues")
        if not args.yes:
            input("Press Enter to close.")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
