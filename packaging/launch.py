"""The program PyInstaller turns into ClaudeIn.exe: starts ClaudeIn exactly as `pythonw -m claudein` does.

ClaudeIn.exe has no console window, so an early failure (before ClaudeIn's own logging starts) has nowhere to show. It is
written to %USERPROFILE%\\.claudein\\startup-error.txt instead.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def run() -> int:
    try:
        from claudein.__main__ import main

        return main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        try:
            folder = Path(os.environ.get("CLAUDEIN_HOME") or Path.home() / ".claudein")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "startup-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(run())
