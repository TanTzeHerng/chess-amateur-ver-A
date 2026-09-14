# Chess Amateur - play chess against Stockfish (depth 1) in your browser.
#
# The container ships TWO Stockfish binaries, both GENERIC/portable x86-64
# builds (NEVER AVX2/BMI2 -- see the arch note below):
#
#   /usr/local/bin/stockfish            <- the PATCHED engine the app uses
#                                          (STOCKFISH_PATH points here). Built
#                                          FROM SOURCE at tag sf_17 with the
#                                          Chess Amateur probe-instrumentation
#                                          patch (stockfish-ca-probe.patch)
#                                          applied to src/search.cpp.
#   /usr/local/bin/stockfish-prebuilt   <- a PRISTINE, unmodified official
#                                          release binary kept as a fallback.
#
# WHY A PATCHED BUILD: the patch makes the engine emit a distinctive UCI line
# `info string CA_TB_ABOUT_TO_PROBE` exactly when it is about to probe the
# Syzygy tablebases for a position it is SEARCHING but the tablebase is not
# loaded/insufficient. The app reads the raw UCI stream and uses this marker to
# defer a move while the ~386 MB tablebase download is still in progress. The
# patch touches ONLY instrumentation (it prints on TB::ProbeState::FAIL); it
# does NOT alter the search result path, so play is identical to stock sf_17.
#
# WHY GENERIC x86-64 (ARCH=x86-64), NOT AVX2/BMI2: the optimized builds contain
# CPU instructions (AVX2, BMI2, ...) that not every cloud host supports; on such
# a host the binary dies instantly with SIGILL on launch, the engine never
# starts, and a move request hangs -> the platform proxy returns a 502 with
# nothing in the logs (exactly the failure seen on Render). The generic build
# runs on ANY 64-bit x86 CPU. Because Chess Amateur searches at depth 1, the
# CPU-optimization level is irrelevant to move quality/speed, so the generic
# build costs us nothing while eliminating SIGILL-on-launch.
#
# FALLBACK: the builder stage runs STRICT self-tests on the freshly compiled
# patched binary (a real `uciok` UCI handshake, a normal depth-1 `bestmove`,
# and confirmation the CA_TB_ABOUT_TO_PROBE marker fires for a searched <=5-man
# position with tablebases absent but NOT for a full-board depth-1 search). If
# ALL of those pass, the patched binary becomes /usr/local/bin/stockfish. If any
# check fails, the builder falls back to the PRISTINE prebuilt release binary as
# /usr/local/bin/stockfish so the image still ships a working engine (just
# without the probe marker). The pristine binary is ALSO always shipped
# separately at /usr/local/bin/stockfish-prebuilt.

# =========================================================================
# Stage 1: builder -- download prebuilt release + compile patched from source
# =========================================================================
FROM --platform=linux/amd64 python:3.11-slim AS builder

# Toolchain for cloning + compiling Stockfish from source, plus curl/tar to
# fetch the pristine prebuilt release used as the fallback engine.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        git build-essential g++ make curl ca-certificates xz-utils; \
    rm -rf /var/lib/apt/lists/*

# --- Pristine prebuilt release (fallback engine) -------------------------
# Fetch an official GENERIC x86-64 Stockfish release. This binary is NEVER
# patched; it is the pristine fallback shipped at /usr/local/bin/stockfish-prebuilt
# and used as /usr/local/bin/stockfish only if the from-source patched build
# fails its self-tests below.
ARG STOCKFISH_URL=https://github.com/official-stockfish/Stockfish/releases/download/sf_17/stockfish-ubuntu-x86-64.tar
RUN set -eux; \
    curl -fL "$STOCKFISH_URL" -o /tmp/stockfish.tar; \
    mkdir -p /tmp/sf; \
    tar -xf /tmp/stockfish.tar -C /tmp/sf; \
    bin="$(find /tmp/sf -type f -name 'stockfish*' -perm -u+x | head -n 1)"; \
    if [ -z "$bin" ]; then bin="$(find /tmp/sf -type f -name 'stockfish*' | head -n 1)"; fi; \
    cp "$bin" /out-stockfish-prebuilt; \
    chmod +x /out-stockfish-prebuilt; \
    rm -rf /tmp/stockfish.tar /tmp/sf; \
    # STRICT smoke test on the pristine prebuilt binary (this is the safety net,
    # it MUST run on this platform).
    printf 'uci\nquit\n' | /out-stockfish-prebuilt | grep -q '^uciok$'; \
    echo "Prebuilt (pristine) Stockfish smoke test passed: uciok received from generic x86-64 build"

# --- From-source patched build -------------------------------------------
# Clone sf_17, apply the Chess Amateur probe-instrumentation patch, and compile
# a GENERIC/portable build (ARCH=x86-64 base -- NOT x86-64-avx2/bmi2).
#
# The patch is part of the build context (chess_amateur/stockfish-ca-probe.patch)
# and is used ONLY here in the builder; it is NEVER copied into the runtime image.
COPY stockfish-ca-probe.patch /tmp/stockfish-ca-probe.patch
RUN set -eux; \
    git clone --branch sf_17 --depth 1 https://github.com/official-stockfish/Stockfish /tmp/Stockfish; \
    cd /tmp/Stockfish; \
    # Apply the minimal search.cpp probe-instrumentation patch from the repo root.
    git apply --verbose /tmp/stockfish-ca-probe.patch; \
    # Confirm the marker string is actually present in the patched source.
    grep -q 'CA_TB_ABOUT_TO_PROBE' src/search.cpp; \
    cd src; \
    # GENERIC portable build. ARCH=x86-64 is the base generic arch; do NOT use
    # x86-64-avx2 / x86-64-bmi2 (they SIGILL on hosts lacking those extensions).
    make -j"$(nproc)" build ARCH=x86-64; \
    strip stockfish || true; \
    cp stockfish /out-stockfish-patched; \
    chmod +x /out-stockfish-patched

# --- Self-test the patched binary, choose which becomes /usr/local/bin/stockfish
# All checks run against the freshly compiled PATCHED binary. If they all pass,
# the patched binary is selected; otherwise we fall back to the pristine prebuilt.
#
# HOW THE MARKER CHECK WORKS (important -- see verification note):
# Stockfish only enters the Step-5 probe block when tbConfig.cardinality > 0,
# which is derived from MaxCardinality, which is 0 unless SyzygyPath points at a
# directory containing at least one valid Syzygy table. So to make the engine
# ACTUALLY attempt (and fail) a probe on a searched position, we stage a single
# tiny 3-man table (KRvK.rtbw, ~208 bytes) in a temp dir and point SyzygyPath
# there. That raises the probe cardinality to 3 (MaxCardinality=3) while the
# KQvK table remains MISSING. We then search a KQvKQ position (4 men, so the
# ROOT itself is above cardinality and is not resolved by the root probe): as
# the engine searches captures it reaches KQvK sub-positions (3 men, rule50 == 0
# after the queen capture) and probes them; those probes FAIL because the KQvK
# table is missing -> err == TB::ProbeState::FAIL -> CA_TB_ABOUT_TO_PROBE fires.
# Conversely, searching a KRvKR position reaches KRvK sub-positions whose table
# IS present, so those probes SUCCEED and NOTHING is emitted (no UCI spam). This
# "some material present, the searched material missing/insufficient" state is
# exactly the runtime "tablebases still downloading" case the app detects. (The
# staged table is used ONLY for this build-time self-test and is discarded;
# nothing tablebase-related is baked into the image.)
#
# NOTE ON TIMING: the search must actually RUN before `quit`, otherwise `quit`
# aborts it immediately and it returns the first legal move without searching
# (and without ever reaching the Step-5 probe). We therefore feed the `go`
# command, `sleep` to let the search descend into the endgame nodes, then send
# `quit` -- rather than piping `go ... \n quit` in one shot.
RUN set -eux; \
    SF=/out-stockfish-patched; \
    ok=1; \
    mkdir -p /tmp/tbtest; \
    curl -fL "https://tablebase.lichess.ovh/tables/standard/3-4-5-wdl/KRvK.rtbw" -o /tmp/tbtest/KRvK.rtbw; \
    # (1) STRICT uciok smoke test.
    if printf 'uci\nquit\n' | "$SF" | grep -q '^uciok$'; then \
        echo "PATCHED: uciok smoke test PASSED"; \
    else echo "PATCHED: uciok smoke test FAILED"; ok=0; fi; \
    # (2) Functional check: plays a normal depth-1 bestmove from the start position.
    if { printf 'uci\nisready\nposition startpos\ngo depth 1\n'; sleep 1; printf 'quit\n'; } | "$SF" | grep -q '^bestmove '; then \
        echo "PATCHED: depth-1 bestmove check PASSED"; \
    else echo "PATCHED: depth-1 bestmove check FAILED"; ok=0; fi; \
    # (3) Marker FIRES for a SEARCHED endgame whose table is absent/insufficient.
    #     SyzygyPath -> dir holding ONLY KRvK, so cardinality becomes 3 but KQvK
    #     is missing. Searching KQvKQ reaches KQvK (3-man, rule50==0) sub-nodes;
    #     those probes FAIL (missing table) -> CA_TB_ABOUT_TO_PROBE is emitted.
    if { printf 'uci\nsetoption name SyzygyPath value /tmp/tbtest\nisready\nposition fen q3k3/8/8/8/8/8/8/Q3K3 w - - 0 1\ngo movetime 2000\n'; sleep 3; printf 'quit\n'; } | "$SF" | grep -q 'CA_TB_ABOUT_TO_PROBE'; then \
        echo "PATCHED: CA_TB_ABOUT_TO_PROBE marker PRESENT for searched position with absent/insufficient TB -- PASSED"; \
    else echo "PATCHED: marker MISSING for endgame search with insufficient TB -- FAILED"; ok=0; fi; \
    # (4) Marker must NOT fire for a full-board search (32 men, far above the
    #     probe cardinality -> the Step-5 block never runs).
    if { printf 'uci\nsetoption name SyzygyPath value /tmp/tbtest\nisready\nposition startpos\ngo movetime 800\n'; sleep 1; printf 'quit\n'; } | "$SF" | grep -q 'CA_TB_ABOUT_TO_PROBE'; then \
        echo "PATCHED: marker unexpectedly emitted for full-board search -- FAILED"; ok=0; \
    else echo "PATCHED: no marker for full-board search (as expected) -- PASSED"; fi; \
    # (5) Marker must NOT fire when the probe SUCCEEDS (table present): searching a
    #     KRvKR position reaches KRvK (3-man) sub-nodes whose table IS staged, so
    #     those probes succeed -> err != FAIL -> silent (no spam when TB present).
    if { printf 'uci\nsetoption name SyzygyPath value /tmp/tbtest\nisready\nposition fen r3k3/8/8/8/8/8/8/R3K3 w - - 0 1\ngo movetime 2000\n'; sleep 3; printf 'quit\n'; } | "$SF" | grep -q 'CA_TB_ABOUT_TO_PROBE'; then \
        echo "PATCHED: marker unexpectedly emitted when TB probe SUCCEEDS -- FAILED"; ok=0; \
    else echo "PATCHED: no marker when TB probe succeeds (no spam when TB present) -- PASSED"; fi; \
    rm -rf /tmp/tbtest; \
    # Select the engine that becomes /usr/local/bin/stockfish.
    if [ "$ok" = "1" ]; then \
        echo "All patched-binary self-tests PASSED: shipping the PATCHED engine as /usr/local/bin/stockfish"; \
        cp /out-stockfish-patched /out-stockfish; \
    else \
        echo "WARNING: patched-binary self-tests FAILED: falling back to the PRISTINE prebuilt engine as /usr/local/bin/stockfish"; \
        cp /out-stockfish-prebuilt /out-stockfish; \
    fi; \
    chmod +x /out-stockfish

# =========================================================================
# Stage 2: runtime -- Python app + the two compiled engine binaries only
# =========================================================================
# The runtime image contains NO Stockfish source and NO .patch file: only the
# compiled binaries are copied out of the builder stage.
FROM --platform=linux/amd64 python:3.11-slim

# Copy the selected engine (patched if it passed self-tests, else pristine
# prebuilt) and the pristine prebuilt fallback out of the builder stage.
COPY --from=builder /out-stockfish          /usr/local/bin/stockfish
COPY --from=builder /out-stockfish-prebuilt /usr/local/bin/stockfish-prebuilt
RUN chmod +x /usr/local/bin/stockfish /usr/local/bin/stockfish-prebuilt; \
    # Runtime smoke test: the shipped engine must complete a UCI handshake.
    printf 'uci\nquit\n' | /usr/local/bin/stockfish | grep -q '^uciok$'; \
    echo "Runtime Stockfish smoke test passed: uciok received from /usr/local/bin/stockfish"

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

# Point the app at the (patched) downloaded engine and default to 128 threads.
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
#                   Default https://tablebase.lichess.ovh/tables/standard/3-4-5-wdl/
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
# The 5-men WDL Syzygy download now runs in a BACKGROUND daemon thread started
# at app import (app.py calls syzygy.start_background_download()), NOT as a
# blocking prelude before gunicorn. gunicorn therefore binds the port
# IMMEDIATELY on container start, so Render never reports 'No open ports
# detected' while the ~386 MB download proceeds in the background. The download
# writes into $SYZYGY_DIR (ephemeral, marker-guarded so a restart reuses it; a
# no-op when SYZYGY_DISABLE=1 or SYZYGY_DIR is empty) and is NOT baked into the
# image. /healthz and the port bind NEVER wait on it; the frontend polls
# GET /api/tablebase-status ({ready, downloaded, total, percent}) for progress.
CMD ["sh", "-c", "gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT} app:app"]
