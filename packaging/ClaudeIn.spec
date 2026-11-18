# PyInstaller recipe for ClaudeIn.exe: a folder (not one big file, which would unpack itself at every start), no console.
# Built by packaging\build.py. Run from the project folder with the .venv's Python.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "build", "claudein.ico")

hidden = (collect_submodules("claudein") + collect_submodules("claude_agent_sdk")
          + ["win32com.client", "pythoncom", "pywintypes", "qasync"])
datas = collect_data_files("qfluentwidgets") + collect_data_files("qframelesswindow")

# Qt parts ClaudeIn never uses: they would only add size.
unused = ["tkinter", "pytest", "_pytest", "pytest_asyncio",
          "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
          "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
          "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtCharts",
          "PySide6.QtDataVisualization", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQml",
          "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtTest", "PySide6.QtSql"]

a = Analysis([os.path.join(SPECPATH, "launch.py")], pathex=[ROOT], datas=datas, hiddenimports=hidden, excludes=unused)

# The Claude Agent SDK carries its own 355 MB copy of Claude Code. ClaudeIn never uses it (it runs the Claude Code
# installed on the PC), so it is left out of the program.
a.binaries = [b for b in a.binaries if "_bundled" not in b[0].replace("\\", "/")]
a.datas = [d for d in a.datas if "_bundled" not in d[0].replace("\\", "/")]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ClaudeIn", console=False,
          icon=ICON if os.path.exists(ICON) else None)
coll = COLLECT(exe, a.binaries, a.datas, name="ClaudeIn")
