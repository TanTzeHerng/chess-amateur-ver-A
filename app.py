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
import logging
import os
import secrets

from flask import (Flask, request, session, jsonify, redirect, url_for,
                   render_template, abort)

import chess_core as core
import auth
import ratings as R
import timecontrol as TC
import clockclient
import eco
import fide
import puzzles as P
import syzygy
from storage import Store

# Default seed rating for any FIDE time control the player does not hold (or
# when there is no FIDE ID / the scrape fails). Matches the users table default.
FIDE_DEFAULT_RATING = 1400

# FEAT-009 / follow-up FEAT-001: version tag for the onboarding demo content.
# NOTE: the demo is now NEW-USERS-ONLY, gated by the per-user demo_pending flag
# (set TRUE only at registration, cleared on POST /api/demo/seen). This constant
# NO LONGER gates whether the demo is shown, and bumping it MUST NOT re-trigger
# the demo for existing users. It is retained only as a harmless content tag
# (still recorded in demo_seen_version for back-compat). The demo CONTENT lives
# in the frontend (static/app.js); keep it BRIEF.
CURRENT_DEMO_VERSION = 1

app = Flask(__name__)

# Make module-level loggers (fide.py / auth.py use logging.getLogger(__name__))
# actually reach stdout under gunicorn on Render. Without any root logging
# configuration those INFO/WARNING records are dropped, which is why the
# "FIDE fetch ..." / "parsed ratings=..." lines never showed up in the deploy
# logs even though the code that emits them may have run. We attach a single
# StreamHandler to the root logger at INFO exactly once (guarded so repeated
# imports / multiple gunicorn workers do not stack duplicate handlers, and so
# we do not clobber a handler an operator/gunicorn already installed). This is
# diagnostics only: it changes NO seeding/auth behavior.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
else:  # a handler already exists (e.g. gunicorn) -- just ensure INFO passes
    logging.getLogger().setLevel(
        min(logging.getLogger().level or logging.INFO, logging.INFO))
# Belt-and-suspenders: make sure the fide module logger emits at INFO and
# propagates to the root handler above, regardless of root level juggling.
logging.getLogger("fide").setLevel(logging.INFO)

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

# Kick off the ~386 MB Syzygy tablebase download in a BACKGROUND daemon thread
# at import/boot. This is deliberately NON-blocking: gunicorn binds the port
# immediately (so Render never reports 'No open ports detected') while the
# download proceeds in the background under the single worker. The frontend
# polls GET /api/tablebase-status for progress. A no-op when SYZYGY_DISABLE=1.
syzygy.start_background_download()


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


def _eco_for(moves_uci):
    """Classify a game's move list to an ECO opening. Returns (code, name) or
    (None, None) when nothing matches (too short / off-book)."""
    hit = eco.classify(moves_uci)
    if not hit:
        return None, None
    return hit.get("eco"), hit.get("name")


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
        # Refresh the in-memory user dict so any post-update display_ratings()
        # (in _move_extra / the resign+in-progress responses) reports the NEW
        # rating, not the pre-game value the request read at start.
        user["fide_%s" % tclass] = new_rating
        # FEAT-010: record the new FIDE-class rating on the dashboard series.
        STORE.append_rating_history(
            user["id"], "fide_%s" % tclass, new_rating, _now_iso())
        return R.format_delta(delta, suffix="FIDE %s" % tclass)

    # rated (Glicko-2)
    new_r, new_rd, new_vol, delta = R.glicko2_update(
        user["rated_rating"], user["rated_rd"], user["rated_vol"],
        R.CHESS_AMATEUR_RATED, R.CHESS_AMATEUR_RATED_RD, score)
    STORE.update_rated_rating(user["id"], new_r, new_rd, new_vol)
    # Refresh the in-memory user dict (see FIDE note above).
    user["rated_rating"] = new_r
    user["rated_rd"] = new_rd
    user["rated_vol"] = new_vol
    # FEAT-010: record the new Rated rating on the dashboard series.
    STORE.append_rating_history(user["id"], "rated", new_r, _now_iso())
    return R.format_delta(delta)


def _player_rating_after(user, mode, base_seconds, increment):
    """Return the player's rating AFTER a finished rated/FIDE game, for
    persistence in games.player_rating_after (shown in My Games).

    Call this AFTER _apply_result_and_rating, which refreshes the in-memory
    user dict with the new rating; display_ratings then reports that post-game
    value for the game's mode + time class. Returns None for casual/guest.
    """
    player_rating, _bot_rating = R.display_ratings(
        user, mode, base_seconds, increment)
    return player_rating


def _seed_fide_ratings(user_id, fide_id, scraper=fide.lookup_ratings):
    """Seed a newly-registered user's three FIDE ratings from their FIDE ID.

    For each of classical/rapid/blitz: use the scraped rating when present,
    else default to 1400. Stores the fide_id on the user. NEVER blocks signup:
    any scrape failure (bad id, network error, unparseable page) yields all
    three at 1400. ``scraper`` is injectable so tests can stub the network.

    Returns the seeded {classical, rapid, blitz} dict actually written.
    """
    # Diagnostics (app.logger is reliably visible under gunicorn on Render):
    # announce that seeding was ENTERED so we can distinguish "reached the
    # scraper" from "skipped before it". No sensitive data -- a FIDE ID is
    # public.
    app.logger.info("seeding FIDE for user=%s id=%s", user_id, fide_id)
    if fide_id:
        STORE.set_fide_id(user_id, fide_id)
    ratings = {"classical": None, "rapid": None, "blitz": None}
    if fide_id:
        try:
            scraped = scraper(fide_id) or {}
        except Exception:
            # Log the ACTUAL exception + traceback rather than swallowing it,
            # so a scrape failure is diagnosable instead of silently yielding
            # all-1400. Still non-blocking: signup proceeds with defaults.
            app.logger.exception(
                "seeding FIDE for user=%s id=%s: scraper raised", user_id,
                fide_id)
            scraped = {}
        app.logger.info("seeding FIDE for user=%s id=%s: scraped=%s",
                        user_id, fide_id, scraped)
        for k in ratings:
            ratings[k] = scraped.get(k)
    seeded = {}
    for tclass in ("classical", "rapid", "blitz"):
        value = ratings.get(tclass)
        rating = value if value is not None else FIDE_DEFAULT_RATING
        seeded[tclass] = rating
        STORE.update_fide_rating(user_id, tclass, rating)
    app.logger.info("seeding FIDE for user=%s id=%s: seeded=%s",
                    user_id, fide_id, seeded)
    return seeded


def _clean_fide_id(value):
    """Normalize a submitted FIDE ID to a digit string, or None. Rejects
    anything with non-digit content so a typo does not trigger a bogus scrape."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s if s.isdigit() else None


# ==========================================================================
# Pages
# ==========================================================================

@app.get("/")
def index():
    user = _current_user()
    # FEAT-001 (follow-up): the onboarding demo is NEW-USERS-ONLY. Show it only
    # when the logged-in user still has demo_pending set (TRUE only for freshly
    # registered accounts, cleared once the demo is dismissed). Pre-existing /
    # migrated accounts default to False, and guests (user is None) never see
    # it. A CURRENT_DEMO_VERSION bump does NOT re-trigger it.
    show_demo = bool(user) and user.get("demo_pending", False)
    return render_template(
        "index.html",
        bot_name=core.BOT_NAME,
        user=user,
        accounts_enabled=STORE.enabled,
        default_threads=core.DEFAULT_GAME_THREADS,
        show_demo=show_demo,
        current_demo_version=CURRENT_DEMO_VERSION,
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
    collections = STORE.list_collections(user["id"])
    game_collections = STORE.list_game_collection_ids(user["id"])
    # Embed each game's collection membership so the tree UI can filter without
    # an extra round-trip. Keys are strings once JSON-encoded in the template.
    for g in games:
        g["collections"] = game_collections.get(g["id"], [])
    return render_template("history.html", bot_name=core.BOT_NAME,
                           user=user, games=games, collections=collections)


# ==========================================================================
# Auth API
# ==========================================================================

@app.post("/api/register")
def api_register():
    if not STORE.enabled:
        return jsonify({"error": "Accounts are unavailable right now."}), 503
    data = request.get_json(silent=True) or {}
    user, err = auth.register_user(STORE, data.get("username"),
                                   data.get("password"),
                                   email=data.get("email"))
    if err:
        return jsonify({"error": err}), 400
    # FEAT-001 (follow-up): the onboarding demo is NEW-USERS-ONLY. Mark the demo
    # as pending for this freshly-registered account (both the bcrypt-local and
    # Supabase paths return through here). Pre-existing / migrated rows keep the
    # migration default False, so they are NEVER shown the demo. Never set on
    # login. Best-effort: a storage hiccup must not block signup.
    try:
        STORE.set_demo_pending(user["id"], True)
    except Exception:  # pragma: no cover - defensive; demo flag is non-critical
        app.logger.warning("Failed to set demo_pending for user %s", user["id"])
    # Seed FIDE ratings from an optional FIDE ID (present -> scraped value,
    # absent/unrated -> 1400). Never blocks signup on a scrape failure.
    #
    # Diagnostics: log the RAW submitted fide_id and the _clean_fide_id result
    # so a deploy can tell three cases apart: (a) the client never sent the
    # key, (b) it was sent but rejected as non-digit, (c) it was accepted. A
    # FIDE ID is public data -- safe to log; we do NOT log username/password/
    # email values. Emitted via app.logger (INFO), which is reliably visible in
    # Render's gunicorn log stream.
    _raw_fide_id = data.get("fide_id")
    _has_fide_key = "fide_id" in data
    fide_id = _clean_fide_id(_raw_fide_id)
    if not _has_fide_key:
        app.logger.info("register: no fide_id key in request body")
    elif fide_id is None:
        app.logger.info(
            "register: raw fide_id=%r -> cleaned=None (rejected: not a digit "
            "string), seeding skipped", _raw_fide_id)
    else:
        app.logger.info("register: raw fide_id=%r -> cleaned=%r (accepted)",
                        _raw_fide_id, fide_id)
    if fide_id:
        try:
            _seed_fide_ratings(user["id"], fide_id)
        except Exception:  # pragma: no cover - defensive; seeding is best-effort
            # Log the actual exception + traceback, not just a generic warning,
            # so the failure point is visible. Still non-blocking.
            app.logger.exception("FIDE seeding failed for user %s", user["id"])
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


@app.post("/api/forgot-password")
def api_forgot_password():
    """Trigger a password-reset email via Supabase (which sends the email).

    Delegates entirely to Supabase Auth when configured. When Supabase is
    unconfigured (bcrypt-local mode) this is a clear no-op: there is no email
    delivery in the local path, so we return a 501 with a plain message rather
    than pretending. Never reveals whether an account exists when configured.
    """
    if not auth.supabase_configured():
        return jsonify({
            "error": "Password reset by email is unavailable on this server."
        }), 501
    data = request.get_json(silent=True) or {}
    identifier = (data.get("email") or data.get("username") or "").strip()
    if not identifier:
        return jsonify({"error": "Enter your username or email."}), 400
    redirect_to = data.get("redirect_to")
    ok, err = auth.send_password_reset(identifier, redirect_to=redirect_to,
                                       store=STORE)
    if not ok:
        return jsonify({"error": err or "Password reset is unavailable."}), 503
    # Uniform success response (does not leak whether the account exists).
    return jsonify({"ok": True,
                    "message": "If that account exists, a reset email is on its way."})


@app.post("/api/delete-account")
def api_delete_account():
    """Delete the logged-in user's account and all of their games.

    Requires an authenticated session (401 otherwise). The client confirms
    intent before calling this. Games are removed via the ON DELETE CASCADE FK
    (and explicitly by delete_user for SQLite). The session is cleared on
    success so the (now non-existent) user is logged out.
    """
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    # If Supabase owns identity, also delete the Supabase auth user (service
    # key). Guarded: a no-op when Supabase/service key is unconfigured, so
    # local (bcrypt) deletion keeps working unchanged. Look up the linked
    # Supabase id BEFORE removing the local row.
    if auth.supabase_admin_configured():
        try:
            row = STORE._execute(
                "SELECT supabase_user_id FROM users WHERE id = %s"
                % STORE._placeholder(), (user["id"],), fetch="one")
            sb_uid = row[0] if row else None
            if sb_uid:
                auth.delete_supabase_user(sb_uid)
        except Exception:  # pragma: no cover - never block local deletion
            app.logger.warning("Supabase user deletion failed for %s",
                               user["id"])
    STORE.delete_user(user["id"])
    session.clear()
    return jsonify({"ok": True})


# ==========================================================================
# Onboarding demo API (FEAT-009)
# ==========================================================================

@app.get("/api/demo")
def api_demo():
    """Return whether the logged-in user should be shown the onboarding demo.

    Requires an authenticated session (401 otherwise). FEAT-001 (follow-up):
    `show` is driven by the per-user demo_pending flag (TRUE only for freshly
    registered accounts, cleared on POST /api/demo/seen), so the demo is shown
    EXACTLY ONCE and NEVER re-shown on a version bump. The current_version /
    seen_version fields are kept for back-compat only.
    """
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    return jsonify({
        "current_version": CURRENT_DEMO_VERSION,
        "seen_version": user.get("demo_seen_version", 0),
        "show": user.get("demo_pending", False),
    })


@app.post("/api/demo/seen")
def api_demo_seen():
    """Mark the onboarding demo as seen for the logged-in user.

    FEAT-001 (follow-up): clears the one-shot demo_pending flag so the demo is
    NEVER shown again (new-users-only). Also bumps demo_seen_version for
    back-compat (harmless; it no longer gates showing the demo). Requires an
    authenticated session (401 otherwise).
    """
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    STORE.set_demo_pending(user["id"], False)
    STORE.set_demo_seen_version(user["id"], CURRENT_DEMO_VERSION)
    return jsonify({"ok": True, "seen_version": CURRENT_DEMO_VERSION})


# ==========================================================================
# Collections API (FEAT-006) -- organize My Games into nestable folders.
#
# Every endpoint requires an authenticated session (401 otherwise) and the
# storage layer enforces that the target collection/game belongs to the
# logged-in user (so a user can never read or modify another user's folders).
#
# Request/response contracts:
#   GET    /api/collections
#       -> 200 {"collections": [ {id, parent_id, name, created_at}, ... ]}
#          Flat rows for the user; the client assembles the tree via parent_id.
#   POST   /api/collections            {name, parent_id?}
#       -> 200 {"collection": {id, parent_id, name}}  on success
#       -> 400 {"error": ...} for a missing name or a parent_id the user does
#          not own.
#   PATCH  /api/collections/<id>       {name}   (POST is accepted as an alias)
#       -> 200 {"ok": true} | 404 {"error": ...} if not owned / not found.
#   DELETE /api/collections/<id>
#       -> 200 {"ok": true} (cascades subfolders + memberships) | 404.
#   POST   /api/collections/<id>/games {game_id}
#       -> 200 {"ok": true} | 400 {"error": ...} if the collection or game is
#          not owned by the user.
#   DELETE /api/collections/<id>/games/<game_id>
#       -> 200 {"ok": true} | 404 {"error": ...} if the membership was absent
#          or not owned.
# ==========================================================================

@app.get("/api/collections")
def api_list_collections():
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    return jsonify({"collections": STORE.list_collections(user["id"])})


@app.post("/api/collections")
def api_create_collection():
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "A collection name is required."}), 400
    parent_id = data.get("parent_id")
    cid = STORE.create_collection(user["id"], name, parent_id=parent_id)
    if cid is None:
        return jsonify({"error": "Could not create the collection."}), 400
    return jsonify({"collection": {"id": cid, "parent_id": parent_id,
                                   "name": name}})


@app.route("/api/collections/<int:collection_id>", methods=["PATCH", "POST"])
def api_rename_collection(collection_id):
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "A collection name is required."}), 400
    if not STORE.rename_collection(user["id"], collection_id, name):
        return jsonify({"error": "Collection not found."}), 404
    return jsonify({"ok": True})


@app.delete("/api/collections/<int:collection_id>")
def api_delete_collection(collection_id):
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    if not STORE.delete_collection(user["id"], collection_id):
        return jsonify({"error": "Collection not found."}), 404
    return jsonify({"ok": True})


@app.post("/api/collections/<int:collection_id>/games")
def api_add_game_to_collection(collection_id):
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    data = request.get_json(silent=True) or {}
    game_id = data.get("game_id")
    if game_id is None:
        return jsonify({"error": "A game_id is required."}), 400
    if not STORE.add_game_to_collection(user["id"], collection_id, game_id):
        return jsonify({"error": "Could not add the game to that collection."}), 400
    return jsonify({"ok": True})


@app.delete("/api/collections/<int:collection_id>/games/<int:game_id>")
def api_remove_game_from_collection(collection_id, game_id):
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    if not STORE.remove_game_from_collection(user["id"], collection_id, game_id):
        return jsonify({"error": "Membership not found."}), 404
    return jsonify({"ok": True})


# ==========================================================================
# Puzzle API (Train tab, FEAT-008)
#
# A logged-in user has ONE currently-assigned puzzle, persisted on their row
# (users.assigned_puzzle_id) so a page reload shows the SAME puzzle until it is
# solved or failed. Puzzles come from the curated `puzzles` table (FEAT-007).
#
# Lichess puzzle format: `fen` is the position BEFORE the opponent's setup
# move; `moves` is the space-separated UCI line where moves[0] is the
# opponent's setup move and the SOLVER plays moves at odd indices (1, 3, 5...).
# We validate the solver's move with python-chess against the known solution
# (NO engine needed -> the single-Stockfish invariant is preserved).
#
#   GET  /api/puzzle
#       -> 200 {"puzzle": {id, fen, solver_color, displayed_rating,
#                          rating (player puzzle rating), ...}} (assigns one via
#          the bell-curve sampler if none is assigned, and persists it). The
#          SOLUTION moves are NOT returned up front.
#       -> 200 {"puzzle": null, ...} if there are no curated puzzles.
#       -> 401 if not signed in.
#   POST /api/puzzle/move   {move: "e2e4", index: <solver ply index>}
#       -> 200 with the outcome of the submitted move. On a wrong move the
#          puzzle is FAILED; on the last correct solver move it is SOLVED. In
#          both terminal cases the puzzle Glicko-2 rating updates vs the
#          puzzle's LICHESS rating, the assignment is cleared, and the new
#          puzzle rating is returned. A correct-but-not-final move returns the
#          opponent's auto-reply so the client can animate it.
#       -> 401 if not signed in.
# ==========================================================================


def _puzzle_public(puzzle, user):
    """Public puzzle payload for the client. Includes the position AFTER the
    opponent's setup move (the position the solver actually sees), the solver's
    color, and the DISPLAYED rating (lichess - 600). Does NOT include the
    solution moves."""
    import chess
    board = chess.Board(puzzle["fen"])
    setup = puzzle["moves"][0]
    board.push(chess.Move.from_uci(setup))
    return {
        "id": puzzle["id"],
        # Position the solver is presented with (after the opponent's setup
        # move) plus the raw setup move so the client can animate it in.
        "fen": board.fen(),
        "setup_fen": puzzle["fen"],
        "setup_move": setup,
        "solver_color": "white" if board.turn == chess.WHITE else "black",
        "turn": "white" if board.turn == chess.WHITE else "black",
        # First solver ply the client should submit (index 1 in the UCI line).
        "next_index": 1,
        "displayed_rating": R.puzzle_displayed_rating(puzzle["lichess_rating"]),
        "player_rating": round(float(user["puzzle_rating"])),
    }


def _assign_puzzle(user):
    """Pick a puzzle via the bell-curve sampler and persist it as the user's
    current assignment. Returns the puzzle dict, or None if the curated table
    is empty."""
    extent = STORE.puzzle_rating_extent()
    if extent[0] is None:
        return None
    puzzle = P.sample_puzzle(
        float(user["puzzle_rating"]),
        fetch_band=lambda low, high: STORE.fetch_puzzles_in_rating_band(
            low, high, limit=50),
        extent=extent)
    if puzzle is None:
        return None
    STORE.set_assigned_puzzle(user["id"], puzzle["id"])
    return puzzle


@app.get("/api/puzzle")
def api_puzzle():
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    puzzle = STORE.get_assigned_puzzle(user["id"])
    if puzzle is None:
        puzzle = _assign_puzzle(user)
    if puzzle is None:
        return jsonify({"puzzle": None,
                        "player_rating": round(float(user["puzzle_rating"]))})
    return jsonify({"puzzle": _puzzle_public(puzzle, user),
                    "player_rating": round(float(user["puzzle_rating"]))})


def _finish_puzzle(user, puzzle, solved):
    """Update the player's puzzle Glicko-2 rating vs the puzzle's LICHESS
    rating (win if solved, loss if failed), clear the assignment, and return
    the new (rounded) puzzle rating."""
    new_r, new_rd, new_vol, _delta = R.puzzle_rating_update(
        user["puzzle_rating"], user["puzzle_rd"], user["puzzle_vol"],
        puzzle["lichess_rating"], solved)
    STORE.update_puzzle_rating(user["id"], new_r, new_rd, new_vol)
    STORE.set_assigned_puzzle(user["id"], None)
    # Refresh the in-memory user so a follow-up read sees the new rating.
    user["puzzle_rating"] = new_r
    user["puzzle_rd"] = new_rd
    user["puzzle_vol"] = new_vol
    # FEAT-010: record the new puzzle rating on the dashboard series.
    STORE.append_rating_history(user["id"], "puzzle", new_r, _now_iso())
    return round(float(new_r))


@app.post("/api/puzzle/move")
def api_puzzle_move():
    import chess
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    data = request.get_json(silent=True) or {}
    move_uci = data.get("move")
    puzzle = STORE.get_assigned_puzzle(user["id"])
    if puzzle is None:
        return jsonify({"error": "No puzzle assigned."}), 400

    line = puzzle["moves"]
    try:
        index = int(data.get("index", 1))
    except (TypeError, ValueError):
        index = 1
    # The solver plays at odd indices (1, 3, 5, ...); index 0 is the opponent's
    # setup move. Anything else is a bad request.
    if index < 1 or index % 2 == 0 or index >= len(line):
        return jsonify({"error": "Invalid solver move index."}), 400

    expected = line[index]
    correct = bool(move_uci) and (str(move_uci) == expected)

    if not correct:
        # Wrong move -> puzzle FAILED. Rating drops vs the puzzle's rating.
        new_rating = _finish_puzzle(user, puzzle, solved=False)
        return jsonify({
            "correct": False, "solved": False, "failed": True,
            "expected": expected,
            "puzzle_rating": new_rating,
        })

    # Correct solver move. Is there an opponent reply after it (index+1)?
    reply_index = index + 1
    if reply_index >= len(line):
        # No further moves -> the puzzle is fully SOLVED.
        new_rating = _finish_puzzle(user, puzzle, solved=True)
        return jsonify({
            "correct": True, "solved": True, "failed": False,
            "puzzle_rating": new_rating,
        })

    # Auto-play the opponent's reply and hand the client the next solver index.
    reply = line[reply_index]
    next_index = reply_index + 1
    done = next_index >= len(line)
    resp = {
        "correct": True, "solved": False, "failed": False,
        "opponent_move": reply,
        "next_index": next_index if not done else None,
    }
    if done:
        # The opponent's reply was the final move -> solved after it.
        resp["solved"] = True
        resp["puzzle_rating"] = _finish_puzzle(user, puzzle, solved=True)
    return jsonify(resp)


@app.get("/api/me")
def api_me():
    user = _current_user()
    return jsonify({
        "user": user,
        "accounts_enabled": STORE.enabled,
        "bot_name": core.BOT_NAME,
    })


# The rating series the dashboard renders, in a stable display order.
DASHBOARD_KINDS = ("fide_blitz", "fide_rapid", "fide_classical", "rated",
                   "puzzle")


def _clean_date_bound(value, end_of_day=False):
    """Normalize a from/to query param (YYYY-MM-DD) into a comparable string.

    Returns None for a missing/blank value. For a `to` bound we append the
    end-of-day time so the whole day is inclusive against second-precision
    stored timestamps. Anything not shaped like a date is ignored (None)."""
    if not value:
        return None
    s = str(value).strip()
    if len(s) < 10:
        return None
    day = s[:10]
    # Cheap shape check: YYYY-MM-DD.
    if day[4] != "-" or day[7] != "-":
        return None
    if not (day[:4].isdigit() and day[5:7].isdigit() and day[8:10].isdigit()):
        return None
    return day + "T23:59:59" if end_of_day else day


# GMT+8 (Asia/Singapore, UTC+8, no DST) as a fixed offset. The dashboard buckets
# rating history into calendar days at 00:00 GMT+8 boundaries.
GMT8 = datetime.timezone(datetime.timedelta(hours=8))


def _parse_utc(at):
    """Parse a rating_history `at` string into an aware UTC datetime.

    Handles the shapes storage produces on both backends: ISO with a trailing
    'Z' or '+00:00' offset, a space OR 'T' date/time separator, and
    second-precision (with or without fractional seconds). A naive timestamp is
    assumed to already be UTC. Returns None if it cannot be parsed."""
    if at is None:
        return None
    s = str(at).strip()
    if not s:
        return None
    s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        # Fall back to bare second precision without an offset.
        try:
            dt = datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _parse_gmt8_day(day):
    """Parse a 'YYYY-MM-DD' string into a datetime.date, or None."""
    if not day:
        return None
    try:
        return datetime.date.fromisoformat(str(day)[:10])
    except ValueError:
        return None


def daily_series_gmt8(points, start_date, end_date):
    """Derive a per-day rating series at GMT+8 00:00 boundaries (read-only).

    `points` is the raw rating_history for ONE series: an iterable of
    {"at": <UTC ISO-ish string>, "rating": <number>}, ascending by time (order
    is not relied upon; it is sorted defensively). `start_date` and `end_date`
    are inclusive GMT+8 calendar-day bounds as 'YYYY-MM-DD' strings (or
    datetime.date). Returns a list of {"date": 'YYYY-MM-DD', "rating": float}
    with one entry per GMT+8 day in [start_date, end_date].

    For each GMT+8 calendar day D the plotted value is the rating of the LATEST
    event whose instant is AT OR BEFORE that day's 00:00 GMT+8 boundary. That
    boundary instant is D 00:00:00 +08:00 == (D-1) 16:00:00 UTC. A day with no
    new event carries forward the prior known value (last-known-value / step);
    days before the series' first event have NO point (are skipped). This is a
    pure function -- no Flask/DB -- so it is directly unit-testable."""
    if isinstance(start_date, datetime.date):
        start = start_date
    else:
        start = _parse_gmt8_day(start_date)
    if isinstance(end_date, datetime.date):
        end = end_date
    else:
        end = _parse_gmt8_day(end_date)
    if start is None or end is None or end < start:
        return []

    # Normalize events to (utc_instant, rating), ascending.
    events = []
    for p in points or []:
        inst = _parse_utc(p.get("at"))
        if inst is None:
            continue
        try:
            rating = round(float(p["rating"]), 2)
        except (TypeError, ValueError, KeyError):
            continue
        events.append((inst, rating))
    events.sort(key=lambda e: e[0])
    if not events:
        return []

    out = []
    idx = 0
    n = len(events)
    last_rating = None
    have_value = False
    day = start
    one_day = datetime.timedelta(days=1)
    while day <= end:
        # 00:00 GMT+8 of `day` == (day-1) 16:00:00 UTC.
        boundary = datetime.datetime(
            day.year, day.month, day.day, tzinfo=GMT8
        ).astimezone(datetime.timezone.utc)
        # Advance through every event at or before this boundary instant.
        while idx < n and events[idx][0] <= boundary:
            last_rating = events[idx][1]
            have_value = True
            idx += 1
        if have_value:
            out.append({"date": day.isoformat(), "rating": last_rating})
        day += one_day
    return out


@app.get("/api/dashboard")
def api_dashboard():
    """Return the logged-in player's DAILY rating series for the Dashboard.

    Auth required (401 otherwise). Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD
    bounds the selected period as GMT+8 calendar days (inclusive). Presets
    (Past week/month/year) map to from/to client-side; 'All' omits `from` so
    the lower bound becomes the GMT+8 day of the user's EARLIEST rating_history
    event (upper bound defaults to today's GMT+8 day when `to` is absent).

    The durable rating_history event log is UNCHANGED (event-driven; no
    snapshot table). Each series is DERIVED READ-ONLY into per-day points at
    GMT+8 00:00 boundaries via daily_series_gmt8: for every GMT+8 day in the
    window the value is the most recent event at or before that day's midnight
    (last-known-value/step), days with no new event carry forward the prior
    value, and days before a series' first event have no point. Response:
    series[kind] is a list of {date: 'YYYY-MM-DD', rating: <float>} (a shape
    change from the earlier raw {at, rating} event points)."""
    user = _current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401

    from_arg = _clean_date_bound(request.args.get("from"))
    to_arg = _clean_date_bound(request.args.get("to"))

    # Today's GMT+8 calendar day is the default upper bound.
    today_gmt8 = datetime.datetime.now(GMT8).date()
    end_day = _parse_gmt8_day(to_arg) or today_gmt8

    if from_arg:
        start_day = _parse_gmt8_day(from_arg)
    else:
        # 'All': lower bound is the GMT+8 day of the earliest event (if any).
        earliest = STORE.earliest_rating_history_at(user["id"])
        earliest_dt = _parse_utc(earliest) if earliest else None
        start_day = earliest_dt.astimezone(GMT8).date() if earliest_dt else None

    series = {kind: [] for kind in DASHBOARD_KINDS}
    if start_day is not None and end_day is not None and start_day <= end_day:
        # Read the full event log once; derive each series read-only.
        points = STORE.list_rating_history(user["id"])
        by_kind = {kind: [] for kind in DASHBOARD_KINDS}
        for p in points:
            by_kind.setdefault(p["kind"], []).append(p)
        for kind in DASHBOARD_KINDS:
            series[kind] = daily_series_gmt8(
                by_kind.get(kind, []), start_day, end_day)

    return jsonify({
        "from": start_day.isoformat() if start_day is not None else None,
        "to": end_day.isoformat() if (
            start_day is not None and end_day is not None) else None,
        "kinds": list(DASHBOARD_KINDS),
        "series": series,
    })


# ==========================================================================
# Game API
# ==========================================================================

def _persist_in_progress(user_id, gid, human_color, moves_uci, started_at,
                         base_seconds=None, increment=0, clock=None,
                         mode=None, start_fen=None):
    """Create or update the single in-progress game row for a user.

    `clock` is the live {white, black} remaining seconds (or None for
    unlimited) so a resumed game continues with the correct times.
    `mode` (fide/rated/casual) is stored so the My Games Mode filter works for
    ongoing games too. `start_fen` is the custom starting position (casual
    only; None for a standard game) so a resumed custom game replays correctly.
    Returns the game id (existing or newly created).
    """
    cw = clock.get("white") if isinstance(clock, dict) else None
    cb = clock.get("black") if isinstance(clock, dict) else None
    return STORE.upsert_in_progress_game(
        user_id=user_id, game_id=gid, human_color=human_color,
        moves_uci=moves_uci, started_at=started_at,
        base_seconds=base_seconds, increment=increment,
        clock_white=cw, clock_black=cb, mode=mode, start_fen=start_fen)


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

    # Custom starting position (Casual mode ONLY): a pasted FEN or a
    # board-editor-produced FEN. Honored only in casual; ignored otherwise so
    # rated/FIDE games always start from the standard position.
    start_fen = data.get("fen")
    if start_fen is not None and not str(start_fen).strip():
        start_fen = None
    if start_fen is not None and mode != "casual":
        start_fen = None
    if start_fen is not None:
        # Validate the FEN up front so an invalid one is a clear 400.
        try:
            core.board_from_start_fen(start_fen)
        except core.InvalidStartFen as exc:
            return jsonify({"error": str(exc)}), 400

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

    board, san_history, moves_uci = core.rebuild_board([], start_fen=start_fen)
    started_at = _now_iso()
    last_bot_move = None
    # Chess Amateur moves first when it is on move at the start: the human is
    # Black in a standard game, OR (custom position) the side to move is not
    # the human's color. Mode-aware reply so FIDE uses the book-then-fallback.
    bot_to_move_at_start = (core.turn_str(board) != human_color)
    try:
        if bot_to_move_at_start and not board.is_game_over(claim_draw=True):
            uci, san = core.reply_move(board, mode=mode, threads=threads)
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
                                   clock=clock, mode=mode, start_fen=start_fen)

    player_rating, bot_rating = R.display_ratings(
        user, mode, base_seconds, increment)
    extra = {
        "started_at": started_at, "logged_in": bool(user),
        "in_progress": bool(user), "mode": mode,
        "base_seconds": base_seconds, "increment": increment,
        "unlimited": base_seconds is None,
        "clock": clock,
        "time_class": R.time_class(base_seconds, increment),
        "player_rating": player_rating, "bot_rating": bot_rating,
    }
    return jsonify(core.state_dict(board, human_color, threads, san_history,
                                   moves_uci, game_id=gid,
                                   last_bot_move=last_bot_move, extra=extra,
                                   start_fen=start_fen))


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

    # Custom starting position (Casual only), carried statelessly by the client.
    start_fen = data.get("fen") or data.get("start_fen")
    if start_fen is not None and not str(start_fen).strip():
        start_fen = None

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
            # Authoritative mode from the persisted game (fall back to the
            # request's parsed mode for rows written before mode was stored).
            if ip.get("mode"):
                mode = _parse_mode(ip["mode"])
            # Authoritative custom start FEN from the persisted game.
            if ip.get("start_fen"):
                start_fen = ip["start_fen"]

    # A custom start FEN is only meaningful in casual mode.
    if mode != "casual":
        start_fen = None

    # Rebuild authoritative position from carried history (on the custom start).
    try:
        board, san_history, moves_uci = core.rebuild_board(
            data.get("moves"), start_fen=start_fen)
    except core.InvalidStartFen:
        return jsonify({"error": "invalid FEN"}), 400
    except core.InvalidMoveHistory:
        return jsonify({"error": "invalid move history"}), 400

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
                eco_code, eco_name = _eco_for(moves_uci)
                STORE.finish_game(
                    user_id=user["id"], game_id=gid, human_color=human_color,
                    result=result, result_reason="time forfeit",
                    moves_uci=moves_uci, started_at=started_at,
                    ended_at=_now_iso(), base_seconds=base_seconds,
                    increment=increment, rating_delta=rating_delta,
                    eco_code=eco_code, eco_name=eco_name, mode=mode,
                    player_rating_after=_player_rating_after(
                        user, mode, base_seconds, increment))
            extra = _move_extra(user, mode, base_seconds, increment, started_at,
                                clock, in_progress=False, game_over=True,
                                rating_delta=rating_delta,
                                result=result, result_reason="time forfeit")
            return jsonify(core.state_dict(
                board, human_color, threads, san_history, moves_uci,
                game_id=gid, last_bot_move=None, extra=extra,
                start_fen=start_fen))
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
    tb_warming = False
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
                    eco_code, eco_name = _eco_for(moves_uci)
                    STORE.finish_game(
                        user_id=user["id"], game_id=gid, human_color=human_color,
                        result=result, result_reason="time forfeit",
                        moves_uci=moves_uci, started_at=started_at,
                        ended_at=_now_iso(), base_seconds=base_seconds,
                        increment=increment, rating_delta=rating_delta,
                        eco_code=eco_code, eco_name=eco_name, mode=mode,
                        player_rating_after=_player_rating_after(
                            user, mode, base_seconds, increment))
                extra = _move_extra(user, mode, base_seconds, increment,
                                    started_at, clock, in_progress=False,
                                    game_over=True, rating_delta=rating_delta,
                                    result=result, result_reason="time forfeit")
                return jsonify(core.state_dict(
                    board, human_color, threads, san_history, moves_uci,
                    game_id=gid, last_bot_move=None, extra=extra,
                    start_fen=start_fen))
        try:
            # FIDE-rated mode uses the Polyglot book (pc2500.bin) when the
            # position is in it, else falls through to Stockfish depth 1.
            # Rated/Casual never consult the book. push=False so we can DEFER
            # an engine move (tablebase warming) WITHOUT committing a
            # tablebase-absent search result to the game.
            reply = core.reply_move_ex(board, mode=mode, threads=threads,
                                       push=False)
        except core.EngineUnavailable:
            return jsonify({"error": "engine unavailable"}), 500
        uci, san = reply["uci"], reply["san"]

        # TABLEBASE-WARMING DEFERRAL (FEAT-004): if this search was about to
        # probe a tablebase that is not yet fully downloaded, we must NOT use
        # the tablebase-absent result. Book moves never defer (from_book).
        # The bot is still charged EXACTLY the ChessMimic think-time it WOULD
        # be charged if the tablebase were present -- the download WAIT is NOT
        # charged. The move itself is left PENDING (bot move not pushed) until
        # the tablebases are ready, at which point the client re-requests it
        # via /api/resolve-bot-move and a FRESH search resolves it.
        # Deferral (with ChessMimic clock preservation) is scoped to
        # FIDE-with-clock: those are the games that have a time model to
        # preserve. Rated/Casual/unlimited move instantly (no clock model) and
        # are unaffected. Book moves never defer.
        defer = (mode == "fide" and not unlimited
                 and uci is not None
                 and not reply["from_book"]
                 and reply["tb_probe_seen"]
                 and not syzygy.status().get("ready", True))

        if defer:
            tb_warming = True
            # Charge the bot clock EXACTLY as if the move had been made
            # (ChessMimic think-time only; the warming wait is NOT charged).
            if not unlimited:
                bot_prev = float(clock.get(bot_color, base_seconds))
                clock[bot_color] = bot_prev - bot_think + increment
            # last_bot_move stays None; the bot move is NOT pushed. The human
            # move IS applied and persisted; the bot move is pending.
        elif uci:
            # Commit the chosen reply (engine or book) to the game.
            move = core.chess.Move.from_uci(uci)
            board.push(move)
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
            eco_code, eco_name = _eco_for(moves_uci)
            STORE.finish_game(
                user_id=user["id"], game_id=gid, human_color=human_color,
                result=result, result_reason=core.result_reason(board),
                moves_uci=moves_uci, started_at=started_at,
                ended_at=_now_iso(), base_seconds=base_seconds,
                increment=increment, rating_delta=rating_delta,
                eco_code=eco_code, eco_name=eco_name, mode=mode,
                player_rating_after=_player_rating_after(
                    user, mode, base_seconds, increment))
        else:
            gid = _persist_in_progress(user["id"], gid, human_color,
                                       moves_uci, started_at,
                                       base_seconds=base_seconds,
                                       increment=increment, clock=clock,
                                       mode=mode, start_fen=start_fen)

    extra = _move_extra(user, mode, base_seconds, increment, started_at, clock,
                        in_progress=bool(user) and not game_over,
                        game_over=game_over, rating_delta=rating_delta)
    # Tell the client how long Chess Amateur "thought" so it can HOLD the bot's
    # move for that real duration before revealing it (FIDE-mode human pacing).
    # Only meaningful in FIDE mode with a clock; 0 otherwise (instant).
    extra["bot_think"] = bot_think if (mode == "fide" and not unlimited) else 0.0
    # TABLEBASE-WARMING (FEAT-004): when true, the bot move is PENDING because
    # its search was about to probe a not-yet-downloaded tablebase. last_bot_move
    # is None and the client should show the "warming up" window + poll
    # /api/tablebase-status, then call /api/resolve-bot-move once ready. The bot
    # was already charged bot_think (the download wait is NOT charged).
    extra["tb_warming"] = tb_warming
    return jsonify(core.state_dict(board, human_color, threads, san_history,
                                   moves_uci, game_id=gid,
                                   last_bot_move=last_bot_move, extra=extra,
                                   start_fen=start_fen))


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
    player_rating, bot_rating = R.display_ratings(
        user, mode, base_seconds, increment)
    extra = {
        "started_at": started_at, "logged_in": bool(user), "mode": mode,
        "base_seconds": base_seconds, "increment": increment,
        "unlimited": base_seconds is None, "clock": clock,
        "in_progress": in_progress,
        "time_class": R.time_class(base_seconds, increment),
        "rating_delta": rating_delta,
        "player_rating": player_rating, "bot_rating": bot_rating,
    }
    if result is not None:
        extra["game_over"] = True
        extra["result"] = result
        extra["result_reason"] = result_reason
        extra["status"] = "game_over"
        extra["legal_moves"] = []
        # A clock (time-forfeit) ending is not a board game-over, so
        # state_dict would compute result_line=None. Force the canonical
        # winner-phrased tail here (e.g. '1-0 (White won on time)').
        extra["result_line"] = core.result_line(result, result_reason)
    return extra


@app.post("/api/resolve-bot-move")
def api_resolve_bot_move():
    """Resolve a bot move that was DEFERRED because tablebases were warming up.

    STATELESS + client-carried: the request carries the SAME move list that was
    returned by the deferring /api/move response (the human move applied, the
    bot move pending) plus the game context (human_color, mode, threads,
    start_fen). No think-time is (re)computed here: the bot was ALREADY charged
    exactly the single ChessMimic think-time during the deferring /api/move, and
    the client carries the already-decremented clock. This endpoint therefore
    does NOT touch the clock; it only runs a FRESH search once tablebases are
    ready and returns the bot move.

    Behavior:
      * If tablebases are NOT ready yet -> respond tb_warming:true, last_bot_move
        None (the client keeps polling /api/tablebase-status and retries).
      * If ready -> run a FRESH search of the current position (Stockfish is only
        called for a fresh search once tablebases are all downloaded), push the
        bot move, persist in-progress state, and return the updated game state.
    """
    data = request.get_json(silent=True) or {}
    human_color = str(data.get("human_color", "white")).lower()
    if human_color not in ("white", "black"):
        human_color = "white"
    bot_color = "black" if human_color == "white" else "white"
    threads = core.coerce_threads(data.get("threads"))
    mode = _parse_mode(data.get("mode"))
    user = _current_user()
    if mode in ("fide", "rated") and not user:
        mode = "casual"

    base_seconds = data.get("base_seconds")
    increment = data.get("increment") or 0
    if isinstance(base_seconds, bool):
        base_seconds = None
    unlimited = base_seconds is None

    start_fen = data.get("fen") or data.get("start_fen")
    if start_fen is not None and not str(start_fen).strip():
        start_fen = None

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
            if ip.get("mode"):
                mode = _parse_mode(ip["mode"])
            if ip.get("start_fen"):
                start_fen = ip["start_fen"]

    if mode != "casual":
        start_fen = None

    # The clock is carried by the client, already reflecting the bot_think
    # charged by the deferring /api/move. We do NOT modify it here.
    clock = data.get("clock")
    if not unlimited and not isinstance(clock, dict):
        clock = {"white": float(base_seconds), "black": float(base_seconds)}

    try:
        board, san_history, moves_uci = core.rebuild_board(
            data.get("moves"), start_fen=start_fen)
    except core.InvalidStartFen:
        return jsonify({"error": "invalid FEN"}), 400
    except core.InvalidMoveHistory:
        return jsonify({"error": "invalid move history"}), 400

    if board.is_game_over(claim_draw=True):
        return jsonify({"error": "game is over"}), 400

    # Not ready yet: keep the move pending; the client should keep polling.
    if not syzygy.status().get("ready", True):
        extra = _move_extra(user, mode, base_seconds, increment, started_at,
                            clock, in_progress=bool(user), game_over=False)
        extra["bot_think"] = 0.0
        extra["tb_warming"] = True
        return jsonify(core.state_dict(
            board, human_color, threads, san_history, moves_uci, game_id=gid,
            last_bot_move=None, extra=extra, start_fen=start_fen))

    # Tablebases are ready: run a FRESH search of the current position and use
    # its result. No extra think-time is charged (the single ChessMimic
    # bot_think was already applied by the deferring /api/move).
    last_bot_move = None
    try:
        uci, san = core.reply_move(board, mode=mode, threads=threads)
    except core.EngineUnavailable:
        return jsonify({"error": "engine unavailable"}), 500
    if uci:
        san_history.append(san)
        moves_uci.append(uci)
        last_bot_move = {"uci": uci, "san": san}

    game_over = board.is_game_over(claim_draw=True)

    rating_delta = None
    if user:
        if game_over:
            result = board.result(claim_draw=True)
            rating_delta = _apply_result_and_rating(
                user, mode, human_color, base_seconds, increment, result)
            eco_code, eco_name = _eco_for(moves_uci)
            STORE.finish_game(
                user_id=user["id"], game_id=gid, human_color=human_color,
                result=result, result_reason=core.result_reason(board),
                moves_uci=moves_uci, started_at=started_at,
                ended_at=_now_iso(), base_seconds=base_seconds,
                increment=increment, rating_delta=rating_delta,
                eco_code=eco_code, eco_name=eco_name, mode=mode,
                player_rating_after=_player_rating_after(
                    user, mode, base_seconds, increment))
        else:
            gid = _persist_in_progress(user["id"], gid, human_color,
                                       moves_uci, started_at,
                                       base_seconds=base_seconds,
                                       increment=increment, clock=clock,
                                       mode=mode, start_fen=start_fen)

    extra = _move_extra(user, mode, base_seconds, increment, started_at, clock,
                        in_progress=bool(user) and not game_over,
                        game_over=game_over, rating_delta=rating_delta)
    # The bot was already charged its single ChessMimic think-time on the
    # deferring /api/move; nothing more is charged for the fresh search.
    extra["bot_think"] = 0.0
    extra["tb_warming"] = False
    return jsonify(core.state_dict(board, human_color, threads, san_history,
                                   moves_uci, game_id=gid,
                                   last_bot_move=last_bot_move, extra=extra,
                                   start_fen=start_fen))


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
    # Prefer the persisted game mode (authoritative); fall back to the request.
    mode = _parse_mode(ip.get("mode") or (
        request.get_json(silent=True).get("mode")
        if request.get_json(silent=True) else None))
    result = _human_is_loser_result(ip["human_color"])
    rating_delta = _apply_result_and_rating(
        user, mode, ip["human_color"], ip["base_seconds"], ip["increment"],
        result)
    eco_code, eco_name = _eco_for(ip["moves"])
    STORE.finish_game(
        user_id=user["id"], game_id=ip["id"], human_color=ip["human_color"],
        result=result, result_reason="resignation",
        moves_uci=ip["moves"], started_at=ip["started_at"],
        ended_at=_now_iso(), base_seconds=ip["base_seconds"],
        increment=ip["increment"], rating_delta=rating_delta,
        eco_code=eco_code, eco_name=eco_name, mode=mode,
        player_rating_after=_player_rating_after(
            user, mode, ip["base_seconds"], ip["increment"]))
    # _apply_result_and_rating refreshed `user` in place, so display_ratings
    # now reports the POST-update player rating (with the delta shown alongside).
    player_rating, bot_rating = R.display_ratings(
        user, mode, ip["base_seconds"], ip["increment"])
    return jsonify({"ok": True, "result": result,
                    "result_reason": "resignation",
                    "rating_delta": rating_delta,
                    "player_rating": player_rating, "bot_rating": bot_rating,
                    "result_line": core.result_line(result, "resignation"),
                    "eco_code": eco_code, "eco_name": eco_name})


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
    start_fen = data.get("fen") or data.get("start_fen")
    if start_fen is not None and not str(start_fen).strip():
        start_fen = None
    try:
        board, san_history, moves_uci = core.rebuild_board(
            data.get("moves"), start_fen=start_fen)
    except core.InvalidStartFen:
        return jsonify({"error": "invalid FEN"}), 400
    except core.InvalidMoveHistory:
        return jsonify({"error": "invalid move history"}), 400
    threads = core.coerce_threads(data.get("threads"))
    eco_code, eco_name = _eco_for(moves_uci)
    return jsonify(core.state_dict(board, human_color, threads, san_history,
                                   moves_uci, game_id=data.get("game_id"),
                                   last_bot_move=None,
                                   extra={"eco_code": eco_code,
                                          "eco_name": eco_name},
                                   start_fen=start_fen))


@app.get("/api/in-progress")
def api_in_progress():
    """Return the user's current in-progress game (for resume), or null."""
    user = _current_user()
    if not user:
        return jsonify({"in_progress": None})
    ip = STORE.get_in_progress_game(user["id"])
    if ip is None:
        return jsonify({"in_progress": None})
    ip_mode = _parse_mode(ip.get("mode"))
    # Include the ratings so a resumed FIDE/Rated game shows them next to the
    # names immediately (Casual -> None,None, so the client shows nothing).
    player_rating, bot_rating = R.display_ratings(
        user, ip_mode, ip["base_seconds"], ip["increment"])
    return jsonify({"in_progress": {
        "id": ip["id"], "human_color": ip["human_color"],
        "moves": ip["moves"], "started_at": ip["started_at"],
        "base_seconds": ip["base_seconds"], "increment": ip["increment"],
        "unlimited": ip["base_seconds"] is None,
        "time_class": R.time_class(ip["base_seconds"], ip["increment"]),
        "clock": ip.get("clock"),
        "mode": ip.get("mode"),
        "start_fen": ip.get("start_fen"),
        "player_rating": player_rating, "bot_rating": bot_rating,
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


@app.get("/api/tablebase-status")
def tablebase_status():
    """GLOBAL (server-wide) Syzygy download progress for the warming-up UI.

    Returns {ready, downloaded, total, percent}. Single worker, so this is one
    server-wide state (NOT per-user); no auth required. Deliberately decoupled
    from /healthz -- healthz stays instant and never waits on the download.
    """
    return jsonify(syzygy.status())


def main():
    port = int(os.environ.get("PORT", "8000"))
    app.logger.info("%s (Flask) starting on 0.0.0.0:%d | accounts_enabled=%s "
                    "| default threads=%d",
                    core.BOT_NAME, port, STORE.enabled, core.DEFAULT_GAME_THREADS)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
