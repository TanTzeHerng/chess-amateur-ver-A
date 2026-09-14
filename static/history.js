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
var fTcCustom = document.getElementById("fTcCustom");
var customTcInputs = document.getElementById("customTcInputs");
var fCustomH = document.getElementById("fCustomH");
var fCustomM = document.getElementById("fCustomM");
var fCustomS = document.getElementById("fCustomS");
var fCustomInc = document.getElementById("fCustomInc");

// -------- classification helpers (mirror ratings.py) --------------------

// Time-class by OUR definition (same thresholds as ratings.time_class):
//   T = base + 60*inc; T<=600 blitz; T<3600 rapid; else classical.
function timeClass(base, inc) {
  var t = base + 60 * (inc || 0);
  if (t <= 600) return "blitz";
  if (t < 3600) return "rapid";
  return "classical";
}

// Time-control CLASS buckets. Exactly four, NO bullet:
//   * 'unlimited' when base_seconds is null (no clock).
//   * else the derived class blitz/rapid/classical.
// NOTE: 'custom' is NOT a class bucket. It is a SEPARATE filter meaning "this
// exact base+increment" (see customTcMatches); a limited game does NOT auto-
// qualify for 'custom' just by having a clock.
function timeControlBuckets(base, inc) {
  if (base === null || base === undefined) return ["unlimited"];
  return [timeClass(base, inc)];
}

// CUSTOM time-control match: true when the game's base_seconds and increment
// equal the values the user entered in the custom base(h/m/s)+increment inputs.
// Unlimited games (base null) never match. Reads the inputs live each call.
function customTcMatches(base, inc) {
  if (base === null || base === undefined) return false;
  var h = intVal(fCustomH), m = intVal(fCustomM), s = intVal(fCustomS);
  var wantBase = h * 3600 + m * 60 + s;
  var wantInc = intVal(fCustomInc);
  return (Number(base) === wantBase) && (Number(inc || 0) === wantInc);
}

function intVal(el) {
  if (!el || el.value === "" || el.value === null) return 0;
  var n = parseInt(el.value, 10);
  return isNaN(n) ? 0 : n;
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
  if (c === "time forfeit" || c === "time" || c === "on time" ||
      c === "timeout") return "timeout";
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

// -------- hover mini-board replay (self-contained, no app.js) ------------
//
// history.js runs on the My Games page where app.js is NOT loaded, so this is
// a small standalone board renderer. It does NOT need full chess legality: the
// stored games are already-legal move lists, so we simply apply each UCI move
// to an 8x8 array (moving the piece, handling captures, promotions, castling
// and en-passant) and repaint. On hover over a game's result we show a tiny
// board near the cursor and replay the moves on a short timer; on mouseleave
// we hide it (via the [hidden] attribute) and stop the timer.

var GLYPHS = {
  K: "\u2654", Q: "\u2655", R: "\u2656", B: "\u2657", N: "\u2658", P: "\u2659",
  k: "\u265A", q: "\u265B", r: "\u265C", b: "\u265D", n: "\u265E", p: "\u265F"
};

var REPLAY_STEP_MS = 220;   // quick replay cadence (identify the game fast)

var miniEl = null;          // the floating mini-board container
var miniSquares = null;     // 64 cell <span>s, index 0 = a8 .. 63 = h1
var miniTimer = null;

// Standard starting placement as an 8x8 array of piece chars (or "").
// board[rank][file] with rank 0 = rank 8 (top), file 0 = file a (left).
function startBoard() {
  var back = "rnbqkbnr";
  var b = [];
  var r, f, row;
  row = [];
  for (f = 0; f < 8; f++) row.push(back.charAt(f));           // black back rank
  b.push(row);
  row = [];
  for (f = 0; f < 8; f++) row.push("p");                       // black pawns
  b.push(row);
  for (r = 0; r < 4; r++) {
    row = [];
    for (f = 0; f < 8; f++) row.push("");
    b.push(row);
  }
  row = [];
  for (f = 0; f < 8; f++) row.push("P");                       // white pawns
  b.push(row);
  row = [];
  for (f = 0; f < 8; f++) row.push(back.charAt(f).toUpperCase()); // white back
  b.push(row);
  return b;
}

// Convert a UCI square like "e4" to [rank, file] into the board array.
function sqToRF(sq) {
  var file = sq.charCodeAt(0) - 97;        // 'a' -> 0
  var rank = 8 - parseInt(sq.charAt(1), 10); // '8' -> 0
  return [rank, file];
}

// Apply one UCI move (e.g. "e2e4", "e7e8q", "e1g1") to the board array.
function applyUci(board, uci) {
  if (!uci || uci.length < 4) return;
  var from = sqToRF(uci.slice(0, 2));
  var to = sqToRF(uci.slice(2, 4));
  var promo = uci.length > 4 ? uci.charAt(4) : "";
  var piece = board[from[0]][from[1]];
  if (!piece) return;
  var isPawn = (piece === "P" || piece === "p");
  var isKing = (piece === "K" || piece === "k");
  // En-passant: a pawn moves diagonally to an empty square -> remove the
  // captured pawn that sits on the from-rank, to-file.
  if (isPawn && from[1] !== to[1] && board[to[0]][to[1]] === "") {
    board[from[0]][to[1]] = "";
  }
  board[from[0]][from[1]] = "";
  // Promotion: replace with the promoted piece (case matches the mover).
  if (promo) {
    piece = (piece === "P") ? promo.toUpperCase() : promo.toLowerCase();
  }
  board[to[0]][to[1]] = piece;
  // Castling: king moves two files -> move the matching rook too.
  if (isKing && Math.abs(to[1] - from[1]) === 2) {
    var rank = from[0];
    if (to[1] === 6) {            // king-side
      board[rank][5] = board[rank][7];
      board[rank][7] = "";
    } else if (to[1] === 2) {     // queen-side
      board[rank][3] = board[rank][0];
      board[rank][0] = "";
    }
  }
}

// Build the mini-board DOM once (64 cells) and keep it hidden until hover.
function ensureMini() {
  if (miniEl) return;
  miniEl = document.createElement("div");
  miniEl.className = "mini-board";
  miniEl.hidden = true;
  miniSquares = [];
  for (var i = 0; i < 64; i++) {
    var cell = document.createElement("span");
    var r = Math.floor(i / 8), f = i % 8;
    // Light/dark checker using in-palette colors (see style.css classes).
    cell.className = "mini-sq " + (((r + f) % 2 === 0) ? "mini-light" : "mini-dark");
    miniEl.appendChild(cell);
    miniSquares.push(cell);
  }
  document.body.appendChild(miniEl);
}

// Paint the current board array into the mini-board cells.
function paintMini(board) {
  for (var r = 0; r < 8; r++) {
    for (var f = 0; f < 8; f++) {
      var cell = miniSquares[r * 8 + f];
      var p = board[r][f];
      cell.textContent = p ? GLYPHS[p] : "";
      // White pieces = white glyph color; black pieces = yellow (both in
      // palette and distinct on the cyan/blue checker).
      cell.className = cell.className.replace(/\s*mini-wp|\s*mini-bp/g, "");
      if (p) cell.className += (p === p.toUpperCase()) ? " mini-wp" : " mini-bp";
    }
  }
}

function stopReplay() {
  if (miniTimer) { clearInterval(miniTimer); miniTimer = null; }
}

// Start (or restart) a quick replay of a game's moves on the mini-board.
function startReplay(moves) {
  ensureMini();
  stopReplay();
  var board = startBoard();
  paintMini(board);
  if (!moves || !moves.length) return;
  var i = 0;
  miniTimer = setInterval(function () {
    if (i >= moves.length) {
      // Loop: pause briefly at the final position, then restart so a long
      // hover keeps replaying (helps identify the game).
      stopReplay();
      miniTimer = setInterval(function () {
        board = startBoard();
        paintMini(board);
        i = 0;
        stopReplay();
        startReplay(moves);
      }, REPLAY_STEP_MS * 3);
      return;
    }
    applyUci(board, moves[i]);
    paintMini(board);
    i++;
  }, REPLAY_STEP_MS);
}

// Position the mini-board near the cursor (offset so it does not sit under the
// pointer), clamped to the viewport.
function moveMiniTo(x, y) {
  if (!miniEl) return;
  var pad = 16;
  var w = miniEl.offsetWidth || 160;
  var h = miniEl.offsetHeight || 160;
  var left = x + pad;
  var top = y + pad;
  if (left + w > window.innerWidth) left = x - w - pad;
  if (top + h > window.innerHeight) top = y - h - pad;
  if (left < 0) left = 0;
  if (top < 0) top = 0;
  miniEl.style.left = left + "px";
  miniEl.style.top = top + "px";
}

// Wire hover on a result cell to show/replay the mini-board for game `g`.
function wireMiniHover(cell, g) {
  cell.addEventListener("mouseenter", function (ev) {
    ensureMini();
    startReplay(g.moves || []);
    moveMiniTo(ev.clientX, ev.clientY);
    miniEl.hidden = false;
  });
  cell.addEventListener("mousemove", function (ev) {
    moveMiniTo(ev.clientX, ev.clientY);
  });
  cell.addEventListener("mouseleave", function () {
    stopReplay();
    if (miniEl) miniEl.hidden = true;
  });
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

  // TIME CONTROL: game matches if ANY selected bucket applies (OR). The class
  // buckets (blitz/rapid/classical/unlimited) match via timeControlBuckets;
  // 'custom' is a SEPARATE match on the exact base+increment inputs.
  if (wantTc.length) {
    var buckets = timeControlBuckets(g.base_seconds, g.increment);
    var tcHit = false;
    for (var i = 0; i < buckets.length; i++) {
      if (contains(wantTc, buckets[i])) { tcHit = true; break; }
    }
    if (!tcHit && contains(wantTc, "custom") &&
        customTcMatches(g.base_seconds, g.increment)) {
      tcHit = true;
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

  // DATE RANGE on the server-derived GMT+8 date column (g.date, YYYY-MM-DD
  // string compare is safe). We rely solely on g.date so the filter never
  // diverges from the server's GMT+8 bucketing; if it is absent we treat the
  // game as not date-filterable rather than fabricating a client-local date.
  if (dateFrom || dateTo) {
    var start = g.date || "";
    if (!start) return false;
    if (dateFrom && start < dateFrom) return false;
    if (dateTo && start > dateTo) return false;
  }

  // COLLECTION filter: when a folder is selected, only show games filed in it
  // (or any of its subfolders). null selection = All games (no restriction).
  if (typeof selectedCollectionId !== "undefined" && selectedCollectionId !== null) {
    if (!gameInSubtree(g.id, selectedCollectionId)) return false;
  }

  return true;
}

// -------- sorting --------------------------------------------------------

function cmpDesc(a, b) { return a < b ? 1 : (a > b ? -1 : 0); }
function cmpAsc(a, b) { return a < b ? -1 : (a > b ? 1 : 0); }

function sortGames(list, how) {
  var arr = list.slice();
  if (how === "date_asc") {
    // By game start DATE, oldest first.
    arr.sort(function (a, b) {
      return cmpAsc(String(a.started_at || ""), String(b.started_at || ""));
    });
  } else if (how === "date_desc") {
    // By game start DATE, newest first.
    arr.sort(function (a, b) {
      return cmpDesc(String(a.started_at || ""), String(b.started_at || ""));
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

// Format the post-game rating for display (integer rounding, matching how the
// board shows ratings). Returns "" when there is nothing to show.
function formatRating(r) {
  if (r === null || r === undefined || r === "") return "";
  var n = Number(r);
  if (isNaN(n)) return "";
  return String(Math.round(n));
}

// Result cell: colored token + optional post-game rating (bold) and delta (in
// parentheses). Coloring: green=human win, red=loss, white=draw; in-progress
// '*' stays the default color.
function makeResultCell(g) {
  var td = document.createElement("td");
  td.className = "mono result-cell";

  var token = document.createElement("span");
  token.className = "result-token";
  token.textContent = resultToken(g);
  if (g.status !== "in_progress" && g.result) {
    var rc = resultClass(g.result, g.human_color);  // win|draw|loss
    token.className += " result-" + rc;
  }
  td.appendChild(token);

  // Post-game rating + delta (rated/FIDE only). Casual/guest games store
  // neither, so nothing extra is shown.
  var ratingText = formatRating(g.player_rating_after);
  var deltaText = (g.rating_delta ? String(g.rating_delta) : "");
  if (ratingText || deltaText) {
    if (ratingText) {
      var ratingEl = document.createElement("strong");
      ratingEl.className = "result-rating";
      ratingEl.textContent = ratingText;
      td.appendChild(document.createTextNode(" "));
      td.appendChild(ratingEl);
    }
    if (deltaText) {
      var deltaEl = document.createElement("span");
      deltaEl.className = "result-delta";
      deltaEl.textContent = "(" + deltaText + ")";
      td.appendChild(document.createTextNode(" "));
      td.appendChild(deltaEl);
    }
  }
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
    // Result cell: the token, colored green/red/white for win/loss/draw
    // (relative to the human), plus the post-game rating (bold) and delta (in
    // parentheses) to its right for rated/FIDE games. Hovering it shows the
    // cursor-following mini-board that quickly replays this game.
    var resultTd = makeResultCell(g);
    wireMiniHover(resultTd, g);
    tr.appendChild(resultTd);
    // Action: Resume (in-progress) or Review (finished), plus an Add-to-
    // collection control so the game can be filed into a folder.
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
    actionTd.appendChild(document.createTextNode(" "));
    actionTd.appendChild(makeAssignControl(g));
    tr.appendChild(actionTd);
    bodyEl.appendChild(tr);
  }

  if (noMatchEl) noMatchEl.hidden = (ordered.length !== 0);
}

// -------- collections tree (folders for My Games) ------------------------
//
// A per-user nestable folder tree. Data starts from window.HISTORY_COLLECTIONS
// (flat rows {id, parent_id, name}) which we assemble into a tree by parent_id.
// The user can create/rename/delete folders and subfolders, assign a game to a
// folder (via the per-row Add control), and filter the games list to the games
// in the selected folder. All network calls hit the /api/collections endpoints
// (see app.py). Show/hide uses the [hidden] attribute; palette-only styling.

var COLLECTIONS = window.HISTORY_COLLECTIONS || [];
var treeEl = document.getElementById("collectionsTree");
var collNewRootBtn = document.getElementById("collNewRoot");
// Currently selected collection id for filtering (null = All games). Set of
// expanded folder ids so the tree keeps its open/closed state across renders.
var selectedCollectionId = null;
var expanded = {};

// Membership lookup {gameId: [collectionId,...]} built from each embedded
// game's `collections` array (added by the history_page endpoint).
function membershipOf(gameId) {
  for (var i = 0; i < GAMES.length; i++) {
    if (GAMES[i].id === gameId) return GAMES[i].collections || [];
  }
  return [];
}

function childrenOf(parentId) {
  var out = [];
  for (var i = 0; i < COLLECTIONS.length; i++) {
    var c = COLLECTIONS[i];
    var pid = (c.parent_id === undefined ? null : c.parent_id);
    if (pid === parentId || (parentId === null && (pid === null || pid === undefined))) {
      out.push(c);
    }
  }
  out.sort(function (a, b) { return cmpAsc(String(a.name), String(b.name)); });
  return out;
}

// True when `gameId` belongs to `collectionId` OR any of its descendants (so
// selecting a parent folder shows games filed in its subfolders too).
function gameInSubtree(gameId, collectionId) {
  var members = membershipOf(gameId);
  var ids = subtreeIds(collectionId);
  for (var i = 0; i < members.length; i++) {
    if (contains(ids, members[i])) return true;
  }
  return false;
}

function subtreeIds(collectionId) {
  var ids = [collectionId];
  var frontier = [collectionId];
  while (frontier.length) {
    var kids = childrenOf(frontier.pop());
    for (var i = 0; i < kids.length; i++) {
      ids.push(kids[i].id);
      frontier.push(kids[i].id);
    }
  }
  return ids;
}

// --- API helpers (thin fetch wrappers; all same-origin, session-cookie) ---

function apiJson(url, method, body) {
  var opts = { method: method, credentials: "same-origin",
               headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  return fetch(url, opts).then(function (r) {
    return r.json().then(function (data) {
      return { ok: r.ok, status: r.status, data: data };
    }, function () { return { ok: r.ok, status: r.status, data: {} }; });
  });
}

function reloadCollections() {
  return apiJson("/api/collections", "GET").then(function (res) {
    if (res.ok && res.data && res.data.collections) {
      COLLECTIONS = res.data.collections;
    }
    renderTree();
  });
}

function createCollection(name, parentId) {
  apiJson("/api/collections", "POST",
          { name: name, parent_id: parentId }).then(function (res) {
    if (!res.ok) { alert((res.data && res.data.error) || "Could not create folder."); return; }
    if (parentId !== null && parentId !== undefined) expanded[parentId] = true;
    reloadCollections();
  });
}

function renameCollection(id, name) {
  apiJson("/api/collections/" + id, "PATCH", { name: name }).then(function (res) {
    if (!res.ok) { alert((res.data && res.data.error) || "Could not rename folder."); return; }
    reloadCollections();
  });
}

function deleteCollection(id) {
  apiJson("/api/collections/" + id, "DELETE").then(function (res) {
    if (!res.ok) { alert((res.data && res.data.error) || "Could not delete folder."); return; }
    // Drop local memberships to the removed subtree so filtering stays correct
    // until the next full page load.
    var removed = subtreeIds(id);
    for (var i = 0; i < GAMES.length; i++) {
      var m = GAMES[i].collections || [];
      var kept = [];
      for (var j = 0; j < m.length; j++) {
        if (!contains(removed, m[j])) kept.push(m[j]);
      }
      GAMES[i].collections = kept;
    }
    if (contains(removed, selectedCollectionId)) selectedCollectionId = null;
    reloadCollections();
    renderRows();
  });
}

function assignGame(gameId, collectionId) {
  apiJson("/api/collections/" + collectionId + "/games", "POST",
          { game_id: gameId }).then(function (res) {
    if (!res.ok) { alert((res.data && res.data.error) || "Could not add game."); return; }
    var m = membershipOf(gameId);
    if (!contains(m, collectionId)) m.push(collectionId);
    setMembership(gameId, m);
    renderRows();
  });
}

function unassignGame(gameId, collectionId) {
  apiJson("/api/collections/" + collectionId + "/games/" + gameId,
          "DELETE").then(function (res) {
    if (!res.ok) { alert((res.data && res.data.error) || "Could not remove game."); return; }
    var m = membershipOf(gameId);
    var kept = [];
    for (var i = 0; i < m.length; i++) { if (m[i] !== collectionId) kept.push(m[i]); }
    setMembership(gameId, kept);
    renderRows();
  });
}

function setMembership(gameId, list) {
  for (var i = 0; i < GAMES.length; i++) {
    if (GAMES[i].id === gameId) { GAMES[i].collections = list; return; }
  }
}

// --- tree rendering ---

function selectCollection(id) {
  selectedCollectionId = id;
  renderTree();
  renderRows();
}

function makeTreeNode(coll) {
  var li = document.createElement("li");
  li.className = "coll-node";
  var kids = childrenOf(coll.id);

  var rowEl = document.createElement("div");
  rowEl.className = "coll-row" + (selectedCollectionId === coll.id ? " coll-selected" : "");

  // Expand/collapse toggle (only meaningful when there are children).
  var toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "coll-toggle";
  toggle.textContent = kids.length ? (expanded[coll.id] ? "\u25BE" : "\u25B8") : "\u00B7";
  if (kids.length) {
    toggle.addEventListener("click", function () {
      expanded[coll.id] = !expanded[coll.id];
      renderTree();
    });
  }
  rowEl.appendChild(toggle);

  // Folder name selects it for filtering.
  var label = document.createElement("button");
  label.type = "button";
  label.className = "coll-name btnlink";
  label.textContent = coll.name;
  label.addEventListener("click", function () { selectCollection(coll.id); });
  rowEl.appendChild(label);

  // Actions: new subfolder, rename, delete.
  var actions = document.createElement("span");
  actions.className = "coll-actions";
  actions.appendChild(makeCollActionBtn("+", "New subfolder", function () {
    var name = prompt("New subfolder name:");
    if (name && name.trim()) createCollection(name.trim(), coll.id);
  }));
  actions.appendChild(makeCollActionBtn("\u270E", "Rename", function () {
    var name = prompt("Rename folder:", coll.name);
    if (name && name.trim()) renameCollection(coll.id, name.trim());
  }));
  actions.appendChild(makeCollActionBtn("\u2715", "Delete", function () {
    if (confirm("Delete \"" + coll.name + "\" and its subfolders? Games are not deleted.")) {
      deleteCollection(coll.id);
    }
  }));
  rowEl.appendChild(actions);
  li.appendChild(rowEl);

  // Children (hidden via [hidden] when collapsed).
  if (kids.length) {
    var childUl = document.createElement("ul");
    childUl.className = "collections-tree";
    childUl.hidden = !expanded[coll.id];
    for (var i = 0; i < kids.length; i++) {
      childUl.appendChild(makeTreeNode(kids[i]));
    }
    li.appendChild(childUl);
  }
  return li;
}

function makeCollActionBtn(text, title, onClick) {
  var b = document.createElement("button");
  b.type = "button";
  b.className = "coll-act btnlink";
  b.textContent = text;
  b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

function renderTree() {
  if (!treeEl) return;
  while (treeEl.firstChild) treeEl.removeChild(treeEl.firstChild);

  // 'All games' pseudo-root (selectedCollectionId === null).
  var allLi = document.createElement("li");
  allLi.className = "coll-node";
  var allRow = document.createElement("div");
  allRow.className = "coll-row" + (selectedCollectionId === null ? " coll-selected" : "");
  var allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "coll-name btnlink";
  allBtn.textContent = "All games";
  allBtn.addEventListener("click", function () { selectCollection(null); });
  allRow.appendChild(allBtn);
  allLi.appendChild(allRow);
  treeEl.appendChild(allLi);

  var roots = childrenOf(null);
  for (var i = 0; i < roots.length; i++) {
    treeEl.appendChild(makeTreeNode(roots[i]));
  }
}

// Per-row "Add to collection" control: a small dropdown of the user's folders.
// Selecting a folder assigns (or, if already a member, removes) the game.
function makeAssignControl(g) {
  var sel = document.createElement("select");
  sel.className = "assign-select";
  var def = document.createElement("option");
  def.value = "";
  def.textContent = "Collection\u2026";
  sel.appendChild(def);
  var members = membershipOf(g.id);
  for (var i = 0; i < COLLECTIONS.length; i++) {
    var c = COLLECTIONS[i];
    var opt = document.createElement("option");
    opt.value = String(c.id);
    var isMember = contains(members, c.id);
    opt.textContent = (isMember ? "\u2713 " : "") + collectionPath(c);
    sel.appendChild(opt);
  }
  sel.addEventListener("change", function () {
    var id = parseInt(sel.value, 10);
    sel.value = "";
    if (isNaN(id)) return;
    if (contains(membershipOf(g.id), id)) unassignGame(g.id, id);
    else assignGame(g.id, id);
  });
  return sel;
}

// Human-readable folder path "Parent / Child" for the assign dropdown.
function collectionPath(coll) {
  var byId = {};
  for (var i = 0; i < COLLECTIONS.length; i++) byId[COLLECTIONS[i].id] = COLLECTIONS[i];
  var parts = [];
  var cur = coll;
  var guard = 0;
  while (cur && guard < 64) {
    parts.unshift(cur.name);
    cur = (cur.parent_id === null || cur.parent_id === undefined) ? null : byId[cur.parent_id];
    guard++;
  }
  return parts.join(" / ");
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
  // Reveal/hide the custom base+increment inputs when 'custom' is toggled.
  if (fTcCustom) fTcCustom.addEventListener("change", syncCustomTcInputs);
  var customInputs = [fCustomH, fCustomM, fCustomS, fCustomInc];
  for (var k = 0; k < customInputs.length; k++) {
    if (customInputs[k]) customInputs[k].addEventListener("input", renderRows);
  }
}

// Show the custom base+increment inputs only while 'custom' is checked
// (show/hide via the [hidden] attribute).
function syncCustomTcInputs() {
  if (!customTcInputs) return;
  customTcInputs.hidden = !(fTcCustom && fTcCustom.checked);
  renderRows();
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
  if (fCustomH) fCustomH.value = "0";
  if (fCustomM) fCustomM.value = "0";
  if (fCustomS) fCustomS.value = "0";
  if (fCustomInc) fCustomInc.value = "0";
  if (customTcInputs) customTcInputs.hidden = true;
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
  if (customTcInputs) {
    customTcInputs.hidden = !(fTcCustom && fTcCustom.checked);
  }
  // Collections tree: create a root folder button + initial render.
  if (collNewRootBtn) {
    collNewRootBtn.addEventListener("click", function () {
      var name = prompt("New folder name:");
      if (name && name.trim()) createCollection(name.trim(), null);
    });
  }
  renderTree();
  renderRows();
}
