#!/usr/bin/env python3
"""Self-contained smoke tests for the Chess Amateur backend.

Run: python3 chess_amateur/test_backend.py

Exercises:
  - engine.py returns a legal UCI move for the start position
  - POST /api/new (white and black)
  - POST /api/move with a legal move (engine replies)
  - STATELESS REGRESSION: after clearing the server's in-memory GAMES store
    (simulating a different worker / a cold-started process on Render), a
    POST /api/move that carries the client-side "moves" history still
    SUCCEEDS (HTTP 200, engine replies, correct san_history) instead of 404
  - POST /api/new with a specific "threads" value is honored (returned in
    state) and the engine still returns a legal reply; missing/out-of-range/
    invalid "threads" falls back to the default (128)
  - POST /api/move with an illegal move -> HTTP 400, state unchanged (the
    carried history is unaffected -> no desync)
  - GET /api/state
  - in-progress result fields are None
  - explicit game-over detection: Fool's-mate checkmate reported with the
    correct result ("0-1") and result_reason ("checkmate")
  - /api/move on a finished game -> HTTP 400
  - static index.html served at GET /

Uses only the standard library plus python-chess. Spins the real server on a
throwaway port in a background thread.
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import chess  # noqa: E402
from engine import ChessAmateurEngine  # noqa: E402

PORT = int(os.environ.get("TEST_PORT", "8137"))
BASE = "http://127.0.0.1:%d" % PORT


def _post(path, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(path):
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def start_server():
    os.environ["PORT"] = str(PORT)
    import server  # noqa: E402
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, server


def _live_stockfish_pids():
    """Return the PIDs of currently-alive processes whose comm is 'stockfish'.

    Enumerates /proc directly (pgrep is absent from the slim Docker image).
    """
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry) as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        # /proc/<pid>/comm is truncated to 15 chars, so the local binary
        # named "stockfish-linux-x86-64-universal" reports "stockfish-linux",
        # while the Docker image's /usr/local/bin/stockfish reports
        # "stockfish". Match the shared "stockfish" prefix to cover both.
        if comm.startswith("stockfish"):
            pids.append(int(entry))
    return pids


def test_engine():
    eng = ChessAmateurEngine()
    try:
        start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        mv = eng.best_move(start)
        assert mv is not None, "engine returned no move"
        board = chess.Board(start)
        assert chess.Move.from_uci(mv) in board.legal_moves, "engine move illegal: %s" % mv
        print("PASS engine.best_move ->", mv)
    finally:
        eng.close()


def test_single_process_invariant_across_respawn():
    """Prove the respawn path never leaves two Stockfish processes alive.

    Spawn one engine, capture its PID, then simulate a broken pipe by killing
    the underlying process out from under the wrapper. The next best_move()
    must (a) still return a legal move, (b) do so on a NEW process whose PID
    differs from the dead one, and (c) leave exactly one live 'stockfish'
    process (the old PID must be gone/reaped before/when the new one exists).
    """
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    board = chess.Board(start)
    eng = ChessAmateurEngine(threads=1)
    try:
        mv1 = eng.best_move(start, threads=1)
        assert mv1 is not None and chess.Move.from_uci(mv1) in board.legal_moves, mv1
        old_pid = eng._proc.pid
        assert old_pid in _live_stockfish_pids(), "engine process not found after spawn"

        # Force a respawn: kill the live proc and close its stdin so the next
        # write raises BrokenPipeError/OSError, driving best_move's retry path.
        eng._proc.kill()
        eng._proc.wait(timeout=5)
        try:
            eng._proc.stdin.close()
        except Exception:
            pass

        mv2 = eng.best_move(start, threads=1)
        assert mv2 is not None and chess.Move.from_uci(mv2) in board.legal_moves, mv2
        new_pid = eng._proc.pid
        assert new_pid != old_pid, ("respawn should create a new process", old_pid, new_pid)

        live = _live_stockfish_pids()
        # The dead PID must be gone (terminated + reaped before the respawn).
        assert old_pid not in live, ("old engine still alive after respawn", old_pid, live)
        # This wrapper's engine must have exactly one live process.
        assert new_pid in live, ("new engine not alive", new_pid, live)
        print("PASS single-process invariant across respawn (old pid %d gone, new pid %d alive)"
              % (old_pid, new_pid))
    finally:
        eng.close()


def test_engine_fails_fast_when_binary_broken():
    """A binary that exits immediately must make best_move raise PROMPTLY
    (bounded time) instead of hanging forever on readline().

    This is the regression guard for the Render 502: an incompatible binary
    that dies on launch (SIGILL) used to leave the unbounded readline() loop
    blocking until the platform proxy timed out. Now the bounded/fail-fast
    reads raise EngineUnavailable quickly.
    """
    from engine import EngineUnavailable
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    # /bin/false exits immediately with status 1 and never speaks UCI.
    eng = ChessAmateurEngine(path="/bin/false", threads=1)
    try:
        t0 = time.monotonic()
        raised = False
        try:
            eng.best_move(start)
        except EngineUnavailable:
            raised = True
        elapsed = time.monotonic() - t0
        assert raised, "best_move should raise EngineUnavailable for a broken binary"
        # Must fail fast, not hang. The handshake happens on a dead process so
        # this returns near-instantly (poll() != None); allow generous slack.
        assert elapsed < 15, "best_move took too long to fail fast: %.1fs" % elapsed
        print("PASS engine fails fast on broken binary (raised in %.2fs)" % elapsed)
    finally:
        eng.close()


def test_server_returns_500_when_engine_unavailable():
    """When the engine raises, POST /api/move must return a FAST HTTP 500
    {'error': 'engine unavailable'} rather than hanging (which manifests as a
    proxy 502 on the host)."""
    import server
    saved = server.ENGINE
    from engine import ChessAmateurEngine as _Eng
    server.ENGINE = _Eng(path="/bin/false", threads=1)
    try:
        status, data = _post("/api/move", {
            "game_id": "x", "move": "e2e4",
            "moves": [], "human_color": "white",
        })
        assert status == 500, (status, data)
        assert data.get("error") == "engine unavailable", data
        print("PASS /api/move -> 500 'engine unavailable' when engine broken")
    finally:
        try:
            server.ENGINE.close()
        except Exception:
            pass
        server.ENGINE = saved


def test_canonical_reason_strings():
    """Every canonical, winner-phrased result reason string (spec-exact)."""
    import chess_core as core

    # Decisive endings: phrased from the WINNER's color, always "won".
    assert core.canonical_reason("1-0", "checkmate") == "White won by checkmate"
    assert core.canonical_reason("0-1", "checkmate") == "Black won by checkmate"
    assert core.canonical_reason("1-0", "resignation") == "White won by resignation"
    assert core.canonical_reason("0-1", "resignation") == "Black won by resignation"
    assert core.canonical_reason("1-0", "time forfeit") == "White won on time"
    assert core.canonical_reason("0-1", "time forfeit") == "Black won on time"
    # Draw endings.
    assert core.canonical_reason("1/2-1/2", "repetition") == "Draw by 3-fold repetition"
    assert core.canonical_reason("1/2-1/2", "stalemate") == "Draw by stalemate"
    assert core.canonical_reason("1/2-1/2", "insufficient material") == "Draw by insufficient material"
    assert core.canonical_reason("1/2-1/2", "fifty-move rule") == "Draw by 50-move rule"
    print("PASS canonical_reason strings (winner-phrased, 'won' not 'lost')")


def test_result_line():
    import chess_core as core
    assert core.result_line("1-0", "checkmate") == "1-0 (White won by checkmate)"
    assert core.result_line("0-1", "checkmate") == "0-1 (Black won by checkmate)"
    assert core.result_line("1-0", "time forfeit") == "1-0 (White won on time)"
    assert core.result_line("1/2-1/2", "stalemate") == "1/2-1/2 (Draw by stalemate)"
    assert core.result_line("1/2-1/2", "repetition") == "1/2-1/2 (Draw by 3-fold repetition)"
    print("PASS result_line format '1-0 (White won by checkmate)' etc")


def test_eco_classification():
    import eco
    # 1.e4 e5 2.Nf3 -> King's Knight Opening (C40); adding Nc6 -> C44;
    # deeper Ruy Lopez continuation is a LONGER prefix and wins.
    r1 = eco.classify(["e2e4", "e7e5", "g1f3"])
    assert r1 and r1["eco"] == "C40", r1
    r2 = eco.classify(["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"])
    assert r2 and r2["eco"] == "C60" and "Ruy Lopez" in r2["name"], r2
    # 1.d4 d5 -> D00 (Queen's Pawn Game).
    r3 = eco.classify(["d2d4", "d7d5"])
    assert r3 and r3["eco"] == "D00", r3
    # Longest-prefix: Najdorf is deeper than the Sicilian roots.
    r4 = eco.classify(["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4",
                       "f3d4", "g8f6", "b1c3", "a7a6"])
    assert r4 and r4["eco"] == "B90", r4
    # Too short / no move -> no match.
    assert eco.classify([]) is None
    assert eco.classify(["a2a3"]) is None
    # SAN convenience API agrees with the UCI API.
    r5 = eco.classify_san(["e4", "e5", "Nf3", "Nc6", "Bb5"])
    assert r5 and r5["eco"] == "C60", r5
    print("PASS eco.classify longest-prefix classification")


def test_display_ratings_by_mode():
    import ratings as R
    user = {"fide_blitz": 1450.0, "fide_rapid": 1400.0,
            "fide_classical": 1500.0, "rated_rating": 42.0,
            "rated_rd": 200.0, "rated_vol": 0.06}
    # FIDE mode picks the rating for the game's time class. base=180,inc=2 ->
    # 180+120=300 <= 600 -> blitz.
    p, b = R.display_ratings(user, "fide", 180, 2)
    assert p == 1450.0 and b == R.CHESS_AMATEUR_FIDE["blitz"], (p, b)
    # Classical (no clock / unlimited -> classical).
    p, b = R.display_ratings(user, "fide", None, 0)
    assert p == 1500.0 and b == R.CHESS_AMATEUR_FIDE["classical"], (p, b)
    # Rated -> single glicko rating vs the fixed Chess Amateur rated rating.
    p, b = R.display_ratings(user, "rated", 300, 0)
    assert p == 42.0 and b == R.CHESS_AMATEUR_RATED, (p, b)
    # Casual -> no ratings.
    assert R.display_ratings(user, "casual", 300, 0) == (None, None)
    # Guest (no account) in any mode -> no ratings.
    assert R.display_ratings(None, "fide", 300, 0) == (None, None)
    print("PASS display_ratings by mode/time-class (fide/rated/casual/guest)")


def test_format_delta_zero():
    import ratings as R
    assert R.format_delta(0) == "+0"
    assert R.format_delta(0.0) == "+0"
    assert R.format_delta(0, suffix="FIDE classical") == "+0 FIDE classical"
    assert R.format_delta(6.4) == "+6.40"
    assert R.format_delta(6.4, suffix="FIDE rapid") == "+6.40 FIDE rapid"
    assert R.format_delta(-3.21) == "-3.21"
    print("PASS format_delta zero -> '+0'")


def test_flask_new_move_ratings_and_result_line():
    """Flask test-client: /api/new + /api/move expose player_rating/bot_rating
    (null for a guest/casual), and a finished game returns a result_line."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app
    client = flask_app.app.test_client()

    # Guest, casual -> ratings are null.
    r = client.post("/api/new", json={"human_color": "white", "mode": "casual",
                                       "unlimited": True})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["player_rating"] is None and data["bot_rating"] is None, data
    assert data.get("result_line") is None, data

    # A move keeps ratings null for a guest.
    r = client.post("/api/move", json={
        "move": "e2e4", "moves": [], "human_color": "white",
        "mode": "casual", "unlimited": True})
    assert r.status_code == 200, r.get_json()
    md = r.get_json()
    assert md["player_rating"] is None and md["bot_rating"] is None, md

    # A finished game (guest, Fool's mate) returns the canonical result_line.
    r = client.post("/api/move", json={
        "move": "d8h4", "moves": ["f2f3", "e7e5", "g2g4"],
        "human_color": "black", "mode": "casual", "unlimited": True})
    assert r.status_code == 200, r.get_json()
    fd = r.get_json()
    assert fd["game_over"] is True, fd
    assert fd["result"] == "0-1", fd
    assert fd["result_line"] == "0-1 (Black won by checkmate)", fd
    print("PASS Flask /api/new + /api/move ratings null (guest) + result_line")


def test_flask_view_includes_eco():
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app
    client = flask_app.app.test_client()
    r = client.post("/api/view", json={
        "human_color": "white",
        "moves": ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["eco_code"] == "C60", data
    assert "Ruy Lopez" in (data["eco_name"] or ""), data
    print("PASS Flask /api/view includes eco classification")


def test_storage_eco_columns_and_finish_game():
    """SQLite Store: eco columns migrate + finish_game persists + surfaces
    them in list_games/get_game with a 'date' convenience field."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    assert st.enabled, "sqlite store should be enabled"
    uid = st.create_user("ecouser", "hash")
    assert uid is not None
    gid = st.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate",
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"],
        started_at="2026-01-02T03:04:05+00:00",
        ended_at="2026-01-02T03:10:00+00:00",
        base_seconds=None, increment=0, rating_delta="+0",
        eco_code="C60", eco_name="Ruy Lopez", mode="fide")
    assert gid is not None
    g = st.get_game(uid, gid)
    assert g["eco_code"] == "C60" and g["eco_name"] == "Ruy Lopez", g
    assert g["date"] == "2026-01-02", g
    assert g["rating_delta"] == "+0", g
    assert g["mode"] == "fide", g
    games = st.list_games(uid)
    assert len(games) == 1 and games[0]["eco_code"] == "C60", games
    assert games[0]["mode"] == "fide", games
    print("PASS storage eco_code/eco_name columns + finish_game + date field")


def test_storage_mode_column_roundtrip():
    """SQLite Store: the `mode` column migrates and round-trips through the
    in-progress upsert AND finish_game, and surfaces via get_in_progress_game,
    list_games and get_game (needed by the My Games Mode filter)."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("modeuser", "hash")
    # In-progress game carries mode so ongoing games can be filtered.
    gid = st.upsert_in_progress_game(
        user_id=uid, game_id=None, human_color="black",
        moves_uci=["e2e4"], started_at="2026-02-03T04:05:06+00:00",
        base_seconds=180, increment=2, clock_white=180.0, clock_black=180.0,
        mode="rated")
    ip = st.get_in_progress_game(uid)
    assert ip is not None and ip["mode"] == "rated", ip
    lst = st.list_games(uid)
    assert lst[0]["mode"] == "rated" and lst[0]["status"] == "in_progress", lst
    # Finishing keeps/overwrites the mode.
    st.finish_game(
        user_id=uid, game_id=gid, human_color="black", result="0-1",
        result_reason="resignation", moves_uci=["e2e4"],
        started_at="2026-02-03T04:05:06+00:00",
        ended_at="2026-02-03T04:20:00+00:00", base_seconds=180, increment=2,
        rating_delta="+3.14", eco_code=None, eco_name=None, mode="rated")
    g = st.get_game(uid, gid)
    assert g["mode"] == "rated" and g["status"] == "finished", g
    print("PASS storage mode column round-trip (upsert + finish + reads)")


def test_time_control_bucket_mapping():
    """My Games time-control FILTER buckets: exactly unlimited/blitz/rapid/
    classical (NO bullet); 'custom' is any time-limited control."""
    import ratings as R
    assert R.time_control_bucket(None, 0) == "unlimited"
    assert R.time_control_bucket(180, 2) == "blitz"       # 180+120=300 <=600
    assert R.time_control_bucket(600, 0) == "blitz"       # ==600 boundary
    assert R.time_control_bucket(900, 10) == "rapid"      # 900+600=1500 <3600
    assert R.time_control_bucket(3600, 0) == "classical"  # ==3600 boundary
    assert R.time_control_bucket(5400, 30) == "classical"
    # No bullet bucket exists anywhere.
    assert R.time_control_bucket(60, 0) == "blitz"
    print("PASS time_control_bucket mapping (5 buckets, no bullet)")


def test_result_class_derivation():
    """win/draw/loss RELATIVE TO THE HUMAN, from (result, human_color)."""
    import ratings as R
    assert R.result_class("1-0", "white") == "win"
    assert R.result_class("1-0", "black") == "loss"
    assert R.result_class("0-1", "black") == "win"
    assert R.result_class("0-1", "white") == "loss"
    assert R.result_class("1/2-1/2", "white") == "draw"
    assert R.result_class("1/2-1/2", "black") == "draw"
    print("PASS result_class win/draw/loss-from-(result,human_color)")


def test_flask_history_page_renders():
    """GET /history renders 200 for a logged-in user with a finished game
    (no Jinja errors after the column/filter overhaul)."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app
    c = flask_app.app.test_client()
    r = c.post("/api/register", json={"username": "histuser",
                                      "password": "pw123456"})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = flask_app.STORE.get_user_by_username("histuser")["id"]
    flask_app.STORE.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate",
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"],
        started_at="2026-03-04T05:06:07+00:00",
        ended_at="2026-03-04T05:30:00+00:00", base_seconds=180, increment=2,
        rating_delta="+6.40 FIDE blitz", eco_code="C60", eco_name="Ruy Lopez",
        mode="fide")
    resp = c.get("/history")
    assert resp.status_code == 200, resp.status_code
    body = resp.get_data(as_text=True)
    # New: single Date column header, Home nav, embedded games JSON + history.js
    # (the Review/Resume buttons + rows are rendered client-side by history.js).
    assert "Home" in body, "Home nav link missing"
    assert ">Date<" in body, "single Date column header missing"
    assert "history.js" in body, "history.js not referenced"
    assert "HISTORY_GAMES" in body and "C60" in body, "games JSON not embedded"
    # Removed columns/labels gone from the server-rendered table.
    assert ">Started<" not in body and ">Ended<" not in body, "old columns"
    assert "You played" not in body and ">Status<" not in body, "old columns"
    assert "Signed in as" not in body, "'Signed in as' should be removed"
    print("PASS Flask /history renders 200 with new single Date column")


def _rating_numeric_delta(delta_str):
    """Extract the numeric value from a rating_delta string such as
    '+6.40 FIDE classical', '+12.34' or '+0'. Returns a float."""
    assert delta_str is not None, "expected a rating_delta"
    token = delta_str.split()[0]   # drop any 'FIDE classical' suffix
    return float(token)


def test_flask_ratings_reflect_update_end_to_end():
    """REVIEW-FIX regression: the rating shown to the client on a game-ending
    move / resign / resume must be the POST-update value, not the pre-game one.

    (a) A game-ending /api/move (here a human time-forfeit in a FIDE game)
        returns player_rating == old_rating + numeric(rating_delta).
    (b) /api/resign returns the updated player_rating (old + numeric delta).
    (c) /api/in-progress returns player_rating/bot_rating for a rated/fide
        in-progress game, and null for a casual one.

    Each assertion would FAIL against the pre-fix behavior (stale in-memory
    user dict / omitted fields).
    """
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app
    from ratings import time_class

    # --- (a) game-ending move (time forfeit) returns the POST-update rating ---
    c = flask_app.app.test_client()
    r = c.post("/api/register", json={"username": "rateduser1",
                                      "password": "pw12345678"})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = flask_app.STORE.get_user_by_username("rateduser1")["id"]
    # FIDE game, white, 3+2 blitz (base 180, inc 2 -> time_class blitz).
    r = c.post("/api/new", json={"human_color": "white", "mode": "fide",
                                 "hours": 0, "minutes": 3, "seconds": 0,
                                 "increment": 2})
    assert r.status_code == 200, r.get_json()
    newd = r.get_json()
    tclass = time_class(180, 2)
    pre_rating = flask_app.STORE.get_user_by_id(uid)["fide_%s" % tclass]
    assert abs(newd["player_rating"] - pre_rating) < 1e-9, (newd, pre_rating)
    # Human flags: report an elapsed exceeding the remaining clock -> game over.
    r = c.post("/api/move", json={
        "move": "e2e4", "moves": [], "human_color": "white", "mode": "fide",
        "base_seconds": 180, "increment": 2,
        "clock": {"white": 180.0, "black": 180.0}, "elapsed": 999.0,
        "game_id": newd.get("game_id"),
        "started_at": newd.get("started_at")})
    assert r.status_code == 200, r.get_json()
    md = r.get_json()
    assert md["game_over"] is True, md
    assert md["result"] == "0-1", md          # white flagged -> black wins
    assert md["rating_delta"] is not None, md
    post_rating_db = flask_app.STORE.get_user_by_id(uid)["fide_%s" % tclass]
    # The response's player_rating must equal the POST-update rating (the DB
    # value) -- NOT the stale pre-game value. And pre + numeric(delta) must land
    # on it too (delta string is 2dp-rounded, so allow a small tolerance).
    assert abs(md["player_rating"] - post_rating_db) < 1e-6, (md, post_rating_db)
    expected = pre_rating + _rating_numeric_delta(md["rating_delta"])
    assert abs(md["player_rating"] - expected) < 0.01, (md, expected)
    assert abs(md["player_rating"] - pre_rating) > 1e-6, \
        ("player_rating is stale (pre-game value)", md, pre_rating)
    print("PASS Flask game-ending move returns POST-update player_rating")

    # --- (b) /api/resign returns the updated player_rating -------------------
    c2 = flask_app.app.test_client()
    r = c2.post("/api/register", json={"username": "rateduser2",
                                       "password": "pw12345678"})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid2 = flask_app.STORE.get_user_by_username("rateduser2")["id"]
    r = c2.post("/api/new", json={"human_color": "white", "mode": "rated",
                                  "hours": 0, "minutes": 5, "seconds": 0,
                                  "increment": 0})
    assert r.status_code == 200, r.get_json()
    pre2 = flask_app.STORE.get_user_by_id(uid2)["rated_rating"]
    r = c2.post("/api/resign", json={})
    assert r.status_code == 200, r.get_json()
    rd = r.get_json()
    assert rd["result"] == "0-1", rd
    assert rd["rating_delta"] is not None, rd
    assert "player_rating" in rd and rd["player_rating"] is not None, rd
    post2_db = flask_app.STORE.get_user_by_id(uid2)["rated_rating"]
    assert abs(rd["player_rating"] - post2_db) < 1e-6, (rd, post2_db)
    expected2 = pre2 + _rating_numeric_delta(rd["rating_delta"])
    assert abs(rd["player_rating"] - expected2) < 0.01, (rd, expected2)
    assert abs(rd["player_rating"] - pre2) > 1e-6, \
        ("resign player_rating is stale", rd, pre2)
    assert "bot_rating" in rd and rd["bot_rating"] is not None, rd
    print("PASS Flask /api/resign returns updated player_rating (+bot_rating)")

    # --- (c) /api/in-progress carries ratings (rated) / null (casual) --------
    c3 = flask_app.app.test_client()
    r = c3.post("/api/register", json={"username": "rateduser3",
                                       "password": "pw12345678"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = c3.post("/api/new", json={"human_color": "white", "mode": "rated",
                                  "hours": 0, "minutes": 5, "seconds": 0,
                                  "increment": 0})
    assert r.status_code == 200, r.get_json()
    ip = c3.get("/api/in-progress").get_json()["in_progress"]
    assert ip is not None, "expected an in-progress game"
    assert "player_rating" in ip and ip["player_rating"] is not None, ip
    assert "bot_rating" in ip and ip["bot_rating"] is not None, ip

    c4 = flask_app.app.test_client()
    r = c4.post("/api/register", json={"username": "casualuser",
                                       "password": "pw12345678"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = c4.post("/api/new", json={"human_color": "white", "mode": "casual",
                                  "hours": 0, "minutes": 5, "seconds": 0,
                                  "increment": 0})
    assert r.status_code == 200, r.get_json()
    ipc = c4.get("/api/in-progress").get_json()["in_progress"]
    assert ipc is not None, "expected an in-progress game"
    assert ipc["player_rating"] is None and ipc["bot_rating"] is None, ipc
    print("PASS Flask /api/in-progress carries ratings (rated) / null (casual)")


def main():
    test_engine()
    test_single_process_invariant_across_respawn()
    test_engine_fails_fast_when_binary_broken()
    # Pure-python + Flask test-client checks for the FEAT-002 backend
    # foundation (reason strings, result_line, ECO, ratings-by-mode, storage).
    test_canonical_reason_strings()
    test_result_line()
    test_eco_classification()
    test_display_ratings_by_mode()
    test_format_delta_zero()
    test_flask_new_move_ratings_and_result_line()
    test_flask_view_includes_eco()
    test_storage_eco_columns_and_finish_game()
    # FEAT-004: mode column + My Games filter/sort helpers + /history render.
    test_storage_mode_column_roundtrip()
    test_time_control_bucket_mapping()
    test_result_class_derivation()
    test_flask_history_page_renders()
    # REVIEW-FIX: end-to-end rating shown to the client on move/resign/resume.
    test_flask_ratings_reflect_update_end_to_end()
    httpd, server = start_server()
    try:
        # new game as white
        status, data = _post("/api/new", {"human_color": "white"})
        assert status == 200, data
        assert data["bot_name"] == "Chess Amateur"
        assert data["human_color"] == "white"
        assert data["fen"].startswith("rnbqkbnr/pppppppp"), data["fen"]
        assert len(data["legal_moves"]) == 20, data["legal_moves"]
        gid = data["game_id"]
        assert data["moves"] == [], data.get("moves")
        print("PASS /api/new (white) game_id=%s" % gid)

        # legal move -> engine replies (client carries the move history)
        status, data = _post("/api/move", {
            "game_id": gid, "move": "e2e4",
            "moves": [], "human_color": "white",
        })
        assert status == 200, data
        assert data["san_history"][0] == "e4", data["san_history"]
        assert data["last_bot_move"] is not None, data
        assert len(data["san_history"]) == 2, data["san_history"]
        # The authoritative history now carries both plies (human + bot).
        assert data["moves"][0] == "e2e4", data["moves"]
        assert data["moves"][1] == data["last_bot_move"]["uci"], data["moves"]
        assert not data["game_over"]
        moves_after = data["moves"]
        print("PASS /api/move legal, bot replied:", data["last_bot_move"])

        # STATELESS REGRESSION TEST: simulate a different worker / a restarted
        # process by WIPING the server's in-memory game store, then send a move
        # carrying only the client-side history. Pre-fix this returned 404
        # ("unknown game_id"); now it must succeed by rebuilding from "moves".
        with server.GAMES_LOCK:
            server.GAMES.clear()
        status, data = _post("/api/move", {
            "game_id": "gone-after-restart", "move": "g1f3",
            "moves": moves_after, "human_color": "white",
        })
        assert status == 200, ("stateless move should succeed after memory wipe", status, data)
        # History rebuilt + advanced: e4, <bot reply>, Nf3, <bot reply>.
        assert data["san_history"][0] == "e4", data["san_history"]
        assert data["san_history"][2] == "Nf3", data["san_history"]
        assert data["moves"][2] == "g1f3", data["moves"]
        assert data["last_bot_move"] is not None, data
        rboard = chess.Board()
        for uci in data["moves"]:
            mv = chess.Move.from_uci(uci)
            assert mv in rboard.legal_moves, ("rebuilt move illegal", uci, data["moves"])
            rboard.push(mv)
        print("PASS stateless /api/move after GAMES.clear() -> 200 (no 404), history rebuilt")

        # illegal move -> 400, carried history unchanged (no desync)
        status, data = _post("/api/move", {
            "game_id": gid, "move": "e2e4",
            "moves": moves_after, "human_color": "white",
        })
        assert status == 400, (status, data)
        assert data.get("error") == "illegal move", data
        # A follow-up legal move using the SAME carried history still works,
        # proving the illegal attempt did not corrupt/advance client state.
        status, data = _post("/api/move", {
            "game_id": gid, "move": "g1f3",
            "moves": moves_after, "human_color": "white",
        })
        assert status == 200, data
        assert data["moves"][2] == "g1f3", data["moves"]
        print("PASS /api/move illegal -> 400, carried history unchanged (no desync)")

        # threads: a specific in-range value is honored and returned in state,
        # and the engine still returns a legal reply.
        status, data = _post("/api/new", {"human_color": "white", "threads": 2})
        assert status == 200, data
        assert data["threads"] == 2, data
        tgid = data["game_id"]
        status, data = _post("/api/move", {
            "game_id": tgid, "move": "e2e4",
            "moves": [], "human_color": "white", "threads": 2,
        })
        assert status == 200, data
        assert data["threads"] == 2, data
        assert data["last_bot_move"] is not None, data
        # The engine's reply must be legal in the position after 1.e4.
        rboard = chess.Board()
        rboard.push(chess.Move.from_uci("e2e4"))
        assert chess.Move.from_uci(data["last_bot_move"]["uci"]) in rboard.legal_moves, data
        print("PASS /api/new threads=2 honored, engine replied:", data["last_bot_move"])

        # threads: missing -> defaults to 128
        status, data = _post("/api/new", {"human_color": "white"})
        assert status == 200, data
        assert data["threads"] == 128, data
        print("PASS /api/new missing threads -> default 128")

        # threads: out-of-range and invalid values -> default 128
        for bad in (0, -5, 999, "abc", 3.5, True, None):
            status, data = _post("/api/new", {"human_color": "white", "threads": bad})
            assert status == 200, (bad, data)
            assert data["threads"] == 128, (bad, data)
        print("PASS /api/new out-of-range/invalid threads -> default 128")

        # new game as black -> engine moves first
        status, data = _post("/api/new", {"human_color": "black"})
        assert status == 200, data
        assert data["human_color"] == "black"
        assert len(data["san_history"]) == 1, data["san_history"]
        assert len(data["moves"]) == 1, data["moves"]
        assert data["last_bot_move"] is not None, data
        print("PASS /api/new (black), bot first move:", data["last_bot_move"])

        # in-progress result fields are None while the game continues
        status, data = _post("/api/new", {"human_color": "white"})
        gid2 = data["game_id"]
        status, data = _post("/api/move", {
            "game_id": gid2, "move": "e2e4",
            "moves": [], "human_color": "white",
        })
        assert data["result"] is None and data["result_reason"] is None
        assert data["status"] in ("white_to_move", "black_to_move")
        assert not data["game_over"]
        print("PASS in-progress result fields are None")

        # EXPLICIT game-over / terminal-position test, driven STATELESSLY.
        # Fool's mate: 1.f3 e5 2.g4 Qh4#. The human (White) plays g2g4 as the
        # final move, carrying the prior history; Black's mating reply already
        # happened via the client... except here White is mated by Black's
        # queen. Build it as: human=black, carried moves ["f2f3","e7e5","g2g4"],
        # and the human (Black) plays the mating d8h4. Wipe memory first so this
        # also proves terminal detection does not depend on server state.
        with server.GAMES_LOCK:
            server.GAMES.clear()
        status, data = _post("/api/move", {
            "game_id": "fresh-worker", "move": "d8h4",
            "moves": ["f2f3", "e7e5", "g2g4"], "human_color": "black",
        })
        assert status == 200, (status, data)
        assert data["game_over"] is True, data
        # White is checkmated, so Black wins -> "0-1".
        assert data["result"] == "0-1", data["result"]
        assert data["result_reason"] == "checkmate", data["result_reason"]
        assert data["status"] == "game_over", data["status"]
        assert data["legal_moves"] == [], data["legal_moves"]
        assert data["san_history"] == ["f3", "e5", "g4", "Qh4#"], data["san_history"]
        # No bot reply once the game is already over.
        assert data["last_bot_move"] is None, data
        mate_moves = data["moves"]
        print("PASS stateless game-over detection: Fool's mate reported (result 0-1)")

        # Verify /api/move refuses to advance once the carried history is over.
        status, data = _post("/api/move", {
            "game_id": "fresh-worker", "move": "e2e4",
            "moves": mate_moves, "human_color": "black",
        })
        assert status == 400, (status, data)
        assert data.get("error") == "game is over", data
        print("PASS /api/move on finished game -> 400 'game is over'")

        # A tampered / illegal move history is rejected wholesale.
        status, data = _post("/api/move", {
            "move": "e2e4", "moves": ["e2e4", "e2e4"], "human_color": "white",
        })
        assert status == 400, (status, data)
        assert data.get("error") == "invalid move history", data
        print("PASS /api/move with illegal move history -> 400 'invalid move history'")

        # static index served
        status, body = _get("/")
        assert status == 200, status
        assert "Chess Amateur" in body, body[:200]
        print("PASS GET / serves index.html")

        # Engine-unavailable path: swap in a broken binary and confirm a fast
        # HTTP 500 (not a hang / 502). Runs against the live test server.
        test_server_returns_500_when_engine_unavailable()

        print("\nALL TESTS PASSED")
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.ENGINE.close()


if __name__ == "__main__":
    main()
