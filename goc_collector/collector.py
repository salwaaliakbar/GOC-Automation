"""Collection engine: smart resume, sequential processing, stop rule, Excel export.

Every result is written to the database immediately (see :mod:`store`). The
Excel file is an export, written on pause, stop and finish (and on download);
if it is open in Excel, the export is retried in the background until it works.

Stopping rule (no fixed end number):
  GOC numbers are issued in blocks, so the register contains long runs of
  numbers with no record (several hundred) between populated ranges. The
  collector keeps going until ``tail_gap`` consecutive numbers after the
  highest number that has a record are confirmed empty, then checks a further
  ``lookahead`` numbers in wide batches. If that finds anything, sequential
  collection continues (nothing is skipped) and the rule is applied again.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import goc_numbers as gn
from .locking import CollectionLock, CollectionLocked  # noqa: F401  (re-exported)
from .scraper import PAGE_SIZE, RegisterClient, RegisterError
from .store import (ERROR, FOUND, HAS_DATA, MAX_ATTEMPTS, NO_RECORD, PARTIAL, RESOLVED,
                    RecordStore, now_iso, parse_reg_date)

log = logging.getLogger(__name__)

DEFAULT_START = "01-11200"


@dataclass
class CollectionConfig:
    start_goc: str = DEFAULT_START
    batch_size: int = 25          # GOC numbers per register search request
    workers: int = 1              # parallel batches (requests are still rate limited)
    min_interval: float = 3.0     # seconds between any two requests (adaptive)
    tail_gap: int = 2000          # consecutive empty numbers that end a collection
    lookahead: int = 10000        # extra range checked after the tail
    probe_width: int = 100        # numbers per look-ahead request
    retry_passes: int = 3         # passes over failed numbers before finishing
    retry_cooldown: float = 30.0  # seconds before each retry pass
    max_goc: str | None = None    # optional upper bound (testing / partial runs)

    @classmethod
    def from_dict(cls, d: dict | None) -> "CollectionConfig":
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and v not in (None, "")}
        cfg = cls(**known)
        cfg.start_goc = gn.normalise(cfg.start_goc)
        if cfg.max_goc:
            cfg.max_goc = gn.normalise(cfg.max_goc)
        cfg.batch_size = max(1, min(int(cfg.batch_size), 50))
        cfg.workers = max(1, min(int(cfg.workers), 4))
        cfg.min_interval = max(0.2, float(cfg.min_interval))
        cfg.tail_gap = max(50, int(cfg.tail_gap))
        cfg.lookahead = max(0, int(cfg.lookahead))
        return cfg


@dataclass
class ResumePlan:
    prefix: str
    width: int
    start_seq: int
    last_found_seq: int | None
    gaps: list[int]               # unresolved numbers between start and last record
    continue_from: int            # first number of the sequential phase
    resolved_in_range: int

    def describe(self) -> dict:
        f = lambda s: gn.fmt(s, self.prefix, self.width) if s is not None else None  # noqa: E731
        return {
            "start_goc": f(self.start_seq),
            "last_found_goc": f(self.last_found_seq),
            "already_processed": self.resolved_in_range,
            "gap_count": len(self.gaps),
            "gaps_preview": [f(s) for s in self.gaps[:50]],
            "next_goc": f(self.gaps[0]) if self.gaps else f(self.continue_from),
            "continue_from_goc": f(self.continue_from),
        }


def compute_plan(store: RecordStore, start_goc: str) -> ResumePlan:
    """Work out where a collection must (re)start.

    * Numbers in ``[start, highest number with a record]`` that are pending,
      failed or missing are *gaps* and are processed first (numbers that
      already failed ``MAX_ATTEMPTS`` times are given up and left out).
    * Collection then continues after the highest number with a record.
      "No record" numbers after that point are checked again, because that is
      exactly where new registrations appear.
    """
    prefix, start_seq, width = gn.parse(start_goc)
    width = max(width, gn.DEFAULT_WIDTH)
    _, hi = store.seq_bounds(prefix)
    if hi is None or hi < start_seq:
        return ResumePlan(prefix, width, start_seq, hi, [], start_seq, 0)
    gaps, resolved = store.plan_gaps(prefix, start_seq, hi, MAX_ATTEMPTS)
    return ResumePlan(prefix, width, start_seq, hi, gaps, hi + 1, resolved)


@dataclass
class Progress:
    state: str = "idle"            # idle|running|pausing|paused|stopping|stopped|completed|failed
    activity: str = ""             # what is happening right now, in plain words
    phase: str = ""
    mode: str = ""
    start_goc: str | None = None
    current_goc: str | None = None
    run_checked: int = 0
    run_found: int = 0
    run_partial: int = 0
    run_no_record: int = 0
    run_errors: int = 0
    run_latest_date: str | None = None
    consecutive_empty: int = 0
    tail_gap: int = 0
    started_at: str | None = None
    started_ts: float = 0.0
    finished_at: str | None = None
    last_save: str | None = None
    save_problem: str = ""
    saving: bool = False
    end_verified: bool = False
    message: str = ""
    requests: int = 0
    last_activity_ts: float = 0.0
    recent: list = field(default_factory=list)


class Collector:
    """Runs one collection at a time in a background thread."""

    export_retry_every = 10.0     # seconds between attempts while the Excel file is open

    def __init__(self, store: RecordStore, save: Callable[[], None] | None = None,
                 lock_path: str | None = None, excel_path=None):
        self.store = store
        self._save_fn = save
        self.progress = Progress()
        self.events: deque[dict] = deque(maxlen=200)
        self.recent: deque[dict] = deque(maxlen=15)
        self._thread: threading.Thread | None = None
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._rec_lock = threading.Lock()
        self.client: RegisterClient | None = None
        self.cfg: CollectionConfig | None = None
        self.lock = CollectionLock(lock_path) if lock_path else None
        self.excel_path = excel_path
        self._save_lock = threading.Lock()
        self._export_lock = threading.Lock()
        self._export_thread: threading.Thread | None = None
        self._export_again = False
        self._closing = threading.Event()

    # ------------------------------------------------------------ control
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, cfg: CollectionConfig, mode: str = "new",
              client: RegisterClient | None = None) -> None:
        with self._lock:
            if self.busy:
                raise RuntimeError("A collection is already running")
            if self.lock:
                self.lock.acquire()      # raises CollectionLocked if another process runs one
            self.cfg = cfg
            self.client = client or RegisterClient(min_interval=cfg.min_interval)
            self._stop.clear()
            self._resume.set()
            self.recent.clear()
            self.progress = Progress(state="running", mode=mode, start_goc=cfg.start_goc,
                                     tail_gap=cfg.tail_gap, started_at=now_iso(),
                                     started_ts=time.time(), last_activity_ts=time.time(),
                                     activity="Starting...")
            self._thread = threading.Thread(target=self._run, args=(cfg, mode),
                                            name="goc-collector", daemon=True)
            self._thread.start()

    def pause(self) -> None:
        if self.busy and self._resume.is_set():
            self._resume.clear()
            self.progress.state = "pausing"
            self._event("Pausing - finishing the current step first")

    def resume(self) -> None:
        if self.busy and not self._resume.is_set():
            self._resume.set()
            self.progress.state = "running"
            self._event("Continuing")

    def stop(self) -> None:
        if self.busy:
            self._stop.set()
            self._resume.set()
            if self.client is not None and hasattr(self.client, "cancel"):
                self.client.cancel()
            self.progress.state = "stopping"
            self._event("Stopping - finishing the current step")

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ------------------------------------------------------------ helpers
    def _event(self, msg: str, level: str = "info") -> None:
        log.log(logging.WARNING if level == "warn" else logging.INFO, msg)
        self.events.appendleft({"time": now_iso(), "level": level, "message": msg})
        self.progress.message = msg

    def _activity(self, text: str, goc: str | None = None) -> None:
        self.progress.activity = text
        if goc:
            self.progress.current_goc = goc
        self.progress.last_activity_ts = time.time()

    def _fmt(self, seq: int) -> str:
        return gn.fmt(seq, self._prefix, self._width)

    def excel_up_to_date(self) -> bool:
        if self.store.meta.get("excel_version") != self.store.version:
            return False
        return self.excel_path is None or os.path.exists(self.excel_path)

    def save_now(self, reason: str = "") -> bool:
        """Write the Excel export if the data changed since the last one.
        Returns True when the Excel file is up to date. Safe from any thread."""
        if self._save_fn is None:
            return False
        with self._save_lock:
            if self.excel_up_to_date():
                self.progress.save_problem = ""
                return True
            version = self.store.version
            self.progress.saving = True
            try:
                self._save_fn()
            except Exception as exc:  # noqa: BLE001  (e.g. the file is open in Excel)
                if self.progress.save_problem != str(exc):
                    self._event(str(exc), "warn")
                self.progress.save_problem = str(exc)
                return False
            finally:
                self.progress.saving = False
            self.store.meta["excel_version"] = version
            self.store.meta["excel_saved_at"] = now_iso()
            self.progress.last_save = now_iso()
            if self.progress.save_problem:
                self._event("The Excel file is up to date again")
            self.progress.save_problem = ""
            log.info("Excel file updated%s", f" ({reason})" if reason else "")
            return True

    @property
    def exporting(self) -> bool:
        return self.progress.saving

    def export_soon(self, reason: str = "") -> None:
        """Update the Excel file in the background. If it cannot be written
        (open in Excel), keep trying every ``export_retry_every`` seconds."""
        if self._save_fn is None:
            return
        with self._export_lock:
            if self._export_thread is not None and self._export_thread.is_alive():
                self._export_again = True
                return
            self._export_again = False
            self._export_thread = threading.Thread(target=self._export_loop, args=(reason,),
                                                   name="goc-excel-export", daemon=True)
            self._export_thread.start()

    def _export_loop(self, reason: str) -> None:
        while not self._closing.is_set():
            ok = self.save_now(reason)
            with self._export_lock:
                if ok and not self._export_again:
                    return
                self._export_again = False
            if not ok and self._closing.wait(self.export_retry_every):
                return

    def _export_or_retry(self, reason: str) -> None:
        if not self.save_now(reason):
            self.export_soon(reason)

    def close(self, timeout: float = 60) -> None:
        """Used when the app shuts down: stop collecting, write Excel once more."""
        self.stop()
        self.join(timeout)
        self._closing.set()
        if self._export_thread is not None:
            self._export_thread.join(timeout)
        try:
            self.save_now("closing")
        except Exception:  # noqa: BLE001
            pass

    def _checkpoint(self) -> bool:
        """Blocks while paused. Returns False if a stop was requested."""
        if not self._resume.is_set():
            if self.progress.state != "stopping":
                self._activity("Pausing - updating the Excel file...")
                self._export_or_retry("paused")
                self.progress.state = "paused"
                self._activity("Paused - everything collected so far is saved. "
                               "Press Continue to carry on.")
            self._resume.wait()
            if not self._stop.is_set():
                self.progress.state = "running"
        return not self._stop.is_set()

    def _may_submit(self, pending) -> bool:
        if self._stop.is_set():
            return False
        if not self._resume.is_set():
            if pending:
                return False
            return self._checkpoint()
        return True

    # ----------------------------------------------------------- results
    def _record(self, goc: str, status: str, data: dict | None, err: str | None) -> None:
        """Store one number's result and update the live counters immediately."""
        # a step cut short by Stop is not the website's fault: no attempt counted
        stopped = status == ERROR and self._stop.is_set()
        self.store.save_result(goc, status, data, err, count_attempt=not stopped)
        pr = self.progress
        seq = gn.parse(goc)[1]
        with self._rec_lock:
            if status == FOUND:
                pr.run_found += 1
                text = f"{data.get('name')} - {data.get('town')}"
            elif status == PARTIAL:
                pr.run_partial += 1
                text = f"{data.get('name')} - some details not shown on the register"
            elif status == NO_RECORD:
                pr.run_no_record += 1
                text = "No registrant with this number"
            else:
                pr.run_errors += 1
                text = "Website did not answer - will try again later"
            if status in RESOLVED:
                pr.run_checked += 1
            if status in HAS_DATA:
                if self._last_found is None or seq > self._last_found:
                    self._last_found = seq
                d = parse_reg_date(data.get("date_registered"))
                if d and (self._latest is None or d > self._latest[0]):
                    self._latest = (d, data["date_registered"])
                    pr.run_latest_date = data["date_registered"]
            self.recent.appendleft({"goc": goc, "status": status, "text": text,
                                    "time": now_iso()[11:]})
            pr.recent = list(self.recent)
            pr.current_goc = goc
            pr.last_activity_ts = time.time()
            pr.requests = getattr(self.client, "request_count", 0)

    def _process_batch(self, seqs: list[int]) -> None:
        """Search a batch of numbers and record every result."""
        pins = [self._fmt(s) for s in seqs]
        label = pins[0] if len(pins) == 1 else f"{pins[0]} to {pins[-1]}"
        self._activity(f"Searching the register for {label}", pins[0])
        groups: list[tuple[list[str], dict]] = []
        try:
            groups.append((pins, self.client.search_pins(pins)))
        except RegisterError as exc:
            log.warning("batch %s..%s failed (%s); splitting", pins[0], pins[-1], exc)
            for i in range(0, len(pins), PAGE_SIZE):
                chunk = pins[i:i + PAGE_SIZE]
                try:
                    groups.append((chunk, self.client.search_pins(chunk)))
                except RegisterError:
                    for p in chunk:       # isolate the failing number(s)
                        try:
                            groups.append(([p], self.client.search_pins([p])))
                        except RegisterError as exc3:
                            self._record(p, ERROR, None, f"search failed: {exc3}")
        for chunk, hits in groups:
            n_hits = len(hits)
            done_hits = 0
            for p in chunk:
                hit = hits.get(p)
                if hit is None:
                    self._record(p, NO_RECORD, None, None)
                    continue
                done_hits += 1
                self._activity(f"Reading the details of {p} ({hit.name})"
                               + (f" - {done_hits} of {n_hits} found in this group" if n_hits > 1 else ""), p)
                try:
                    reg = self.client.fetch_detail(hit)
                except RegisterError as exc:
                    self._record(p, ERROR, None, f"detail page failed: {exc}")
                    continue
                self._record(p, PARTIAL if reg.missing else FOUND, asdict(reg), None)

    # ------------------------------------------------------------ phases
    def _run_batches(self, batches: list[list[int]], label: str) -> None:
        if not batches:
            return
        self.progress.phase = label
        with ThreadPoolExecutor(self.cfg.workers) as pool:
            pending: set[Future] = set()
            it = iter(batches)
            exhausted = False
            while True:
                while not exhausted and len(pending) < self.cfg.workers:
                    if not self._may_submit(pending):
                        if self._stop.is_set():
                            exhausted = True
                        break
                    b = next(it, None)
                    if b is None:
                        exhausted = True
                        break
                    pending.add(pool.submit(self._process_batch, b))
                if not pending:
                    break
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in finished:
                    f.result()

    def _run_sequential(self, from_seq: int) -> int:
        """Collect from ``from_seq`` upwards until the stop rule is satisfied.
        Returns the last number covered (everything up to it is processed)."""
        cfg = self.cfg
        max_seq = gn.parse(cfg.max_goc)[1] if cfg.max_goc else None
        next_seq = from_seq
        completed: dict[int, int] = {}
        frontier = from_seq - 1

        def target() -> int:
            base = self._last_found if self._last_found is not None else self._start_seq - 1
            t = max(base + cfg.tail_gap, self._min_target)
            return min(t, max_seq) if max_seq is not None else t

        self._update_phase(frontier)
        with ThreadPoolExecutor(cfg.workers) as pool:
            pending: dict[Future, tuple[int, int]] = {}
            while True:
                while len(pending) < cfg.workers and next_seq <= target():
                    if not self._may_submit(pending):
                        break
                    end = min(next_seq + cfg.batch_size - 1, target())
                    fut = pool.submit(self._process_batch, list(range(next_seq, end + 1)))
                    pending[fut] = (next_seq, end)
                    next_seq = end + 1
                if not pending:
                    break
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for f in finished:
                    s, e = pending.pop(f)
                    f.result()
                    completed[s] = e
                while frontier + 1 in completed:
                    frontier = completed.pop(frontier + 1)
                self._update_phase(frontier)
                if self._stop.is_set() and not pending:
                    break
        return frontier

    def _update_phase(self, frontier: int) -> None:
        pr = self.progress
        base = self._last_found if self._last_found is not None else self._start_seq - 1
        pr.consecutive_empty = max(0, frontier - base)
        if pr.consecutive_empty >= 100 and self._last_found is not None:
            pr.phase = (f"Checking whether the register ends here: {pr.consecutive_empty:,} of "
                        f"{self.cfg.tail_gap:,} numbers after {self._fmt(self._last_found)} "
                        "are empty so far")
        else:
            pr.phase = "Collecting records"

    def _retry_errors(self, upto: int) -> None:
        for n in range(1, self.cfg.retry_passes + 1):
            errs = [gn.parse(g)[1] for g in
                    self.store.unresolved_numbers(self._prefix, MAX_ATTEMPTS)]
            errs = [s for s in errs if self._start_seq <= s <= upto]
            if not errs or self._stop.is_set():
                return
            self._event(f"Trying again {len(errs)} number(s) the website did not answer", "warn")
            t_end = time.monotonic() + self.cfg.retry_cooldown
            while time.monotonic() < t_end and not self._stop.is_set():
                left = int(t_end - time.monotonic())
                self._activity(f"Short break before trying {len(errs)} number(s) again ({left}s)")
                time.sleep(0.5)
            self._run_batches([errs[i:i + PAGE_SIZE] for i in range(0, len(errs), PAGE_SIZE)],
                              f"Trying failed numbers again (round {n})")

    def _probe_ahead(self, covered: int) -> tuple[int | None, bool]:
        cfg = self.cfg
        if cfg.lookahead <= 0:
            return None, True
        max_seq = gn.parse(cfg.max_goc)[1] if cfg.max_goc else None
        self.progress.phase = (f"Final check: making sure there are no registrations between "
                               f"{self._fmt(covered + 1)} and {self._fmt(covered + cfg.lookahead)}")
        self._event(self.progress.phase)
        s = covered + 1
        while s <= covered + cfg.lookahead:
            if max_seq is not None and s > max_seq:
                break
            if not self._checkpoint():
                return None, False
            e = min(s + cfg.probe_width - 1, covered + cfg.lookahead)
            pins = [self._fmt(x) for x in range(s, e + 1)]
            self._activity(f"Final check of {pins[0]} to {pins[-1]}", pins[0])
            try:
                n = self.client.count_pins(pins)
            except RegisterError as exc:
                self._event(f"Final check {pins[0]}-{pins[-1]} failed: {exc}", "warn")
                return None, False
            if n > 0:
                self._event(f"Found {n} more registrant(s) between {pins[0]} and {pins[-1]} - "
                            "continuing", "warn")
                return e, True
            s = e + 1
        return None, True

    # -------------------------------------------------------------- main
    def _run(self, cfg: CollectionConfig, mode: str) -> None:
        pr = self.progress
        final_state = "completed"
        self._prefix, _, self._width = gn.parse(cfg.start_goc)
        self._width = max(self._width, gn.DEFAULT_WIDTH)
        self.store.meta["end_verified"] = False
        try:
            plan = compute_plan(self.store, cfg.start_goc)
            self._prefix, self._width = plan.prefix, plan.width
            self._start_seq = plan.start_seq
            self._last_found = plan.last_found_seq
            self._latest = None
            self._min_target = plan.start_seq - 1
            info = plan.describe()
            if info["already_processed"]:
                self._event(f"Continuing: {info['already_processed']:,} numbers already done, "
                            f"next number {info['next_goc']}")
            else:
                self._event(f"Starting a new collection at {info['start_goc']}")
            if plan.gaps:
                self._run_batches([plan.gaps[i:i + cfg.batch_size]
                                   for i in range(0, len(plan.gaps), cfg.batch_size)],
                                  f"Finishing {len(plan.gaps):,} number(s) left over from last time")
            cont = plan.continue_from
            covered = cont - 1
            verified = False
            while not self._stop.is_set():
                covered = max(covered, self._run_sequential(cont))
                if self._stop.is_set():
                    break
                self._retry_errors(covered)
                base = self._last_found if self._last_found is not None else self._start_seq - 1
                max_seq = gn.parse(cfg.max_goc)[1] if cfg.max_goc else None
                if max_seq is not None and covered >= max_seq:
                    self._event(f"Reached the upper limit {cfg.max_goc}")
                    break
                if covered < base + cfg.tail_gap:
                    cont = covered + 1
                    continue
                hit_end, ok = self._probe_ahead(covered)
                if not ok:
                    break
                if hit_end is None:
                    verified = True
                    break
                self._min_target = hit_end
                cont = covered + 1
            if self._stop.is_set():
                final_state = "stopped"
            remaining = [g for g in self.store.unresolved_numbers(self._prefix, MAX_ATTEMPTS)
                         if gn.parse(g)[1] >= self._start_seq]
            gave_up = self.store.stats(self._prefix).get("gave_up", 0)
            pr.end_verified = verified and not remaining
            self.store.meta["end_verified"] = pr.end_verified
            if final_state == "completed":
                if remaining:
                    self._event(f"Finished, but the website did not answer for {len(remaining)} "
                                "number(s). Press Start again later to retry them.", "warn")
                elif verified:
                    lf = self._fmt(self._last_found) if self._last_found else "n/a"
                    self._event(f"Finished - all available records collected. Highest registered "
                                f"number: {lf}")
                    if gave_up:
                        self._event(f"{gave_up} number(s) did not answer after {MAX_ATTEMPTS} "
                                    "tries and are marked as failed in the Excel file.", "warn")
                else:
                    self._event("Finished (the end of the register was not confirmed)", "warn")
        except Exception as exc:  # noqa: BLE001
            log.exception("collection failed")
            final_state = "failed"
            self._event(f"Something went wrong: {exc}. Press Start to continue.", "warn")
        finally:
            self.progress.state = "stopping" if final_state == "stopped" else self.progress.state
            # the data is already in the database; this only refreshes the Excel copy
            self._activity("Updating the Excel file...")
            self._export_or_retry("finished")
            pr.state = final_state
            pr.finished_at = now_iso()
            pr.phase = {"completed": "Finished", "stopped": "Stopped", "failed": "Stopped"}[final_state]
            pr.activity = {
                "completed": "Finished - everything is saved",
                "stopped": "Stopped - everything collected so far is saved. "
                           "Press Continue collecting to carry on.",
                "failed": "Stopped because of an error - everything collected so far is saved. "
                          "Press Continue collecting to carry on."}[final_state]
            if self.lock:
                self.lock.release()
