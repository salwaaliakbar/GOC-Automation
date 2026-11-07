"""Access to the official GOC register (https://str.optical.org/).

The register's own search page sends a JSON POST to ``/registrant/list`` and
renders the returned HTML.  We send exactly the same request the public search
form sends (including its documented "separate multiple GOC numbers with a
comma" feature), then open each registrant's public detail page.

Politeness / compliance:
* a global rate limiter spaces out every request (shared across threads);
* HTTP 429 / 5xx responses trigger exponential back-off (honouring
  ``Retry-After``);
* no login, CAPTCHA or access control is bypassed - only public pages are read.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from . import NOT_AVAILABLE

log = logging.getLogger(__name__)

BASE_URL = "https://str.optical.org"
LIST_URL = BASE_URL + "/registrant/list"
PAGE_SIZE = 5  # results per page returned by the register
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GOC-Register-Data-Collector/1.0"
)

# Fallback copy of the filter definition embedded in the register home page.
# At runtime we read the live definition from the home page when possible.
_FALLBACK_FILTERS = [
    {"IndexFilterId": "Registrant", "Column": c, "Display": d, "AdditionalText": None,
     "AllowMultiple": "true" if c == "Pin" else None, "Type": t,
     "WildcardStart": ws, "WildcardEnd": ws, "SoundsLike": False,
     "SoundsLikeEnabled": c == "RegistrantSurname", "SoundsLikeDefault": False,
     "SelectItems": None, "Value": None}
    for c, d, t, ws in [
        ("Pin", "GOC number", "text", False),
        ("RegistrantSurname", "Surname contains", "text", True),
        ("RegistrantForename", "First name", "text", True),
        ("Register", "Registered as", "Select", False),
        ("Specialities", "Specialty", "Select", True),
        ("Address", "Address", "text", False),
        ("Geography", "Distance from postcode", "radius", False),
    ]
]


class RegisterError(Exception):
    """A temporary / technical failure talking to the register.

    Never interpreted as "no record exists".
    """


@dataclass
class ListHit:
    goc_number: str
    name: str
    detail_url: str
    registration_status: str = ""
    registered_as: str = ""


@dataclass
class Registrant:
    goc_number: str
    name: str
    town: str
    date_registered: str
    practice_address: str
    registration_status: str = ""
    registered_as: str = ""
    detail_url: str = ""
    missing: list[str] = field(default_factory=list)


class RateLimiter:
    """Ensures a minimum interval between requests across all threads.

    Adaptive, but proportionate: an isolated failure (one slow or broken page)
    does not slow the whole collection down. Only *consecutive* failures, which
    suggest the register itself is struggling, raise the interval. Successes
    bring it back quickly. After ``breaker_failures`` consecutive failures all
    requests pause for ``breaker_cooldown`` seconds (circuit breaker).

    Two further rules keep the load smooth:
    * ``min_gap``: the next request never starts sooner than ``min_gap`` seconds
      after the previous answer arrived, even when that answer was slow. (The
      interval alone is counted from the *start* of a request, so a 17 s answer
      would otherwise be followed by the next request immediately.)
    * slowness is judged on the *median* of recent answers, not on one page:
      a single heavy registrant page does not slow everything down, but a
      register that is slow overall makes the collector go more gently.
    """

    def __init__(self, min_interval: float, max_interval: float = 15.0,
                 breaker_failures: int = 4, breaker_cooldown: float = 120.0,
                 min_gap: float = 1.0, slow_median: float = 5.0):
        self.base_interval = min_interval
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.min_gap = min_gap
        self.slow_median = slow_median
        self._lock = threading.Lock()
        self._next = 0.0
        self._penalty_until = 0.0
        self._ok_streak = 0
        self._fail_streak = 0
        self._latencies: deque[float] = deque(maxlen=10)
        self.breaker_failures = breaker_failures
        self.breaker_cooldown = breaker_cooldown
        self.cooldowns = 0
        self.cancel = threading.Event()   # set by the client to abort waits

    def record_success(self) -> None:
        with self._lock:
            self._fail_streak = 0
            self._ok_streak += 1
            if self._ok_streak >= 5 and self.min_interval > self.base_interval:
                self.min_interval = max(self.base_interval, self.min_interval * 0.7)
                self._ok_streak = 0

    def record_failure(self) -> None:
        with self._lock:
            self._ok_streak = 0
            self._fail_streak += 1
            if self._fail_streak >= 2:
                self.min_interval = min(self.max_interval, self.min_interval * 2)
            if self._fail_streak >= self.breaker_failures:
                # circuit breaker: give the register time to recover
                self._fail_streak = 0
                self.cooldowns += 1
                self._penalty_until = max(self._penalty_until,
                                          time.monotonic() + self.breaker_cooldown)
                log.warning("Register struggling - pausing all requests for %.0f s",
                            self.breaker_cooldown)

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next, self._penalty_until)
            self._next = start + self.min_interval
        delay = start - time.monotonic()
        if delay > 0:
            self.cancel.wait(delay)

    def request_done(self, elapsed: float | None = None) -> bool:
        """Called after every answer (or failure). Enforces ``min_gap`` and, when
        ``elapsed`` is given, tracks response times. Returns True when the
        register is slow overall (median of recent answers above ``slow_median``),
        in which case the interval is widened."""
        with self._lock:
            now = time.monotonic()
            self._next = max(self._next, now + self.min_gap)
            if elapsed is None:
                return False
            self._latencies.append(elapsed)
            if len(self._latencies) < 5:
                return False
            median = sorted(self._latencies)[len(self._latencies) // 2]
            if median > self.slow_median:
                self.min_interval = min(self.max_interval, self.min_interval * 1.5)
                self._ok_streak = 0
                return True
            return False

    def rest_remaining(self) -> float:
        """Seconds left of a deliberate rest (circuit breaker / HTTP 429 / 503).
        Normal spacing between requests is not counted."""
        with self._lock:
            return max(0.0, self._penalty_until - time.monotonic())

    def pause_remaining(self) -> float:
        """Seconds until the next request may start (normal spacing included)."""
        with self._lock:
            return max(0.0, max(self._next, self._penalty_until) - time.monotonic())

    def penalise(self, seconds: float) -> None:
        """Pause all threads (e.g. after HTTP 429)."""
        with self._lock:
            self._penalty_until = max(self._penalty_until, time.monotonic() + seconds)


class RegisterClient:
    def __init__(self, min_interval: float = 3.0, timeout: float = 20.0,
                 max_retries: int = 3, backoff_base: float = 5.0,
                 total_timeout: float = 40.0, slow_threshold: float = 8.0,
                 detail_retries: int = 2):
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.slow_threshold = slow_threshold
        self.max_retries = max_retries
        # A registrant page that keeps failing is usually that one heavy page, not
        # the whole register. Asking for it again and again only adds load, so it
        # gets fewer attempts; the number is retried later in the retry round.
        self.detail_retries = min(detail_retries, max_retries)
        self.backoff_base = backoff_base
        self.limiter = RateLimiter(min_interval)
        self._local = threading.local()
        self._filters_lock = threading.Lock()
        self._filters: list[dict] | None = None
        self.request_count = 0
        self.failure_count = 0
        self._latency_sum = 0.0
        self._latency_n = 0
        self._stats_lock = threading.Lock()
        self.backoff_until = 0.0
        self.cancelled = self.limiter.cancel

    def cancel(self) -> None:
        """Abort waits/retries promptly (used by Stop). In-flight requests end
        with RegisterError('cancelled'), which is recorded as a retryable error."""
        self.cancelled.set()

    # ------------------------------------------------------------------ http
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            # The register is stateless for searches. Refusing cookies avoids the
            # Azure "ARRAffinity" cookie pinning us to one (possibly overloaded)
            # backend instance, so the load balancer can route each request.
            s.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            self._local.session = s
        return s

    def _request(self, method: str, url: str, attempts: int | None = None,
                 **kw) -> requests.Response:
        attempts = attempts or self.max_retries
        last_exc: Exception | None = None
        counted = False      # count this request towards the failure streak only once

        def failed() -> None:
            nonlocal counted
            self.failure_count += 1
            if not counted:
                # Retries of the *same* page do not count again: the circuit
                # breaker should react to several different requests failing in
                # a row (the register struggling), not to one broken page.
                counted = True
                self.limiter.record_failure()

        for attempt in range(1, attempts + 1):
            if self.cancelled.is_set():
                raise RegisterError("cancelled by user")
            self.limiter.wait()
            if self.cancelled.is_set():
                raise RegisterError("cancelled by user")
            self.request_count += 1
            t0 = time.monotonic()
            try:
                r = self._timed_request(method, url, t0, **kw)
            except requests.RequestException as exc:
                self.limiter.request_done()
                last_exc = exc
                log.warning("%s %s failed (attempt %d): %s", method, url, attempt, exc)
                failed()
                # a fresh session after connection problems
                self._local.session = None
            else:
                elapsed = time.monotonic() - t0
                self._note_latency(elapsed)
                if self.limiter.request_done(elapsed):
                    log.warning("register is slow overall - interval now %.1f s",
                                self.limiter.min_interval)
                if elapsed > self.slow_threshold:
                    # one slow page is logged but does not slow everything down;
                    # a new connection avoids staying on a struggling server
                    log.info("slow response: %s %s took %.1f s", method, url, elapsed)
                    self._local.session = None
                if r.status_code == 200:
                    if elapsed <= self.slow_threshold:
                        self.limiter.record_success()
                    return r
                last_exc = RegisterError(f"HTTP {r.status_code} from {url}")
                log.warning("%s %s -> HTTP %s (attempt %d)", method, url, r.status_code, attempt)
                if r.status_code >= 500 or r.status_code == 429:
                    failed()
                if r.status_code in (429, 503):
                    ra = r.headers.get("Retry-After", "")
                    if ra.isdigit() or r.status_code == 429:
                        # the register asks us to wait: everything waits
                        self.limiter.penalise(min(float(ra), 600.0) if ra.isdigit() else 60.0)
                elif 400 <= r.status_code < 500 and r.status_code not in (408,):
                    # client errors other than timeout are not worth retrying
                    raise RegisterError(f"HTTP {r.status_code} from {url}")
            if attempt < attempts:
                delay = self.backoff_base * (2 ** (attempt - 1))
                self.backoff_until = time.monotonic() + delay
                try:
                    if self.cancelled.wait(delay):
                        raise RegisterError("cancelled by user")
                finally:
                    self.backoff_until = 0.0
        raise RegisterError(str(last_exc))

    def waiting_seconds(self) -> float:
        """How long the client is deliberately resting (circuit breaker, a wait the
        register asked for, or the pause before retrying a page). The normal few
        seconds between requests are not included."""
        return max(self.limiter.rest_remaining(), self.backoff_until - time.monotonic(), 0.0)

    def _timed_request(self, method: str, url: str, t0: float, **kw) -> requests.Response:
        """Request with a hard overall deadline.

        ``requests`` timeouts only limit each individual socket wait, so a server
        that trickles bytes can hold a request open for minutes. The body is read
        in chunks and abandoned once ``total_timeout`` is exceeded.
        """
        r = self._session().request(method, url, timeout=(10, self.timeout), stream=True, **kw)
        chunks = []
        try:
            for chunk in r.iter_content(16384):
                chunks.append(chunk)
                if time.monotonic() - t0 > self.total_timeout:
                    raise requests.Timeout(f"total deadline {self.total_timeout:.0f}s exceeded")
        finally:
            r.close()
        r._content = b"".join(chunks)
        return r

    def _note_latency(self, elapsed: float) -> None:
        with self._stats_lock:
            self._latency_sum += elapsed
            self._latency_n += 1
            if self._latency_n >= 100:
                log.info("throughput: %d requests, avg response %.2f s, interval %.1f s, "
                         "failures so far %d, breaker pauses %d", self.request_count,
                         self._latency_sum / self._latency_n, self.limiter.min_interval,
                         self.failure_count, self.limiter.cooldowns)
                self._latency_sum, self._latency_n = 0.0, 0

    # -------------------------------------------------------------- filters
    def _filter_template(self) -> list[dict]:
        with self._filters_lock:
            if self._filters is None:
                try:
                    r = self._request("GET", BASE_URL + "/")
                    m = re.search(r"var filterData = (\[.*?\]);", r.text)
                    allf = json.loads(m.group(1)) if m else []
                    regs = [f for f in allf if f.get("IndexFilterId") == "Registrant"]
                    if not any(f.get("Column") == "Pin" for f in regs):
                        raise ValueError("Pin filter missing")
                    self._filters = regs
                except Exception as exc:  # noqa: BLE001
                    log.warning("Using built-in filter template (%s)", exc)
                    self._filters = _FALLBACK_FILTERS
            return json.loads(json.dumps(self._filters))

    # --------------------------------------------------------------- search
    def _search_page(self, pins: list[str], page: int) -> tuple[int, list[ListHit]]:
        filters = self._filter_template()
        for f in filters:
            f["Value"] = ",".join(pins) if f["Column"] == "Pin" else None
        payload = {"filters": filters, "sorting": "Registrant Surname (a - z)",
                   "bounds": None, "indexFilterId": "Registrant", "page": page}
        r = self._request("POST", LIST_URL, json=payload)
        return parse_list_html(r.text)

    def search_pins(self, pins: list[str]) -> dict[str, ListHit]:
        """Search a batch of GOC numbers. Returns ``{goc_number: hit}``.

        Every page of results is fetched and the number of distinct hits is
        checked against the total the register reports; any inconsistency
        raises :class:`RegisterError` so the batch is retried / split instead
        of silently recording a number as "no record".
        """
        wanted = set(pins)
        total, hits = self._search_page(pins, 0)
        found: dict[str, ListHit] = {}
        pages = max(1, math.ceil(total / PAGE_SIZE))
        for p in range(pages):
            if p > 0:
                t2, hits = self._search_page(pins, p)
                if t2 != total:
                    raise RegisterError("result count changed between pages")
            for h in hits:
                if h.goc_number not in wanted:
                    raise RegisterError(f"unexpected GOC number {h.goc_number} in results")
                found[h.goc_number] = h
        if len(found) != total:
            raise RegisterError(
                f"pagination inconsistent: register reported {total}, got {len(found)}")
        return found

    def count_pins(self, pins: list[str]) -> int:
        """Number of registrants among ``pins`` (single request, used for look-ahead)."""
        total, _ = self._search_page(pins, 0)
        return total

    # --------------------------------------------------------------- detail
    def fetch_detail(self, hit: ListHit) -> Registrant:
        url = hit.detail_url if hit.detail_url.startswith("http") else BASE_URL + hit.detail_url
        r = self._request("GET", url, attempts=self.detail_retries)
        reg = parse_detail_html(r.text, url)
        if reg.goc_number != hit.goc_number:
            raise RegisterError(
                f"detail page GOC number {reg.goc_number!r} != searched {hit.goc_number!r}")
        if not reg.registered_as:
            reg.registered_as = hit.registered_as
        if not reg.registration_status:
            reg.registration_status = hit.registration_status
        return reg


# ------------------------------------------------------------------ parsing
_COUNT_RE = re.compile(r"\((\d+)\s+Results?\)")
_TITLE_RE = re.compile(r"^(.*)\(([^()]+)\)\s*$", re.S)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_list_html(html: str) -> tuple[int, list[ListHit]]:
    if 'id="searchResultCount"' not in html:
        raise RegisterError("unexpected search response (no result count)")
    m = _COUNT_RE.search(html)
    if not m:
        raise RegisterError("could not read result count")
    total = int(m.group(1))
    soup = BeautifulSoup(html, "lxml")
    hits: list[ListHit] = []
    for li in soup.select("li.media"):
        title = li.select_one("strong.title-font")
        link = li.select_one('a[href*="/Registrant/"]')
        if not title or not link:
            raise RegisterError("malformed search result item")
        tm = _TITLE_RE.match(_clean(title.get_text()))
        if not tm:
            raise RegisterError(f"cannot parse result title {title.get_text()!r}")
        fields = {}
        for b in li.select(".media-body > b"):
            label = _clean(b.get_text()).rstrip(":")
            nxt = b.next_sibling
            if label and nxt is not None:
                val = nxt.get_text() if hasattr(nxt, "get_text") else str(nxt)
                fields[label] = _clean(val)
        hits.append(ListHit(goc_number=_clean(tm.group(2)), name=_clean(tm.group(1)),
                            detail_url=link["href"],
                            registration_status=fields.get("Registration Status", ""),
                            registered_as=fields.get("Registered as", "")))
    if total and not hits:
        raise RegisterError("result count > 0 but no result items parsed")
    return total, hits


def parse_detail_html(html: str, url: str = "") -> Registrant:
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("#detail-title")
    if title is None:
        raise RegisterError("registrant detail page not recognised")
    strongs = [_clean(s.get_text()) for s in title.select("strong")]
    fields: dict[str, list[str]] = {}
    current = None
    for body in soup.select("div.media-body"):
        lab = body.find("label")
        label = _clean(lab.get_text()) if lab else ""
        values = [_clean(s.get_text(" ")) for s in body.select("span.d-block")]
        values = [v for v in values if v]
        if label:
            current = label
            fields.setdefault(label, []).extend(values)
        elif current == "Practice addresses":
            # additional practice addresses appear as unlabelled blocks
            fields[current].extend(values)

    def first(key: str) -> str:
        v = fields.get(key) or []
        return v[0] if v else ""

    goc = first("GOC Number") or (strongs[1] if len(strongs) > 1 else "")
    name = strongs[0] if strongs else ""
    addresses = [a for a in fields.get("Practice addresses", []) if a.lower() != "none"]
    town = first("Town")
    date = first("Date of most recent registration")

    reg = Registrant(
        goc_number=goc,
        name=name,
        town=town if town.lower() != "none" else "",
        date_registered=date,
        practice_address="; ".join(addresses),
        registration_status=first("Registration Status"),
        registered_as=first("Registered as"),
        detail_url=url,
    )
    for attr, label in (("name", "Name"), ("town", "Town"),
                        ("date_registered", "Date Registered"),
                        ("practice_address", "Practice Address")):
        if not getattr(reg, attr):
            reg.missing.append(label)
            setattr(reg, attr, NOT_AVAILABLE)
    return reg
