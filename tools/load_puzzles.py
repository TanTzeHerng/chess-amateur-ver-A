#!/usr/bin/env python3
"""OFFLINE bulk loader for curated Chess Amateur puzzles (FEAT-007).

Loads a JSONL file of curated puzzles (as produced by filter_puzzles.py with
--jsonl) into the puzzles table, IDEMPOTENTLY. Each line is a JSON object:
  {"lichess_id": "...", "fen": "...", "moves": "e2e4 ...",
   "lichess_rating": 1500, "themes": "mateIn2 ..."}

Idempotency: insert_puzzle uses ON CONFLICT (lichess_id) DO NOTHING on Postgres
and INSERT OR IGNORE on SQLite, so re-running this loader on the same file
never duplicates rows. This lives under tools/ and is NEVER run by the web app.

RUN:
  DATABASE_URL=postgresql://... python tools/load_puzzles.py curated.jsonl
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_HERE)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)


def load_jsonl(store, jsonl_path):
    """Bulk-load a JSONL of curated puzzles into `store`, idempotently.

    Returns (inserted, seen) counts."""
    inserted = 0
    seen = 0
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            seen += 1
            rec = json.loads(line)
            ok = store.insert_puzzle(
                rec["lichess_id"], rec["fen"], rec["moves"],
                rec.get("lichess_rating") or 0, rec.get("themes"))
            if ok:
                inserted += 1
    return inserted, seen


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bulk-load curated puzzles JSONL into the puzzles table.")
    parser.add_argument("jsonl", help="Path to the curated puzzles JSONL.")
    parser.add_argument(
        "--database-url", default=os.environ.get("DATABASE_URL"),
        help="Target DB (defaults to $DATABASE_URL).")
    args = parser.parse_args(argv)

    if not args.database_url:
        print("[load_puzzles] ERROR: no --database-url / $DATABASE_URL",
              file=sys.stderr)
        return 2

    from storage import Store
    store = Store(args.database_url)
    if not store.enabled:
        print("[load_puzzles] ERROR: store not enabled for %s"
              % args.database_url, file=sys.stderr)
        return 2

    inserted, seen = load_jsonl(store, args.jsonl)
    print("[load_puzzles] loaded %d new / %d seen; total puzzles now %d"
          % (inserted, seen, store.count_puzzles()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
