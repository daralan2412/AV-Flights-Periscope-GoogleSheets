#!/usr/bin/env python3
"""
scrape_and_upload.py - AV-Flights-Periscope-GoogleSheets

NEW pipeline, independent of daralan2412/FX-periscope-googlesheets (FedEx
REP-1901), daralan2412/CM-Master-Periscope-Googlesheets (Copa) and
daralan2412/periscope-to-sheets (PTY wheelchair). Those are left untouched;
this repo shares no secrets, sheet or Apps Script project with them.

Source: Periscope/Sisense shared report "Flight Lookup"
        https://app.periscopedata.com/shared/3aada203-6788-454f-bca4-760507f50817
        Report-level filters: Airport, Mainline, Regional, EquipmentType,
        Flight_Number, Date Range. The scraper sets Mainline = "Avianca"
        (ALL stations - per instruction "all stations but Avianca airline")
        and a Custom Range date window; every other filter is left blank.
Target: Google Sheet "AV - Flights - BOG"
        https://docs.google.com/spreadsheets/d/1VOw-Ahx7F7INfRzzu-UFtfy3GLrW0sAihMTSL6i7YTU
        data tab "AV - Flights - BOG" (gid 1896233863; the "SOURCE" tab,
        gid 981954045, only holds the report URL). 23 columns, see HEADERS.
        The scraper never touches the sheet itself - it POSTs rows to the
        Apps Script Web App (SHEETS_WEBAPP_URL / WEBAPP_TOKEN secrets) which
        UPSERTS them by segment_id (freshest scrape wins).

Flow (runs 4x a day: 00:07, 06:07, 12:07, 18:07 America/Bogota - see run.yml):
  1. Open the report, tick Mainline = Avianca, set Date Range to a rolling
     "D-2 to D0" window via Custom Range (plus one older 3-day backfill
     chunk per run, see BACKFILL_*), then use the Flight Details widget's own
     "Download Data" CSV export (NOT DOM scraping - the grid is virtualized,
     only the rows/columns near the viewport exist in the DOM; the CSV is
     generated server-side and is complete).
  2. POST {"rows": [[...23 cols...], ...]} to the Web App. Rows are upserted
     by segment_id, so re-pulling the same days four times a day never grows
     the sheet with duplicates - "N updated in place" is the steady state.

Volumes observed live 2026-09-20: Mainline=Avianca, all stations, 09/18-09/20
= 9,542 CSV rows (the report repeats a segment_id many times - the same
flight with slightly different equip_type etc.; the sheet keeps ONE row per
segment_id, the last one in the export). The all-station query is heavy:
~60-90 s on Sisense before the grid renders, hence the long waits below.

The browser-driving code is the hardened v4.1 logic from the FedEx pipeline
(2026-09-02), reused because this report is the same Sisense template:
  - do NOT wait for the grid / "networkidle" before applying our filters (the
    default "All Dates" query is slow and we replace it anyway);
  - "Custom Range" lives in .custom-date-option, NOT inside .radio-button-group;
  - type into inputs with press_sequentially (the datepicker AND the filter
    search boxes ignore .fill() / synthetic input events - confirmed live
    2026-09-20: setting .value + dispatching "input" left the Airport list
    unfiltered, real keystrokes filtered it);
    no Escape (it can clear the field), no Tab; click-into-next-field commits;
  - wait for .apply-button to lose "disabled" before clicking it;
  - the real breadcrumbs are ".filters-bar .filter-group" (".filters-bar-label"
    only ever reads "Filters (N)"); after Apply they read
    "Mainline : Avianca" and "DateRange : 2026-09-18 to 2026-09-20";
  - ".error-message" is ALWAYS in the DOM (display:none) - check visibility,
    never existence, or every run silently reports "no rows";
  - whole-scrape retry with a fresh browser (SCRAPE_ATTEMPTS).
"""

import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

PERISCOPE_URL = "https://app.periscopedata.com/shared/3aada203-6788-454f-bca4-760507f50817"
LOCAL_TZ = ZoneInfo("America/Bogota")  # BOG station time; UTC-5 all year (no DST).
MAINLINE_FILTER = "Avianca"  # exact label in the report's Mainline filter list

# PRIMARY window = today back through LOOKBACK_DAYS days ago, inclusive.
# D-2..D0 (same choice as the Copa pipeline): flight rows keep changing for
# hours after the day ends (actual arrival/departure times, gates, delays get
# filled in), and the Web App upserts, so re-posting known segment_ids just
# refreshes them. Every day is re-synced ~12 times after it ends.
LOOKBACK_DAYS = 2

# BACKFILL window - one extra, older 3-day chunk per run, rotating by slot
# (00:07 -> D-5..D-3, 06:07 -> D-8..D-6, 12:07 -> D-11..D-9, 18:07 ->
# D-14..D-12), so late corrections in the source still reach the sheet for
# two weeks. Chunks are 3 days because this report's all-station query is
# heavy (~1 min for 3 days on 2026-09-20). Rows are upserted, so this only
# ever adds/refreshes. Manual override for a one-off catch-up
# (workflow_dispatch inputs or env): BACKFILL_START="09/01/2026"
# BACKFILL_END="09/12/2026" replaces the rotating chunk with that exact range
# (scraped in BACKFILL_CHUNK_DAYS pieces).
BACKFILL_CHUNK_DAYS = 3
BACKFILL_SLOTS = 4  # = number of runs per day

SCRAPE_ATTEMPTS = 3  # whole-scrape retries with a fresh browser (see main()).
SCRAPE_RETRY_DELAY_S = 60
WEBAPP_URL = os.environ["SHEETS_WEBAPP_URL"]
WEBAPP_TOKEN = os.environ["WEBAPP_TOKEN"]

# Column ORDER of the report's "Flight Details" widget / CSV export (confirmed
# live 2026-09-20 against the grid header: segment id, station, mainline,
# regional, tail, equip type, arrival flight, arrival gate, arrival time,
# arrival scheduled time, arrival delay, upline station, upline scheduled
# time, upline departure time, departure flight, departure gate, departure
# time, departure scheduled time, departure delay, downline station, downline
# scheduled time, downline arrival time, ground time). The NAMES below are the
# sheet's existing header row; the CSV's own header spelling may differ -
# only the order and count matter.
HEADERS = [
    "segment_id", "station", "mainline", "regional", "tail", "equip_type",
    "arrival_flight", "arrival_gate", "arrival_time", "arrival_scheduled_time", "arrival_delay",
    "upline_station", "upline_scheduled_time", "upline_departure_time",
    "departure_flight", "departure_gate", "departure_time", "departure_scheduled_time", "departure_delay",
    "downline_station", "downline_scheduled_time", "downline_arrival_time", "ground_time",
]

# The sheet's existing rows (May-Aug 2026, hand-exported) store timestamps as
# "5/12/2026 15:56" (M/D/YYYY H:MM, no seconds). The Sisense CSV/grid gives
# "2026-05-12 15:56:00". Normalise to the sheet's convention so the tab stays
# uniform for anything (dashboard, formulas) that parses these columns.
DATETIME_COLS = [
    "arrival_time", "arrival_scheduled_time", "upline_scheduled_time", "upline_departure_time",
    "departure_time", "departure_scheduled_time", "downline_scheduled_time", "downline_arrival_time",
]
_ISO_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?$")


def normalize_datetime(v: str) -> str:
    m = _ISO_DT.match(v.strip()) if v else None
    if not m:
        return v
    y, mo, d, h, mi = m.groups()
    return f"{int(mo)}/{int(d)}/{y} {int(h)}:{mi}"


WEBAPP_ATTEMPTS = 4
WEBAPP_RETRY_DELAY_S = 45


class PermanentWebAppError(RuntimeError):
    """The Web App answered in a way a retry cannot fix (bad token, doPost error)."""


def webapp_request(method: str, timeout: int, **kwargs) -> dict:
    """GET/POST the Apps Script Web App and return its JSON, retrying transients.

    Apps Script answers the /exec URL with a 302 to a one-time
    googleusercontent "echo" URL, and Google intermittently 404s that redirect
    (seen on the Copa pipeline: 3 of 4 scheduled runs on 2026-09-18/19).
    Both calls are safe to repeat: the GET is read-only and the POST is an
    upsert by segment_id, so a retry after an ambiguous failure can at worst
    rewrite identical rows.

    Retryable: connection errors, timeouts, HTTP 5xx / 429 / 408 / 404, a
    non-JSON body, and the Web App's own "another upload is in progress"
    lock timeout. Not retryable (PermanentWebAppError): 401/403 and any other
    4xx, and an explicit success:false from doGet/doPost.
    """
    last_err = None
    for attempt in range(1, WEBAPP_ATTEMPTS + 1):
        try:
            resp = requests.request(
                method, WEBAPP_URL, params={"token": WEBAPP_TOKEN}, timeout=timeout, **kwargs
            )
            if resp.status_code >= 500 or resp.status_code in (404, 408, 429):
                raise RuntimeError(
                    f"HTTP {resp.status_code} from Web App ({resp.url[:60]}...): {resp.text[:160]!r}"
                )
            if resp.status_code >= 400:
                raise PermanentWebAppError(
                    f"Web App {method} rejected with HTTP {resp.status_code}: {resp.text[:160]!r}"
                )
            try:
                data = resp.json()
            except ValueError:
                raise RuntimeError(f"non-JSON reply from Web App (HTTP {resp.status_code}): {resp.text[:160]!r}")
            if not data.get("success"):
                err = str(data.get("error", ""))
                if "lock timeout" in err or "in progress" in err:
                    raise RuntimeError(f"Web App busy: {err}")
                raise PermanentWebAppError(f"Web App {method} failed: {data}")
            return data
        except PermanentWebAppError:
            raise
        except Exception as exc:  # noqa: BLE001 - the transient classes listed above
            last_err = exc
            print(f"Web App {method} attempt {attempt}/{WEBAPP_ATTEMPTS} failed: {exc}", file=sys.stderr)
            if attempt < WEBAPP_ATTEMPTS:
                print(f"Retrying in {WEBAPP_RETRY_DELAY_S}s...", file=sys.stderr)
                time.sleep(WEBAPP_RETRY_DELAY_S)
    raise RuntimeError(f"Web App {method} failed after {WEBAPP_ATTEMPTS} attempts: {last_err}")


def check_token():
    """Cheap pre-flight auth/connectivity check before paying for a scrape."""
    webapp_request("GET", timeout=30)


def _fmt(d):
    return d.strftime("%m/%d/%Y")


def compute_windows():
    """All (label, start, end) windows this run must pull, primary first.

    - primary: D-2..D0 (see LOOKBACK_DAYS), America/Bogota, computed at call
      time so a late-firing cron still pulls the right days.
    - backfill: BACKFILL_START/BACKFILL_END if set (manual catch-up, cut into
      BACKFILL_CHUNK_DAYS pieces), else the rotating chunk for this run's
      slot (slot = Bogota hour // 6).
    """
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    windows = [("primary D-%d..D0" % LOOKBACK_DAYS, _fmt(today - timedelta(days=LOOKBACK_DAYS)), _fmt(today))]

    bf_start, bf_end = os.environ.get("BACKFILL_START", "").strip(), os.environ.get("BACKFILL_END", "").strip()
    if bf_start and bf_end:
        a = datetime.strptime(bf_start, "%m/%d/%Y").date()
        b = datetime.strptime(bf_end, "%m/%d/%Y").date()
        while a <= b:
            c = min(a + timedelta(days=BACKFILL_CHUNK_DAYS - 1), b)
            windows.append(("manual backfill", _fmt(a), _fmt(c)))
            a = c + timedelta(days=1)
        return windows

    if os.environ.get("SKIP_BACKFILL", "").strip().lower() in ("1", "true", "yes"):
        return windows

    slot = (now.hour // (24 // BACKFILL_SLOTS)) % BACKFILL_SLOTS
    end = today - timedelta(days=LOOKBACK_DAYS + 1 + slot * BACKFILL_CHUNK_DAYS)
    start = end - timedelta(days=BACKFILL_CHUNK_DAYS - 1)
    age_hi = (today - start).days
    age_lo = (today - end).days
    windows.append(("backfill slot %d D-%d..D-%d" % (slot, age_hi, age_lo), _fmt(start), _fmt(end)))
    return windows


def _filter_column(page, label):
    """Locator for one report-level filter column (.setting.dimension-setting)
    identified by its header label ("Mainline", "Airport", "Date Range"...)."""
    return page.locator(".setting.dimension-setting", has=page.locator(".setting-header .label", has_text=label)).first


def scrape_window_csv(start_str, end_str):
    """Filter the report to Mainline=Avianca + a Custom Range date window and
    pull the Flight Details widget's CSV export.

    Returns the CSV text, or None if the widget shows "Query returned no
    matching rows" - Sisense doesn't even offer a "Download Data" menu item
    when there's nothing to export, so this has to be checked for explicitly
    rather than treated as a scrape failure.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        # Capture the export URL from the first matching response, then poll
        # it ourselves. Registered before any interaction so we can't race it.
        export_url = {"value": None}

        def on_response(resp):
            if export_url["value"] is None and "/download_csv/" in resp.url:
                export_url["value"] = resp.url

        page.on("response", on_response)

        try:
            # "domcontentloaded" rather than "networkidle": the report's
            # default Date Range is "All Dates" and it queries every airline
            # on load - we replace that query, so never wait on it.
            page.goto(PERISCOPE_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_selector(".filters-bar-label", timeout=90_000)

            # Open the report-level filters panel.
            page.locator(".filters-bar-label").first.click()
            page.wait_for_selector(".radio-button-group", timeout=30_000)
            page.wait_for_timeout(300)

            # --- Mainline = Avianca -------------------------------------
            # The Mainline column is a searchable checkbox list of ~100
            # airline names ("Air Central", "9 Air", ...). Typing in its
            # search box narrows the list (real keystrokes only - a synthetic
            # value/input event leaves the list unfiltered, confirmed live
            # 2026-09-20), then the row labelled exactly "Avianca" is ticked
            # (not "Avianca Brasil" / "Avianca Cargo", which the search also
            # shows). Each row is <div class="checkbox small-checkbox">
            # <div class="icon"/><div class="label">Avianca</div></div>.
            mainline_col = _filter_column(page, "Mainline")
            search = mainline_col.locator("input.search-input").first
            search.click(force=True)
            search.press_sequentially(MAINLINE_FILTER, delay=40)
            option = mainline_col.locator(".checkbox-list .checkbox", has=page.locator(".label", has_text=re.compile(rf"^\s*{re.escape(MAINLINE_FILTER)}\s*$"))).first
            option.wait_for(state="visible", timeout=30_000)
            option.locator(".icon").click(force=True)
            page.wait_for_timeout(500)

            # --- Date Range = Custom Range start..end ------------------
            # "Custom Range" is rendered in its own sibling container
            # (.custom-date-option), not inside .radio-button-group.
            custom_range_option = page.locator(".custom-date-option .small-radio-button").first
            custom_range_option.click(force=True)
            page.wait_for_timeout(800)

            # jQuery-UI-style datepicker (hasDatepicker): only real
            # keystrokes register; .fill() leaves the value visible but the
            # filter state unset. No Escape (clears the field), no Tab:
            # clicking into the next field / Apply is the blur that commits.
            start_input = page.locator(".range-start")
            end_input = page.locator(".range-end")
            start_input.click(force=True)
            start_input.clear()
            start_input.press_sequentially(start_str, delay=40)
            end_input.click(force=True)
            end_input.clear()
            end_input.press_sequentially(end_str, delay=40)

            # Apply enables only once the widget validated both dates.
            page.wait_for_function(
                """() => {
                    const btn = document.querySelector('.apply-button');
                    return btn && !btn.classList.contains('disabled');
                }""",
                timeout=15_000,
            )

            apply_button = page.locator(".apply-button")
            apply_button.click(force=True)

            # Poll the real per-filter breadcrumbs (".filters-bar
            # .filter-group"), which after Apply read e.g.
            # "Mainline : Avianca" and "DateRange : 2026-09-18 to 2026-09-20".
            def crumbs_text():
                return " | ".join(t.strip() for t in page.locator(".filters-bar .filter-group").all_text_contents())

            try:
                page.wait_for_function(
                    """(mainline) => {
                        const groups = Array.from(document.querySelectorAll('.filters-bar .filter-group'))
                            .map(g => g.textContent.replace(/\\s+/g, ' ').trim());
                        const hasDate = groups.some(t => t.includes('DateRange') && t.includes(' to '));
                        const hasMainline = groups.some(t => t.includes('Mainline') && t.includes(mainline));
                        return hasDate && hasMainline;
                    }""",
                    arg=MAINLINE_FILTER,
                    timeout=15_000,
                )
            except Exception:
                raise RuntimeError(
                    "Filters did not commit - breadcrumbs never showed both "
                    f"'Mainline : {MAINLINE_FILTER}' and 'DateRange : <d> to <d>' after Apply "
                    f"(last breadcrumbs: {crumbs_text()!r})"
                )

            # --- The Flight Details grid widget ------------------------
            # The report has three .widget-container nodes: a "Date Range:
            # ..." text widget, the Flight Details grid (id class
            # widget-3342615 on 2026-09-20) and a second widget with the SAME
            # title that only shows a SQL-query placeholder. Prefer the id
            # class; fall back to the first titled one that owns a grid.
            widget = page.locator(".widget-container.widget-3342615")
            if widget.count() == 0:
                widget = page.locator(
                    ".widget-container",
                    has=page.locator(".widget-title", has_text="Flight Details"),
                ).first
            widget.scroll_into_view_if_needed()

            # Wait for the query Apply triggered to finish: the widget shows a
            # ".widget-loader" overlay on top of its PREVIOUS results while
            # requerying. ".error-message" is always in the DOM (hidden), so
            # check offsetParent, never existence.
            widget_handle = widget.element_handle()
            page.wait_for_function(
                """(el) => {
                    const loader = el.querySelector('.widget-loader');
                    if (loader && loader.offsetParent !== null) return false;
                    const err = el.querySelector('.error-message');
                    const errVisible = !!err && err.offsetParent !== null;
                    const grid = el.querySelector('.ninja-grid');
                    return errVisible || !!grid;
                }""",
                arg=widget_handle,
                # The all-station Avianca query took ~60-90 s live on
                # 2026-09-20 and Sisense is slower at scheduled hours.
                timeout=300_000,
            )

            if widget.locator(".error-message", has_text="no matching rows").is_visible():
                browser.close()
                return None

            # Open the per-widget menu (hamburger, top-right) -> Download Data.
            widget.hover()
            page.wait_for_timeout(500)
            widget.locator(".controls .expand.button").click(force=True)
            page.wait_for_selector("text=Download Data", timeout=30_000)
            page.get_by_text("Download Data", exact=True).click()

            deadline = time.time() + 60
            while export_url["value"] is None and time.time() < deadline:
                page.wait_for_timeout(250)
            if export_url["value"] is None:
                raise RuntimeError("Did not observe a download_csv request after clicking Download Data")
        except Exception:
            try:
                page.screenshot(path="debug_failure.png", full_page=True)
                with open("debug_failure.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
            except Exception as diag_err:
                print(f"(could not capture debug artifacts: {diag_err})", file=sys.stderr)
            browser.close()
            raise

        # Sisense generates the CSV server-side; the URL is non-200 until ready.
        csv_text = None
        deadline = time.time() + 300
        while time.time() < deadline:
            resp = page.context.request.get(export_url["value"])
            if resp.status == 200:
                csv_text = resp.text()
                break
            page.wait_for_timeout(2000)

        browser.close()

        if csv_text is None:
            raise RuntimeError("Timed out waiting for the CSV export to become ready")
        return csv_text


def parse_csv_rows(csv_text: str):
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return []

    norm = lambda h: h.strip().lower().replace(" ", "_")  # noqa: E731
    if len(header) != len(HEADERS):
        print(
            f"WARNING: CSV header has {len(header)} columns, expected {len(HEADERS)}. Got: {header}",
            file=sys.stderr,
        )
    else:
        mism = [(i, header[i], HEADERS[i]) for i in range(len(HEADERS)) if norm(header[i]) != norm(HEADERS[i])]
        if mism:
            print(f"WARNING: CSV header names differ from the sheet header at: {mism}", file=sys.stderr)

    dt_idx = [HEADERS.index(c) for c in DATETIME_COLS]
    rows = []
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        # Defensive pad/truncate: Apps Script's setValues() needs a fixed width.
        if len(row) < len(HEADERS):
            row = row + [""] * (len(HEADERS) - len(row))
        elif len(row) > len(HEADERS):
            row = row[: len(HEADERS)]
        for i in dt_idx:
            row[i] = normalize_datetime(row[i])
        rows.append(row)
    return rows


def post_rows(rows: list):
    """POST the scraped rows to the Web App (retried, see webapp_request).
    Generous timeout: Apps Script's own hard limit is 6 minutes."""
    return webapp_request(
        "POST",
        timeout=400,
        data=json.dumps({"rows": rows}),
        headers={"Content-Type": "application/json"},
    )


def scrape_with_retry(start_str, end_str):
    """Whole-scrape retry with a fresh browser. The debug screenshot/HTML from
    the LAST failed attempt is what ends up in the workflow's artifacts."""
    last_exc = None
    for attempt in range(1, SCRAPE_ATTEMPTS + 1):
        try:
            return scrape_window_csv(start_str, end_str)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
            last_exc = exc
            print(f"Scrape attempt {attempt}/{SCRAPE_ATTEMPTS} for {start_str}-{end_str} failed: {exc}", file=sys.stderr)
            if attempt < SCRAPE_ATTEMPTS:
                print(f"Retrying in {SCRAPE_RETRY_DELAY_S}s with a fresh browser...", file=sys.stderr)
                time.sleep(SCRAPE_RETRY_DELAY_S)
    raise last_exc


def print_result(result):
    print(
        f"Posted {result.get('rows_received')} rows ({result.get('unique_ids')} distinct segment_id): "
        f"{result.get('rows_updated')} updated in place, {result.get('rows_appended')} appended; "
        f"{result.get('duplicates_removed')} stray duplicate row(s) removed; "
        f"sheet now has {result.get('total_rows')} data rows."
    )


def main():
    check_token()

    windows = compute_windows()
    print("Windows this run (America/Bogota): " + "; ".join(f"{lbl} = {a}..{b}" for lbl, a, b in windows))

    failures = []
    for label, start_str, end_str in windows:
        print(f"Pulling 'Flight Lookup' (Mainline={MAINLINE_FILTER}, all stations) for {start_str} to {end_str} ({label})...")
        try:
            csv_text = scrape_with_retry(start_str, end_str)
        except Exception as exc:  # noqa: BLE001
            # The primary window is what the pipeline exists for - a failure
            # there fails the run. A backfill chunk is best-effort.
            if label.startswith("primary"):
                raise
            print(f"Backfill window {start_str}-{end_str} skipped after retries: {exc}", file=sys.stderr)
            failures.append(label)
            continue

        if csv_text is None:
            print(f"No rows for {start_str} to {end_str} - nothing to post for this window.")
            continue

        rows = parse_csv_rows(csv_text)
        print(f"Scraped {len(rows)} rows for {start_str} to {end_str}.")
        print_result(post_rows(rows))

    if failures:
        print(f"Note: {len(failures)} backfill window(s) skipped this run: {', '.join(failures)}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
