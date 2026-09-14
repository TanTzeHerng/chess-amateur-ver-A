# Chess Amateur - play chess against Stockfish (depth 1) in your browser.
#
# Stockfish is downloaded during the build (official release, linux x86-64),
# so this repository does NOT need to contain the ~103 MB engine binary. The
# server itself is pure Python stdlib plus python-chess.
FROM --platform=linux/amd64 python:3.11-slim

# --- Download the Stockfish engine at build time -------------------------
# Fetch an official Stockfish release and place the binary at a stable path.
#
# We deliberately use the GENERIC x86-64 build (`stockfish-ubuntu-x86-64.tar`),
# NOT the AVX2/BMI2 variant. The optimized builds contain CPU instructions
# (AVX2, etc.) that not every cloud host supports; on such a host the binary
# dies instantly with SIGILL on launch, the engine never starts, and a move
# request hangs -> the platform proxy returns a 502 with nothing in the logs
# (exactly the failure seen on Render). The generic build runs on ANY 64-bit
# x86 CPU. Because Chess Amateur searches at depth 1, the CPU-optimization
# level is irrelevant to move quality/speed, so the generic build costs us
# nothing while eliminating SIGILL-on-launch.
ARG STOCKFISH_URL=https://github.com/official-stockfish/Stockfish/releases/download/sf_17/stockfish-ubuntu-x86-64.tar
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fL "$STOCKFISH_URL" -o /tmp/stockfish.tar; \
    mkdir -p /tmp/sf; \
    tar -xf /tmp/stockfish.tar -C /tmp/sf; \
    bin="$(find /tmp/sf -type f -name 'stockfish*' -perm -u+x | head -n 1)"; \
    if [ -z "$bin" ]; then bin="$(find /tmp/sf -type f -name 'stockfish*' | head -n 1)"; fi; \
    cp "$bin" /usr/local/bin/stockfish; \
    chmod +x /usr/local/bin/stockfish; \
    rm -rf /tmp/stockfish.tar /tmp/sf; \
    # STRICT smoke test: actually EXECUTE the binary and require a real UCI
    # handshake. If the binary cannot run on this platform (SIGILL, missing
    # instructions, corrupt download), 'uciok' will be absent and the build
    # FAILS here instead of shipping a broken engine that only fails at
    # runtime with an unexplained 502.
    printf 'uci\nquit\n' | /usr/local/bin/stockfish | grep -q '^uciok$'; \
    echo "Stockfish smoke test passed: uciok received from generic x86-64 build"

# --- Application ---------------------------------------------------------
WORKDIR /app

# python-chess (the only Python dependency).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code. The Flask app (app.py) is the entry point; it reuses the
# engine (engine.py) and the stateless chess core (chess_core.py), and adds
# accounts/history (auth.py, storage.py). Templates + static assets included.
COPY engine.py chess_core.py auth.py storage.py ratings.py timecontrol.py clockclient.py eco.py fide.py book.py syzygy.py puzzles.py app.py ./
COPY eco.json ./
# Polyglot opening book (used ONLY in FIDE-rated mode; probed in the app layer
# because Stockfish has no built-in Polyglot support). MUST be shipped.
COPY pc2500.bin ./
COPY static/ ./static/
COPY templates/ ./templates/

# Point the app at the downloaded engine and default to 128 threads.
# Override with SF_THREADS on small hosts if memory is tight.
# PYTHONUNBUFFERED=1 forces Python's stdout/stderr to be unbuffered so the
# app's startup + per-request logs (incl. operator UCI telemetry) actually
# reach the platform log collector (block-buffering otherwise hides them).
#   DATABASE_URL : Postgres connection string. If UNSET, the app runs in
#                  GUEST-ONLY mode (play works; accounts/history disabled).
#   SECRET_KEY   : Flask session signing key (set this in production so logins
#                  persist across restarts).
#   SUPABASE_URL         : Supabase project URL. When set together with
#                          SUPABASE_ANON_KEY, identity + login/signup +
#                          password-reset emails are handled by Supabase Auth.
#                          If UNSET, the app falls back to the built-in
#                          bcrypt-local auth (and guest play) exactly as before.
#   SUPABASE_ANON_KEY    : Supabase anon/public API key (client-side auth calls:
#                          sign_up / sign_in / password-reset email).
#   SUPABASE_SERVICE_KEY : Supabase service-role key (server-side admin calls;
#                          required only for deleting the Supabase auth user on
#                          account deletion). Optional; account deletion still
#                          removes the local row without it.
#   -- Tablebases (Syzygy, 5-men WDL) ------------------------------------------
#   SYZYGY_DIR    : Directory the 5-men WDL (.rtbw) tablebase files are
#                   downloaded into AT CONTAINER STARTUP and that Stockfish's
#                   SyzygyPath UCI option points at. Default /tmp/syzygy (an
#                   EPHEMERAL path -- NOT baked into the image, NOT a paid
#                   persistent disk). Set to "" to DISABLE tablebases.
#   SYZYGY_URL    : Base URL to download the individual .rtbw files from.
#                   Default https://tablebase.lichess.ovh/tables/standard/3-4-5/
#                   (the public Lichess Syzygy mirror). WDL-only (~386 MB): at
#                   depth 1 the WDL tables are what change the root move; the
#                   DTZ tables are not needed.
#   SYZYGY_DISABLE: "1" to skip the startup download AND leave SyzygyPath unset
#                   (used for local dev / the offline test suite so no ~386 MB
#                   download is attempted). The download is guarded by a marker
#                   file (.syzygy_complete) so a restart reuses an existing
#                   download and the single gunicorn worker never re-downloads.
#   -- Opening book ------------------------------------------------------------
#   POLYGLOT_BOOK : Path to the Polyglot book (default: pc2500.bin next to the
#                   app). Consulted ONLY in FIDE-rated mode.
ENV STOCKFISH_PATH=/usr/local/bin/stockfish \
    SYZYGY_DIR=/tmp/syzygy \
    PORT=8000 \
    SF_THREADS=128 \
    PYTHONUNBUFFERED=1

# Hosting platforms typically inject their own PORT; honored below. Bind 0.0.0.0.
EXPOSE 8000

# Run Flask via gunicorn. ONE worker preserves the single-Stockfish-process
# memory model (the whole app shares one engine instance); --threads lets that
# one worker handle concurrent HTTP requests. The engine's own lock serializes
# actual searches. `sh -c` so ${PORT} is expanded at runtime (Render injects it).
#
# BEFORE gunicorn boots we run `python -m syzygy`, which downloads the 5-men WDL
# Syzygy set into $SYZYGY_DIR (ephemeral, marker-guarded so a restart reuses it;
# a no-op when SYZYGY_DISABLE=1 or SYZYGY_DIR is empty). The download runs to
# completion first so /healthz is only served once the engine can probe the
# tablebases; it is NOT baked into the image and uses no persistent disk.
CMD ["sh", "-c", "python -m syzygy; gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT} app:app"]
