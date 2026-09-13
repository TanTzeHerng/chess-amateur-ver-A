#!/usr/bin/env python3
"""ECO (Encyclopaedia of Chess Openings) classification for Chess Amateur.

Pure-python, no new pip dependencies (python-chess is already a dependency and
is used only to convert the bundled SAN move sequences into UCI so a game's
UCI history can be matched directly).

Design:
  * A compact, hand-bundled ECO dataset (eco.json) lives next to this module.
    Each entry is [eco_code, opening_name, defining_moves_in_SAN].
  * classify(moves_uci) converts each dataset entry's SAN moves to UCI once
    (cached on first use) and returns the entry whose full UCI move sequence is
    the LONGEST prefix of the game's moves. Longest-prefix match means a more
    specific/deeper opening line wins over a shorter one.
  * Games too short to match any entry (or that diverge immediately) return
    None.

The dataset file MUST be shipped alongside this module (see the Dockerfile
COPY line) or classification silently degrades to "no match" (returns None).
"""
import json
import os

import chess

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATASET_PATH = os.path.join(_HERE, "eco.json")

# Lazily-built list of (eco_code, name, tuple_of_uci_moves), sorted so that
# longer sequences are considered first (longest-prefix match).
_ENTRIES = None


def _san_sequence_to_uci(san_moves):
    """Convert a list of SAN moves to a tuple of UCI strings by replaying them
    on a fresh board. Returns None if any SAN token is not legal (a malformed
    dataset row is skipped rather than crashing classification)."""
    board = chess.Board()
    uci = []
    for san in san_moves:
        try:
            move = board.parse_san(san)
        except (ValueError, AssertionError):
            return None
        uci.append(move.uci())
        board.push(move)
    return tuple(uci)


def _load_entries():
    """Load + compile the dataset into (code, name, uci_tuple) rows once.

    Sorted by descending sequence length so classify() can return the first
    (i.e. longest) prefix match it finds.
    """
    global _ENTRIES
    if _ENTRIES is not None:
        return _ENTRIES
    entries = []
    try:
        with open(_DATASET_PATH, "r") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        _ENTRIES = []
        return _ENTRIES
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            continue
        code, name, moves_str = row[0], row[1], row[2]
        san_moves = str(moves_str).split()
        uci_seq = _san_sequence_to_uci(san_moves)
        if uci_seq:
            entries.append((code, name, uci_seq))
    entries.sort(key=lambda e: len(e[2]), reverse=True)
    _ENTRIES = entries
    return _ENTRIES


def classify(moves_uci):
    """Classify a game by its UCI move list via LONGEST move-prefix match.

    moves_uci: list/tuple of UCI move strings (the game's leading moves).
    Returns {'eco': code, 'name': name} for the deepest dataset opening whose
    full move sequence is a prefix of the game, or None when nothing matches
    (e.g. the game is empty/too short or diverges from every known line).
    """
    if not moves_uci:
        return None
    game = tuple(str(m) for m in moves_uci)
    n = len(game)
    for code, name, seq in _load_entries():
        if len(seq) <= n and game[:len(seq)] == seq:
            return {"eco": code, "name": name}
    return None


def classify_san(san_list):
    """Convenience: classify from a SAN move list (converts to UCI first).

    Returns the same shape as classify(), or None on an unparseable SAN list.
    """
    uci = _san_sequence_to_uci(list(san_list or []))
    if uci is None:
        return None
    return classify(list(uci))
