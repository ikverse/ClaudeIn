"""Makes ClaudeIn's icon file for the build: `python packaging/make_icon.py <where to save the .ico>`."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication

from claudein.tray import save_ico

if __name__ == "__main__":
    app = QGuiApplication(sys.argv[:1])  # noqa: F841  (needed for drawing)
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    save_ico(target)
    print("Icon saved:", target)
