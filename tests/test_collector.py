"""Offline tests: collection, smart resume from Excel, autosave, Excel round trip.

A fake register client stands in for https://str.optical.org so the tests are
deterministic and never load the real website.
"""

from __future__ import annotations

import threading
import time

import pytest
from openpyxl import Workbook, load_workbook

from goc_collector import NOT_AVAILABLE, SEARCH_FAILED
from goc_collector import goc_numbers as gn
from goc_collector.collector import CollectionConfig, Collector, compute_plan
from goc_collector.excel_io import HEADERS, MAIN_SHEET, load_workbook_into, save_workbook
from goc_collector.scraper import (ListHit, Registrant, RegisterError, parse_detail_html,
                                   parse_list_html)
from goc_collector.store import ERROR, FOUND, NO_RECORD, PARTIAL, RecordStore


class FakeClient:
    def __init__(self, people, fail=None, fail_times=10**9, delay=0.0):
        self.people = people
        self.fail = set(fail or ())
        self.fail_left = {s: fail_times for s in self.fail}
        self.delay = delay
        self.searched: list[str] = []
        self.request_count = 0
        self.lock = threading.Lock()

    def _seq(self, pin):
        return gn.parse(pin)[1]

    def waiting_seconds(self):
        return 0

    def search_pins(self, pins):
        with self.lock:
            self.request_count += 1
            self.searched.extend(pins)
            for p in pins:
                s = self._seq(p)
                if s in self.fail and self.fail_left[s] > 0:
                    self.fail_left[s] -= 1
                    raise RegisterError("HTTP 503 (simulated)")
        if self.delay:
            time.sleep(self.delay)
        return {p: ListHit(p, self.people[self._seq(p)]["name"], f"/Registrant/{p}")
                for p in pins if self._seq(p) in self.people}

    def count_pins(self, pins):
        self.request_count += 1
        return sum(1 for p in pins if self._seq(p) in self.people)

    def fetch_detail(self, hit):
        d = self.people[self._seq(hit.goc_number)]
        r = Registrant(goc_number=hit.goc_number, name=d["name"], town=d.get("town", ""),
                       date_registered=d.get("date", ""), practice_address=d.get("addr", ""))
        for attr, label in (("town", "Town"), ("date_registered", "Date Registered"),
                            ("practice_address", "Practice Address")):
            if not getattr(r, attr):
                r.missing.append(label)
                setattr(r, attr, NOT_AVAILABLE)
        return r


def person(i, partial=False, date="01-01-2020"):
    d = {"name": f"Person {i}", "town": f"Town {i}", "date": date, "addr": f"{i} High St, AB1 2CD"}
    if partial:
        d.pop("addr")
    return d


def cfg(**kw):
    base = dict(start_goc="01-11200", batch_size=5, workers=1, min_interval=0.2,
                tail_gap=50, lookahead=200, probe_width=100, retry_passes=2, retry_cooldown=0,
                autosave_every=0.2)
    base.update(kw)
    return CollectionConfig.from_dict(base)


def run(store, client, xlsx=None, **kw):
    mode = kw.pop("mode", "new")
    col = Collector(store, save=(lambda: save_workbook(store, xlsx)) if xlsx else None,
                    lock_path=str(xlsx) if xlsx else None)
    col.start(cfg(**kw), mode=mode, client=client)
    col.join(60)
    assert not col.busy
    return col


def rows_of(xlsx):
    return {r[0]: r for r in load_workbook(xlsx)[MAIN_SHEET].iter_rows(min_row=2, values_only=True)}


PEOPLE = {11200: person(11200), 11203: person(11203, partial=True),
          11210: person(11210, date="04-11-2023"), 11290: person(11290, date="04-10-2026")}


@pytest.fixture
def store():
    return RecordStore()


# ---- Test 1: first-time collection starts at 01-11200, ends by the stop rule, Excel saved
def test_first_collection(store, tmp_path):
    xlsx = tmp_path / "GOC_Register.xlsx"
    client = FakeClient(PEOPLE)
    col = run(store, client, xlsx)
    assert client.searched[0] == "01-11200"
    assert col.progress.state == "completed" and col.progress.end_verified
    st = store.stats()
    assert st["found"] == 3 and st["partial"] == 1
    assert st["last_found_goc"] == "01-11290" and st["latest_date"] == "04-10-2026"
    assert len(store.statuses_between("01-", 11200, 11340)) == 141     # nothing skipped
    rows = rows_of(xlsx)                                                # saved to Excel
    assert list(rows) == [gn.fmt(s) for s in range(11200, 11291)]
    assert col.progress.recent and col.progress.recent[0]["goc"].startswith("01-")


def test_block_gap_larger_than_tail_is_found_by_lookahead(store):
    people = {11200: person(11200), 11370: person(11370)}
    col = run(store, FakeClient(people))
    assert store.stats()["last_found_goc"] == "01-11370" and col.progress.end_verified


# ---- Test 2: uploaded workbook -> next unprocessed number
def test_resume_from_excel_only(store, tmp_path):
    xlsx = tmp_path / "prev.xlsx"
    run(store, FakeClient(PEOPLE), xlsx)
    fresh = RecordStore()
    report = load_workbook_into(fresh, xlsx)
    assert report["first_goc"] == "01-11200" and report["last_goc"] == "01-11290"
    assert report["end_verified"] is True
    plan = compute_plan(fresh, "01-11200")
    assert plan.gaps == [] and plan.describe()["next_goc"] == "01-11291"


def test_legacy_sheet_with_gaps_and_failures(store, tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(HEADERS)
    ws.append(["01-11200", "A", "T", "01-01-2000", "Addr"])
    ws.append(["01-11201", NOT_AVAILABLE, NOT_AVAILABLE, NOT_AVAILABLE, NOT_AVAILABLE])
    ws.append(["01-11202", SEARCH_FAILED, SEARCH_FAILED, SEARCH_FAILED, SEARCH_FAILED])
    ws.append(["01-11204", "B", NOT_AVAILABLE, "02-02-2000", NOT_AVAILABLE])
    p = tmp_path / "legacy.xlsx"
    wb.save(p)
    report = load_workbook_into(store, p)
    assert report["counts"] == {"found": 1, "partial": 1, "no_record": 1, "error": 1, "pending": 0}
    assert report["missing_numbers_preview"] == ["01-11203"]
    plan = compute_plan(store, "01-11200")
    assert [gn.fmt(s) for s in plan.gaps] == ["01-11202", "01-11203"]
    assert store.get("01-11201")["processing_status"] == NO_RECORD


# ---- Test 3: no duplicates, history not searched again
def test_update_does_not_research_history(store, tmp_path):
    xlsx = tmp_path / "g.xlsx"
    run(store, FakeClient(PEOPLE), xlsx)
    people2 = dict(PEOPLE)
    people2[11320] = person(11320, date="01-11-2026")
    client2 = FakeClient(people2)
    store2 = RecordStore()
    load_workbook_into(store2, xlsx)          # e.g. months later, from the Excel file
    col = run(store2, client2, xlsx, mode="update")
    assert min(gn.parse(p)[1] for p in client2.searched) == 11291
    assert store2.get("01-11320")["processing_status"] == FOUND
    keys = list(rows_of(xlsx))
    assert len(keys) == len(set(keys)) and keys[-1] == "01-11320"
    assert col.progress.end_verified


def test_import_never_downgrades(store, tmp_path):
    run(store, FakeClient(PEOPLE))
    wb = Workbook()
    ws = wb.active
    ws.append(HEADERS)
    ws.append(["01-11200", NOT_AVAILABLE, NOT_AVAILABLE, NOT_AVAILABLE, NOT_AVAILABLE])
    ws.append(["01-11203", "Person 11203", "Town 11203", "01-01-2020", "New address"])
    p = tmp_path / "worse.xlsx"
    wb.save(p)
    load_workbook_into(store, p)
    assert store.get("01-11200")["name"] == "Person 11200"
    assert store.get("01-11203")["practice_address"] == "New address"


# ---- Test 4: missing records
def test_missing_records_written_as_not_available(store, tmp_path):
    xlsx = tmp_path / "o.xlsx"
    run(store, FakeClient(PEOPLE), xlsx)
    rows = rows_of(xlsx)
    assert rows["01-11201"] == ("01-11201",) + (NOT_AVAILABLE,) * 4
    assert rows["01-11203"][4] == NOT_AVAILABLE and rows["01-11203"][1] == "Person 11203"


# ---- Test 5: temporary errors kept separate and retried
def test_errors_are_separate_and_retryable(store, tmp_path):
    xlsx = tmp_path / "e.xlsx"
    col = run(store, FakeClient(PEOPLE, fail={11210}, fail_times=100), xlsx, retry_passes=1)
    assert store.get("01-11210")["processing_status"] == ERROR
    assert not col.progress.end_verified
    assert rows_of(xlsx)["01-11210"][1] == SEARCH_FAILED      # never "Data not available"
    s2 = RecordStore()
    load_workbook_into(s2, xlsx)
    assert compute_plan(s2, "01-11200").gaps == [11210]
    run(s2, FakeClient(PEOPLE), xlsx, mode="update")
    assert s2.get("01-11210")["processing_status"] == FOUND


def test_transient_error_recovered_within_run(store):
    col = run(store, FakeClient(PEOPLE, fail={11203}, fail_times=2))
    assert store.get("01-11203")["processing_status"] == PARTIAL and col.progress.end_verified


# ---- Test 6: pause / resume / stop without losing progress
def test_pause_resume_stop_and_continue(store, tmp_path):
    xlsx = tmp_path / "p.xlsx"
    people = {s: person(s) for s in range(11200, 11400, 3)}
    col = Collector(store, save=lambda: save_workbook(store, xlsx), lock_path=str(xlsx))
    col.start(cfg(), client=FakeClient(people, delay=0.05))
    time.sleep(0.6)
    col.pause()
    deadline = time.time() + 10
    while col.progress.state != "paused" and time.time() < deadline:
        time.sleep(0.05)
    assert col.progress.state == "paused"
    n1 = store.stats()["processed"]
    time.sleep(0.4)
    assert store.stats()["processed"] == n1
    assert xlsx.exists()                         # saved when paused
    col.resume()
    time.sleep(0.4)
    col.stop()
    col.join(30)
    assert col.progress.state == "stopped"
    # continue from the Excel file alone
    s2 = RecordStore()
    load_workbook_into(s2, xlsx)
    plan = compute_plan(s2, "01-11200")
    client2 = FakeClient(people)
    run(s2, client2, xlsx, mode="update")
    assert min(gn.parse(p)[1] for p in client2.searched) >= min([plan.continue_from] + plan.gaps)
    assert s2.stats()["last_found_goc"] == gn.fmt(max(people))


def test_only_one_collection_per_file(store, tmp_path):
    from goc_collector.locking import CollectionLocked
    xlsx = tmp_path / "l.xlsx"
    people = {s: person(s) for s in range(11200, 11400)}
    a = Collector(store, save=lambda: save_workbook(store, xlsx), lock_path=str(xlsx))
    a.start(cfg(), client=FakeClient(people, delay=0.05))
    b = Collector(RecordStore(), lock_path=str(xlsx))
    with pytest.raises(CollectionLocked):
        b.start(cfg(), client=FakeClient(people))
    a.stop()
    a.join(30)


# ---- Test 7 & 8: history + new records, columns and formatting
def test_updated_workbook_keeps_history_and_format(store, tmp_path):
    xlsx = tmp_path / "w.xlsx"
    run(store, FakeClient(PEOPLE), xlsx)
    people2 = dict(PEOPLE)
    people2[11300] = person(11300, date="02-11-2026")
    s2 = RecordStore()
    load_workbook_into(s2, xlsx)
    run(s2, FakeClient(people2), xlsx, mode="update")
    wb = load_workbook(xlsx)
    ws = wb[MAIN_SHEET]
    assert [c.value for c in ws[1]] == HEADERS
    assert ws.freeze_panes == "A2"
    rows = rows_of(xlsx)
    assert rows["01-11200"][1] == "Person 11200" and rows["01-11300"][1] == "Person 11300"
    assert list(rows) == [gn.fmt(s) for s in range(11200, 11301)]
    assert {"Collection Status", "Summary"} <= set(wb.sheetnames)


def test_save_when_file_open_in_excel_is_reported(store, tmp_path, monkeypatch):
    from goc_collector import excel_io
    run(store, FakeClient(PEOPLE))

    def locked(*a, **kw):
        raise PermissionError("in use")
    monkeypatch.setattr(excel_io.os, "replace", locked)
    with pytest.raises(excel_io.WorkbookInUse):
        save_workbook(store, tmp_path / "x.xlsx")


# ---- parsers against markup copied from the live register
LIST_HTML = """<div class="card" id="search-results"><span id="searchResultCount">
 <span>Registrant (2 Results)</span></span><ul class="list-unstyled">
 <li class="media mb-3"><div class="media-body"><strong class="mt-0 mb-1 title-font">Andrea Lee (01-11231)</strong><br />
 <b>Registration Status: </b><b style="color: #4E8A51;">Registered</b><br />
 <b>Date of most recent registration: </b>07-01-83 <br /><b>Registered as: </b>Optometrist<br /></div>
 <a href="/Registrant/041A?filterId=Registrant" class="btn">View</a></li>
 <li class="media mb-3"><div class="media-body"><strong class="mt-0 mb-1 title-font">Mary-Jo O'Neil (Smith) (01-11238)</strong><br />
 </div><a href="/Registrant/141A?filterId=Registrant" class="btn">View</a></li></ul></div>"""

DETAIL_HTML = """<h1 class="title-font" id="detail-title"><strong>Andrew Smith </strong> <br /><strong>01-11023</strong></h1>
<div class="media"><div class="media-body" aria-label="GOC Number"><label>GOC Number</label><span class="d-block">01-11023</span></div></div>
<div class="media"><div class="media-body"><label>Registration Status</label><span class="d-block"><i></i>Registered</span></div></div>
<div class="media"><div class="media-body"><label>Date of most recent registration</label><span class="d-block">23-07-1982</span></div></div>
<div class="media"><div class="media-body"><label>Town</label>
  <span class="d-block">Harrogate</span></div></div>
<div class="media"><div class="media-body"><label>Practice addresses</label><span class="d-block">Doncaster Royal Infirmary, DN2 5LT, UNITED KINGDOM</span></div></div>
<div class="media"><div class="media-body" aria-label=""><span class="d-block">University of Bradford, BD7 1DP, UNITED KINGDOM</span></div></div>
<div class="media"><div class="media-body"><label>FTP Decisions</label><span class="d-block">None</span></div></div>"""


def test_parse_list():
    total, hits = parse_list_html(LIST_HTML)
    assert total == 2 and [h.goc_number for h in hits] == ["01-11231", "01-11238"]
    assert hits[1].name == "Mary-Jo O'Neil (Smith)"


def test_parse_list_rejects_garbage():
    with pytest.raises(RegisterError):
        parse_list_html("<html>Service unavailable</html>")


def test_parse_detail_multiple_addresses():
    r = parse_detail_html(DETAIL_HTML)
    assert (r.goc_number, r.name, r.town, r.date_registered) == \
        ("01-11023", "Andrew Smith", "Harrogate", "23-07-1982")
    assert r.practice_address.count("; ") == 1 and r.missing == []


def test_goc_number_format():
    assert gn.parse("01-11200") == ("01-", 11200, 5)
    assert gn.normalise(" 01 - 09999 ") == "01-09999"
