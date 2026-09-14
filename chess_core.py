#!/usr/bin/env python3
"""Framework-neutral chess helpers for Chess Amateur.

This module holds the proven, engine-facing game logic so it can be reused by
the Flask app WITHOUT changing any behavior:
  * python-chess is the single source of truth for legality/SAN/FEN/game-over.
  * The game is STATELESS on the server: a position is rebuilt by replaying a
    client/DB-supplied list of UCI moves.
  * The engine (ChessAmateurEngine) plays depth 1; it logs full UCI output to
    the server logs (operator-only) and returns ONLY the bestmove -- the player
    never sees the eval/PV.

The single shared engine instance and the thread-count resolution live here so
both any legacy stdlib server and the new Flask app share identical behavior.
"""
import os

import chess

import book
from engine import ChessAmateurEngine, DEFAULT_THREADS, EngineUnavailable  # noqa: F401

BOT_NAME = "Chess Amateur"

MIN_THREADS = 1
MAX_THREADS = 128


# -- thread-count resolution (per-request > SF_THREADS env > 128) ----------

def _clamp_threads(value):
    return max(MIN_THREADS, min(MAX_THREADS, int(value)))


def _server_default_threads():
    raw = os.environ.get("SF_THREADS")
    if raw is None:
        return DEFAULT_THREADS
    try:
        return _clamp_threads(raw)
    except (TypeError, ValueError):
        return DEFAULT_THREADS


DEFAULT_GAME_THREADS = _server_default_threads()


def coerce_threads(value):
    """Resolve a per-request threads value; forgiving (never raises)."""
    if value is None:
        return DEFAULT_GAME_THREADS
    if isinstance(value, bool):
        return DEFAULT_GAME_THREADS
    if not isinstance(value, int):
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            return DEFAULT_GAME_THREADS
    if value < MIN_THREADS or value > MAX_THREADS:
        return DEFAULT_GAME_THREADS
    return value


# -- shared engine ---------------------------------------------------------

ENGINE = ChessAmateurEngine()


# -- board / state helpers -------------------------------------------------

def result_reason(board):
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


# -- canonical, winner-phrased result reason strings ----------------------
#
# The frontend shows a single human string for how a game ended. It is ALWAYS
# phrased from the WINNER's color and ALWAYS uses "won" (never "lost"); draws
# are phrased "Draw by ...". These are the AUTHORITATIVE strings from the spec.
#
# The winner's color is derived from the RESULT string ('1-0' -> White wins,
# '0-1' -> Black wins). The internal `reason` codes (as emitted by
# result_reason() below, plus 'resignation'/'time forfeit' set by app.py) are
# mapped to the canonical wording. Raw code fields are preserved for storage;
# this function produces the DISPLAY string only.

def _winner_color(result):
    """Winner color word ('White'/'Black') from a result string, else None."""
    if result == "1-0":
        return "White"
    if result == "0-1":
        return "Black"
    return None


def canonical_reason(result, reason):
    """Map (result, internal reason code) to the canonical display string.

    result: '1-0' / '0-1' / '1/2-1/2'. reason: one of the internal codes
    ('checkmate', 'resignation', 'time forfeit'/'time', 'repetition',
    'stalemate', 'insufficient material', 'fifty-move rule'). Returns the
    winner-phrased string, or None if it cannot be determined.
    """
    winner = _winner_color(result)
    code = (reason or "").lower()

    # Winner-phrased decisive endings.
    if code == "checkmate" and winner:
        return "%s won by checkmate" % winner
    if code == "resignation" and winner:
        return "%s won by resignation" % winner
    if code in ("time forfeit", "time", "on time", "timeout") and winner:
        return "%s won on time" % winner

    # Draw endings (phrased "Draw by ...").
    if code == "repetition":
        return "Draw by 3-fold repetition"
    if code == "stalemate":
        return "Draw by stalemate"
    if code == "insufficient material":
        return "Draw by insufficient material"
    if code == "fifty-move rule":
        return "Draw by 50-move rule"
    return None


def result_line(result, reason):
    """Move-log tail: '1-0' / '0-1' / '1/2-1/2' plus the canonical reason in
    parentheses, e.g. '1-0 (White won by checkmate)'. If the reason cannot be
    resolved, returns just the result string. Returns None without a result."""
    if not result:
        return None
    reason_text = canonical_reason(result, reason)
    if reason_text:
        return "%s (%s)" % (result, reason_text)
    return result


def status_str(board):
    if board.is_game_over(claim_draw=True):
        return "game_over"
    return "white_to_move" if board.turn == chess.WHITE else "black_to_move"


def legal_moves(board):
    return [m.uci() for m in board.legal_moves]


def turn_str(board):
    return "white" if board.turn == chess.WHITE else "black"


class InvalidMoveHistory(ValueError):
    """Raised when a client/DB-supplied move list cannot be legally replayed."""


class InvalidStartFen(ValueError):
    """Raised when a custom start FEN cannot be parsed by python-chess."""


def board_from_start_fen(start_fen):
    """Build a fresh board from an optional custom start FEN.

    None/empty -> the standard start position. A non-empty FEN is validated
    with python-chess; an invalid FEN raises InvalidStartFen so the caller can
    return a clear 400. This is the STATELESS custom-position anchor: the client
    carries start_fen + moves and the server replays moves onto this board.
    """
    if not start_fen:
        return chess.Board()
    try:
        return chess.Board(str(start_fen))
    except (ValueError, TypeError) as exc:
        raise InvalidStartFen("invalid FEN: %s" % (exc,))


def rebuild_board(moves, start_fen=None):
    """Replay a list of UCI moves onto a board (optionally a custom start FEN).

    Returns (board, san_history, moves_uci). python-chess validates every move;
    an illegal continuation raises InvalidMoveHistory. Empty/None moves -> the
    start position (standard, or `start_fen` when supplied). An invalid
    `start_fen` raises InvalidStartFen.
    """
    board = board_from_start_fen(start_fen)
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


def engine_move(board, threads=None):
    """Ask Chess Amateur for a move, validate it, push it. Returns (uci, san)
    or (None, None). The engine logs its full UCI output to the server logs;
    only the move is returned here (never the eval/PV)."""
    uci, _san, _probe = engine_move_ex(board, threads=threads)
    return uci, _san


def engine_move_ex(board, threads=None):
    """Like engine_move(), but also report whether the search emitted the
    CA_TB_ABOUT_TO_PROBE marker.

    Returns (uci, san, tb_probe_seen). tb_probe_seen is True iff the patched
    engine signalled it was about to probe a searched position whose tablebase
    is absent/insufficient. The marker itself is NEVER returned to the browser;
    it only informs the app-layer deferral policy (FEAT-004). The move is
    validated + pushed exactly as in engine_move(). On no legal move / invalid
    move the pushed state is unchanged and (None, None, tb_probe_seen) is
    returned (the probe flag is still reported so the caller can decide).
    """
    uci, tb_probe_seen = ENGINE.best_move_with_probe_flag(
        board.fen(), threads=threads)
    if not uci:
        return None, None, tb_probe_seen
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None, None, tb_probe_seen
    if move not in board.legal_moves:
        return None, None, tb_probe_seen
    san = board.san(move)
    board.push(move)
    return uci, san, tb_probe_seen


def _apply_book_uci(board, uci):
    """Validate + push a book UCI move. Returns (uci, san) or (None, None)."""
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


def reply_move(board, mode=None, threads=None, rng=None):
    """Choose Chess Amateur's reply for the given mode, validate + push it.

    FIDE-rated mode ONLY: first probe the Polyglot book (pc2500.bin); if the
    position is in the book, play a weighted book move. Otherwise (not in book,
    or any other mode) fall through to normal Stockfish depth-1 selection via
    engine_move. Rated/Casual modes NEVER consult the book.

    Book probing does NOT spawn an engine, preserving the single-process
    invariant. `rng` is injectable so tests can make the weighted pick
    deterministic. Returns (uci, san) or (None, None).
    """
    if mode == "fide":
        uci = book.book_move(board, rng=rng)
        if uci:
            got_uci, san = _apply_book_uci(board, uci)
            if got_uci:
                return got_uci, san
        # Not in book (or unusable) -> fall through to the engine.
    return engine_move(board, threads=threads)


def reply_move_ex(board, mode=None, threads=None, rng=None, push=True):
    """Choose Chess Amateur's reply and report deferral-relevant metadata.

    Returns a dict:
      {"uci": str|None, "san": str|None,
       "from_book": bool,   # True iff the move came from the Polyglot book
       "tb_probe_seen": bool}  # True iff the engine search hit CA_TB_ABOUT_TO_PROBE

    Book moves (FIDE only) NEVER trigger deferral: from_book=True implies
    tb_probe_seen=False (the book path does not spawn/consult the engine).

    When `push` is True (default) the chosen move is validated + pushed onto
    `board` exactly like reply_move(). When `push` is False the board is left
    UNCHANGED (the move is still returned/validated as legal) -- this lets the
    app-layer decide to DEFER an engine move (marker fired AND tablebases not
    ready) without ever committing a tablebase-absent search result.

    Single-process invariant preserved: the book probe never spawns an engine;
    the engine path uses the single shared ENGINE.
    """
    if mode == "fide":
        book_uci = book.book_move(board, rng=rng)
        if book_uci:
            # Validate the book move without necessarily pushing.
            try:
                move = chess.Move.from_uci(book_uci)
            except ValueError:
                move = None
            if move is not None and move in board.legal_moves:
                san = board.san(move)
                if push:
                    board.push(move)
                return {"uci": book_uci, "san": san,
                        "from_book": True, "tb_probe_seen": False}
        # Not in book (or unusable) -> fall through to the engine.

    # Engine path: run the search WITHOUT pushing so the caller can defer.
    uci, tb_probe_seen = ENGINE.best_move_with_probe_flag(
        board.fen(), threads=threads)
    if not uci:
        return {"uci": None, "san": None,
                "from_book": False, "tb_probe_seen": tb_probe_seen}
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return {"uci": None, "san": None,
                "from_book": False, "tb_probe_seen": tb_probe_seen}
    if move not in board.legal_moves:
        return {"uci": None, "san": None,
                "from_book": False, "tb_probe_seen": tb_probe_seen}
    san = board.san(move)
    if push:
        board.push(move)
    return {"uci": uci, "san": san,
            "from_book": False, "tb_probe_seen": tb_probe_seen}


def state_dict(board, human_color, threads, san_history, moves_uci,
               game_id=None, last_bot_move=None, extra=None, start_fen=None):
    """Serialize a position into the API state shape used by the frontend.

    `extra` merges additional keys (e.g. account/persistence fields) without
    the core needing to know about them. `moves` is the authoritative UCI
    history the client echoes back on the next move. `start_fen` is the custom
    starting position (None for a standard game); the client echoes it back so
    a custom-position game replays statelessly.
    """
    game_over = board.is_game_over(claim_draw=True)
    result = board.result(claim_draw=True) if game_over else None
    reason = result_reason(board) if game_over else None
    out = {
        "game_id": game_id,
        "fen": board.fen(),
        "human_color": human_color,
        "threads": threads,
        "turn": turn_str(board),
        "legal_moves": legal_moves(board),
        "status": status_str(board),
        "san_history": list(san_history),
        "moves": list(moves_uci),
        # Custom starting position (None for a standard game). The client
        # carries this back so the stateless replay anchors correctly.
        "start_fen": start_fen,
        "bot_name": BOT_NAME,
        "last_bot_move": last_bot_move,
        "game_over": game_over,
        "result": result,
        "result_reason": reason,
        # Move-log tail ('1-0 (White won by checkmate)') when the game is over.
        "result_line": result_line(result, reason) if game_over else None,
    }
    if extra:
        out.update(extra)
    return out
