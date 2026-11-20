"""The Usage dashboard: its filtered sums and the page itself."""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from PySide6.QtWidgets import QApplication

from claudein.usage import Usage, period_key, start_of_today


def add(u: Usage, asked_at: float, app="village", device="This PC", model="sonnet", ending="done", kind=None,
        tokens=1000, points=1.0, usd=0.01, queued=0, run=2000) -> None:
    u.db.execute("INSERT INTO runs (asked_at, app, device, model, queued_ms, run_ms, ending, error_kind, input,"
                 " api_price_usd, plan_points, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (asked_at, app, device, model, queued, run, ending, kind, tokens, usd, points, asked_at,
                  asked_at + run / 1000))
    u.db.commit()


@pytest.fixture
def u(tmp_path):
    usage = Usage(tmp_path / "usage.db")
    now = time.time()
    add(usage, now - 60, app="signal-lab", device="Phone", model="opus", tokens=5000, points=4.0, queued=4000)
    add(usage, now - 120, app="signal-lab", device="Phone", ending="failed", kind="timeout", tokens=0, points=0.0)
    add(usage, now - 180, app="village")
    add(usage, now - 3 * 86400, app="village", model="haiku", tokens=200, points=0.2)
    add(usage, now - 40 * 86400, app="village", ending="stopped")
    yield usage
    usage.close()


def test_summary_follows_the_filters(u):
    today = start_of_today()
    assert u.summary(today)["questions"] == 3
    assert u.summary(0)["questions"] == 5
    s = u.summary(today, {"app": "signal-lab"})
    assert s["questions"] == 2 and s["tokens"] == 5000 and s["plan_points"] == pytest.approx(4.0)
    assert s["done"] == 1 and s["avg_queued_ms"] == pytest.approx(2000)
    assert u.summary(0, {"app": "signal-lab", "ending": "failed"})["questions"] == 1
    assert u.summary(0, {"device": "Phone", "model": "opus"})["questions"] == 1
    assert u.summary(0, {"app": ""})["questions"] == 5  # empty means all
    assert u.summary(today, {"app": "nobody"})["questions"] == 0


def test_only_known_columns_can_be_filtered(u):
    with pytest.raises(ValueError):
        u.summary(0, {"prompt": "x"})
    with pytest.raises(ValueError):
        u.breakdown(0, None, "ending; DROP TABLE runs")


def test_breakdown_endings_and_runs(u):
    by_app = {r["key"]: r for r in u.breakdown(0, None, "app")}
    assert by_app["village"]["questions"] == 3 and by_app["signal-lab"]["failed"] == 1
    assert [r["key"] for r in u.breakdown(0, None, "model")][0] == "sonnet"
    e = u.endings(0)
    assert e["endings"] == {"done": 3, "failed": 1, "stopped": 1} and e["failures"] == {"timeout": 1}
    runs = u.runs(0, {"app": "village"}, limit=2)
    assert len(runs) == 2 and runs[0]["asked_at"] > runs[1]["asked_at"]
    assert set(runs[0]) >= {"device", "model", "tokens", "plan_points", "ending"}
    assert u.choices()["app"] == ["signal-lab", "village"]


def test_series_keys_match_the_buckets(u):
    now = time.time()
    sums = u.series(0, None, "day")
    assert sums[(period_key(now - 3 * 86400, "day"), "village")]["questions"] == 1
    assert sum(v["questions"] for v in sums.values()) == 5
    months = u.series(0, None, "month")
    assert sum(v["questions"] for v in months.values()) == 5


def test_version_changes_with_new_rows(u):
    before = u.version()
    add(u, time.time())
    assert u.version() != before


def test_periods_and_buckets():
    from claudein.windows import buckets, period_start

    now = datetime(2026, 11, 20, 16, 30).timestamp()
    assert period_start("today", now) == datetime(2026, 11, 20).timestamp()
    assert period_start("7", now) == datetime(2026, 11, 14).timestamp()
    assert period_start("all", now) == 0
    step, keys, labels = buckets("today", None, now)
    assert step == "hour" and len(keys) == 17 and labels[-1] == "16:00"
    step, keys, labels = buckets("7", None, now)
    assert step == "day" and len(keys) == 7 and keys[-1] == "2026-11-20" and labels[0] == "14 Nov"
    assert len(buckets("30", None, now)[1]) == 30
    assert buckets("all", None, now)[1] == []
    assert buckets("all", datetime(2026, 11, 1).timestamp(), now)[0] == "day"
    step, keys, _ = buckets("all", datetime(2026, 1, 5).timestamp(), now)
    assert step == "month" and keys[0] == "2026-01" and keys[-1] == "2026-11"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


async def test_the_usage_page_filters(qapp, svc):
    from claudein.windows import UsagePage

    now = time.time()
    add(svc.usage, now - 60, app="signal-lab", device="Phone", model="opus")
    add(svc.usage, now - 120, app="village", ending="failed", kind="timeout")
    add(svc.usage, now - 10 * 86400, app="village")
    page = UsagePage(svc)
    page.refresh()
    assert page.kpi["questions"].text() == "2"  # 7 days
    assert page.table.rowCount() == 2 and page.recent.rowCount() == 2
    assert page.pick["app"].count() == 3  # All + two apps

    page.set_filter("app", "village")
    assert page.kpi["questions"].text() == "1" and page.recent.rowCount() == 1
    assert "failed (timeout)" in page.recent.item(0, 8).text()
    assert page.kpi["ok"].text() == "0%"

    page._set_period("all")
    assert page.kpi["questions"].text() == "2"
    page._row_clicked(0, 0)  # by app: the one row is village, already picked
    assert page.filters["app"] == "village"

    page.group.setCurrentIndex(1)  # by device
    assert page._rows == ["This PC"]
    page.measure.setCurrentIndex(1)  # tokens
    assert page.chart.series and page.chart.labels

    page.reset()
    assert page.filters == {"app": "", "device": "", "model": "", "ending": ""} and page.period == "7"
    assert page.kpi["questions"].text() == "2"

    sig = page._sig
    page.refresh()
    assert page._sig == sig  # nothing changed: nothing redrawn
    add(svc.usage, time.time())
    page.refresh()
    assert page.kpi["questions"].text() == "3"
    page.chart.resize(600, 260)
    page.chart.grab()  # draws without trouble
    page.share.resize(300, 100)
    page.share.grab()
