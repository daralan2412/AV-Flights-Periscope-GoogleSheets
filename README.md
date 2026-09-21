# AV-Flights-Periscope-GoogleSheets

Pulls the Sisense/Periscope shared report **"Flight Lookup"** filtered to
**Mainline = Avianca (all stations)** into the Google Sheet
**"AV - Flights - BOG"**, four times a day, via GitHub Actions + Playwright +
an Apps Script Web App. Built 2026-09-20 as a new, independent instance of the
FedEx (`FX-periscope-googlesheets`) / Copa (`CM-Master-Periscope-Googlesheets`)
pipelines - none of those repos, sheets or Apps Script projects are touched.

| | |
|---|---|
| Source | https://app.periscopedata.com/shared/3aada203-6788-454f-bca4-760507f50817 (no password) |
| Filters applied | Mainline = `Avianca`; Date Range = Custom Range (see window). Airport / Regional / EquipmentType / Flight_Number left blank = all. |
| Target | https://docs.google.com/spreadsheets/d/1VOw-Ahx7F7INfRzzu-UFtfy3GLrW0sAihMTSL6i7YTU - tab `AV - Flights - BOG` (gid 1896233863). The `SOURCE` tab only holds the report URL. |
| Key | `segment_id` (column A) - one row per id, freshest scrape wins |
| Window | primary **D-2..D0** (America/Bogota) + one rotating 3-day backfill chunk per run (D-5..D-3, D-8..D-6, D-11..D-9, D-14..D-12 by 6-hour slot) |
| Schedule | 00:07, 06:07, 12:07, 18:07 America/Bogota (`7 5,11,17,23 * * *` UTC). GitHub usually starts crons 1-4 h late; the window is computed at run time so that is harmless. |
| Volume | ~9.5k CSV rows per 3-day window (2026-09-20), collapsing to far fewer distinct `segment_id`s |

## How it works

1. `scrape_and_upload.py` (GitHub Actions, ubuntu, Python 3.11, headless
   Chromium) opens the report, opens the Filters panel, types `Avianca` into
   the Mainline search box and ticks the row labelled exactly `Avianca`,
   picks Custom Range and types Start/End (MM/DD/YYYY), clicks Apply and
   waits until the breadcrumbs read `Mainline : Avianca` and
   `DateRange : YYYY-MM-DD to YYYY-MM-DD`.
2. It waits for the "Flight Details" widget's loader to disappear (up to 5
   min - the all-station query is heavy), checks the *visible* empty state,
   then clicks the widget menu -> **Download Data** and polls the
   `/download_csv/` URL until HTTP 200.
3. Timestamp columns are normalised from `2026-09-17 23:00:00` to the tab's
   existing `9/17/2026 23:00` convention; rows are POSTed as
   `{"rows": [[...23 cols...]]}` to the Web App.
4. `apps-script/Code.gs` upserts by `segment_id` (in-place overwrite for
   known ids, append for new ones, all cells as text) and then removes any
   stray duplicate ids keeping the last one. Response:
   `{rows_received, unique_ids, rows_updated, rows_appended, duplicates_removed, total_rows}`.

The run log's evidence of success is the three lines `Pulling ...`,
`Scraped N rows ...`, `Posted N rows (...)`. A green check alone is not proof
(the script exits 0 on a genuine "no rows" window).

## Files

- `scrape_and_upload.py` - the scraper (Playwright) + Web App client.
- `.github/workflows/run.yml` - schedule + `workflow_dispatch` inputs
  (`backfill_start`/`backfill_end` MM/DD/YYYY for a one-off catch-up,
  `skip_backfill=true` to pull only D-2..D0).
- `apps-script/Code.gs`, `apps-script/appsscript.json` - the Web App bound to
  the target sheet (project "AV-Flights Periscope WebApp").
- `requirements.txt` - `requests`, `playwright`.

## Secrets (repo Settings > Secrets and variables > Actions)

- `SHEETS_WEBAPP_URL` - the Web App `/exec` URL.
- `WEBAPP_TOKEN` - must equal the Apps Script Script Property `AUTH_TOKEN`.

## Operating notes

- Re-pulling the same days is expected and safe (upsert). "N updated in
  place" is the steady state, not an error.
- Web App code changes only go live after **Deploy > Manage deployments >
  Edit > Version: New version**.
- `GET <exec>?token=...&action=rebuild` runs the dedupe pass on its own.
- The report repeats a `segment_id` several times with small differences
  (e.g. `equip_type` A319 vs A320); the sheet keeps the last one exported.
- Do not use `.fill()` for the Sisense inputs (search boxes and datepicker
  ignore it), do not press Escape in the datepicker, and never test
  `.error-message` for existence - it is always in the DOM, hidden.
# AV-Flights-Periscope-GoogleSheets
Periscope "Flight Lookup" (Mainline=Avianca, all staions) -> Google Sheet "AV - Flights - BOG", 4x dailyvia GitHub Actions + Apps Script
