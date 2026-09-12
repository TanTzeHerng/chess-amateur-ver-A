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


def main():
    test_engine()
    test_single_process_invariant_across_respawn()
    test_engine_fails_fast_when_binary_broken()
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
