#!/usr/bin/env python3
"""Polyglot opening-book probing for Chess Amateur (FIDE-rated mode only).

Stockfish itself has NO built-in Polyglot (.bin) book support, so the book is
consulted in the APP LAYER, in front of the engine: given a position, we open
the Polyglot book (pc2500.bin), look up the entries for the position's Polyglot
Zobrist key, and pick a move WEIGHTED by the entry weights. If the position is
not in the book we return None and the caller falls through to the normal
Stockfish depth-1 selection.

python-chess computes the Polyglot Zobrist key and reads the book for us
(chess.polyglot); we never hand-roll the hash. Probing does NOT spawn any
engine process, so the single-Stockfish-process invariant is preserved.

The book file path is resolvable relative to this module (like eco.json) and is
overridable via the POLYGLOT_BOOK environment variable.
"""
import os

import chess
import chess.polyglot

# Default book path: pc2500.bin sitting next to this module (shipped in the
# image via the Dockerfile COPY step). Overridable with POLYGLOT_BOOK.
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BOOK_PATH = os.path.join(_HERE, "pc2500.bin")


def book_path():
    """Resolve the Polyglot book path (env override -> module-relative default)."""
    return os.environ.get("POLYGLOT_BOOK") or DEFAULT_BOOK_PATH


def book_move(board, rng=None, path=None):
    """Return a weighted-random book move (UCI str) for `board`, or None.

    Opens the Polyglot book, finds entries for the position and picks one
    weighted by its entry weight. Returns None when the position is not in the
    book, the book file is missing/unreadable, or no legal entry exists. Never
    raises: any error degrades to None so the caller falls through to Stockfish.

    `rng` (a random.Random) is injectable so tests can make the weighted pick
    deterministic. `path` overrides the book file (defaults to book_path()).
    """
    resolved = path or book_path()
    try:
        with chess.polyglot.open_reader(resolved) as reader:
            entry = reader.weighted_choice(board, random=rng)
    except (IndexError, FileNotFoundError, OSError, ValueError):
        # IndexError: position absent from the book (weighted_choice raises it).
        # The rest: missing/corrupt book file.
        return None
    move = entry.move
    # Only return a move that is actually legal in this position (defensive:
    # a book could in principle carry a move illegal here).
    if move not in board.legal_moves:
        return None
    return move.uci()


def in_book(board, path=None):
    """True if `board`'s position has at least one Polyglot book entry."""
    resolved = path or book_path()
    try:
        with chess.polyglot.open_reader(resolved) as reader:
            for _ in reader.find_all(board):
                return True
    except (FileNotFoundError, OSError, ValueError):
        return False
    return False
