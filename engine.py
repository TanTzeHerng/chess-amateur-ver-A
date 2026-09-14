#!/usr/bin/env python3
"""Stockfish UCI wrapper for the "Chess Amateur" bot.

The bot is Stockfish restricted to a very shallow search (depth 1) but with a
large thread count (128), matching the proven invocation in
/projects/sandbox/sf_move.py.

PLAYER-FACING HONESTY CONSTRAINT (unchanged):
  best_move() still returns ONLY the bestmove (UCI) to the server/API/UI. The
  human opponent NEVER sees Stockfish's principal variation or evaluation
  score through the app.

OPERATOR TELEMETRY (added):
  For engine testing/observability, the FULL raw UCI output of each search
  (every 'info' line carrying depth/score/PV/nodes, plus the final 'bestmove')
  is logged to STDOUT so it is captured by the platform's server logs
  (e.g. Render). This telemetry lives only in the server logs the operator
  sees; it is never sent to the browser, so the player-facing constraint above
  still holds. Set SF_LOG_UCI=0 to disable this logging.

Configuration via environment:
  STOCKFISH_PATH  path to the Stockfish binary
                  (default: /projects/sandbox/stockfish/stockfish-linux-x86-64-universal)
  SF_LOG_UCI      "1" (default) to log full UCI search output to server logs;
                  "0" to disable.
"""
import os
import select
import subprocess
import sys
import threading
import time

try:
    import syzygy as _syzygy
except Exception:  # pragma: no cover - syzygy module must import, but be safe
    _syzygy = None


def syzygy_setoption_commands(dir_getter=None):
    """Return the list of UCI setoption commands for Syzygy tablebases.

    Sent AFTER 'uci'/'uciok' and BEFORE 'isready'/'readyok'. Emits a single
    'setoption name SyzygyPath value <dir>' when a non-empty, existing Syzygy
    directory is configured; otherwise returns an empty list (no SyzygyPath).

    `dir_getter` is injectable for tests; defaults to syzygy.syzygy_dir which
    returns the configured dir only when it exists and holds .rtbw files.
    """
    if dir_getter is None:
        dir_getter = _syzygy.syzygy_dir if _syzygy is not None else (lambda: None)
    try:
        directory = dir_getter()
    except Exception:
        directory = None
    if not directory:
        return []
    return ["setoption name SyzygyPath value %s" % directory]

DEFAULT_STOCKFISH_PATH = "/projects/sandbox/stockfish/stockfish-linux-x86-64-universal"
DEFAULT_DEPTH = 1
DEFAULT_THREADS = 128

# Bounded waits so a dead/incompatible engine FAILS FAST instead of hanging.
# The uci/isready handshake must complete within a few seconds; a depth-1
# search is near-instant but we allow a generous finite cap for slow/busy
# hosts. On timeout (or if the process already exited) best_move raises a
# clear RuntimeError rather than blocking forever on readline().
HANDSHAKE_TIMEOUT = float(os.environ.get("SF_HANDSHAKE_TIMEOUT", "10"))
SEARCH_TIMEOUT = float(os.environ.get("SF_SEARCH_TIMEOUT", "30"))

# Operator telemetry: log the full raw UCI search output (info + bestmove) to
# stdout so the platform's server logs capture it. Never sent to the player.
LOG_UCI = os.environ.get("SF_LOG_UCI", "1") != "0"


def _uci_log(line):
    """Write one raw UCI line to stdout (server logs), timestamped + flushed.

    This is operator-only telemetry captured by the platform's log collector
    (e.g. Render). It is NEVER returned to the API/UI, so the player still
    cannot see Stockfish's analysis. Flushed because container stdout is
    block-buffered and would otherwise not reach the log collector.
    """
    if not LOG_UCI:
        return
    try:
        sys.stdout.write("[%s] SF| %s\n"
                         % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
        sys.stdout.flush()
    except Exception:
        pass


class EngineUnavailable(RuntimeError):
    """Raised when Stockfish fails to start or respond in a bounded time."""


class ChessAmateurEngine:
    """A thin, reusable wrapper around a long-lived Stockfish process.

    A single engine process is kept alive across moves for efficiency; between
    positions we send 'ucinewgame'/'isready'. The process is re-spawned
    automatically if it has died. Access is serialized with a lock so the
    wrapper is safe to share across HTTP handler threads.

    SINGLE-PROCESS INVARIANT (memory-critical):
      At most ONE Stockfish process may ever be alive at any instant. One
      Stockfish process at Threads=1 uses ~248 MB RSS (dominated by the NNUE
      net). On a 512 MB host (Render free tier) there is only room for a single
      engine plus the Python process. If a replacement engine were spawned
      while the previous one were still alive/shutting down, RSS would
      transiently double to ~500 MB and the OOM killer would terminate the
      service (observed as intermittent 502s with nothing in the app logs).
      Therefore EVERY code path that spawns a replacement engine MUST first
      fully terminate AND os-reap (wait() returned) the previous process before
      calling Popen() again. _kill() is the single choke point that guarantees
      this; _ensure_proc() and best_move()'s retry path both route replacement
      through it so two engines never coexist.
    """

    def __init__(self, path=None, depth=DEFAULT_DEPTH, threads=DEFAULT_THREADS):
        self.path = path or os.environ.get("STOCKFISH_PATH", DEFAULT_STOCKFISH_PATH)
        self.depth = int(depth)
        self.threads = int(threads)
        self._proc = None
        # Thread count currently applied to the live Stockfish process. We only
        # re-send "setoption name Threads" when the requested value changes.
        self._applied_threads = None
        # SyzygyPath currently applied to the live Stockfish process (or None
        # when none is set). While the background download runs, the Syzygy
        # directory starts EMPTY (no SyzygyPath -> tbConfig.cardinality==0 ->
        # the Step-5 probe path is inactive and the CA_TB_ABOUT_TO_PROBE marker
        # can never fire). Once the dir holds >=1 .rtbw, we re-apply SyzygyPath
        # on the SAME long-lived process (a plain 'setoption', NOT a respawn) so
        # the probe path activates and the marker fires for searched
        # sub-positions whose table is not yet downloaded. This preserves the
        # single-process invariant (no second engine is ever spawned).
        self._applied_syzygy_path = None
        self._lock = threading.Lock()

    # -- process lifecycle -------------------------------------------------

    def _spawn(self):
        # Unbuffered BINARY stdout (bufsize=0, no text wrapper). We do our own
        # line splitting over raw bytes read with os.read()+select so that
        # select() reliably reflects data availability. A line-buffered
        # TextIOWrapper (text=True, bufsize=1) would pull a big chunk from the
        # pipe into Python's internal buffer, after which select() reports the
        # fd "not readable" even though whole lines (e.g. 'uciok') are already
        # buffered -> the bounded read would spuriously time out. Reading raw
        # bytes avoids that hidden-buffer trap entirely.
        proc = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        # Per-process byte buffer for our line reader.
        proc._sf_buf = b""
        self._send(proc, "uci")
        self._read_until(proc, "uciok", timeout=HANDSHAKE_TIMEOUT)
        self._send(proc, "setoption name Threads value %d" % self.threads)
        # Tablebases: after 'uciok' and BEFORE 'isready'/'readyok', point
        # Stockfish at the Syzygy directory when one is configured + populated.
        # WDL-only 5-men set; disabled/absent -> no command emitted.
        applied_path = None
        for cmd in syzygy_setoption_commands():
            self._send(proc, cmd)
            # Remember the value we applied so we can detect later that the
            # download populated the dir and re-apply on the live process.
            if cmd.startswith("setoption name SyzygyPath value "):
                applied_path = cmd[len("setoption name SyzygyPath value "):]
        self._send(proc, "isready")
        self._read_until(proc, "readyok", timeout=HANDSHAKE_TIMEOUT)
        self._applied_threads = self.threads
        self._applied_syzygy_path = applied_path
        return proc

    def _apply_syzygy_path(self, proc):
        """Re-apply SyzygyPath on the LIVE process if the configured dir became
        populated (or changed) since the last handshake.

        This is a plain UCI 'setoption' on the SAME process -- NOT a respawn --
        so the single-process invariant is untouched. It lets the marker fire
        during the background download: the dir starts empty (no path) and,
        once it holds >=1 .rtbw, Stockfish is pointed at it so the Step-5 probe
        path activates for searched positions whose table is still missing.
        """
        cmds = syzygy_setoption_commands()
        desired = None
        for cmd in cmds:
            if cmd.startswith("setoption name SyzygyPath value "):
                desired = cmd[len("setoption name SyzygyPath value "):]
        if desired == self._applied_syzygy_path:
            return
        # Only ever ADD/UPDATE a path here (the download only grows the dir).
        # If desired is None we leave the previously-applied path in place.
        if desired is None:
            return
        self._send(proc, "setoption name SyzygyPath value %s" % desired)
        self._send(proc, "isready")
        self._read_until(proc, "readyok", timeout=HANDSHAKE_TIMEOUT)
        self._applied_syzygy_path = desired

    def _apply_threads(self, proc, threads):
        """Re-apply the Threads UCI option only when it changes."""
        if threads == self._applied_threads:
            return
        self._send(proc, "setoption name Threads value %d" % threads)
        self._send(proc, "isready")
        self._read_until(proc, "readyok", timeout=HANDSHAKE_TIMEOUT)
        self._applied_threads = threads

    def _ensure_proc(self):
        # Reuse the live process if it is still running.
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        # Otherwise a replacement is needed. Enforce the single-process
        # invariant: fully terminate AND reap any existing process (even a
        # dead-but-unreaped one) BEFORE spawning, so two engines never coexist
        # and the OS reclaims the old process's memory first.
        self._kill()
        self._applied_threads = None
        self._applied_syzygy_path = None
        self._proc = self._spawn()
        return self._proc

    @staticmethod
    def _send(proc, line):
        # stdin is a raw binary pipe (bufsize=0); encode and write bytes.
        proc.stdin.write((line + "\n").encode("ascii"))
        try:
            proc.stdin.flush()
        except Exception:
            pass

    @staticmethod
    def _readline_bounded(proc, deadline):
        """Read one stdout line (str), waiting at most until `deadline`.

        Reads RAW BYTES from the stdout pipe fd with os.read() gated by
        select(), maintaining our own byte buffer (proc._sf_buf) that we split
        on newlines. Because we never rely on a TextIOWrapper's hidden internal
        buffer, select() is an accurate readiness signal and a dead/hung engine
        can never block us forever.

        Raises EngineUnavailable if the process has already exited with no
        remaining buffered line, or the deadline elapses. Returns the decoded
        line (without trailing newline) otherwise.
        """
        fd = proc.stdout.fileno()
        while True:
            # Serve a complete line already sitting in our own buffer first.
            nl = proc._sf_buf.find(b"\n")
            if nl != -1:
                line = proc._sf_buf[:nl]
                proc._sf_buf = proc._sf_buf[nl + 1:]
                return line.decode("utf-8", "replace").rstrip("\r")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineUnavailable(
                    "Stockfish failed to start / respond (timed out)")
            rlist, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            if not rlist:
                # No data yet. If the process has exited and drained, fail fast.
                if proc.poll() is not None:
                    raise EngineUnavailable(
                        "Stockfish failed to start / respond "
                        "(process exited with %s)" % proc.returncode)
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                raise EngineUnavailable(
                    "Stockfish failed to start / respond (read error)")
            if chunk == b"":
                # EOF: the pipe closed, i.e. the process died. Surface any
                # trailing partial line, else fail fast.
                if proc._sf_buf:
                    line = proc._sf_buf
                    proc._sf_buf = b""
                    return line.decode("utf-8", "replace").rstrip("\r")
                raise EngineUnavailable(
                    "Stockfish failed to start / respond (stdout closed)")
            proc._sf_buf += chunk

    def _read_until(self, proc, prefix, timeout):
        """Read stdout lines until one starts with `prefix`, bounded by
        `timeout` seconds. Ignores other lines. Raises EngineUnavailable on
        timeout or if the process exits before the expected line arrives."""
        deadline = time.monotonic() + timeout
        while True:
            line = self._readline_bounded(proc, deadline)
            if line.strip().startswith(prefix):
                return line.strip()

    # -- public API --------------------------------------------------------

    # Distinctive UCI line the PATCHED Stockfish emits (FEAT-003) when it is
    # ABOUT to probe a SEARCHED position but the tablebase is absent/insufficient
    # (probe_wdl returned ProbeState::FAIL). We detect it during a search to
    # drive the app-layer move deferral (FEAT-004). It is operator-only
    # telemetry -- logged to server stdout, NEVER returned to the browser.
    PROBE_MARKER = "CA_TB_ABOUT_TO_PROBE"

    def best_move(self, fen, threads=None):
        """Return Chess Amateur's move (UCI string) for the given FEN.

        Returns None if there is no legal move ('bestmove (none)'/'0000').

        `threads` optionally overrides the Stockfish Threads option for this
        move (and stays applied until changed again). When omitted, the
        engine's default thread count is used. Depth is never affected.

        This preserves the historic contract: callers that only want the move
        get ONLY the move. The tablebase-probe signal is exposed separately via
        best_move_with_probe_flag() so the browser-facing path is unchanged.
        """
        uci, _ = self.best_move_with_probe_flag(fen, threads=threads)
        return uci

    def best_move_with_probe_flag(self, fen, threads=None):
        """Like best_move(), but also report whether the search emitted the
        CA_TB_ABOUT_TO_PROBE marker.

        Returns (uci, tb_probe_seen) where uci is the bestmove (or None) and
        tb_probe_seen is True iff the patched engine signalled it was about to
        probe a searched position whose tablebase is absent/insufficient. The
        marker is NEVER included in the returned move and is NEVER sent to the
        browser by callers -- it only informs the server-side deferral policy.
        """
        with self._lock:
            try:
                return self._best_move_locked(fen, threads)
            except (BrokenPipeError, OSError, ValueError, EngineUnavailable):
                # Process may have died mid-request (broken pipe) or failed to
                # respond in time. Fully terminate + reap the old process
                # (single-process invariant) BEFORE _best_move_locked ->
                # _ensure_proc spawns a replacement, so RSS never doubles. We
                # retry exactly once: if the binary is genuinely broken (e.g.
                # SIGILL-on-launch because the CPU lacks the required
                # instructions), this second attempt also raises
                # EngineUnavailable, which propagates to the caller as a
                # fast, clear failure instead of an infinite hang.
                self._kill()
                return self._best_move_locked(fen, threads)

    def _best_move_locked(self, fen, threads=None):
        proc = self._ensure_proc()
        if threads is not None:
            self._apply_threads(proc, int(threads))
        # If the background download has populated the Syzygy dir since this
        # process handshook, point the LIVE engine at it (setoption, no
        # respawn) so the Step-5 probe path is active and the marker can fire.
        self._apply_syzygy_path(proc)
        self._send(proc, "ucinewgame")
        self._send(proc, "isready")
        self._read_until(proc, "readyok", timeout=HANDSHAKE_TIMEOUT)
        self._send(proc, "position fen %s" % fen)
        _uci_log("position fen %s" % fen)
        self._send(proc, "go depth %d" % self.depth)
        _uci_log("go depth %d" % self.depth)

        deadline = time.monotonic() + SEARCH_TIMEOUT
        bestmove = None
        tb_probe_seen = False
        while True:
            line = self._readline_bounded(proc, deadline)
            line = line.strip()
            if not line:
                continue
            # OPERATOR TELEMETRY: log every raw UCI line (info lines carrying
            # depth/score/PV/nodes, and the final bestmove) to the server logs.
            # This is captured by the platform (e.g. Render) for the operator
            # only; it is never returned to the API/UI below.
            _uci_log(line)
            # DEFERRAL SIGNAL (server-side only): the patched engine emits
            # 'info string CA_TB_ABOUT_TO_PROBE' when it is about to probe a
            # SEARCHED position whose tablebase is absent/insufficient. Record
            # that the marker fired; it is logged above but NEVER returned to
            # the browser -- only best_move_with_probe_flag()'s bool surfaces it
            # to the app-layer deferral policy.
            if self.PROBE_MARKER in line:
                tb_probe_seen = True
            # PLAYER-FACING: only the bestmove is ever RETURNED. 'info' lines
            # (PV + eval) are logged above but never surfaced to the player.
            if line.startswith("bestmove"):
                parts = line.split()
                if len(parts) >= 2:
                    bestmove = parts[1]
                break
        if bestmove in (None, "(none)", "0000"):
            return None, tb_probe_seen
        return bestmove, tb_probe_seen

    # -- shutdown ----------------------------------------------------------

    def _kill(self):
        """Synchronously terminate and OS-reap the current process.

        This is the single choke point that upholds the single-process
        invariant: it does not return until the old Stockfish process is dead
        and reaped (wait() has returned), so its ~248 MB of RSS is reclaimed
        before any replacement is spawned. Escalates terminate() -> kill() and
        always wait()s so no zombie/live process lingers.
        """
        proc = self._proc
        # Clear the reference up front so no other logic can observe a
        # half-dead process as "current".
        self._proc = None
        self._applied_threads = None
        self._applied_syzygy_path = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                # Ask politely first, then wait a short while for it to exit.
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    # Still alive -> force kill and reap.
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
            else:
                # Already exited; reap it so the OS releases the process slot.
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        finally:
            # Close pipes to release fds regardless of exit path.
            for stream in (getattr(proc, "stdin", None),
                           getattr(proc, "stdout", None)):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

    def close(self):
        """Cleanly stop the engine process (send 'quit', then wait/kill)."""
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    self._send(proc, "quit")
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":
    eng = ChessAmateurEngine()
    try:
        start_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        print(eng.best_move(start_fen))
    finally:
        eng.close()
