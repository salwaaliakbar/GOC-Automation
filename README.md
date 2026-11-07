# GOC Register Data Collector

This program collects optometrist details from the official **General Optical Council register**
(<https://str.optical.org/>) and puts them in an **Excel file**.

For every GOC number, the Excel file shows:

| Column | What it shows |
|---|---|
| A – GOC No. | the GOC number, for example `01-11200` |
| B – Name | the optometrist's name |
| C – Town | their town |
| D – Date Registered | date of most recent registration (`DD-MM-YYYY`) |
| E – Practice Address | all practice addresses, separated by `;` |

---

# Step-by-step guide

## Part 1 – Set up the computer (only once)

You only need to do this **once per computer**.

**Step 1 – Install Python** (a free program the collector needs)

1. Open this page: <https://www.python.org/downloads/>
2. Click the big yellow **Download Python** button.
3. Open the file you downloaded.
4. **Important:** at the bottom of the first window, tick the box
   **"Add python.exe to PATH"**.
5. Click **Install Now** and wait until it says *"Setup was successful"*. Click **Close**.

**Step 2 – Put the collector folder on the computer**

Copy the whole collector folder (the one that contains this guide) somewhere easy to find,
for example your **Desktop**.

That's all. The first time you start the collector (Part 2), it finishes the setup by itself.
This takes about a minute and needs an internet connection.

---

## Part 2 – Collect the data

**Step 1 – Start the collector**

Open the collector folder and **double-click `run_dashboard.bat`**.

* A **black window** opens. **Leave it open** the whole time. You can minimise it, but do not
  close it.
* After a few seconds, the collector opens in your **web browser**.

> If Windows shows a blue box saying *"Windows protected your PC"*, click **More info**, then
> **Run anyway**.

**Step 2 – Press Start**

* **The first time**, you see a box *"Start from GOC number"* with **`01-11200`** already filled
  in. **Leave it as it is** and press the green **▶ Start collecting** button.
* **Any later time**, the button says **▶ Continue collecting**. Press it and the collector
  carries on from where it stopped.

**Step 3 – Let it run**

The screen updates by itself every second. It shows:

* what it is doing right now, for example *"Reading the details of 01-12305 (Jane Smith)"*;
* the GOC number it has reached, how many numbers were checked, and how many optometrists were found;
* a list of the latest numbers checked.

Sometimes it says *"The register website is busy … it will continue automatically in 1:45"*.
**This is normal.** The collector gives the website a short rest. Just leave it running.

**Step 4 – Take a break whenever you like**

* **❚❚ Pause** – stops for a moment. Press **▶ Continue** to carry on.
* **■ Stop** – stops completely. Next time, press **▶ Continue collecting**.
* You may even **close the browser and the black window**, or switch off the computer.

**Nothing is ever lost.** Every result is saved the moment it arrives.

**Step 5 – Get your Excel file**

When the screen says **"Finished – all available records collected"**, press the blue
**⬇ Download Excel** button. The file is saved in your **Downloads** folder with today's date,
for example `GOC_Register_2026-11-05.xlsx`.

You can also press **⬇ Download Excel** at any time **during** the collection to see what has
been collected so far.

---

## Part 3 – Using it on a new computer

On a new computer, the collector **starts from the very beginning (GOC number `01-11200`)**.

1. Do **Part 1** on the new computer (install Python, copy the collector folder).
2. Do **Part 2**. You will see *"Start from GOC number: 01-11200"*. **Leave it as it is** and
   press **▶ Start collecting**.

You do not need to copy or upload anything.

---

## Part 4 – A few months later (new registrations)

New optometrists get new GOC numbers over time. To add them to your list:

1. Double-click `run_dashboard.bat` **on the same computer** as before.
2. Press **▶ Continue collecting**.

The collector checks only the new numbers and adds them to the same list. When it says
**Finished**, press **⬇ Download Excel**.

---

## Where are my files?

Everything is kept in the **`output`** folder inside the collector folder:

| File | What it is |
|---|---|
| `GOC_Register.db` | **The saved collection.** Every result is saved here straight away. **Do not delete this file**, or the collector starts again from the beginning. |
| `GOC_Register.xlsx` | The Excel file. It is updated when you pause, stop, or when the collection finishes. |

The **"Your data"** box on the screen always tells you whether the Excel file is up to date.

---

## Good to know

* **Keep the computer awake.** The computer must stay switched on and must not go to sleep while
  collecting. Go to **Settings → System → Power & battery → Screen and sleep** and set
  *"When plugged in, put my device to sleep after"* to **Never**.
* **It takes a long time.** A full collection covers about 38,000 GOC numbers and takes many
  hours, because the register website is slow and the collector deliberately goes gently. You can
  spread it over several days using **Stop** and **Continue collecting**.
* **Keep the Excel file in the `output` folder closed while collecting.** Open the downloaded
  copy instead. If it is open, collecting carries on as normal and the screen asks you to close
  the file. It updates by itself once it is closed.
* **Numbers that did not answer.** If the website does not answer for a number, the collector
  tries again automatically. After 5 tries it skips that number for now (it shows as a red row in
  Excel). Under **Other options**, press **"Try the skipped numbers again"** to retry them.
* **Starting again from zero.** Under **Other options**, press **"Start a completely new list"**.
  The old data is kept as a backup copy in the `output` folder.

---

## Help – something is not working

| What you see | What to do |
|---|---|
| *"Python is not installed on this computer"* in the black window | Do **Part 1, Step 1** again and make sure you tick **"Add python.exe to PATH"**. |
| *"Installation failed"* in the black window | Check the internet connection, close the black window, and double-click `run_dashboard.bat` again. |
| The browser says *"Cannot reach the collector"* | The black window was closed. Double-click `run_dashboard.bat` again. Nothing is lost. |
| The browser page did not open | Open your browser and go to **http://127.0.0.1:8000** |
| *"The collection is already running in another window"* | The collector is already open. Look for the other browser tab or black window. |
| *"The Excel file could not be updated because … is open in Excel"* | Close that Excel file. It updates by itself a few seconds later. Your data is safe. |
| It says *"Waiting for the register website to answer"* for a long time | The website is slow. Leave it running; it keeps trying by itself. |

**Optional (for later):** to **continue** an earlier collection on a new computer instead of
starting from the beginning, close the collector on both computers and copy the file
`output\GOC_Register.db` from the old computer into the `output` folder on the new one before
pressing Start.

---

# For developers

## How it works (technical)

### Storage: SQLite database + Excel export
* **`output/GOC_Register.db`** (SQLite, built into Python, nothing to install) is the source of
  truth. Each result is written in its own transaction the moment it arrives (WAL journal,
  `synchronous=FULL`), so a crash, power cut or closed window loses nothing that was already
  shown on the screen.
* Only what is needed is read into memory: the highest registered number, the numbers to retry,
  and totals (SQL queries on indexed columns). Memory stays small however large the register
  grows.
* **`output/GOC_Register.xlsx`** is an export, written atomically (temporary file, then
  rename) on pause, stop, finish and download. The rows are streamed from the database in chunks.
  The database stores which data version the Excel file holds, so on start-up a stale Excel file
  (after a crash) is rebuilt automatically. If the file is open in Excel, the export is retried
  every 10 s in the background; collecting is not affected.
* **Download Excel** always builds a fresh file from the database into a temporary location.
* **Migration:** on the first start with an empty database, an existing `GOC_Register.xlsx` is
  imported automatically (one transaction).
* On shutdown (Ctrl+C) the collection is stopped and the Excel file updated once more.

The workbook has three sheets:

* **GOC Register**: columns A–E exactly as above, one row per GOC number from the first number
  to the highest registered number, in order, none skipped.
* **Collection Status**: processing status of every number (`found`, `partial`, `no_record`,
  `error`), when it was checked, and any error. This makes exact resuming possible from the
  Excel file alone (for example on a computer without the database file).
* **Summary**: totals, latest registration date, and whether the end of the register was verified.

Workbooks without a *Collection Status* sheet (for example a hand-made list) can also be loaded.
Status is then inferred from the cell values.

| Status | Meaning | In the Excel sheet |
|---|---|---|
| `found` | record collected, all 4 fields present | values |
| `partial` | record exists, some fields missing on the register | values + `Data not available` in the missing cells |
| `no_record` | search succeeded, nobody has this number | `Data not available` in B–E |
| `error` | the website did not answer, **not** treated as "no record" | `Search failed - retry required` (red row), retried automatically |

### Smart resume
* Numbers between the start and the highest registered number that are failed or missing are
  done first.
* Collection then continues after the highest registered number. Numbers there that were empty
  last time are checked again, because that is where new registrations appear.
* Numbers already confirmed (`found`, `partial`, `no_record`) are never searched again. Loading a
  workbook never replaces existing data with less complete data.
* A failed number is retried at most 5 times in total (`MAX_ATTEMPTS` in `store.py`); after that
  it stays `error` but is skipped until reset from the screen.

### Where collection stops (no fixed end number)
GOC numbers are issued in **blocks**, so the register has runs of several hundred unused numbers
between populated ranges. The collector continues until **2,000 consecutive numbers** after the
highest registered number are empty. It then checks the next **10,000** numbers in wide batches,
and if anything turns up there, collection continues. Only then does it report the end of the
register as verified. The Summary sheet shows the newest registration date found.

### Website access and politeness
* Only public pages are used: the register's own search (including its "separate multiple GOC
  numbers with a comma" feature) and each registrant's public page. There is no login or CAPTCHA,
  and the site publishes no `robots.txt` restrictions.
* One request at a time, at least 3 s apart, and never sooner than 1 s after the previous answer
  arrived (so a slow answer is never followed by an immediate new request).
* If the register is slow overall (median of the last 10 answers above 5 s), the gap widens
  automatically (up to 15 s). One single slow page does not slow the whole collection down.
* A failing registrant page is tried at most twice, then the number is retried later in a
  separate round, instead of asking the register for the same heavy page again and again.
* If 4 *different* requests in a row fail, everything pauses for 2 minutes (circuit breaker).
  HTTP 429, and 503 with `Retry-After`, make the collector wait as long as the register asks.
* Each request has a hard 40 s limit, and failed numbers are retried later.
* Cookies are not stored. The register's load-balancer cookie would otherwise pin the collector
  to one, sometimes overloaded, server.
* Every registrant page must show exactly the GOC number searched before it is stored. Search
  results are checked against the result count the register reports.
* Only one collection can run per Excel file at a time (a lock file in `logs/`).

### Project structure
```
run_dashboard.bat     double-click to start (Windows)
goc_collector/
  goc_numbers.py      GOC number parsing/formatting (keeps leading zeros and hyphen)
  scraper.py          register client: rate limiting, retries, circuit breaker, HTML parsing
  store.py            SQLite record store (statuses, merge rules, statistics, retry limit)
  collector.py        collection engine: smart resume, stop rule, pause/stop, Excel export, live progress
  excel_io.py         export the database to Excel / import an Excel file
  locking.py          one collection per file
  server.py           backend for the screen (FastAPI)
  cli.py              entry point
frontend/             the screen (React); ready-built files in frontend/dist
tests/                automatic tests (simulated register, no internet needed)
output/               GOC_Register.db (the collection) and GOC_Register.xlsx (Excel export)
logs/                 collector.log (technical log)
```

### Command line (optional)
```bash
python -m goc_collector                          # start the screen (same as run_dashboard.bat)
python -m goc_collector collect                  # collect without the screen
python -m goc_collector --file other.xlsx        # work on a different Excel file
```

### Tests
```bash
python -m pytest -q
```
The tests cover: first collection from `01-11200`; resuming from the Excel file alone (including
plain sheets with gaps and failed rows); no duplicates and no re-searching of history; "Data not
available" handling; failed searches kept separate and retried; pause/resume/stop without loss;
updated workbook with history plus new records; column structure and formatting; one collection
per file; Excel open while saving; and the website parsers, timeouts and circuit breaker; the
database (results survive a crash without any Excel save, settings persist, retry limit,
one-time import of an existing Excel file, Excel export retried while the file is open, stale
Excel detected).

### Rebuilding the screen (developers only)
```bash
cd frontend
npm install
npm run build      # output goes to frontend/dist, served by the Python backend
```
