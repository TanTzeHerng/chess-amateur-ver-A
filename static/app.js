"use strict";

// Chess Amateur frontend. Renders an interactive board (Unicode glyphs) and
// talks to the JSON API. Account-aware:
//   * Guests: stateless play, nothing saved.
//   * Logged-in users: server autosaves each move; ONE in-progress game at a
//     time (New Game is refused while one is live); auto-resume on load;
//     a Resign button; and a read-only Replay mode for finished games.

const CFG = window.CHESS_AMATEUR || { botName: "Chess Amateur", loggedIn: false, accountsEnabled: false, defaultThreads: 128 };
const BOT_NAME = CFG.botName;

const GLYPHS = {
  K: "\u2654", Q: "\u2655", R: "\u2656", B: "\u2657", N: "\u2658", P: "\u2659",
  k: "\u265A", q: "\u265B", r: "\u265C", b: "\u265D", n: "\u265E", p: "\u265F",
};

// --- DOM refs ---
const boardEl = document.getElementById("board");
const turnEl = document.getElementById("turnIndicator");
const messageEl = document.getElementById("message");
const moveLogEl = document.getElementById("moveLog");
const bannerEl = document.getElementById("banner");
const thinkingEl = document.getElementById("thinking");
const newGameBtn = document.getElementById("newGame");
const resignBtn = document.getElementById("resignBtn");
const promoOverlay = document.getElementById("promoOverlay");
const promoChoices = document.getElementById("promoChoices");
const threadsInput = document.getElementById("threads");
const threadsBadge = document.getElementById("threadsBadge");

// --- client state ---
let state = null;
let moves = [];
let humanColor = "white";
let threadsCount = CFG.defaultThreads || 128;
let startedAt = null;      // server-provided game start (echoed back on moves)
let inProgress = false;    // logged-in user has a live, saved game
let replayMode = false;    // read-only viewing of a finished game
let replayIndex = 0;
let replayMoves = [];
let selected = null;
let legalFrom = {};
let busy = false;

// -------------------------------------------------------------------------
// FEN + rendering (unchanged core)
// -------------------------------------------------------------------------
function parseFen(fen) {
  const pieces = {};
  const rows = fen.split(" ")[0].split("/");
  for (let r = 0; r < 8; r++) {
    const rank = 8 - r; let file = 0;
    for (const ch of rows[r]) {
      if (/\d/.test(ch)) file += parseInt(ch, 10);
      else { pieces[String.fromCharCode(97 + file) + rank] = ch; file += 1; }
    }
  }
  return pieces;
}
function squareName(f, r) { return String.fromCharCode(97 + f) + r; }
function isLightSquare(f, r) { return (f + r) % 2 === 0; }

function renderBoard() {
  boardEl.innerHTML = "";
  const pieces = state ? parseFen(state.fen) : {};
  const flipped = humanColor === "black";
  const files = [0,1,2,3,4,5,6,7], ranks = [8,7,6,5,4,3,2,1];
  const fileOrder = flipped ? [...files].reverse() : files;
  const rankOrder = flipped ? [...ranks].reverse() : ranks;
  const lastMove = state && state.last_bot_move ? state.last_bot_move.uci : null;
  const lastFrom = lastMove ? lastMove.slice(0,2) : null;
  const lastTo = lastMove ? lastMove.slice(2,4) : null;

  for (const rank of rankOrder) {
    for (const file of fileOrder) {
      const name = squareName(file, rank);
      const sq = document.createElement("div");
      sq.className = "square " + (isLightSquare(file, rank) ? "light" : "dark");
      sq.dataset.square = name;
      if (name === selected) sq.classList.add("selected");
      if (name === lastFrom || name === lastTo) sq.classList.add("last-move");
      if (selected && legalFrom[selected] && legalFrom[selected].includes(name)) {
        sq.classList.add("legal");
        if (pieces[name]) sq.classList.add("occupied");
      }
      if (file === fileOrder[0]) {
        const c = document.createElement("span"); c.className = "coord rank";
        c.textContent = rank; sq.appendChild(c);
      }
      if (rank === rankOrder[rankOrder.length - 1]) {
        const c = document.createElement("span"); c.className = "coord file";
        c.textContent = String.fromCharCode(97 + file); sq.appendChild(c);
      }
      const p = pieces[name];
      if (p) {
        const span = document.createElement("span");
        span.className = "piece " + (p === p.toUpperCase() ? "white" : "black");
        span.textContent = GLYPHS[p];
        sq.appendChild(span);
      }
      sq.addEventListener("click", () => onSquareClick(name));
      boardEl.appendChild(sq);
    }
  }
}

function rebuildLegalMap() {
  legalFrom = {};
  if (!state || !state.legal_moves) return;
  for (const uci of state.legal_moves) {
    (legalFrom[uci.slice(0,2)] = legalFrom[uci.slice(0,2)] || []).push(uci.slice(2,4));
  }
}

function renderMoveLog() {
  moveLogEl.innerHTML = "";
  const hist = state ? state.san_history : [];
  for (let i = 0; i < hist.length; i += 2) {
    const li = document.createElement("li");
    li.value = i/2 + 1;
    const w = document.createElement("span"); w.className = "white-move";
    w.textContent = hist[i] || ""; li.appendChild(w);
    if (hist[i+1] !== undefined) {
      const b = document.createElement("span"); b.className = "black-move";
      b.textContent = " " + hist[i+1]; li.appendChild(b);
    }
    moveLogEl.appendChild(li);
  }
  moveLogEl.scrollTop = moveLogEl.scrollHeight;
}

function renderTurn() {
  if (!state) { turnEl.textContent = ""; return; }
  if (replayMode) { turnEl.textContent = "Replay (" + replayIndex + "/" + replayMoves.length + ")"; return; }
  if (state.game_over) { turnEl.textContent = "Game over"; return; }
  turnEl.textContent = (state.turn === humanColor)
    ? "Your move (" + humanColor + ")" : BOT_NAME + " to move";
}

function renderBanner() {
  if (!state || !state.game_over) { bannerEl.hidden = true; bannerEl.className = "banner"; return; }
  bannerEl.hidden = false;
  const reason = state.result_reason || "game over";
  const result = state.result;
  let text, cls = "banner";
  if (result === "1/2-1/2") { text = "Draw by " + reason + "."; cls += " draw"; }
  else {
    const humanIsWhite = humanColor === "white";
    const humanWon = (result === "1-0" && humanIsWhite) || (result === "0-1" && !humanIsWhite);
    if (humanWon) { text = "You beat " + BOT_NAME + " by " + reason + "!"; cls += " win"; }
    else { text = BOT_NAME + " wins by " + reason + "."; cls += " loss"; }
  }
  bannerEl.textContent = text; bannerEl.className = cls;
}

function renderThreadsBadge() {
  if (!threadsBadge) return;
  threadsBadge.textContent = (state && typeof state.threads === "number")
    ? "\u00b7 " + state.threads + (state.threads === 1 ? " thread" : " threads") : "";
}

function renderResign() {
  if (!resignBtn) return;
  // Resign is available only for a logged-in user's live, unfinished game.
  resignBtn.hidden = !(inProgress && state && !state.game_over && !replayMode);
}

function renderAll() {
  rebuildLegalMap();
  renderBoard();
  renderMoveLog();
  renderTurn();
  renderBanner();
  renderThreadsBadge();
  renderResign();
}

// -------------------------------------------------------------------------
// Interaction
// -------------------------------------------------------------------------
function clearMessage() { messageEl.textContent = ""; }
function showMessage(m) { messageEl.textContent = m; }
function humansTurnNow() { return state && !state.game_over && !replayMode && state.turn === humanColor; }

function onSquareClick(name) {
  if (busy || !state || state.game_over || replayMode) return;
  if (!humansTurnNow()) return;
  const pieces = parseFen(state.fen);
  if (selected && legalFrom[selected] && legalFrom[selected].includes(name)) {
    attemptMove(selected, name, pieces[selected]); return;
  }
  if (legalFrom[name] && legalFrom[name].length > 0) {
    selected = name; clearMessage(); renderBoard(); return;
  }
  selected = null; renderBoard();
}

function needsPromotion(fromPiece, toSquare) {
  if (!fromPiece || fromPiece.toLowerCase() !== "p") return false;
  const r = parseInt(toSquare[1], 10);
  return r === 8 || r === 1;
}
function attemptMove(from, to, fromPiece) {
  if (needsPromotion(fromPiece, to)) {
    askPromotion((promo) => { if (!promo) { selected = null; renderBoard(); return; } sendMove(from + to + promo); });
  } else sendMove(from + to);
}
function askPromotion(cb) {
  promoChoices.innerHTML = "";
  const whiteSide = humanColor === "white";
  for (const opt of ["q","r","b","n"]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = GLYPHS[whiteSide ? opt.toUpperCase() : opt];
    btn.addEventListener("click", () => { promoOverlay.hidden = true; cb(opt); });
    promoChoices.appendChild(btn);
  }
  promoOverlay.hidden = false;
}

// -------------------------------------------------------------------------
// API
// -------------------------------------------------------------------------
function setBusy(on) {
  busy = on;
  thinkingEl.hidden = !on;
  if (newGameBtn) newGameBtn.disabled = on;
  if (resignBtn) resignBtn.disabled = on;
}

function adoptState(s) {
  state = s;
  if (Array.isArray(s.moves)) moves = s.moves;
  if (typeof s.threads === "number") threadsCount = s.threads;
  if (typeof s.human_color === "string") humanColor = s.human_color;
  if (typeof s.started_at === "string") startedAt = s.started_at;
  inProgress = !!s.in_progress;
}

async function sendMove(uci) {
  if (busy || !state) return;
  setBusy(true); selected = null; renderBoard(); clearMessage();
  try {
    const res = await fetch("/api/move", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        game_id: state.game_id, move: uci, moves: moves,
        human_color: humanColor, threads: threadsCount, started_at: startedAt,
      }),
    });
    if (res.status === 400) { showMessage("Illegal move. Try a different move."); return; }
    if (!res.ok) { showMessage("Server error (" + res.status + ")."); return; }
    adoptState(await res.json());
    renderAll();
  } catch (e) {
    showMessage("Network error. Please try again.");
  } finally { setBusy(false); }
}

function chosenThreads() {
  let n = parseInt(threadsInput && threadsInput.value, 10);
  if (!Number.isFinite(n)) n = CFG.defaultThreads || 128;
  n = Math.max(1, Math.min(128, n));
  if (threadsInput) threadsInput.value = String(n);
  return n;
}

async function newGame() {
  replayMode = false;
  const chosen = document.querySelector('input[name="color"]:checked');
  humanColor = chosen ? chosen.value : "white";
  threadsCount = chosenThreads();
  moves = []; startedAt = null;
  setBusy(true); selected = null; clearMessage(); bannerEl.hidden = true;
  try {
    const res = await fetch("/api/new", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ human_color: humanColor, threads: threadsCount }),
    });
    if (res.status === 409) {
      // Integrity rule: a logged-in user already has an in-progress game.
      const data = await res.json();
      showMessage((data && data.error) || "You have a game in progress.");
      // Load and resume that game instead of starting a new one.
      await resumeInProgress();
      return;
    }
    if (!res.ok) { showMessage("Could not start a new game (" + res.status + ")."); return; }
    adoptState(await res.json());
    renderAll();
  } catch (e) {
    showMessage("Network error starting game.");
  } finally { setBusy(false); }
}

async function resign() {
  if (busy || !inProgress) return;
  if (!window.confirm("Resign this game? It will be recorded as a loss.")) return;
  setBusy(true);
  try {
    const res = await fetch("/api/resign", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!res.ok) { showMessage("Could not resign (" + res.status + ")."); return; }
    // Reflect the finished game locally: show the loss banner.
    inProgress = false;
    if (state) {
      state.game_over = true;
      state.result = humanColor === "white" ? "0-1" : "1-0";
      state.result_reason = "resignation";
      state.legal_moves = [];
    }
    renderAll();
    showMessage("You resigned. See it in \u201CMy games\u201D.");
  } catch (e) {
    showMessage("Network error resigning.");
  } finally { setBusy(false); }
}

// Load the logged-in user's in-progress game (auto-resume).
async function resumeInProgress() {
  try {
    const res = await fetch("/api/in-progress");
    if (!res.ok) return false;
    const data = await res.json();
    if (!data.in_progress) return false;
    const g = data.in_progress;
    humanColor = g.human_color;
    moves = g.moves || [];
    startedAt = g.started_at;
    // Rebuild the board view by asking the server for state via a no-op:
    // we reconstruct locally by replaying is done server-side on next move;
    // to render now, fetch a fresh render by posting the history to /api/new
    // is NOT valid (409). Instead we derive the display via /api/move is also
    // not valid. So we render from a lightweight state fetch:
    await renderFromMoves(moves, humanColor, g.started_at, true);
    return true;
  } catch (e) { return false; }
}

// Render a position from a move list by asking the server to echo state.
// Uses a dedicated endpoint that rebuilds without mutating anything.
async function renderFromMoves(moveList, color, started, live) {
  const res = await fetch("/api/view", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ moves: moveList, human_color: color }),
  });
  if (!res.ok) return;
  const s = await res.json();
  adoptState(s);
  humanColor = color;
  moves = moveList;
  startedAt = started || s.started_at;
  inProgress = !!live;
  replayMode = false;
  renderAll();
}

// Replay a finished game read-only.
async function startReplay(gameId) {
  try {
    const res = await fetch("/api/games/" + encodeURIComponent(gameId));
    if (!res.ok) { showMessage("Could not load that game."); return; }
    const g = (await res.json()).game;
    replayMode = true;
    replayMoves = g.moves || [];
    humanColor = g.human_color;
    replayIndex = replayMoves.length;
    await renderFromMoves(replayMoves, humanColor, g.started_at, false);
    replayMode = true; // renderFromMoves resets it; re-set for view
    inProgress = false;
    renderAll();
    showMessage("Replaying a finished game (read-only).");
  } catch (e) { showMessage("Network error loading game."); }
}

// -------------------------------------------------------------------------
// Wire up + initial load
// -------------------------------------------------------------------------
if (newGameBtn) newGameBtn.addEventListener("click", newGame);
if (resignBtn) resignBtn.addEventListener("click", resign);

async function init() {
  // ?replay=<id> => show a finished game read-only.
  const params = new URLSearchParams(window.location.search);
  const replayId = params.get("replay");

  if (CFG.loggedIn && replayId) { await startReplay(replayId); return; }

  if (CFG.loggedIn) {
    // Auto-resume an in-progress game if one exists; else start fresh.
    const resumed = await resumeInProgress();
    if (resumed) { showMessage("Resumed your game in progress."); return; }
  }
  // Guests, or logged-in users with no active game: start a new game.
  await newGame();
}

init();
