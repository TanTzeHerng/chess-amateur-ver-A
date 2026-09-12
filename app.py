#!/usr/bin/env python3
"""Chess Amateur web app with optional accounts + resumable game history.

Flask front end that wraps the proven, STATELESS chess core (chess_core.py)
and the depth-1 Stockfish engine (engine.py). Adds:

  * Accounts: username + password (bcrypt), Flask signed-session cookies.
  * Guest play: no login required; guest games are NEVER persisted.
  * Per-user history: finished games store started_at + ended_at (to the
    second) and the full move list; a history view lists + replays them.
  * Resumable in-progress games with autosave after every move.
  * INTEGRITY RULE: a logged-in user may have AT MOST ONE in-progress game.
    "New game" is refused server-side while one is in progress (they must
    resume or resign it first) so a losing position cannot be silently
    abandoned by starting fresh.

Engine behavior is unchanged: depth 1, threads configurable, full UCI output
logged to the SERVER logs only (operator telemetry), and ONLY the bestmove is
ever returned to the browser -- the player never sees the engine's eval/PV.

Environment:
  DATABASE_URL  Postgres connection string (Render). If unset, the app runs in
                GUEST-ONLY mode: play works, accounts/history are disabled.
  SECRET_KEY    Flask session signing key. Required for logins to persist; a
                random ephemeral key is generated if unset (sessions won't
                survive a restart, and multi-instance won't share sessions).
  SF_THREADS    default engine thread count (see chess_core / engine).
  PORT          port to bind (default 8000).
"""
import datetime
import os
import secrets

from flask import (Flask, request, session, jsonify, redirect, url_for,
                   render_template, abort)

import chess_core as core
import auth
import ratings as R
import timecontrol as TC
import clockclient
from storage import Store

app = Flask(__name__)

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    app.logger.warning("SECRET_KEY not set; using an ephemeral key "
                       "(sessions will not survive restarts).")
app.config.update(
    SECRET_KEY=_secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure cookies in production (HTTPS on Render). Allow override for local
    # HTTP testing via SESSION_COOKIE_SECURE=0.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") != "0",
)

STORE = Store()  # reads DATABASE_URL; disabled (guest-only) if unset/unreachable


def _now_iso():
    """Current UTC time as an ISO-8601 string accurate to the second."""
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


def _current_user():
    """Return the logged-in user dict {id, username} or None."""
    uid = session.get("uid")
    if uid is None or not STORE.enabled:
        return None
    return STORE.get_user_by_id(uid)


def _human_is_loser_result(human_color):
    """Result string for a human resignation (opponent wins)."""
    return "0-1" if human_color == "white" else "1-0"


VALID_MODES = ("fide", "rated", "casual")


def _parse_mode(value):
    m = str(value or "casual").lower()
    return m if m in VALID_MODES else "casual"


def _parse_time_control(data):
    """Read a time control from the request. Returns (base_seconds, increment)
    where base_seconds is None for unlimited. Raises TC.InvalidTimeControl for
    an out-of-range custom control (caller turns that into a 400 with the
    message)."""
    if data.get("unlimited"):
        return None, 0
    base = TC.parse_base_seconds(
        data.get("hours", 0), data.get("minutes", 0), data.get("seconds", 0))
    inc = data.get("increment", 0)
    if isinstance(inc, bool) or not isinstance(inc, int) or inc < 0:
        raise TC.InvalidTimeControl("increment must be a nonnegative integer")
    TC.validate_custom(base, inc)   # raises with the correspondence message if >= 1 day
    return base, inc


def _apply_result_and_rating(user, mode, human_color, base_seconds, increment,
                             result):
    """Apply the rating change for a finished game and return the
    self-describing rating_delta string (or None for casual/guest).

    FIDE: Elo K=10 against Chess Amateur's fixed rating for the game's time
    class (OUR definition); updates the matching one of the player's three
    FIDE ratings; returns e.g. '+6.40 FIDE classical'.
    Rated: Glicko-2 vs the fixed Chess Amateur rating; returns e.g. '+12.34'.
    Casual: no change; returns None.
    """
    if not user or mode == "casual":
        return None
    score = R.human_score(result, human_color)
    tclass = R.time_class(base_seconds, increment)

    if mode == "fide":
        current = {"blitz": user["fide_blitz"], "rapid": user["fide_rapid"],
                   "classical": user["fide_classical"]}[tclass]
        opp = R.CHESS_AMATEUR_FIDE[tclass]
        new_rating, delta = R.elo_update(current, opp, score)
        STORE.update_fide_rating(user["id"], tclass, new_rating)
        sign = "+" if delta >= 0 else ""
        return "%s%.2f FIDE %s" % (sign, delta, tclass)

    # rated (Glicko-2)
    new_r, new_rd, new_vol, delta = R.glicko2_update(
        user["rated_rating"], user["rated_rd"], user["rated_vol"],
        R.CHESS_AMATEUR_RATED, R.CHESS_AMATEUR_RATED_RD, score)
    STORE.update_rated_rating(user["id"], new_r, new_rd, new_vol)
    sign = "+" if delta >= 0 else ""
    return "%s%.2f" % (sign, delta)


# ==========================================================================
# Pages
# ==========================================================================

@app.get("/")
def index():
    user = _current_user()
    return render_template(
        "index.html",
        bot_name=core.BOT_NAME,
        user=user,
        accounts_enabled=STORE.enabled,
        default_threads=core.DEFAULT_GAME_THREADS,
    )


@app.get("/login")
def login_page():
    if _current_user():
        return redirect(url_for("index"))
    return render_template("login.html", bot_name=core.BOT_NAME,
                           accounts_enabled=STORE.enabled, mode="login")


@app.get("/register")
def register_page():
    if _current_user():
        return redirect(url_for("index"))
    return render_template("login.html", bot_name=core.BOT_NAME,
                           accounts_enabled=STORE.enabled, mode="register")


@app.get("/history")
def history_page():
    user = _current_user()
    if not user:
        return redirect(url_for("login_page"))
    games = STORE.list_games(user["id"])
    return render_template("history.html", bot_name=core.BOT_NAME,
                           user=user, games=games)


# ==========================================================================
# Auth API
# ==========================================================================

@app.post("/api/register")
def api_register():
    if not STORE.enabled:
        return jsonify({"error": "Accounts are unavailable right now."}), 503
    data = request.get_json(silent=True) or {}
    user, err = auth.register_user(STORE, data.get("username"),
                                   data.get("password"))
    if err:
        return jsonify({"error": err}), 400
    session.clear()
    session["uid"] = user["id"]
    return jsonify({"user": user})


@app.post("/api/login")
def api_login():
    if not STORE.enabled:
        return jsonify({"error": "Accounts are unavailable right now."}), 503
    data = request.get_json(silent=True) or {}
    user, err = auth.authenticate_user(STORE, data.get("username"),
                                       data.get("password"))
    if err:
        return jsonify({"error": err}), 401
    session.clear()
    session["uid"] = user["id"]
    return jsonify({"user": user})


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    user = _current_user()
    return jsonify({
        "user": user,
        "accounts_enabled": STORE.enabled,
        "bot_name": core.BOT_NAME,
    })


# ==========================================================================
# Game API
# ==========================================================================

def _persist_in_progress(user_id, gid, human_color, moves_uci, started_at,
                         base_seconds=None, increment=0, clock=None):
    """Create or update the single in-progress game row for a user.

    `clock` is the live {white, black} remaining seconds (or None for
    unlimited) so a resumed game continues with the correct times.
    Returns the game id (existing or newly created).
    """
    cw = clock.get("white") if isinstance(clock, dict) else None
    cb = clock.get("black") if isinstance(clock, dict) else None
    return STORE.upsert_in_progress_game(
        user_id=user_id, game_id=gid, human_color=human_color,
        moves_uci=moves_uci, started_at=started_at,
        base_seconds=base_seconds, increment=increment,
        clock_white=cw, clock_black=cb)


@app.post("/api/new")
def api_new():
    """Start a new game.

    Guests: stateless, nothing saved.
    Logged-in users: REFUSED (409) if they already have an in-progress game
    (must resume or resign it first) -- server-side enforcement of the
    one-active-game integrity rule.
    """
    data = request.get_json(silent=True) or {}
    human_color = str(data.get("human_color", "white")).lower()
    if human_color not in ("white", "black"):
        return jsonify({"error": "human_color must be 'white' or 'black'"}), 400
    threads = core.coerce_threads(data.get("threads"))
    mode = _parse_mode(data.get("mode"))
    user = _current_user()

    # Rated/FIDE modes require an account (nothing to rate for a guest).
    if mode in ("fide", "rated") and not user:
        mode = "casual"

    # Parse + validate the time control (custom or unlimited).
    try:
        base_seconds, increment = _parse_time_control(data)
    except TC.InvalidTimeControl as exc:
        return jsonify({"error": str(exc)}), 400

    if user:
        existing = STORE.get_in_progress_game(user["id"])
        if existing is not None:
            # Enforce the rule: cannot start a new game while one is live.
            return jsonify({
                "error": "You have a game in progress. Resume it or resign it "
                         "before starting a new game.",
                "in_progress_game_id": existing["id"],
            }), 409

    board, san_history, moves_uci = core.rebuild_board([])
    started_at = _now_iso()
    last_bot_move = None
    try:
        if human_color == "black" and not board.is_game_over(claim_draw=True):
            uci, san = core.engine_move(board, threads=threads)
            if uci:
                san_history.append(san)
                moves_uci.append(uci)
                last_bot_move = {"uci": uci, "san": san}
    except core.EngineUnavailable:
        return jsonify({"error": "engine unavailable"}), 500

    # Initial clocks (both sides start with the full base time). Unlimited ->
    # None (no clock). These are carried by the client and echoed back; the
    # server enforces flagging using client-reported elapsed time (clamped).
    clock = None
    if base_seconds is not None:
        clock = {"white": float(base_seconds), "black": float(base_seconds)}

    gid = None
    if user:
        gid = _persist_in_progress(user["id"], None, human_color,
                                   moves_uci, started_at,
                                   base_seconds=base_seconds, increment=increment,
                                   clock=clock)

    extra = {
        "started_at": started_at, "logged_in": bool(user),
        "in_progress": bool(user), "mode": mode,
        "base_seconds": base_seconds, "increment": increment,
        "unlimited": base_seconds is None,
        "clock": clock,
        "time_class": R.time_class(base_seconds, increment),
    }
    return jsonify(core.state_dict(board, human_color, threads, san_history,
                                   moves_uci, game_id=gid,
                                   last_bot_move=last_bot_move, extra=extra))


@app.post("/api/move")
def api_move():
    """Apply the human's move, then Chess Amateur replies.

    Stateless: the client carries the move history. For logged-in users the
    in-progress game is AUTOSAVED after the move; if the game ends, it is
    finalized (ended_at set to the second).
    """
    data = request.get_json(silent=True) or {}
    move_uci = data.get("move")
    human_color = str(data.get("human_color", "white")).lower()
    if human_color not in ("white", "black"):
        human_color = "white"
    bot_color = "black" if human_color == "white" else "white"
    threads = core.coerce_threads(data.get("threads"))
    mode = _parse_mode(data.get("mode"))
    user = _current_user()
    if mode in ("fide", "rated") and not user:
        mode = "casual"

    # Time control for this game.
    base_seconds = data.get("base_seconds")
    increment = data.get("increment") or 0
    if isinstance(base_seconds, bool):
        base_seconds = None
    unlimited = base_seconds is None

    # Rebuild authoritative position from carried history.
    try:
        board, san_history, moves_uci = core.rebuild_board(data.get("moves"))
    except core.InvalidMoveHistory:
        return jsonify({"error": "invalid move history"}), 400

    # For logged-in users, bind this to their in-progress game (if any) so we
    # update the right row, and take the authoritative time control from the DB.
    gid = data.get("game_id")
    started_at = data.get("started_at") or _now_iso()
    if user:
        ip = STORE.get_in_progress_game(user["id"])
        if ip is not None:
            gid = ip["id"]
            started_at = ip["started_at"]
            base_seconds = ip["base_seconds"]
            increment = ip["increment"]
            unlimited = base_seconds is None

    if board.is_game_over(claim_draw=True):
        return jsonify({"error": "game is over"}), 400

    # --- Clock: deduct the human's elapsed time (fairness rule) --------------
    # The client reports how long the human took THIS move (his clock runs only
    # from when Chess Amateur's animation ended to when he moved). We clamp it
    # to [0, remaining] and enforce flagging. Unlimited -> no clock.
    clock = data.get("clock")
    if not unlimited:
        if not isinstance(clock, dict):
            clock = {"white": float(base_seconds), "black": float(base_seconds)}
        try:
            elapsed = float(data.get("elapsed", 0.0))
        except (TypeError, ValueError):
            elapsed = 0.0
        elapsed = max(0.0, elapsed)
        human_remaining = float(clock.get(human_color, base_seconds))
        if elapsed >= human_remaining:
            # Human flagged: loss on time. Finalize immediately.
            clock[human_color] = 0.0
            result = _human_is_loser_result(human_color)
            rating_delta = None
            if user:
                rating_delta = _apply_result_and_rating(
                    user, mode, human_color, base_seconds, increment, result)
                STORE.finish_game(
                    user_id=user["id"], game_id=gid, human_color=human_color,
                    result=result, result_reason="time forfeit",
                    moves_uci=moves_uci, started_at=started_at,
                    ended_at=_now_iso(), base_seconds=base_seconds,
                    increment=increment, rating_delta=rating_delta)
            extra = _move_extra(user, mode, base_seconds, increment, started_at,
                                clock, in_progress=False, game_over=True,
                                rating_delta=rating_delta,
                                result=result, result_reason="time forfeit")
            return jsonify(core.state_dict(
                board, human_color, threads, san_history, moves_uci,
                game_id=gid, last_bot_move=None, extra=extra))
        # Normal deduction + increment for the human's move.
        clock[human_color] = human_remaining - elapsed + increment

    # --- Validate + apply the human move ------------------------------------
    try:
        move = core.chess.Move.from_uci(str(move_uci))
    except (ValueError, TypeError):
        return jsonify({"error": "illegal move"}), 400
    if move not in board.legal_moves:
        return jsonify({"error": "illegal move"}), 400
    human_san = board.san(move)
    board.push(move)
    san_history.append(human_san)
    moves_uci.append(move.uci())

    # --- Engine reply if the game continues ---------------------------------
    last_bot_move = None
    bot_think = 0.0
    if not board.is_game_over(claim_draw=True):
        # FIDE mode: the human-like TIME MANAGER decides how long Chess Amateur
        # "thinks" (via the external model + scaling); that time is deducted
        # from its clock. Rated/Casual: it moves instantly (no time manager).
        if mode == "fide" and not unlimited:
            bot_remaining = float(clock.get(bot_color, base_seconds))
            bot_think = clockclient.thinking_time(
                board.fen(), moves_uci, base_seconds, increment,
                bot_remaining, float(clock.get(human_color, base_seconds)))
            if bot_think >= bot_remaining:
                # Engine flags (rare, safety): human wins on time.
                clock[bot_color] = 0.0
                result = "1-0" if human_color == "white" else "0-1"
                rating_delta = None
                if user:
                    rating_delta = _apply_result_and_rating(
                        user, mode, human_color, base_seconds, increment, result)
                    STORE.finish_game(
                        user_id=user["id"], game_id=gid, human_color=human_color,
                        result=result, result_reason="time forfeit",
                        moves_uci=moves_uci, started_at=started_at,
                        ended_at=_now_iso(), base_seconds=base_seconds,
                        increment=increment, rating_delta=rating_delta)
                extra = _move_extra(user, mode, base_seconds, increment,
                                    started_at, clock, in_progress=False,
                                    game_over=True, rating_delta=rating_delta,
                                    result=result, result_reason="time forfeit")
                return jsonify(core.state_dict(
                    board, human_color, threads, san_history, moves_uci,
                    game_id=gid, last_bot_move=None, extra=extra))
        try:
            uci, san = core.engine_move(board, threads=threads)
        except core.EngineUnavailable:
            return jsonify({"error": "engine unavailable"}), 500
        if uci:
            san_history.append(san)
            moves_uci.append(uci)
            last_bot_move = {"uci": uci, "san": san}
            if not unlimited:
                bot_prev = float(clock.get(bot_color, base_seconds))
                clock[bot_color] = bot_prev - bot_think + increment

    game_over = board.is_game_over(claim_draw=True)

    # --- Ratings + persistence ----------------------------------------------
    rating_delta = None
    if user:
        if game_over:
            result = board.result(claim_draw=True)
            rating_delta = _apply_result_and_rating(
                user, mode, human_color, base_seconds, increment, result)
            STORE.finish_game(
                user_id=user["id"], game_id=gid, human_color=human_color,
                result=result, result_reason=core.result_reason(board),
                moves_uci=moves_uci, started_at=started_at,
                ended_at=_now_iso(), base_seconds=base_seconds,
                increment=increment, rating_delta=rating_delta)
        else:
            gid = _persist_in_progress(user["id"], gid, human_color,
                                       moves_uci, started_at,
                                       base_seconds=base_seconds,
                                       increment=increment, clock=clock)

    extra = _move_extra(user, mode, base_seconds, increment, started_at, clock,
                        in_progress=bool(user) and not game_over,
                        game_over=game_over, rating_delta=rating_delta)
    return jsonify(core.state_dict(board, human_color, threads, san_history,
                                   moves_uci, game_id=gid,
                                   last_bot_move=last_bot_move, extra=extra))


def _move_extra(user, mode, base_seconds, increment, started_at, clock,
                in_progress, game_over, rating_delta=None,
                result=None, result_reason=None):
    """Assemble the `extra` fields carried in a move/new response.

    For a TIME-FORFEIT / flag ending (result passed in), the game is over by
    the CLOCK, not the board -- so we force the terminal fields here. These
    keys override the board-derived values in state_dict (extra is merged
    last), so the client sees game_over/result/result_reason correctly even
    though the board position itself is not checkmate/stalemate.
    """
    extra = {
        "started_at": started_at, "logged_in": bool(user), "mode": mode,
        "base_seconds": base_seconds, "increment": increment,
        "unlimited": base_seconds is None, "clock": clock,
        "in_progress": in_progress,
        "time_class": R.time_class(base_seconds, increment),
        "rating_delta": rating_delta,
    }
    if result is not None:
        extra["game_over"] = True
        extra["result"] = result
        extra["result_reason"] = result_reason
        extra["status"] = "game_over"
        extra["legal_moves"] = []
    return extra


@app.post("/api/resign")
def api_resign():
    """Resign the current game (logged-in users). Records a loss for the human
    with result_reason 'resignation' and ended_at to the second."""
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in to resign a saved game."}), 401
    ip = STORE.get_in_progress_game(user["id"])
    if ip is None:
        return jsonify({"error": "No game in progress."}), 400
    mode = _parse_mode(request.get_json(silent=True).get("mode")
                       if request.get_json(silent=True) else None)
    result = _human_is_loser_result(ip["human_color"])
    rating_delta = _apply_result_and_rating(
        user, mode, ip["human_color"], ip["base_seconds"], ip["increment"],
        result)
    STORE.finish_game(
        user_id=user["id"], game_id=ip["id"], human_color=ip["human_color"],
        result=result, result_reason="resignation",
        moves_uci=ip["moves"], started_at=ip["started_at"],
        ended_at=_now_iso(), base_seconds=ip["base_seconds"],
        increment=ip["increment"], rating_delta=rating_delta)
    return jsonify({"ok": True, "result": result,
                    "result_reason": "resignation",
                    "rating_delta": rating_delta})


@app.post("/api/view")
def api_view():
    """Read-only: rebuild a position from a move list and return its state.

    Used by the frontend to render a resumed in-progress game or replay a
    finished one WITHOUT mutating anything or invoking the engine. Safe for
    guests and logged-in users alike.
    """
    data = request.get_json(silent=True) or {}
    human_color = str(data.get("human_color", "white")).lower()
    if human_color not in ("white", "black"):
        human_color = "white"
    try:
        board, san_history, moves_uci = core.rebuild_board(data.get("moves"))
    except core.InvalidMoveHistory:
        return jsonify({"error": "invalid move history"}), 400
    threads = core.coerce_threads(data.get("threads"))
    return jsonify(core.state_dict(board, human_color, threads, san_history,
                                   moves_uci, game_id=data.get("game_id"),
                                   last_bot_move=None))


@app.get("/api/in-progress")
def api_in_progress():
    """Return the user's current in-progress game (for resume), or null."""
    user = _current_user()
    if not user:
        return jsonify({"in_progress": None})
    ip = STORE.get_in_progress_game(user["id"])
    if ip is None:
        return jsonify({"in_progress": None})
    return jsonify({"in_progress": {
        "id": ip["id"], "human_color": ip["human_color"],
        "moves": ip["moves"], "started_at": ip["started_at"],
        "base_seconds": ip["base_seconds"], "increment": ip["increment"],
        "unlimited": ip["base_seconds"] is None,
        "time_class": R.time_class(ip["base_seconds"], ip["increment"]),
        "clock": ip.get("clock"),
    }})


@app.get("/api/games")
def api_games():
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    return jsonify({"games": STORE.list_games(user["id"])})


@app.get("/api/games/<int:game_id>")
def api_game(game_id):
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    g = STORE.get_game(user["id"], game_id)
    if not g:
        abort(404)
    return jsonify({"game": g})


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "bot": core.BOT_NAME,
                    "accounts_enabled": STORE.enabled})


def main():
    port = int(os.environ.get("PORT", "8000"))
    app.logger.info("%s (Flask) starting on 0.0.0.0:%d | accounts_enabled=%s "
                    "| default threads=%d",
                    core.BOT_NAME, port, STORE.enabled, core.DEFAULT_GAME_THREADS)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
