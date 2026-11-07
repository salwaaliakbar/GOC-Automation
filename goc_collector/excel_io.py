"""Excel workbook = an export of the database (and a way to bring data in).

The SQLite database (:mod:`store`) is the source of truth. The workbook is built
from it on pause, stop, finish and download, and can be rebuilt at any time.

Workbook layout written by :func:`save_workbook`:

* ``GOC Register`` - the deliverable: columns A-E exactly
  ``GOC No. | Name | Town | Date Registered | Practice Address``, one row per
  GOC number from the first number to the highest registered number.
* ``Collection Status`` - processing status of every number, so that a
  collection can also be resumed from the Excel file alone (e.g. on a new
  computer without the database file).
* ``Summary`` - totals.

:func:`load_workbook_into` reads these workbooks back *and* plain sheets that
only contain the five register columns (status is then inferred).
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import NOT_AVAILABLE, NOT_PROCESSED, SEARCH_FAILED
from . import goc_numbers as gn
from .store import (ERROR, FIELDS, FOUND, MAX_ATTEMPTS, NO_RECORD, PARTIAL, PENDING,
                    RecordStore, data_score, now_iso)

HEADERS = ["GOC No.", "Name", "Town", "Date Registered", "Practice Address"]
MAIN_SHEET = "GOC Register"
STATUS_SHEET = "Collection Status"
SUMMARY_SHEET = "Summary"
STATUS_HEADERS = ["GOC No.", "Processing Status", "Last Checked", "Error Message"]

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center")
_NA_FONT = Font(color="808080", italic=True)
_FAIL_FILL = PatternFill("solid", fgColor="FCE4E4")
_TOP = Alignment(vertical="top")
_TOP_WRAP = Alignment(vertical="top", wrap_text=True)
_BOLD = Font(bold=True)


class WorkbookInUse(PermissionError):
    """The workbook could not be written (usually because it is open in Excel)."""


def _row_values(rec: dict | None, goc: str) -> list[str]:
    if rec is None or rec["processing_status"] == PENDING:
        return [goc] + [NOT_PROCESSED] * 4
    st = rec["processing_status"]
    if st == NO_RECORD:
        return [goc] + [NOT_AVAILABLE] * 4
    if st == ERROR:
        return [goc] + [SEARCH_FAILED] * 4
    return [goc] + [(rec.get(f) or NOT_AVAILABLE) for f in FIELDS]


def _header(ws, names, widths) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    cells = []
    for n in names:
        c = WriteOnlyCell(ws, value=n)
        c.fill, c.font, c.alignment = _HEADER_FILL, _HEADER_FONT, _HEADER_ALIGN
        cells.append(c)
    ws.append(cells)


def build_workbook(store: RecordStore, prefix: str = gn.DEFAULT_PREFIX) -> Workbook:
    """Build the workbook from a snapshot of the store (write-only mode: fast and
    memory-friendly for tens of thousands of rows)."""
    lo, hi = store.seq_bounds(prefix)
    stats = store.stats(prefix)
    verified = store.meta.get("end_verified")

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(MAIN_SHEET)
    st = wb.create_sheet(STATUS_SHEET)
    sm = wb.create_sheet(SUMMARY_SHEET)
    _header(ws, HEADERS, [12, 32, 22, 17, 90])
    _header(st, STATUS_HEADERS, [12, 18, 20, 60])

    counts = {FOUND: 0, PARTIAL: 0, NO_RECORD: 0, ERROR: 0, PENDING: 0}
    first_goc = last_goc = ""
    n_rows = 0
    if lo is not None and hi is not None and hi >= lo:
        # records are streamed from the database in number order (low memory)
        it = store.iter_records(prefix, lo, hi)
        nxt = next(it, None)
        for seq in range(lo, hi + 1):
            while nxt is not None and nxt["seq"] < seq:
                nxt = next(it, None)
            rec = None
            if nxt is not None and nxt["seq"] == seq:
                rec, nxt = nxt, next(it, None)
            goc = rec["goc_number"] if rec else gn.fmt(seq, prefix)
            status = rec["processing_status"] if rec else PENDING
            counts[status] = counts.get(status, 0) + 1
            vals = _row_values(rec, goc)
            failed = status in (ERROR, PENDING)
            row = []
            for c, v in enumerate(vals):
                cell = WriteOnlyCell(ws, value=v)
                if c == 0:
                    cell.number_format = "@"          # keep leading zeros as text
                elif c == 4:
                    cell.alignment = _TOP_WRAP
                if c and v in (NOT_AVAILABLE, NOT_PROCESSED):
                    cell.font = _NA_FONT
                if failed:
                    cell.fill = _FAIL_FILL
                row.append(cell)
            ws.append(row)
            st.append([goc, status, (rec or {}).get("last_checked") or "",
                       (rec or {}).get("error_message") or ""])
            first_goc = first_goc or goc
            last_goc = goc
            n_rows += 1
    if n_rows:
        ws.auto_filter.ref = f"A1:E{n_rows + 1}"
        st.auto_filter.ref = f"A1:D{n_rows + 1}"

    sm.column_dimensions["A"].width = 58
    sm.column_dimensions["B"].width = 70
    rows = [
        ("Saved", now_iso()),
        ("Source", "General Optical Council register - https://str.optical.org/"),
        ("First GOC number in workbook", first_goc),
        ("Last GOC number in workbook", last_goc),
        ("GOC numbers listed", n_rows),
        ("Records found (complete)", counts[FOUND]),
        ("Records with missing information (partial)", counts[PARTIAL]),
        ("Numbers with no associated record", counts[NO_RECORD]),
        ("Failed searches (will be retried)", counts[ERROR]),
        (f"  ...of which given up after {MAX_ATTEMPTS} tries", stats.get("gave_up", 0)),
        ("Not yet processed", counts[PENDING]),
        ("Latest 'Date Registered' encountered", stats["latest_date"] or ""),
        ("  ...for GOC number", stats["latest_date_goc"] or ""),
        ("Last number checked (incl. empty numbers after the last record)",
         stats["last_goc_processed"] or ""),
        ("End of register verified", "Yes" if verified else "No - collection not finished yet"),
        ("Note", "'Date Registered' is the register's 'Date of most recent registration' "
                 "(DD-MM-YYYY), exactly as displayed."),
    ]
    title = WriteOnlyCell(sm, value="GOC Register Data Collector - summary")
    title.font = Font(bold=True, size=13)
    sm.append([title])
    for k, v in rows:
        kc = WriteOnlyCell(sm, value=k)
        kc.font = _BOLD
        sm.append([kc, v])
    return wb


def save_workbook(store: RecordStore, path: Path, prefix: str = gn.DEFAULT_PREFIX) -> Path:
    """Write the workbook atomically (temp file + rename).

    Raises :class:`WorkbookInUse` when the file is open in Excel; the previous
    copy is then left untouched and the caller can simply try again later.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = build_workbook(store, prefix)
    tmp = path.with_name(f"~tmp_{os.getpid()}_{path.name}")
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    except PermissionError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkbookInUse(
            f"The Excel file could not be updated because {path.name} is open in Excel. "
            "Your data is safe. Close the file and it will be updated automatically.") from exc
    return path


# ------------------------------------------------------------------- read
def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%d-%m-%Y")
    return str(v).strip()


def _find_header(ws) -> tuple[int, dict[str, int]] | None:
    for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=15, values_only=True), start=1):
        norm = [_cell(v).lower().rstrip(".") for v in row]
        if "goc no" in norm or "goc number" in norm:
            cols = {}
            for i, h in enumerate(norm):
                key = {"goc no": "goc", "goc number": "goc", "name": "name", "town": "town",
                       "date registered": "date_registered",
                       "practice address": "practice_address",
                       "processing status": "status", "last checked": "last_checked",
                       "error message": "error"}.get(h)
                if key and key not in cols:
                    cols[key] = i
            return r_idx, cols
    return None


def _infer_status(vals: dict) -> str:
    texts = [vals.get(f, "") for f in FIELDS]
    if any(t == SEARCH_FAILED for t in texts):
        return ERROR
    if any(t == NOT_PROCESSED for t in texts):
        return PENDING
    present = [t for t in texts if t and t != NOT_AVAILABLE]
    if not present:
        return NO_RECORD
    return FOUND if len(present) == 4 else PARTIAL


def read_workbook(path: Path) -> tuple[list[dict], dict]:
    """Read a workbook into normalised rows plus a report. Does not modify anything."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return _read_open_workbook(wb, path)
    finally:
        wb.close()      # read-only workbooks keep the file open (blocks saving on Windows)


def _read_open_workbook(wb, path: Path) -> tuple[list[dict], dict]:
    main = None
    for ws in wb.worksheets:
        if ws.title == STATUS_SHEET:
            continue
        h = _find_header(ws)
        if h and {"goc", "name"} <= set(h[1]):
            main = (ws, *h)
            break
    if main is None:
        raise ValueError("No sheet with a 'GOC No.' / 'Name' header row was found")
    ws, hdr_row, cols = main

    statuses: dict[str, dict] = {}
    if STATUS_SHEET in wb.sheetnames:
        sws = wb[STATUS_SHEET]
        sh = _find_header(sws)
        if sh and "status" in sh[1]:
            c = sh[1]
            for row in sws.iter_rows(min_row=sh[0] + 1, values_only=True):
                g = _cell(row[c["goc"]]) if len(row) > c["goc"] else ""
                if gn.is_valid(g):
                    statuses[gn.normalise(g)] = {
                        "status": _cell(row[c["status"]]),
                        "last_checked": _cell(row[c["last_checked"]]) if "last_checked" in c else "",
                        "error": _cell(row[c["error"]]) if "error" in c else ""}
    verified = False
    if SUMMARY_SHEET in wb.sheetnames:
        for row in wb[SUMMARY_SHEET].iter_rows(values_only=True):
            if row and _cell(row[0]) == "End of register verified":
                verified = _cell(row[1] if len(row) > 1 else "").lower() == "yes"

    rows: dict[str, dict] = {}
    invalid, duplicates = [], 0
    for r_idx, row in enumerate(ws.iter_rows(min_row=hdr_row + 1, values_only=True),
                                start=hdr_row + 1):
        raw = _cell(row[cols["goc"]]) if len(row) > cols["goc"] else ""
        if not raw:
            continue
        if not gn.is_valid(raw):
            invalid.append({"row": r_idx, "value": raw})
            continue
        goc = gn.normalise(raw)
        vals = {f: (_cell(row[cols[f]]) if f in cols and len(row) > cols[f] else "") for f in FIELDS}
        status = _infer_status(vals)
        meta = statuses.get(goc)
        if meta and meta["status"] in (FOUND, PARTIAL, NO_RECORD, ERROR, PENDING):
            status = meta["status"]
        rec = {"goc_number": goc, **{f: (vals[f] or NOT_AVAILABLE) for f in FIELDS},
               "processing_status": status,
               "last_checked": (meta or {}).get("last_checked") or None,
               "error_message": (meta or {}).get("error") or None}
        if status in (NO_RECORD, ERROR, PENDING):
            for f in FIELDS:
                rec[f] = None
        if goc in rows:
            duplicates += 1
            if data_score(rec) <= data_score(rows[goc]):
                continue
        rows[goc] = rec

    by_prefix: dict[str, list[int]] = {}
    for g in rows:
        p, s, _ = gn.parse(g)
        by_prefix.setdefault(p, []).append(s)
    gaps = []
    for p, seqs in by_prefix.items():
        present = set(seqs)
        gaps += [gn.fmt(s, p) for s in range(min(seqs), max(seqs) + 1) if s not in present]

    counts = {k: 0 for k in (FOUND, PARTIAL, NO_RECORD, ERROR, PENDING)}
    for r in rows.values():
        counts[r["processing_status"]] += 1
    report = {
        "file": Path(path).name,
        "rows_read": len(rows),
        "duplicate_rows_in_file": duplicates,
        "invalid_goc_values": invalid[:50],
        "counts": counts,
        "first_goc": min(rows, key=lambda g: gn.parse(g)[1]) if rows else None,
        "last_goc": max(rows, key=lambda g: gn.parse(g)[1]) if rows else None,
        "missing_numbers_in_file": len(gaps),
        "missing_numbers_preview": gaps[:50],
        "end_verified": verified,
    }
    return list(rows.values()), report


def load_workbook_into(store: RecordStore, path: Path) -> dict:
    """Merge a workbook into the store (never downgrading better data)."""
    rows, report = read_workbook(path)
    result = {"inserted": 0, "updated": 0, "kept": 0}
    with store.transaction():          # one transaction: fast, and all-or-nothing
        for r in rows:
            if r["processing_status"] == PENDING:
                continue
            result[store.merge_import(r)] += 1
    if report["first_goc"] and not store.meta.get("start_goc"):
        store.meta["start_goc"] = report["first_goc"]
    report["merge"] = result
    return report
