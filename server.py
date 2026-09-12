#!/usr/bin/env python3
"""HTTP backend for playing chess against "Chess Amateur" (Stockfish depth 1).

Uses only the Python standard library (http.server) plus python-chess. No
external web framework. python-chess is the single source of truth for board
state, move legality, SAN, FEN, and result detection.

STATELESS DESIGN (survives multiple workers / process restarts):
  The authoritative game state is carried by the CLIENT as an ordered list of
  UCI moves ("moves"). The server never depends on its own memory to serve a
  move: it rebuilds the position by replaying the client-supplied moves onto a
  fresh chess.Board, using python-chess as the single source of truth. This is
  what makes the app correct on hosts like Render, where the process that
  served POST /api/new is not guaranteed to be the one serving a later
  POST /api/move (multiple workers, cold starts, recycles). A "game_id" is
  still issued for display/compatibility, but it is NON-authoritative; the
  in-memory GAMES dict is only a best-effort cache and correctness never
  depends on an entry being present.

Endpoints:
  POST /api/new    body {"human_color": "white"|"black", "threads": 1..128?}
  POST /api/move   body {"moves": ["e2e4", ...], "move": "e7e5",
                         "human_color": "white"|"black", "threads": 1..128?,
                         "game_id": ...?}
                   ("moves" is the authoritative history so far; "move" is the
                    new human move to apply. "game_id" is optional/display-only.)
  GET  /api/state?game_id=...   (best-effort; rebuilds from cache if present)
  GET  /            -> static/index.html
  GET  /<path>      -> static/<path>

Environment:
  PORT            server port (default 8000)
  STOCKFISH_PATH  Stockfish binary path (see engine.py)
  SF_THREADS      server-wide default Stockfish thread count (default 128).
                  Per-request "threads" in POST /api/new overrides this.

Run: python3 chess_amateur/server.py
"""
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import chess

from engine import ChessAmateurEngine, DEFAULT_THREADS, EngineUnavailable

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
BOT_NAME = "Chess Amateur"


def log(msg):
    """Write a timestamped log line to stdout and FLUSH immediately.

    In a container Python's stdout is block-buffered, so bare print() output
    can sit unflushed and never reach the platform's log collector (observed
    on Render: build logs present, zero application logs). Flushing on every
    line guarantees the platform captures startup + per-request logs so a
    failing move is diagnosable instead of invisible.
    """
    sys.stdout.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stdout.flush()

# Allowed range for the Stockfish "Threads" option (per game).
MIN_THREADS = 1
MAX_THREADS = 128


def _clamp_threads(value):
    """Clamp a candidate thread count to [MIN_THREADS, MAX_THREADS]."""
    return max(MIN_THREADS, min(MAX_THREADS, int(value)))


def _server_default_threads():
    """Server-wide default thread count: SF_THREADS env, else 128.

    An unset, invalid, or out-of-range SF_THREADS falls back to the hard-coded
    DEFAULT_THREADS (128), clamped into the valid range.
    """
    raw = os.environ.get("SF_THREADS")
    if raw is None:
        return DEFAULT_THREADS
    try:
        return _clamp_threads(raw)
    except (TypeError, ValueError):
        return DEFAULT_THREADS


# Server-wide default applied when a game does not request a specific count.
DEFAULT_GAME_THREADS = _server_default_threads()


def _coerce_threads(value):
    """Resolve a per-request threads value.

    Priority: a valid integer in range from the request body wins; anything
    missing, non-integer, or out of range falls back to the server default
    (SF_THREADS env, else 128). Forgiving by design: never errors.
    """
    if value is None:
        return DEFAULT_GAME_THREADS
    if isinstance(value, bool):  # bool is an int subclass; reject it.
        return DEFAULT_GAME_THREADS
    if not isinstance(value, int):
        # Accept numeric strings like "2" but reject junk.
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            return DEFAULT_GAME_THREADS
    if value < MIN_THREADS or value > MAX_THREADS:
        return DEFAULT_GAME_THREADS
    return value


# Shared, thread-safe engine instance reused across all games/requests.
ENGINE = ChessAmateurEngine()

# In-memory game store: game_id -> {board, human_color, san_history, threads}
GAMES = {}
GAMES_LOCK = threading.Lock()

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


# -- game-state helpers ----------------------------------------------------

def _result_reason(board):
    """Human-readable reason the game ended, or None if still in progress."""
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "insufficient material"
    if board.is_seventyfive_moves() or board.can_claim_fifty_moves():
        return "fifty-move rule"
    if board.is_fivefold_repetition() or board.can_claim_threefold_repetition():
        return "repetition"
    return None


def _status(board):
    if board.is_game_over(claim_draw=True):
        return "game_over"
    return "white_to_move" if board.turn == chess.WHITE else "black_to_move"


def _legal_moves(board):
    return [m.uci() for m in board.legal_moves]


def _turn_str(board):
    return "white" if board.turn == chess.WHITE else "black"


def _engine_move(board, threads=None):
    """Ask Chess Amateur for a move, validate it, push it. Returns (uci, san)
    or (None, None) if no move was made. `threads` sets the Stockfish Threads
    option for this move."""
    uci = ENGINE.best_move(board.fen(), threads=threads)
    if not uci:
        return None, None
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None, None
    if move not in board.legal_moves:
        return None, None
    san = board.san(move)
    board.push(move)
    return uci, san


class InvalidMoveHistory(ValueError):
    """Raised when a client-supplied move list cannot be legally replayed."""


def _rebuild_board(moves):
    """Replay a client-supplied list of UCI moves onto a fresh board.

    python-chess is the single source of truth: every move must be a legal
    continuation of the game, otherwise the whole history is rejected. Returns
    (board, san_history, moves_uci) where san_history is regenerated
    server-side (client SAN is never trusted) and moves_uci is the normalized
    UCI list actually applied.

    A missing/empty list yields the starting position. This is what lets a
    fresh server process (empty memory) reconstruct any game authoritatively.
    """
    board = chess.Board()
    san_history = []
    moves_uci = []
    if not moves:
        return board, san_history, moves_uci
    if not isinstance(moves, (list, tuple)):
        raise InvalidMoveHistory("moves must be a list of UCI strings")
    for raw in moves:
        try:
            move = chess.Move.from_uci(str(raw))
        except (ValueError, TypeError):
            raise InvalidMoveHistory("invalid move in history: %r" % (raw,))
        if move not in board.legal_moves:
            raise InvalidMoveHistory("illegal move in history: %r" % (raw,))
        san_history.append(board.san(move))
        board.push(move)
        moves_uci.append(move.uci())
    return board, san_history, moves_uci


def _state_dict(board, human_color, threads, san_history, moves_uci,
                game_id=None, last_bot_move=None):
    """Serialize a game position into the API state shape.

    Built purely from primitives (board + carried metadata) so it never
    depends on the in-memory GAMES store. `moves` is the authoritative UCI
    history the client must echo back on the next /api/move.
    """
    game_over = board.is_game_over(claim_draw=True)
    result = board.result(claim_draw=True) if game_over else None
    return {
        "game_id": game_id,
        "fen": board.fen(),
        "human_color": human_color,
        "threads": threads,
        "turn": _turn_str(board),
        "legal_moves": _legal_moves(board),
        "status": _status(board),
        "san_history": list(san_history),
        "moves": list(moves_uci),
        "bot_name": BOT_NAME,
        "last_bot_move": last_bot_move,
        "game_over": game_over,
        "result": result,
        "result_reason": _result_reason(board) if game_over else None,
    }


def _cache_game(game_id, board, human_color, threads, san_history, moves_uci):
    """Best-effort, NON-authoritative cache of a game by id. Correctness never
    depends on this; it exists only so GET /api/state can answer quickly when
    the same process is hit again."""
    if not game_id:
        return
    with GAMES_LOCK:
        GAMES[game_id] = {
            "game_id": game_id,
            "board": board,
            "human_color": human_color,
            "threads": threads,
            "san_history": list(san_history),
            "moves": list(moves_uci),
        }


# -- request handler -------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ChessAmateur/1.0"

    def log_message(self, fmt, *args):
        # Route request logs through the flushed logger so they reach the
        # platform log collector (default BaseHTTPRequestHandler writes to an
        # unflushed stderr stream).
        log("%s - %s" % (self.address_string(), fmt % args))

    # -- helpers --
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({"error": message}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # -- routing --
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            # Cheap liveness probe (no engine call) for platform health checks
            # and manual reachability testing.
            return self._send_json({"status": "ok", "bot": BOT_NAME})
        if path == "/api/state":
            return self._handle_state(parse_qs(parsed.query))
        if path.startswith("/api/"):
            return self._send_error_json("not found", status=404)
        return self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/new":
                return self._handle_new()
            if path == "/api/move":
                return self._handle_move()
        except json.JSONDecodeError:
            return self._send_error_json("invalid JSON body", status=400)
        except EngineUnavailable as exc:
            # The Stockfish engine failed to start or respond (e.g. an
            # incompatible binary that dies on launch). Fail FAST with a clear
            # 500 instead of hanging until the platform proxy times out (502).
            log("ENGINE UNAVAILABLE on %s: %s" % (path, exc))
            return self._send_error_json("engine unavailable", status=500)
        return self._send_error_json("not found", status=404)

    # -- API handlers --
    def _handle_new(self):
        body = self._read_json_body()
        human_color = str(body.get("human_color", "white")).lower()
        if human_color not in ("white", "black"):
            return self._send_error_json("human_color must be 'white' or 'black'", status=400)

        threads = _coerce_threads(body.get("threads"))

        game_id = str(uuid.uuid4())
        board = chess.Board()
        san_history = []
        moves_uci = []

        last_bot_move = None
        # If the human is Black, Chess Amateur (White) moves first.
        if human_color == "black" and not board.is_game_over(claim_draw=True):
            uci, san = _engine_move(board, threads=threads)
            if uci:
                san_history.append(san)
                moves_uci.append(uci)
                last_bot_move = {"uci": uci, "san": san}

        # Best-effort cache only; the client carries the authoritative moves.
        _cache_game(game_id, board, human_color, threads, san_history, moves_uci)
        return self._send_json(_state_dict(
            board, human_color, threads, san_history, moves_uci,
            game_id=game_id, last_bot_move=last_bot_move))

    def _handle_move(self):
        body = self._read_json_body()
        game_id = body.get("game_id")
        move_uci = body.get("move")
        human_color = str(body.get("human_color", "white")).lower()
        if human_color not in ("white", "black"):
            human_color = "white"
        threads = _coerce_threads(body.get("threads"))

        # STATELESS: rebuild the authoritative position by replaying the
        # client-supplied move history onto a fresh board. This works even if
        # this process has never seen this game (multiple workers / restarts).
        try:
            board, san_history, moves_uci = _rebuild_board(body.get("moves"))
        except InvalidMoveHistory:
            return self._send_error_json("invalid move history", status=400)

        if board.is_game_over(claim_draw=True):
            return self._send_error_json("game is over", status=400)

        # Parse + validate the human move. Illegal => 400, no state change.
        try:
            move = chess.Move.from_uci(str(move_uci))
        except (ValueError, TypeError):
            return self._send_error_json("illegal move", status=400)
        if move not in board.legal_moves:
            return self._send_error_json("illegal move", status=400)

        # Apply human move (record SAN before pushing).
        human_san = board.san(move)
        board.push(move)
        san_history.append(human_san)
        moves_uci.append(move.uci())

        # Chess Amateur replies if the game continues.
        last_bot_move = None
        if not board.is_game_over(claim_draw=True):
            log("move: human %s (history=%d plies, threads=%d) -> asking engine"
                % (move.uci(), len(moves_uci), threads))
            t0 = time.monotonic()
            uci, san = _engine_move(board, threads=threads)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if uci:
                san_history.append(san)
                moves_uci.append(uci)
                last_bot_move = {"uci": uci, "san": san}
                log("move: engine replied %s (%s) in %d ms" % (uci, san, elapsed_ms))
            else:
                log("move: engine returned no move in %d ms" % elapsed_ms)

        # Refresh the best-effort cache (still non-authoritative).
        _cache_game(game_id, board, human_color, threads, san_history, moves_uci)
        return self._send_json(_state_dict(
            board, human_color, threads, san_history, moves_uci,
            game_id=game_id, last_bot_move=last_bot_move))

    def _handle_state(self, query):
        game_id_list = query.get("game_id")
        game_id = game_id_list[0] if game_id_list else None
        with GAMES_LOCK:
            game = GAMES.get(game_id)
        if game is None:
            return self._send_error_json("unknown game_id", status=404)
        return self._send_json(_state_dict(
            game["board"], game["human_color"],
            game.get("threads", DEFAULT_GAME_THREADS),
            game["san_history"], game.get("moves", []),
            game_id=game_id))

    # -- static files --
    def _serve_static(self, path):
        if path == "/" or path == "":
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        # Prevent path traversal: resolve and confirm the target stays inside
        # the static directory.
        static_root = os.path.realpath(STATIC_DIR)
        full = os.path.realpath(os.path.join(static_root, rel))
        if full != static_root and not full.startswith(static_root + os.sep):
            return self._send_error_json("forbidden", status=403)
        if not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log("%s server starting on 0.0.0.0:%d" % (BOT_NAME, port))
    log("engine binary: %s" % ENGINE.path)
    log("default threads: %d (SF_THREADS=%s)"
        % (DEFAULT_GAME_THREADS, os.environ.get("SF_THREADS", "<unset>")))
    log("ready: open http://localhost:%d/  (health: /healthz)" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ENGINE.close()


if __name__ == "__main__":
    main()
