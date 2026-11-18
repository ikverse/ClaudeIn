"""The queue (plan 4.4): N runs at once (2 by default), the rest wait in order, phone questions first.

- A waiting phone run starts before any waiting program run; within each group runs keep their order.
- Background runs (phase 8b), from phones and programs alike, wait behind every interactive run, in arrival order.
  They have their own limit of `max_waiting` waiting and a longer wait limit, so they never crowd out a person.
- A program's run already going is never stopped for a phone.
- At most `max_waiting` wait; more are refused. A run waiting longer than the wait limit gives up.
- A run lasting longer than the run limit is stopped.
- While anything is going or waiting, the PC is kept awake.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from . import runs, winproc
from .config import Settings
from .runs import Run

log = logging.getLogger("claudein")

WAIT_NOTE_EVERY_S = 15  # how often a waiting run hears where it stands, besides whenever that changes

PAUSED = "failed: ClaudeIn is paused on the PC."
FULL = "failed: The PC has too many questions waiting."
TOO_LONG_IN_LINE = "failed: The PC was busy for too long. Try again."


def cap_used_up(app_name: str) -> str:
    return f"failed: Today's limit for {app_name} is used up (resets at midnight)."


class Scheduler:
    def __init__(self, settings: Settings, execute: Callable[[Run], Awaitable[dict]],
                 on_finished: Callable[[Run], None] | None = None, on_change: Callable[[], None] | None = None,
                 over_cap: Callable[[Run], bool] | None = None):
        self.settings = settings
        self.execute = execute
        self.over_cap = over_cap or (lambda run: False)  # phase 2: has this run's app used up today's cap?
        self.on_finished = on_finished or (lambda run: None)
        self.on_change = on_change or (lambda: None)
        self.phone_waiting: deque[Run] = deque()
        self.program_waiting: deque[Run] = deque()
        self.background_waiting: deque[Run] = deque()
        self.running: dict[str, tuple[Run, asyncio.Task]] = {}
        self._watchers: dict[str, asyncio.Task] = {}
        self._awake = False

    # --- what the tray and /check show ----------------------------------------------------------------------------------

    @property
    def waiting(self) -> list[Run]:
        """Everything in line, in the order it will start."""
        return list(self.phone_waiting) + list(self.program_waiting) + list(self.background_waiting)

    @property
    def full(self) -> bool:
        """The line for interactive runs (people) is full."""
        return len(self.phone_waiting) + len(self.program_waiting) >= self.settings.max_waiting

    @property
    def background_full(self) -> bool:
        return len(self.background_waiting) >= self.settings.max_waiting

    def _queue_for(self, run: Run) -> deque[Run]:
        if run.spec.priority == "background":
            return self.background_waiting
        return self.phone_waiting if run.is_phone else self.program_waiting

    def all_runs(self) -> list[Run]:
        return [r for r, _ in self.running.values()] + self.waiting

    # --- asking ---------------------------------------------------------------------------------------------------------

    def submit(self, run: Run) -> None:
        """Puts [run] in line (or starts it). Refusals end the run at once with a done event."""
        run.on_close = self._closed
        if self.settings.paused:
            self._refuse(run, PAUSED, "paused")
            return
        if self.over_cap(run):
            self._refuse(run, cap_used_up(run.app_name), "cap")
            return
        background = run.spec.priority == "background"
        if len(self.running) >= self.settings.workers and (self.background_full if background else self.full):
            self._refuse(run, FULL, "unavailable")
            return
        self._queue_for(run).append(run)
        self._watchers[run.id] = asyncio.create_task(self._watch_waiting(run))
        self._pump()

    def _refuse(self, run: Run, ending: str, kind: str) -> None:
        run.finish(runs.done(ending, error_kind=kind))
        self.on_finished(run)

    def _position(self, run: Run) -> int:
        """How many are ahead of [run] in line."""
        order = self.waiting
        return order.index(run) if run in order else 0

    async def _watch_waiting(self, run: Run) -> None:
        """Tells a waiting run where it stands, and gives up after the wait limit."""
        minutes = (self.settings.background_wait_limit_min if run.spec.priority == "background"
                   else self.settings.wait_limit_min)
        limit = minutes * 60
        loop = asyncio.get_running_loop()
        started = loop.time()
        last_ahead = None
        last_note = 0.0
        try:
            while True:
                now = loop.time()
                if now - started >= limit:
                    self._remove_waiting(run)
                    self._refuse(run, TOO_LONG_IN_LINE, "unavailable")
                    return
                ahead = self._position(run)
                if ahead != last_ahead or now - last_note >= WAIT_NOTE_EVERY_S:
                    run.emit(runs.working(f"Waiting for the PC ({ahead} ahead)…"))
                    last_ahead, last_note = ahead, now
                await asyncio.sleep(min(1.0, max(0.05, limit - (now - started))))
        except asyncio.CancelledError:
            pass

    def _remove_waiting(self, run: Run) -> None:
        for q in (self.phone_waiting, self.program_waiting, self.background_waiting):
            if run in q:
                q.remove(run)
        w = self._watchers.pop(run.id, None)
        if w is not None and w is not asyncio.current_task():
            w.cancel()
        self._update_awake()
        self.on_change()

    def _pump(self) -> None:
        while len(self.running) < self.settings.workers and self.waiting:
            run = (self.phone_waiting or self.program_waiting or self.background_waiting).popleft()
            w = self._watchers.pop(run.id, None)
            if w is not None:
                w.cancel()
            if self.over_cap(run):  # its app reached the cap while it waited
                self._refuse(run, cap_used_up(run.app_name), "cap")
                continue
            run.started_mono = time.monotonic()
            task = asyncio.create_task(self._work(run))
            self.running[run.id] = (run, task)
        self._update_awake()
        self.on_change()

    async def _work(self, run: Run) -> None:
        try:
            try:
                d = await asyncio.wait_for(self.execute(run), self.settings.run_limit_min * 60)
            except asyncio.TimeoutError:
                mins = self.settings.run_limit_min
                d = runs.done(f"failed: The question took longer than {mins:g} minutes and was stopped.",
                              error_kind="limit")
            except asyncio.CancelledError:
                d = runs.done(run.close_ending, error_kind=run.close_kind)
            except Exception as e:  # never leave a phone waiting forever
                log.exception("Run failed inside ClaudeIn")
                d = runs.done(f"failed: {type(e).__name__}", error_kind="service")
            if run.closed:  # stopped while running: say so, whatever Claude Code managed
                d = runs.done(run.close_ending, error_kind=run.close_kind)
            run.finish(d)
            self.on_finished(run)
        finally:
            self.running.pop(run.id, None)
            self._pump()

    def _closed(self, run: Run) -> None:
        """A run was stopped: leave the line, or end the Claude Code run."""
        if run.id in self.running:
            self.running[run.id][1].cancel()
        elif run in self.waiting:
            self._remove_waiting(run)
            run.finish(runs.done(run.close_ending, error_kind=run.close_kind))
            self.on_finished(run)
        else:
            run.finish(runs.done(run.close_ending, error_kind=run.close_kind))

    # --- owner's controls ----------------------------------------------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        """Paused: new runs are refused, waiting ones end with the paused message, running ones finish."""
        self.settings.paused = paused
        self.settings.save()
        if paused:
            for run in self.waiting:
                self._remove_waiting(run)
                self._refuse(run, PAUSED, "paused")
        self.on_change()

    def set_workers(self, n: int) -> None:
        self.settings.workers = max(1, min(4, int(n)))
        self.settings.save()
        self._pump()

    def stop_device(self, device_id: str) -> None:
        """Forget: that device's runs stop at once."""
        for run in self.all_runs():
            if run.device_id == device_id:
                run.close()

    def set_keep_awake(self, on: bool) -> None:
        self.settings.keep_awake = on
        self.settings.save()
        self._update_awake()

    def _update_awake(self) -> None:
        want = self.settings.keep_awake or bool(self.running) or bool(self.waiting)
        if want != self._awake:
            winproc.keep_awake(want)
            self._awake = want

    async def shutdown(self) -> None:
        for run in self.all_runs():
            run.close()
        tasks = [t for _, t in self.running.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._awake:
            winproc.keep_awake(False)
            self._awake = False
