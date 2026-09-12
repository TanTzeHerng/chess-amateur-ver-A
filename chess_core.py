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


def rebuild_board(moves):
    """Replay a list of UCI moves onto a fresh board.

    Returns (board, san_history, moves_uci). python-chess validates every move;
    an illegal continuation raises InvalidMoveHistory. Empty/None -> start pos.
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


def engine_move(board, threads=None):
    """Ask Chess Amateur for a move, validate it, push it. Returns (uci, san)
    or (None, None). The engine logs its full UCI output to the server logs;
    only the move is returned here (never the eval/PV)."""
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


def state_dict(board, human_color, threads, san_history, moves_uci,
               game_id=None, last_bot_move=None, extra=None):
    """Serialize a position into the API state shape used by the frontend.

    `extra` merges additional keys (e.g. account/persistence fields) without
    the core needing to know about them. `moves` is the authoritative UCI
    history the client echoes back on the next move.
    """
    game_over = board.is_game_over(claim_draw=True)
    result = board.result(claim_draw=True) if game_over else None
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
        "bot_name": BOT_NAME,
        "last_bot_move": last_bot_move,
        "game_over": game_over,
        "result": result,
        "result_reason": result_reason(board) if game_over else None,
    }
    if extra:
        out.update(extra)
    return out
