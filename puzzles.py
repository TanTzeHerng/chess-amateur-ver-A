#!/usr/bin/env python3
"""Bell-curve puzzle selection for the Train tab (FEAT-008).

Pure, framework-free selection helpers so they can be unit-tested in isolation
(the DB access is injected as a callback). The player is assigned a random
puzzle whose probability follows a BELL CURVE over the distance between the
puzzle's rating and the player's puzzle rating: the more standard deviations a
puzzle's rating is from the player's, the LOWER its probability of being
picked.

IMPORTANT: selection compares against the puzzle's RAW LICHESS rating (the same
value the Glicko-2 update uses). Only the value SHOWN to the player is
lichess_rating - 600 (see ratings.puzzle_displayed_rating); the selection math
never uses the displayed value.

Efficiency: we do NOT load the whole puzzles table. We draw a TARGET rating from
the normal distribution N(player_rating, SIGMA), then fetch a small band of
candidate puzzles around that target from the DB (an indexed range scan on
lichess_rating) and pick the nearest, weighting ties/near-ties by the same
normal PDF. This yields the bell-curve distribution while touching only a
handful of rows per assignment.
"""
import math
import random

# Standard deviation (in rating points) of the bell curve used to pick a
# puzzle relative to the player's puzzle rating. 200 points is roughly one
# Glicko class: puzzles within ~200 of the player are common, those ~400+
# (two sigma) away are rare but still possible, so difficulty tracks skill
# while staying varied. Chosen as a reasonable, documented default.
DEFAULT_SIGMA = 200.0

# Half-width (in rating points) of the candidate band fetched around the drawn
# target rating. Wide enough that a band almost always contains a puzzle even
# where the curated set is sparse, narrow enough to stay a small indexed scan.
CANDIDATE_BAND = 100


def normal_pdf(x, mean, sigma):
    """Unnormalized-friendly normal probability density at x.

    Standard Gaussian PDF; used as the selection WEIGHT. The normalizing
    constant is irrelevant for weighted sampling but included so the function
    is a genuine PDF and easy to reason about in tests."""
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    z = (x - mean) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


def selection_weight(candidate_rating, player_rating, sigma=DEFAULT_SIGMA):
    """Bell-curve WEIGHT for a candidate puzzle rating given the player's
    puzzle rating.

    A pure function (the unit-tested core of the requirement): the weight is
    the normal PDF over |candidate_rating - player_rating|, so a candidate
    NEARER the player's rating always gets a HIGHER weight, and the weight
    falls off with the number of standard deviations away."""
    return normal_pdf(float(candidate_rating), float(player_rating), sigma)


def weighted_choice(candidates, player_rating, sigma=DEFAULT_SIGMA, rng=None):
    """Pick one candidate puzzle weighted by the bell curve, or None if the
    list is empty.

    `candidates` is a list of puzzle dicts (each with a 'lichess_rating').
    Selection uses each candidate's RAW lichess_rating. `rng` is injectable for
    deterministic tests."""
    if not candidates:
        return None
    rng = rng or random
    weights = [selection_weight(c["lichess_rating"], player_rating, sigma)
               for c in candidates]
    total = sum(weights)
    if total <= 0:
        # Degenerate (all weights underflowed to 0): fall back to uniform.
        return rng.choice(candidates)
    r = rng.uniform(0.0, total)
    upto = 0.0
    for cand, w in zip(candidates, weights):
        upto += w
        if upto >= r:
            return cand
    return candidates[-1]  # floating-point guard


def sample_puzzle(player_rating, fetch_band, extent=(None, None),
                  sigma=DEFAULT_SIGMA, band=CANDIDATE_BAND, rng=None,
                  max_tries=6):
    """Assign a puzzle via the bell-curve sampler.

    Efficient (never loads all puzzles):
      1. Draw a TARGET rating ~ N(player_rating, sigma), clamped to the range
         of ratings that actually have puzzles (`extent` = (min, max)).
      2. Fetch a small candidate band [target-band, target+band] via
         `fetch_band(low, high)` (an indexed DB range scan).
      3. Pick one candidate weighted by the bell curve (weighted_choice).
    Retries a few times with a fresh target if a band happens to be empty; as a
    last resort widens the band to the full extent so a puzzle is always
    returned when the table is non-empty.

    `fetch_band(low, high) -> list[puzzle dict]` is injected so this stays
    DB-agnostic and unit-testable. Returns a puzzle dict or None (empty table).
    """
    rng = rng or random
    lo_ext, hi_ext = extent

    def clamp(v):
        if lo_ext is not None and v < lo_ext:
            v = lo_ext
        if hi_ext is not None and v > hi_ext:
            v = hi_ext
        return v

    for _ in range(max_tries):
        target = clamp(rng.gauss(float(player_rating), sigma))
        low = int(round(target - band))
        high = int(round(target + band))
        candidates = fetch_band(low, high)
        chosen = weighted_choice(candidates, player_rating, sigma, rng=rng)
        if chosen is not None:
            return chosen

    # Fallback: widen to the whole extent (still one indexed range scan) so a
    # non-empty table always yields a puzzle even if every random band missed.
    if lo_ext is not None and hi_ext is not None:
        candidates = fetch_band(lo_ext, hi_ext)
        return weighted_choice(candidates, player_rating, sigma, rng=rng)
    return None
