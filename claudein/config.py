"""Where ClaudeIn keeps its files, and its settings (settings.json)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

VERSION = "0.1.0"

#: What /hello says as "app". Today's phones only accept this name (plan 4.2).
HELLO_APP = "Signal Lab analyst host"
PROTOCOL = 1


def home() -> Path:
    """%USERPROFILE%\\.claudein, or CLAUDEIN_HOME when set (tests)."""
    override = os.environ.get("CLAUDEIN_HOME")
    path = Path(override) if override else Path.home() / ".claudein"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_atomic(path: Path, text: str) -> None:
    """Writes the whole file or nothing, so a crash never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Settings:
    phone_port: int = 8790
    local_port: int = 8791
    workers: int = 2  # runs at once, 1-4
    max_waiting: int = 20
    wait_limit_min: float = 10  # longest time in line before giving up
    run_limit_min: float = 15  # longest a run may last
    keep_awake: bool = False  # keep the PC awake all the time, not only while busy
    paused: bool = False
    own_login: bool = True  # ClaudeIn keeps its own Claude login (.claudein\claude), apart from the terminal's
    claude_copy: str = "pc"  # "pc": the Claude Code installed on this PC; "own": the copy ClaudeIn downloaded for itself

    def clamp(self) -> "Settings":
        self.workers = max(1, min(4, int(self.workers)))
        self.max_waiting = max(0, int(self.max_waiting))
        if self.claude_copy not in ("pc", "own"):
            self.claude_copy = "pc"
        return self

    @classmethod
    def load(cls, folder: Path | None = None) -> "Settings":
        path = (folder or home()) / "settings.json"
        known = {f.name for f in fields(cls)}
        data = {}
        if path.exists():
            try:
                data = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k in known}
            except (OSError, ValueError):
                data = {}
        s = cls(**data).clamp()
        if not path.exists():
            s.save(folder)
        return s

    def save(self, folder: Path | None = None) -> None:
        write_atomic((folder or home()) / "settings.json", json.dumps(asdict(self), indent=2))
