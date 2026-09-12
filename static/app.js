"use strict";

// Chess Amateur frontend. Interactive board (Unicode glyphs), account-aware
// play, replay of finished games with transport controls, plus animation and
// synthesized-sound effects (see effects.js).

const CFG = window.CHESS_AMATEUR || { botName: "Chess Amateur", loggedIn: false, accountsEnabled: false, defaultThreads: 128 };
const BOT_NAME = CFG.botName;
// NOTE: effects.js declares top-level `const CASettings`/`const CASound` and also
// exposes them as window.CA_Settings/CA_Sound. Because both files load into the
// same global scope, we must NOT redeclare `CASettings`/`CASound` here (that throws
// "Identifier 'CASettings' has already been declared" and stops app.js). Use
// distinct local names bound to the globals instead.
const CASettings = window.CA_Settings;
const CASound = window.CA_Sound;

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
// settings + replay
const animToggle = document.getElementById("animToggle");
const soundToggle = document.getElementById("soundToggle");
const soundNote = document.getElementById("soundNote");
const replayBlock = document.getElementById("replayBlock");
const replayStatus = document.getElementById("replayStatus");
const rFirst = document.getElementById("replayFirst");
const rPrev = document.getElementById("replayPrev");
const rPlay = document.getElementById("replayPlay");
const rNext = document.getElementById("replayNext");
const rLast = document.getElementById("replayLast");

// --- client state ---
let state = null;
let moves = [];
let humanColor = "white";
let threadsCount = CFG.defaultThreads || 128;
let startedAt = null;
let inProgress = false;
let selected = null;
let legalFrom = {};
let busy = false;

// replay state
let replayMode = false;
let replayMoves = [];   // full UCI move list of the finished game
let replayIndex = 0;    // how many plies are shown (0..replayMoves.length)
let replayTimer = null;

const ANIM_MS = 280;    // must match .piece.sliding transition in CSS

// =========================================================================
// FEN + rendering
// =========================================================================
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

function orientation() {
  const flipped = humanColor === "black";
  const files = [0,1,2,3,4,5,6,7], ranks = [8,7,6,5,4,3,2,1];
  return {
    fileOrder: flipped ? [...files].reverse() : files,
    rankOrder: flipped ? [...ranks].reverse() : ranks,
  };
}

function renderBoard() {
  boardEl.innerHTML = "";
  const pieces = state ? parseFen(state.fen) : {};
  const { fileOrder, rankOrder } = orientation();
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
  if (!state || !state.legal_moves || replayMode) return;
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
  if (replayMode) { turnEl.textContent = "Replay mode"; return; }
  if (state.game_over) { turnEl.textContent = "Game over"; return; }
  turnEl.textContent = (state.turn === humanColor)
    ? "Your move (" + humanColor + ")" : BOT_NAME + " to move";
}

function bannerInfo(result, result_reason, color) {
  // returns {text, cls} for a game-over result relative to `color`.
  const reason = result_reason || "game over";
  if (result === "1/2-1/2") return { text: "Draw by " + reason + ".", cls: "banner draw", kind: "draw" };
  const humanIsWhite = color === "white";
  const humanWon = (result === "1-0" && humanIsWhite) || (result === "0-1" && !humanIsWhite);
  return humanWon
    ? { text: "You beat " + BOT_NAME + " by " + reason + "!", cls: "banner win", kind: "win" }
    : { text: BOT_NAME + " wins by " + reason + ".", cls: "banner loss", kind: "loss" };
}

function renderBanner() {
  if (replayMode || !state || !state.game_over) { bannerEl.hidden = true; bannerEl.className = "banner"; return; }
  bannerEl.hidden = false;
  const info = bannerInfo(state.result, state.result_reason, humanColor);
  bannerEl.textContent = info.text; bannerEl.className = info.cls;
}

function renderThreadsBadge() {
  if (!threadsBadge) return;
  threadsBadge.textContent = (state && typeof state.threads === "number")
    ? "\u00b7 " + state.threads + (state.threads === 1 ? " thread" : " threads") : "";
}
function renderResign() {
  if (!resignBtn) return;
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

// =========================================================================
// Piece-slide animation (toggle-controlled)
// =========================================================================
function centerOf(square) {
  const el = boardEl.querySelector('[data-square="' + square + '"]');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

// Animate the piece currently on `from` sliding to `to`, then run cb().
// If animations are off (or the squares can't be found), cb() runs immediately.
function animateSlide(from, to, cb) {
  if (!CASettings.animations) { cb(); return; }
  const fromSq = boardEl.querySelector('[data-square="' + from + '"]');
  const pieceEl = fromSq && fromSq.querySelector(".piece");
  const a = centerOf(from), b = centerOf(to);
  if (!pieceEl || !a || !b) { cb(); return; }
  const dx = b.x - a.x, dy = b.y - a.y;
  pieceEl.classList.add("sliding");
  // force reflow so the transition applies
  void pieceEl.offsetWidth;
  pieceEl.style.transform = "translate(" + dx + "px," + dy + "px)";
  let done = false;
  const finish = () => { if (done) return; done = true; cb(); };
  pieceEl.addEventListener("transitionend", finish, { once: true });
  setTimeout(finish, ANIM_MS + 80); // fallback if transitionend doesn't fire
}

// =========================================================================
// Interaction (live play)
// =========================================================================
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
    askPromotion((promo) => { if (!promo) { selected = null; renderBoard(); return; } sendMove(from + to + promo, from, to); });
  } else sendMove(from + to, from, to);
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

// =========================================================================
// API
// =========================================================================
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

// FEN "side to move" after applying our moves lets us detect check via the
// server state; simpler: infer check from SAN ('+' or '#') in san_history.
function lastSanIndicatesCheck(s) {
  const h = s && s.san_history;
  if (!h || !h.length) return false;
  const last = h[h.length - 1];
  return last.includes("+") || last.includes("#");
}

// Play the sound appropriate to the new state after a move resolves.
function playMoveSounds(prev, next) {
  if (!CASettings.sound) return;
  if (next.game_over) {
    // Move knock first (the move that ended it), then the end tune.
    const info = bannerInfo(next.result, next.result_reason, humanColor);
    if (lastSanIndicatesCheck(next)) CASound.check(); else CASound.move();
    setTimeout(() => {
      if (info.kind === "win") CASound.win();
      else if (info.kind === "draw") CASound.draw();
      else CASound.loss();
    }, 180);
  } else {
    if (lastSanIndicatesCheck(next)) CASound.check(); else CASound.move();
  }
}

async function sendMove(uci, fromSq, toSq) {
  if (busy || !state) return;
  setBusy(true); selected = null; clearMessage();
  // Animate the human's piece sliding first (if enabled), then post.
  const doPost = async () => {
    try {
      const res = await fetch("/api/move", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          game_id: state.game_id, move: uci, moves: moves,
          human_color: humanColor, threads: threadsCount, started_at: startedAt,
        }),
      });
      if (res.status === 400) { showMessage("Illegal move. Try a different move."); renderBoard(); return; }
      if (!res.ok) { showMessage("Server error (" + res.status + ")."); return; }
      const next = await res.json();
      const botUci = next.last_bot_move ? next.last_bot_move.uci : null;
      adoptState(next);
      if (CASettings.animations && botUci) {
        // Human piece already slid (before the POST). Render the post-human
        // position, knock for the human move, then slide the bot's piece and
        // play the bot's resolution sound (check knock / normal knock / end
        // tune) after its slide. Animations stagger the two knocks in time.
        renderAll();
        if (CASettings.sound) CASound.move();
        animateSlide(botUci.slice(0, 2), botUci.slice(2, 4), () => {
          renderAll();
          playBotResolutionSound(next);
        });
      } else {
        // No animation (or no bot reply): render final state and play a single
        // sound event for the resolved position.
        renderAll();
        playMoveSounds(null, next);
      }
    } catch (e) {
      showMessage("Network error. Please try again.");
    } finally { setBusy(false); }
  };
  if (CASettings.animations) animateSlide(fromSq, toSq, doPost);
  else doPost();
}

// CASound after the bot's move resolves (check knock or end tune).
function playBotResolutionSound(next) {
  if (!CASettings.sound) return;
  if (next.game_over) {
    const info = bannerInfo(next.result, next.result_reason, humanColor);
    if (lastSanIndicatesCheck(next)) CASound.check(); else CASound.move();
    setTimeout(() => {
      if (info.kind === "win") CASound.win();
      else if (info.kind === "draw") CASound.draw();
      else CASound.loss();
    }, 180);
  } else {
    if (lastSanIndicatesCheck(next)) CASound.check(); else CASound.move();
  }
}

function chosenThreads() {
  let n = parseInt(threadsInput && threadsInput.value, 10);
  if (!Number.isFinite(n)) n = CFG.defaultThreads || 128;
  n = Math.max(1, Math.min(128, n));
  if (threadsInput) threadsInput.value = String(n);
  return n;
}

async function newGame() {
  exitReplay();
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
      const data = await res.json();
      showMessage((data && data.error) || "You have a game in progress.");
      await resumeInProgress();
      return;
    }
    if (!res.ok) { showMessage("Could not start a new game (" + res.status + ")."); return; }
    adoptState(await res.json());
    renderAll();
    // If bot (white) opened, knock for it.
    if (state.last_bot_move && CASettings.sound) CASound.move();
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
    inProgress = false;
    if (state) {
      state.game_over = true;
      state.result = humanColor === "white" ? "0-1" : "1-0";
      state.result_reason = "resignation";
      state.legal_moves = [];
    }
    renderAll();
    if (CASettings.sound) CASound.loss();
    showMessage("You resigned. See it in \u201CMy games\u201D.");
  } catch (e) {
    showMessage("Network error resigning.");
  } finally { setBusy(false); }
}

async function resumeInProgress() {
  try {
    const res = await fetch("/api/in-progress");
    if (!res.ok) return false;
    const data = await res.json();
    if (!data.in_progress) return false;
    const g = data.in_progress;
    await renderFromMoves(g.moves || [], g.human_color, g.started_at, true);
    return true;
  } catch (e) { return false; }
}

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

// =========================================================================
// Replay of finished games (read-only) with transport controls
// =========================================================================
function exitReplay() {
  replayMode = false;
  stopAutoplay();
  if (replayBlock) replayBlock.classList.remove("active");
}

async function startReplay(gameId) {
  try {
    const res = await fetch("/api/games/" + encodeURIComponent(gameId));
    if (!res.ok) { showMessage("Could not load that game."); return; }
    const g = (await res.json()).game;
    replayMode = true;
    replayMoves = g.moves || [];
    humanColor = g.human_color;
    replayIndex = replayMoves.length;   // start at final position
    replayBlock.classList.add("active");
    resignBtn.hidden = true;
    await renderReplayPosition();
    showMessage("Replaying a finished game (read-only).");
  } catch (e) { showMessage("Network error loading game."); }
}

async function renderReplayPosition() {
  // Render the position after the first `replayIndex` plies.
  const partial = replayMoves.slice(0, replayIndex);
  const res = await fetch("/api/view", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ moves: partial, human_color: humanColor }),
  });
  if (!res.ok) return;
  state = await res.json();
  replayMode = true;
  renderAll();
  updateReplayControls();
}

function updateReplayControls() {
  const atStart = replayIndex <= 0;
  const atEnd = replayIndex >= replayMoves.length;
  rFirst.disabled = atStart; rPrev.disabled = atStart;
  rNext.disabled = atEnd; rLast.disabled = atEnd;
  replayStatus.textContent = "Move " + replayIndex + " / " + replayMoves.length;
  rPlay.textContent = replayTimer ? "\u23F8" : "\u23EF"; // pause vs play glyph
}

function replayGoto(i) {
  replayIndex = Math.max(0, Math.min(replayMoves.length, i));
  renderReplayPosition();
}
function replayStep(delta) { stopAutoplay(); replayGoto(replayIndex + delta); }

function startAutoplay() {
  if (replayTimer) return;
  if (replayIndex >= replayMoves.length) replayIndex = 0; // restart if at end
  replayTimer = setInterval(() => {
    if (replayIndex >= replayMoves.length) { stopAutoplay(); updateReplayControls(); return; }
    replayIndex += 1;
    renderReplayPosition();
  }, 1000); // 1 move per second
  updateReplayControls();
}
function stopAutoplay() {
  if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
}
function toggleAutoplay() { replayTimer ? stopAutoplay() : startAutoplay(); updateReplayControls(); }

// =========================================================================
// CASettings toggles (with the sound-needs-animations dependency)
// =========================================================================
function syncToggleUI() {
  animToggle.checked = CASettings.animations;
  soundToggle.checked = CASettings.sound;
  // CASound toggle is disabled + greyed when animations are off.
  soundToggle.disabled = !CASettings.animations;
  soundNote.hidden = CASettings.animations;
}
function wireSettings() {
  syncToggleUI();
  animToggle.addEventListener("change", () => {
    CASettings.setAnimations(animToggle.checked);
    syncToggleUI();
  });
  soundToggle.addEventListener("change", () => {
    CASettings.setSound(soundToggle.checked);
    syncToggleUI();
    // A tiny knock confirms sound is on (and satisfies the user-gesture
    // requirement to unlock audio).
    if (CASettings.sound) CASound.move();
  });
}

// =========================================================================
// Wire up + initial load
// =========================================================================
if (newGameBtn) newGameBtn.addEventListener("click", newGame);
if (resignBtn) resignBtn.addEventListener("click", resign);
if (rFirst) rFirst.addEventListener("click", () => replayStep(-replayMoves.length));
if (rPrev) rPrev.addEventListener("click", () => replayStep(-1));
if (rPlay) rPlay.addEventListener("click", toggleAutoplay);
if (rNext) rNext.addEventListener("click", () => replayStep(1));
if (rLast) rLast.addEventListener("click", () => replayStep(replayMoves.length));

async function init() {
  wireSettings();
  const params = new URLSearchParams(window.location.search);
  const replayId = params.get("replay");
  if (CFG.loggedIn && replayId) { await startReplay(replayId); return; }
  if (CFG.loggedIn) {
    const resumed = await resumeInProgress();
    if (resumed) { showMessage("Resumed your game in progress."); return; }
  }
  await newGame();
}
init();
