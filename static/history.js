"use strict";

// My Games page: client-side sort + filter over the embedded games list
// (window.HISTORY_GAMES). Kept ES5-safe/simple, no framework. Show/hide relies
// on the [hidden] attribute (the global CSS rule governs it).

var GAMES = window.HISTORY_GAMES || [];
var INDEX_URL = window.HISTORY_INDEX_URL || "/";

var bodyEl = document.getElementById("historyBody");
var noMatchEl = document.getElementById("noMatch");
var sortSelect = document.getElementById("sortSelect");
var filterToggle = document.getElementById("filterToggle");
var filterBody = document.getElementById("filterBody");
var filterClear = document.getElementById("filterClear");
var fDateFrom = document.getElementById("fDateFrom");
var fDateTo = document.getElementById("fDateTo");
var fEco = document.getElementById("fEco");

// -------- classification helpers (mirror ratings.py) --------------------

// Time-class by OUR definition (same thresholds as ratings.time_class):
//   T = base + 60*inc; T<=600 blitz; T<3600 rapid; else classical.
function timeClass(base, inc) {
  var t = base + 60 * (inc || 0);
  if (t <= 600) return "blitz";
  if (t < 3600) return "rapid";
  return "classical";
}

// Time-control FILTER buckets. Exactly five, NO bullet:
//   * 'unlimited' when base_seconds is null (no clock).
//   * else the derived class blitz/rapid/classical.
//   * 'custom' ALSO matches any explicitly time-limited control (the app only
//     offers Custom or Unlimited), so a limited game matches BOTH its class
//     and 'custom'. This returns the full set of buckets a game qualifies for
//     so the filter can OR across selected buckets.
function timeControlBuckets(base, inc) {
  if (base === null || base === undefined) return ["unlimited"];
  return [timeClass(base, inc), "custom"];
}

// win/draw/loss RELATIVE TO THE HUMAN, from (result, human_color).
function resultClass(result, humanColor) {
  if (result === "1/2-1/2") return "draw";
  var whiteWon = (result === "1-0");
  var humanIsWhite = (humanColor === "white");
  return (whiteWon === humanIsWhite) ? "win" : "loss";
}

// Map a raw stored result_reason code to a canonical reason CATEGORY used by
// the reason sub-filter. Mirrors chess_core.canonical_reason categories.
function reasonCategory(code) {
  if (!code) return null;
  var c = String(code).toLowerCase();
  if (c === "checkmate") return "checkmate";
  if (c === "resignation") return "resignation";
  if (c === "time forfeit" || c === "time") return "on time";
  if (c === "repetition" || c.indexOf("repetition") >= 0) return "3-fold repetition";
  if (c === "stalemate") return "stalemate";
  if (c.indexOf("insufficient") >= 0) return "insufficient material";
  if (c.indexOf("fifty") >= 0 || c.indexOf("50") >= 0) return "50-move rule";
  return null;
}

// The Result column token only: '*' unfinished, else the result string.
function resultToken(g) {
  if (g.status === "in_progress" || !g.result) return "*";
  return g.result;   // '1-0' / '0-1' / '1/2-1/2'
}

// -------- checkbox helpers ----------------------------------------------

function checkedValues(cls) {
  var out = [];
  var els = document.getElementsByClassName(cls);
  for (var i = 0; i < els.length; i++) {
    if (els[i].checked) out.push(els[i].value);
  }
  return out;
}

function contains(arr, v) {
  for (var i = 0; i < arr.length; i++) { if (arr[i] === v) return true; }
  return false;
}

// -------- filtering (AND across categories, OR within a multi-select) ----

function passesFilters(g) {
  var wantResult = checkedValues("f-result");
  var wantReason = checkedValues("f-reason");
  var wantTc = checkedValues("f-tc");
  var wantColor = checkedValues("f-color");
  var wantMode = checkedValues("f-mode");
  var ecoQuery = (fEco && fEco.value ? fEco.value.trim().toUpperCase() : "");
  var dateFrom = (fDateFrom && fDateFrom.value ? fDateFrom.value : "");
  var dateTo = (fDateTo && fDateTo.value ? fDateTo.value : "");

  // RESULT (only meaningful for finished games; an in-progress game has no
  // win/draw/loss so it is excluded when a result filter is active).
  if (wantResult.length) {
    if (g.status === "in_progress" || !g.result) return false;
    if (!contains(wantResult, resultClass(g.result, g.human_color))) return false;
  }

  // REASON sub-filter (over the canonical reason category).
  if (wantReason.length) {
    if (g.status === "in_progress") return false;
    var cat = reasonCategory(g.result_reason);
    if (!cat || !contains(wantReason, cat)) return false;
  }

  // TIME CONTROL: game matches if ANY of its buckets is selected.
  if (wantTc.length) {
    var buckets = timeControlBuckets(g.base_seconds, g.increment);
    var tcHit = false;
    for (var i = 0; i < buckets.length; i++) {
      if (contains(wantTc, buckets[i])) { tcHit = true; break; }
    }
    if (!tcHit) return false;
  }

  // COLOR (human's color).
  if (wantColor.length && !contains(wantColor, g.human_color)) return false;

  // MODE. NULL mode (pre-migration rows) never matches an explicit selection.
  if (wantMode.length) {
    if (!g.mode || !contains(wantMode, g.mode)) return false;
  }

  // ECO CODE (prefix match on the classified code).
  if (ecoQuery) {
    if (!g.eco_code || String(g.eco_code).toUpperCase().indexOf(ecoQuery) !== 0) {
      return false;
    }
  }

  // DATE RANGE on the START date (YYYY-MM-DD string compare is safe).
  var start = g.date || (g.started_at ? String(g.started_at).slice(0, 10) : "");
  if (dateFrom && start < dateFrom) return false;
  if (dateTo && start > dateTo) return false;

  return true;
}

// -------- sorting --------------------------------------------------------

function cmpDesc(a, b) { return a < b ? 1 : (a > b ? -1 : 0); }

function sortGames(list, how) {
  var arr = list.slice();
  if (how === "start") {
    // Latest start first.
    arr.sort(function (a, b) {
      return cmpDesc(String(a.started_at || ""), String(b.started_at || ""));
    });
  } else if (how === "end") {
    // By end time, latest first. Ongoing games (no end time) sort LAST.
    arr.sort(function (a, b) {
      var ae = a.ended_at || "";
      var be = b.ended_at || "";
      if (!ae && !be) {
        return cmpDesc(String(a.started_at || ""), String(b.started_at || ""));
      }
      if (!ae) return 1;   // a ongoing -> after b
      if (!be) return -1;  // b ongoing -> after a
      return cmpDesc(String(ae), String(be));
    });
  } else {
    // Default: ongoing (in-progress) games ABOVE completed; within each group
    // latest start on top.
    arr.sort(function (a, b) {
      var aLive = (a.status === "in_progress") ? 0 : 1;
      var bLive = (b.status === "in_progress") ? 0 : 1;
      if (aLive !== bLive) return aLive - bLive;   // live group first
      return cmpDesc(String(a.started_at || ""), String(b.started_at || ""));
    });
  }
  return arr;
}

// -------- rendering ------------------------------------------------------

function makeCell(text, cls) {
  var td = document.createElement("td");
  if (cls) td.className = cls;
  td.textContent = text;
  return td;
}

function renderRows() {
  if (!bodyEl) return;
  var how = sortSelect ? sortSelect.value : "default";
  var filtered = [];
  for (var i = 0; i < GAMES.length; i++) {
    if (passesFilters(GAMES[i])) filtered.push(GAMES[i]);
  }
  var ordered = sortGames(filtered, how);

  // Clear existing rows.
  while (bodyEl.firstChild) bodyEl.removeChild(bodyEl.firstChild);

  for (var j = 0; j < ordered.length; j++) {
    var g = ordered[j];
    var tr = document.createElement("tr");
    // Date = START date only (YYYY-MM-DD).
    var dateStr = g.date || (g.started_at ? String(g.started_at).slice(0, 10) : "\u2014");
    tr.appendChild(makeCell(dateStr, "mono"));
    // Result = token only (no reason text).
    tr.appendChild(makeCell(resultToken(g), "mono"));
    // Action: Resume (in-progress) or Review (finished).
    var actionTd = document.createElement("td");
    var a = document.createElement("a");
    a.className = "btn btn-small";
    if (g.status === "in_progress") {
      a.textContent = "Resume";
      a.href = INDEX_URL;
    } else {
      a.textContent = "Review";
      a.href = INDEX_URL + "?replay=" + encodeURIComponent(g.id);
    }
    actionTd.appendChild(a);
    tr.appendChild(actionTd);
    bodyEl.appendChild(tr);
  }

  if (noMatchEl) noMatchEl.hidden = (ordered.length !== 0);
}

// -------- wiring ---------------------------------------------------------

function wireFilterInputs() {
  var classes = ["f-result", "f-reason", "f-tc", "f-color", "f-mode"];
  for (var c = 0; c < classes.length; c++) {
    var els = document.getElementsByClassName(classes[c]);
    for (var i = 0; i < els.length; i++) {
      els[i].addEventListener("change", renderRows);
    }
  }
  if (fEco) fEco.addEventListener("input", renderRows);
  if (fDateFrom) fDateFrom.addEventListener("change", renderRows);
  if (fDateTo) fDateTo.addEventListener("change", renderRows);
  if (sortSelect) sortSelect.addEventListener("change", renderRows);
}

function clearFilters() {
  var classes = ["f-result", "f-reason", "f-tc", "f-color", "f-mode"];
  for (var c = 0; c < classes.length; c++) {
    var els = document.getElementsByClassName(classes[c]);
    for (var i = 0; i < els.length; i++) els[i].checked = false;
  }
  if (fEco) fEco.value = "";
  if (fDateFrom) fDateFrom.value = "";
  if (fDateTo) fDateTo.value = "";
  renderRows();
}

// Generic disclosure toggle using the [hidden] attribute.
function wireDisclosure(toggleBtn, panel) {
  if (!toggleBtn || !panel) return;
  toggleBtn.addEventListener("click", function () {
    var open = panel.hidden;
    panel.hidden = !open;
    toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { toggleBtn.className += " open"; }
    else { toggleBtn.className = toggleBtn.className.replace(/\s*open/g, ""); }
  });
}

if (bodyEl) {
  wireDisclosure(filterToggle, filterBody);
  wireFilterInputs();
  if (filterClear) filterClear.addEventListener("click", clearFilters);
  renderRows();
}
