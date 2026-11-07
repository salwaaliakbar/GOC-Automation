"""Offline tests for the SQLite database (source of truth) and the Excel export."""

from __future__ import annotations

import time

from goc_collector import goc_numbers as gn
from goc_collector.collector import Collector, compute_plan
from goc_collector.excel_io import save_workbook
from goc_collector.server import db_path_for, open_store
from goc_collector.store import ERROR, FOUND, MAX_ATTEMPTS, NO_RECORD, RecordStore

from test_collector import PEOPLE, FakeClient, cfg, person, rows_of, run


def test_every_result_survives_a_crash_without_any_excel_save(tmp_path):
    """Results are in the database the moment they arrive - no Excel save needed."""
    db = tmp_path / "GOC_Register.db"
    store = RecordStore(db)
    people = {s: person(s) for s in range(11200, 11400, 3)}
    col = Collector(store, save=None)                 # no Excel export at all
    col.start(cfg(), client=FakeClient(people, delay=0.02))
    time.sleep(0.5)
    col.stop()
    col.join(30)
    done = store.stats()["processed"]
    version = store.version
    assert done > 0
    store._conn.close()                               # "crash": no clean shutdown

    again = RecordStore(db)
    assert again.stats()["processed"] == done
    assert again.version == version
    assert again.get("01-11200")["processing_status"] == FOUND
    plan = compute_plan(again, "01-11200")
    client = FakeClient(people)
    run(again, client, mode="update")
    assert min(gn.parse(p)[1] for p in client.searched) >= min([plan.continue_from] + plan.gaps)
    assert again.stats()["last_found_goc"] == gn.fmt(max(people))


def test_meta_is_persisted(tmp_path):
    db = tmp_path / "m.db"
    s = RecordStore(db)
    s.meta["start_goc"] = "01-11200"
    s.meta["end_verified"] = True
    s.close()
    s2 = RecordStore(db)
    assert s2.meta["start_goc"] == "01-11200" and s2.meta.get("end_verified") is True
    s2.clear()
    assert len(s2) == 0 and s2.meta.get("start_goc") is None


def test_given_up_numbers_are_not_retried_until_reset():
    s = RecordStore()
    s.save_result("01-11200", FOUND, {"name": "A", "town": "T", "date_registered": "01-01-2000",
                                      "practice_address": "X"})
    for _ in range(MAX_ATTEMPTS):
        s.save_result("01-11201", ERROR, error="timeout")
    s.save_result("01-11202", ERROR, error="timeout")
    s.save_result("01-11203", FOUND, {"name": "B", "town": "T", "date_registered": "01-01-2000",
                                      "practice_address": "X"})
    assert s.stats()["gave_up"] == 1
    assert compute_plan(s, "01-11200").gaps == [11202]          # 11201 given up
    assert s.unresolved_numbers("01-", MAX_ATTEMPTS) == ["01-11202"]
    assert s.reset_attempts() == 1
    assert compute_plan(s, "01-11200").gaps == [11201, 11202]


def test_error_never_downgrades_a_resolved_number():
    s = RecordStore()
    s.save_result("01-11200", NO_RECORD)
    s.save_result("01-11200", ERROR, error="timeout")
    assert s.get("01-11200")["processing_status"] == NO_RECORD


def test_existing_excel_is_imported_once_into_a_new_database(tmp_path):
    xlsx = tmp_path / "GOC_Register.xlsx"
    s = RecordStore()
    run(s, FakeClient(PEOPLE), xlsx)                  # an Excel file from before the database
    assert not db_path_for(xlsx).exists()

    store, report = open_store(xlsx)
    assert report["rows_read"] == 91 and store.stats()["found"] == 3
    col = Collector(store, save=lambda: save_workbook(store, xlsx), excel_path=xlsx)
    assert col.excel_up_to_date()                     # no needless re-export
    store.close()

    store2, report2 = open_store(xlsx)                # second start: database used, no import
    assert report2 is None and store2.stats()["found"] == 3


def test_excel_open_does_not_stop_collection_and_export_retries(tmp_path):
    xlsx = tmp_path / "GOC_Register.xlsx"
    store = RecordStore(tmp_path / "GOC_Register.db")
    calls = {"n": 0}

    def flaky_save():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("The Excel file could not be updated because it is open")
        save_workbook(store, xlsx)

    col = Collector(store, save=flaky_save, excel_path=xlsx)
    col.export_retry_every = 0.1
    col.start(cfg(), client=FakeClient(PEOPLE))
    col.join(60)
    assert col.progress.state == "completed"          # collection was not affected
    assert store.stats()["found"] == 3
    deadline = time.time() + 10
    while not col.excel_up_to_date() and time.time() < deadline:
        time.sleep(0.05)
    assert col.excel_up_to_date() and calls["n"] >= 3
    assert col.progress.save_problem == ""
    assert "01-11290" in rows_of(xlsx)


def test_excel_rebuilt_when_database_is_newer(tmp_path):
    """After a crash the Excel copy is older than the database; it is detected."""
    xlsx = tmp_path / "GOC_Register.xlsx"
    store = RecordStore(tmp_path / "GOC_Register.db")
    col = Collector(store, save=lambda: save_workbook(store, xlsx), excel_path=xlsx)
    store.save_result("01-11200", NO_RECORD)
    assert not col.excel_up_to_date()
    assert col.save_now("test") and col.excel_up_to_date()
    store.save_result("01-11201", NO_RECORD)          # new data, Excel not written
    assert not col.excel_up_to_date()
    xlsx.unlink()
    col.save_now("test")
    assert xlsx.exists() and col.excel_up_to_date()
