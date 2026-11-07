"""Command line entry point.

    python -m goc_collector                       # start the dashboard (normal use)
    python -m goc_collector collect               # collect without the dashboard
    python -m goc_collector --file other.xlsx     # use a different Excel file
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "output" / "GOC_Register.xlsx"


def _setup_logging(file: Path) -> None:
    logdir = ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(logdir / "collector.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_collect(args) -> int:
    from .collector import DEFAULT_START, CollectionConfig, Collector
    from .excel_io import save_workbook
    from .server import db_path_for, open_store

    store, rep = open_store(args.file)
    if rep:
        print(f"Imported {rep['rows_read']:,} GOC numbers from {args.file.name}")
    print(f"Database: {db_path_for(args.file)} ({len(store):,} GOC numbers)")
    start = store.meta.get("start_goc") or args.start or DEFAULT_START
    cfg = CollectionConfig.from_dict({
        "start_goc": start, "batch_size": args.batch_size, "workers": args.workers,
        "min_interval": args.min_interval, "tail_gap": args.tail_gap,
        "lookahead": args.lookahead, "max_goc": args.max_goc})
    col = Collector(store, save=lambda: save_workbook(store, args.file),
                    lock_path=str(ROOT / "logs" / args.file.name), excel_path=args.file)
    signal.signal(signal.SIGINT, lambda *_: col.stop())
    try:
        col.start(cfg, mode="update" if len(store) else "new")
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2
    last = 0.0
    while col.busy:
        time.sleep(1)
        if time.monotonic() - last > 30:
            last = time.monotonic()
            p = col.progress
            print(f"[{p.state}] {p.activity} | checked {p.run_checked} found "
                  f"{p.run_found + p.run_partial} errors {p.run_errors}", flush=True)
    col.join()
    col.close()
    store.close()
    print(f"Finished: {col.progress.state}. Excel file: {args.file}")
    return 0 if col.progress.state == "completed" else 1


def _port_in_use(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(1)
        return sk.connect_ex((host, port)) == 0


def cmd_serve(args) -> int:
    url = f"http://{args.host}:{args.port}"
    if _port_in_use(args.host, args.port):
        print(f"\n  The collector is already open - showing it in your browser: {url}\n")
        if not args.no_browser:
            import webbrowser
            webbrowser.open(url)
        return 0
    import uvicorn

    from .server import create_app
    app = create_app(args.file)
    print("\n  GOC Register Data Collector is running.")
    print(f"  Open this address in your browser: {url}")
    print("  Keep this window open while collecting. Close it to quit.\n")
    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="goc_collector", description="GOC Register Data Collector")
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE,
                    help="Excel file that holds the collection (default output/GOC_Register.xlsx)")
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("collect", help="run a collection without the dashboard")
    c.add_argument("--start", default=None, help="first GOC number (new collections only)")
    c.add_argument("--batch-size", type=int, default=25)
    c.add_argument("--workers", type=int, default=1)
    c.add_argument("--min-interval", type=float, default=3.0)
    c.add_argument("--tail-gap", type=int, default=2000)
    c.add_argument("--lookahead", type=int, default=10000)
    c.add_argument("--max-goc", default=None)
    c.set_defaults(func=cmd_collect)

    v = sub.add_parser("serve", help="start the dashboard (default)")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--no-browser", action="store_true")
    v.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    if not args.cmd:
        args = ap.parse_args(["--file", str(args.file), "serve"])
    _setup_logging(args.file)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
