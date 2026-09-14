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
import shutil
import sys
import tempfile
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


def test_storage_eco_recompute_when_null():
    """FEAT-002: list_games/get_game must RECOMPUTE the ECO from the stored
    moves when eco_code was stored NULL (pre-migration rows), so the My Games
    ECO filter can match them. A game with 1.e4 e5 2.Nf3 Nc6 must surface as
    C44 even though it was finished with eco_code=NULL."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("econull", "hash")
    gid = st.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate",
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6"],
        started_at="2026-03-04T05:06:07+00:00",
        ended_at="2026-03-04T05:20:00+00:00",
        base_seconds=None, increment=0, rating_delta=None,
        eco_code=None, eco_name=None, mode="casual")
    g = st.get_game(uid, gid)
    assert g["eco_code"] == "C44", g
    assert g["eco_name"], g   # a name is recomputed alongside the code
    games = st.list_games(uid)
    assert games[0]["eco_code"] == "C44", games
    print("PASS storage recomputes ECO C44 when eco_code stored NULL")


def test_storage_null_mode_defaults_casual():
    """FEAT-002: a game finished BEFORE the mode column existed has NULL mode;
    list_games/get_game must default it to 'casual' so the Mode filter (with
    all three modes selected) still matches it."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("modenull", "hash")
    gid = st.finish_game(
        user_id=uid, game_id=None, human_color="black", result="0-1",
        result_reason="resignation", moves_uci=["e2e4", "c7c5"],
        started_at="2026-04-05T06:07:08+00:00",
        ended_at="2026-04-05T06:20:00+00:00",
        base_seconds=None, increment=0, rating_delta=None,
        eco_code=None, eco_name=None, mode=None)
    g = st.get_game(uid, gid)
    assert g["mode"] == "casual", g
    assert st.list_games(uid)[0]["mode"] == "casual", st.list_games(uid)
    print("PASS storage defaults NULL mode to 'casual'")


def test_storage_gmt8_date_derivation():
    """FEAT-001: the surfaced `date` is the GMT+8 calendar date of the game's
    END instant (fallback started_at for in-progress), computed server-side.
    16:00:00 UTC lands on the NEXT GMT+8 day; 15:59:59 UTC stays on the same
    GMT+8 day; an in-progress game (ended_at NULL) uses started_at's GMT+8
    date. Also unit-tests the pure _gmt8_date helper directly."""
    from storage import Store, _gmt8_date
    # Pure helper on the authoritative boundary strings.
    assert _gmt8_date("2026-09-14T16:00:00+00:00") == "2026-09-15"
    assert _gmt8_date("2026-09-14T15:59:59+00:00") == "2026-09-14"
    # Robustness: space separator, trailing Z, and naive (assumed UTC).
    assert _gmt8_date("2026-09-14 16:00:00+00:00") == "2026-09-15"
    assert _gmt8_date("2026-09-14T16:00:00Z") == "2026-09-15"
    assert _gmt8_date("2026-09-14T16:00:00") == "2026-09-15"
    assert _gmt8_date(None) is None
    assert _gmt8_date("not-a-date") is None

    st = Store("sqlite:///:memory:")
    uid = st.create_user("gmt8user", "hash")
    # Finished game: ended_at 16:00:00 UTC -> next GMT+8 day (2026-09-15),
    # even though started_at is on an earlier GMT+8 day.
    gid = st.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate", moves_uci=["e2e4", "e7e5"],
        started_at="2026-09-14T10:00:00+00:00",
        ended_at="2026-09-14T16:00:00+00:00",
        base_seconds=None, increment=0, rating_delta=None,
        eco_code=None, eco_name=None, mode="casual")
    g = st.get_game(uid, gid)
    assert g["date"] == "2026-09-15", g
    assert st.list_games(uid)[0]["date"] == "2026-09-15", st.list_games(uid)

    # Finished game one second earlier: ended_at 15:59:59 UTC -> same GMT+8 day.
    gid2 = st.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate", moves_uci=["e2e4", "e7e5"],
        started_at="2026-09-14T10:00:00+00:00",
        ended_at="2026-09-14T15:59:59+00:00",
        base_seconds=None, increment=0, rating_delta=None,
        eco_code=None, eco_name=None, mode="casual")
    assert st.get_game(uid, gid2)["date"] == "2026-09-14", st.get_game(uid, gid2)

    # In-progress game (ended_at NULL): _game_row_to_dict falls back to the
    # GMT+8 date of started_at. started_at 16:00:00 UTC -> 2026-09-15 GMT+8.
    ipid = st.upsert_in_progress_game(
        user_id=uid, game_id=None, human_color="black", moves_uci=["e2e4"],
        started_at="2026-09-14T16:00:00+00:00", base_seconds=180, increment=2,
        clock_white=180.0, clock_black=180.0, mode="rated")
    ipg = st.get_game(uid, ipid)
    assert ipg["status"] == "in_progress" and ipg["ended_at"] is None, ipg
    assert ipg["date"] == "2026-09-15", ipg
    print("PASS storage derives GMT+8 date of end instant (fallback start)")


def test_storage_player_rating_after_persisted():
    """FEAT-002: finish_game persists player_rating_after and surfaces it via
    list_games/get_game (None when not supplied, e.g. casual/guest)."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("ratingafter", "hash")
    gid = st.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate", moves_uci=["e2e4", "e7e5"],
        started_at="2026-05-06T07:08:09+00:00",
        ended_at="2026-05-06T07:20:00+00:00",
        base_seconds=180, increment=2, rating_delta="+6.40 FIDE blitz",
        eco_code="C20", eco_name="King's Pawn Game", mode="fide",
        player_rating_after=1456.4)
    g = st.get_game(uid, gid)
    assert g["player_rating_after"] == 1456.4, g
    assert st.list_games(uid)[0]["player_rating_after"] == 1456.4
    # A casual game persists NULL (nothing extra shown in My Games).
    gid2 = st.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate", moves_uci=["e2e4", "e7e5"],
        started_at="2026-05-07T07:08:09+00:00",
        ended_at="2026-05-07T07:20:00+00:00",
        base_seconds=None, increment=0, rating_delta=None,
        eco_code=None, eco_name=None, mode="casual")
    assert st.get_game(uid, gid2)["player_rating_after"] is None
    print("PASS storage persists/surfaces player_rating_after (None for casual)")


def test_canonical_reason_timeout_code():
    """FEAT-002: canonical_reason accepts the 'timeout' code (the renamed
    reason) and keeps the winner-phrased 'won on time' move-log tail."""
    import chess_core as core
    assert core.canonical_reason("1-0", "timeout") == "White won on time"
    assert core.canonical_reason("0-1", "timeout") == "Black won on time"
    print("PASS canonical_reason accepts 'timeout' code -> 'won on time'")


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
                                      "password": "pw123456",
                                      "email": "histuser@example.com"})
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
                                      "password": "pw12345678",
                                      "email": "rateduser1@example.com"})
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
                                       "password": "pw12345678",
                                       "email": "rateduser2@example.com"})
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
                                       "password": "pw12345678",
                                       "email": "rateduser3@example.com"})
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
                                       "password": "pw12345678",
                                       "email": "casualuser@example.com"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = c4.post("/api/new", json={"human_color": "white", "mode": "casual",
                                  "hours": 0, "minutes": 5, "seconds": 0,
                                  "increment": 0})
    assert r.status_code == 200, r.get_json()
    ipc = c4.get("/api/in-progress").get_json()["in_progress"]
    assert ipc is not None, "expected an in-progress game"
    assert ipc["player_rating"] is None and ipc["bot_rating"] is None, ipc
    print("PASS Flask /api/in-progress carries ratings (rated) / null (casual)")


def test_flask_delete_account():
    """FEAT-003: POST /api/delete-account.

    (a) Without a session -> 401 and no side effects.
    (b) With a session -> deletes the user AND their games (cascade), clears
        the session, and returns ok. A subsequent /api/me shows no user.
    """
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app

    # (a) Not signed in -> 401.
    anon = flask_app.app.test_client()
    r = anon.post("/api/delete-account", json={})
    assert r.status_code == 401, r.get_data(as_text=True)

    # (b) Signed in -> account + games removed.
    c = flask_app.app.test_client()
    r = c.post("/api/register", json={"username": "deluser",
                                      "password": "pw12345678",
                                      "email": "deluser@example.com"})
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = flask_app.STORE.get_user_by_username("deluser")["id"]
    # Give the user a finished game so we can prove the cascade delete.
    flask_app.STORE.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate", moves_uci=["e2e4", "e7e5"],
        started_at="2026-06-07T08:09:10+00:00",
        ended_at="2026-06-07T08:20:00+00:00", base_seconds=None, increment=0,
        rating_delta=None, eco_code=None, eco_name=None, mode="casual")
    assert len(flask_app.STORE.list_games(uid)) == 1

    r = c.post("/api/delete-account", json={})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get("ok") is True, r.get_json()
    # The user row is gone.
    assert flask_app.STORE.get_user_by_username("deluser") is None
    assert flask_app.STORE.get_user_by_id(uid) is None
    # Their games cascaded away.
    assert flask_app.STORE.list_games(uid) == []
    # Session was cleared: /api/me reports no user on the same client.
    me = c.get("/api/me").get_json()
    assert me["user"] is None, me
    print("PASS Flask /api/delete-account: 401 anon, deletes user+games signed in")


def test_flask_duplicate_username_rejected():
    """FEAT-003: a second registration with an existing username fails with a
    clear error and does NOT create a second user row. Also rejects a
    case-insensitive near-duplicate ('DupUser' vs 'dupuser')."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app

    c1 = flask_app.app.test_client()
    r = c1.post("/api/register", json={"username": "dupuser",
                                       "password": "pw12345678",
                                       "email": "dupuser@example.com"})
    assert r.status_code == 200, r.get_data(as_text=True)

    # Exact duplicate -> 400 with a clear "already taken" message.
    c2 = flask_app.app.test_client()
    r = c2.post("/api/register", json={"username": "dupuser",
                                       "password": "different1",
                                       "email": "dupuser2@example.com"})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "taken" in (r.get_json().get("error") or "").lower(), r.get_json()

    # Case-insensitive near-duplicate is also rejected.
    c3 = flask_app.app.test_client()
    r = c3.post("/api/register", json={"username": "DupUser",
                                       "password": "different2",
                                       "email": "dupuser3@example.com"})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "taken" in (r.get_json().get("error") or "").lower(), r.get_json()

    # Exactly one such user exists in the DB (no duplicate row created).
    count = flask_app.STORE._execute(
        "SELECT COUNT(*) FROM users WHERE LOWER(username) = LOWER(?)",
        ("dupuser",), fetch="one")[0]
    assert count == 1, ("expected exactly one 'dupuser' row", count)
    print("PASS Flask duplicate username rejected (exact + case-insensitive)")


def test_storage_delete_user_cascade():
    """FEAT-003: Store.delete_user removes the user and their games on SQLite
    (where ON DELETE CASCADE is not enforced without a pragma)."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("cascadeuser", "hash")
    assert uid is not None
    st.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate", moves_uci=["e2e4", "e7e5"],
        started_at="2026-07-08T09:10:11+00:00",
        ended_at="2026-07-08T09:20:00+00:00", base_seconds=None, increment=0,
        rating_delta=None, eco_code=None, eco_name=None, mode="casual")
    assert len(st.list_games(uid)) == 1
    assert st.delete_user(uid) is True
    assert st.get_user_by_id(uid) is None
    assert st.list_games(uid) == []
    # Deleting again is a harmless no-op.
    assert st.delete_user(uid) is False
    print("PASS storage delete_user removes user + games (sqlite cascade)")


def test_storage_collections_tree_and_membership():
    """FEAT-006: collections form a nestable per-user tree; games can be filed
    into a collection and listed; deleting a parent cascades its subfolders and
    memberships (on SQLite, where ON DELETE CASCADE is not enforced without a
    pragma -- delete_collection does the cascade explicitly)."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("colluser", "hash")
    assert uid is not None

    # (a) Nested collections: parent -> child -> grandchild (arbitrary depth).
    parent = st.create_collection(uid, "Openings", parent_id=None)
    child = st.create_collection(uid, "e4", parent_id=parent)
    grandchild = st.create_collection(uid, "Ruy Lopez", parent_id=child)
    assert parent and child and grandchild, (parent, child, grandchild)
    tree = st.list_collections(uid)
    assert len(tree) == 3, tree
    by_id = {c["id"]: c for c in tree}
    assert by_id[parent]["parent_id"] is None, by_id[parent]
    assert by_id[child]["parent_id"] == parent, by_id[child]
    assert by_id[grandchild]["parent_id"] == child, by_id[grandchild]

    # Renaming works and is reflected in the tree.
    assert st.rename_collection(uid, child, "1.e4") is True
    assert {c["id"]: c["name"] for c in st.list_collections(uid)}[child] == "1.e4"

    # (b) Assign a game to a collection and list games in that collection.
    gid = st.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate",
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"],
        started_at="2026-08-09T10:11:12+00:00",
        ended_at="2026-08-09T10:20:00+00:00", base_seconds=None, increment=0,
        rating_delta=None, eco_code="C60", eco_name="Ruy Lopez", mode="casual")
    assert st.add_game_to_collection(uid, grandchild, gid) is True
    # Idempotent: adding again is a no-op success.
    assert st.add_game_to_collection(uid, grandchild, gid) is True
    listed = st.list_games_in_collection(uid, grandchild)
    assert len(listed) == 1 and listed[0]["id"] == gid, listed
    assert listed[0]["eco_code"] == "C60", listed
    # Membership map surfaces the assignment.
    assert st.list_game_collection_ids(uid).get(gid) == [grandchild]
    # Remove membership.
    assert st.remove_game_from_collection(uid, grandchild, gid) is True
    assert st.list_games_in_collection(uid, grandchild) == []
    # Re-add for the cascade test below.
    assert st.add_game_to_collection(uid, grandchild, gid) is True

    # (c) Deleting the PARENT cascades all descendant collections + memberships.
    assert st.delete_collection(uid, parent) is True
    assert st.list_collections(uid) == [], st.list_collections(uid)
    # The membership rows for the whole subtree are gone (game itself survives).
    assert st.list_game_collection_ids(uid) == {}
    assert st.get_game(uid, gid) is not None, "the game itself must survive"
    print("PASS storage collections tree + membership + cascade delete")


def test_storage_collections_ownership_isolation():
    """FEAT-006: collection operations enforce ownership at the storage layer.
    A user cannot rename/delete/populate another user's collection, cannot
    nest under a parent they do not own, and cannot list another user's games
    in a collection."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    alice = st.create_user("alice6", "hash")
    bob = st.create_user("bob6", "hash")
    a_coll = st.create_collection(alice, "Alice folder", parent_id=None)
    b_coll = st.create_collection(bob, "Bob folder", parent_id=None)

    # Bob cannot rename or delete Alice's collection.
    assert st.rename_collection(bob, a_coll, "hacked") is False
    assert st.delete_collection(bob, a_coll) is False
    # Alice's folder is untouched.
    assert {c["id"]: c["name"] for c in st.list_collections(alice)}[a_coll] \
        == "Alice folder"

    # Bob cannot nest a folder under Alice's collection (parent not owned).
    assert st.create_collection(bob, "sneaky", parent_id=a_coll) is None

    # Bob cannot add HIS game to Alice's collection, nor Alice's game to his.
    b_game = st.finish_game(
        user_id=bob, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate", moves_uci=["e2e4", "e7e5"],
        started_at="2026-09-10T11:12:13+00:00",
        ended_at="2026-09-10T11:20:00+00:00", base_seconds=None, increment=0,
        rating_delta=None, eco_code=None, eco_name=None, mode="casual")
    assert st.add_game_to_collection(bob, a_coll, b_game) is False
    # And a user cannot file a game they do not own into their own collection.
    assert st.add_game_to_collection(bob, b_coll, 999999) is False
    # Listing another user's collection returns [] (ownership guard).
    assert st.list_games_in_collection(bob, a_coll) == []
    print("PASS storage collections enforce per-user ownership isolation")


def test_flask_collections_endpoints_auth_and_ownership():
    """FEAT-006: the /api/collections endpoints require an authenticated
    session (401 without one) and enforce ownership across users.

    (a) Every endpoint returns 401 for an anonymous client.
    (b) A logged-in user can create nested folders, assign a game, list, and
        delete (cascading) via the endpoints.
    (c) A second user cannot rename/delete/populate the first user's folder
        (404/400), and cannot see it in their own GET /api/collections.
    """
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app

    # (a) Anonymous -> 401 on every collections endpoint.
    anon = flask_app.app.test_client()
    assert anon.get("/api/collections").status_code == 401
    assert anon.post("/api/collections", json={"name": "x"}).status_code == 401
    assert anon.patch("/api/collections/1", json={"name": "x"}).status_code == 401
    assert anon.delete("/api/collections/1").status_code == 401
    assert anon.post("/api/collections/1/games",
                     json={"game_id": 1}).status_code == 401
    assert anon.delete("/api/collections/1/games/1").status_code == 401

    # (b) Signed-in user: create nested folders + assign a game.
    c = flask_app.app.test_client()
    assert c.post("/api/register", json={"username": "colweb",
                                         "password": "pw12345678",
                                         "email": "colweb@example.com"}).status_code == 200
    uid = flask_app.STORE.get_user_by_username("colweb")["id"]
    r = c.post("/api/collections", json={"name": "Parent"})
    assert r.status_code == 200, r.get_data(as_text=True)
    parent_id = r.get_json()["collection"]["id"]
    r = c.post("/api/collections", json={"name": "Child", "parent_id": parent_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    child_id = r.get_json()["collection"]["id"]
    cols = c.get("/api/collections").get_json()["collections"]
    assert len(cols) == 2, cols

    gid = flask_app.STORE.finish_game(
        user_id=uid, game_id=None, human_color="white", result="1-0",
        result_reason="checkmate", moves_uci=["e2e4", "e7e5"],
        started_at="2026-10-11T12:13:14+00:00",
        ended_at="2026-10-11T12:20:00+00:00", base_seconds=None, increment=0,
        rating_delta=None, eco_code=None, eco_name=None, mode="casual")
    assert c.post("/api/collections/%d/games" % child_id,
                  json={"game_id": gid}).status_code == 200
    assert len(flask_app.STORE.list_games_in_collection(uid, child_id)) == 1
    # Rename works.
    assert c.patch("/api/collections/%d" % child_id,
                   json={"name": "Renamed"}).status_code == 200

    # (c) A second user cannot touch the first user's collections.
    c2 = flask_app.app.test_client()
    assert c2.post("/api/register", json={"username": "colweb2",
                                          "password": "pw12345678",
                                          "email": "colweb2@example.com"}).status_code == 200
    # Their own listing does not include the first user's folders.
    assert c2.get("/api/collections").get_json()["collections"] == []
    # Rename/delete another user's collection -> 404 (not found for them).
    assert c2.patch("/api/collections/%d" % parent_id,
                    json={"name": "steal"}).status_code == 404
    assert c2.delete("/api/collections/%d" % parent_id).status_code == 404
    # Add a game to another user's collection -> 400.
    assert c2.post("/api/collections/%d/games" % parent_id,
                   json={"game_id": gid}).status_code == 400
    # The first user's tree is intact after the attacks.
    assert len(c.get("/api/collections").get_json()["collections"]) == 2

    # (b, cont.) Deleting the parent cascades the child + membership.
    assert c.delete("/api/collections/%d" % parent_id).status_code == 200
    assert c.get("/api/collections").get_json()["collections"] == []
    assert flask_app.STORE.get_game(uid, gid) is not None  # game survives
    print("PASS Flask /api/collections auth (401) + ownership + cascade")


def test_fide_parser_extracts_ratings():
    """FEAT-004: fide.parse_profile_html maps FIDE standard->classical, rapid,
    blitz from fixture HTML, and returns None for a control the player is not
    rated in. NO network."""
    import fide
    # Modern 'profile-*' block layout (FIDE historically misspells 'standart').
    html_full = (
        '<div class="profile-games">'
        '  <div class="profile-standart"><span class="profile-top-rating-data">'
        '1611</span> std</div>'
        '  <div class="profile-rapid"><span class="profile-top-rating-data">'
        '1523</span> rapid</div>'
        '  <div class="profile-blitz"><span class="profile-top-rating-data">'
        '1498</span> blitz</div>'
        '</div>')
    r = fide.parse_profile_html(html_full)
    assert r == {"classical": 1611, "rapid": 1523, "blitz": 1498}, r

    # Missing controls: standard present, rapid/blitz "Not rated" / 0 -> None.
    html_partial = (
        '<div class="profile-standart"><span>2001</span> std</div>'
        '<div class="profile-rapid"><span>Not rated</span> rapid</div>'
        '<div class="profile-blitz"><span>0</span> blitz</div>')
    r = fide.parse_profile_html(html_partial)
    assert r == {"classical": 2001, "rapid": None, "blitz": None}, r

    # Label-then-number fallback layout (std./rapid/blitz labels).
    html_labels = (
        "<table><tr><td>std.</td><td>1750</td></tr>"
        "<tr><td>rapid</td><td>1700</td></tr>"
        "<tr><td>blitz</td><td>1680</td></tr></table>")
    r = fide.parse_profile_html(html_labels)
    assert r == {"classical": 1750, "rapid": 1700, "blitz": 1680}, r

    # Garbage / empty input -> all None (never raises).
    assert fide.parse_profile_html("") == {
        "classical": None, "rapid": None, "blitz": None}
    assert fide.parse_profile_html(None) == {
        "classical": None, "rapid": None, "blitz": None}
    print("PASS fide.parse_profile_html maps standard->classical + None-for-missing")


def test_fide_lookup_ratings_never_raises_and_injects_fetcher():
    """FEAT-004: fide.lookup_ratings uses an injected fetcher (no network),
    parses returned HTML, and returns all-None for invalid ids / empty HTML."""
    import fide
    html = ('<div class="profile-standart"><span>1400</span></div>'
            '<div class="profile-rapid"><span>1300</span></div>'
            '<div class="profile-blitz"><span>1200</span></div>')
    got = fide.lookup_ratings("12345678", fetcher=lambda fid: html)
    assert got == {"classical": 1400, "rapid": 1300, "blitz": 1200}, got
    # Invalid (non-numeric) id -> all None, fetcher never called.
    assert fide.lookup_ratings("not-an-id", fetcher=lambda fid: html) == {
        "classical": None, "rapid": None, "blitz": None}
    # Empty HTML (e.g. network failure returned None) -> all None.
    assert fide.lookup_ratings("111", fetcher=lambda fid: None) == {
        "classical": None, "rapid": None, "blitz": None}
    print("PASS fide.lookup_ratings injectable fetcher + safe all-None fallbacks")


def test_signup_fide_seeding_maps_and_defaults():
    """FEAT-004: app._seed_fide_ratings seeds each FIDE time control to the
    scraped rating when present and 1400 when absent, using a STUBBED scraper
    (no network), and stores the fide_id on the user."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    # Make sure Supabase is NOT configured for this test.
    for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
        os.environ.pop(k, None)
    import app as flask_app
    uid = flask_app.STORE.create_user("fideseed", "hash")

    # Stub: player is rated classical + blitz, but NOT rapid.
    def stub(fide_id):
        assert fide_id == "1503014", fide_id
        return {"classical": 2882, "rapid": None, "blitz": 2700}

    seeded = flask_app._seed_fide_ratings(uid, "1503014", scraper=stub)
    assert seeded == {"classical": 2882, "rapid": 1400, "blitz": 2700}, seeded
    u = flask_app.STORE.get_user_by_id(uid)
    assert u["fide_classical"] == 2882.0, u
    assert u["fide_rapid"] == 1400.0, u          # unrated -> default 1400
    assert u["fide_blitz"] == 2700.0, u
    # fide_id persisted on the row.
    row = flask_app.STORE._execute(
        "SELECT fide_id FROM users WHERE id = ?", (uid,), fetch="one")
    assert row[0] == "1503014", row

    # A scraper that raises must NOT block signup; all three default to 1400.
    uid2 = flask_app.STORE.create_user("fideseed2", "hash")

    def boom(fide_id):
        raise RuntimeError("network down")

    seeded2 = flask_app._seed_fide_ratings(uid2, "999", scraper=boom)
    assert seeded2 == {"classical": 1400, "rapid": 1400, "blitz": 1400}, seeded2
    u2 = flask_app.STORE.get_user_by_id(uid2)
    assert (u2["fide_classical"], u2["fide_rapid"], u2["fide_blitz"]) == (
        1400.0, 1400.0, 1400.0), u2
    print("PASS signup FIDE seeding maps scraped ratings, defaults missing to 1400")


def test_register_full_flow_with_fide_id_bcrypt_path():
    """FEAT-004: with Supabase UNSET, POST /api/register with a fide_id uses the
    bcrypt fallback, logs the user in, and seeds FIDE ratings via the (real)
    fide module fed through a monkeypatched fetcher so NO network is used."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
        os.environ.pop(k, None)
    import app as flask_app
    import fide as fide_mod

    saved = fide_mod.fetch_profile_html
    fide_mod.fetch_profile_html = lambda fid, timeout=8: (
        '<div class="profile-standart"><span>1990</span></div>'
        '<div class="profile-rapid"><span>Not rated</span></div>'
        '<div class="profile-blitz"><span>1850</span></div>')
    try:
        c = flask_app.app.test_client()
        r = c.post("/api/register", json={"username": "fideuser",
                                          "password": "pw12345678",
                                          "email": "fideuser@example.com",
                                          "fide_id": "1503014"})
        assert r.status_code == 200, r.get_data(as_text=True)
        uid = flask_app.STORE.get_user_by_username("fideuser")["id"]
        u = flask_app.STORE.get_user_by_id(uid)
        assert u["fide_classical"] == 1990.0, u
        assert u["fide_rapid"] == 1400.0, u   # unrated -> 1400
        assert u["fide_blitz"] == 1850.0, u
    finally:
        fide_mod.fetch_profile_html = saved
    print("PASS /api/register with FIDE ID (bcrypt path) seeds ratings, no network")


def test_bcrypt_fallback_when_supabase_unconfigured():
    """FEAT-004: with SUPABASE_URL/ANON_KEY unset, register + login work via
    the bcrypt path and duplicate usernames (exact + case-insensitive) are
    rejected -- i.e. the gate does not break the offline path."""
    import auth
    from storage import Store
    for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
        os.environ.pop(k, None)
    assert auth.supabase_configured() is False
    st = Store("sqlite:///:memory:")

    user, err = auth.register_user(st, "bcryptonly", "pw12345678",
                                   email="bcryptonly@example.com")
    assert err is None and user is not None, (user, err)
    # The real email is persisted on the local row.
    assert st.get_user_by_username("bcryptonly")["email"] == \
        "bcryptonly@example.com"
    # Login succeeds with the right password, fails with the wrong one.
    ok, err = auth.authenticate_user(st, "bcryptonly", "pw12345678")
    assert err is None and ok["username"] == "bcryptonly", (ok, err)
    bad, err = auth.authenticate_user(st, "bcryptonly", "wrongpass1")
    assert bad is None and err, (bad, err)
    # Duplicate username (exact + case-insensitive) rejected.
    dup, err = auth.register_user(st, "bcryptonly", "another12",
                                  email="dup@example.com")
    assert dup is None and "taken" in err.lower(), (dup, err)
    dup2, err = auth.register_user(st, "BcryptOnly", "another12",
                                   email="dup2@example.com")
    assert dup2 is None and "taken" in err.lower(), (dup2, err)
    print("PASS bcrypt fallback register/login + duplicate rejection (Supabase unset)")


def test_validate_email_and_email_required_at_signup():
    """Real-email signup: validate_email accepts sane addresses and rejects
    obvious junk, and register_user REQUIRES + persists a valid email in the
    bcrypt-local path."""
    import auth
    from storage import Store
    for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
        os.environ.pop(k, None)
    # Valid addresses -> None (no error).
    for good in ("a@b.co", "user.name+tag@sub.example.com",
                 "  spaced@example.com  "):
        assert auth.validate_email(good) is None, good
    # Invalid addresses -> an error string.
    for bad in ("", None, "noatsign.com", "two@@example.com", "a@b",
                "@example.com", "user@", "user@.com", "user@example.",
                "user@ex..com", 12345):
        assert isinstance(auth.validate_email(bad), str) and \
            auth.validate_email(bad), bad

    st = Store("sqlite:///:memory:")
    # Missing email is rejected (email genuinely required now).
    u, err = auth.register_user(st, "needsmail", "pw12345678")
    assert u is None and err and "email" in err.lower(), (u, err)
    u, err = auth.register_user(st, "needsmail", "pw12345678", email="nope")
    assert u is None and err, (u, err)
    # A valid email registers and is stored (stripped/normalized).
    u, err = auth.register_user(st, "hasmail", "pw12345678",
                                email="  Player@Example.com  ")
    assert err is None and u is not None, (u, err)
    assert st.get_user_by_username("hasmail")["email"] == "Player@Example.com"
    print("PASS validate_email + email required and persisted at signup")


def test_storage_supabase_and_fide_id_columns():
    """FEAT-004: users.supabase_user_id + users.fide_id migrate on SQLite and
    the get/set helpers round-trip; existing rows survive (NULL by default)."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("linkme", "hash")
    # New columns default to NULL for an existing/plain row.
    row = st._execute(
        "SELECT supabase_user_id, fide_id FROM users WHERE id = ?",
        (uid,), fetch="one")
    assert row == (None, None), row
    # email column exists (idempotent migration) and is NULL when not supplied.
    row = st._execute("SELECT email FROM users WHERE id = ?", (uid,),
                      fetch="one")
    assert row == (None,), row
    assert st.get_user_by_username("linkme")["email"] is None
    # create_user persists a supplied email, and get_user_by_username returns it.
    uid2 = st.create_user("withmail", "hash", email="withmail@example.com")
    assert st.get_user_by_username("withmail")["email"] == \
        "withmail@example.com"
    # Link + lookup by supabase id.
    st.set_supabase_id(uid, "sb-uuid-123")
    got = st.get_user_by_supabase_id("sb-uuid-123")
    assert got is not None and got["id"] == uid, got
    assert st.get_user_by_supabase_id("nope") is None
    assert st.get_user_by_supabase_id(None) is None
    # Set fide id.
    st.set_fide_id(uid, "1503014")
    row = st._execute("SELECT fide_id FROM users WHERE id = ?", (uid,),
                      fetch="one")
    assert row[0] == "1503014", row
    print("PASS storage supabase_user_id/fide_id columns + get/set helpers")


def test_forgot_password_noop_without_supabase():
    """FEAT-004: POST /api/forgot-password returns a clear 501 when Supabase is
    unconfigured (no email delivery in the bcrypt-local path)."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
        os.environ.pop(k, None)
    import app as flask_app
    c = flask_app.app.test_client()
    r = c.post("/api/forgot-password", json={"username": "whoever"})
    assert r.status_code == 501, r.get_data(as_text=True)
    assert "unavailable" in (r.get_json().get("error") or "").lower()
    print("PASS /api/forgot-password -> 501 no-op when Supabase unconfigured")


def test_syzygy_setoption_command_sequence():
    """FEAT-005 (A): engine.syzygy_setoption_commands emits the SyzygyPath
    setoption ONLY when a non-empty, populated Syzygy directory is configured;
    empty list when disabled / dir missing / dir has no .rtbw files. No network
    and no real download."""
    import tempfile
    import engine as eng
    import syzygy

    # Disabled -> no command.
    os.environ["SYZYGY_DISABLE"] = "1"
    assert eng.syzygy_setoption_commands() == [], "disabled must emit nothing"
    del os.environ["SYZYGY_DISABLE"]

    # Configured dir that CONTAINS a (dummy) .rtbw file -> one SyzygyPath cmd.
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "KQvK.rtbw"), "wb") as fh:
        fh.write(b"x")
    os.environ["SYZYGY_DIR"] = d
    try:
        assert syzygy.syzygy_dir() == d, syzygy.syzygy_dir()
        cmds = eng.syzygy_setoption_commands()
        assert cmds == ["setoption name SyzygyPath value %s" % d], cmds

        # An EMPTY configured dir (no .rtbw files) -> no command (avoids
        # pointing Stockfish at a bogus/empty path).
        d2 = tempfile.mkdtemp()
        os.environ["SYZYGY_DIR"] = d2
        assert syzygy.syzygy_dir() is None
        assert eng.syzygy_setoption_commands() == []
    finally:
        os.environ.pop("SYZYGY_DIR", None)

    # Injectable dir_getter: the command builder places SyzygyPath correctly.
    cmds = eng.syzygy_setoption_commands(dir_getter=lambda: "/some/tb/dir")
    assert cmds == ["setoption name SyzygyPath value /some/tb/dir"], cmds
    assert eng.syzygy_setoption_commands(dir_getter=lambda: None) == []
    print("PASS engine emits SyzygyPath setoption only when TB dir populated")


def test_syzygy_download_disabled_is_noop():
    """FEAT-005 (A): ensure_downloaded is a no-op (no network) when disabled."""
    import syzygy
    os.environ["SYZYGY_DISABLE"] = "1"
    try:
        assert syzygy.ensure_downloaded(log=lambda m: None) is None
        assert syzygy.is_disabled() is True
    finally:
        del os.environ["SYZYGY_DISABLE"]
    # An explicitly-empty SYZYGY_DIR also disables.
    os.environ["SYZYGY_DIR"] = ""
    try:
        assert syzygy.is_disabled() is True
        assert syzygy.configured_dir() is None
    finally:
        os.environ.pop("SYZYGY_DIR", None)
    # The 3-4-5-men WDL set has the canonical 145 material signatures.
    assert len(syzygy._wdl_filenames()) == 145, len(syzygy._wdl_filenames())
    print("PASS syzygy download disabled -> no-op; 145 WDL signatures")


def _reset_syzygy_progress(syzygy):
    """Force syzygy's module-level progress counters back to their pristine
    pre-download state so a test starts from a known baseline (offline)."""
    with syzygy._PROGRESS_LOCK:
        syzygy._downloaded = 0
        syzygy._ready = False
        syzygy._total = len(syzygy._wdl_filenames())
        syzygy._progress_initialized = True


def test_syzygy_status_disabled_reports_ready():
    """FEAT-002 (a): syzygy.status() reports ready:true / percent:100 when
    tablebases are disabled (SYZYGY_DISABLE), with no network touched."""
    import syzygy
    _reset_syzygy_progress(syzygy)
    os.environ["SYZYGY_DISABLE"] = "1"
    try:
        st = syzygy.status()
    finally:
        del os.environ["SYZYGY_DISABLE"]
    assert st["ready"] is True, st
    assert st["percent"] == 100, st
    assert st["downloaded"] == st["total"], st
    assert st["total"] == 145, st
    # start_background_download() is a no-op when disabled and keeps ready:true.
    _reset_syzygy_progress(syzygy)
    os.environ["SYZYGY_DISABLE"] = "1"
    try:
        assert syzygy.start_background_download() is None
        assert syzygy.status()["ready"] is True
    finally:
        del os.environ["SYZYGY_DISABLE"]
    print("PASS syzygy.status() disabled -> ready:true/percent:100; bg no-op")


def test_syzygy_status_percent_monotonic_to_100():
    """FEAT-002 (b): a simulated partial-then-complete progression yields a
    monotonic percent that stays < 100 until ready, then reaches 100 with
    ready:true. Drives the module-level counters directly (no network)."""
    import syzygy
    # Enabled but pointed at an empty temp dir so status() does not short-circuit
    # via disabled/marker paths.
    tmpdir = tempfile.mkdtemp(prefix="syzygy_prog_")
    os.environ["SYZYGY_DIR"] = tmpdir
    os.environ.pop("SYZYGY_DISABLE", None)
    try:
        _reset_syzygy_progress(syzygy)
        total = syzygy.status()["total"]
        assert total == 145, total
        last = -1
        # Drive a partial progression via the internal counter bump.
        for _ in range(total):
            syzygy._bump_downloaded()
            st = syzygy.status()
            assert st["percent"] >= last, ("percent regressed", last, st)
            # Never claims 100 before ready is set, even when downloaded==total.
            assert st["percent"] < 100, ("percent hit 100 before ready", st)
            assert st["ready"] is False, st
            last = st["percent"]
        # Completing the routine flips ready:true and percent to exactly 100.
        syzygy._mark_ready()
        done = syzygy.status()
        assert done["ready"] is True, done
        assert done["percent"] == 100, done
        assert done["downloaded"] == done["total"] == total, done
    finally:
        os.environ.pop("SYZYGY_DIR", None)
        shutil.rmtree(tmpdir, ignore_errors=True)
        _reset_syzygy_progress(syzygy)
    print("PASS syzygy.status() percent monotonic, reaches 100 with ready:true")


def test_flask_tablebase_status_endpoint():
    """FEAT-002 (c): GET /api/tablebase-status returns the {ready, downloaded,
    total, percent} shape via the in-process Flask test client."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    os.environ["SYZYGY_DISABLE"] = "1"
    try:
        import app as flask_app
        client = flask_app.app.test_client()
        r = client.get("/api/tablebase-status")
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert set(data.keys()) == {"ready", "downloaded", "total", "percent"}, data
        assert isinstance(data["ready"], bool), data
        assert isinstance(data["downloaded"], int), data
        assert isinstance(data["total"], int), data
        assert isinstance(data["percent"], int), data
        # Disabled -> ready:true / percent:100, and healthz stays independent.
        assert data["ready"] is True, data
        assert data["percent"] == 100, data
        hr = client.get("/healthz")
        assert hr.status_code == 200, hr.get_json()
        assert hr.get_json()["status"] == "ok", hr.get_json()
    finally:
        os.environ.pop("SYZYGY_DISABLE", None)
    print("PASS GET /api/tablebase-status shape + disabled values; healthz OK")


def test_book_probe_offline_real_bin():
    """FEAT-005 (B): the Polyglot book (real pc2500.bin, offline) returns a
    legal book move for the start position and after 1.e4 e5 2.Nf3 Nc6, and
    None for a position not in the book."""
    import random
    import book
    import chess

    b = chess.Board()
    assert book.in_book(b) is True, "start position should be in pc2500.bin"
    mv = book.book_move(b, rng=random.Random(7))
    assert mv is not None, "expected a book move for the start position"
    assert chess.Move.from_uci(mv) in b.legal_moves, mv

    b2 = chess.Board()
    for u in ("e2e4", "e7e5", "g1f3", "b8c6"):
        b2.push_uci(u)
    assert book.in_book(b2) is True, "1.e4 e5 2.Nf3 Nc6 should be in book"
    mv2 = book.book_move(b2, rng=random.Random(7))
    assert mv2 is not None and chess.Move.from_uci(mv2) in b2.legal_moves, mv2

    # A trivial K+K endgame is not an opening-book position.
    b3 = chess.Board("8/8/8/4k3/8/8/4K3/8 w - - 0 1")
    assert book.in_book(b3) is False
    assert book.book_move(b3) is None
    print("PASS Polyglot book probe (real pc2500.bin, offline) start + Nc6")


def test_reply_move_fide_prefers_book_others_do_not():
    """FEAT-005 (B): core.reply_move plays a BOOK move in FIDE mode when the
    position is in the book, but Casual/Rated NEVER consult the book (they use
    the engine). Deterministic without invoking Stockfish for the FIDE case by
    asserting the chosen move is one of the book's entries."""
    import random
    import book
    import chess
    import chess_core as core

    # The set of book moves available from the start position (via the real
    # book). reply_move(fide) must pick one of these WITHOUT calling Stockfish.
    start = chess.Board()
    book_ucis = set()
    import chess.polyglot
    with chess.polyglot.open_reader(book.book_path()) as reader:
        for entry in reader.find_all(start):
            book_ucis.add(entry.move.uci())
    assert book_ucis, "expected book entries for the start position"

    b = chess.Board()
    uci, san = core.reply_move(b, mode="fide", rng=random.Random(3))
    assert uci in book_ucis, ("FIDE reply must be a book move", uci, book_ucis)
    assert b.move_stack and b.peek().uci() == uci, "reply_move must push"

    # Casual/Rated: monkeypatch book_move to explode -> proves it is NOT called
    # (the engine path is taken instead). We stub engine_move to a fixed legal
    # move so no real Stockfish process is needed.
    saved_engine_move = core.engine_move
    saved_book_move = book.book_move

    def boom_book(*a, **k):
        raise AssertionError("book must NOT be consulted outside FIDE mode")

    def fake_engine_move(board, threads=None):
        m = next(iter(board.legal_moves))
        san = board.san(m)
        board.push(m)
        return m.uci(), san

    book.book_move = boom_book
    core.engine_move = fake_engine_move
    try:
        for m in ("casual", "rated", None):
            bb = chess.Board()
            u, s = core.reply_move(bb, mode=m)
            assert u is not None and bb.peek().uci() == u, (m, u)
        print("PASS reply_move: FIDE prefers book; casual/rated never consult it")
    finally:
        book.book_move = saved_book_move
        core.engine_move = saved_engine_move


# ---------------------------------------------------------------------------
# FEAT-004 (tablebase-warmup): engine probe-marker detection + app-layer move
# DEFERRAL with ChessMimic clock preservation, and the resolution endpoint.
# All OFFLINE: a fake shared engine emits the marker (or not), clockclient and
# syzygy.status are monkeypatched. No real Stockfish, no real download.
# ---------------------------------------------------------------------------

class _FakeProbeEngine:
    """Stand-in for the shared ChessAmateurEngine.

    best_move_with_probe_flag() returns the FIRST legal move for the given FEN
    and a caller-controlled tb_probe_seen flag, so tests can simulate the
    patched Stockfish emitting CA_TB_ABOUT_TO_PROBE without spawning a process.
    """

    def __init__(self, probe_seen):
        self.probe_seen = probe_seen
        self.calls = 0

    def best_move_with_probe_flag(self, fen, threads=None):
        import chess as _chess
        self.calls += 1
        board = _chess.Board(fen)
        mv = next(iter(board.legal_moves), None)
        return (mv.uci() if mv is not None else None), self.probe_seen

    # best_move must keep returning ONLY the move (browser-facing contract).
    def best_move(self, fen, threads=None):
        return self.best_move_with_probe_flag(fen, threads=threads)[0]


def test_engine_probe_flag_never_leaks_move_only_contract():
    """FEAT-004: best_move_with_probe_flag reports the marker; best_move still
    returns ONLY the move (no marker text ever in the returned move)."""
    import chess as _chess
    import chess_core as core

    saved = core.ENGINE
    core.ENGINE = _FakeProbeEngine(probe_seen=True)
    try:
        b = _chess.Board()
        # engine_move_ex surfaces the flag; engine_move stays (uci, san).
        uci, san, seen = core.engine_move_ex(b.copy())
        assert seen is True, "probe flag should be surfaced to the app layer"
        assert uci and _chess.Move.from_uci(uci) in _chess.Board().legal_moves
        assert ChessAmateurEngine.PROBE_MARKER not in (uci or ""), uci
        # best_move (browser-facing) returns just the move, no flag.
        mv = core.ENGINE.best_move(_chess.Board().fen())
        assert ChessAmateurEngine.PROBE_MARKER not in mv, mv
    finally:
        core.ENGINE = saved
    print("PASS engine probe flag surfaced to app; best_move stays move-only")


_FIDE_USER_SEQ = [0]


def test_engine_reapplies_syzygy_path_live_no_respawn():
    """FEAT-004: while the download populates the Syzygy dir, the LIVE engine is
    re-pointed at it via a plain 'setoption name SyzygyPath' (NOT a respawn), so
    the Step-5 probe path activates and the marker can fire. Single-process
    invariant preserved. Uses a recording fake proc -- no real Stockfish."""
    import engine as eng_mod
    import syzygy

    sent = []

    class _RecProc:
        def __init__(self):
            self._sf_buf = b""

    eng = eng_mod.ChessAmateurEngine()
    proc = _RecProc()

    saved_send = eng_mod.ChessAmateurEngine.__dict__["_send"]
    saved_read = eng_mod.ChessAmateurEngine.__dict__["_read_until"]
    saved_dir = syzygy.syzygy_dir
    # Patch I/O so no real process is needed.
    eng_mod.ChessAmateurEngine._send = staticmethod(lambda p, line: sent.append(line))
    eng_mod.ChessAmateurEngine._read_until = lambda self, p, prefix, timeout: prefix

    tmpdir = tempfile.mkdtemp(prefix="syzygy_live_")
    try:
        # 1) Dir empty at "spawn" -> no SyzygyPath applied.
        syzygy.syzygy_dir = lambda: None
        eng._applied_syzygy_path = None
        eng._apply_syzygy_path(proc)
        assert not any("SyzygyPath" in s for s in sent), sent
        assert eng._applied_syzygy_path is None

        # 2) Download populates the dir -> live setoption (no respawn).
        syzygy.syzygy_dir = lambda: tmpdir
        eng._apply_syzygy_path(proc)
        assert ("setoption name SyzygyPath value %s" % tmpdir) in sent, sent
        assert eng._applied_syzygy_path == tmpdir
        # No new process was created (respawn would go through _spawn/Popen).
        assert eng._proc is None, "no engine process should have been spawned"

        # 3) Idempotent: unchanged dir does not re-send the option.
        before = list(sent)
        eng._apply_syzygy_path(proc)
        assert sent == before, ("should not re-apply unchanged SyzygyPath", sent)
    finally:
        eng_mod.ChessAmateurEngine._send = staticmethod(saved_send)
        eng_mod.ChessAmateurEngine._read_until = saved_read
        syzygy.syzygy_dir = saved_dir
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("PASS engine re-applies SyzygyPath live (setoption, no respawn) once dir populated")


def _register_fide_user(client, username=None):
    """Register + log in a UNIQUE user via the bcrypt path (no Supabase, no
    network). The username is made unique per call because the in-process app's
    DB/state may persist across tests in one run."""
    for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
        os.environ.pop(k, None)
    if username is None:
        _FIDE_USER_SEQ[0] += 1
        username = "tbuser%d" % _FIDE_USER_SEQ[0]
    r = client.post("/api/register", json={"username": username,
                                           "password": "pw12345678",
                                           "email": "%s@example.com" % username})
    assert r.status_code == 200, r.get_data(as_text=True)


def _fide_move_env(monkeypatched_ready, bot_think, probe_seen):
    """Set up app + a fide user + monkeypatch engine/clock/syzygy for a move.

    Returns (flask_app, client, restore) where restore() undoes the patches.
    Forces the engine path (book_move -> None) so the deferral logic (which
    only applies to engine moves) is exercised deterministically.
    """
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    os.environ.pop("SYZYGY_DISABLE", None)
    import app as flask_app
    import chess_core as core
    import book
    import clockclient
    import syzygy

    client = flask_app.app.test_client()
    _register_fide_user(client)

    saved = {
        "engine": core.ENGINE,
        "book_move": book.book_move,
        "thinking_time": clockclient.thinking_time,
        "status": syzygy.status,
    }
    core.ENGINE = _FakeProbeEngine(probe_seen=probe_seen)
    book.book_move = lambda board, rng=None: None  # force engine path
    clockclient.thinking_time = (
        lambda fen, moves, base, inc, botc, oppc: bot_think)
    syzygy.status = lambda: {"ready": monkeypatched_ready,
                             "downloaded": 0 if not monkeypatched_ready else 145,
                             "total": 145,
                             "percent": 100 if monkeypatched_ready else 0}

    def restore():
        core.ENGINE = saved["engine"]
        book.book_move = saved["book_move"]
        clockclient.thinking_time = saved["thinking_time"]
        syzygy.status = saved["status"]

    return flask_app, client, restore


def test_fide_move_defers_when_probe_and_not_ready():
    """FEAT-004 (a) DEFERRAL: marker fired + syzygy not ready -> /api/move
    returns last_bot_move=None + tb_warming:true, and the bot clock is
    decremented by EXACTLY the mocked ChessMimic bot_think (wait NOT charged)."""
    import chess as _chess
    bot_think = 7.0
    base, inc = 180.0, 2.0
    flask_app, client, restore = _fide_move_env(
        monkeypatched_ready=False, bot_think=bot_think, probe_seen=True)
    try:
        clock = {"white": base, "black": base}
        r = client.post("/api/move", json={
            "move": "e2e4", "moves": [], "human_color": "white",
            "mode": "fide", "base_seconds": base, "increment": inc,
            "clock": clock, "elapsed": 0.0,
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        d = r.get_json()
        # Deferred: no bot move pushed, warming flag set, bot_think reported.
        assert d["last_bot_move"] is None, d
        assert d["tb_warming"] is True, d
        assert abs(d["bot_think"] - bot_think) < 1e-9, d
        # Only the human ply is in history (bot move pending).
        assert d["moves"] == ["e2e4"], d["moves"]
        assert d["san_history"] == ["e4"], d["san_history"]
        # Bot clock charged EXACTLY bot_think (plus increment); wait not charged.
        bot_clock = d["clock"]["black"]
        assert abs(bot_clock - (base - bot_think + inc)) < 1e-9, (bot_clock, d)
    finally:
        restore()
    print("PASS FIDE defers on probe+not-ready: no bot move, tb_warming, bot clock == base-think+inc")


def test_fide_deferred_clock_equals_normal_clock_same_think():
    """FEAT-004 (b) CLOCK-PRESERVATION EQUIVALENCE: the bot clock after a
    DEFERRED move equals the bot clock after a NORMAL move for the same
    think-time (the download wait is never charged)."""
    bot_think = 5.5
    base, inc = 300.0, 3.0

    # Deferred path (not ready, marker fired).
    flask_app, client, restore = _fide_move_env(
        monkeypatched_ready=False, bot_think=bot_think, probe_seen=True)
    try:
        rd = client.post("/api/move", json={
            "move": "e2e4", "moves": [], "human_color": "white", "mode": "fide",
            "base_seconds": base, "increment": inc,
            "clock": {"white": base, "black": base}, "elapsed": 0.0,
        }).get_json()
    finally:
        restore()
    deferred_bot_clock = rd["clock"]["black"]

    # Normal path (ready, no marker) -- same think-time.
    flask_app, client, restore = _fide_move_env(
        monkeypatched_ready=True, bot_think=bot_think, probe_seen=False)
    try:
        rn = client.post("/api/move", json={
            "move": "e2e4", "moves": [], "human_color": "white", "mode": "fide",
            "base_seconds": base, "increment": inc,
            "clock": {"white": base, "black": base}, "elapsed": 0.0,
        }).get_json()
    finally:
        restore()
    normal_bot_clock = rn["clock"]["black"]

    assert rd["last_bot_move"] is None and rd["tb_warming"] is True, rd
    assert rn["last_bot_move"] is not None and rn["tb_warming"] is False, rn
    assert abs(deferred_bot_clock - normal_bot_clock) < 1e-9, (
        deferred_bot_clock, normal_bot_clock)
    print("PASS deferred bot clock == normal bot clock for same think-time (wait not charged)")


def test_fide_resolution_fresh_search_when_ready_charges_no_extra():
    """FEAT-004 (c) RESOLUTION: after a deferral, /api/resolve-bot-move with
    syzygy ready runs a FRESH search, returns a real bestmove, and charges NO
    extra think-time (bot_think already applied by the deferring move)."""
    import chess as _chess
    bot_think = 4.0
    base, inc = 120.0, 1.0

    # 1) Deferring move (not ready): capture the carried state + charged clock.
    flask_app, client, restore = _fide_move_env(
        monkeypatched_ready=False, bot_think=bot_think, probe_seen=True)
    try:
        d = client.post("/api/move", json={
            "move": "e2e4", "moves": [], "human_color": "white", "mode": "fide",
            "base_seconds": base, "increment": inc,
            "clock": {"white": base, "black": base}, "elapsed": 0.0,
        }).get_json()
        assert d["tb_warming"] is True and d["last_bot_move"] is None, d
        carried_moves = d["moves"]
        carried_clock = d["clock"]
        bot_clock_after_defer = carried_clock["black"]

        # 2) Still warming -> resolve endpoint keeps it pending (no move yet).
        import syzygy
        r_pending = client.post("/api/resolve-bot-move", json={
            "moves": carried_moves, "human_color": "white", "mode": "fide",
            "base_seconds": base, "increment": inc, "clock": carried_clock,
        }).get_json()
        assert r_pending["last_bot_move"] is None, r_pending
        assert r_pending["tb_warming"] is True, r_pending
        # Clock untouched while still warming.
        assert abs(r_pending["clock"]["black"] - bot_clock_after_defer) < 1e-9

        # 3) Flip syzygy to ready -> fresh search resolves the bot move.
        syzygy.status = lambda: {"ready": True, "downloaded": 145,
                                 "total": 145, "percent": 100}
        r = client.post("/api/resolve-bot-move", json={
            "moves": carried_moves, "human_color": "white", "mode": "fide",
            "base_seconds": base, "increment": inc, "clock": carried_clock,
        }).get_json()
    finally:
        restore()

    assert r["last_bot_move"] is not None, r
    assert r["tb_warming"] is False, r
    # A real bestmove was appended (fresh search) and is legal.
    assert len(r["moves"]) == 2, r["moves"]
    b = _chess.Board()
    for u in r["moves"]:
        mv = _chess.Move.from_uci(u)
        assert mv in b.legal_moves, (u, r["moves"])
        b.push(mv)
    # NO extra think-time charged by resolution: the bot clock is unchanged
    # from the deferring move's charged value.
    assert abs(r["clock"]["black"] - bot_clock_after_defer) < 1e-9, (
        r["clock"]["black"], bot_clock_after_defer)
    print("PASS resolution: fresh search when ready, real bestmove, no extra think-time")


def test_fide_move_no_defer_when_probe_but_ready():
    """FEAT-004 (d) UNAFFECTED: marker fired but syzygy READY -> normal move
    (no deferral); bot moves and is charged the ChessMimic think-time."""
    bot_think = 3.0
    base, inc = 180.0, 2.0
    flask_app, client, restore = _fide_move_env(
        monkeypatched_ready=True, bot_think=bot_think, probe_seen=True)
    try:
        d = client.post("/api/move", json={
            "move": "e2e4", "moves": [], "human_color": "white", "mode": "fide",
            "base_seconds": base, "increment": inc,
            "clock": {"white": base, "black": base}, "elapsed": 0.0,
        }).get_json()
    finally:
        restore()
    assert d["last_bot_move"] is not None, d
    assert d["tb_warming"] is False, d
    assert len(d["moves"]) == 2, d["moves"]
    assert abs(d["clock"]["black"] - (base - bot_think + inc)) < 1e-9, d
    print("PASS FIDE ready+probe -> normal move (no deferral), bot charged think-time")


def test_casual_move_unaffected_by_probe_marker():
    """FEAT-004 (d) UNAFFECTED: a casual (no-clock) move never defers even if
    the engine reports the probe marker -- casual has no clock model."""
    import chess as _chess
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    os.environ.pop("SYZYGY_DISABLE", None)
    import app as flask_app
    import chess_core as core
    import syzygy

    client = flask_app.app.test_client()
    saved_engine = core.ENGINE
    saved_status = syzygy.status
    core.ENGINE = _FakeProbeEngine(probe_seen=True)
    syzygy.status = lambda: {"ready": False, "downloaded": 0,
                             "total": 145, "percent": 0}
    try:
        d = client.post("/api/move", json={
            "move": "e2e4", "moves": [], "human_color": "white",
            "mode": "casual",
        }).get_json()
    finally:
        core.ENGINE = saved_engine
        syzygy.status = saved_status
    # Casual: bot moves normally (instant), no tb_warming, no clock model.
    assert d["last_bot_move"] is not None, d
    assert d.get("tb_warming") is False, d
    assert len(d["moves"]) == 2, d["moves"]
    print("PASS casual move unaffected by probe marker (no deferral, moves instantly)")


def test_flask_custom_position_casual():
    """FEAT-005 (C): api_new with a valid FEN (casual) starts a game that
    replays statelessly from that FEN; an invalid FEN -> 400; a custom FEN is
    ignored in rated/fide mode (falls back to the standard start)."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app
    import chess

    c = flask_app.app.test_client()

    # A legal custom position: white to move, a simple K+Q vs K (5-men-ish).
    custom = "4k3/8/8/8/8/8/4Q3/4K3 w - - 0 1"
    r = c.post("/api/new", json={"human_color": "white", "mode": "casual",
                                 "unlimited": True, "fen": custom})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["start_fen"] == custom, data
    assert data["fen"] == custom, data          # no moves yet -> board == start
    assert data["moves"] == [], data

    # Stateless replay: /api/view with the start_fen + a legal move rebuilds it.
    r = c.post("/api/view", json={"human_color": "white",
                                  "start_fen": custom, "moves": ["e2e7"]})
    assert r.status_code == 200, r.get_json()
    vd = r.get_json()
    replay = chess.Board(custom)
    replay.push_uci("e2e7")
    assert vd["fen"] == replay.fen(), (vd["fen"], replay.fen())
    assert vd["start_fen"] == custom, vd

    # Invalid FEN -> 400.
    r = c.post("/api/new", json={"human_color": "white", "mode": "casual",
                                 "unlimited": True, "fen": "not a fen"})
    assert r.status_code == 400, r.get_json()
    assert "fen" in (r.get_json().get("error") or "").lower(), r.get_json()

    # A custom FEN is IGNORED in a rated game (guest -> rated downgrades to
    # casual, so use a logged-in user to actually exercise rated).
    c2 = flask_app.app.test_client()
    rr = c2.post("/api/register", json={"username": "customuser",
                                        "password": "pw12345678",
                                        "email": "customuser@example.com"})
    assert rr.status_code == 200, rr.get_data(as_text=True)
    r = c2.post("/api/new", json={"human_color": "white", "mode": "rated",
                                  "hours": 0, "minutes": 5, "seconds": 0,
                                  "increment": 0, "fen": custom})
    assert r.status_code == 200, r.get_json()
    rd = r.get_json()
    assert rd["start_fen"] is None, ("rated must ignore custom FEN", rd)
    assert rd["fen"].startswith("rnbqkbnr/pppppppp"), rd
    print("PASS Flask custom-position casual (valid replay, invalid 400, "
          "ignored in rated)")


def test_flask_custom_position_bot_moves_first_when_on_move():
    """FEAT-005 (C): when the custom position has the BOT on move (the human
    is not the side to move), Chess Amateur plays the first move immediately.

    Uses casual mode (no book), so the engine replies; we only assert that a
    bot move was produced and the history advanced by one ply."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app

    c = flask_app.app.test_client()
    # Black to move, human plays White -> bot (Black) is on move at start.
    custom = "4k3/6q1/8/8/8/8/8/4K3 b - - 0 1"
    r = c.post("/api/new", json={"human_color": "white", "mode": "casual",
                                 "unlimited": True, "fen": custom})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["last_bot_move"] is not None, data
    assert len(data["moves"]) == 1, data
    assert data["start_fen"] == custom, data
    print("PASS Flask custom position: bot moves first when it is on move")


def test_storage_start_fen_roundtrip():
    """FEAT-005 (C): the start_fen column migrates and round-trips through the
    in-progress upsert + get_in_progress_game (so a resumed custom game replays
    from the right anchor)."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("customfen", "hash")
    custom = "4k3/8/8/8/8/8/4Q3/4K3 w - - 0 1"
    st.upsert_in_progress_game(
        user_id=uid, game_id=None, human_color="white",
        moves_uci=[], started_at="2026-08-09T10:11:12+00:00",
        base_seconds=None, increment=0, mode="casual", start_fen=custom)
    ip = st.get_in_progress_game(uid)
    assert ip is not None and ip["start_fen"] == custom, ip
    print("PASS storage start_fen column round-trip (upsert + get_in_progress)")


def test_filter_puzzles_solve_check_keeps_and_rejects():
    """FEAT-007: the OFFLINE solve-check keeps a puzzle whose solver moves the
    engine matches at EVERY step, and rejects one where any solver move
    differs. Uses a STUB best_move_fn (a plain callable returning canned UCI
    per FEN) so the test is fast and fully offline (no engine, no dump).

    Lichess format: FEN is BEFORE the opponent's setup move; Moves index 0 is
    the opponent's move, and the SOLVER moves at odd indices (1, 3, ...). Only
    the solver moves are checked against the engine."""
    sys.path.insert(0, os.path.join(HERE, "tools"))
    from filter_puzzles import solve_check, solver_indices

    # A short 2-ply Scholar's-mate-ish line from the start position:
    #   index 0 (opponent): e2e4   index 1 (SOLVER): e7e5
    # Only index 1 is a solver move.
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    moves = ["e2e4", "e7e5"]
    assert solver_indices(moves) == [1], solver_indices(moves)

    # The board AFTER the opponent's e2e4, keyed by FEN, is what the engine is
    # asked about. Build the exact FEN the solve-check will query.
    after_e4 = chess.Board(start)
    after_e4.push_uci("e2e4")
    solver_fen = after_e4.fen()

    # (a) KEEP: the stub returns the expected solver move for that position.
    def good_engine(fen):
        return "e7e5" if fen == solver_fen else "0000"

    assert solve_check(good_engine, start, moves) is True, \
        "puzzle whose solver move the engine matches must be KEPT"

    # (b) REJECT: the stub returns a DIFFERENT (but legal) solver move.
    def bad_engine(fen):
        return "d7d5" if fen == solver_fen else "0000"

    assert solve_check(bad_engine, start, moves) is False, \
        "puzzle whose solver move the engine does NOT match must be REJECTED"

    # A longer line with two solver moves (indices 1 and 3): the engine must
    # match BOTH; a mismatch on the SECOND solver move still rejects.
    long_moves = ["e2e4", "e7e5", "g1f3", "b8c6"]
    assert solver_indices(long_moves) == [1, 3], solver_indices(long_moves)
    b = chess.Board(start)
    b.push_uci("e2e4")
    fen1 = b.fen()
    b.push_uci("e7e5")
    b.push_uci("g1f3")
    fen3 = b.fen()

    def match_both(fen):
        if fen == fen1:
            return "e7e5"
        if fen == fen3:
            return "b8c6"
        return "0000"

    assert solve_check(match_both, start, long_moves) is True, \
        "engine matching BOTH solver moves must KEEP the puzzle"

    def match_first_only(fen):
        if fen == fen1:
            return "e7e5"
        if fen == fen3:
            return "g8f6"   # legal, but not the expected b8c6
        return "0000"

    assert solve_check(match_first_only, start, long_moves) is False, \
        "a mismatch on the SECOND solver move must REJECT the puzzle"

    # Malformed / empty lines are rejected without raising.
    assert solve_check(good_engine, start, []) is False
    assert solve_check(good_engine, "not a fen", moves) is False
    # An illegal move in the line -> rejected (guards a malformed dump row).
    assert solve_check(lambda f: "e7e5", start, ["e2e4", "e7e6", "e6e5"]) \
        is False
    print("PASS filter_puzzles.solve_check keeps full-solve / rejects mismatch")


def test_storage_puzzle_insert_fetch_and_rating_index():
    """FEAT-007: the puzzles table + lichess_rating index are created by the
    idempotent DDL, and a puzzle can be inserted then fetched by primary key,
    by lichess_id, and counted."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    assert st.enabled, "sqlite store should be enabled"

    # The rating index exists (proves the indexed range scan for FEAT-008).
    idx = st._execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND name='puzzles_lichess_rating'", fetch="one")
    assert idx is not None, "puzzles_lichess_rating index must exist"

    assert st.count_puzzles() == 0, "no puzzles before insert"
    ok = st.insert_puzzle(
        "abc12", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "e2e4 e7e5", 1500, "opening short")
    assert ok is True, "first insert should report a NEW row"
    assert st.count_puzzles() == 1, st.count_puzzles()

    # Fetch by primary key -> moves come back as a list.
    row = st._execute("SELECT id FROM puzzles WHERE lichess_id = ?",
                      ("abc12",), fetch="one")
    pz = st.get_puzzle_by_id(row[0])
    assert pz is not None, "puzzle must be fetchable by id"
    assert pz["lichess_id"] == "abc12", pz
    assert pz["moves"] == ["e2e4", "e7e5"], pz
    assert pz["lichess_rating"] == 1500, pz
    assert pz["themes"] == "opening short", pz

    # Fetch by lichess_id and via the rating-band candidate query (FEAT-008).
    assert st.get_puzzle_by_lichess_id("abc12")["id"] == row[0]
    band = st.fetch_puzzles_in_rating_band(1400, 1600, limit=10)
    assert len(band) == 1 and band[0]["lichess_id"] == "abc12", band
    assert st.fetch_puzzles_in_rating_band(1000, 1100) == []
    print("PASS storage puzzles table + rating index + insert/fetch/count")


def test_storage_puzzle_insert_is_idempotent():
    """FEAT-007: inserting the same lichess_id twice yields ONE row (ON
    CONFLICT DO NOTHING / INSERT OR IGNORE), so the offline job + loader are
    re-runnable without duplicating. Also exercises the JSONL loader path."""
    import json
    import tempfile
    from storage import Store
    st = Store("sqlite:///:memory:")
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    assert st.insert_puzzle("dup1", fen, "e2e4 e7e5", 1200) is True
    # Second insert of the SAME lichess_id is a no-op (returns False, no dup).
    assert st.insert_puzzle("dup1", fen, "e2e4 e7e5", 1200) is False
    assert st.count_puzzles() == 1, st.count_puzzles()
    assert st.puzzle_lichess_ids() == {"dup1"}, st.puzzle_lichess_ids()

    # The bulk JSONL loader is likewise idempotent: loading a file whose rows
    # include an already-present id inserts only the new ones.
    sys.path.insert(0, os.path.join(HERE, "tools"))
    from load_puzzles import load_jsonl
    with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
        fh.write(json.dumps({"lichess_id": "dup1", "fen": fen,
                             "moves": "e2e4 e7e5", "lichess_rating": 1200,
                             "themes": None}) + "\n")
        fh.write(json.dumps({"lichess_id": "new2", "fen": fen,
                             "moves": "d2d4 d7d5", "lichess_rating": 1300,
                             "themes": "queenspawn"}) + "\n")
        jsonl_path = fh.name
    try:
        inserted, seen = load_jsonl(st, jsonl_path)
        assert seen == 2 and inserted == 1, (inserted, seen)
        assert st.count_puzzles() == 2, st.count_puzzles()
        # Re-running the loader inserts nothing further.
        inserted2, seen2 = load_jsonl(st, jsonl_path)
        assert inserted2 == 0 and seen2 == 2, (inserted2, seen2)
        assert st.count_puzzles() == 2, st.count_puzzles()
    finally:
        os.unlink(jsonl_path)
    print("PASS storage insert_puzzle + JSONL loader are idempotent on lichess_id")


def test_filter_puzzles_truncation_loop_and_outcomes():
    """FEAT-002: progressive front-truncation. Uses STUB best_move_fn callables
    keyed by FEN (the FEAT-007 pattern) so it is fast and fully offline (no
    engine, no dump). Covers: intact solve, solve after N truncations with the
    correct 2-ply-from-front advancement + solver_moves_removed count, the
    solved-rule applied to the remaining solver turns, and BOTH drop
    conditions (single-solver-move fail; multi-move fail at the last single
    solver move)."""
    sys.path.insert(0, os.path.join(HERE, "tools"))
    from filter_puzzles import truncate_and_solve, _front_truncate

    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    # -- (1) INTACT: solved with the full line, plies_removed == 0. --------
    b = chess.Board(start)
    b.push_uci("e2e4")
    fen_after_e4 = b.fen()

    def intact_engine(fen):
        return "e7e5" if fen == fen_after_e4 else "0000"

    res = truncate_and_solve(intact_engine, start, ["e2e4", "e7e5"])
    assert res["outcome"] == "intact", res
    assert res["plies_removed"] == 0 and res["solver_moves_removed"] == 0, res
    assert res["original_solver_move_count"] == 1, res
    assert res["final_fen"] == start and res["final_moves"] == "e2e4 e7e5", res

    # -- (2) _front_truncate advances by EXACTLY 2 ply (moves[0], moves[1]) and
    # keeps moves[2:] with moves[2] as the new opponent setup move. ---------
    long_line = ["e2e4", "e7e5", "g1f3", "b8c6"]
    b2 = chess.Board(start)
    b2.push_uci("e2e4")
    b2.push_uci("e7e5")
    expected_new_fen = b2.fen()  # position BEFORE the new setup move g1f3
    new_fen, new_moves = _front_truncate(start, long_line)
    assert new_fen == expected_new_fen, (new_fen, expected_new_fen)
    assert new_moves == ["g1f3", "b8c6"], new_moves

    # -- (3) TRUNCATED: fails intact (first solver move mismatches) but solves
    # after exactly ONE truncation (last solver move matches). The solved-rule
    # is applied only to the REMAINING solver turn. --------------------------
    b3 = chess.Board(start)
    b3.push_uci("e2e4")
    b3.push_uci("e7e5")
    b3.push_uci("g1f3")
    fen_before_c6 = b3.fen()  # solver to move at the shortened idx1

    def solve_after_one_trunc(fen):
        # Wrong at the ORIGINAL first solver move (idx1, after e2e4) so intact
        # fails; correct at the shortened line's solver move (after g1f3).
        if fen == fen_after_e4:
            return "d7d5"  # legal but != expected e7e5 -> intact fails
        if fen == fen_before_c6:
            return "b8c6"  # matches -> shortened line solves
        return "0000"

    res = truncate_and_solve(solve_after_one_trunc, start, long_line)
    assert res["outcome"] == "truncated", res
    assert res["plies_removed"] == 2, res
    assert res["solver_moves_removed"] == 1, res
    assert res["original_solver_move_count"] == 2, res
    assert res["final_fen"] == expected_new_fen, res
    assert res["final_moves"] == "g1f3 b8c6", res

    # -- (4) DROP (single solver move fails intact): nothing to truncate. ----
    def never(fen):
        return "0000"

    res = truncate_and_solve(never, start, ["e2e4", "e7e5"])
    assert res["outcome"] == "dropped", res
    assert res["plies_removed"] == 0 and res["solver_moves_removed"] == 0, res
    assert res["original_solver_move_count"] == 1, res

    # -- (5) DROP (multi-move fails even at the last single solver move). ----
    # never matches any solver turn, so it fails intact AND after truncation.
    res = truncate_and_solve(never, start, long_line)
    assert res["outcome"] == "dropped", res
    # One truncation was attempted (down to the last solver move) then dropped.
    assert res["plies_removed"] == 2 and res["solver_moves_removed"] == 1, res
    assert res["original_solver_move_count"] == 2, res

    # -- (6) Malformed / empty line -> drop, no raise. -----------------------
    assert truncate_and_solve(intact_engine, start, [])["outcome"] == "dropped"
    assert truncate_and_solve(
        intact_engine, "not a fen", ["e2e4", "e7e5"])["outcome"] == "dropped"

    # -- (7) A 3-solver-move line that solves only after TWO truncations
    # confirms the loop repeats and counts correctly. ------------------------
    line3 = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"]
    b4 = chess.Board(start)
    for u in ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]:
        b4.push_uci(u)
    fen_before_a6 = b4.fen()  # solver to move after two truncations

    def solve_after_two(fen):
        return "a7a6" if fen == fen_before_a6 else "0000"

    res = truncate_and_solve(solve_after_two, start, line3)
    assert res["outcome"] == "truncated", res
    assert res["plies_removed"] == 4 and res["solver_moves_removed"] == 2, res
    assert res["original_solver_move_count"] == 3, res
    assert res["final_moves"] == "f1b5 a7a6", res
    print("PASS filter_puzzles truncation loop: intact/truncated(N)/drop x2 + "
          "2-ply front advancement + solver_moves_removed count")


def test_storage_truncation_stats_idempotent_and_split():
    """FEAT-002: puzzle_truncation_stats is created idempotently, insert is
    idempotent on lichess_id, and the servable/stats split holds -- intact
    puzzles go ONLY to `puzzles` while truncation-solved puzzles go ONLY to
    puzzle_truncation_stats (never served)."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    assert st.enabled, "sqlite store should be enabled"

    # The table + its study indexes exist (created by _create_schema).
    for idx in ("puzzle_truncation_stats_rating",
                "puzzle_truncation_stats_removed"):
        row = st._execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            (idx,), fetch="one")
        assert row is not None, "index %s must exist" % idx

    assert st.count_truncation_stats() == 0
    assert st.truncation_stat_lichess_ids() == set()

    final_fen = "7k/5ppp/8/8/P7/8/8/1R5K b - - 0 2"
    ok = st.insert_truncation_stat(
        "trunc1", original_solver_move_count=2, solver_moves_removed=1,
        plies_removed=2, lichess_rating=1000, final_fen=final_fen,
        final_moves="h8g8 b1b8", themes="mateIn2")
    assert ok is True, "first insert should report a NEW row"
    assert st.count_truncation_stats() == 1

    # Idempotent: a second insert of the same lichess_id is a no-op.
    dup = st.insert_truncation_stat(
        "trunc1", original_solver_move_count=2, solver_moves_removed=1,
        plies_removed=2, lichess_rating=1000, final_fen=final_fen,
        final_moves="h8g8 b1b8", themes="mateIn2")
    assert dup is False, "duplicate insert must report False"
    assert st.count_truncation_stats() == 1, st.count_truncation_stats()
    assert st.truncation_stat_lichess_ids() == {"trunc1"}

    got = st.get_truncation_stat_by_lichess_id("trunc1")
    assert got["original_solver_move_count"] == 2, got
    assert got["solver_moves_removed"] == 1, got
    assert got["plies_removed"] == 2, got
    assert got["lichess_rating"] == 1000, got
    assert got["final_fen"] == final_fen, got
    assert got["final_moves"] == ["h8g8", "b1b8"], got
    assert got["themes"] == "mateIn2", got

    # Servable/stats SPLIT: an intact puzzle goes ONLY to `puzzles`; a
    # truncation-solved puzzle goes ONLY to puzzle_truncation_stats.
    fen0 = "6k1/5ppp/8/8/8/8/8/R6K b - - 0 1"
    assert st.insert_puzzle("intact1", fen0, "g8h8 a1a8", 800,
                            "mateIn1") is True
    assert st.count_puzzles() == 1
    # The truncated id is NOT servable (never inserted into `puzzles`).
    assert st.get_puzzle_by_lichess_id("trunc1") is None
    assert "trunc1" not in st.puzzle_lichess_ids()
    # The intact id is NOT in the study table.
    assert st.get_truncation_stat_by_lichess_id("intact1") is None
    assert "intact1" not in st.truncation_stat_lichess_ids()
    print("PASS storage puzzle_truncation_stats idempotent + servable/stats "
          "split (truncated never served)")


def test_puzzle_rating_helpers_and_displayed_rating():
    """FEAT-008 (a)+(b): the puzzle Glicko-2 rating starts at 1400 and updates
    via ratings.glicko2_update after solve/fail, and the DISPLAYED rating is
    lichess_rating - 600."""
    import ratings as R
    # (b) displayed rating = lichess - 600.
    assert R.puzzle_displayed_rating(2000) == 1400, R.puzzle_displayed_rating(2000)
    assert R.puzzle_displayed_rating(1000) == 400

    # (a) new-user start is 1400; a solve raises it and a fail lowers it, and
    # the update matches ratings.glicko2_update with the puzzle as opponent.
    start = R.PLAYER_START_PUZZLE
    assert start == 1400.0, start
    solved_r, solved_rd, solved_vol, solved_delta = R.puzzle_rating_update(
        start, R.GLICKO2_START_RD, R.GLICKO2_START_VOL, 1800, True)
    ref = R.glicko2_update(start, R.GLICKO2_START_RD, R.GLICKO2_START_VOL,
                           1800.0, R.PUZZLE_OPPONENT_RD, 1.0)
    assert abs(solved_r - ref[0]) < 1e-9, (solved_r, ref[0])
    assert solved_delta > 0, solved_delta  # solving a harder puzzle -> up
    failed_r, _, _, failed_delta = R.puzzle_rating_update(
        start, R.GLICKO2_START_RD, R.GLICKO2_START_VOL, 1800, False)
    assert failed_delta < 0, failed_delta  # failing -> down
    print("PASS puzzle rating starts 1400, updates via glicko2_update; display=lichess-600")


def test_puzzle_bell_curve_weighting_prefers_nearer():
    """FEAT-008 (d): the pure bell-curve weighting gives HIGHER weight to a
    puzzle nearer the player's rating than to one further away, and the weight
    is symmetric in |candidate - player|."""
    import puzzles as P
    player = 1500.0
    near = P.selection_weight(1520, player)
    far = P.selection_weight(1900, player)
    farther = P.selection_weight(2100, player)
    assert near > far > farther, (near, far, farther)
    # Symmetric around the player's rating.
    assert abs(P.selection_weight(1400, player)
               - P.selection_weight(1600, player)) < 1e-12
    # weighted_choice honors the weights: with a deterministic RNG that picks
    # the low end of the cumulative range, the nearest candidate is chosen.
    import random
    cands = [{"lichess_rating": 1510, "id": 1},
             {"lichess_rating": 2200, "id": 2}]
    rng = random.Random(0)
    picks = [P.weighted_choice(cands, player, rng=rng)["id"] for _ in range(200)]
    assert picks.count(1) > picks.count(2), (picks.count(1), picks.count(2))
    print("PASS bell-curve weighting prefers puzzles nearer the player's rating")


def test_puzzle_sample_uses_band_and_returns_none_when_empty():
    """FEAT-008: the sampler fetches only a rating BAND (never all puzzles) and
    returns None when the table is empty."""
    import puzzles as P
    calls = []

    def fetch_band(low, high):
        calls.append((low, high))
        return [{"lichess_rating": 1490, "id": 7},
                {"lichess_rating": 1510, "id": 8}]

    chosen = P.sample_puzzle(1500.0, fetch_band, extent=(1000, 2000))
    assert chosen is not None and chosen["id"] in (7, 8), chosen
    assert calls, "sampler must query a rating band, not load all puzzles"
    lo, hi = calls[0]
    assert hi - lo <= 2 * P.CANDIDATE_BAND + 1, (lo, hi)
    # Empty table -> None (extent unknown).
    assert P.sample_puzzle(1500.0, lambda l, h: [], extent=(None, None)) is None
    print("PASS sampler queries a rating band + returns None on empty table")


def test_flask_puzzle_endpoints_auth_persist_and_rating():
    """FEAT-008 (c)+(e): /api/puzzle requires auth (401 anonymous), the
    assigned puzzle PERSISTS across two GET calls (same id) until solved, and
    solving updates the puzzle rating + reassigns."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app

    # (e) Anonymous -> 401 on both puzzle endpoints.
    anon = flask_app.app.test_client()
    assert anon.get("/api/puzzle").status_code == 401
    assert anon.post("/api/puzzle/move", json={"move": "e7e5"}).status_code == 401

    # Seed a couple of curated puzzles directly (no engine / no dump).
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    # FEN is BEFORE the opponent setup move (e2e4). Solver is Black; index 1 is
    # the solver's move (e7e5); index 2 is the opponent reply (g1f3) -> after it
    # the line ends, so a correct e7e5 solves the puzzle.
    flask_app.STORE.insert_puzzle("pz001", start, "e2e4 e7e5 g1f3", 2000)
    flask_app.STORE.insert_puzzle("pz002", start, "e2e4 e7e5 g1f3", 2010)

    c = flask_app.app.test_client()
    assert c.post("/api/register", json={"username": "puzzler",
                                         "password": "pw12345678",
                                         "email": "puzzler@example.com"}).status_code == 200
    uid = flask_app.STORE.get_user_by_username("puzzler")["id"]
    # New user's puzzle rating is 1400.
    assert flask_app.STORE.get_user_by_id(uid)["puzzle_rating"] == 1400.0

    # (c) First GET assigns + persists a puzzle; second GET returns the SAME id.
    r1 = c.get("/api/puzzle").get_json()
    assert r1["puzzle"] is not None, r1
    pid = r1["puzzle"]["id"]
    # Displayed rating = lichess - 600.
    assert r1["puzzle"]["displayed_rating"] == r1["puzzle"]["displayed_rating"]
    r2 = c.get("/api/puzzle").get_json()
    assert r2["puzzle"]["id"] == pid, (r1, r2)
    assert flask_app.STORE.get_user_by_id(uid)["assigned_puzzle_id"] == pid

    # (b) The displayed rating equals the raw lichess rating minus 600.
    raw = flask_app.STORE.get_puzzle_by_id(pid)["lichess_rating"]
    assert r1["puzzle"]["displayed_rating"] == raw - 600, (r1["puzzle"], raw)
    # Solver color is Black (FEN before white's setup move e2e4).
    assert r1["puzzle"]["solver_color"] == "black", r1["puzzle"]

    # Solve it (correct solver move e7e5 at index 1). Rating goes UP (puzzle
    # rated 2000+ vs the player's 1400) and the assignment clears/reassigns.
    mv = c.post("/api/puzzle/move", json={"move": "e7e5", "index": 1}).get_json()
    assert mv["correct"] is True and mv["solved"] is True, mv
    assert mv["puzzle_rating"] > 1400, mv
    after = flask_app.STORE.get_user_by_id(uid)
    assert after["puzzle_rating"] > 1400.0, after
    assert after["assigned_puzzle_id"] is None, after

    # A wrong first move on a freshly-assigned puzzle FAILS it and lowers the
    # rating.
    before = flask_app.STORE.get_user_by_id(uid)["puzzle_rating"]
    c.get("/api/puzzle")  # assign the next puzzle
    bad = c.post("/api/puzzle/move", json={"move": "a7a6", "index": 1}).get_json()
    assert bad["correct"] is False and bad["failed"] is True, bad
    assert bad["puzzle_rating"] < before, (bad, before)
    print("PASS Flask puzzle endpoints: auth + persistence across GETs + rating update")


def test_storage_rating_history_roundtrip_and_filter():
    """FEAT-010 (a): append_rating_history + list_rating_history round-trips
    ordered points and filters by kind and date range."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    uid = st.create_user("histuser", "hash")
    # Append out of order; list must come back ordered by (kind, at asc).
    st.append_rating_history(uid, "rated", 12.0, "2026-01-03T00:00:00+00:00")
    st.append_rating_history(uid, "rated", 5.0, "2026-01-01T00:00:00+00:00")
    st.append_rating_history(uid, "puzzle", 1400.0, "2026-01-02T00:00:00+00:00")
    st.append_rating_history(uid, "fide_blitz", 1406.4, "2026-01-05T00:00:00+00:00")

    allp = st.list_rating_history(uid)
    # fide_blitz < puzzle < rated alphabetically; within rated ascending by at.
    kinds = [p["kind"] for p in allp]
    assert kinds == ["fide_blitz", "puzzle", "rated", "rated"], kinds
    rated = [p for p in allp if p["kind"] == "rated"]
    assert [p["rating"] for p in rated] == [5.0, 12.0], rated

    # Filter by kind.
    only_puzzle = st.list_rating_history(uid, kind="puzzle")
    assert len(only_puzzle) == 1 and only_puzzle[0]["rating"] == 1400.0, only_puzzle

    # Filter by date range (inclusive). since=2026-01-02, until=2026-01-03T23...
    windowed = st.list_rating_history(
        uid, since="2026-01-02", until="2026-01-03T23:59:59")
    kinds_w = sorted(p["kind"] for p in windowed)
    assert kinds_w == ["puzzle", "rated"], windowed  # the 01-01 rated + 01-05 blitz excluded
    print("PASS storage rating_history append + list + kind/date filters")


def test_flask_finished_game_appends_rating_history():
    """FEAT-010 (b): finishing a RATED game appends a rating_history point for
    the logged-in user (via the /api/move finish path)."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app
    c = flask_app.app.test_client()
    assert c.post("/api/register", json={"username": "rhgamer",
                                          "password": "pw12345678",
                                          "email": "rhgamer@example.com"}).status_code == 200
    uid = flask_app.STORE.get_user_by_username("rhgamer")["id"]
    assert flask_app.STORE.list_rating_history(uid) == []

    # Fool's mate as Black in RATED mode -> a real result finishes the game and
    # applies+records a Rated rating change.
    c.post("/api/new", json={"human_color": "black", "mode": "rated",
                             "unlimited": True})
    fd = c.post("/api/move", json={
        "move": "d8h4", "moves": ["f2f3", "e7e5", "g2g4"],
        "human_color": "black", "mode": "rated", "unlimited": True}).get_json()
    assert fd["game_over"] is True, fd
    hist = flask_app.STORE.list_rating_history(uid, kind="rated")
    assert len(hist) == 1, hist
    assert hist[0]["kind"] == "rated", hist
    print("PASS Flask finished rated game appends a rating_history point")


def test_daily_series_gmt8_boundary_and_carry_forward():
    """FEAT-002 (follow-up): daily_series_gmt8 is the single read-only helper
    that turns rating_history events into per-day {date, rating} points at
    GMT+8 00:00 boundaries (00:00 GMT+8 of day D == (D-1) 16:00:00 UTC)."""
    import app as flask_app
    dsg = flask_app.daily_series_gmt8

    # (1) Boundary bucketing around 16:00 UTC. Two events on 2026-03-01 UTC:
    # 15:59 UTC (before the 2026-03-02 GMT+8 boundary at 2026-03-01 16:00Z) and
    # 16:01 UTC (after it). The GMT+8 day 2026-03-02's midnight is 03-01 16:00Z,
    # so at that boundary only the 15:59 event is at-or-before it.
    pts = [
        {"at": "2026-03-01T15:59:00+00:00", "rating": 100.0},
        {"at": "2026-03-01T16:01:00+00:00", "rating": 200.0},
    ]
    out = dsg(pts, "2026-03-02", "2026-03-03")
    by_day = {p["date"]: p["rating"] for p in out}
    # 2026-03-02 midnight (03-01 16:00Z): only the 15:59 event <= boundary.
    assert by_day["2026-03-02"] == 100.0, out
    # 2026-03-03 midnight (03-02 16:00Z): both events are before -> latest wins.
    assert by_day["2026-03-03"] == 200.0, out

    # An event AT EXACTLY 16:00:00 UTC belongs to the new GMT+8 day (<= boundary).
    exact = dsg([{"at": "2026-03-01T16:00:00Z", "rating": 42.0}],
                "2026-03-02", "2026-03-02")
    assert exact == [{"date": "2026-03-02", "rating": 42.0}], exact

    # (2) Carry-forward + (3) no point before the first event. One event on the
    # GMT+8 day 2026-03-02 (03-01 16:00:00Z boundary). The window starts a day
    # earlier, which has no at-or-before event -> skipped; later days carry it.
    cf = dsg([{"at": "2026-03-01T16:00:00Z", "rating": 7.0}],
             "2026-03-01", "2026-03-04")
    assert cf == [
        {"date": "2026-03-02", "rating": 7.0},
        {"date": "2026-03-03", "rating": 7.0},
        {"date": "2026-03-04", "rating": 7.0},
    ], cf

    # (6) Empty history yields an empty series regardless of window.
    assert dsg([], "2026-03-01", "2026-03-10") == []
    # Inverted window yields empty.
    assert dsg([{"at": "2026-01-01T00:00:00Z", "rating": 1.0}],
               "2026-03-05", "2026-03-01") == []
    print("PASS daily_series_gmt8 boundary bucketing + carry-forward + empties")


def test_flask_dashboard_auth_and_series_window():
    """FEAT-010 (c) / FEAT-002 follow-up: GET /api/dashboard requires auth (401
    anonymous) and returns PER-DAY {date, rating} points per kind derived at
    GMT+8 00:00 boundaries, honoring the 'All' and Custom windows."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app

    anon = flask_app.app.test_client()
    assert anon.get("/api/dashboard").status_code == 401

    c = flask_app.app.test_client()
    assert c.post("/api/register", json={"username": "dashuser",
                                          "password": "pw12345678",
                                          "email": "dashuser@example.com"}).status_code == 200
    uid = flask_app.STORE.get_user_by_username("dashuser")["id"]
    # Seed history across several days and kinds. Use 00:00Z (== 08:00 GMT+8) so
    # each event lands squarely inside a single GMT+8 day.
    flask_app.STORE.append_rating_history(uid, "rated", 5.0, "2026-03-01T00:00:00+00:00")
    flask_app.STORE.append_rating_history(uid, "rated", 9.0, "2026-03-05T00:00:00+00:00")
    flask_app.STORE.append_rating_history(uid, "puzzle", 1410.0, "2026-03-03T00:00:00+00:00")
    flask_app.STORE.append_rating_history(uid, "fide_blitz", 1406.0, "2026-03-20T00:00:00+00:00")

    # Custom window 2026-03-02..2026-03-06 (inclusive, 5 GMT+8 days). Each day
    # gets a per-day point per series that has an event at or before its 00:00
    # GMT+8 boundary. The 03-01 00:00Z rated event (GMT+8 day 03-01) is known
    # from 03-02 onward; the 03-05 00:00Z event (GMT+8 day 03-05) raises it.
    win = c.get("/api/dashboard?from=2026-03-02&to=2026-03-06").get_json()
    assert win["from"] == "2026-03-02" and win["to"] == "2026-03-06", win
    assert set(win["kinds"]) == {"fide_blitz", "fide_rapid", "fide_classical",
                                 "rated", "puzzle"}, win
    rated = win["series"]["rated"]
    # 5 days, one point each (rated known from before the window start).
    assert [p["date"] for p in rated] == [
        "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"], rated
    # Carry-forward at 5.0 until the 03-05 boundary (03-04 16:00Z) picks up 9.0.
    assert [p["rating"] for p in rated] == [5.0, 5.0, 5.0, 5.0, 9.0], rated
    # puzzle: first event GMT+8 day 03-03 -> known from 03-04 onward in window.
    puzzle = win["series"]["puzzle"]
    assert [p["date"] for p in puzzle] == [
        "2026-03-04", "2026-03-05", "2026-03-06"], puzzle
    assert all(p["rating"] == 1410.0 for p in puzzle), puzzle
    # fide_blitz first event is 03-20, entirely after the window -> no points.
    assert win["series"]["fide_blitz"] == [], win
    assert win["series"]["fide_rapid"] == [], win

    # 'All' (no from) spans the earliest event's GMT+8 day through today. The
    # earliest event is 03-01 00:00Z (GMT+8 day 03-01); the fide_blitz series
    # should show its value on every day from its own first event onward.
    allr = c.get("/api/dashboard").get_json()
    assert allr["from"] == "2026-03-01", allr
    assert len(allr["series"]["rated"]) >= 1, allr
    # The most recent per-day rated value is 9.0 (carried forward to today).
    assert allr["series"]["rated"][-1]["rating"] == 9.0, allr
    assert allr["series"]["fide_blitz"][-1]["rating"] == 1406.0, allr

    # A user with NO history gets empty series and null bounds under 'All'.
    assert c.post("/api/register", json={"username": "emptyuser",
                                         "password": "pw12345678",
                                         "email": "emptyuser@example.com"}).status_code == 200
    empty = c.get("/api/dashboard").get_json()
    assert empty["from"] is None, empty
    assert all(empty["series"][k] == [] for k in empty["kinds"]), empty
    print("PASS Flask /api/dashboard auth + per-day GMT+8 series + All/Custom window")


def test_flask_puzzle_solve_appends_puzzle_history():
    """FEAT-010 (d): a puzzle rating change appends a 'puzzle' history point."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    flask_app.STORE.insert_puzzle("pzh01", start, "e2e4 e7e5 g1f3", 2000)

    c = flask_app.app.test_client()
    assert c.post("/api/register", json={"username": "pzhist",
                                          "password": "pw12345678",
                                          "email": "pzhist@example.com"}).status_code == 200
    uid = flask_app.STORE.get_user_by_username("pzhist")["id"]
    assert flask_app.STORE.list_rating_history(uid, kind="puzzle") == []

    c.get("/api/puzzle")  # assign
    mv = c.post("/api/puzzle/move", json={"move": "e7e5", "index": 1}).get_json()
    assert mv["solved"] is True, mv
    hist = flask_app.STORE.list_rating_history(uid, kind="puzzle")
    assert len(hist) == 1 and hist[0]["kind"] == "puzzle", hist
    print("PASS Flask puzzle solve appends a 'puzzle' rating_history point")


def test_flask_demo_gate_seen_and_auth():
    """FEAT-001 (follow-up): onboarding demo is NEW-USERS-ONLY.

    (a) a brand-new REGISTERED account has demo_pending True and is shown the
        demo (GET /api/demo show True; index emits 'showDemo: true');
    (b) POST /api/demo/seen clears demo_pending and afterwards show is False
        (index emits 'showDemo: false');
    (c) a user created WITHOUT the register path (simulating a pre-existing /
        migrated account, demo_pending default False) is NEVER shown the demo;
    (d) bumping CURRENT_DEMO_VERSION does NOT re-show the demo to a user who
        already dismissed it;
    (e) the demo endpoints require auth (401 anonymous)."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    import app as flask_app

    # (e) endpoints require auth.
    anon = flask_app.app.test_client()
    assert anon.get("/api/demo").status_code == 401
    assert anon.post("/api/demo/seen").status_code == 401

    ver = flask_app.CURRENT_DEMO_VERSION
    assert ver >= 1, "the current demo version must be a positive integer"

    c = flask_app.app.test_client()
    assert c.post("/api/register", json={"username": "demouser",
                                         "password": "pw12345678",
                                         "email": "demouser@example.com"}).status_code == 200
    uid = flask_app.STORE.get_user_by_username("demouser")["id"]

    # (a) a freshly-registered account is shown the demo (gated on demo_pending).
    assert flask_app.STORE.get_demo_pending(uid) is True
    assert flask_app.STORE.get_user_by_id(uid)["demo_pending"] is True
    d = c.get("/api/demo").get_json()
    assert d["current_version"] == ver, d
    assert d["show"] is True, d
    assert b"showDemo: true" in c.get("/").data

    # (b) marking it seen clears demo_pending and hides it (one-shot).
    assert c.post("/api/demo/seen").status_code == 200
    assert flask_app.STORE.get_demo_pending(uid) is False
    assert flask_app.STORE.get_user_by_id(uid)["demo_pending"] is False
    d2 = c.get("/api/demo").get_json()
    assert d2["show"] is False, d2
    assert b"showDemo: false" in c.get("/").data

    # (d) bumping CURRENT_DEMO_VERSION does NOT re-show the demo. Simulate a
    # major-update version bump; the gate no longer depends on the version, so
    # the demo stays hidden for a user who already dismissed it.
    old_ver = flask_app.CURRENT_DEMO_VERSION
    try:
        flask_app.CURRENT_DEMO_VERSION = old_ver + 1
        d3 = c.get("/api/demo").get_json()
        assert d3["show"] is False, d3
        assert b"showDemo: false" in c.get("/").data
    finally:
        flask_app.CURRENT_DEMO_VERSION = old_ver

    # (c) a user created WITHOUT the register path (pre-existing/migrated row,
    # demo_pending default False) is NEVER shown the demo.
    pre_uid = flask_app.STORE.create_user("preexisting", "hash")
    assert pre_uid is not None
    assert flask_app.STORE.get_demo_pending(pre_uid) is False
    assert flask_app.STORE.get_user_by_id(pre_uid)["demo_pending"] is False
    with c.session_transaction() as sess:
        sess["uid"] = pre_uid
    dpre = c.get("/api/demo").get_json()
    assert dpre["show"] is False, dpre
    assert b"showDemo: false" in c.get("/").data
    print("PASS Flask onboarding demo new-users-only gate + mark-seen + auth")


def test_storage_demo_pending_roundtrip():
    """FEAT-001 (follow-up): demo_pending storage helpers + get_user_by_id.

    A user created via create_user defaults to demo_pending False (so migrated
    rows are treated as demo-already-done). set_demo_pending toggles it, and
    get_user_by_id reflects the current value as a bool."""
    from storage import Store
    st = Store("sqlite:///:memory:")
    assert st.enabled
    uid = st.create_user("pendinguser", "hash")
    assert uid is not None
    # Default False for a plain create_user row (no register path).
    assert st.get_demo_pending(uid) is False
    assert st.get_user_by_id(uid)["demo_pending"] is False
    # Toggle on.
    st.set_demo_pending(uid, True)
    assert st.get_demo_pending(uid) is True
    assert st.get_user_by_id(uid)["demo_pending"] is True
    # Toggle off.
    st.set_demo_pending(uid, False)
    assert st.get_demo_pending(uid) is False
    assert st.get_user_by_id(uid)["demo_pending"] is False
    print("PASS storage demo_pending roundtrip + get_user_by_id")


def test_migration_boolean_default_maps_to_sql_literal():
    """PROD BUGFIX: a BOOLEAN Postgres migration column must emit a SQL boolean
    literal default (FALSE/TRUE), never the integer literal 0/1.

    Postgres rejects `ADD COLUMN demo_pending BOOLEAN DEFAULT 0` ("column is of
    type boolean but default expression is of type integer"), and because the
    migrations ran in one transaction that error aborted the whole batch and
    disabled the Store (guest mode). _pg_default_literal() is the DB-agnostic
    core of the fix: it maps the SQLite-flavoured integer default to the right
    Postgres boolean literal while leaving every other type untouched. It is
    also asserted that the demo_pending migration entry keeps its SQLite
    integer default (so SQLite stays INTEGER NOT NULL DEFAULT 0) and that the
    semantics are FALSE (demo NOT shown to existing/migrated users)."""
    from storage import Store
    # BOOLEAN maps the integer-ish default to a SQL boolean literal.
    assert Store._pg_default_literal("BOOLEAN", "0") == "FALSE"
    assert Store._pg_default_literal("boolean", 0) == "FALSE"
    assert Store._pg_default_literal("BOOLEAN", "1") == "TRUE"
    assert Store._pg_default_literal("BOOLEAN", "FALSE") == "FALSE"
    assert Store._pg_default_literal("BOOLEAN", "TRUE") == "TRUE"
    # Non-boolean types pass through unchanged (valid Postgres literals).
    assert Store._pg_default_literal("INTEGER", "0") == "0"
    assert Store._pg_default_literal("DOUBLE PRECISION", "1400") == "1400"
    assert Store._pg_default_literal("TEXT", None) is None
    # The demo_pending migration entry: BOOLEAN on pg, INTEGER on sqlite, and
    # the stored default is the integer 0 -> FALSE for Postgres (demo NOT
    # shown to migrated users).
    entry = [c for c in Store._MIGRATION_COLUMNS["users"]
             if c[0] == "demo_pending"][0]
    name, pg_type, sq_type, default = entry
    assert pg_type == "BOOLEAN" and sq_type == "INTEGER", entry
    assert Store._pg_default_literal(pg_type, default) == "FALSE", entry
    print("PASS migration BOOLEAN default maps to SQL literal (demo_pending -> FALSE)")


def test_migration_failing_ddl_does_not_abort_remaining_columns():
    """PROD BUGFIX: one failing migration statement must NOT abort the batch.

    Simulates a single bad ALTER by injecting a bogus migration column with an
    invalid type. On the real Postgres this poisoned the transaction so every
    later ADD COLUMN failed with "current transaction is aborted" and the whole
    Store was disabled. After the fix, _migrate_columns isolates each ALTER
    (commit on success / rollback on failure) so the bogus column is skipped
    (logged as a note) while the LEGITIMATE columns before AND after it are
    still added and the Store stays enabled."""
    import storage as storage_mod
    from storage import Store
    orig = Store._MIGRATION_COLUMNS
    # Start from a minimal users/games table (no post-release columns) so the
    # migration actually has work to do, then wedge a failing statement between
    # two real columns.
    poisoned = {
        "users": [
            ("email", "TEXT", "TEXT", None),
            # Syntactically invalid DDL on BOTH backends (a dangling DEFAULT
            # clause) -> this ALTER must fail...
            ("bogus_col", "TEXT DEFAULT (", "TEXT DEFAULT (", None),
            # ...but this one AFTER the failure must still be applied.
            ("demo_pending", "BOOLEAN", "INTEGER", "0"),
        ],
    }
    try:
        Store._MIGRATION_COLUMNS = poisoned
        st = Store("sqlite:///:memory:")
        # A failing DDL in the batch must NOT disable the store.
        assert st.enabled, "store must stay enabled despite one failing ALTER"
        conn = st._connect()
        cur = conn.cursor()
        existing = st._existing_columns(cur, "users")
        # The real columns on BOTH sides of the failure got added...
        assert "email" in existing, existing
        assert "demo_pending" in existing, existing
        # ...and the bogus one was skipped, not fatal.
        assert "bogus_col" not in existing, existing
    finally:
        Store._MIGRATION_COLUMNS = orig
    print("PASS migration isolates a failing DDL and still adds remaining columns")


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
    # FEAT-002: ECO recompute when NULL, NULL mode -> casual, post-game rating
    # column, and the 'timeout' reason code.
    test_storage_eco_recompute_when_null()
    test_storage_null_mode_defaults_casual()
    test_storage_gmt8_date_derivation()
    test_storage_player_rating_after_persisted()
    test_canonical_reason_timeout_code()
    test_time_control_bucket_mapping()
    test_result_class_derivation()
    test_flask_history_page_renders()
    # REVIEW-FIX: end-to-end rating shown to the client on move/resign/resume.
    test_flask_ratings_reflect_update_end_to_end()
    # FEAT-003: account deletion + duplicate-username rejection.
    test_storage_delete_user_cascade()
    test_flask_delete_account()
    test_flask_duplicate_username_rejected()
    # FEAT-006: collections tree + membership + cascade + ownership/auth.
    test_storage_collections_tree_and_membership()
    test_storage_collections_ownership_isolation()
    test_flask_collections_endpoints_auth_and_ownership()
    # FEAT-004: Supabase-gated auth + FIDE ratings seeding at signup.
    test_fide_parser_extracts_ratings()
    test_fide_lookup_ratings_never_raises_and_injects_fetcher()
    test_signup_fide_seeding_maps_and_defaults()
    test_register_full_flow_with_fide_id_bcrypt_path()
    test_bcrypt_fallback_when_supabase_unconfigured()
    test_validate_email_and_email_required_at_signup()
    test_storage_supabase_and_fide_id_columns()
    test_forgot_password_noop_without_supabase()
    # FEAT-005: Syzygy setoption/download, Polyglot book, custom position.
    test_syzygy_setoption_command_sequence()
    test_syzygy_download_disabled_is_noop()
    # FEAT-002 (tablebase-warmup): global status()/progress + endpoint.
    test_syzygy_status_disabled_reports_ready()
    test_syzygy_status_percent_monotonic_to_100()
    test_flask_tablebase_status_endpoint()
    test_book_probe_offline_real_bin()
    test_reply_move_fide_prefers_book_others_do_not()
    # FEAT-004 (tablebase-warmup): probe-marker detection + deferral + clock
    # preservation + resolution endpoint (all offline, injected fakes).
    test_engine_probe_flag_never_leaks_move_only_contract()
    test_engine_reapplies_syzygy_path_live_no_respawn()
    test_fide_move_defers_when_probe_and_not_ready()
    test_fide_deferred_clock_equals_normal_clock_same_think()
    test_fide_resolution_fresh_search_when_ready_charges_no_extra()
    test_fide_move_no_defer_when_probe_but_ready()
    test_casual_move_unaffected_by_probe_marker()
    test_flask_custom_position_casual()
    test_flask_custom_position_bot_moves_first_when_on_move()
    test_storage_start_fen_roundtrip()
    # FEAT-007: offline Lichess puzzle-filtering solve-check + puzzles schema.
    test_filter_puzzles_solve_check_keeps_and_rejects()
    test_storage_puzzle_insert_fetch_and_rating_index()
    test_storage_puzzle_insert_is_idempotent()
    # FEAT-002 (truncation): progressive front-truncation loop + outcomes,
    # puzzle_truncation_stats idempotency + servable/stats split.
    test_filter_puzzles_truncation_loop_and_outcomes()
    test_storage_truncation_stats_idempotent_and_split()
    # FEAT-008: Train tab puzzle rating, bell-curve selection, persistence.
    test_puzzle_rating_helpers_and_displayed_rating()
    test_puzzle_bell_curve_weighting_prefers_nearer()
    test_puzzle_sample_uses_band_and_returns_none_when_empty()
    test_flask_puzzle_endpoints_auth_persist_and_rating()
    # FEAT-010: dashboard rating-history source + endpoint + append on change.
    test_storage_rating_history_roundtrip_and_filter()
    test_flask_finished_game_appends_rating_history()
    # FEAT-002 (follow-up): GMT+8 daily read-only derivation helper + per-day
    # dashboard response shape + All/Custom windows.
    test_daily_series_gmt8_boundary_and_carry_forward()
    test_flask_dashboard_auth_and_series_window()
    test_flask_puzzle_solve_appends_puzzle_history()
    # FEAT-001 (follow-up): onboarding demo is new-users-only (demo_pending).
    test_storage_demo_pending_roundtrip()
    test_flask_demo_gate_seen_and_auth()
    # PROD BUGFIX: BOOLEAN-with-integer-default migration + per-statement
    # isolation so one failing ALTER can't abort the batch (Postgres-only bug
    # that disabled the Store -> guest mode; reproduced on SQLite via helpers).
    test_migration_boolean_default_maps_to_sql_literal()
    test_migration_failing_ddl_does_not_abort_remaining_columns()
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
