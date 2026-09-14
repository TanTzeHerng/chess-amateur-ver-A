#!/usr/bin/env python3
"""OFFLINE Lichess puzzle-filtering job for Chess Amateur (FEAT-007 / FEAT-002).

WHAT THIS DOES
--------------
Downloads the FULL official Lichess puzzle dump, streams it, and for EVERY
puzzle asks Chess Amateur (Stockfish at depth 1, the exact same engine the app
plays with) to solve it. A puzzle "solves" only if Chess Amateur's best move
equals the puzzle's expected solution move at EVERY step where the solver is to
move (see solve_check -- semantics unchanged).

FEAT-002 (progressive front-truncation) replaces the old discard-on-fail with
three outcome categories per puzzle:

  * INTACT   -- solved with the FULL line (plies_removed == 0). This is the
                only servable category: it is inserted into the `puzzles` table
                and mirrored to the servable JSONL, exactly as before.
  * TRUNCATED -- FAILED intact but SOLVED after removing the first 2 ply from
                the FRONT one or more times (the solver's first move + the
                opponent's reply). Each truncation drops exactly ONE solver
                move. These are NOT servable: they are written ONLY to the
                puzzle_truncation_stats table + the analysis CSV/JSONL, as a
                CORRELATION-STUDY dataset (original_solver_move_count,
                solver_moves_removed, plies_removed, lichess_rating, ...) so a
                rating model for truncated puzzles can be built later. They get
                NO rating in the app and are never served.
  * DROPPED  -- did not solve even when truncated down to its LAST single
                solver move (or a single-solver-move puzzle that failed intact,
                or a malformed/empty line). Recorded NOWHERE except the
                checkpoint. The user only wants "intuitive" puzzles, so these
                unintuitive ones are discarded.

Because the runtime app serves ONLY from the `puzzles` table (app.py
_assign_puzzle -> Store.puzzle_rating_extent()/fetch_puzzles_in_rating_band()),
truncated puzzles are excluded from serving BY CONSTRUCTION -- they simply are
never inserted there. Both the `puzzles` and `puzzle_truncation_stats` tables
are created idempotently by Store(url) construction, so this job creates them
itself without any deploy ordering dependency.

WHERE THIS RUNS
---------------
This is a ONE-TIME OFFLINE job run in the sandbox (open internet, ample disk).
It MUST NEVER run on the Render web instance or at request time: it lives under
chess_amateur/tools/, is NOT added to the Dockerfile COPY, and is NOT imported
by app.py (app.py imports `puzzles as P`, the bell-curve module, NOT this
module). The web app only QUERIES the `puzzles` table at runtime.

LICHESS PUZZLE FORMAT
---------------------
CSV columns:
  PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags
* FEN is the position BEFORE the opponent's first move.
* Moves is a space-separated UCI list. The FIRST move is the OPPONENT's setup
  move; then the SOLVER moves, alternating. So the solver is to move at indices
  1, 3, 5, ... We apply the opponent moves to advance the position and check
  the engine's choice only on the solver's moves.

RESUMABILITY
------------
The job is resumable/incremental: before running the engine on a puzzle it
skips any lichess_id already present in the output DB (Store.puzzle_lichess_ids)
and any id recorded in the checkpoint file of already-processed (kept OR
rejected) puzzles. Re-running never redoes finished work, and the loader is
idempotent (INSERT OR IGNORE / ON CONFLICT DO NOTHING) so re-running never
duplicates. The raw ~5GB dump is discarded after filtering (delete the
downloaded .zst yourself once done; nothing here keeps it).

RUN (see FEAT-007 findings for the full command reference)
----------------------------------------------------------
  # Sample run proving the three outcomes (fast; --stats-* capture the study):
  SYZYGY_DISABLE=1 STOCKFISH_PATH=$PWD/stockfish-linux-x86-64-universal \
      python tools/filter_puzzles.py --no-download \
      --dump tools/sample_lichess_db_puzzle.csv.zst \
      --database-url sqlite:////tmp/trunc_curated.db \
      --jsonl /tmp/servable.jsonl \
      --stats-csv /tmp/trunc_stats.csv --stats-jsonl /tmp/trunc_stats.jsonl \
      --checkpoint /tmp/trunc_ckpt.txt --threads 128

  # Full production run into Postgres (creates BOTH tables itself):
  DATABASE_URL=postgresql://... SF_THREADS=128 \
      python tools/filter_puzzles.py \
      --stats-csv trunc_stats.csv --stats-jsonl trunc_stats.jsonl

The servable `puzzles` table + --jsonl get ONLY intact-solved puzzles; the
puzzle_truncation_stats table + --stats-csv/--stats-jsonl get ONLY truncation-
solved puzzles; fully-unsolvable puzzles are dropped (checkpoint only).
"""
import argparse
import csv
import io
import os
import sys
import time

# Make the sibling app modules importable whether this is run as
# `python tools/filter_puzzles.py` (cwd = chess_amateur/) or as a module.
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_HERE)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import chess  # noqa: E402  (python-chess; the app's source of truth)

# Official Lichess puzzle database dump (see https://database.lichess.org).
LICHESS_PUZZLE_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
DEFAULT_DUMP_PATH = os.environ.get(
    "LICHESS_PUZZLE_DUMP",
    os.path.join(_APP_DIR, "lichess_db_puzzle.csv.zst"))


# --------------------------------------------------------------------------
# Solve-check logic (pure; unit-tested directly in test_backend.py)
# --------------------------------------------------------------------------

def solver_indices(moves):
    """Indices in `moves` where the SOLVER is to move (1, 3, 5, ...).

    Lichess puts the opponent's setup move at index 0; the solver replies at
    every odd index thereafter."""
    return list(range(1, len(moves), 2))


def solve_check(best_move_fn, fen, moves):
    """Return True iff Chess Amateur solves the puzzle at EVERY solver move.

    `best_move_fn(fen) -> uci` is the engine's best-move function (normally
    ChessAmateurEngine(...).best_move, but any callable works, so this is
    trivially unit-testable with a stub). We replay the line move by move on a
    python-chess board: opponent moves (even indices) are simply applied; at
    each solver move (odd index) we ask the engine for its best move in the
    current position and require it to EXACTLY equal the expected UCI. The
    first mismatch rejects the puzzle. A puzzle with no solver move, or an
    illegal/malformed line, is rejected.
    """
    if not moves:
        return False
    try:
        board = chess.Board(fen)
    except Exception:
        return False
    solver_idx = set(solver_indices(moves))
    if not solver_idx:
        return False
    for i, uci in enumerate(moves):
        # Validate the move is legal in the current position (guards against a
        # malformed dump line and keeps the board in sync).
        try:
            mv = chess.Move.from_uci(uci)
        except Exception:
            return False
        if mv not in board.legal_moves:
            return False
        if i in solver_idx:
            engine_uci = best_move_fn(board.fen())
            if engine_uci != uci:
                return False
        board.push(mv)
    return True


def _front_truncate(fen, moves):
    """Remove the FIRST 2 ply from the FRONT of a Lichess-format line.

    The removed 2 ply are the solver's first move (index 1) and the opponent's
    reply to it (index 2). We advance the start position by pushing ONLY
    moves[0] (the old opponent setup move) and moves[1] (the solver's first
    move) onto a python-chess board built from `fen`; the resulting
    board.fen() is the new start FEN and new_moves is moves[2:]. In the
    SHORTENED line the OLD moves[2] becomes the new opponent setup move at
    index 0 (still to be played from the new start FEN), and the OLD solver
    move at index 3 becomes the new solver move at index 1 -- so exactly ONE
    solver move (2 ply) is removed while the Lichess convention (index 0 =
    opponent setup) is preserved. Returns (None, None) if the line is too short
    (no solver move would remain) or any of the advanced moves is
    illegal/malformed."""
    # Need moves[0], moves[1] to advance and at least moves[2] (new setup) +
    # moves[3] (new solver move) to remain, i.e. >= 4 plies.
    if len(moves) < 4:
        return None, None
    try:
        board = chess.Board(fen)
        for uci in moves[:2]:
            mv = chess.Move.from_uci(uci)
            if mv not in board.legal_moves:
                return None, None
            board.push(mv)
    except Exception:
        return None, None
    return board.fen(), list(moves[2:])


def truncate_and_solve(best_move_fn, fen, moves):
    """Progressive front-truncation solve (FEAT-002).

    Attempts solve_check on the full (fen, moves). On failure it removes the
    first 2 ply from the FRONT (the solver's first move + the opponent's reply)
    via _front_truncate and retries the UNCHANGED solved-rule, repeating while
    the CURRENT line still has at least 2 solver indices (so at least one
    solver move remains after removal). It stops as soon as a line solves, or
    when only the LAST single solver move remains -- if even that fails the
    puzzle is DROPPED.

    Returns a dict:
      outcome: 'intact' (solved, plies_removed == 0),
               'truncated' (solved after >= 1 truncation),
               'dropped' (never solved, or malformed/empty line).
      plies_removed: int (== 2 * number of truncations applied).
      solver_moves_removed: int (== plies_removed // 2).
      original_solver_move_count: int (solver indices in the FULL line).
      final_fen: the start FEN of the line that solved (or the last attempted).
      final_moves: the space-separated UCI line that solved (or last attempted).

    solve_check's semantics are reused verbatim; this never changes them.
    """
    original_solver_move_count = len(solver_indices(moves)) if moves else 0

    def _result(outcome, plies_removed, cur_fen, cur_moves):
        return {
            "outcome": outcome,
            "plies_removed": plies_removed,
            "solver_moves_removed": plies_removed // 2,
            "original_solver_move_count": original_solver_move_count,
            "final_fen": cur_fen,
            "final_moves": " ".join(cur_moves),
        }

    # Malformed / empty line: nothing to solve, nothing to truncate.
    if not moves or original_solver_move_count == 0:
        return _result("dropped", 0, fen, list(moves or []))

    cur_fen, cur_moves = fen, list(moves)
    plies_removed = 0

    # Solved intact?
    if solve_check(best_move_fn, cur_fen, cur_moves):
        return _result("intact", 0, cur_fen, cur_moves)

    # Progressive front-truncation. Only truncate while at least 2 solver
    # moves remain (so removing one still leaves a solver move to check).
    while len(solver_indices(cur_moves)) >= 2:
        new_fen, new_moves = _front_truncate(cur_fen, cur_moves)
        if new_fen is None:
            # Can't advance the board (malformed) -> drop.
            return _result("dropped", plies_removed, cur_fen, cur_moves)
        cur_fen, cur_moves = new_fen, new_moves
        plies_removed += 2
        if solve_check(best_move_fn, cur_fen, cur_moves):
            return _result("truncated", plies_removed, cur_fen, cur_moves)

    # Down to (at most) the last single solver move and still unsolved: drop.
    return _result("dropped", plies_removed, cur_fen, cur_moves)


# --------------------------------------------------------------------------
# Checkpoint (resumability)
# --------------------------------------------------------------------------

def load_checkpoint(path):
    """Return the set of lichess_ids already PROCESSED (kept or rejected)."""
    if not path or not os.path.exists(path):
        return set()
    ids = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                pid = line.strip()
                if pid:
                    ids.add(pid)
    except Exception:
        pass
    return ids


class Checkpoint:
    """Append-only record of processed lichess_ids so a restart can resume
    without redoing engine work. Flushed after every write so a crash loses at
    most the last puzzle."""

    def __init__(self, path):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def record(self, lichess_id):
        if self._fh is not None:
            self._fh.write(lichess_id + "\n")
            self._fh.flush()

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Streaming input (zstandard, never loads the whole dump into memory)
# --------------------------------------------------------------------------

def iter_dump_rows(dump_path):
    """Yield CSV rows (dicts) from a Lichess puzzle .zst dump by STREAM-
    decompressing it line by line -- the ~5GB file is never loaded into
    memory. Handles both a header-carrying dump and (as a fallback) a
    header-less one by using the known column order."""
    import zstandard

    fieldnames = ["PuzzleId", "FEN", "Moves", "Rating", "RatingDeviation",
                  "Popularity", "NbPlays", "Themes", "GameUrl", "OpeningTags"]
    dctx = zstandard.ZstdDecompressor()
    with open(dump_path, "rb") as raw:
        with dctx.stream_reader(raw) as reader:
            text = io.TextIOWrapper(reader, encoding="utf-8", newline="")
            # Peek at the first line to decide whether a header is present.
            first = text.readline()
            if not first:
                return
            has_header = first.startswith("PuzzleId")
            if has_header:
                dict_reader = csv.DictReader(text, fieldnames=fieldnames)
            else:
                # No header: re-stitch the first line back in front of the
                # stream via itertools.chain so no row is lost.
                import itertools
                dict_reader = csv.DictReader(
                    itertools.chain([first], text), fieldnames=fieldnames)
            for row in dict_reader:
                if row.get("PuzzleId") == "PuzzleId":
                    continue  # a stray header row
                yield row


def download_dump(dump_path, url=LICHESS_PUZZLE_URL):
    """Download the full Lichess puzzle dump to `dump_path` if not present.

    Streams to disk (never into memory). Skips the download when the file
    already exists (resumability of the download itself is out of scope; delete
    a partial file to re-fetch)."""
    if os.path.exists(dump_path) and os.path.getsize(dump_path) > 0:
        print("[filter_puzzles] dump already present: %s (%d bytes)"
              % (dump_path, os.path.getsize(dump_path)))
        return dump_path
    import urllib.request
    print("[filter_puzzles] downloading %s -> %s (this is ~5GB compressed)"
          % (url, dump_path))
    tmp = dump_path + ".part"
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dump_path)
    print("[filter_puzzles] download complete: %d bytes"
          % os.path.getsize(dump_path))
    return dump_path


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

# CSV header for the truncation-study analysis dataset (--stats-csv).
_STATS_CSV_HEADER = ["original_solver_move_count", "solver_moves_removed",
                     "plies_removed", "lichess_rating", "lichess_id",
                     "final_fen", "final_moves", "themes"]


def run(dump_path, store=None, jsonl_path=None, checkpoint_path=None,
        limit=None, threads=None, best_move_fn=None, progress_every=100,
        stats_csv_path=None, stats_jsonl_path=None):
    """Stream the dump, run progressive front-truncation per puzzle, and route
    the three outcomes (FEAT-002).

    Returns a stats dict:
      {processed, intact_kept, truncated_kept, dropped, skipped}.

    * `store`      : a storage.Store (Postgres or SQLite). INTACT puzzles are
                     inserted into `puzzles`; TRUNCATED puzzles into
                     `puzzle_truncation_stats`; both idempotently. May be None
                     (file-only output).
    * `jsonl_path` : optional; INTACT (servable) puzzles are appended here.
    * `stats_csv_path`  : optional; TRUNCATED puzzles appended as CSV rows.
    * `stats_jsonl_path`: optional; TRUNCATED puzzles appended as JSON objects.
    * `checkpoint_path` : optional; EVERY processed id is recorded (intact,
                     truncated, or dropped) for resumability.
    * `limit`      : cap the number of puzzles PROCESSED this run (sample runs).
    * `best_move_fn`: override the engine (tests pass a stub). When None a real
                     ChessAmateurEngine (depth 1, SF_THREADS/128) is used.
    """
    import json

    # Resume set: ids in EITHER output table + ids in the checkpoint file, so
    # neither a servable nor a truncated puzzle is re-processed.
    already = set()
    if store is not None and getattr(store, "enabled", False):
        try:
            already |= store.puzzle_lichess_ids()
        except Exception:
            pass
        try:
            already |= store.truncation_stat_lichess_ids()
        except Exception:
            pass
    already |= load_checkpoint(checkpoint_path)

    checkpoint = Checkpoint(checkpoint_path) if checkpoint_path else None
    jsonl_fh = open(jsonl_path, "a", encoding="utf-8") if jsonl_path else None
    stats_jsonl_fh = (open(stats_jsonl_path, "a", encoding="utf-8")
                      if stats_jsonl_path else None)
    stats_csv_fh = None
    stats_csv_writer = None
    if stats_csv_path:
        write_header = (not os.path.exists(stats_csv_path)
                        or os.path.getsize(stats_csv_path) == 0)
        stats_csv_fh = open(stats_csv_path, "a", encoding="utf-8", newline="")
        stats_csv_writer = csv.writer(stats_csv_fh)
        if write_header:
            stats_csv_writer.writerow(_STATS_CSV_HEADER)
            stats_csv_fh.flush()

    # Engine setup (real engine unless a stub was injected).
    engine = None
    if best_move_fn is None:
        from engine import ChessAmateurEngine
        sf_threads = threads
        if sf_threads is None:
            sf_threads = int(os.environ.get("SF_THREADS", "128"))
        engine = ChessAmateurEngine(threads=sf_threads)
        best_move_fn = engine.best_move

    stats = {"processed": 0, "intact_kept": 0, "truncated_kept": 0,
             "dropped": 0, "skipped": 0}
    start = time.time()
    try:
        for row in iter_dump_rows(dump_path):
            if limit is not None and stats["processed"] >= limit:
                break
            pid = (row.get("PuzzleId") or "").strip()
            fen = (row.get("FEN") or "").strip()
            moves_str = (row.get("Moves") or "").strip()
            if not pid or not fen or not moves_str:
                continue
            if pid in already:
                stats["skipped"] += 1
                continue
            moves = moves_str.split()
            try:
                rating = int(float(row.get("Rating") or 0))
            except Exception:
                rating = 0
            themes = (row.get("Themes") or "").strip() or None

            stats["processed"] += 1
            res = truncate_and_solve(best_move_fn, fen, moves)
            outcome = res["outcome"]
            if outcome == "intact":
                # SERVABLE: the full line solved. Store the ORIGINAL puzzle.
                stats["intact_kept"] += 1
                if store is not None and getattr(store, "enabled", False):
                    store.insert_puzzle(pid, fen, moves_str, rating, themes)
                if jsonl_fh is not None:
                    jsonl_fh.write(json.dumps({
                        "lichess_id": pid, "fen": fen, "moves": moves_str,
                        "lichess_rating": rating, "themes": themes}) + "\n")
                    jsonl_fh.flush()
            elif outcome == "truncated":
                # NOT servable: correlation-study dataset only.
                stats["truncated_kept"] += 1
                if store is not None and getattr(store, "enabled", False):
                    store.insert_truncation_stat(
                        pid, res["original_solver_move_count"],
                        res["solver_moves_removed"], res["plies_removed"],
                        rating, res["final_fen"], res["final_moves"], themes)
                if stats_csv_writer is not None:
                    stats_csv_writer.writerow([
                        res["original_solver_move_count"],
                        res["solver_moves_removed"], res["plies_removed"],
                        rating, pid, res["final_fen"], res["final_moves"],
                        themes or ""])
                    stats_csv_fh.flush()
                if stats_jsonl_fh is not None:
                    stats_jsonl_fh.write(json.dumps({
                        "original_solver_move_count":
                            res["original_solver_move_count"],
                        "solver_moves_removed": res["solver_moves_removed"],
                        "plies_removed": res["plies_removed"],
                        "lichess_rating": rating, "lichess_id": pid,
                        "final_fen": res["final_fen"],
                        "final_moves": res["final_moves"],
                        "themes": themes}) + "\n")
                    stats_jsonl_fh.flush()
            else:
                # DROPPED: recorded nowhere but the checkpoint.
                stats["dropped"] += 1
            already.add(pid)
            if checkpoint is not None:
                checkpoint.record(pid)
            if stats["processed"] % progress_every == 0:
                elapsed = time.time() - start
                print("[filter_puzzles] processed=%d intact=%d truncated=%d "
                      "dropped=%d skipped=%d (%.1fs, %.1f/s)"
                      % (stats["processed"], stats["intact_kept"],
                         stats["truncated_kept"], stats["dropped"],
                         stats["skipped"], elapsed,
                         stats["processed"] / elapsed if elapsed else 0))
    finally:
        if engine is not None:
            engine.close()
        if checkpoint is not None:
            checkpoint.close()
        if jsonl_fh is not None:
            jsonl_fh.close()
        if stats_jsonl_fh is not None:
            stats_jsonl_fh.close()
        if stats_csv_fh is not None:
            stats_csv_fh.close()

    elapsed = time.time() - start
    print("[filter_puzzles] DONE processed=%d intact=%d truncated=%d "
          "dropped=%d skipped=%d in %.1fs"
          % (stats["processed"], stats["intact_kept"],
             stats["truncated_kept"], stats["dropped"], stats["skipped"],
             elapsed))
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="OFFLINE Lichess puzzle-filtering job for Chess Amateur.")
    parser.add_argument(
        "--dump", default=DEFAULT_DUMP_PATH,
        help="Path to the Lichess puzzle .zst dump (downloaded if missing).")
    parser.add_argument(
        "--no-download", action="store_true",
        help="Do NOT download the dump; require it to already exist.")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N puzzles this run (for sample/verification "
             "runs). Omit for the full dump.")
    parser.add_argument(
        "--database-url", default=os.environ.get("DATABASE_URL"),
        help="Store kept puzzles here (Postgres in prod). Defaults to "
             "$DATABASE_URL. If unset and --jsonl is given, writes JSONL only.")
    parser.add_argument(
        "--jsonl", default=None,
        help="Also append INTACT (servable) puzzles to this JSONL file.")
    parser.add_argument(
        "--stats-csv", default=None,
        help="Append TRUNCATION-STUDY rows (truncation-solved puzzles) to this "
             "CSV file (header: original_solver_move_count,solver_moves_removed,"
             "plies_removed,lichess_rating,lichess_id,final_fen,final_moves,"
             "themes). NOT servable; correlation-study dataset only.")
    parser.add_argument(
        "--stats-jsonl", default=None,
        help="Append the SAME truncation-study fields as JSON objects to this "
             "JSONL file.")
    parser.add_argument(
        "--checkpoint", default=None,
        help="Resumability checkpoint file of processed puzzle ids.")
    parser.add_argument(
        "--threads", type=int, default=None,
        help="Stockfish Threads (defaults to $SF_THREADS or 128).")
    args = parser.parse_args(argv)

    # Ensure the dump exists (download unless suppressed).
    if not (os.path.exists(args.dump) and os.path.getsize(args.dump) > 0):
        if args.no_download:
            print("[filter_puzzles] ERROR: dump missing and --no-download set: "
                  "%s" % args.dump, file=sys.stderr)
            return 2
        download_dump(args.dump)

    store = None
    if args.database_url:
        from storage import Store
        store = Store(args.database_url)
        if not store.enabled:
            print("[filter_puzzles] WARNING: store not enabled for %s"
                  % args.database_url, file=sys.stderr)
    elif not args.jsonl:
        # No DB and no JSONL: default to a local SQLite sink so a bare sample
        # run still produces visible output rather than discarding everything.
        from storage import Store
        default_db = os.path.join(_APP_DIR, "curated_puzzles.sqlite.db")
        args.database_url = "sqlite:///" + default_db
        store = Store(args.database_url)
        print("[filter_puzzles] no --database-url/--jsonl given; writing to "
              "local SQLite: %s" % default_db)

    stats = run(
        dump_path=args.dump, store=store, jsonl_path=args.jsonl,
        checkpoint_path=args.checkpoint, limit=args.limit,
        threads=args.threads, stats_csv_path=args.stats_csv,
        stats_jsonl_path=args.stats_jsonl)
    if store is not None and getattr(store, "enabled", False):
        print("[filter_puzzles] servable puzzles now in DB: %d; "
              "truncation-study rows: %d"
              % (store.count_puzzles(), store.count_truncation_stats()))
    return 0 if stats["processed"] >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
