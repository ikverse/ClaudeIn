"""Usage records and logs (plan 4.5 and 4.7).

usage.db gets one row per run: who asked, when, how long it waited and ran, how it ended, token counts, web searches and
fetches, API-price equivalent, the plan-limit use reported at its end, and its estimated plan points. Never the
question, the answer, codes, tokens or tool data. The same goes for the log files.

Plan points (plan 4.5, phase 2): 100 points = one full 5-hour window of the Claude plan. Claude Code reports only how
full the whole plan's window is, so each run's points are an estimate:
- the rise since the previous report goes to the runs that ended in between, split by their tokens, but only when
  ClaudeIn was busy the whole time since that report (no idle gap over 30 seconds). Otherwise the owner's own Claude
  use in the gap could be in the rise, so it isn't trusted;
- if the window reset in between (the number went down), the new number counts as the rise;
- if the number didn't move, or the rise isn't trusted, a run's points come from its tokens at the points-per-token
  rate measured today, else on the most recent day with a measured rate in the last 7 days, else 0.
A run that ends without a report waits; the next report shares its rise with it. Use of Claude outside ClaudeIn
*while* a question is running still lands on that question: Claude gives no way to tell the two apart.
"""

from __future__ import annotations

import logging
import logging.handlers
import sqlite3
import time
from pathlib import Path

from .runs import Run

ENDINGS = ("done", "stopped", "declined", "rounds", "cut short")
BUSY_GAP_S = 30  # the longest idle moment that still counts as "busy the whole time"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at REAL NOT NULL,          -- Unix time
    app TEXT NOT NULL,
    device TEXT NOT NULL,
    model TEXT NOT NULL,
    queued_ms INTEGER NOT NULL,
    run_ms INTEGER NOT NULL,
    ending TEXT NOT NULL,            -- done | stopped | declined | rounds | cut short | failed
    error_kind TEXT,
    input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    searches INTEGER NOT NULL DEFAULT 0,
    fetches INTEGER NOT NULL DEFAULT 0,
    api_price_usd REAL NOT NULL DEFAULT 0,
    five_hour REAL,
    seven_day REAL,
    plan_points REAL,                -- estimated; NULL until a plan report has come for it
    points_measured INTEGER NOT NULL DEFAULT 0,  -- 1 when from a measured rise, 0 when from tokens
    started_at REAL,                 -- Unix time it started (NULL: never started, or an older row)
    ended_at REAL                    -- Unix time it ended (NULL in older rows)
);
CREATE INDEX IF NOT EXISTS runs_app_time ON runs (app, asked_at);
"""

_ADDED_COLUMNS = {  # added after the first version; put into older usage.db files without losing rows
    "plan_points": "REAL",
    "points_measured": "INTEGER NOT NULL DEFAULT 0",
    "started_at": "REAL",
    "ended_at": "REAL",
}


def ending_kind(ending: str) -> str:
    """Only the kind of ending is kept: a "failed: …" text could quote anything."""
    return ending if ending in ENDINGS else "failed"


def start_of_today(now: float | None = None) -> float:
    """Midnight this morning, PC time."""
    t = time.localtime(time.time() if now is None else now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


class Usage:
    def __init__(self, path: Path, clock=time.time):
        self.clock = clock  # tests pass a pretend clock to check midnight
        self.db = sqlite3.connect(str(path))
        self.db.executescript(_SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        for col, kind in _ADDED_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} {kind}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record(self, run: Run, started_at: float | None = None, ended_at: float | None = None) -> None:
        """Writes the run's row. [started_at] and [ended_at] are worked out when not given (tests give them)."""
        d = run.result or {}
        u = d.get("usage") or {}
        lim = d.get("limits") or {}
        never_ran = run.started_mono is None  # refused before starting: it used nothing
        if ended_at is None:
            ended_at = self.clock()
        run_ms = 0
        if not never_ran:
            if started_at is None:
                run_ms = int((time.monotonic() - run.started_mono) * 1000)
                started_at = ended_at - run_ms / 1000
            else:
                run_ms = int((ended_at - started_at) * 1000)
        five = lim.get("five_hour")
        cur = self.db.execute(
            "INSERT INTO runs (asked_at, app, device, model, queued_ms, run_ms, ending, error_kind, input, output,"
            " cache_read, cache_write, searches, fetches, api_price_usd, five_hour, seven_day, plan_points,"
            " started_at, ended_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.asked_at, run.app_id, run.device_label, run.spec.model, int(d.get("queued_ms") or 0), run_ms,
             ending_kind(str(d.get("ending") or "")), d.get("error_kind"),
             u.get("input", 0), u.get("output", 0), u.get("cache_read", 0), u.get("cache_write", 0),
             u.get("searches", 0), u.get("fetches", 0), float(d.get("api_price_usd") or 0.0),
             five, lim.get("seven_day"), 0.0 if never_ran else None,
             None if never_ran else started_at, ended_at),
        )
        if isinstance(five, (int, float)) and not never_ran:
            self._share_points(cur.lastrowid, float(five))
        self.db.commit()

    # --- plan points ---------------------------------------------------------------------------------------------------

    def _share_points(self, row_id: int, five: float) -> None:
        """Gives out the rise in the 5-hour window reported at the end of run [row_id] (see the top of this file)."""
        prev = self.db.execute("SELECT id, five_hour, ended_at FROM runs WHERE five_hour IS NOT NULL AND id < ?"
                               " ORDER BY id DESC LIMIT 1", (row_id,)).fetchone()
        prev_id = prev[0] if prev else 0
        pending = self.db.execute(
            "SELECT id, input+output+cache_read+cache_write, started_at, ended_at FROM runs"
            " WHERE id > ? AND id <= ? AND plan_points IS NULL ORDER BY id", (prev_id, row_id)).fetchall()
        if not pending:
            return
        if prev is None:
            rise = None  # the first report ever: nothing to measure the rise from
        else:
            rise = five - prev[1] if five >= prev[1] else five  # went down: the window reset; the new number is the rise
        if rise is not None and rise > 0 and self._busy_since(prev[2], pending):
            points = rise * 100
            total = sum(p[1] for p in pending)
            for rid, tokens, _, _ in pending:
                share = tokens / total if total else 1 / len(pending)
                self.db.execute("UPDATE runs SET plan_points = ?, points_measured = 1 WHERE id = ?",
                                (points * share, rid))
            return
        rate = self.points_per_token()
        for rid, tokens, _, _ in pending:
            self.db.execute("UPDATE runs SET plan_points = ?, points_measured = 0 WHERE id = ?", (tokens * rate, rid))

    @staticmethod
    def _busy_since(report_at: float | None, pending: list) -> bool:
        """Was ClaudeIn running questions the whole time from the previous report until now, with no idle gap over
        BUSY_GAP_S? Rows without times (from before this check existed) count as not busy."""
        if report_at is None or any(p[2] is None or p[3] is None for p in pending):
            return False
        covered_to = report_at
        for _, _, start, end in sorted(pending, key=lambda p: p[2]):
            if start > covered_to + BUSY_GAP_S:
                return False  # idle in between: the owner may have used Claude then
            covered_to = max(covered_to, end)
        return True

    def points_per_token(self) -> float:
        """The points-per-token rate from runs whose points came from a real rise: today's, else that of the most
        recent day in the last 7 days that had one, else 0."""
        today = start_of_today(self.clock())

        def rate_since(since: float, until: float) -> float:
            r = self.db.execute(
                "SELECT SUM(plan_points), SUM(input+output+cache_read+cache_write) FROM runs"
                " WHERE points_measured = 1 AND asked_at >= ? AND asked_at < ?", (since, until)).fetchone()
            return (r[0] / r[1]) if r and r[0] and r[1] else 0.0

        r = rate_since(today, float("inf"))
        if r:
            return r
        last = self.db.execute("SELECT MAX(asked_at) FROM runs WHERE points_measured = 1 AND asked_at >= ?"
                               " AND asked_at < ?", (today - 7 * 86400, today)).fetchone()[0]
        if last is None:
            return 0.0
        day = start_of_today(last)
        return rate_since(day, day + 86400)

    def points_today(self, app: str) -> float:
        r = self.db.execute("SELECT SUM(plan_points) FROM runs WHERE app = ? AND asked_at >= ?",
                            (app, start_of_today(self.clock()))).fetchone()
        return float(r[0] or 0.0)

    # --- for the Usage window -----------------------------------------------------------------------------------------

    def totals(self, since: float) -> dict[str, dict]:
        """Per app since [since]: questions, tokens (all four kinds together) and API-price USD."""
        rows = self.db.execute(
            "SELECT app, COUNT(*), SUM(input+output+cache_read+cache_write), SUM(api_price_usd), SUM(plan_points)"
            " FROM runs WHERE asked_at >= ? GROUP BY app", (since,)).fetchall()
        return {app: {"questions": n, "tokens": int(tok or 0), "api_price_usd": float(usd or 0),
                      "plan_points": float(pts or 0)}
                for app, n, tok, usd, pts in rows}

    def recent(self, app: str, limit: int = 20) -> list[dict]:
        cols = ("asked_at", "device", "model", "queued_ms", "run_ms", "ending", "error_kind")
        rows = self.db.execute(
            f"SELECT {', '.join(cols)} FROM runs WHERE app = ? ORDER BY asked_at DESC LIMIT ?", (app, limit)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def latest_limits(self) -> dict | None:
        r = self.db.execute("SELECT five_hour, seven_day, asked_at FROM runs WHERE five_hour IS NOT NULL"
                            " ORDER BY id DESC LIMIT 1").fetchone()
        return {"five_hour": r[0], "seven_day": r[1], "at": r[2]} if r else None


def setup_logs(folder: Path) -> logging.Logger:
    """logs\\claudein.log, a new file each day, kept 14 days."""
    logs = folder / "logs"
    logs.mkdir(exist_ok=True)
    log = logging.getLogger("claudein")
    log.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in log.handlers):
        h = logging.handlers.TimedRotatingFileHandler(logs / "claudein.log", when="midnight", backupCount=14,
                                                      encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    return log


def log_run(log: logging.Logger, run: Run) -> None:
    """One line per run: who, which model, how long, how it ended, token counts. Nothing else."""
    d = run.result or {}
    u = d.get("usage") or {}
    log.info("run app=%s device=%s model=%s waited_ms=%s ending=%s kind=%s in=%s out=%s cache_r=%s cache_w=%s",
             run.app_id, run.device_label, run.spec.model, d.get("queued_ms"), ending_kind(str(d.get("ending") or "")),
             d.get("error_kind") or "-", u.get("input", 0), u.get("output", 0), u.get("cache_read", 0),
             u.get("cache_write", 0))
