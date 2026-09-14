#!/usr/bin/env python3
"""Create a TINY synthetic Lichess-format puzzle dump (.csv.zst) for offline
verification of the streaming path in filter_puzzles.py.

The FULL production run downloads the real ~5GB dump from
https://database.lichess.org/lichess_db_puzzle.csv.zst; this helper exists only
so the sample run and the offline test suite can exercise the exact same
zstandard-streaming CSV path WITHOUT any network access. The puzzles below are
hand-built so their outcome under Chess Amateur (Stockfish depth 1) is known. FEAT-002's
progressive front-truncation pipeline routes each puzzle to one of THREE
outcomes, and this sample exercises ALL THREE (verified empirically by running
filter_puzzles.py on the regenerated dump -- the engine moves below are NOT
guessed):

  KEEP0001 : INTACT-SOLVED -> SERVABLE. opponent Kg8h8, then solver Ra1a8#
             (a mate-in-1 depth-1 finds). Goes to the `puzzles` table + the
             servable JSONL.
  REJECT01 : DROPPED. A single-solver-move puzzle whose claimed move (Qh4h1)
             is NOT what depth-1 plays (Qh4e4). Nothing to truncate (only one
             solver move), so it is dropped -- recorded nowhere but the
             checkpoint.
  KEEP0002 : INTACT-SOLVED -> SERVABLE. Same back-rank mate motif with the
             rook on the b-file (Rb1b8#), at a different rating, so the sample
             yields >1 servable puzzle and >1 rating band.
  TRUNC001 : TRUNCATION-SOLVED -> puzzle_truncation_stats ONLY (NOT servable).
             Two solver moves: FAILS intact because the solver's first move
             (a2a4, a quiet pawn push) is not what depth-1 plays (it plays the
             immediate mate b1b8). After ONE front-truncation (2 ply removed,
             solver_moves_removed==1) the shortened line is the mate-in-1
             b1b8, which depth-1 finds -> solved. original_solver_move_count==2,
             solver_moves_removed==1.
  DROP0001 : DROPPED. Two solver moves; a free white bishop on b3 is hanging,
             so depth-1 grabs it (Rb8b3) at EVERY solver turn instead of the
             expected quiet king moves. It fails intact AND still fails when
             truncated down to its last single solver move -> dropped.

Columns match the real dump:
  PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags
"""
import csv
import io
import os
import sys

_HEADER = ["PuzzleId", "FEN", "Moves", "Rating", "RatingDeviation",
           "Popularity", "NbPlays", "Themes", "GameUrl", "OpeningTags"]

SAMPLE_ROWS = [
    # INTACT-SOLVED -> SERVABLE: forced back-rank mate the depth-1 engine finds.
    ["KEEP0001", "6k1/5ppp/8/8/8/8/8/R6K b - - 0 1", "g8h8 a1a8",
     "800", "75", "90", "1000", "mateIn1 backRankMate",
     "https://lichess.org/example#0", ""],
    # DROPPED (single solver move): the claimed solver move (Qh4h1) is NOT what
    # depth-1 plays (Qh4e4); with only ONE solver move there is nothing to
    # truncate, so it is dropped.
    ["REJECT01", "4k3/8/8/8/7q/8/8/R3K2R w - - 0 1", "e1e2 h4h1",
     "1600", "80", "50", "500", "quietMove",
     "https://lichess.org/example#1", ""],
    # A second INTACT-SOLVED -> SERVABLE back-rank mate (rook on the b-file so
    # Rb1b8# is forced), at a different rating so the sample yields >1 servable
    # puzzle and >1 rating band.
    ["KEEP0002", "6k1/5ppp/8/8/8/8/8/1R5K b - - 0 1", "g8h8 b1b8",
     "1200", "70", "80", "800", "mateIn1 backRankMate",
     "https://lichess.org/example#2", ""],
    # TRUNCATION-SOLVED -> puzzle_truncation_stats ONLY (NOT servable).
    # Two solver moves. FAILS intact: the solver's first move a2a4 (a quiet
    # pawn push) is not what depth-1 plays (it plays the immediate mate b1b8).
    # After ONE front-truncation (remove a2a4 + the opponent reply h8g8) the
    # shortened line is the mate-in-1 b1b8, which depth-1 finds -> solved.
    # Verified: original_solver_move_count==2, solver_moves_removed==1.
    ["TRUNC001", "6k1/5ppp/8/8/8/8/P7/1R5K b - - 0 1", "g8h8 a2a4 h8g8 b1b8",
     "1000", "78", "70", "900", "mateIn2",
     "https://lichess.org/example#3", ""],
    # DROPPED (multi-move, fails even at the last single solver move). A free
    # white bishop on b3 is hanging, so depth-1 grabs it (b8b3) at EVERY solver
    # turn instead of the expected quiet king moves; it fails intact AND still
    # fails when truncated to its last single solver move -> dropped.
    ["DROP0001", "1r4k1/5ppp/8/8/8/1B6/6PP/6K1 w - - 0 1",
     "g1h1 g8h8 h1g1 h8g8",
     "1400", "82", "40", "300", "quietMove",
     "https://lichess.org/example#4", ""],
]


def build_bytes():
    """Return the .zst-compressed bytes of the synthetic dump."""
    import zstandard
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_HEADER)
    for row in SAMPLE_ROWS:
        w.writerow(row)
    raw = buf.getvalue().encode("utf-8")
    return zstandard.ZstdCompressor().compress(raw)


def write(path):
    with open(path, "wb") as fh:
        fh.write(build_bytes())
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "sample_lichess_db_puzzle.csv.zst")
    write(out)
    print("wrote synthetic dump:", out)
