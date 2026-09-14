#!/usr/bin/env python3
"""FIDE rating lookup for Chess Amateur signup seeding.

Given a FIDE ID supplied at registration, we fetch the player's public profile
page from ratings.fide.com and read their three published ratings:

    FIDE "standard"  -> our "classical"
    FIDE "rapid"     -> our "rapid"
    FIDE "blitz"     -> our "blitz"

Design (so it is unit-testable WITHOUT network):
  * ``parse_profile_html(html)`` is a PURE function: feed it the raw profile
    HTML and it returns ``{"classical": int|None, "rapid": int|None,
    "blitz": int|None}``. Any rating the player does not hold (FIDE shows it as
    a blank / "0" / "Not rated") comes back as ``None``. This is the piece the
    tests exercise with fixture HTML -- no sockets involved.
  * ``fetch_profile_html(fide_id, ...)`` is a THIN HTTP wrapper that downloads
    the page. It is the only part that touches the network and is kept out of
    the tests.
  * ``lookup_ratings(fide_id, ...)`` glues the two together and NEVER raises:
    on any failure (bad id, network error, unparseable page) it returns
    all-``None`` so a signup can safely fall back to the 1400 default.

The parser is deliberately tolerant of markup changes: FIDE has shipped several
profile layouts over the years (a ``profile-standart`` / ``profile-rapid`` /
``profile-blitz`` block set, and a "std./rapid/blitz" label-then-number table).
We try a couple of strategies and treat a rating of 0 / non-numeric / absent as
"not rated" -> ``None``.
"""
import re

__all__ = ["parse_profile_html", "fetch_profile_html", "lookup_ratings",
           "FIDE_PROFILE_URL"]

# Public profile page. %s is the numeric FIDE ID.
FIDE_PROFILE_URL = "https://ratings.fide.com/profile/%s"

# Reasonable bounds for a real FIDE rating; anything outside is treated as
# "not a rating" (e.g. a stray year like 2024 sitting next to a label would
# still parse, but our label-anchored regexes avoid that). A published FIDE
# rating is at least ~1000; 0 means "not rated".
_MIN_RATING = 1000
_MAX_RATING = 3500


def _clean_rating(value):
    """Coerce a scraped token to an int rating, or None when 'not rated'.

    FIDE renders an absent rating as an empty cell, ``0``, ``-`` or
    ``Not rated``. Returns None for all of those (and for anything outside a
    plausible rating range) so the caller can fall back to the 1400 default.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"\d+", s)
    if not m:
        return None
    try:
        n = int(m.group(0))
    except (TypeError, ValueError):
        return None
    if n < _MIN_RATING or n > _MAX_RATING:
        return None
    return n


# Strategy A: the modern profile layout renders each rating inside a block whose
# class names the time control, e.g.
#   <div class="profile-standart"><span class="...">1611</span> std</div>
# FIDE historically MISSPELLS standard as "standart"; accept both spellings.
# We capture the FIRST run of >=3 digits after the control's class name. The
# rating lives inside the block's <span>; the class attribute may itself
# contain hyphens/words (e.g. "profile-top-rating-data") but no 3+ digit runs,
# so anchoring on the class then grabbing the first 3-4 digit number is safe.
# A block with no such number (unrated: blank / "Not rated" / "0") yields no
# match here -> None. NB: a bare "0" (unrated) is <3 digits so it is ignored.
# The guard "(?:(?!profile-(?:standar[dt]|rapid|blitz)).)*?" stops the scan at
# the NEXT rating-control block, so an unrated control (no number in its own
# block) does NOT steal the next control's rating -> it correctly yields None.
# Crucially it does NOT stop on unrelated "profile-*" classes (e.g. the
# "profile-top-rating-data" span that actually WRAPS the number).
_STOP = r"(?:(?!profile-(?:standar[dt]|rapid|blitz)).)*?"
_BLOCK_PATTERNS = {
    "classical": re.compile(
        r"profile-standar[dt]\b" + _STOP + r"(\d{3,4})",
        re.IGNORECASE | re.DOTALL),
    "rapid": re.compile(
        r"profile-rapid\b" + _STOP + r"(\d{3,4})",
        re.IGNORECASE | re.DOTALL),
    "blitz": re.compile(
        r"profile-blitz\b" + _STOP + r"(\d{3,4})",
        re.IGNORECASE | re.DOTALL),
}

# Strategy B: a label-then-number layout, e.g. a table/list where the words
# "std."/"standard", "rapid", "blitz" are immediately followed by the number.
_LABEL_PATTERNS = {
    "classical": re.compile(
        r"(?:std\.?|standard)\D{0,40}?(\d{3,4}|Not\s*rated|-)",
        re.IGNORECASE | re.DOTALL),
    "rapid": re.compile(
        r"rapid\D{0,40}?(\d{3,4}|Not\s*rated|-)",
        re.IGNORECASE | re.DOTALL),
    "blitz": re.compile(
        r"blitz\D{0,40}?(\d{3,4}|Not\s*rated|-)",
        re.IGNORECASE | re.DOTALL),
}


def parse_profile_html(html):
    """PURE parser: extract {classical, rapid, blitz} from FIDE profile HTML.

    Returns a dict with those three keys; each value is an ``int`` rating or
    ``None`` when the player has no rating in that time control. Never raises:
    on empty/garbage input it returns all-``None``.

    Maps FIDE's "standard" (a.k.a. the misspelled "standart" / "std.") to our
    "classical". This function does NO network I/O and is the unit-tested core.
    """
    out = {"classical": None, "rapid": None, "blitz": None}
    if not html or not isinstance(html, str):
        return out
    for control in ("classical", "rapid", "blitz"):
        rating = None
        m = _BLOCK_PATTERNS[control].search(html)
        if m:
            rating = _clean_rating(m.group(1))
        if rating is None:
            m = _LABEL_PATTERNS[control].search(html)
            if m:
                rating = _clean_rating(m.group(1))
        out[control] = rating
    return out


def fetch_profile_html(fide_id, timeout=8):
    """THIN network wrapper: download the FIDE profile page for ``fide_id``.

    Returns the HTML string, or ``None`` on any HTTP/network error or if the id
    is missing/non-numeric. Kept separate from the parser so tests can supply
    fixture HTML without hitting the network. Not exercised by the offline test
    suite.
    """
    fid = _normalize_id(fide_id)
    if fid is None:
        return None
    try:
        import requests  # imported lazily so the offline path needs no network dep
    except Exception:  # pragma: no cover - requests is a declared dependency
        return None
    url = FIDE_PROFILE_URL % fid
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (ChessAmateur signup FIDE lookup)"})
    except Exception:  # pragma: no cover - network failures are non-fatal
        return None
    if resp.status_code != 200:
        return None
    return resp.text


def _normalize_id(fide_id):
    """Return the FIDE id as a clean numeric string, or None if invalid.

    A FIDE ID is a run of digits (leading zeros are not meaningful but harmless
    in the URL). Accepts an int or a string with surrounding whitespace; any
    non-digit content makes it invalid -> None.
    """
    if fide_id is None:
        return None
    s = str(fide_id).strip()
    if not s or not s.isdigit():
        return None
    return s


def lookup_ratings(fide_id, fetcher=None):
    """Resolve a FIDE ID to ``{classical, rapid, blitz}`` (values int|None).

    Glue that NEVER raises: on a missing/invalid id, a network failure, or an
    unparseable page it returns all-``None`` so signup can fall back to 1400.

    ``fetcher`` is an injection point for testing: pass a callable taking the
    (normalized) FIDE id and returning HTML (or None) to avoid the network.
    Defaults to :func:`fetch_profile_html`.
    """
    all_none = {"classical": None, "rapid": None, "blitz": None}
    fid = _normalize_id(fide_id)
    if fid is None:
        return dict(all_none)
    fetch = fetcher or fetch_profile_html
    try:
        html = fetch(fid)
    except Exception:  # pragma: no cover - defensive; fetcher should not raise
        return dict(all_none)
    if not html:
        return dict(all_none)
    try:
        return parse_profile_html(html)
    except Exception:  # pragma: no cover - parse_profile_html already guards
        return dict(all_none)
