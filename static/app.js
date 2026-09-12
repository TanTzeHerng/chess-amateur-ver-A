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
// clocks
const clockBlock = document.getElementById("clockBlock");
const clockTopLabel = document.getElementById("clockTopLabel");
const clockTopTime = document.getElementById("clockTopTime");
const clockBottomLabel = document.getElementById("clockBottomLabel");
const clockBottomTime = document.getElementById("clockBottomTime");
const tcInputs = document.getElementById("tcInputs");
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

// -------------------------------------------------------------------------
// Clocks + fairness rule
// -------------------------------------------------------------------------
function fmtClock(sec) {
  if (sec == null) return "--:--";
  sec = Math.max(0, sec);
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
  if (!clockBlock) return;
  if (!clockState || replayMode) { clockBlock.hidden = true; return; }
  clockBlock.hidden = false;
  const botColor = humanColor === "white" ? "black" : "white";
  // Top = opponent (Chess Amateur), bottom = human.
  clockTopLabel.textContent = BOT_NAME;
  clockBottomLabel.textContent = "You";
  const humanRem = liveHumanRemaining();
  clockBottomTime.textContent = fmtClock(humanRem);
  clockTopTime.textContent = fmtClock(clockState[botColor]);
  // active/low styling
  const bottomEl = clockBottomTime.parentElement;
  const topEl = clockTopTime.parentElement;
  bottomEl.className = "clock" + (humanClockRunning ? " active" : "") + (humanRem != null && humanRem < 10 ? " low" : "");
  topEl.className = "clock" + (!humanClockRunning && state && !state.game_over ? " active" : "") + (clockState[botColor] != null && clockState[botColor] < 10 ? " low" : "");
}

function startClockTicker() {
  stopClockTicker();
  if (!clockState) return;
  clockTicker = setInterval(() => {
    renderClocks();
    // Client-side flag: if the human's live clock hits 0 while running, submit
    // a forfeit by sending a move attempt with the elapsed >= remaining. We
    // simply let the server enforce it on the next move; to end promptly we
    // trigger a "timeout" move with a null move so the server flags. Simpler:
    // once it hits 0, stop the human clock and show the loss locally; the
    // server will finalize on the next interaction. To be safe we auto-submit.
    const rem = liveHumanRemaining();
    if (humanClockRunning && rem != null && rem <= 0) {
      humanClockRunning = false;
      submitTimeout();
    }
  }, 200);
}
function stopClockTicker() {
  if (clockTicker) { clearInterval(clockTicker); clockTicker = null; }
}

// The human's clock RESUMES (starts counting) only after Chess Amateur's move
// animation has ended and it is the human's turn (fairness rule).
function resumeHumanClock() {
  if (!clockState || !state || state.game_over || replayMode) return;
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
  // reflect locally (server will return the authoritative value)
  if (clockState) clockState[humanColor] = Math.max(0, humanClockAtResume - elapsed);
  renderClocks();
  return elapsed;
}

async function submitTimeout() {
  // Human flagged locally: tell the server via a move with huge elapsed so it
  // finalizes as a time forfeit. We send the last legal-looking move field but
  // the server flags before applying it (elapsed >= remaining).
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
      }),
    });
    if (res.ok) { adoptState(await res.json()); renderAll(); afterMoveResolved(); }
  } catch (e) { /* ignore */ }
  finally { setBusy(false); }
}

function renderAll() {
  rebuildLegalMap();
  renderBoard();
  renderMoveLog();
  renderTurn();
  renderBanner();
  renderThreadsBadge();
  renderResign();
  renderClocks();
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
  if (typeof s.mode === "string") mode = s.mode;
  if ("base_seconds" in s) baseSeconds = s.base_seconds;
  if ("increment" in s) increment = s.increment || 0;
  // Absorb the authoritative clock from the server (null => unlimited).
  if ("clock" in s) clockState = s.clock ? { white: s.clock.white, black: s.clock.black } : null;
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
  // Fairness rule: stop the human's clock the INSTANT he commits the move and
  // capture the elapsed time NOW (before any animation), so the slide/network
  // don't eat into his clock. Server is authoritative and clamps/flags.
  const elapsed = stopHumanClockAndGetElapsed();
  const clockSnapshot = clockState ? { white: clockState.white, black: clockState.black } : null;
  setBusy(true); selected = null; clearMessage();
  // Animate the human's piece sliding first (if enabled), then post.
  const doPost = async () => {
    try {
      const res = await fetch("/api/move", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          game_id: state.game_id, move: uci, moves: moves,
          human_color: humanColor, threads: threadsCount, started_at: startedAt,
          mode: mode, base_seconds: baseSeconds, increment: increment,
          clock: clockSnapshot, elapsed: elapsed,
        }),
      });
      if (res.status === 400) { showMessage("Illegal move. Try a different move."); renderBoard(); return; }
      if (!res.ok) { showMessage("Server error (" + res.status + ")."); return; }
      const next = await res.json();
      const botUci = next.last_bot_move ? next.last_bot_move.uci : null;

      // BUG-FIX 1 (sound ordering): play the HUMAN's move sound NOW, the moment
      // his move resolves on the server -- not later, buried in the bot's
      // animation path. Human's own move (check knock if it gave check, else a
      // normal knock). If the human's move ended the game, play the end tune.
      playHumanMoveSound(next, botUci);

      // BUG-FIX 2 (instant bot move): in FIDE mode Chess Amateur must actually
      // WAIT its computed think-time before revealing its move, with its clock
      // ticking down live (the human's clock stays stopped -- fairness). Only
      // after the pause do we reveal/animate the reply.
      const botThink = (typeof next.bot_think === "number") ? next.bot_think : 0;

      const revealBot = () => {
        if (!botUci) {
          // No bot reply (human move ended the game): just commit + resume.
          adoptState(next);
          renderAll();
          afterMoveResolved();
          setBusy(false);
          return;
        }
        if (CASettings.animations) {
          // Show the INTERMEDIATE position (human moved, bot not yet) so the
          // bot's piece is on its origin square, then slide it.
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
        holdForBotThink(next, botThink, revealBot);   // ticks bot clock, then reveals
      } else {
        revealBot();
      }
    } catch (e) {
      showMessage("Network error. Please try again.");
      setBusy(false);
    }
  };
  if (CASettings.animations) animateSlide(fromSq, toSq, doPost);
  else doPost();
}

// Sound for the HUMAN's own move, played immediately when it resolves.
// san_history layout after a move: [..., humanSan] or [..., humanSan, botSan].
// If there is a bot reply (botUci set), the human's SAN is the second-to-last;
// otherwise (human move ended the game) it's the last entry.
function playHumanMoveSound(next, botUci) {
  if (!CASettings.sound) return;
  const h = next.san_history || [];
  const humanSan = botUci ? h[h.length - 2] : h[h.length - 1];
  const humanGaveCheckOrMate = humanSan && (humanSan.includes("+") || humanSan.includes("#"));
  // If the human's move ended the game, play the end tune for the human's move.
  if (next.game_over && !botUci) {
    const info = bannerInfo(next.result, next.result_reason, humanColor);
    if (humanGaveCheckOrMate) CASound.check(); else CASound.move();
    setTimeout(() => {
      if (info.kind === "win") CASound.win();
      else if (info.kind === "draw") CASound.draw();
      else CASound.loss();
    }, 180);
  } else {
    if (humanGaveCheckOrMate) CASound.check(); else CASound.move();
  }
}

// Hold Chess Amateur's move for `seconds` (its real think-time), ticking its
// clock down live while the human's clock stays stopped (fairness). Calls
// `done()` once the pause elapses. A "thinking" indicator is shown throughout.
function holdForBotThink(next, seconds, done) {
  const botColor = humanColor === "white" ? "black" : "white";
  const startClock = (next.clock && next.clock[botColor] != null)
    ? next.clock[botColor] + seconds   // clock in `next` already has think-time deducted;
    : null;                            // add it back so we can animate it counting down
  const t0 = performance.now();
  thinkingEl.hidden = false;
  const tick = setInterval(() => {
    const elapsed = (performance.now() - t0) / 1000;
    if (clockState && startClock != null) {
      clockState[botColor] = Math.max(next.clock[botColor], startClock - elapsed);
      renderClocks();
    }
    if (elapsed >= seconds) {
      clearInterval(tick);
      thinkingEl.hidden = true;
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
    body: JSON.stringify({ moves: interMoves, human_color: humanColor }),
  }).then(r => r.ok ? r.json() : null)
    .then(d => finish(d ? d.fen : null))
    .catch(() => finish(null));
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

// Called once a move (and Chess Amateur's animated reply) has fully resolved.
// Handles game-over (show rating delta, stop clocks) or resumes the human's
// clock per the fairness rule.
function afterMoveResolved() {
  if (state && state.game_over) {
    stopClockTicker();
    humanClockRunning = false;
    showRatingDelta(state.rating_delta);
    return;
  }
  // Fairness: the human's clock only starts now (after CA's animation ended).
  if (clockState) { resumeHumanClock(); startClockTicker(); }
}

function showRatingDelta(delta) {
  if (!delta) return;   // casual / guest -> nothing to show
  // delta is a self-describing string, e.g. "+6.40 FIDE classical" or "-12.34".
  showMessage("Rating change: " + delta);
}

function chosenThreads() {
  let n = parseInt(threadsInput && threadsInput.value, 10);
  if (!Number.isFinite(n)) n = CFG.defaultThreads || 128;
  n = Math.max(1, Math.min(128, n));
  if (threadsInput) threadsInput.value = String(n);
  return n;
}

// Read mode + time control from the controls for a new game.
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

async function newGame() {
  exitReplay();
  stopClockTicker();
  const chosen = document.querySelector('input[name="color"]:checked');
  humanColor = chosen ? chosen.value : "white";
  threadsCount = chosenThreads();
  mode = chosenMode();
  const tc = chosenTimeControl();
  moves = []; startedAt = null; clockState = null; humanClockRunning = false;
  setBusy(true); selected = null; clearMessage(); bannerEl.hidden = true;
  try {
    const body = Object.assign(
      { human_color: humanColor, threads: threadsCount, mode: mode }, tc);
    const res = await fetch("/api/new", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 409) {
      const data = await res.json();
      showMessage((data && data.error) || "You have a game in progress.");
      await resumeInProgress();
      return;
    }
    if (res.status === 400) {
      // e.g. the correspondence-chess message for >= 1 day.
      const data = await res.json();
      showMessage((data && data.error) || "Invalid time control.");
      return;
    }
    if (!res.ok) { showMessage("Could not start a new game (" + res.status + ")."); return; }
    adoptState(await res.json());
    renderAll();
    if (state.last_bot_move && CASettings.sound) CASound.move();
    afterMoveResolved();   // start the human clock (fairness) once it's his turn
  } catch (e) {
    showMessage("Network error starting game.");
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
    if (!res.ok) { showMessage("Could not resign (" + res.status + ")."); return; }
    const data = await res.json();
    inProgress = false;
    if (state) {
      state.game_over = true;
      state.result = data.result || (humanColor === "white" ? "0-1" : "1-0");
      state.result_reason = "resignation";
      state.legal_moves = [];
    }
    renderAll();
    if (CASettings.sound) CASound.loss();
    if (data.rating_delta) showMessage("You resigned. Rating change: " + data.rating_delta);
    else showMessage("You resigned. See it in \u201CMy games\u201D.");
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
    await renderFromMoves(g.moves || [], g.human_color, g.started_at, true, g);
    return true;
  } catch (e) { return false; }
}

async function renderFromMoves(moveList, color, started, live, resumeInfo) {
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
  // Restore mode/time-control + the LIVE clock for a resumed game. The server
  // persists each side's remaining time on every autosave, so a resumed game
  // continues with the correct clock rather than restarting at the base time.
  if (live && resumeInfo) {
    baseSeconds = resumeInfo.base_seconds != null ? resumeInfo.base_seconds : null;
    increment = resumeInfo.increment || 0;
    if (baseSeconds == null) {
      clockState = null;                 // unlimited
    } else if (resumeInfo.clock && resumeInfo.clock.white != null) {
      clockState = { white: resumeInfo.clock.white, black: resumeInfo.clock.black };
    } else {
      clockState = { white: baseSeconds, black: baseSeconds };  // fallback
    }
    humanClockRunning = false;
  }
  renderAll();
  if (live) afterMoveResolved();   // resume the human clock if it's his turn
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

let replayReqSeq = 0;   // guards against out-of-order /api/view responses
async function renderReplayPosition() {
  // Render the position after the first `replayIndex` plies. Guard against
  // races: during 1s autoplay, multiple /api/view fetches may be in flight;
  // only the most recent request is allowed to update the board so a slow
  // earlier response can't clobber a newer position (which looked like the
  // board "sticking" on an early move).
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
  if (myReq !== replayReqSeq) return;   // a newer request superseded this one
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
