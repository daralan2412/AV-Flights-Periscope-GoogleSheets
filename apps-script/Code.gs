/**
 * AV-Flights-Periscope-GoogleSheets - Apps Script Web App
 *
 * NEW, INDEPENDENT PROJECT. It shares nothing (no Script Properties, no
 * deployment, no sheet) with the FedEx REP-1901 pipeline
 * (daralan2412/FX-periscope-googlesheets), the Copa pipeline
 * (daralan2412/CM-Master-Periscope-Googlesheets) or the old PTY wheelchair
 * pipeline (daralan2412/periscope-to-sheets). Those are left completely alone.
 *
 * Source:  Sisense/Periscope shared report "Flight Lookup"
 *          https://app.periscopedata.com/shared/3aada203-6788-454f-bca4-760507f50817
 *          filtered by the scraper to Mainline = Avianca (all stations).
 * Target:  Google Sheet 1VOw-Ahx7F7INfRzzu-UFtfy3GLrW0sAihMTSL6i7YTU
 *          ("AV - Flights - BOG"), tab "AV - Flights - BOG" (gid 1896233863).
 *          The "SOURCE" tab (gid 981954045) only holds the report URL and is
 *          never written. 23 columns, header row in row 1 (see HEADERS).
 *
 * Contract with the scraper (scrape_and_upload.py):
 *   GET  ?token=...                -> {success:true} health check
 *   GET  ?token=...&action=rebuild -> dedupe pass only (no rows posted)
 *   POST ?token=...  {"rows":[[...23 cols...], ...]}
 *        Rows are UPSERTED by segment_id (column A): an id already in the
 *        tab has its row overwritten in place (freshest scrape wins), new
 *        ids are appended. The posted batch is collapsed to one row per id
 *        first (LAST occurrence wins - the report repeats a segment many
 *        times with small differences, e.g. equip_type A319 vs A320).
 *        Then a cleanup pass over column A only removes any stray duplicate
 *        segment_id rows (hand-pasted history), keeping the LAST one. The
 *        cleanup only writes when something has to be removed.
 *        All cells are written as text ('@') so Sheets never locale-parses
 *        "9/18/2026 23:00" into a Date.
 *
 * Both endpoints require ?token=<AUTH_TOKEN> (Script Property, Project
 * Settings > Script Properties). Put the same value in the GitHub secret
 * WEBAPP_TOKEN.
 *
 * Deploy: Deploy > New deployment > Web app, Execute as: Me, Who has access:
 * Anyone. The /exec URL goes in the GitHub secret SHEETS_WEBAPP_URL.
 * Remember: editing this code does NOT change the live /exec until you
 * Deploy > Manage deployments > Edit > Version: New version.
 */

var SPREADSHEET_ID = '1VOw-Ahx7F7INfRzzu-UFtfy3GLrW0sAihMTSL6i7YTU';
var DATA_TAB_NAME = 'AV - Flights - BOG';   // fallback: first tab whose A1 == segment_id
var ID_COL = 1;                              // column A = segment_id (1-based)

// Header row exactly as it exists in the tab today. Column ORDER is what
// matters - it matches the Periscope CSV export column for column.
var HEADERS = [
  'segment_id', 'station', 'mainline', 'regional', 'tail', 'equip_type',
  'arrival_flight', 'arrival_gate', 'arrival_time', 'arrival_scheduled_time', 'arrival_delay',
  'upline_station', 'upline_scheduled_time', 'upline_departure_time',
  'departure_flight', 'departure_gate', 'departure_time', 'departure_scheduled_time', 'departure_delay',
  'downline_station', 'downline_scheduled_time', 'downline_arrival_time', 'ground_time'
];

function doGet(e) {
  if (!checkToken_(e)) return jsonOut_({ success: false, error: 'unauthorized' });
  try {
    var action = (e.parameter && e.parameter.action) || '';
    if (action === 'rebuild') {
      var lock = LockService.getScriptLock();
      if (!lock.tryLock(120000)) return jsonOut_({ success: false, error: 'another upload is in progress (lock timeout)' });
      try {
        return jsonOut_({ success: true, action: action, result: dedupeSheet_(getDataSheet_()) });
      } finally {
        lock.releaseLock();
      }
    }
    var sheet = getDataSheet_();
    return jsonOut_({ success: true, message: 'ok', tab: sheet.getName(), total_rows: Math.max(sheet.getLastRow() - 1, 0) });
  } catch (err) {
    return jsonOut_({ success: false, error: 'doGet failed: ' + err });
  }
}

function doPost(e) {
  if (!checkToken_(e)) return jsonOut_({ success: false, error: 'unauthorized' });

  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return jsonOut_({ success: false, error: 'invalid JSON body: ' + err });
  }
  var rows = (body.rows || []).map(normalizeWidth_);

  // Serialize concurrent runs (a late-firing cron overlapping a manual run)
  // so two upserts never interleave on the tab.
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(120000)) {
    return jsonOut_({ success: false, error: 'another upload is in progress (lock timeout)' });
  }

  try {
    var sheet = getDataSheet_();
    var u = upsertRows_(sheet, rows);
    var d = dedupeSheet_(sheet);
    return jsonOut_({
      success: true,
      rows_received: rows.length,
      unique_ids: u.uniqueIds,
      rows_updated: u.updated,
      rows_appended: u.appended,
      duplicates_removed: d.duplicates,
      total_rows: d.total
    });
  } catch (err) {
    return jsonOut_({ success: false, error: 'doPost failed: ' + err });
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// Sheet lookup
// ---------------------------------------------------------------------------

function getDataSheet_() {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName(DATA_TAB_NAME);
  if (!sheet) {
    var sheets = ss.getSheets();
    for (var i = 0; i < sheets.length; i++) {
      if (String(sheets[i].getRange(1, 1).getValue()).trim() === HEADERS[0]) { sheet = sheets[i]; break; }
    }
  }
  if (!sheet) throw new Error('data tab "' + DATA_TAB_NAME + '" not found (and no tab has segment_id in A1)');
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

// ---------------------------------------------------------------------------
// Upsert: overwrite rows whose segment_id already exists, append the rest
// ---------------------------------------------------------------------------

function upsertRows_(sheet, batch) {
  var lastRow = sheet.getLastRow();

  // Existing ids -> sheet row number (last occurrence wins if the tab has
  // stray duplicates; dedupeSheet_ removes those afterwards).
  var rowById = {};
  if (lastRow >= 2) {
    var ids = sheet.getRange(2, ID_COL, lastRow - 1, 1).getValues();
    for (var i = 0; i < ids.length; i++) {
      var id = String(ids[i][0]).trim();
      if (id) rowById[id] = i + 2;
    }
  }

  // Collapse the batch by id (last posted wins), then split into in-place
  // updates and appends. Rows with a blank id are always appended.
  var updates = {};      // sheet row number -> row values
  var appendsById = {};  // id -> row values
  var appendOrder = [];
  var blankIdRows = [];
  var seen = {};
  batch.forEach(function (row) {
    var id = String(row[ID_COL - 1]).trim();
    if (!id) { blankIdRows.push(row); return; }
    seen[id] = true;
    if (rowById[id]) { updates[rowById[id]] = row; return; }
    if (!appendsById.hasOwnProperty(id)) appendOrder.push(id);
    appendsById[id] = row;
  });

  // Write updates in contiguous blocks (rows of the same days were appended
  // together by an earlier run, so a re-sync usually touches few blocks).
  var rowNums = Object.keys(updates).map(Number).sort(function (a, b) { return a - b; });
  var updated = 0;
  var b = 0;
  while (b < rowNums.length) {
    var e = b;
    while (e + 1 < rowNums.length && rowNums[e + 1] === rowNums[e] + 1) e++;
    var block = [];
    for (var r = b; r <= e; r++) block.push(updates[rowNums[r]]);
    var rng = sheet.getRange(rowNums[b], 1, block.length, HEADERS.length);
    rng.setNumberFormat('@');
    rng.setValues(block);
    updated += block.length;
    b = e + 1;
  }

  var appends = appendOrder.map(function (id) { return appendsById[id]; }).concat(blankIdRows);
  if (appends.length > 0) {
    var target = sheet.getRange(lastRow + 1, 1, appends.length, HEADERS.length);
    target.setNumberFormat('@');
    target.setValues(appends);
  }
  return { updated: updated, appended: appends.length, uniqueIds: Object.keys(seen).length };
}

// ---------------------------------------------------------------------------
// Cleanup: remove stray duplicate segment_id rows (keep the LAST occurrence)
// ---------------------------------------------------------------------------

// Scans only column A. When nothing has to be removed (the normal case, since
// doPost upserts) it returns without touching the sheet. When rows must go,
// it deletes them in contiguous blocks from the bottom up, or - if they are
// scattered across many blocks - falls back to one full rewrite.
function dedupeSheet_(sheet) {
  var lastRow = sheet.getLastRow();
  var lastCol = Math.max(sheet.getLastColumn(), HEADERS.length);
  if (lastRow < 2) return { duplicates: 0, total: 0 };

  var ids = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  var lastIndexById = {};
  for (var j = 0; j < ids.length; j++) {
    var id = String(ids[j][0]).trim();
    if (id) lastIndexById[id] = j;
  }
  var toDelete = [];
  for (var i = 0; i < ids.length; i++) {
    var idI = String(ids[i][0]).trim();
    if (idI && lastIndexById[idI] !== i) toDelete.push(i);
  }

  var total = ids.length - toDelete.length;
  if (toDelete.length === 0) return { duplicates: 0, total: total };

  var blocks = [];
  for (var k = 0; k < toDelete.length; k++) {
    if (blocks.length && toDelete[k] === blocks[blocks.length - 1].end + 1) {
      blocks[blocks.length - 1].end = toDelete[k];
    } else {
      blocks.push({ start: toDelete[k], end: toDelete[k] });
    }
  }

  if (blocks.length <= 50) {
    for (var bi = blocks.length - 1; bi >= 0; bi--) {
      sheet.deleteRows(blocks[bi].start + 2, blocks[bi].end - blocks[bi].start + 1);
    }
  } else {
    // Many scattered rows (e.g. the hand-pasted May-Aug history, which
    // repeated every segment 2-6 times): one read + one write beats
    // thousands of deleteRows calls.
    var data = sheet.getRange(2, 1, lastRow - 1, lastCol).getValues();
    var drop = {};
    toDelete.forEach(function (x) { drop[x] = true; });
    var kept = [];
    for (var d = 0; d < data.length; d++) if (!drop[d]) kept.push(stringifyRow_(data[d]));
    sheet.getRange(2, 1, lastRow - 1, lastCol).clearContent();
    if (kept.length > 0) {
      var dest = sheet.getRange(2, 1, kept.length, lastCol);
      dest.setNumberFormat('@');
      dest.setValues(kept);
    }
  }
  return { duplicates: toDelete.length, total: total };
}

// Legacy rows may hold real Date / number cells (Sheets parsed the pasted
// export). Keep their visible form when rewriting them as text.
function stringifyRow_(row) {
  var tz = SpreadsheetApp.openById(SPREADSHEET_ID).getSpreadsheetTimeZone();
  return row.map(function (v) {
    if (Object.prototype.toString.call(v) === '[object Date]' && !isNaN(v)) {
      var hasTime = v.getHours() !== 0 || v.getMinutes() !== 0;
      return Utilities.formatDate(v, tz, hasTime ? 'M/d/yyyy H:mm' : 'M/d/yyyy');
    }
    if (typeof v === 'number') return String(v);
    return v === null || v === undefined ? '' : String(v);
  });
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

function normalizeWidth_(row) {
  row = row || [];
  if (row.length < HEADERS.length) return row.concat(new Array(HEADERS.length - row.length).fill(''));
  if (row.length > HEADERS.length) return row.slice(0, HEADERS.length);
  return row;
}

function checkToken_(e) {
  var token = PropertiesService.getScriptProperties().getProperty('AUTH_TOKEN');
  return !!token && e && e.parameter && e.parameter.token === token;
}

function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// Manual helper: run once from the editor to grant the Sheets scope and
// confirm the tab is reachable.
function debugInfo() {
  var sheet = getDataSheet_();
  Logger.log(sheet.getParent().getName() + ' / ' + sheet.getName() + ': ' + (sheet.getLastRow() - 1) + ' data rows, ' + sheet.getLastColumn() + ' cols');
  Logger.log('A1..C1 = ' + JSON.stringify(sheet.getRange(1, 1, 1, 3).getValues()[0]));
}

// Manual helper: dedupe the tab without posting anything (one-off after setup).
function debugDedupe() {
  Logger.log(JSON.stringify(dedupeSheet_(getDataSheet_())));
}
