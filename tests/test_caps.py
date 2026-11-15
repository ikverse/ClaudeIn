"""Phase 2: plan points per app and daily caps."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from claudein import runs
from claudein.runs import Run, RunSpec
from claudein.usage import Usage, start_of_today

from .conftest import rate_limit, result, spec, until


def run_with(app: str = "village", tokens: int = 100, five: float | None = None, started: bool = True,
             asked_at: float | None = None) -> Run:
    r = Run(RunSpec(model="claude-opus-5-5", system="s", prompt="p"), app, app, None, f"{app} (this PC)", False)
    if started:
        r.started_mono = time.monotonic()
    if asked_at is not None:
        r.asked_at = asked_at
    limits = None
    if five is not None:
        limits = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": five}}}
    r.finish(runs.done("done", usage={"input_tokens": tokens} if started else None, limits=limits))
    return r


def points(u: Usage) -> list[float | None]:
    return [r[0] for r in u.db.execute("SELECT plan_points FROM runs ORDER BY id")]


@pytest.fixture
def u(tmp_path):
    usage = Usage(tmp_path / "usage.db")
    yield usage
    usage.close()


def test_one_run_gets_the_rise(u):
    u.record(run_with(five=0.30))  # the first report: nothing to measure from
    u.record(run_with(tokens=100, five=0.35))
    assert points(u) == [0.0, pytest.approx(5.0)]


def test_runs_between_reports_share_by_tokens(u):
    u.record(run_with(five=0.30))
    u.record(run_with("signal-lab", tokens=100, five=None))  # ended without a report: waits
    assert points(u)[1] is None
    u.record(run_with("village", tokens=200, five=0.36))
    assert points(u)[1:] == [pytest.approx(2.0), pytest.approx(4.0)]
    assert u.points_today("signal-lab") == pytest.approx(2.0)


def test_no_change_uses_todays_rate(u):
    u.record(run_with(five=0.30))
    u.record(run_with(tokens=100, five=0.35))  # 5 points for 100 tokens: 0.05 a token
    u.record(run_with(tokens=40, five=0.35))  # no change
    assert points(u)[2] == pytest.approx(2.0)


def test_no_rate_yet_counts_zero(u):
    u.record(run_with(five=0.30))
    u.record(run_with(tokens=40, five=0.30))
    assert points(u) == [0.0, 0.0]


def test_window_reset(u):
    u.record(run_with(five=0.90))
    u.record(run_with(tokens=100, five=0.05))  # went down: the window reset, so the new number is the rise
    assert points(u)[1] == pytest.approx(5.0)


def test_refused_run_counts_nothing(u):
    u.record(run_with(five=0.30))
    u.record(run_with("signal-lab", started=False))  # refused before starting
    u.record(run_with("village", tokens=100, five=0.40))
    assert points(u) == [0.0, 0.0, pytest.approx(10.0)]


def test_points_reset_at_midnight(tmp_path):
    now = [time.time()]
    u = Usage(tmp_path / "usage.db", clock=lambda: now[0])
    try:
        u.record(run_with(five=0.30, asked_at=now[0]))
        u.record(run_with(tokens=100, five=0.40, asked_at=now[0]))
        assert u.points_today("village") == pytest.approx(10.0)
        now[0] = start_of_today(now[0]) + 86400 + 60  # one minute past next midnight
        assert u.points_today("village") == 0.0
    finally:
        u.close()


def test_old_usage_db_still_opens(tmp_path):
    path = tmp_path / "usage.db"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, asked_at REAL NOT NULL,
        app TEXT NOT NULL, device TEXT NOT NULL, model TEXT NOT NULL, queued_ms INTEGER NOT NULL,
        run_ms INTEGER NOT NULL, ending TEXT NOT NULL, error_kind TEXT, input INTEGER NOT NULL DEFAULT 0,
        output INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
        searches INTEGER NOT NULL DEFAULT 0, fetches INTEGER NOT NULL DEFAULT 0, api_price_usd REAL NOT NULL DEFAULT 0,
        five_hour REAL, seven_day REAL);
        INSERT INTO runs (asked_at, app, device, model, queued_ms, run_ms, ending) VALUES (1, 'old', 'd', 'm', 0, 0, 'done');""")
    db.commit()
    db.close()
    u = Usage(path)
    try:
        assert u.db.execute("SELECT app, plan_points FROM runs").fetchall() == [("old", None)]
        u.record(run_with(five=0.3))
        assert u.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    finally:
        u.close()


# --- trusting a rise only when ClaudeIn was busy the whole time ---------------------------------------------------------

T0 = 1_800_000_000.0  # a fixed moment, so the timing tests are exact


def rec(u: Usage, start: float, end: float, five: float | None, tokens: int = 100, app: str = "village") -> None:
    u.record(run_with(app, tokens=tokens, five=five, asked_at=T0 + start), started_at=T0 + start, ended_at=T0 + end)


@pytest.fixture
def ut(tmp_path):
    usage = Usage(tmp_path / "usage.db", clock=lambda: T0 + 3600)
    yield usage
    usage.close()


def test_back_to_back_rise_is_trusted(ut):
    rec(ut, 0, 10, 0.30)
    rec(ut, 12, 40, 0.35)  # started 2 s after the last report
    assert points(ut)[1] == pytest.approx(5.0)


def test_overlapping_questions_are_trusted(ut):
    rec(ut, 0, 10, 0.30)
    rec(ut, 5, 60, None, tokens=100)  # already going when the last report came
    rec(ut, 50, 70, 0.36, tokens=200)
    assert points(ut)[1:] == [pytest.approx(2.0), pytest.approx(4.0)]


def test_short_gap_still_trusted(ut):
    rec(ut, 0, 10, 0.30)
    rec(ut, 39, 60, 0.35)  # 29 s idle: under 30
    assert points(ut)[1] == pytest.approx(5.0)


def test_idle_gap_not_charged(ut):
    rec(ut, 0, 10, 0.30)
    rec(ut, 12, 40, 0.35, tokens=100)  # measured: 5 points for 100 tokens, so 0.05 a token
    rec(ut, 600, 620, 0.80, tokens=40)  # idle 9 minutes, during which the owner used Claude: the rise isn't trusted
    assert points(ut)[2] == pytest.approx(2.0)  # 40 tokens x 0.05, not 45 points


def test_yesterdays_rate_when_none_today(tmp_path):
    now = [T0]
    u = Usage(tmp_path / "usage.db", clock=lambda: now[0])
    try:
        rec(u, 0, 10, 0.30)
        rec(u, 12, 40, 0.35)  # 0.05 a token, measured "yesterday"
        now[0] = start_of_today(T0) + 86400 + 3600  # next day
        u.record(run_with(tokens=60, five=0.10, asked_at=now[0]), started_at=now[0], ended_at=now[0] + 5)  # idle gap
        assert points(u)[2] == pytest.approx(3.0)
    finally:
        u.close()


def test_nothing_measured_in_7_days_is_zero(tmp_path):
    now = [T0]
    u = Usage(tmp_path / "usage.db", clock=lambda: now[0])
    try:
        rec(u, 0, 10, 0.30)
        rec(u, 12, 40, 0.35)
        now[0] = start_of_today(T0) + 9 * 86400  # 9 days later
        u.record(run_with(tokens=60, five=0.10, asked_at=now[0]), started_at=now[0], ended_at=now[0] + 5)
        assert points(u)[2] == 0.0
    finally:
        u.close()


def test_old_rows_count_as_not_busy(ut):
    rec(ut, 0, 10, 0.30)
    rec(ut, 12, 40, 0.35)  # 0.05 a token
    ut.db.execute("UPDATE runs SET ended_at = NULL WHERE id = 2")  # as if written before this check existed
    rec(ut, 41, 60, 0.45, tokens=20)
    assert points(ut)[2] == pytest.approx(1.0)  # 20 x 0.05, not the 10-point rise


def test_caps_are_stored(svc):
    p = svc.pairings
    p.set_cap("village", 60)
    assert p.cap("village") == 60
    p.load()
    assert p.cap("village") == 60
    p.set_cap("village", None)
    assert p.cap("village") is None
    with pytest.raises(ValueError):
        p.set_cap("village", -1)


def rising(fake, steps):
    """Each run reports the next 5-hour number in [steps]."""
    it = iter(steps)

    async def script(prompt, options, tools):
        yield rate_limit(five=next(it))
        yield result("ok")

    fake.script = script


async def test_capped_app_refused_other_still_works(svc, phone, program, fake):
    rising(fake, [0.10, 0.20, 0.25, 0.30])  # one for each question that reaches Claude Code
    await phone.ask(spec())  # first report: 0 points
    await phone.ask(spec())  # +10 points for Signal Lab
    svc.pairings.set_cap("signal-lab", 5)
    done = (await phone.ask(spec()))[-1]
    assert done["ending"] == "failed: Today's limit for Signal Lab is used up (resets at midnight)."
    assert done["error_kind"] == "cap"
    assert (await program.ask(spec(app="village")))[-1]["ending"] == "done"  # no cap: never refused
    svc.pairings.set_cap("signal-lab", None)
    assert (await phone.ask(spec()))[-1]["ending"] == "done"


async def test_question_going_finishes_over_cap(svc, phone, fake):
    fake.gate = asyncio.Event()
    going = asyncio.create_task(phone.ask(spec()))
    await until(lambda: len(svc.scheduler.running) == 1)
    svc.pairings.set_cap("signal-lab", 0)  # cap reached (0 >= 0) while it is going
    refused = (await phone.ask(spec()))[-1]
    assert refused["error_kind"] == "cap"
    fake.gate.set()
    assert (await going)[-1]["ending"] == "done"


async def test_waiting_question_refused_when_cap_reached(svc, phone, program, fake):
    svc.scheduler.set_workers(1)
    fake.gate = asyncio.Event()
    first = asyncio.create_task(program.ask(spec(app="village")))
    await until(lambda: len(svc.scheduler.running) == 1)
    waiting = asyncio.create_task(phone.ask(spec()))
    await until(lambda: len(svc.scheduler.waiting) == 1)
    svc.pairings.set_cap("signal-lab", 0)
    fake.gate.set()
    await first
    assert (await waiting)[-1]["error_kind"] == "cap"
    assert fake.started == ["Say ready"]  # the waiting one never reached Claude Code


async def test_check_claude_code_is_never_capped(svc):
    svc.pairings.set_cap("claudein", 0)
    assert (await svc.check_claude())["ending"] == "done"


async def test_points_in_totals(svc, phone, fake):
    rising(fake, [0.10, 0.13])
    await phone.ask(spec())
    await phone.ask(spec())
    assert svc.usage.totals(0)["signal-lab"]["plan_points"] == pytest.approx(3.0)
