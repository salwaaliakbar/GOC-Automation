"""SQLite record store - the saved collection (source of truth).

Every result is written to the database file (default ``output/GOC_Register.db``)
the moment it arrives, so nothing is lost if the computer switches off or the
window is closed. Only what is needed is read back into memory (the highest
registered number, numbers to retry, totals), so memory use stays small however
large the register grows.

The Excel workbook is an *export* of this database (see :mod:`excel_io`); it can
be rebuilt from the database at any time.

``RecordStore()`` without a path uses an in-memory database (used by the tests).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path

from . import NOT_AVAILABLE
from . import goc_numbers as gn

# processing statuses
PENDING = "pending"
FOUND = "found"
NO_RECORD = "no_record"
PARTIAL = "partial"
ERROR = "error"
RESOLVED = (FOUND, PARTIAL, NO_RECORD)
HAS_DATA = (FOUND, PARTIAL)
ALL_STATUSES = (PENDING, FOUND, PARTIAL, NO_RECORD, ERROR)
FIELDS = ("name", "town", "date_registered", "practice_address")
EXTRA_FIELDS = ("registration_status", "registered_as", "detail_url")

# A number whose search failed this many times is "given up": it stays marked as
# failed (red in Excel) but is not retried on every run. It can be reset.
MAX_ATTEMPTS = 5

_DATE_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$")

_COLUMNS = ("goc_number", "prefix", "seq", "processing_status") + FIELDS + EXTRA_FIELDS + (
    "reg_date_iso", "last_checked", "error_message", "attempts")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    goc_number          TEXT PRIMARY KEY,
    prefix              TEXT NOT NULL,
    seq                 INTEGER NOT NULL,
    processing_status   TEXT NOT NULL,
    name                TEXT,
    town                TEXT,
    date_registered     TEXT,
    practice_address    TEXT,
    registration_status TEXT,
    registered_as       TEXT,
    detail_url          TEXT,
    reg_date_iso        TEXT,
    last_checked        TEXT,
    error_message       TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_records_seq ON records(prefix, seq);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(prefix, processing_status, seq);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def parse_reg_date(text: str | None) -> dt.date | None:
    """Parse a register date (``DD-MM-YYYY``) - used for comparisons only."""
    if not text:
        return None
    m = _DATE_RE.match(str(text).strip())
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    if y < 100:
        y += 2000 if y < 50 else 1900
    try:
        return dt.date(y, mth, d)
    except ValueError:
        return None


def data_score(row: dict) -> int:
    """How many of the four data fields hold real values."""
    return sum(1 for k in FIELDS
               if row.get(k) and str(row[k]).strip() and row[k] != NOT_AVAILABLE)


class _Meta(MutableMapping):
    """Small key/value settings stored in the database (JSON values)."""

    def __init__(self, store: "RecordStore"):
        self._s = store

    def __getitem__(self, key):
        row = self._s._one("SELECT value FROM meta WHERE key = ?", (key,))
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self, key, value):
        with self._s.transaction():
            self._s._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)))

    def __delitem__(self, key):
        with self._s.transaction():
            cur = self._s._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            if cur.rowcount == 0:
                raise KeyError(key)

    def __iter__(self):
        return iter([r[0] for r in self._s._all("SELECT key FROM meta")])

    def __len__(self):
        return self._s._one("SELECT COUNT(*) FROM meta")[0]


class RecordStore:
    """Thread-safe SQLite store of GOC number -> record."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn = sqlite3.connect(str(self.path) if self.path else ":memory:",
                                     check_same_thread=False, isolation_level=None,
                                     timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path:
                self._conn.execute("PRAGMA journal_mode=WAL")
                # FULL: a result that was reported as saved survives a power cut
                self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA)
        self.meta = _Meta(self)
        # increases on every data change; persisted so the app knows on start
        # whether the Excel export is older than the database
        self.version = int(self.meta.get("version", 0))
        self._stats_cache: tuple[int, str, dict] | None = None

    # ------------------------------------------------------------ plumbing
    def _one(self, sql: str, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    @contextmanager
    def transaction(self):
        """Group writes into one transaction (re-entrant). Used for bulk imports."""
        with self._lock:
            if self._tx_depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._conn.execute("COMMIT")

    def _bump(self) -> None:
        self.version += 1
        self.meta["version"] = self.version

    def _put(self, rec: dict) -> None:
        d = parse_reg_date(rec.get("date_registered"))
        rec["reg_date_iso"] = d.isoformat() if d else None
        cols = ", ".join(_COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        self._conn.execute(f"INSERT OR REPLACE INTO records ({cols}) VALUES ({marks})",
                           [rec.get(c) for c in _COLUMNS])
        self._bump()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def backup_to(self, dest: str | Path) -> None:
        """Copy the whole database to ``dest`` (safe while the app is running)."""
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()

    # ------------------------------------------------------------ basics
    def __len__(self) -> int:
        return self._one("SELECT COUNT(*) FROM records")[0]

    def get(self, goc: str) -> dict | None:
        row = self._one("SELECT * FROM records WHERE goc_number = ?", (goc,))
        return dict(row) if row else None

    def clear(self) -> None:
        with self.transaction():
            self._conn.execute("DELETE FROM records")
            self._conn.execute("DELETE FROM meta")
            self._bump()

    # ------------------------------------------------------------ writes
    def save_result(self, goc: str, status: str, data: dict | None = None,
                    error: str | None = None, count_attempt: bool = True) -> None:
        """Store a web search result immediately. A failed search never
        downgrades a number that was already resolved. ``count_attempt=False``
        is used when the user stopped the collection: that is not the website's
        fault and does not count towards ``MAX_ATTEMPTS``."""
        prefix, seq, _ = gn.parse(goc)
        data = data or {}
        with self.transaction():
            cur = self._conn.execute(
                "SELECT processing_status, attempts FROM records WHERE goc_number = ?",
                (goc,)).fetchone()
            if status == ERROR and cur and cur["processing_status"] in RESOLVED:
                self._conn.execute(
                    "UPDATE records SET last_checked = ?, error_message = ? WHERE goc_number = ?",
                    (now_iso(), error, goc))
                return
            rec = {"goc_number": goc, "prefix": prefix, "seq": seq,
                   "processing_status": status, "last_checked": now_iso(),
                   "error_message": error,
                   "attempts": (cur["attempts"] if cur else 0) + (1 if count_attempt else 0)}
            for k in FIELDS + EXTRA_FIELDS:
                rec[k] = data.get(k) if status in HAS_DATA else None
            self._put(rec)

    def merge_import(self, row: dict) -> str:
        """Merge one row read from Excel. Returns 'inserted', 'updated' or 'kept'.

        Never loses better information: a resolved number is only replaced when
        the imported row holds strictly more data.
        """
        goc = row["goc_number"]
        prefix, seq, _ = gn.parse(goc)
        new_status = row["processing_status"]
        with self.transaction():
            cur = self.get(goc)
            if cur is not None:
                cs = cur["processing_status"]
                if cs in RESOLVED:
                    if new_status not in HAS_DATA or data_score(row) <= data_score(cur):
                        return "kept"
                elif new_status not in RESOLVED:
                    return "kept"
            rec = {"goc_number": goc, "prefix": prefix, "seq": seq,
                   "processing_status": new_status,
                   "last_checked": row.get("last_checked"),
                   "error_message": row.get("error_message"), "attempts": 0}
            for k in FIELDS:
                rec[k] = row.get(k) if new_status in HAS_DATA else None
            self._put(rec)
            return "inserted" if cur is None else "updated"

    def reset_attempts(self, prefix: str = gn.DEFAULT_PREFIX) -> int:
        """Make given-up numbers eligible for retrying again. Returns how many."""
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE records SET attempts = 0 WHERE prefix = ? AND processing_status = ? "
                "AND attempts >= ?", (prefix, ERROR, MAX_ATTEMPTS))
            if cur.rowcount:
                self._bump()
            return cur.rowcount

    # ------------------------------------------------------------- reads
    def seq_bounds(self, prefix: str) -> tuple[int | None, int | None]:
        """(lowest number, highest number that has a record) for a prefix."""
        lo = self._one("SELECT MIN(seq) FROM records WHERE prefix = ?", (prefix,))[0]
        hi = self._one("SELECT MAX(seq) FROM records WHERE prefix = ? AND processing_status IN (?, ?)",
                       (prefix, *HAS_DATA))[0]
        return lo, hi

    def export_bounds(self, prefix: str, trim_tail: bool = False) -> tuple[int | None, int | None]:
        """Rows that belong in the Excel file.

        * While collecting (or stopped part-way): from the lowest number up to the
          last number checked, so every checked number is visible - including
          "no record" numbers - even before anyone was found.
        * ``trim_tail`` (collection finished, end of register verified): the long
          run of empty numbers after the last registrant is left out; the rows end
          at the highest number with a record or one that still needs work.
        """
        lo = self._one("SELECT MIN(seq) FROM records WHERE prefix = ?", (prefix,))[0]
        last_checked = self._one("SELECT MAX(seq) FROM records WHERE prefix = ?", (prefix,))[0]
        if not trim_tail:
            return lo, last_checked
        hi = self._one("SELECT MAX(seq) FROM records WHERE prefix = ? AND processing_status "
                       "IN (?, ?, ?, ?)", (prefix, FOUND, PARTIAL, ERROR, PENDING))[0]
        return lo, (hi if hi is not None else last_checked)

    def statuses_between(self, prefix: str, lo: int, hi: int) -> dict[int, str]:
        rows = self._all("SELECT seq, processing_status FROM records "
                         "WHERE prefix = ? AND seq BETWEEN ? AND ?", (prefix, lo, hi))
        return {r[0]: r[1] for r in rows}

    def plan_gaps(self, prefix: str, lo: int, hi: int,
                  max_attempts: int = MAX_ATTEMPTS) -> tuple[list[int], int]:
        """Numbers in ``[lo, hi]`` that still need work (failed, pending or
        missing; given-up numbers excluded), and how many are resolved."""
        done = {r[0] for r in self._all(
            "SELECT seq FROM records WHERE prefix = ? AND seq BETWEEN ? AND ? AND "
            "(processing_status IN (?, ?, ?) OR (processing_status = ? AND attempts >= ?))",
            (prefix, lo, hi, *RESOLVED, ERROR, max_attempts))}
        resolved = self._one(
            "SELECT COUNT(*) FROM records WHERE prefix = ? AND seq BETWEEN ? AND ? "
            "AND processing_status IN (?, ?, ?)", (prefix, lo, hi, *RESOLVED))[0]
        return [s for s in range(lo, hi + 1) if s not in done], resolved

    def iter_records(self, prefix: str, lo: int, hi: int,
                     chunk: int = 2000) -> Iterator[dict]:
        """Records in ``[lo, hi]`` in number order, read in small chunks so that
        memory stays low and other threads are never blocked for long."""
        last = lo - 1
        while True:
            rows = self._all("SELECT * FROM records WHERE prefix = ? AND seq > ? AND seq <= ? "
                             "ORDER BY seq LIMIT ?", (prefix, last, hi, chunk))
            if not rows:
                return
            for r in rows:
                yield dict(r)
            last = rows[-1]["seq"]

    def records_in_range(self, prefix: str, lo: int, hi: int) -> list[dict]:
        return list(self.iter_records(prefix, lo, hi))

    def unresolved_numbers(self, prefix: str, max_attempts: int | None = None) -> list[str]:
        """Failed or pending numbers, lowest first. With ``max_attempts``,
        numbers that already failed that many times are left out."""
        sql = ("SELECT goc_number FROM records WHERE prefix = ? "
               "AND processing_status IN (?, ?)")
        args: list = [prefix, ERROR, PENDING]
        if max_attempts is not None:
            sql += " AND attempts < ?"
            args.append(max_attempts)
        return [r[0] for r in self._all(sql + " ORDER BY seq", args)]

    def stats(self, prefix: str = gn.DEFAULT_PREFIX) -> dict:
        with self._lock:
            if self._stats_cache and self._stats_cache[:2] == (self.version, prefix):
                return dict(self._stats_cache[2])
            version = self.version
            counts = {s: 0 for s in ALL_STATUSES}
            for st, n in self._all("SELECT processing_status, COUNT(*) FROM records "
                                   "WHERE prefix = ? GROUP BY processing_status", (prefix,)):
                counts[st] = n

            def first_goc(where: str, args=(), order: str = "seq"):
                row = self._one(f"SELECT goc_number FROM records WHERE prefix = ? {where} "
                                f"ORDER BY {order} LIMIT 1", (prefix, *args))
                return row[0] if row else None

            latest = self._one(
                "SELECT date_registered, goc_number FROM records WHERE prefix = ? "
                "AND processing_status IN (?, ?) AND reg_date_iso IS NOT NULL "
                "ORDER BY reg_date_iso DESC, seq LIMIT 1", (prefix, *HAS_DATA))
            gave_up = self._one(
                "SELECT COUNT(*) FROM records WHERE prefix = ? AND processing_status = ? "
                "AND attempts >= ?", (prefix, ERROR, MAX_ATTEMPTS))[0]
            out = {
                "total_rows": sum(counts.values()),
                "processed": counts[FOUND] + counts[PARTIAL] + counts[NO_RECORD],
                "found": counts[FOUND], "partial": counts[PARTIAL],
                "no_record": counts[NO_RECORD], "error": counts[ERROR],
                "pending": counts[PENDING],
                "gave_up": gave_up,
                "first_goc": first_goc(""),
                "last_goc_processed": first_goc("AND processing_status IN (?, ?, ?)",
                                                RESOLVED, "seq DESC"),
                "last_found_goc": first_goc("AND processing_status IN (?, ?)",
                                            HAS_DATA, "seq DESC"),
                "latest_date": latest[0] if latest else None,
                "latest_date_goc": latest[1] if latest else None,
            }
            self._stats_cache = (version, prefix, out)
            return dict(out)
