import React, { useCallback, useEffect, useRef, useState } from "react";

const api = async (path, opts = {}) => {
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(body?.detail || body || res.statusText);
  return body;
};
const post = (path, data) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });

const n = (v) => (v == null ? "0" : Number(v).toLocaleString());
const GOC_RE = /^\s*[A-Za-z0-9]+-\d+\s*$/;

function duration(sec) {
  if (!sec) return "0m";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}
const clock = (sec) => `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}`;

// "2026-11-07 15:06:38" -> "today at 15:06" / "6 Nov at 15:06"
function when(ts) {
  if (!ts) return "";
  const [d, t = ""] = ts.split(" ");
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const hm = t.slice(0, 5);
  if (d === today) return `today at ${hm}`;
  const date = new Date(`${d}T00:00:00`);
  return `${date.toLocaleDateString(undefined, { day: "numeric", month: "short" })} at ${hm}`;
}

const HEADLINE = {
  running: "Collecting",
  pausing: "Pausing…",
  paused: "Paused",
  stopping: "Stopping…",
  stopped: "Stopped",
  completed: "Finished",
  failed: "Stopped",
};

export default function App() {
  const [s, setS] = useState(null);
  const [offline, setOffline] = useState(false);
  const [lastPoll, setLastPoll] = useState(Date.now());
  const [now, setNow] = useState(Date.now());
  const [startGoc, setStartGoc] = useState("01-11200");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const fileRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      const d = await api("/api/status");
      setS(d);
      setOffline(false);
      setLastPoll(Date.now());
    } catch {
      setOffline(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 1000);
    const c = setInterval(() => setNow(Date.now()), 500);
    return () => {
      clearInterval(t);
      clearInterval(c);
    };
  }, [refresh]);

  const act = async (name, fn) => {
    setError("");
    setNotice("");
    setBusy(name);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  };

  if (!s) {
    return (
      <div className="page">
        <h1>GOC Register Data Collector</h1>
        <p className="muted">{offline ? "Cannot reach the collector. Please start it again (run_dashboard.bat)." : "Loading…"}</p>
      </div>
    );
  }

  const t = s.totals || {};
  const run = s.run || {};
  const state = s.state;
  const active = s.busy;
  const isRunning = active && (state === "running" || state === "stopping");
  const isPaused = active && (state === "paused" || state === "pausing");
  const found = (t.found || 0) + (t.partial || 0);
  const updatedAgo = Math.max(0, Math.round((now - lastPoll) / 1000));
  const waiting = isRunning && s.waiting_seconds > 3 ? s.waiting_seconds : 0;

  let headline = HEADLINE[state] || (s.has_data ? "Ready to continue" : "Ready to start");
  if (!active && state === "idle") headline = s.has_data ? "Ready to continue" : "Ready to start";
  if (state === "completed" && s.end_verified) headline = "Finished – all available records collected";

  const start = () =>
    act("start", async () => {
      if (!s.has_data && !GOC_RE.test(startGoc)) throw new Error("Please enter a GOC number like 01-11200.");
      await post("/api/start", s.has_data ? {} : { start_goc: startGoc.trim() });
    });

  const upload = (file) =>
    act("upload", async () => {
      const fd = new FormData();
      fd.append("file", file);
      const r = await api("/api/upload", { method: "POST", body: fd });
      const added = (r.merge?.inserted || 0) + (r.merge?.updated || 0);
      setNotice(
        `Loaded "${r.file}": ${n(r.rows_read)} GOC numbers (${r.first_goc} to ${r.last_goc}). ` +
          `${n(added)} added to the list. Press "Continue collecting" to collect the newer numbers.`
      );
    });

  const retryGivenUp = () =>
    act("retry", async () => {
      const r = await post("/api/retry-given-up");
      setNotice(`${n(r.count)} number(s) will be tried again. Press "Continue collecting".`);
    });

  // ---- "Your data" panel: database is always saved, Excel is a copy
  const ex = s.excel || {};
  let excelClass = "info";
  let excelIcon = "i";
  let excelLine;
  if (ex.problem) {
    excelClass = "warn";
    excelIcon = "!";
    excelLine = ex.problem;
  } else if (ex.updating) {
    excelClass = "busy";
    excelIcon = "↻";
    excelLine = "Updating the Excel file…";
  } else if (ex.up_to_date && ex.exists) {
    excelClass = "ok";
    excelIcon = "✓";
    excelLine = <>Up to date{ex.last_saved ? <> — last updated {when(ex.last_saved)}</> : null}.</>;
  } else if (active) {
    excelLine = (
      <>
        Updated when you pause or stop, and when the collection finishes
        {ex.last_saved ? <> (last updated {when(ex.last_saved)})</> : null}. <b>Download Excel</b> always gives
        you the latest data.
      </>
    );
  } else if (!s.has_data) {
    excelLine = "Created as soon as the first numbers are collected.";
  } else {
    excelClass = "busy";
    excelIcon = "↻";
    excelLine = "Will be updated in a moment.";
  }
  const retrying = Math.max(0, (t.error || 0) - (t.gave_up || 0));

  const startOver = () => {
    if (!window.confirm("Start a completely new list?\n\nThe current data is kept as a backup copy (database and Excel file) in the output folder, and the collection starts again from the first number.")) return;
    act("over", async () => {
      const r = await post("/api/start-over");
      setNotice(r.backup ? `The previous file was kept as "${r.backup}". You can now start a new collection.` : "You can now start a new collection.");
    });
  };

  return (
    <div className="page">
      <header>
        <h1>GOC Register Data Collector</h1>
        <p className="muted">Collects optometrist details from the official GOC register into Excel.</p>
      </header>

      {offline && (
        <div className="banner bad">
          Cannot reach the collector. Please check that the black “GOC Register Data Collector” window is still open
          (or double-click <b>run_dashboard.bat</b> again). Nothing collected so far is lost.
        </div>
      )}
      {s.running_elsewhere && <div className="banner info">The collection is already running in another window.</div>}
      {s.startup_error && <div className="banner bad">{s.startup_error}</div>}
      {s.startup_notice && <div className="banner good">{s.startup_notice}</div>}
      {error && <div className="banner bad">{error}</div>}
      {notice && <div className="banner good">{notice}</div>}

      {/* ---------------- live status ---------------- */}
      <section className={`status card state-${state}`}>
        <div className="status-top">
          <div className="headline">
            <span className={`dot ${isRunning ? "live" : ""}`} />
            {headline}
          </div>
          <div className="heartbeat" title="The page refreshes every second">
            {offline ? "Not connected" : `Live · updated ${updatedAgo}s ago`}
          </div>
        </div>

        {(active || state === "completed" || state === "stopped" || state === "failed") && s.activity && (
          <div className="activity">{s.activity}</div>
        )}
        {waiting > 0 && (
          <div className="waiting">
            The register website is busy, so the collector is giving it a short rest.
            It will continue automatically in <b>{clock(waiting)}</b>.
          </div>
        )}
        {waiting === 0 && isRunning && s.seconds_since_activity > 8 && (
          <div className="slow">
            Waiting for the register website to answer… ({Math.round(s.seconds_since_activity)}s). The website is
            sometimes slow; the collector keeps trying by itself.
          </div>
        )}
        {active && s.phase && s.phase !== "Collecting records" && <div className="phase">{s.phase}</div>}
        {isRunning && <div className="bar"><div /></div>}

        <div className="tiles">
          <div className="tile primary">
            <div className="label">Now at GOC number</div>
            <div className="value">{s.current_goc || s.next_goc || "—"}</div>
          </div>
          <div className="tile">
            <div className="label">Numbers checked</div>
            <div className="value">{n(t.processed)}</div>
          </div>
          <div className="tile good">
            <div className="label">Optometrists found</div>
            <div className="value">{n(found)}</div>
          </div>
          <div className="tile">
            <div className="label">Numbers with no record</div>
            <div className="value">{n(t.no_record)}</div>
          </div>
        </div>

        <div className="facts">
          {active && (
            <span>
              This session: <b>+{n(run.checked)}</b> checked, <b>+{n(run.found)}</b> found · running for{" "}
              <b>{duration(s.elapsed_seconds)}</b>
              {s.speed_per_minute ? <> · about <b>{s.speed_per_minute}</b> numbers per minute</> : null}
            </span>
          )}
          {t.latest_date && (
            <span>
              Newest registration date found so far: <b>{t.latest_date}</b>
            </span>
          )}
          {retrying > 0 && (
            <span className="warn-text">
              {n(retrying)} number(s) will be tried again automatically (the website did not answer).
            </span>
          )}
          {t.gave_up > 0 && (
            <span className="warn-text">
              {n(t.gave_up)} number(s) did not answer after {s.max_attempts} tries and are skipped for now
              (marked in red in the Excel file).
            </span>
          )}
        </div>
      </section>

      {/* ---------------- buttons ---------------- */}
      <section className="card controls">
        {!active && !s.has_data && (
          <label className="startfrom">
            Start from GOC number
            <input value={startGoc} onChange={(e) => setStartGoc(e.target.value)} />
          </label>
        )}
        <div className="buttons">
          {!active && (
            <button className="big go" onClick={start} disabled={busy === "start" || s.running_elsewhere}>
              ▶ {s.has_data ? "Continue collecting" : "Start collecting"}
            </button>
          )}
          {isRunning && state !== "stopping" && (
            <button className="big" onClick={() => act("pause", () => post("/api/pause"))}>
              ❚❚ Pause
            </button>
          )}
          {isPaused && (
            <button className="big go" onClick={() => act("resume", () => post("/api/resume"))} disabled={state === "pausing"}>
              ▶ Continue
            </button>
          )}
          {active && (
            <button className="big stop" onClick={() => act("stop", () => post("/api/stop"))} disabled={state === "stopping"}>
              ■ Stop
            </button>
          )}
          <a
            className={`big download ${s.has_data ? "" : "disabled"}`}
            href={s.has_data ? "/api/download" : undefined}
            title="Always contains the latest data, also while collecting"
          >
            ⬇ Download Excel
          </a>
        </div>
        <p className="hint">
          You can pause, stop or close this window at any time — nothing is lost. Next time, just press
          <b> Continue collecting</b> and it carries on from where it stopped.
        </p>
      </section>

      {/* ---------------- saving ---------------- */}
      <section className="card saved">
        <h2>Your data</h2>
        <div className="saverow ok">
          <span className="sicon">✓</span>
          <div>
            <b>Saved automatically.</b>{" "}
            {s.has_data
              ? "Every result is stored the moment it arrives, so nothing is lost if the computer switches off or this window is closed."
              : "Every result will be stored the moment it arrives, so nothing is lost if the computer switches off or this window is closed."}
            <div className="where">
              <span className="path">{s.file_folder}\{s.db_file}</span>
            </div>
          </div>
        </div>
        <div className={`saverow ${excelClass}`}>
          <span className="sicon">{excelIcon}</span>
          <div>
            <b>Excel file:</b> {excelLine}
            <div className="where">
              <span className="path">{s.file_folder}\{ex.file_name}</span>
            </div>
          </div>
        </div>
      </section>

      {/* ---------------- live feed ---------------- */}
      <section className="card">
        <h2>Latest numbers checked</h2>
        {(!s.recent || s.recent.length === 0) && (
          <p className="muted">Results appear here as soon as the collection starts.</p>
        )}
        <ul className="feed">
          {(s.recent || []).map((r) => (
            <li key={r.goc + r.time} className={r.status}>
              <span className="icon">
                {r.status === "found" ? "✓" : r.status === "partial" ? "✓" : r.status === "no_record" ? "–" : "!"}
              </span>
              <span className="goc">{r.goc}</span>
              <span className="text">{r.text}</span>
              <span className="time">{r.time}</span>
            </li>
          ))}
        </ul>
      </section>

      {/* ---------------- other options ---------------- */}
      <section className="card other">
        <h2>Other options</h2>
        <input ref={fileRef} type="file" accept=".xlsx,.xlsm" hidden onChange={(e) => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ""; }} />
        <p>
          <button className="link" onClick={() => fileRef.current?.click()} disabled={active || busy === "upload"}>
            {busy === "upload" ? "Reading the file…" : "Upload an Excel file from an earlier collection"}
          </button>{" "}
          <span className="muted">
            — only needed on a new computer when you don't have the <b>{s.db_file}</b> file. (With that file, just
            put it in the output folder.)
          </span>
        </p>
        {t.gave_up > 0 && (
          <p>
            <button className="link" onClick={retryGivenUp} disabled={active || busy === "retry"}>
              Try the {n(t.gave_up)} skipped number(s) again
            </button>{" "}
            <span className="muted">— they did not answer after {s.max_attempts} tries.</span>
          </p>
        )}
        <p>
          <button className="link danger" onClick={startOver} disabled={active || !s.has_data}>
            Start a completely new list
          </button>{" "}
          <span className="muted">— the current data is kept as a backup.</span>
        </p>
        {s.messages?.length > 0 && (
          <details>
            <summary>Show activity history</summary>
            <ul className="history">
              {s.messages.map((m, i) => (
                <li key={i} className={m.level}>
                  <time>{m.time.slice(11)}</time> {m.message}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
    </div>
  );
}
