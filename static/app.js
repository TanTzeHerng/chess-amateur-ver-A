"use strict";

// Chess Amateur frontend. Interactive board (Unicode glyphs), account-aware
// play, in-game review + finished-game review with transport controls, plus
// animation and synthesized-sound effects (see effects.js).

const CFG = window.CHESS_AMATEUR || { botName: "Chess Amateur", loggedIn: false, accountsEnabled: false, defaultThreads: 128, username: "Guest" };
const BOT_NAME = CFG.botName;
const HUMAN_NAME = CFG.username || "Guest";
// NOTE: effects.js declares top-level `const Settings`/`const Sound` and also
// exposes them as window.CA_Settings/CA_Sound. Because both files load into the
// same global scope, we must NOT redeclare `Settings`/`Sound`/`CASettings`/
// `CASound` here (that throws "Identifier has already been declared" and stops
// app.js). Use distinct local names bound to the globals instead.
const CASettings = window.CA_Settings;
const CASound = window.CA_Sound;

const GLYPHS = {
  K: "\u2654", Q: "\u2655", R: "\u2656", B: "\u2657", N: "\u2658", P: "\u2659",
  k: "\u265A", q: "\u265B", r: "\u265C", b: "\u265D", n: "\u265E", p: "\u265F",
};

// --- DOM refs ---
const boardEl = document.getElementById("board");
const moveLogEl = document.getElementById("moveLog");
const thinkingEl = document.getElementById("thinking");
const newGameBtn = document.getElementById("newGame");
const resumeBtn = document.getElementById("resumeBtn");
const resignBtn = document.getElementById("resignBtn");
const promoOverlay = document.getElementById("promoOverlay");
const promoChoices = document.getElementById("promoChoices");
const threadsInput = document.getElementById("threads");
// names / clocks / ratings around the board
const clockTopTime = document.getElementById("clockTopTime");
const clockBottomTime = document.getElementById("clockBottomTime");
const ratingTop = document.getElementById("ratingTop");
const ratingBottom = document.getElementById("ratingBottom");
const humanNameEl = document.getElementById("humanName");
const speechBubble = document.getElementById("speechBubble");
// new-game popout
const newGamePopout = document.getElementById("newGamePopout");
const startGameBtn = document.getElementById("startGame");
const cancelNewGameBtn = document.getElementById("cancelNewGame");
const tcInputs = document.getElementById("tcInputs");
// custom-position (casual only) + board editor
const customChoice = document.getElementById("customChoice");
const customEnable = document.getElementById("customEnable");
const customBody = document.getElementById("customBody");
const customFen = document.getElementById("customFen");
const customFenError = document.getElementById("customFenError");
const openEditorBtn = document.getElementById("openEditor");
const editorOverlay = document.getElementById("editorOverlay");
const editorBoardEl = document.getElementById("editorBoard");
const editorPaletteEl = document.getElementById("editorPalette");
const editorError = document.getElementById("editorError");
const editorEp = document.getElementById("editorEp");
const editorHalfmove = document.getElementById("editorHalfmove");
const editorUseBtn = document.getElementById("editorUse");
const editorClearBtn = document.getElementById("editorClear");
const editorCancelBtn = document.getElementById("editorCancel");
// self-analysis
const selfAnalysisRow = document.getElementById("selfAnalysisRow");
const selfAnalysisToggle = document.getElementById("selfAnalysisToggle");
// settings / profile disclosures
const settingsToggle = document.getElementById("settingsToggle");
const settingsBody = document.getElementById("settingsBody");
const advancedToggle = document.getElementById("advancedToggle");
const advancedBody = document.getElementById("advancedBody");
const profileToggle = document.getElementById("profileToggle");
const profileBody = document.getElementById("profileBody");
const logoutBtn = document.getElementById("logoutBtn");
const deleteAccountBtn = document.getElementById("deleteAccountBtn");
const deleteAccountOverlay = document.getElementById("deleteAccountOverlay");
const deleteAccountConfirm = document.getElementById("deleteAccountConfirm");
const deleteAccountCancel = document.getElementById("deleteAccountCancel");
// dashboard (FEAT-010): rating line graphs over a customizable period
const dashboardBtn = document.getElementById("dashboardBtn");
const dashboardOverlay = document.getElementById("dashboardOverlay");
const dashboardClose = document.getElementById("dashboardClose");
const dashboardPeriod = document.getElementById("dashboardPeriod");
const dashboardCustomRange = document.getElementById("dashboardCustomRange");
const dashboardFrom = document.getElementById("dashboardFrom");
const dashboardTo = document.getElementById("dashboardTo");
const dashboardApply = document.getElementById("dashboardApply");
const dashboardCharts = document.getElementById("dashboardCharts");
const dashboardEmpty = document.getElementById("dashboardEmpty");
// settings toggles
const animToggle = document.getElementById("animToggle");
const soundToggle = document.getElementById("soundToggle");
const soundNote = document.getElementById("soundNote");
// network error popout
const netErrorOverlay = document.getElementById("netErrorOverlay");
const netErrorText = document.getElementById("netErrorText");
const netErrorClose = document.getElementById("netErrorClose");
// replay/review controls
const replayBlock = document.getElementById("replayBlock");
const replayStatus = document.getElementById("replayStatus");
const rFirst = document.getElementById("replayFirst");
const rPrev = document.getElementById("replayPrev");
const rPlay = document.getElementById("replayPlay");
const rNext = document.getElementById("replayNext");
const rLast = document.getElementById("replayLast");
// review Info disclosure (start/end time of the reviewed game)
const reviewInfoToggle = document.getElementById("reviewInfoToggle");
const reviewInfoBody = document.getElementById("reviewInfoBody");
const reviewStartEl = document.getElementById("reviewStart");
const reviewEndEl = document.getElementById("reviewEnd");

// --- client state ---
let state = null;
let moves = [];
let startFen = null;   // custom starting position (null => standard start)
let humanColor = "white";
let threadsCount = CFG.defaultThreads || 128;
let startedAt = null;
let inProgress = false;
// clock / mode / time-control state
let mode = "casual";
let baseSeconds = null;    // null => unlimited (no clock)
let increment = 0;
let clockState = null;     // {white, black} remaining seconds, or null (unlimited)
let clockTicker = null;    // setInterval handle for the display countdown
let humanClockRunning = false;   // fairness: true only from CA-animation-end until the human moves
let humanTurnStart = 0;    // performance.now() when the human's clock resumed
let humanClockAtResume = 0;      // human's remaining seconds at the moment his clock resumed
let selected = null;
let legalFrom = {};
let busy = false;
let playerRating = null;   // display ratings (null => not shown)
let botRating = null;

// in-game review state: when viewing an earlier ply of the LIVE game the
// player may look but not move. reviewIndex === null means "on the last move".
let reviewIndex = null;

// self-analysis (casual only): a scratch layer over the real game.
let selfAnalysis = false;
let realState = null;      // preserved true game state while in self-analysis
let realMoves = [];        // preserved true move list while in self-analysis
let scratchMoves = [];     // moves array used only during self-analysis

// finished-game replay state
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

// True when board interaction should be blocked (viewing an earlier ply of the
// live game). Distinct from finished-game replay.
function inReview() { return reviewIndex !== null; }

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

// Render the move log. Each move span is clickable to jump the board to that
// ply (in-game review). After the last move, when the game is over, append the
// canonical result line ('1-0 (White won by checkmate)').
function renderMoveLog() {
  moveLogEl.innerHTML = "";
  // In finished-game replay, show the full game's move log/result (replayState);
  // in normal/in-game-review play, show the current state's history.
  const src = (replayMode && replayState) ? replayState.san_history : (state && state.san_history);
  const h = src || [];
  for (let i = 0; i < h.length; i += 2) {
    const li = document.createElement("li");
    li.value = i/2 + 1;
    const w = document.createElement("span");
    w.className = "white-move move-link";
    w.textContent = h[i] || "";
    w.addEventListener("click", () => jumpToPly(i + 1));
    li.appendChild(w);
    if (h[i+1] !== undefined) {
      const b = document.createElement("span");
      b.className = "black-move move-link";
      b.textContent = " " + h[i+1];
      b.addEventListener("click", () => jumpToPly(i + 2));
      li.appendChild(b);
    }
    moveLogEl.appendChild(li);
  }
  // Result line after the last move (from FEAT-002 result_line).
  const rl = replayMode ? (replayState && replayState.result_line)
                        : (state && state.game_over ? state.result_line : null);
  if (rl) {
    const li = document.createElement("li");
    li.className = "result-line";
    li.value = "";
    li.style.listStyle = "none";
    li.textContent = rl;
    moveLogEl.appendChild(li);
  }
  moveLogEl.scrollTop = moveLogEl.scrollHeight;
}

function renderThinking() {
  if (thinkingEl) thinkingEl.hidden = !busy;
}

function renderControls() {
  // New Game shows when no game is active (load, after game over) and not
  // reviewing a finished game. It is HIDDEN while a live game is in progress.
  const liveGame = inProgress && state && !state.game_over && !replayMode;
  const hasUnresumed = !!(pendingResume && !liveGame && !replayMode);
  if (newGameBtn) newGameBtn.hidden = liveGame || replayMode || hasUnresumed;
  if (resumeBtn) resumeBtn.hidden = !hasUnresumed;
  if (resignBtn) resignBtn.hidden = !liveGame;
  // Self-analysis: casual mode only, during a live game. Keep the row visible
  // while self-analysis is active so the player can always turn it back OFF.
  if (selfAnalysisRow) {
    selfAnalysisRow.hidden = !(mode === "casual" && (liveGame || selfAnalysis));
  }
}

// -------------------------------------------------------------------------
// Clocks + fairness rule
// -------------------------------------------------------------------------
function fmtClock(sec) {
  if (sec == null) return "--:--";
  sec = Math.max(0, sec);
  // Deciseconds precision ONLY when below 20 seconds, e.g. '19.4' / '0:08.7'.
  if (sec < 20) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    if (m > 0) {
      // pad the seconds to two integer digits, keeping one decimal.
      const sStr = (s < 10 ? "0" : "") + s.toFixed(1);
      return m + ":" + sStr;
    }
    return s.toFixed(1);
  }
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return h + ":" + String(m % 60).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }
  return m + ":" + String(s).padStart(2, "0");
}

// Live remaining for the human, accounting for time elapsed since his clock
// resumed (only while humanClockRunning). Bot clock is whatever the server set.
function liveHumanRemaining() {
  if (!clockState) return null;
  let rem = clockState[humanColor];
  if (humanClockRunning) {
    rem = humanClockAtResume - (performance.now() - humanTurnStart) / 1000;
  }
  return rem;
}

function renderClocks() {
  if (!clockTopTime || !clockBottomTime) return;
  if (!clockState || replayMode) {
    clockTopTime.hidden = true;
    clockBottomTime.hidden = true;
    return;
  }
  clockTopTime.hidden = false;
  clockBottomTime.hidden = false;
  const botColor = humanColor === "white" ? "black" : "white";
  // Top = Chess Amateur, bottom = human.
  const humanRem = liveHumanRemaining();
  clockBottomTime.textContent = fmtClock(humanRem);
  clockTopTime.textContent = fmtClock(clockState[botColor]);
  // active/low styling
  clockBottomTime.className = "clock-time" + (humanClockRunning ? " active" : "") + (humanRem != null && humanRem < 10 ? " low" : "");
  clockTopTime.className = "clock-time" + (!humanClockRunning && state && !state.game_over ? " active" : "") + (clockState[botColor] != null && clockState[botColor] < 10 ? " low" : "");
}

function startClockTicker() {
  stopClockTicker();
  if (!clockState) return;
  clockTicker = setInterval(() => {
    renderClocks();
    const rem = liveHumanRemaining();
    if (humanClockRunning && rem != null && rem <= 0) {
      humanClockRunning = false;
      submitTimeout();
    }
  }, 100);
}
function stopClockTicker() {
  if (clockTicker) { clearInterval(clockTicker); clockTicker = null; }
}

// The human's clock RESUMES (starts counting) only after Chess Amateur's move
// animation has ended and it is the human's turn (fairness rule).
function resumeHumanClock() {
  if (!clockState || !state || state.game_over || replayMode) return;
  if (inReview() || selfAnalysis) return;
  if (state.turn !== humanColor) return;
  humanClockAtResume = clockState[humanColor];
  humanTurnStart = performance.now();
  humanClockRunning = true;
  renderClocks();
}

// Stop the human clock the instant he moves; returns the elapsed seconds to
// send to the server (which is authoritative + clamps/flags).
function stopHumanClockAndGetElapsed() {
  if (!humanClockRunning) return 0;
  const elapsed = (performance.now() - humanTurnStart) / 1000;
  humanClockRunning = false;
  if (clockState) clockState[humanColor] = Math.max(0, humanClockAtResume - elapsed);
  renderClocks();
  return elapsed;
}

async function submitTimeout() {
  if (busy || !state || !clockState) return;
  setBusy(true);
  try {
    const res = await fetch("/api/move", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        game_id: state.game_id, move: "0000", moves: moves,
        human_color: humanColor, threads: threadsCount, mode: mode,
        base_seconds: baseSeconds, increment: increment,
        clock: clockState, elapsed: 1e9, started_at: startedAt,
        start_fen: startFen,
      }),
    });
    if (res.ok) { adoptState(await res.json()); renderAll(); afterMoveResolved(); }
  } catch (e) { /* ignore */ }
  finally { setBusy(false); }
}

// -------------------------------------------------------------------------
// Ratings around the names
// -------------------------------------------------------------------------
function fmtRating(r) {
  if (r == null) return "";
  const n = Number(r);
  if (!isFinite(n)) return "";
  return String(Math.round(n));
}
function renderRatings() {
  if (!ratingTop || !ratingBottom) return;
  // No ratings in casual mode / for guests (null values).
  if (botRating == null || replayMode) { ratingTop.hidden = true; }
  else { ratingTop.hidden = false; ratingTop.textContent = "(" + fmtRating(botRating) + ")"; }
  if (playerRating == null || replayMode) { ratingBottom.hidden = true; }
  else { ratingBottom.hidden = false; ratingBottom.textContent = "(" + fmtRating(playerRating) + ")"; }
}

function renderNames() {
  if (humanNameEl) humanNameEl.textContent = HUMAN_NAME;
}

function renderAll() {
  rebuildLegalMap();
  renderBoard();
  renderMoveLog();
  renderThinking();
  renderControls();
  renderClocks();
  renderRatings();
  renderNames();
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

function animateSlide(from, to, cb) {
  if (!CASettings.animations) { cb(); return; }
  const fromSq = boardEl.querySelector('[data-square="' + from + '"]');
  const pieceEl = fromSq && fromSq.querySelector(".piece");
  const a = centerOf(from), b = centerOf(to);
  if (!pieceEl || !a || !b) { cb(); return; }
  const dx = b.x - a.x, dy = b.y - a.y;
  pieceEl.classList.add("sliding");
  void pieceEl.offsetWidth;
  pieceEl.style.transform = "translate(" + dx + "px," + dy + "px)";
  let done = false;
  const finish = () => { if (done) return; done = true; cb(); };
  pieceEl.addEventListener("transitionend", finish, { once: true });
  setTimeout(finish, ANIM_MS + 80);
}

// =========================================================================
// Interaction (live play)
// =========================================================================
function humansTurnNow() {
  if (!state || state.game_over || replayMode || inReview()) return false;
  if (selfAnalysis) return true;   // human moves for both sides
  return state.turn === humanColor;
}

function onSquareClick(name) {
  if (busy || !state || state.game_over || replayMode) return;
  // In-game review: viewing an earlier ply -> look only, no moves.
  if (inReview()) return;
  if (!humansTurnNow()) return;
  const pieces = parseFen(state.fen);
  if (selected && legalFrom[selected] && legalFrom[selected].includes(name)) {
    attemptMove(selected, name, pieces[selected]); return;
  }
  if (legalFrom[name] && legalFrom[name].length > 0) {
    selected = name; renderBoard(); return;
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
    askPromotion((promo) => { if (!promo) { selected = null; renderBoard(); return; } commitMove(from + to + promo, from, to); });
  } else commitMove(from + to, from, to);
}
function commitMove(uci, fromSq, toSq) {
  if (selfAnalysis) { selfAnalysisMove(uci, fromSq, toSq); return; }
  sendMove(uci, fromSq, toSq);
}
function askPromotion(cb) {
  promoChoices.innerHTML = "";
  const whiteSide = selfAnalysis ? (state.turn === "white") : (humanColor === "white");
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
  if (thinkingEl) thinkingEl.hidden = !on;
  if (newGameBtn) newGameBtn.disabled = on;
  if (resignBtn) resignBtn.disabled = on;
  if (resumeBtn) resumeBtn.disabled = on;
}
function adoptState(s) {
  state = s;
  if (Array.isArray(s.moves)) moves = s.moves;
  if ("start_fen" in s) startFen = s.start_fen || null;
  if (typeof s.threads === "number") threadsCount = s.threads;
  if (typeof s.human_color === "string") humanColor = s.human_color;
  if (typeof s.started_at === "string") startedAt = s.started_at;
  inProgress = !!s.in_progress;
  if (typeof s.mode === "string") mode = s.mode;
  if ("base_seconds" in s) baseSeconds = s.base_seconds;
  if ("increment" in s) increment = s.increment || 0;
  if ("clock" in s) clockState = s.clock ? { white: s.clock.white, black: s.clock.black } : null;
  if ("player_rating" in s) playerRating = s.player_rating;
  if ("bot_rating" in s) botRating = s.bot_rating;
}

function lastSanIndicatesCheck(s) {
  const h = s && s.san_history;
  if (!h || !h.length) return false;
  const last = h[h.length - 1];
  return last.includes("+") || last.includes("#");
}

// kind of result relative to the human, used for sound selection.
function resultKind(result) {
  if (result === "1/2-1/2") return "draw";
  const humanIsWhite = humanColor === "white";
  const humanWon = (result === "1-0" && humanIsWhite) || (result === "0-1" && !humanIsWhite);
  return humanWon ? "win" : "loss";
}

async function sendMove(uci, fromSq, toSq) {
  if (busy || !state) return;
  const elapsed = stopHumanClockAndGetElapsed();
  const clockSnapshot = clockState ? { white: clockState.white, black: clockState.black } : null;
  setBusy(true); selected = null;
  const doPost = async () => {
    try {
      const res = await fetch("/api/move", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          game_id: state.game_id, move: uci, moves: moves,
          human_color: humanColor, threads: threadsCount, started_at: startedAt,
          mode: mode, base_seconds: baseSeconds, increment: increment,
          clock: clockSnapshot, elapsed: elapsed, start_fen: startFen,
        }),
      });
      // Illegal move: silently disallow, just re-render (no message/hint).
      if (res.status === 400) { renderBoard(); setBusy(false); return; }
      if (!res.ok) { showNetworkError("Server error (" + res.status + ")."); setBusy(false); return; }
      const next = await res.json();
      const botUci = next.last_bot_move ? next.last_bot_move.uci : null;

      playHumanMoveSound(next, botUci);

      const botThink = (typeof next.bot_think === "number") ? next.bot_think : 0;

      const revealBot = () => {
        if (!botUci) {
          adoptState(next);
          renderAll();
          afterMoveResolved();
          setBusy(false);
          return;
        }
        if (CASettings.animations) {
          revealBotAnimated(next, botUci);
        } else {
          adoptState(next);
          renderAll();
          playBotResolutionSound(next);
          afterMoveResolved();
          setBusy(false);
        }
      };

      if (botUci && botThink > 0) {
        holdForBotThink(next, botThink, revealBot);
      } else {
        revealBot();
      }
    } catch (e) {
      showNetworkError("A network error occurred. Please try again.");
      setBusy(false);
    }
  };
  if (CASettings.animations) animateSlide(fromSq, toSq, doPost);
  else doPost();
}

function playHumanMoveSound(next, botUci) {
  if (!CASettings.sound) return;
  const h = next.san_history || [];
  const humanSan = botUci ? h[h.length - 2] : h[h.length - 1];
  const humanGaveCheckOrMate = humanSan && (humanSan.includes("+") || humanSan.includes("#"));
  if (next.game_over && !botUci) {
    const kind = resultKind(next.result);
    if (humanGaveCheckOrMate) CASound.check(); else CASound.move();
    setTimeout(() => {
      if (kind === "win") CASound.win();
      else if (kind === "draw") CASound.draw();
      else CASound.loss();
    }, 180);
  } else {
    if (humanGaveCheckOrMate) CASound.check(); else CASound.move();
  }
}

function holdForBotThink(next, seconds, done) {
  const botColor = humanColor === "white" ? "black" : "white";
  const startClock = (next.clock && next.clock[botColor] != null)
    ? next.clock[botColor] + seconds
    : null;
  const t0 = performance.now();
  if (thinkingEl) thinkingEl.hidden = false;
  const tick = setInterval(() => {
    const elapsed = (performance.now() - t0) / 1000;
    if (clockState && startClock != null) {
      clockState[botColor] = Math.max(next.clock[botColor], startClock - elapsed);
      renderClocks();
    }
    if (elapsed >= seconds) {
      clearInterval(tick);
      if (thinkingEl) thinkingEl.hidden = true;
      done();
    }
  }, 100);
}

// Reveal the bot's move with the slide animation (intermediate position first).
function revealBotAnimated(next, botUci) {
  const interMoves = next.moves.slice(0, next.moves.length - 1);
  const finish = (interFen) => {
    if (interFen) {
      state = Object.assign({}, next, { fen: interFen, last_bot_move: null });
      renderBoard();
      animateSlide(botUci.slice(0, 2), botUci.slice(2, 4), () => {
        adoptState(next);
        renderAll();
        playBotResolutionSound(next);
        afterMoveResolved();
        setBusy(false);
      });
    } else {
      adoptState(next);
      renderAll();
      playBotResolutionSound(next);
      afterMoveResolved();
      setBusy(false);
    }
  };
  fetch("/api/view", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ moves: interMoves, human_color: humanColor,
                           start_fen: startFen }),
  }).then(r => r.ok ? r.json() : null)
    .then(d => finish(d ? d.fen : null))
    .catch(() => finish(null));
}

function playBotResolutionSound(next) {
  if (!CASettings.sound) return;
  if (next.game_over) {
    const kind = resultKind(next.result);
    if (lastSanIndicatesCheck(next)) CASound.check(); else CASound.move();
    setTimeout(() => {
      if (kind === "win") CASound.win();
      else if (kind === "draw") CASound.draw();
      else CASound.loss();
    }, 180);
  } else {
    if (lastSanIndicatesCheck(next)) CASound.check(); else CASound.move();
  }
}

// Called once a move (and Chess Amateur's animated reply) has fully resolved.
function afterMoveResolved() {
  if (state && state.game_over) {
    stopClockTicker();
    humanClockRunning = false;
    showRatingDelta(state.rating_delta);
    renderControls();
    return;
  }
  if (selfAnalysis) return;   // no clock during self-analysis
  if (clockState) { resumeHumanClock(); startClockTicker(); }
}

// On game end, update the displayed player rating (the server returns the new
// player_rating) and show the signed delta ('+0' when zero) next to the name.
function showRatingDelta(delta) {
  renderRatings();
  if (!delta) return;   // casual / guest -> nothing to show
  if (ratingBottom && playerRating != null) {
    ratingBottom.hidden = false;
    ratingBottom.textContent = "(" + fmtRating(playerRating) + " " + delta + ")";
  }
}

function chosenThreads() {
  let n = parseInt(threadsInput && threadsInput.value, 10);
  if (!Number.isFinite(n)) n = CFG.defaultThreads || 128;
  n = Math.max(1, Math.min(128, n));
  if (threadsInput) threadsInput.value = String(n);
  return n;
}

function chosenMode() {
  const el = document.querySelector('input[name="mode"]:checked');
  return el ? el.value : "casual";
}
function chosenTimeControl() {
  const tcEl = document.querySelector('input[name="tc"]:checked');
  if (tcEl && tcEl.value === "unlimited") return { unlimited: true };
  const h = parseInt((document.getElementById("tcH") || {}).value, 10) || 0;
  const m = parseInt((document.getElementById("tcM") || {}).value, 10) || 0;
  const s = parseInt((document.getElementById("tcS") || {}).value, 10) || 0;
  const inc = parseInt((document.getElementById("tcInc") || {}).value, 10) || 0;
  return { unlimited: false, hours: h, minutes: m, seconds: s, increment: inc };
}

// =========================================================================
// New Game popout
// =========================================================================
function openNewGamePopout() {
  hideSpeechBubble();
  if (newGamePopout) newGamePopout.hidden = false;
  syncTcInputs();
  syncCustomVisibility();
}
function closeNewGamePopout() {
  if (newGamePopout) newGamePopout.hidden = true;
}
function syncTcInputs() {
  const tcEl = document.querySelector('input[name="tc"]:checked');
  const unlimited = tcEl && tcEl.value === "unlimited";
  if (tcInputs) tcInputs.hidden = !!unlimited;
}

// =========================================================================
// Custom position (Casual mode only) + board editor
// =========================================================================
// The editor's working board: a { square: pieceChar } map, editable by the
// user. Placing/erasing pieces, side to move, per-side K/Q castling rights, the
// en-passant target square and the halfmove clock all feed buildEditorFen().
let editorPieces = {};
let editorSelected = "P";   // currently-selected palette piece ("" => erase)

// Show the custom-position fieldset ONLY in casual mode (per the spec). When
// leaving casual, force the custom option off so a rated/FIDE game never
// carries a custom FEN.
function syncCustomVisibility() {
  const casual = chosenMode() === "casual";
  if (customChoice) customChoice.hidden = !casual;
  if (!casual && customEnable) {
    customEnable.checked = false;
    if (customBody) customBody.hidden = true;
  }
}

// The custom FEN to start from, or null when the option is off / empty. Casual
// only; returns null otherwise so rated/FIDE never send a FEN.
function chosenCustomFen() {
  if (chosenMode() !== "casual") return null;
  if (!customEnable || !customEnable.checked) return null;
  const raw = (customFen && customFen.value ? customFen.value : "").trim();
  return raw || null;
}

// The standard 8x8 order for building/reading a FEN piece-placement field.
function buildEditorFen() {
  const turnEl = document.querySelector('input[name="editorTurn"]:checked');
  const turn = turnEl ? turnEl.value : "w";
  let placement = "";
  for (let rank = 8; rank >= 1; rank--) {
    let empty = 0;
    for (let file = 0; file < 8; file++) {
      const sq = String.fromCharCode(97 + file) + rank;
      const pc = editorPieces[sq];
      if (pc) {
        if (empty) { placement += empty; empty = 0; }
        placement += pc;
      } else { empty += 1; }
    }
    if (empty) placement += empty;
    if (rank > 1) placement += "/";
  }
  let castling = "";
  if (document.getElementById("castWK").checked) castling += "K";
  if (document.getElementById("castWQ").checked) castling += "Q";
  if (document.getElementById("castBK").checked) castling += "k";
  if (document.getElementById("castBQ").checked) castling += "q";
  if (!castling) castling = "-";
  let ep = (editorEp && editorEp.value ? editorEp.value : "-").trim() || "-";
  let half = parseInt((editorHalfmove || {}).value, 10);
  if (!isFinite(half) || half < 0) half = 0;
  // Fullmove number is not user-facing here; a valid FEN needs one (>=1).
  return placement + " " + turn + " " + castling + " " + ep + " " + half + " 1";
}

function renderEditorBoard() {
  if (!editorBoardEl) return;
  editorBoardEl.innerHTML = "";
  // Always White's perspective in the editor (rank 8 at top).
  for (let rank = 8; rank >= 1; rank--) {
    for (let file = 0; file < 8; file++) {
      const sq = String.fromCharCode(97 + file) + rank;
      const cell = document.createElement("div");
      cell.className = "editor-sq " + (isLightSquare(file, rank) ? "light" : "dark");
      cell.setAttribute("data-square", sq);
      const pc = editorPieces[sq];
      if (pc) {
        const span = document.createElement("span");
        span.className = "piece " + (pc === pc.toUpperCase() ? "white" : "black");
        span.textContent = GLYPHS[pc];
        cell.appendChild(span);
      }
      cell.addEventListener("click", () => {
        if (editorSelected === "") delete editorPieces[sq];
        else editorPieces[sq] = editorSelected;
        renderEditorBoard();
      });
      editorBoardEl.appendChild(cell);
    }
  }
}

function renderEditorPalette() {
  if (!editorPaletteEl) return;
  editorPaletteEl.innerHTML = "";
  const order = ["K", "Q", "R", "B", "N", "P",
                 "k", "q", "r", "b", "n", "p", ""];
  order.forEach((pc) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "editor-palette-btn";
    if (pc === editorSelected) btn.className += " selected";
    if (pc === "") { btn.textContent = "Erase"; }
    else {
      const span = document.createElement("span");
      span.className = "piece " + (pc === pc.toUpperCase() ? "white" : "black");
      span.textContent = GLYPHS[pc];
      btn.appendChild(span);
    }
    btn.addEventListener("click", () => {
      editorSelected = pc;
      renderEditorPalette();
    });
    editorPaletteEl.appendChild(btn);
  });
}

// Seed the editor from the FEN currently typed in (if any), else the standard
// start position, so the user edits from a sensible base.
function seedEditorFromFen(fen) {
  editorPieces = {};
  const base = (fen && fen.trim()) ||
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const parts = base.trim().split(/\s+/);
  const rows = parts[0].split("/");
  if (rows.length === 8) {
    for (let r = 0; r < 8; r++) {
      const rank = 8 - r; let file = 0;
      for (const ch of rows[r]) {
        if (/\d/.test(ch)) file += parseInt(ch, 10);
        else if (file < 8) { editorPieces[String.fromCharCode(97 + file) + rank] = ch; file += 1; }
      }
    }
  }
  // Side to move.
  const turn = parts[1] === "b" ? "b" : "w";
  const turnEl = document.querySelector('input[name="editorTurn"][value="' + turn + '"]');
  if (turnEl) turnEl.checked = true;
  // Castling rights.
  const cr = parts[2] || "-";
  document.getElementById("castWK").checked = cr.indexOf("K") !== -1;
  document.getElementById("castWQ").checked = cr.indexOf("Q") !== -1;
  document.getElementById("castBK").checked = cr.indexOf("k") !== -1;
  document.getElementById("castBQ").checked = cr.indexOf("q") !== -1;
  if (editorEp) editorEp.value = parts[3] || "-";
  if (editorHalfmove) editorHalfmove.value = parts[4] || "0";
}

function openEditor() {
  seedEditorFromFen(customFen ? customFen.value : "");
  editorSelected = "P";
  if (editorError) { editorError.hidden = true; editorError.textContent = ""; }
  renderEditorPalette();
  renderEditorBoard();
  if (editorOverlay) editorOverlay.hidden = false;
}
function closeEditor() { if (editorOverlay) editorOverlay.hidden = true; }

// Validate the editor's FEN by asking the server (python-chess is the source
// of truth). On success, write it into the FEN input and close the editor.
async function useEditorPosition() {
  const fen = buildEditorFen();
  try {
    const res = await fetch("/api/view", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ human_color: "white", start_fen: fen, moves: [] }),
    });
    if (res.status === 400) {
      if (editorError) {
        editorError.textContent = "That position is not a legal FEN. " +
          "Check kings, side to move and castling rights.";
        editorError.hidden = false;
      }
      return;
    }
    if (!res.ok) {
      if (editorError) {
        editorError.textContent = "Could not validate the position (" + res.status + ").";
        editorError.hidden = false;
      }
      return;
    }
  } catch (e) {
    if (editorError) {
      editorError.textContent = "A network error occurred validating the position.";
      editorError.hidden = false;
    }
    return;
  }
  if (customFen) customFen.value = fen;
  if (customEnable) customEnable.checked = true;
  if (customBody) customBody.hidden = false;
  closeEditor();
}

// Impatience speech bubble under Chess Amateur.
function showSpeechBubble(text) {
  if (!speechBubble) return;
  speechBubble.textContent = text;
  speechBubble.hidden = false;
}
function hideSpeechBubble() {
  if (!speechBubble) return;
  speechBubble.hidden = true;
  speechBubble.textContent = "";
}

// Start is the only path that begins a game (from the popout's chosen values).
async function startNewGame() {
  hideSpeechBubble();
  exitReplay();
  stopClockTicker();
  reviewIndex = null;
  selfAnalysis = false;
  if (selfAnalysisToggle) selfAnalysisToggle.checked = false;
  const chosen = document.querySelector('input[name="color"]:checked');
  humanColor = chosen ? chosen.value : "white";
  threadsCount = chosenThreads();
  mode = chosenMode();
  const tc = chosenTimeControl();
  moves = []; startedAt = null; clockState = null; humanClockRunning = false;
  startFen = null;
  playerRating = null; botRating = null;
  setBusy(true); selected = null;
  try {
    const body = Object.assign(
      { human_color: humanColor, threads: threadsCount, mode: mode }, tc);
    // Casual-only custom start position (pasted FEN or board-editor FEN). The
    // server re-validates and returns start_fen, which we then carry back on
    // every /api/move for the stateless replay.
    const customFenValue = chosenCustomFen();
    if (customFenValue) body.fen = customFenValue;
    const res = await fetch("/api/new", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 409) {
      const data = await res.json();
      // A game is already in progress: surface a Resume affordance instead.
      closeNewGamePopout();
      await refreshInProgress();
      return;
    }
    if (res.status === 400) {
      // e.g. the correspondence-chess message for >= 1 day -> impatience bubble;
      // the game does NOT start and the popout stays open.
      const data = await res.json();
      showSpeechBubble((data && data.error) || "I don't want to play that.");
      return;
    }
    if (!res.ok) { showNetworkError("Could not start a new game (" + res.status + ")."); return; }
    pendingResume = null;
    closeNewGamePopout();
    adoptState(await res.json());
    renderAll();
    if (state.last_bot_move && CASettings.sound) CASound.move();
    afterMoveResolved();
  } catch (e) {
    showNetworkError("A network error occurred starting the game.");
  } finally { setBusy(false); }
}

async function resign() {
  if (busy || !inProgress) return;
  if (!window.confirm("Resign this game? It will be recorded as a loss.")) return;
  setBusy(true);
  stopClockTicker(); humanClockRunning = false;
  try {
    const res = await fetch("/api/resign", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode }),
    });
    if (!res.ok) { showNetworkError("Could not resign (" + res.status + ")."); return; }
    const data = await res.json();
    inProgress = false;
    if (state) {
      state.game_over = true;
      state.result = data.result || (humanColor === "white" ? "0-1" : "1-0");
      state.result_reason = "resignation";
      if (data.result_line) state.result_line = data.result_line;
      else {
        const winner = state.result === "1-0" ? "White" : "Black";
        state.result_line = state.result + " (" + winner + " won by resignation)";
      }
      state.legal_moves = [];
      if ("player_rating" in data) playerRating = data.player_rating;
      if ("bot_rating" in data) botRating = data.bot_rating;
      state.rating_delta = data.rating_delta;
    }
    renderAll();
    if (CASettings.sound) CASound.loss();
    showRatingDelta(data.rating_delta);
  } catch (e) {
    showNetworkError("A network error occurred resigning.");
  } finally { setBusy(false); }
}

// =========================================================================
// In-progress game (Resume) — never auto-resumes on load.
// =========================================================================
let pendingResume = null;   // the in-progress game info awaiting a Resume click

async function refreshInProgress() {
  if (!CFG.loggedIn) { pendingResume = null; renderControls(); return; }
  try {
    const res = await fetch("/api/in-progress");
    if (!res.ok) { pendingResume = null; renderControls(); return; }
    const data = await res.json();
    pendingResume = data.in_progress || null;
  } catch (e) { pendingResume = null; }
  renderControls();
}

// Resume starts the clock (the player's clock starts only on resume).
async function resumeInProgress() {
  if (!pendingResume) return false;
  const g = pendingResume;
  await renderFromMoves(g.moves || [], g.human_color, g.started_at, true, g);
  pendingResume = null;
  renderControls();
  return true;
}

async function renderFromMoves(moveList, color, started, live, resumeInfo) {
  const resumeStartFen = (resumeInfo && resumeInfo.start_fen) || null;
  const res = await fetch("/api/view", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ moves: moveList, human_color: color,
                           start_fen: resumeStartFen }),
  });
  if (!res.ok) return;
  const s = await res.json();
  adoptState(s);
  humanColor = color;
  moves = moveList;
  startFen = resumeStartFen;
  startedAt = started || s.started_at;
  inProgress = !!live;
  replayMode = false;
  reviewIndex = null;
  if (live && resumeInfo) {
    baseSeconds = resumeInfo.base_seconds != null ? resumeInfo.base_seconds : null;
    increment = resumeInfo.increment || 0;
    mode = resumeInfo.mode || mode;
    if (typeof resumeInfo.player_rating !== "undefined") playerRating = resumeInfo.player_rating;
    if (typeof resumeInfo.bot_rating !== "undefined") botRating = resumeInfo.bot_rating;
    if (baseSeconds == null) {
      clockState = null;
    } else if (resumeInfo.clock && resumeInfo.clock.white != null) {
      clockState = { white: resumeInfo.clock.white, black: resumeInfo.clock.black };
    } else {
      clockState = { white: baseSeconds, black: baseSeconds };
    }
    humanClockRunning = false;
  }
  renderAll();
  if (live) afterMoveResolved();   // resume the human clock if it's his turn
}

// =========================================================================
// In-game review: click a move in the log to jump the board to that ply.
// The player can only MAKE a move when on the last (current) position.
// This is a lightweight look-only layer over the LIVE game (distinct from the
// finished-game replay controls). The true live state is snapshotted so the
// full move log + result line stay visible while the board shows an old ply.
// =========================================================================
let reviewReqSeq = 0;
let liveSnapshot = null;   // preserved true live state while reviewing

async function jumpToPly(ply) {
  if (replayMode || !state) return;
  // Capture the live state the first time we leave the last move.
  if (!inReview()) liveSnapshot = state;
  const source = liveSnapshot || state;
  const total = (source.san_history || moves).length;
  const target = Math.max(0, Math.min(total, ply));
  if (target >= total) { returnToLive(); return; }
  reviewIndex = target;
  selected = null;
  stopClockTicker();
  humanClockRunning = false;
  const myReq = ++reviewReqSeq;
  const partial = moves.slice(0, target);
  let res;
  try {
    res = await fetch("/api/view", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ moves: partial, human_color: humanColor }),
    });
  } catch (e) { showNetworkError("A network error occurred loading that position."); return; }
  if (!res.ok) return;
  const data = await res.json();
  if (myReq !== reviewReqSeq) return;
  // Show only the reviewed position; keep the real game's move log/result.
  state = Object.assign({}, data, {
    san_history: liveSnapshot.san_history,
    result_line: liveSnapshot.result_line,
    game_over: liveSnapshot.game_over,
  });
  legalFrom = {};   // no interaction while reviewing
  renderBoard();
  renderMoveLog();
  renderClocks();
}

async function returnToLive() {
  if (!liveSnapshot) { reviewIndex = null; return; }
  reviewIndex = null;
  state = liveSnapshot;
  liveSnapshot = null;
  selected = null;
  renderAll();
  // Restore clock behaviour (resume the human clock if it's his turn).
  if (inProgress && state && !state.game_over && clockState) {
    resumeHumanClock();
    startClockTicker();
  }
}

// =========================================================================
// Self-analysis (Casual only): scratch layer over the real game state.
// =========================================================================
function setSelfAnalysis(on) {
  if (on) {
    if (mode !== "casual" || !state || state.game_over) { if (selfAnalysisToggle) selfAnalysisToggle.checked = false; return; }
    // Preserve the true game state + moves and freeze the clock.
    realState = state;
    realMoves = moves.slice();
    scratchMoves = moves.slice();
    stopClockTicker();
    humanClockRunning = false;
    selfAnalysis = true;
    reviewIndex = null;
    renderAll();
  } else {
    // Return the board EXACTLY to the real game position.
    selfAnalysis = false;
    if (realState) {
      state = realState;
      moves = realMoves.slice();
    }
    realState = null;
    scratchMoves = [];
    renderAll();
    if (inProgress && state && !state.game_over && clockState) {
      resumeHumanClock();
      startClockTicker();
    }
  }
}

// Apply a human move on the scratch board via /api/view (no engine reply, real
// server game state untouched). Both sides are moved by the human.
async function selfAnalysisMove(uci, fromSq, toSq) {
  if (busy || !state) return;
  // client-side legality check against the current legal map
  if (!(legalFrom[fromSq] && legalFrom[fromSq].includes(toSq))) { renderBoard(); return; }
  setBusy(true); selected = null;
  const apply = async () => {
    const next = scratchMoves.slice();
    next.push(uci);
    try {
      const res = await fetch("/api/view", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ moves: next, human_color: humanColor }),
      });
      if (!res.ok) { renderBoard(); setBusy(false); return; }
      const s = await res.json();
      scratchMoves = next;
      // Keep self-analysis fields on the scratch state; do NOT touch realState.
      state = s;
      moves = scratchMoves;
      if (CASettings.sound) { if (lastSanIndicatesCheck(s)) CASound.check(); else CASound.move(); }
      renderAll();
    } catch (e) {
      showNetworkError("A network error occurred.");
    } finally { setBusy(false); }
  };
  if (CASettings.animations) animateSlide(fromSq, toSq, apply);
  else apply();
}

// =========================================================================
// Network-error popout (cancellable)
// =========================================================================
function showNetworkError(text) {
  if (!netErrorOverlay) return;
  if (netErrorText) netErrorText.textContent = text || "A network error occurred. Please try again.";
  netErrorOverlay.hidden = false;
}
function hideNetworkError() {
  if (netErrorOverlay) netErrorOverlay.hidden = true;
}

// =========================================================================
// Review of finished games (read-only) with transport controls
// =========================================================================
let replayState = null;   // the finished game's full state (for move log/result)
function exitReplay() {
  replayMode = false;
  stopAutoplay();
  if (replayBlock) replayBlock.classList.remove("active");
}

async function startReplay(gameId) {
  try {
    const res = await fetch("/api/games/" + encodeURIComponent(gameId));
    if (!res.ok) { showNetworkError("Could not load that game."); return; }
    const g = (await res.json()).game;
    replayMode = true;
    replayMoves = g.moves || [];
    humanColor = g.human_color;
    replayIndex = replayMoves.length;   // start at final position
    replayBlock.classList.add("active");
    // Populate the review Info disclosure with the game's start/end time (to
    // the second, straight from the stored timestamps). Ongoing/unfinished
    // games have no end time yet.
    if (reviewStartEl) reviewStartEl.textContent = g.started_at || "\u2014";
    if (reviewEndEl) reviewEndEl.textContent = g.ended_at || "\u2014";
    // The reviewed game belongs to the current user: show their username below
    // the board (the FEAT-003 tiny name row).
    if (humanNameEl) humanNameEl.textContent = HUMAN_NAME;
    if (resignBtn) resignBtn.hidden = true;
    if (newGameBtn) newGameBtn.hidden = true;
    // Load the full final state so we can show the move log + result line.
    try {
      const fres = await fetch("/api/view", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ moves: replayMoves, human_color: humanColor }),
      });
      replayState = fres.ok ? await fres.json() : null;
    } catch (e) { replayState = null; }
    await renderReplayPosition();
  } catch (e) { showNetworkError("A network error occurred loading the game."); }
}

let replayReqSeq = 0;
async function renderReplayPosition() {
  const myReq = ++replayReqSeq;
  const idxAtRequest = replayIndex;
  const partial = replayMoves.slice(0, idxAtRequest);
  let res;
  try {
    res = await fetch("/api/view", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ moves: partial, human_color: humanColor }),
    });
  } catch (e) { return; }
  if (!res.ok) return;
  const data = await res.json();
  if (myReq !== replayReqSeq) return;
  state = data;
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
  rPlay.textContent = replayTimer ? "\u23F8" : "\u23EF";
}

function replayGoto(i) {
  replayIndex = Math.max(0, Math.min(replayMoves.length, i));
  renderReplayPosition();
}
function replayStep(delta) { stopAutoplay(); replayGoto(replayIndex + delta); }

function startAutoplay() {
  if (replayTimer) return;
  if (replayIndex >= replayMoves.length) replayIndex = 0;
  replayTimer = setInterval(() => {
    if (replayIndex >= replayMoves.length) { stopAutoplay(); updateReplayControls(); return; }
    replayIndex += 1;
    renderReplayPosition();
  }, 1000);
  updateReplayControls();
}
function stopAutoplay() {
  if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
}
function toggleAutoplay() { replayTimer ? stopAutoplay() : startAutoplay(); updateReplayControls(); }

// =========================================================================
// Settings toggles (with the sound-needs-animations dependency)
// =========================================================================
function syncToggleUI() {
  animToggle.checked = CASettings.animations;
  soundToggle.checked = CASettings.sound;
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
    if (CASettings.sound) CASound.move();
  });
}

// Generic disclosure toggle using the [hidden] attribute.
function wireDisclosure(toggleBtn, body) {
  if (!toggleBtn || !body) return;
  toggleBtn.addEventListener("click", () => {
    const open = body.hidden;
    body.hidden = !open;
    toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
    toggleBtn.classList.toggle("open", open);
  });
}

// =========================================================================
// Wire up + initial load
// =========================================================================
if (newGameBtn) newGameBtn.addEventListener("click", openNewGamePopout);
if (startGameBtn) startGameBtn.addEventListener("click", startNewGame);
if (cancelNewGameBtn) cancelNewGameBtn.addEventListener("click", closeNewGamePopout);
if (resumeBtn) resumeBtn.addEventListener("click", resumeInProgress);
if (resignBtn) resignBtn.addEventListener("click", resign);
if (selfAnalysisToggle) selfAnalysisToggle.addEventListener("change", () => setSelfAnalysis(selfAnalysisToggle.checked));
if (netErrorClose) netErrorClose.addEventListener("click", hideNetworkError);
if (logoutBtn) logoutBtn.addEventListener("click", async () => {
  try {
    await fetch("/api/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (e) { /* ignore */ }
  window.location.href = "/";
});

// Delete account: require an explicit confirmation through the palette overlay
// (NOT a raw window.confirm) before POSTing. On success the server has cleared
// the session; redirect home.
function openDeleteAccountConfirm() {
  if (deleteAccountOverlay) deleteAccountOverlay.hidden = false;
}
function closeDeleteAccountConfirm() {
  if (deleteAccountOverlay) deleteAccountOverlay.hidden = true;
}
if (deleteAccountBtn) deleteAccountBtn.addEventListener("click", openDeleteAccountConfirm);
if (deleteAccountCancel) deleteAccountCancel.addEventListener("click", closeDeleteAccountConfirm);
if (deleteAccountConfirm) deleteAccountConfirm.addEventListener("click", async () => {
  try {
    const res = await fetch("/api/delete-account", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    if (!res.ok) {
      closeDeleteAccountConfirm();
      showNetworkError("Could not delete your account. Please try again.");
      return;
    }
  } catch (e) {
    closeDeleteAccountConfirm();
    showNetworkError("Could not delete your account. Please try again.");
    return;
  }
  window.location.href = "/";
});

// ---------------------------------------------------------------------------
// Dashboard (FEAT-010): line graphs of the player's gameplay (FIDE classes +
// Rated) and puzzle ratings over a customizable from/to period. The graphs are
// lightweight inline SVG (no external chart library) drawn in the strict
// 8-color palette: one distinct pure color per series, axes/gridlines in blue.
// ---------------------------------------------------------------------------
const DASHBOARD_SERIES = [
  { kind: "fide_blitz", label: "FIDE blitz", color: "#00ff00" },     // green
  { kind: "fide_rapid", label: "FIDE rapid", color: "#00ffff" },     // cyan
  { kind: "fide_classical", label: "FIDE classical", color: "#ffff00" }, // yellow
  { kind: "rated", label: "Rated", color: "#ff00ff" },               // magenta
  { kind: "puzzle", label: "Puzzle", color: "#ffffff" },             // white
];
const DASH_AXIS = "#0000ff"; // blue axes/gridlines (pure palette color)

function svgEl(name, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  if (attrs) {
    for (const k in attrs) {
      if (attrs.hasOwnProperty(k)) el.setAttribute(k, attrs[k]);
    }
  }
  return el;
}

// Render-only downsampling for the dashboard chart. When a per-day series has
// more points than the chart is wide in pixels (> ~1 point/px), the polyline
// vertices/segments overplot and the connecting line becomes visually covered.
// This PURE helper reduces `points` to at most `maxPoints` evenly spaced
// entries while ALWAYS preserving the first (index 0) and last (index n-1)
// points so endpoints stay accurate. It never mutates the input array. When
// points.length <= maxPoints the input is returned unchanged (same reference).
function downsampleForRender(points, maxPoints) {
  const n = points.length;
  if (n <= maxPoints) return points;
  const out = [];
  let prev = -1;
  for (let i = 0; i < maxPoints; i++) {
    const idx = Math.round((i * (n - 1)) / (maxPoints - 1));
    if (idx !== prev) {
      out.push(points[idx]);
      prev = idx;
    }
  }
  return out;
}
// Expose the pure reducer on window so it is unit-testable (additive; does not
// redeclare Settings/Sound).
window.CA_downsampleForRender = downsampleForRender;

// Draw ONE series as an inline SVG line graph. `points` is the per-day series
// [{date, rating}, ...] already ordered ascending by GMT+8 calendar day (one
// point per day, last-known-value). The x-axis is the per-day index. Returns
// an SVG element.
function renderDashboardChart(series, points) {
  const W = 520, H = 180, PADL = 44, PADR = 12, PADT = 14, PADB = 26;
  const svg = svgEl("svg", {
    viewBox: "0 0 " + W + " " + H, class: "dash-chart",
    width: W, height: H, role: "img",
    "aria-label": series.label + " rating over time",
  });
  // Axes.
  svg.appendChild(svgEl("line", {
    x1: PADL, y1: PADT, x2: PADL, y2: H - PADB, stroke: DASH_AXIS,
    "stroke-width": 1, class: "dash-axis",
  }));
  svg.appendChild(svgEl("line", {
    x1: PADL, y1: H - PADB, x2: W - PADR, y2: H - PADB, stroke: DASH_AXIS,
    "stroke-width": 1, class: "dash-axis",
  }));
  // Downsample FOR RENDER ONLY so the polyline stays visible on wide windows.
  // The raw `points` array (and the /api/dashboard response) is not mutated.
  const plotted = downsampleForRender(points, W);
  const n = plotted.length;
  // Compute rating extent (pad a little so a flat line is visible).
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) {
    const r = plotted[i].rating;
    if (r < lo) lo = r;
    if (r > hi) hi = r;
  }
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  if (lo === hi) { lo -= 10; hi += 10; }
  const span = hi - lo;
  function xAt(i) {
    if (n <= 1) return PADL + (W - PADL - PADR) / 2;
    return PADL + (i / (n - 1)) * (W - PADL - PADR);
  }
  function yAt(r) {
    return PADT + (1 - (r - lo) / span) * (H - PADT - PADB);
  }
  // Y-axis min/max labels.
  const yTop = svgEl("text", { x: PADL - 6, y: PADT + 4, class: "dash-label",
    "text-anchor": "end", fill: DASH_AXIS });
  yTop.textContent = String(Math.round(hi));
  svg.appendChild(yTop);
  const yBot = svgEl("text", { x: PADL - 6, y: H - PADB, class: "dash-label",
    "text-anchor": "end", fill: DASH_AXIS });
  yBot.textContent = String(Math.round(lo));
  svg.appendChild(yBot);
  // The polyline (or a single marker if there is just one point).
  if (n === 1) {
    svg.appendChild(svgEl("circle", {
      cx: xAt(0), cy: yAt(plotted[0].rating), r: 3, fill: series.color,
    }));
  } else {
    let d = "";
    for (let i = 0; i < n; i++) {
      d += (i === 0 ? "M" : "L") + xAt(i).toFixed(1) + "," + yAt(plotted[i].rating).toFixed(1) + " ";
    }
    svg.appendChild(svgEl("path", {
      d: d.trim(), fill: "none", stroke: series.color, "stroke-width": 2,
      class: "dash-line",
    }));
  }
  return svg;
}

function renderDashboard(data) {
  if (!dashboardCharts) return;
  dashboardCharts.textContent = "";
  const seriesData = (data && data.series) || {};
  let any = false;
  for (let s = 0; s < DASHBOARD_SERIES.length; s++) {
    const series = DASHBOARD_SERIES[s];
    const points = seriesData[series.kind] || [];
    if (!points.length) continue;
    any = true;
    const card = document.createElement("div");
    card.className = "dash-card";
    const head = document.createElement("div");
    head.className = "dash-card-head";
    const swatch = document.createElement("span");
    swatch.className = "dash-swatch";
    swatch.style.backgroundColor = series.color;
    const title = document.createElement("span");
    title.className = "dash-card-title";
    const latest = points[points.length - 1].rating;
    title.textContent = series.label + " (" + Math.round(latest) + ")";
    head.appendChild(swatch);
    head.appendChild(title);
    card.appendChild(head);
    card.appendChild(renderDashboardChart(series, points));
    dashboardCharts.appendChild(card);
  }
  if (dashboardEmpty) dashboardEmpty.hidden = any;
}

// Format a Date as a plain YYYY-MM-DD string (local calendar sense). The server
// interprets the value as a GMT+8 calendar date. We read the UTC fields of the
// Date because preset math (below) builds a Date whose UTC clock IS the GMT+8
// wall clock, so browser-local timezone never leaks into the bucket boundary.
function dashboardYmd(d) {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return y + "-" + m + "-" + day;
}

// Resolve the current period selection into {from, to} query params. Presets
// map to from/to in GMT+8 terms (NOT browser-local) so preset windows line up
// with the server's GMT+8 buckets: Past week => today-7d..today, Past month =>
// today-30d, Past year => today-365d, where "today" is the current instant's
// GMT+8 calendar day. 'All' omits `from` (server uses the earliest event) and
// omits `to` (server defaults to today). 'Custom' uses the From/To date inputs
// verbatim (either may be blank).
function dashboardPeriodParams() {
  const period = dashboardPeriod ? dashboardPeriod.value : "all";
  if (period === "custom") {
    return {
      from: dashboardFrom && dashboardFrom.value ? dashboardFrom.value : null,
      to: dashboardTo && dashboardTo.value ? dashboardTo.value : null,
    };
  }
  if (period === "all") {
    return { from: null, to: null };
  }
  const days = period === "week" ? 7 : (period === "year" ? 365 : 30);
  // Shift the current UTC instant by +8h so the Date's UTC fields spell out the
  // GMT+8 wall-clock date; offset math then uses UTC day arithmetic (immune to
  // the browser's local zone) and dashboardYmd reads the UTC fields back out.
  const nowGmt8 = new Date(Date.now() + 8 * 60 * 60 * 1000);
  const to = new Date(nowGmt8.getTime());
  const from = new Date(nowGmt8.getTime());
  from.setUTCDate(from.getUTCDate() - days);
  return { from: dashboardYmd(from), to: dashboardYmd(to) };
}

// Show the From/To date inputs only for the Custom period, via [hidden].
function syncDashboardCustomVisibility() {
  if (!dashboardCustomRange) return;
  const isCustom = dashboardPeriod && dashboardPeriod.value === "custom";
  dashboardCustomRange.hidden = !isCustom;
}

async function loadDashboard() {
  const sel = dashboardPeriodParams();
  const params = [];
  if (sel.from) params.push("from=" + encodeURIComponent(sel.from));
  if (sel.to) params.push("to=" + encodeURIComponent(sel.to));
  const qs = params.length ? "?" + params.join("&") : "";
  try {
    const res = await fetch("/api/dashboard" + qs, {
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok) {
      showNetworkError("Could not load your dashboard. Please try again.");
      return;
    }
    const data = await res.json();
    renderDashboard(data);
  } catch (e) {
    showNetworkError("Could not load your dashboard. Please try again.");
  }
}

function openDashboard() {
  if (dashboardOverlay) dashboardOverlay.hidden = false;
  syncDashboardCustomVisibility();
  loadDashboard();
}
function closeDashboard() {
  if (dashboardOverlay) dashboardOverlay.hidden = true;
}
if (dashboardBtn) dashboardBtn.addEventListener("click", openDashboard);
if (dashboardClose) dashboardClose.addEventListener("click", closeDashboard);
if (dashboardApply) dashboardApply.addEventListener("click", loadDashboard);
if (dashboardPeriod) dashboardPeriod.addEventListener("change", () => {
  syncDashboardCustomVisibility();
  // Presets reload immediately; Custom waits for the player to pick dates and
  // press Apply (or change a date input).
  if (dashboardPeriod.value !== "custom") loadDashboard();
});
if (dashboardFrom) dashboardFrom.addEventListener("change", loadDashboard);
if (dashboardTo) dashboardTo.addEventListener("change", loadDashboard);

// time-control radios toggle the h/m/s inputs
const tcRadios = document.querySelectorAll('input[name="tc"]');
for (let i = 0; i < tcRadios.length; i++) tcRadios[i].addEventListener("change", syncTcInputs);

// mode radios toggle the Casual-only custom-position fieldset
const modeRadios = document.querySelectorAll('input[name="mode"]');
for (let i = 0; i < modeRadios.length; i++) modeRadios[i].addEventListener("change", syncCustomVisibility);
if (customEnable) customEnable.addEventListener("change", () => {
  if (customBody) customBody.hidden = !customEnable.checked;
});
if (openEditorBtn) openEditorBtn.addEventListener("click", openEditor);
if (editorUseBtn) editorUseBtn.addEventListener("click", useEditorPosition);
if (editorCancelBtn) editorCancelBtn.addEventListener("click", closeEditor);
if (editorClearBtn) editorClearBtn.addEventListener("click", () => {
  editorPieces = {}; renderEditorBoard();
});

wireDisclosure(settingsToggle, settingsBody);
wireDisclosure(advancedToggle, advancedBody);
wireDisclosure(profileToggle, profileBody);
wireDisclosure(reviewInfoToggle, reviewInfoBody);

if (rFirst) rFirst.addEventListener("click", () => replayStep(-replayMoves.length));
if (rPrev) rPrev.addEventListener("click", () => replayStep(-1));
if (rPlay) rPlay.addEventListener("click", toggleAutoplay);
if (rNext) rNext.addEventListener("click", () => replayStep(1));
if (rLast) rLast.addEventListener("click", () => replayStep(replayMoves.length));

// Render a static starting position with no clock running, no game in progress.
async function renderStartPosition() {
  try {
    const res = await fetch("/api/view", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ moves: [], human_color: humanColor }),
    });
    if (res.ok) {
      state = await res.json();
    }
  } catch (e) { /* leave board empty on failure */ }
  moves = [];
  inProgress = false;
  clockState = null;
  humanClockRunning = false;
  replayMode = false;
  reviewIndex = null;
  renderAll();
}

// =========================================================================
// Train view (FEAT-008): puzzle solving.
//
// Self-contained so it never perturbs the live-game state machine above. It
// reuses the shared FEN parser + GLYPHS + .board/.square/.piece rendering and
// the 8-color palette, and honors the bot-move animation invariant: the
// opponent's auto-reply renders the intermediate position (the solver's move
// landed) BEFORE sliding the opponent piece.
// =========================================================================
const trainTab = document.getElementById("trainTab");
const trainView = document.getElementById("trainView");
const trainBoardEl = document.getElementById("trainBoard");
const trainTurnEl = document.getElementById("trainTurn");
const trainStatusEl = document.getElementById("trainStatus");
const trainHintEl = document.getElementById("trainHint");
const trainNextBtn = document.getElementById("trainNext");
const puzzleRatingLabel = document.getElementById("puzzleRatingLabel");
const playerPuzzleRating = document.getElementById("playerPuzzleRating");

let trainActive = false;
let puzzle = null;          // {id, fen, solver_color, displayed_rating, ...}
let puzzleFen = null;       // current FEN of the puzzle position
let puzzleNextIndex = null; // next solver ply index to submit (null => done)
let puzzleSolverColor = "white";
let puzzleBusy = false;
let puzzleSelected = null;
let puzzleLegalNote = false; // when true, board is frozen (solved/failed)

function trainOrientation() {
  const flipped = puzzleSolverColor === "black";
  const files = [0,1,2,3,4,5,6,7], ranks = [8,7,6,5,4,3,2,1];
  return {
    fileOrder: flipped ? [...files].reverse() : files,
    rankOrder: flipped ? [...ranks].reverse() : ranks,
  };
}

function renderTrainBoard(highlight) {
  if (!trainBoardEl) return;
  trainBoardEl.innerHTML = "";
  const pieces = puzzleFen ? parseFen(puzzleFen) : {};
  const { fileOrder, rankOrder } = trainOrientation();
  const hFrom = highlight ? highlight.slice(0,2) : null;
  const hTo = highlight ? highlight.slice(2,4) : null;
  for (const rank of rankOrder) {
    for (const file of fileOrder) {
      const name = squareName(file, rank);
      const sq = document.createElement("div");
      sq.className = "square " + (isLightSquare(file, rank) ? "light" : "dark");
      sq.dataset.square = name;
      if (name === puzzleSelected) sq.classList.add("selected");
      if (name === hFrom || name === hTo) sq.classList.add("last-move");
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
      sq.addEventListener("click", () => onTrainSquareClick(name));
      trainBoardEl.appendChild(sq);
    }
  }
}

// Apply a UCI move to a FEN string locally (piece move + captures + castling
// rook shift + en-passant capture + promotion). Sufficient for rendering the
// puzzle line; the SERVER remains the source of truth for validity.
function applyUciToFen(fen, uci) {
  const pieces = parseFen(fen);
  const from = uci.slice(0,2), to = uci.slice(2,4);
  const promo = uci.length > 4 ? uci[4] : null;
  const moving = pieces[from];
  if (!moving) return fen;
  const isWhite = moving === moving.toUpperCase();
  // En passant: a pawn moving diagonally onto an empty square captures the
  // pawn behind the target square.
  if (moving.toLowerCase() === "p" && from[0] !== to[0] && !pieces[to]) {
    const capRank = to[1] === "6" ? "5" : (to[1] === "3" ? "4" : null);
    if (capRank) delete pieces[to[0] + capRank];
  }
  delete pieces[from];
  let placed = moving;
  if (promo) placed = isWhite ? promo.toUpperCase() : promo.toLowerCase();
  pieces[to] = placed;
  // Castling: king moved two files -> move the rook too.
  if (moving.toLowerCase() === "k" && Math.abs(from.charCodeAt(0) - to.charCodeAt(0)) === 2) {
    const rank = from[1];
    if (to[0] === "g") { pieces["f" + rank] = pieces["h" + rank]; delete pieces["h" + rank]; }
    else if (to[0] === "c") { pieces["d" + rank] = pieces["a" + rank]; delete pieces["a" + rank]; }
  }
  return fenFromPieces(pieces);
}

// Rebuild just the piece-placement field of a FEN from a {square: piece} map.
// The remaining FEN fields are not needed for rendering, so we emit a minimal
// placeholder tail.
function fenFromPieces(pieces) {
  const rowsOut = [];
  for (let r = 8; r >= 1; r--) {
    let row = "", empty = 0;
    for (let f = 0; f < 8; f++) {
      const p = pieces[String.fromCharCode(97 + f) + r];
      if (p) { if (empty) { row += empty; empty = 0; } row += p; }
      else empty += 1;
    }
    if (empty) row += empty;
    rowsOut.push(row);
  }
  return rowsOut.join("/") + " w - - 0 1";
}

function setPuzzleBusy(on) {
  puzzleBusy = on;
  if (trainNextBtn) trainNextBtn.disabled = on;
}

function trainSquareCenter(square) {
  const el = trainBoardEl.querySelector('[data-square="' + square + '"]');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

// Slide a piece on the TRAIN board (mirrors animateSlide for the play board).
function animateTrainSlide(from, to, cb) {
  if (!CASettings || !CASettings.animations) { cb(); return; }
  const fromSq = trainBoardEl.querySelector('[data-square="' + from + '"]');
  const pieceEl = fromSq && fromSq.querySelector(".piece");
  const a = trainSquareCenter(from), b = trainSquareCenter(to);
  if (!pieceEl || !a || !b) { cb(); return; }
  const dx = b.x - a.x, dy = b.y - a.y;
  pieceEl.classList.add("sliding");
  void pieceEl.offsetWidth;
  pieceEl.style.transform = "translate(" + dx + "px," + dy + "px)";
  let done = false;
  const finish = () => { if (done) return; done = true; cb(); };
  pieceEl.addEventListener("transitionend", finish, { once: true });
  setTimeout(finish, ANIM_MS + 80);
}

function renderPuzzleRatings() {
  if (puzzleRatingLabel) {
    if (puzzle && puzzle.displayed_rating != null) {
      puzzleRatingLabel.hidden = false;
      puzzleRatingLabel.textContent = "(" + puzzle.displayed_rating + ")";
    } else puzzleRatingLabel.hidden = true;
  }
  if (playerPuzzleRating) {
    if (puzzle && puzzle.player_rating != null) {
      playerPuzzleRating.hidden = false;
      playerPuzzleRating.textContent = "(" + puzzle.player_rating + ")";
    } else playerPuzzleRating.hidden = true;
  }
}

function renderPuzzleTurn() {
  if (!trainTurnEl) return;
  if (!puzzle || puzzleLegalNote) { trainTurnEl.hidden = true; return; }
  trainTurnEl.hidden = false;
  trainTurnEl.textContent = (puzzleSolverColor === "white" ? "White" : "Black") + " to move";
}

async function loadPuzzle() {
  setPuzzleBusy(true);
  puzzleSelected = null;
  puzzleLegalNote = false;
  if (trainStatusEl) trainStatusEl.textContent = "Loading a puzzle\u2026";
  if (trainHintEl) trainHintEl.hidden = true;
  if (trainNextBtn) trainNextBtn.hidden = true;
  try {
    const res = await fetch("/api/puzzle");
    if (!res.ok) { if (trainStatusEl) trainStatusEl.textContent = "Please sign in to train."; return; }
    const data = await res.json();
    puzzle = data.puzzle;
    if (!puzzle) {
      puzzleFen = null;
      renderTrainBoard(null);
      renderPuzzleRatings();
      renderPuzzleTurn();
      if (trainStatusEl) trainStatusEl.textContent = "No puzzles are available yet.";
      return;
    }
    puzzleFen = puzzle.fen;
    puzzleNextIndex = puzzle.next_index;
    puzzleSolverColor = puzzle.solver_color;
    renderTrainBoard(puzzle.setup_move || null);
    renderPuzzleRatings();
    renderPuzzleTurn();
    if (trainStatusEl) trainStatusEl.textContent = "Your move.";
    if (trainHintEl) trainHintEl.hidden = false;
  } catch (e) {
    if (trainStatusEl) trainStatusEl.textContent = "A network error occurred. Please try again.";
  } finally {
    setPuzzleBusy(false);
  }
}

function onTrainSquareClick(name) {
  if (puzzleBusy || !puzzle || puzzleLegalNote || puzzleNextIndex == null) return;
  const pieces = parseFen(puzzleFen);
  if (puzzleSelected) {
    if (name === puzzleSelected) { puzzleSelected = null; renderTrainBoard(null); return; }
    // Attempt a move from the selected square to this one.
    const fromPiece = pieces[puzzleSelected];
    if (fromPiece) { attemptPuzzleMove(puzzleSelected, name, fromPiece); return; }
  }
  // Only allow picking up the solver's own pieces.
  const p = pieces[name];
  if (p) {
    const isWhite = p === p.toUpperCase();
    if ((puzzleSolverColor === "white") === isWhite) {
      puzzleSelected = name; renderTrainBoard(null); return;
    }
  }
  puzzleSelected = null; renderTrainBoard(null);
}

function puzzleNeedsPromotion(fromPiece, toSquare) {
  if (!fromPiece || fromPiece.toLowerCase() !== "p") return false;
  const r = parseInt(toSquare[1], 10);
  return r === 8 || r === 1;
}

function attemptPuzzleMove(from, to, fromPiece) {
  if (puzzleNeedsPromotion(fromPiece, to)) {
    askPromotion((promo) => {
      if (!promo) { puzzleSelected = null; renderTrainBoard(null); return; }
      submitPuzzleMove(from + to + promo);
    });
    return;
  }
  submitPuzzleMove(from + to);
}

async function submitPuzzleMove(uci) {
  if (puzzleBusy || puzzleNextIndex == null) return;
  const submittedIndex = puzzleNextIndex;
  const from = uci.slice(0,2), to = uci.slice(2,4);
  puzzleSelected = null;
  setPuzzleBusy(true);
  try {
    const res = await fetch("/api/puzzle/move", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ move: uci, index: submittedIndex }),
    });
    if (!res.ok) { if (trainStatusEl) trainStatusEl.textContent = "Please sign in to train."; return; }
    const data = await res.json();
    if (data.correct) {
      // Render the solver's own move first.
      puzzleFen = applyUciToFen(puzzleFen, uci);
      renderTrainBoard(uci);
      if (data.solved && data.opponent_move == null) {
        finishPuzzle(true, data.puzzle_rating);
        return;
      }
      if (data.opponent_move) {
        // Bot-move animation invariant: intermediate position (solver's move
        // already landed, rendered above) is shown BEFORE sliding the
        // opponent reply.
        const oppFrom = data.opponent_move.slice(0,2);
        const oppTo = data.opponent_move.slice(2,4);
        animateTrainSlide(oppFrom, oppTo, () => {
          puzzleFen = applyUciToFen(puzzleFen, data.opponent_move);
          renderTrainBoard(data.opponent_move);
          if (data.solved) {
            finishPuzzle(true, data.puzzle_rating);
          } else {
            puzzleNextIndex = data.next_index;
            renderPuzzleTurn();
            if (trainStatusEl) trainStatusEl.textContent = "Correct! Keep going.";
          }
        });
      }
    } else {
      // Wrong move: show the attempted move landing, then mark failed.
      puzzleFen = applyUciToFen(puzzleFen, uci);
      renderTrainBoard(uci);
      finishPuzzle(false, data.puzzle_rating);
    }
  } catch (e) {
    if (trainStatusEl) trainStatusEl.textContent = "A network error occurred. Please try again.";
  } finally {
    setPuzzleBusy(false);
  }
}

function finishPuzzle(solved, newRating) {
  puzzleLegalNote = true;
  puzzleNextIndex = null;
  if (puzzle && newRating != null) puzzle.player_rating = newRating;
  renderPuzzleRatings();
  renderPuzzleTurn();
  if (trainStatusEl) {
    trainStatusEl.textContent = solved
      ? ("Solved! Your puzzle rating is now " + newRating + ".")
      : ("Failed. Your puzzle rating is now " + newRating + ".");
  }
  if (trainHintEl) trainHintEl.hidden = true;
  if (trainNextBtn) trainNextBtn.hidden = false;
}

function showTrainView() {
  trainActive = true;
  if (trainView) trainView.hidden = false;
  const playMain = document.querySelector("main.layout:not(.train-layout)");
  if (playMain) playMain.hidden = true;
  if (trainTab) trainTab.setAttribute("aria-pressed", "true");
  loadPuzzle();
}

function hideTrainView() {
  trainActive = false;
  if (trainView) trainView.hidden = true;
  const playMain = document.querySelector("main.layout:not(.train-layout)");
  if (playMain) playMain.hidden = false;
  if (trainTab) trainTab.setAttribute("aria-pressed", "false");
}

if (trainTab) trainTab.addEventListener("click", () => {
  if (trainActive) hideTrainView(); else showTrainView();
});
if (trainNextBtn) trainNextBtn.addEventListener("click", () => { if (!puzzleBusy) loadPuzzle(); });

// ---------------------------------------------------------------------------
// Onboarding demo (FEAT-009; FEAT-001 follow-up): a brief, SKIPPABLE walkthrough
// of the site's main features. NEW-USERS-ONLY: shown automatically (CFG.showDemo,
// set by the server from the per-user demo_pending flag, which is TRUE only for
// freshly-registered accounts) exactly once, right after account creation.
// Pre-existing users NEVER see it, and it is NEVER re-shown on a version bump.
// Stepping past the last slide, or Skip, marks the demo seen (POST /api/demo/seen),
// which clears demo_pending so it is not shown again. Uses the [hidden] attribute
// and the 8-color palette; declares no globals that clash with effects.js.
// ---------------------------------------------------------------------------
const demoOverlay = document.getElementById("demoOverlay");
const demoSlides = demoOverlay
  ? Array.prototype.slice.call(demoOverlay.querySelectorAll(".demo-slide"))
  : [];
const demoSkipBtn = document.getElementById("demoSkip");
const demoNextBtn = document.getElementById("demoNext");
const demoBackBtn = document.getElementById("demoBack");
const demoProgress = document.getElementById("demoProgress");
let demoIndex = 0;
let demoSeenSent = false;

function renderDemoSlide() {
  for (let i = 0; i < demoSlides.length; i++) {
    demoSlides[i].hidden = i !== demoIndex;
  }
  const last = demoIndex >= demoSlides.length - 1;
  if (demoBackBtn) demoBackBtn.hidden = demoIndex === 0;
  if (demoNextBtn) demoNextBtn.textContent = last ? "Finish" : "Next";
  if (demoProgress) {
    demoProgress.textContent = (demoIndex + 1) + " / " + demoSlides.length;
  }
}

// Mark the demo seen so the server stops showing it (until the version bumps).
// Best-effort: a network failure just means the demo may reappear next load.
async function markDemoSeen() {
  if (demoSeenSent) return;
  demoSeenSent = true;
  try {
    await fetch("/api/demo/seen", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
  } catch (e) { /* non-fatal: demo can reappear on the next load */ }
}

function closeDemo() {
  if (demoOverlay) demoOverlay.hidden = true;
  markDemoSeen();
}

function openDemo() {
  if (!demoOverlay || !demoSlides.length) return;
  demoIndex = 0;
  renderDemoSlide();
  demoOverlay.hidden = false;
}

if (demoNextBtn) demoNextBtn.addEventListener("click", () => {
  if (demoIndex >= demoSlides.length - 1) { closeDemo(); return; }
  demoIndex += 1;
  renderDemoSlide();
});
if (demoBackBtn) demoBackBtn.addEventListener("click", () => {
  if (demoIndex > 0) { demoIndex -= 1; renderDemoSlide(); }
});
if (demoSkipBtn) demoSkipBtn.addEventListener("click", closeDemo);

async function init() {
  wireSettings();
  const params = new URLSearchParams(window.location.search);
  const replayId = params.get("replay");
  if (CFG.loggedIn && replayId) { await startReplay(replayId); return; }
  // NO auto-start / NO auto-resume / NO auto-clock. Show the starting position.
  await renderStartPosition();
  // If logged in, surface a Resume affordance (does NOT start the clock).
  if (CFG.loggedIn) { await refreshInProgress(); }
  // Onboarding demo: new-users-only, shown once (server-gated via CFG.showDemo,
  // which now reflects the per-user demo_pending flag).
  if (CFG.loggedIn && CFG.showDemo) { openDemo(); }
}
init();
