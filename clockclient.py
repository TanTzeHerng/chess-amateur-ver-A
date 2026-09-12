#!/usr/bin/env python3
"""FIDE-mode TIME MANAGER client: how long Chess Amateur "thinks" per move.

Used ONLY in FIDE-rated mode. It asks an external clock-model microservice
(the PolyForm-noncommercial ChessMimic clock model, run separately) how long a
human would think, applying the product owner's scaling rules around it:

  * Compute a FIXED scale from the INITIAL time control so that
    (base + 40*inc) maps to the Lichess 5-minute blitz reference (see
    timecontrol.clock_scale_factor). Already-Lichess-blitz controls use scale 1.
  * Feed the model clocks scaled DOWN by that factor.
  * Scale the model's predicted think time back UP by the same factor.
  * The scale is fixed after move 1 (caller passes the initial control).

Configuration:
  CLOCK_MODEL_URL   Base URL of the clock-model service. If unset/unreachable,
                    a SAFE FALLBACK heuristic is used so FIDE mode still works
                    (the app never breaks waiting on an optional service).
  CLOCK_MODEL_TIMEOUT  Per-request timeout seconds (default 4).

The service contract (Track 2 will implement it) — POST {CLOCK_MODEL_URL}/predict
with JSON:
  { "fen": str, "recent_moves": [uci,...], "rating": int,
    "player_clock": float, "opponent_clock": float, "increment": float }
returns JSON: { "thinking_time": float }  (seconds, in the model's blitz scale)
"""
import json
import os
import random
import urllib.request
import urllib.error

import timecontrol as TC

CLOCK_MODEL_URL = os.environ.get("CLOCK_MODEL_URL", "").rstrip("/")
CLOCK_MODEL_TIMEOUT = float(os.environ.get("CLOCK_MODEL_TIMEOUT", "4"))

# Chess Amateur's rating to present to the human-time model. The model was
# trained on the 2200-3500 bucket; we pass a representative rating so the
# think-time distribution matches strong-blitz human behavior.
MODEL_RATING = 2400


def _call_service(payload):
    """POST to the clock-model service; return thinking_time or None on any
    failure (so the caller can fall back)."""
    if not CLOCK_MODEL_URL:
        return None
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        CLOCK_MODEL_URL + "/predict", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=CLOCK_MODEL_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        tt = body.get("thinking_time")
        return float(tt) if tt is not None else None
    except (urllib.error.URLError, ValueError, OSError, TypeError):
        return None


def _fallback_thinking_time(model_player_clock, model_increment):
    """Safe, human-ish fallback when the model service is unavailable.

    Samples from the published ChessMimic blitz bucket distribution shape
    (short moves common, long tail), clamped to the (scaled) remaining clock.
    This keeps FIDE mode playable without the external model; it is NOT the
    neural net and is only used when CLOCK_MODEL_URL is unset/unreachable.
    """
    # Rough blitz think-time shape (seconds), skewed toward fast moves.
    r = random.random()
    if r < 0.30:
        t = random.uniform(0.3, 1.5)
    elif r < 0.65:
        t = random.uniform(1.5, 4.0)
    elif r < 0.88:
        t = random.uniform(4.0, 9.0)
    else:
        t = random.uniform(9.0, 20.0)
    # Never think longer than most of the remaining (scaled) clock.
    cap = max(0.2, model_player_clock * 0.4 + model_increment)
    return min(t, cap)


def thinking_time(fen, recent_moves, initial_base_seconds, initial_increment,
                  player_clock, opponent_clock):
    """Return Chess Amateur's real think time (seconds) for this move.

    initial_base_seconds / initial_increment: the INITIAL time control (used to
      fix the scale). If base is None (unlimited), no clock model applies and a
      modest constant is returned.
    player_clock / opponent_clock: LIVE remaining clocks (real seconds).
    """
    # Unlimited time: no human clock pressure to model. Use a small human-ish
    # pause so play doesn't feel instant in FIDE mode.
    if initial_base_seconds is None:
        return _fallback_thinking_time(60.0, initial_increment or 0)

    scale = TC.clock_scale_factor(initial_base_seconds, initial_increment)
    m_player, m_opp, m_inc = TC.model_input_clocks(
        scale, player_clock, opponent_clock, initial_increment)

    model_tt = _call_service({
        "fen": fen,
        "recent_moves": recent_moves or [],
        "rating": MODEL_RATING,
        "player_clock": m_player,
        "opponent_clock": m_opp,
        "increment": m_inc,
    })
    if model_tt is None:
        model_tt = _fallback_thinking_time(m_player, m_inc)

    real_tt = TC.scale_thinking_time_up(scale, model_tt)
    # Safety: never let the engine think past its own remaining clock (it would
    # flag). Leave a hair so increment keeps it alive.
    if player_clock is not None:
        real_tt = min(real_tt, max(0.1, player_clock - 0.1))
    return real_tt
