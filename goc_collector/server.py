"""FastAPI backend for the simple dashboard (also serves the built React page).

Storage:
* ``output/GOC_Register.db`` - SQLite database, the source of truth. Every result
  is written to it immediately.
* ``output/GOC_Register.xlsx`` - the Excel export, written on pause, stop, finish
  and download, and on start-up when the database is newer than the Excel file.

On the very first start with a database, an existing ``GOC_Register.xlsx`` is
imported automatically, so earlier work carries on.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import goc_numbers as gn
from .collector import DEFAULT_START, CollectionConfig, Collector, compute_plan
from .excel_io import load_workbook_into, save_workbook
from .locking import CollectionLocked
from .store import MAX_ATTEMPTS, RecordStore

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = ROOT / "frontend" / "dist"


class StartRequest(BaseModel):
    start_goc: str | None = None
    # advanced (optional) settings
    batch_size: int | None = None
    workers: int | None = None
    min_interval: float | None = None
    tail_gap: int | None = None
    lookahead: int | None = None
    max_goc: str | None = None


def db_path_for(workbook: Path) -> Path:
    return Path(workbook).with_suffix(".db")


def open_store(workbook: Path) -> tuple[RecordStore, dict | None]:
    """Open the database next to ``workbook``. If the database is empty and the
    Excel file exists (first start after switching to the database, or a copied
    Excel file), import it. Returns the store and the import report (if any)."""
    workbook = Path(workbook)
    store = RecordStore(db_path_for(workbook))
    report = None
    if len(store) == 0 and workbook.exists():
        report = load_workbook_into(store, workbook)
        store.meta["end_verified"] = bool(report.get("end_verified"))
        # the Excel file already holds exactly this data
        store.meta["excel_version"] = store.version
        store.meta["excel_saved_at"] = dt.datetime.fromtimestamp(
            workbook.stat().st_mtime).replace(microsecond=0).isoformat(sep=" ")
        log.info("Imported %s into the database (%s numbers)", workbook.name, report["rows_read"])
    return store, report


def create_app(workbook: Path) -> FastAPI:
    workbook = Path(workbook)
    startup = {"imported": None, "error": None}
    try:
        store, imported = open_store(workbook)
        if imported:
            startup["imported"] = (f"Your earlier collection ({imported['rows_read']:,} GOC numbers) "
                                   f"was loaded from {workbook.name}. From now on every result is "
                                   f"saved immediately in {db_path_for(workbook).name}.")
    except Exception as exc:  # noqa: BLE001
        startup["error"] = f"Could not read {workbook.name}: {exc}"
        store = RecordStore(db_path_for(workbook))

    collector = Collector(store, save=lambda: save_workbook(store, workbook),
                          lock_path=str(ROOT / "logs" / workbook.name), excel_path=workbook)
    if len(store) and not collector.excel_up_to_date():
        collector.export_soon("start-up")        # e.g. after a crash or a closed window

    app = FastAPI(title="GOC Register Data Collector")
    app.state.store = store
    app.state.collector = collector
    plan_cache: dict = {}

    @app.on_event("shutdown")
    def shutdown():
        collector.close()
        store.close()

    def start_goc() -> str:
        return store.meta.get("start_goc") or store.stats()["first_goc"] or DEFAULT_START

    def next_goc() -> str | None:
        key = (store.version, start_goc())
        if plan_cache.get("key") != key:
            plan_cache["key"] = key
            plan_cache["value"] = compute_plan(store, start_goc()).describe()["next_goc"]
        return plan_cache["value"]

    def ensure_idle():
        if collector.busy:
            raise HTTPException(409, "Please stop the collection first.")
        if collector.lock.is_locked_elsewhere():
            raise HTTPException(409, "The collection is already running in another window.")

    @app.get("/api/status")
    def status():
        p = collector.progress
        st = store.stats(gn.parse(start_goc())[0])
        running = collector.busy and p.state in ("running", "pausing", "stopping")
        elapsed = (time.time() - p.started_ts) if p.started_ts and collector.busy else 0
        waiting = collector.client.waiting_seconds() if (collector.client and running) else 0
        speed = round(p.run_checked / (elapsed / 60), 1) if elapsed > 120 else None
        return {
            "state": p.state if (collector.busy or p.state != "idle") else "idle",
            "busy": collector.busy,
            "running_elsewhere": (not collector.busy) and collector.lock.is_locked_elsewhere(),
            "activity": p.activity,
            "phase": p.phase,
            "current_goc": p.current_goc,
            "seconds_since_activity": round(time.time() - p.last_activity_ts, 1) if p.last_activity_ts else None,
            "waiting_seconds": round(waiting),
            "elapsed_seconds": round(elapsed),
            "speed_per_minute": speed,
            "run": {"checked": p.run_checked, "found": p.run_found + p.run_partial,
                    "partial": p.run_partial, "no_record": p.run_no_record,
                    "errors": p.run_errors},
            "recent": p.recent[:12],
            "excel": {
                "file_name": workbook.name,
                "exists": workbook.exists(),
                "up_to_date": collector.excel_up_to_date(),
                "updating": collector.exporting,
                "last_saved": store.meta.get("excel_saved_at"),
                "problem": p.save_problem,
            },
            "db_file": db_path_for(workbook).name,
            "file_folder": str(workbook.parent),
            "end_verified": bool(store.meta.get("end_verified")),
            "totals": st,
            "max_attempts": MAX_ATTEMPTS,
            "has_data": st["total_rows"] > 0,
            "start_goc": start_goc(),
            "next_goc": next_goc(),
            "messages": list(collector.events)[:15],
            "startup_error": startup["error"],
            "startup_notice": startup["imported"],
        }

    @app.post("/api/start")
    def start(req: StartRequest):
        ensure_idle()
        has_data = len(store) > 0
        try:
            first = req.start_goc if (req.start_goc and not has_data) else start_goc()
            cfg = CollectionConfig.from_dict({**req.model_dump(), "start_goc": first})
        except (gn.InvalidGocNumber, ValueError, TypeError) as exc:
            raise HTTPException(400, f"Please check the GOC number (example: 01-11200). {exc}")
        store.meta["start_goc"] = cfg.start_goc
        startup["imported"] = None
        try:
            collector.start(cfg, mode="update" if has_data else "new")
        except CollectionLocked as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}

    @app.post("/api/pause")
    def pause():
        collector.pause()
        return {"ok": True}

    @app.post("/api/resume")
    def resume():
        collector.resume()
        return {"ok": True}

    @app.post("/api/stop")
    def stop():
        collector.stop()
        return {"ok": True}

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...)):
        ensure_idle()
        if not file.filename.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(400, "Please choose an Excel file (.xlsx).")
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            shutil.copyfileobj(file.file, tmp)
        try:
            report = load_workbook_into(store, Path(tmp.name))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"This file could not be read: {exc}")
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        if report.get("first_goc"):
            fp, fs, _ = gn.parse(report["first_goc"])
            cp, cs, _ = gn.parse(start_goc())
            if fp == cp and fs < cs:
                store.meta["start_goc"] = report["first_goc"]
        report["file"] = Path(file.filename).name
        collector.export_soon("upload")
        return report

    @app.get("/api/download")
    def download():
        """Always builds a fresh Excel file from the database (also while
        collecting, and even if the copy in the output folder is open in Excel)."""
        if len(store) == 0:
            raise HTTPException(404, "Nothing collected yet.")
        name = f"GOC_Register_{dt.date.today().isoformat()}.xlsx"
        tmpdir = Path(tempfile.mkdtemp(prefix="goc_download_"))
        save_workbook(store, tmpdir / name)
        if not collector.busy:
            collector.export_soon("download")     # keep the copy in the output folder current
        return FileResponse(tmpdir / name, filename=name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True))

    @app.post("/api/retry-given-up")
    def retry_given_up():
        ensure_idle()
        n = store.reset_attempts(gn.parse(start_goc())[0])
        return {"ok": True, "count": n}

    @app.post("/api/start-over")
    def start_over():
        ensure_idle()
        stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
        backups = []
        if workbook.exists():
            backup = workbook.with_name(f"{workbook.stem}_backup_{stamp}.xlsx")
            try:
                os.replace(workbook, backup)
            except PermissionError:
                raise HTTPException(409, f"Please close {workbook.name} in Excel first, then try again.")
            backups.append(backup.name)
        if len(store):
            db_backup = workbook.with_name(f"{workbook.stem}_backup_{stamp}.db")
            store.backup_to(db_backup)
            backups.append(db_backup.name)
        store.clear()
        startup["imported"] = None
        return {"ok": True, "backup": " and ".join(backups) if backups else None}

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/", response_class=HTMLResponse)
        def index():
            return (FRONTEND_DIST / "index.html").read_text(encoding="utf-8")
    else:
        @app.get("/", response_class=HTMLResponse)
        def index_missing():
            return "<h2>Dashboard not built</h2><p>Run <code>npm install && npm run build</code> in <code>frontend</code>.</p>"

    return app
