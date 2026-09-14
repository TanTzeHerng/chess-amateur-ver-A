#!/usr/bin/env python3
"""5-men WDL-only Syzygy tablebase support for Chess Amateur.

Two responsibilities, both configurable and fully DISABLE-able so local runs
and the offline test suite never attempt a ~386 MB download:

  1. syzygy_dir():  the directory Stockfish's SyzygyPath UCI option should point
     at, or None when tablebases are disabled/unconfigured. engine.py sends the
     'setoption name SyzygyPath value <dir>' ONLY when this returns a non-empty
     existing directory (see engine._syzygy_setoption).

  2. ensure_downloaded(): a CONTAINER-STARTUP step (wired into the Docker CMD)
     that downloads the 5-men WDL (.rtbw) files into an EPHEMERAL directory
     (not baked into the image, no persistent disk). It is idempotent and
     guarded by a marker file so a restart reuses an existing download and the
     single gunicorn worker never double-downloads.

WDL-only is deliberate: at depth 1 the WDL (win/draw/loss) tables are what can
change the root move choice; DTZ (distance-to-zero) tables are not needed and
would roughly double the download size for no benefit here.

Environment:
  SYZYGY_DIR      Target directory for the .rtbw files (default /tmp/syzygy).
                  If explicitly set to empty, tablebases are DISABLED.
  SYZYGY_URL      Base URL to download individual .rtbw files from. Default is
                  the public Lichess Syzygy mirror
                  (https://tablebase.lichess.ovh/tables/standard/3-4-5/).
  SYZYGY_DISABLE  Set to "1"/"true" to skip the download AND unset SyzygyPath
                  (used by tests / local dev so no network is touched).

Startup wiring: the Docker CMD runs `python -m syzygy` (this module's __main__)
BEFORE gunicorn boots. The download runs to completion first; because it writes
into the ephemeral disk and is guarded by a marker, a restart is fast (marker
present -> skip). gunicorn (and therefore /healthz) only starts afterwards, so
healthz is never served against a half-initialized engine. Set SYZYGY_DISABLE=1
to boot instantly without tablebases.
"""
import os
import sys
import urllib.request

DEFAULT_SYZYGY_DIR = "/tmp/syzygy"
DEFAULT_SYZYGY_URL = "https://tablebase.lichess.ovh/tables/standard/3-4-5/"
# Marker file (inside the target dir) written after a COMPLETE download so a
# restart can trust the directory and skip re-downloading.
MARKER_NAME = ".syzygy_complete"


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def is_disabled():
    """True when tablebases are turned off (SYZYGY_DISABLE, or SYZYGY_DIR="")."""
    if _truthy(os.environ.get("SYZYGY_DISABLE")):
        return True
    # An explicitly-empty SYZYGY_DIR means "no tablebases".
    if "SYZYGY_DIR" in os.environ and not os.environ["SYZYGY_DIR"].strip():
        return True
    return False


def configured_dir():
    """The configured target directory (regardless of whether it exists yet),
    or None when disabled."""
    if is_disabled():
        return None
    return os.environ.get("SYZYGY_DIR", DEFAULT_SYZYGY_DIR).strip() or None


def _has_tablebase_files(directory):
    """True when `directory` holds at least one Syzygy WDL (.rtbw) file."""
    try:
        for name in os.listdir(directory):
            if name.endswith(".rtbw"):
                return True
    except OSError:
        return False
    return False


def syzygy_dir():
    """Return the directory to hand Stockfish's SyzygyPath, or None.

    Returns the configured directory ONLY when tablebases are enabled AND the
    directory currently contains at least one .rtbw file; otherwise None so
    engine.py does not set an empty/bogus SyzygyPath.
    """
    directory = configured_dir()
    if not directory:
        return None
    if not _has_tablebase_files(directory):
        return None
    return directory


# --- 5-men WDL (.rtbw) file names -----------------------------------------
#
# The complete 3-4-5-men Syzygy WDL set. These are the canonical Syzygy file
# names (material signatures). Downloading all of them yields the full 5-men
# (and the tiny 3/4-men) WDL tables Stockfish probes at the root.
def _wdl_filenames():
    """Enumerate the 3-4-5-men Syzygy WDL (.rtbw) file base names.

    Rather than hard-code the full ~140-name list, we generate the material
    signatures programmatically: every way to split the non-king pieces across
    the two sides for total piece counts 3..5 (2 kings + 1..3 others), using
    the canonical Syzygy naming (e.g. 'KQvK', 'KRPvKR', 'KQQvKQ').
    """
    import itertools
    pieces = ["Q", "R", "B", "N", "P"]
    order = {p: i for i, p in enumerate(pieces)}

    def side_str(combo):
        # Canonical order Q,R,B,N,P within a side.
        return "".join(sorted(combo, key=lambda p: order[p]))

    names = set()
    for total_others in range(1, 4):          # 1..3 non-king pieces -> 3..5 men
        for white_count in range(0, total_others + 1):
            black_count = total_others - white_count
            for w in itertools.combinations_with_replacement(pieces, white_count):
                for b in itertools.combinations_with_replacement(pieces, black_count):
                    white = "K" + side_str(w)
                    black = "K" + side_str(b)
                    # Canonicalize: the "stronger" (lexicographically larger by
                    # material) side goes first, matching Syzygy's single file
                    # per material signature.
                    a, c = white, black
                    if (len(a), a) < (len(c), c):
                        a, c = c, a
                    names.add("%sv%s" % (a, c))
    return sorted(names)


def ensure_downloaded(log=None):
    """Download the 5-men WDL Syzygy set into the configured dir (idempotent).

    No-op (returns None) when disabled. Guarded by a marker file so a restart
    reuses an existing complete download. Best-effort per file: a file that
    fails to download is skipped (the engine still probes whatever is present).
    Returns the target directory on success, or None when disabled.
    """
    def _log(msg):
        if log:
            log(msg)
        else:
            sys.stdout.write("[syzygy] %s\n" % msg)
            sys.stdout.flush()

    if is_disabled():
        _log("disabled (SYZYGY_DISABLE / empty SYZYGY_DIR); skipping download")
        return None

    directory = configured_dir()
    base_url = os.environ.get("SYZYGY_URL", DEFAULT_SYZYGY_URL).strip() \
        or DEFAULT_SYZYGY_URL
    if not base_url.endswith("/"):
        base_url += "/"

    os.makedirs(directory, exist_ok=True)
    marker = os.path.join(directory, MARKER_NAME)
    if os.path.exists(marker) and _has_tablebase_files(directory):
        _log("already present (marker found); reusing %s" % directory)
        return directory

    names = _wdl_filenames()
    _log("downloading %d WDL (.rtbw) files from %s into %s"
         % (len(names), base_url, directory))
    ok = 0
    for base in names:
        fname = base + ".rtbw"
        dest = os.path.join(directory, fname)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            ok += 1
            continue
        url = base_url + fname
        try:
            _download_one(url, dest)
            ok += 1
        except Exception as exc:  # pragma: no cover - network-dependent
            _log("skip %s (%s)" % (fname, exc))
            # Remove a partial file so a later retry re-fetches it cleanly.
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
    if ok:
        # Write the completion marker so a restart reuses this directory.
        try:
            with open(marker, "w") as fh:
                fh.write("%d files\n" % ok)
        except OSError:
            pass
    _log("done: %d/%d files present in %s" % (ok, len(names), directory))
    return directory


def _download_one(url, dest):
    """Download a single file to `dest` via a temp file + atomic rename."""
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dest)


if __name__ == "__main__":
    # Container-startup entry point (wired into the Docker CMD before gunicorn).
    ensure_downloaded()
