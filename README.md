# Chess Amateur

A small, self-contained website that lets you play chess against **Chess Amateur**,
a bot powered by [Stockfish](https://stockfishchess.org/) running at **search depth 1**
with **128 threads**. Because the search is only one ply deep, Chess Amateur plays
at a beatable, amateur strength while still making legal, sensible-looking moves.

The whole app runs on the Python standard library (`http.server`) plus
[`python-chess`](https://python-chess.readthedocs.io/). There is no web framework,
no bundler, and no npm step. The frontend is plain HTML/CSS/JavaScript served
directly by the backend.

## What it does

- Play a full game of chess in the browser against Chess Amateur.
- You can play as White or Black; if you choose Black, Chess Amateur moves first.
- Every move is validated **server-side** by python-chess, which is the single
  source of truth for board state, move legality, SAN notation, FEN, and
  game-over/result detection. Illegal moves are rejected and never change the board.
- Checkmate, stalemate, and draw conditions (insufficient material, fifty-move
  rule, repetition) are detected and reported.

## Prerequisites

- **Python 3** (tested on 3.9).
- **python-chess** (`pip install chess`). Already installed in this environment
  (version 1.11.2).
- A **Stockfish** binary. By default the app looks for it at:

  ```
  /projects/sandbox/stockfish/stockfish-linux-x86-64-universal
  ```

  You can point it at a different binary by setting the `STOCKFISH_PATH`
  environment variable:

  ```
  export STOCKFISH_PATH=/path/to/your/stockfish
  ```

## Running the app

From inside the `chess_amateur` directory:

```
python3 server.py
```

or from the repository root:

```
python3 chess_amateur/server.py
```

On startup the server prints the URL it is listening on, for example:

```
Chess Amateur server running at http://localhost:8000
```

Then open **http://localhost:8000** in your browser and start playing.

### Environment overrides

| Variable         | Default                                                            | Purpose                                                   |
| ---------------- | ------------------------------------------------------------------ | --------------------------------------------------------- |
| `PORT`           | `8000`                                                             | Port the HTTP server listens on (binds `0.0.0.0`).        |
| `STOCKFISH_PATH` | `/projects/sandbox/stockfish/stockfish-linux-x86-64-universal`     | Path to the Stockfish binary.                             |
| `SF_THREADS`     | `128`                                                              | Server-wide default Stockfish thread count (1&ndash;128). |

Example, running on a different port:

```
PORT=9000 python3 chess_amateur/server.py
# then open http://localhost:9000
```

### Choosing the engine thread count

Chess Amateur always searches only **one ply deep** (depth 1), which is what
keeps it beatable. The **thread count does not change that** — it only affects
how much CPU Stockfish uses while picking its depth-1 move.

There are three ways to set it, in priority order:

1. **Per game (UI / request):** the "Engine threads" control on the page sends a
   `threads` value (1&ndash;128) with `POST /api/new`. This wins for that game.
2. **Server default:** the `SF_THREADS` environment variable (default `128`).
3. **Hard-coded fallback:** `128` when nothing else is set.

Missing, out-of-range, or invalid values fall back to the default rather than
erroring. The active thread count for a game is returned in the API state (the
`threads` field) and shown next to the opponent label in the UI.

On a small host (a tiny VPS, a free tier, a phone-tethered box) set a low count
like `SF_THREADS=2` — spawning 128 threads there is wasteful and slow to start.

> **Note:** The very first engine call can take a little while because Stockfish
> spawns 128 threads on startup. This is expected.

## Stateless design (works across restarts / multiple workers)

The game history is **held by the client and sent with every move**, and the
server rebuilds the position from it. The server does **not** rely on its own
memory to serve a move.

This matters on cloud hosts. On a platform like Render (free tier), the process
that handled `POST /api/new` is **not** guaranteed to be the one that later
handles `POST /api/move`: the platform may run multiple workers and freely
restart, recycle, or cold-start the instance between requests. If the game were
kept only in an in-memory dict keyed by `game_id`, the later request would hit a
process whose memory is empty and get an HTTP `404 "unknown game_id"` — which is
exactly the "Server error (404)." a player saw right after their first move.

The fix makes moves **stateless**: `POST /api/move` carries the authoritative
list of UCI moves played so far (`moves`), plus `human_color` and `threads`. The
server replays that list onto a fresh `chess.Board` (python-chess remains the
single source of truth for legality, SAN, FEN, and game-over detection),
validates the new move, applies it, gets Chess Amateur's reply, and returns the
full updated state — including the new authoritative `moves` list that the client
then echoes back on the next move. A `game_id` is still issued for display, and
an in-memory `GAMES` dict is kept as a **best-effort, non-authoritative cache**,
but correctness never depends on it. So the game survives restarts and works no
matter which worker serves each request.

## HTTP API

The frontend talks to a tiny JSON API (also usable directly with `curl`):

- `POST /api/new` — body `{"human_color": "white" | "black", "threads": 1..128?}`.
  Starts a new game and returns the game state, including a `game_id`, the active
  `threads` count, and `moves` (the starting UCI history: `[]`, or one engine
  move if you chose Black). `threads` is optional; omit it to use the server
  default (`SF_THREADS`, else 128).
- `POST /api/move` — body
  `{"moves": ["e2e4", ...], "move": "e7e5", "human_color": "white" | "black", "threads": 1..128?, "game_id": "..."?}`.
  `moves` is the authoritative history so far (what the previous response
  returned); `move` is the new human move in UCI notation (promotions look like
  `e7e8q`). The server rebuilds the position from `moves`, applies your move,
  then returns Chess Amateur's reply and the updated state (new `fen`,
  `san_history`, `legal_moves`, `last_bot_move`, `moves`, `game_over` / `result`
  / `result_reason`, etc.). `game_id` is optional and display-only.
  - Illegal moves return HTTP 400 with `{"error": "illegal move"}` and leave the
    carried state unchanged (the client re-sends the same history for its next
    attempt — no desync).
  - A move history that cannot be legally replayed returns HTTP 400 with
    `{"error": "invalid move history"}`.
- `GET /api/state?game_id=...` — best-effort snapshot from the in-memory cache
  (may be absent after a restart; not a correctness dependency).
- `GET /` — serves the frontend (`static/index.html`).

## Running the tests

Self-contained backend smoke tests (no external test framework required):

```
python3 chess_amateur/test_backend.py
```

The tests exercise real code paths:

- Chess Amateur (`ChessAmateurEngine.best_move`) returns a legal UCI move.
- Starting a new game as White and as Black.
- A legal move followed by a real engine reply, with a consistent SAN history.
- An illegal move is rejected (HTTP 400) and the board state is unchanged.
- `GET /api/state` returns the current state.
- **Game-over detection**: a Fool's-mate checkmate position is reported as
  `game_over` with result `0-1` and `result_reason` `"checkmate"`.
- `GET /` serves the frontend.

They spin up the real server on a throwaway port within the test process, so no
separate server needs to be running.

## Deploying with Docker

The repo ships a multi-stage `Dockerfile` that produces **two** Stockfish
binaries at build time and places them inside the image, so the container runs
anywhere without a separate engine install. It uses a slim Python base and
installs only `python-chess`.

- **`/usr/local/bin/stockfish`** &mdash; the engine the app actually uses
  (`STOCKFISH_PATH` points here). It is **built from source** at tag `sf_17`
  with a minimal Chess Amateur probe-instrumentation patch
  (`stockfish-ca-probe.patch`, see below) applied to `src/search.cpp`.
- **`/usr/local/bin/stockfish-prebuilt`** &mdash; a **pristine, unmodified**
  official release binary, kept as a fallback.

Both binaries are the **generic** `x86-64` build (compiled with `ARCH=x86-64`
from source; the official `stockfish-ubuntu-x86-64` release for the prebuilt),
never a CPU-optimized variant (AVX2/BMI2). The optimized builds use instructions
that some cloud CPUs lack; on such a host the binary dies instantly with
`SIGILL` on launch, the engine never starts, and a move request hangs until the
platform proxy returns a `502` with nothing in the logs. The generic build runs
on any 64-bit x86 CPU. Because Chess Amateur searches at depth 1, the
CPU-optimization level does not affect move quality, so the generic build costs
nothing.

#### The probe-instrumentation patch

The patched engine emits a distinctive UCI line
`info string CA_TB_ABOUT_TO_PROBE` **only** when it is about to probe the Syzygy
tablebases for a position it is *searching* but the tablebase is not loaded or
insufficient (i.e. `Tablebases::probe_wdl` returns `TB::ProbeState::FAIL` inside
the "Step 5. Tablebases probe" block). The app reads the raw UCI stream and uses
this marker to defer a bot move while the ~386 MB tablebase download is still in
progress, then run a fresh search once the tablebases are ready. The patch is
pure instrumentation: it fires only on `FAIL`, so it never spams when the
tablebase is present, and it leaves the search-result path untouched, so play is
identical to stock `sf_17`. The `.patch` file lives in the build context and is
applied in the builder stage **only**; it is never copied into the runtime
image (which contains just the compiled binaries, no source and no `.patch`).

#### Build-time self-tests and the pristine fallback

The builder stage runs strict self-tests on the freshly compiled patched binary:
a real `uciok` UCI handshake, a normal depth-1 `bestmove`, confirmation that the
`CA_TB_ABOUT_TO_PROBE` marker fires for a searched position whose tablebase is
absent/insufficient, and confirmation that it does **not** fire for a full-board
search or when the probe succeeds. If **all** checks pass, the patched binary
becomes `/usr/local/bin/stockfish`. If any check fails, the build falls back to
the pristine prebuilt release binary as `/usr/local/bin/stockfish` (just without
the probe marker) so the image always ships a working engine. A final runtime
smoke test re-runs the `uciok` handshake and fails the build if the shipped
binary cannot execute, so a broken engine is never shipped.

Build and run locally:

```
docker build -t chess-amateur .
docker run -p 8000:8000 -e SF_THREADS=2 chess-amateur
# then open http://localhost:8000
```

- The container honors `PORT` (default `8000`) and binds `0.0.0.0`, which is
  what hosting platforms expect. Most platforms inject their own `PORT`; the
  server picks it up automatically.
- `SF_THREADS` sets the default engine thread count in the container. On small
  hosts, keep it low (`1`&ndash;`2`). Regardless of thread count, the engine
  stays at depth 1, so Chess Amateur remains beatable.
- `STOCKFISH_PATH` is preset to the bundled **patched** binary
  (`/usr/local/bin/stockfish`) inside the image; you do not need to set it. The
  pristine unmodified fallback stays at `/usr/local/bin/stockfish-prebuilt`.

### Tablebases (Syzygy 5-men WDL)

At boot the app kicks off the ~386 MB 5-men WDL Syzygy download in a
**background daemon thread** started at app import (via
`syzygy.start_background_download()`), so gunicorn **binds the port
immediately**. The container never blocks on the download before serving, which
is why Render (and similar platforms) no longer report *"No open ports
detected"* on startup. The old blocking `python -m syzygy` prelude has been
removed from the Docker `CMD`.

The files are written to `SYZYGY_DIR` (default `/tmp/syzygy`, an **ephemeral**
path, not a paid persistent disk) and are guarded by a `.syzygy_complete` marker
so a restart reuses an existing download. `/healthz` stays instant and **never**
waits on the download. Set `SYZYGY_DISABLE=1` (or `SYZYGY_DIR=""`) to skip the
download entirely.

The frontend polls a server-wide progress endpoint (single worker, no auth):

```
GET /api/tablebase-status
-> {"ready": bool, "downloaded": int, "total": int, "percent": int}
```

- `ready` is `true` once the download has finished (or immediately when
  tablebases are disabled or a complete download already exists on disk).
- `percent` is `100` exactly when `ready` is `true`; otherwise it is
  `int(100 * downloaded / total)` clamped to `< 100` so it never claims to be
  done early.
- `total` reflects the number of files actually **attempted**; benign
  name-order `404`s (e.g. `KNvKB` whose canonical twin is `KBvKN`) are skipped
  but still counted, so the denominator stays honest.

### Picking a host

You just need a host that can run a Docker container and give you a public URL,
which you can then open on your phone. Any of the common container hosts work
(for example a small VPS running `docker run`, or a platform-as-a-service that
builds from a `Dockerfile`). Point it at this directory, let it build the image,
set `SF_THREADS` low if the host is small, and open the URL it gives you.

> **Note:** the image contains a Stockfish binary (~50&nbsp;MB), downloaded
> during the build rather than committed to the repo, so the build context
> stays small. `.dockerignore` trims caches and tests from the context.

## Notes

- All move legality is enforced **server-side** via python-chess; the browser is
  never trusted to decide what is legal. It supplies only the raw move history,
  which the server validates by replaying it move by move.
- Game state is **client-carried and stateless on the server** (see the
  "Stateless design" section), so the app works correctly on hosts that run
  multiple workers or restart the process between requests.
- A single Stockfish process is reused across moves and is shut down cleanly when
  the server stops.
