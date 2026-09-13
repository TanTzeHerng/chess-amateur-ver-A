#!/usr/bin/env python3
"""Rating systems for Chess Amateur: Elo (FIDE mode) and Glicko-2 (Rated mode).

Pure, framework-free functions so they can be unit-tested in isolation.

Modes:
  * FIDE-rated: standard Elo, player K-factor = 10 (always). The player has
    THREE FIDE ratings (blitz / rapid / classical), each starting at 1400; the
    game's time-class (by OUR definition) selects which one updates. Chess
    Amateur's FIDE ratings are FIXED (below) and never change.
  * Rated: Glicko-2, ONE player rating. Player and Chess Amateur both start at
    0. Chess Amateur's Rated rating is FIXED (never updates); only the player's
    rating/RD/volatility update. Glickman defaults: RD0=350, vol0=0.06, tau=0.5.
  * Casual: no rating change.

Time-class (OUR definition; used to pick which rating applies):
  T = base_seconds + 60*increment
  T <= 600            -> "blitz"
  600 < T < 3600      -> "rapid"
  else                -> "classical"
"""
import math

# --- Chess Amateur's FIXED FIDE ratings (engine doesn't play worse with less
#     time, so its blitz > rapid > classical). Computed per the spec:
#     rapid = 1500 + 60*log2((90+60*30)/(15+60*10))
#     blitz = 1500 + 60*log2((90+60*30)/(3+60*2))
CHESS_AMATEUR_FIDE = {
    "classical": 1500.0,
    "rapid": 1500 + 60 * (math.log((90 + 60 * 30) / (15 + 60 * 10)) / math.log(2)),
    "blitz": 1500 + 60 * (math.log((90 + 60 * 30) / (3 + 60 * 2)) / math.log(2)),
}

# Chess Amateur's FIXED Rated (Glicko-2) rating and its (assumed low) RD. Since
# its rating never changes and is effectively "known", we treat it as a very
# reliable opponent (small RD) in the player's Glicko-2 update.
CHESS_AMATEUR_RATED = 0.0
CHESS_AMATEUR_RATED_RD = 30.0

# Player starting values.
PLAYER_START_FIDE = 1400.0
PLAYER_START_RATED = 0.0

FIDE_K = 10.0

# Glicko-2 constants (Glickman's recommended defaults).
GLICKO2_START_RD = 350.0
GLICKO2_START_VOL = 0.06
GLICKO2_TAU = 0.5
GLICKO2_SCALE = 173.7178  # conversion between Glicko and Glicko-2 scales


def time_class(base_seconds, increment):
    """Classify a time control by OUR definition. Unlimited -> 'classical'."""
    if base_seconds is None:
        return "classical"  # unlimited time behaves like classical for rating
    t = base_seconds + 60 * increment
    if t <= 600:
        return "blitz"
    if t < 3600:
        return "rapid"
    return "classical"


# =========================================================================
# Elo (FIDE mode)
# =========================================================================

def elo_expected(player, opponent):
    """Expected score for `player` vs `opponent`."""
    return 1.0 / (1.0 + 10 ** ((opponent - player) / 400.0))


def elo_update(player_rating, opponent_rating, score, k=FIDE_K):
    """Return (new_rating, delta) after a game.

    score: 1.0 win, 0.5 draw, 0.0 loss. Standard Elo, K-factor default 10.
    """
    e = elo_expected(player_rating, opponent_rating)
    delta = k * (score - e)
    return player_rating + delta, delta


# =========================================================================
# Glicko-2 (Rated mode) — single game treated as one rating period.
# Implementation follows Glickman's "Example of the Glicko-2 system".
# =========================================================================

def glicko2_update(rating, rd, vol, opp_rating, opp_rd, score, tau=GLICKO2_TAU):
    """Update a player's Glicko-2 (rating, rd, vol) after ONE game vs an
    opponent. Returns (new_rating, new_rd, new_vol, delta_rating).

    rating/rd on the normal Glicko scale (e.g. 0..3000); vol ~0.06; score
    1/0.5/0. The opponent (Chess Amateur) is treated as fixed.
    """
    # Step 2: convert to the Glicko-2 scale (mu, phi).
    mu = (rating - 1500.0) / GLICKO2_SCALE
    phi = rd / GLICKO2_SCALE
    mu_j = (opp_rating - 1500.0) / GLICKO2_SCALE
    phi_j = opp_rd / GLICKO2_SCALE

    # Step 3: g(phi) and expected score E.
    def g(p):
        return 1.0 / math.sqrt(1.0 + 3.0 * p * p / (math.pi * math.pi))

    def E(m, mj, pj):
        return 1.0 / (1.0 + math.exp(-g(pj) * (m - mj)))

    g_j = g(phi_j)
    e_val = E(mu, mu_j, phi_j)

    # Step 4: estimated variance v of the rating based only on game outcomes.
    v = 1.0 / (g_j * g_j * e_val * (1.0 - e_val))

    # Step 5: estimated improvement in rating (delta).
    delta = v * g_j * (score - e_val)

    # Step 6: iterate to find the new volatility (Illinois algorithm).
    a = math.log(vol * vol)

    def f(x):
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return (num / den) - (x - a) / (tau * tau)

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        B = a - k * tau

    fA = f(A)
    fB = f(B)
    eps = 1e-6
    while abs(B - A) > eps:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0:
            A = B
            fA = fB
        else:
            fA = fA / 2.0
        B = C
        fB = fC
    new_vol = math.exp(A / 2.0)

    # Step 7: new pre-rating-period RD, then new phi and mu.
    phi_star = math.sqrt(phi * phi + new_vol * new_vol)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * g_j * (score - e_val)

    # Step 8: convert back to the Glicko scale.
    new_rating = GLICKO2_SCALE * new_mu + 1500.0
    new_rd = GLICKO2_SCALE * new_phi
    return new_rating, new_rd, new_vol, (new_rating - rating)


# =========================================================================
# Convenience: score from a game result relative to the human's color.
# =========================================================================

def display_ratings(user, mode, base_seconds, increment):
    """Return the (player_rating, bot_rating) pair to show next to the names.

    Depends on the game's MODE and TIME CLASS:
      * FIDE  -> (player's FIDE rating for time_class(base,inc),
                  CHESS_AMATEUR_FIDE[time_class])
      * Rated -> (player's Glicko-2 rated_rating, CHESS_AMATEUR_RATED)
      * Casual, or no account (guest) -> (None, None) meaning show no ratings.

    `user` is a user dict (as from Store.get_user_by_id) or None for a guest.
    Values are floats (frontend rounds for display); None means "hide".
    """
    if not user or mode == "casual":
        return None, None
    tclass = time_class(base_seconds, increment)
    if mode == "fide":
        player = {"blitz": user["fide_blitz"], "rapid": user["fide_rapid"],
                  "classical": user["fide_classical"]}[tclass]
        return float(player), float(CHESS_AMATEUR_FIDE[tclass])
    if mode == "rated":
        return float(user["rated_rating"]), float(CHESS_AMATEUR_RATED)
    return None, None


def format_delta(delta, suffix=None):
    """Format a numeric rating delta as a signed display string.

    A delta of exactly 0 renders as '+0' (per spec), NOT '+0.00'. Nonzero
    deltas keep two decimals with an explicit sign, e.g. '+6.40' / '-3.21'.
    `suffix` (e.g. 'FIDE classical') is appended when given.
    """
    if delta == 0:
        text = "+0"
    else:
        sign = "+" if delta > 0 else ""
        text = "%s%.2f" % (sign, delta)
    if suffix:
        text += " " + suffix
    return text


def time_control_bucket(base_seconds, increment):
    """Map a time control to a My-Games FILTER bucket.

    The filter offers EXACTLY five buckets (NO bullet):
      * 'unlimited' when base_seconds is None (no clock).
      * otherwise the class from time_class(base,inc): 'blitz'/'rapid'/'classical'.

    Note: 'custom' is ALSO a selectable filter bucket but is NOT returned here.
    The app only ever offers Custom or Unlimited time controls, so 'custom'
    means "any explicitly time-limited control" -- i.e. every game whose
    base_seconds is not None qualifies as 'custom' in addition to its
    blitz/rapid/classical class. The UI treats 'custom' as an OR alongside the
    derived class (a limited game matches both its class and 'custom'); this
    helper returns only the single derived class so callers can decide.
    """
    if base_seconds is None:
        return "unlimited"
    return time_class(base_seconds, increment)


def result_class(result, human_color):
    """Classify a finished game's result RELATIVE TO THE HUMAN.

    Returns 'win' / 'draw' / 'loss'. Used by the My Games result filter.
    """
    if result == "1/2-1/2":
        return "draw"
    score = human_score(result, human_color)
    return "win" if score == 1.0 else "loss"


def human_score(result, human_color):
    """Map a chess result string to the human's score (1/0.5/0).

    result: '1-0' (white wins), '0-1' (black wins), '1/2-1/2' (draw).
    """
    if result == "1/2-1/2":
        return 0.5
    white_won = (result == "1-0")
    human_is_white = (human_color == "white")
    return 1.0 if (white_won == human_is_white) else 0.0
